"""Read-only reviews bound to the exact submitted artifact version."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .mailbox import BoardError, envelope_fingerprint, json_object, positive_int
from .scope import file_digest, snapshot
from .sidekick import scoped_path, text

CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    task_id TEXT NOT NULL, attempt INTEGER NOT NULL, digest TEXT NOT NULL, artifact TEXT NOT NULL,
    created_at REAL NOT NULL, PRIMARY KEY(task_id,attempt)
);
CREATE TABLE IF NOT EXISTS candidate_bindings (
    task_id TEXT NOT NULL, attempt INTEGER NOT NULL, candidate_of TEXT NOT NULL,
    candidate_attempt INTEGER NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(task_id,attempt)
);
CREATE TABLE IF NOT EXISTS candidate_reviews (
    task_id TEXT NOT NULL, attempt INTEGER NOT NULL, reviewer TEXT NOT NULL, candidate_of TEXT NOT NULL,
    candidate_attempt INTEGER NOT NULL, digest TEXT NOT NULL, stage TEXT NOT NULL,
    verdict TEXT NOT NULL, findings TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(task_id,attempt)
);
CREATE TABLE IF NOT EXISTS dependency_versions (
    task_id TEXT NOT NULL, attempt INTEGER NOT NULL, dependency TEXT NOT NULL,
    dependency_attempt INTEGER NOT NULL, digest TEXT NOT NULL, artifact TEXT NOT NULL,
    PRIMARY KEY(task_id,attempt,dependency)
);
"""


class CandidateMixin:
    def _invalidate_candidate_reviews_unlocked(self, producer: dict) -> None:
        reviews = [item for item in self._plan_tasks_unlocked(producer["id"]) if item["brief"].get("candidate_of") == producer["id"]]
        for item in reviews:
            active = self._conn.execute("SELECT 1 FROM runtime_jobs WHERE task_id=? AND state IN ('requested','claimed','dispatched','started','working','waiting-input','uncertain')", (item["id"],)).fetchone()
            if item["state"] == "running" or active:
                raise BoardError("stop the original candidate reviewers before revising their producer")
        for item in reviews:
            if item["state"] in ("review", "accepted", "blocked", "escalated"):
                note = "Candidate revision invalidates the previous review; inspect the next submitted digest"
                self._conn.execute("UPDATE work_items SET state='queued',report=NULL,review_note=?,updated_at=? WHERE id=?", (note, time.time(), item["id"]))
                self._task_event_unlocked(item["id"], producer["created_by"], "queued", item["attempt"], note)
                self._notify_task(sender=producer["created_by"], recipient=item["assigned_to"], body=note, kind="decision", task_id=item["id"],
                    payload={"decision": "candidate-invalidated", "candidate_of": producer["id"]}, attempt=item["attempt"],
                    idempotency_key=f"candidate-invalidation:{producer['id']}:{producer['attempt']}:{item['id']}:{item['attempt']}")

    def _artifact_unlocked(self, item: dict) -> dict:
        files = {}
        root = Path(item["brief"]["workspace"])
        exclusions = [str(self.path), str(self.path) + "-wal", str(self.path) + "-shm", str(self.jsonl_path), str(Path(self.path).parent / "runtime")]
        for raw in item["brief"]["writes"]:
            path = scoped_path(str(root), raw)
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                tree = snapshot(str(path), exclusions)
                files.update({(Path(relative) / name).as_posix(): metadata for name, metadata in tree["files"].items()})
            elif path.is_file():
                files[relative] = {"kind": "file", "sha256": file_digest(path)}
            else:
                files[relative] = {"kind": "absent"}
        return {"task_id": item["id"], "attempt": item["attempt"], "report": item["report"], "files": files}

    def _publish_candidate_unlocked(self, item: dict) -> None:
        if item["state"] != "review" or item["brief"].get("candidate_of"):
            return
        artifact = self._artifact_unlocked(item)
        digest = envelope_fingerprint(artifact=artifact)
        self._conn.execute("INSERT OR REPLACE INTO candidates(task_id,attempt,digest,artifact,created_at) VALUES (?,?,?,?,?)",
            (item["id"], item["attempt"], digest, json_object(artifact, "candidate"), time.time()))

    def _candidate_unlocked(self, task_id: str) -> dict:
        item = self._task_unlocked(task_id)
        row = self._conn.execute("SELECT * FROM candidates WHERE task_id=? AND attempt=?", (task_id, item["attempt"])).fetchone()
        if row is None or item["state"] not in ("review", "accepted"):
            raise BoardError("producer has no submitted candidate for this attempt")
        candidate = dict(row)
        candidate["artifact"] = json.loads(candidate["artifact"])
        current = envelope_fingerprint(artifact=self._artifact_unlocked(item))
        candidate["current"] = current == candidate["digest"]
        candidate["current_digest"] = current
        return candidate

    def candidate_status(self, agent_id: str, task_id: str) -> dict:
        with self._lock:
            item = self._task_unlocked(task_id)
            reviews = [t for t in self._plan_tasks_unlocked(task_id) if t["brief"].get("candidate_of") == task_id and t["assigned_to"] == agent_id]
            if agent_id not in (item["created_by"], item["assigned_to"]) and not reviews:
                raise BoardError("candidate is private to its lead, owner and selected reviewers")
            return self._candidate_unlocked(task_id)

    def _dependencies_ready_unlocked(self, brief: dict) -> bool:
        for dependency in brief["dependencies"]:
            item = self._task_unlocked(dependency)
            if dependency == brief.get("candidate_of"):
                if item["state"] not in ("review", "accepted"):
                    return False
                try:
                    if not self._candidate_unlocked(dependency)["current"]:
                        return False
                except BoardError:
                    return False
            elif item["state"] != "accepted":
                return False
            elif self._conn.execute("SELECT 1 FROM candidates WHERE task_id=? AND attempt=?", (dependency, item["attempt"])).fetchone():
                if not self._candidate_unlocked(dependency)["current"]:
                    return False
        return True

    def _bind_dependencies_unlocked(self, item: dict, attempt: int) -> list[dict]:
        bindings = []
        for dependency in item["brief"]["dependencies"]:
            if dependency == item["brief"].get("candidate_of"):
                continue
            parent = self._task_unlocked(dependency)
            if not self._conn.execute("SELECT 1 FROM candidates WHERE task_id=? AND attempt=?", (dependency, parent["attempt"])).fetchone():
                continue
            candidate = self._candidate_unlocked(dependency)
            if not candidate["current"]:
                raise BoardError("accepted dependency artifact changed")
            self._conn.execute("INSERT INTO dependency_versions VALUES (?,?,?,?,?,?)",
                (item["id"], attempt, dependency, parent["attempt"], candidate["digest"], json_object(candidate["artifact"], "artifact")))
            bindings.append({"task_id": dependency, "attempt": parent["attempt"], "digest": candidate["digest"]})
        return bindings

    def _check_dependency_inputs_unlocked(self, item: dict) -> None:
        from .scope import within
        root = Path(item["brief"]["workspace"])
        writable = [scoped_path(str(root), path).relative_to(root).as_posix() for path in item["brief"]["writes"]]
        for row in self._conn.execute("SELECT * FROM dependency_versions WHERE task_id=? AND attempt=?", (item["id"], item["attempt"])).fetchall():
            parent = self._task_unlocked(row["dependency"])
            if parent["state"] != "accepted" or parent["attempt"] != row["dependency_attempt"]:
                raise BoardError("dependency acceptance changed during execution")
            before = json.loads(row["artifact"])["files"]
            current = self._artifact_unlocked(parent)["files"]
            for name in set(before) | set(current):
                if not any(within(name, scope) for scope in writable) and before.get(name) != current.get(name):
                    raise BoardError("read-only dependency input changed during execution: " + name)

    def _bind_candidate_unlocked(self, item: dict, attempt: int) -> dict | None:
        producer = item["brief"].get("candidate_of")
        if not producer:
            return None
        candidate = self._candidate_unlocked(producer)
        if not candidate["current"]:
            raise BoardError("candidate changed after submission; request revision")
        binding = {"candidate_of": producer, "candidate_attempt": candidate["attempt"], "digest": candidate["digest"]}
        self._conn.execute("INSERT INTO candidate_bindings(task_id,attempt,candidate_of,candidate_attempt,digest) VALUES (?,?,?,?,?)",
            (item["id"], attempt, producer, binding["candidate_attempt"], binding["digest"]))
        return binding

    def record_candidate_review(self, agent_id: str, task_id: str, attempt: int, digest: str, verdict: str, findings: list[dict]) -> dict:
        positive_int(attempt, "attempt")
        if verdict not in ("approve", "request_changes") or not isinstance(findings, list):
            raise BoardError("review needs approve/request_changes and concrete findings")
        for finding in findings:
            if (not isinstance(finding, dict) or set(finding) - {"id", "severity", "summary", "evidence", "resolution"}
                or finding.get("severity") not in ("critical", "major", "minor", "info")):
                raise BoardError("invalid review finding")
            for field in ("id", "summary", "evidence"):
                text(finding.get(field), "finding." + field)
            if verdict == "approve" and finding["severity"] in ("critical", "major") and not finding.get("resolution"):
                raise BoardError("unresolved critical or major objections prevent approval")
            if finding.get("resolution"):
                resolution = finding["resolution"]
                if not isinstance(resolution, dict) or set(resolution) != {"summary", "evidence"}:
                    raise BoardError("objection resolution needs summary and evidence")
                text(resolution["summary"], "resolution.summary")
                text(resolution["evidence"], "resolution.evidence")
        if len({finding["id"] for finding in findings}) != len(findings):
            raise BoardError("duplicate finding id")
        with self._transaction():
            item = self._task_unlocked(task_id)
            if item["assigned_to"] != agent_id or item["attempt"] != attempt or item["state"] not in ("running", "review", "accepted"):
                raise BoardError("only the current selected reviewer may record findings")
            binding = self._conn.execute("SELECT * FROM candidate_bindings WHERE task_id=? AND attempt=?", (task_id, attempt)).fetchone()
            if not binding or binding["digest"] != digest:
                raise BoardError("review digest differs from the attempt's bound candidate")
            candidate = self._candidate_unlocked(binding["candidate_of"])
            if candidate["attempt"] != binding["candidate_attempt"] or not candidate["current"] or candidate["digest"] != digest:
                raise BoardError("stale candidate review")
            prior = self._conn.execute("SELECT * FROM candidate_reviews WHERE task_id=? AND attempt=?", (task_id, attempt)).fetchone()
            encoded = json.dumps(findings, sort_keys=True, ensure_ascii=False)
            if prior:
                if prior["verdict"] != verdict or prior["findings"] != encoded:
                    raise BoardError("review attempt already has different findings; request another attempt")
                return dict(prior)
            self._conn.execute("INSERT INTO candidate_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, attempt, agent_id, binding["candidate_of"], binding["candidate_attempt"], digest,
                 item["brief"].get("review_stage", "artifact"), verdict, encoded, time.time()))
            return dict(self._conn.execute("SELECT * FROM candidate_reviews WHERE task_id=? AND attempt=?", (task_id, attempt)).fetchone())

    def _check_candidate_acceptance_unlocked(self, item: dict) -> None:
        if item["brief"].get("candidate_of"):
            review = self._conn.execute("SELECT * FROM candidate_reviews WHERE task_id=? AND attempt=?", (item["id"], item["attempt"])).fetchone()
            if not review or review["verdict"] != "approve":
                raise BoardError("candidate reviewer must record an approving evidenced review")
            candidate = self._candidate_unlocked(review["candidate_of"])
            if candidate["digest"] != review["digest"] or not candidate["current"]:
                raise BoardError("stale candidate approval")
            return
        row = self._conn.execute("SELECT 1 FROM candidates WHERE task_id=? AND attempt=?", (item["id"], item["attempt"])).fetchone()
        if not row:
            return  # Older boards retain their original lead-review contract.
        candidate = self._candidate_unlocked(item["id"])
        if not candidate["current"]:
            raise BoardError("candidate changed after submission; inspect and request revision")
        plan = self._plan_for_task_unlocked(item["id"])
        if plan is None:
            return
        selected = [job for job in plan["jobs"] if job.get("candidate_of") == item["id"]]
        if not selected:
            return
        required = plan.get("strategy", {}).get("review", {}).get("required_stages", [])
        for stage in required:
            valid = False
            for job in selected:
                if job.get("review_stage", "artifact") != stage:
                    continue
                review_item = self._task_unlocked(job["id"])
                review = self._conn.execute("SELECT * FROM candidate_reviews WHERE task_id=? AND attempt=?", (job["id"], review_item["attempt"])).fetchone()
                valid |= bool(review and review_item["state"] == "accepted" and review["verdict"] == "approve" and review["digest"] == candidate["digest"])
            if not valid:
                raise BoardError("candidate lacks accepted current review stage: " + stage)
