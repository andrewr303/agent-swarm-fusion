"""Bind the durable outbox to an explicitly supplied runtime.

Callbacks are adapters to actual exposed host contracts. This class does not
discover hidden tools, select models, invoke a fallback or accept worker results.
"""
from __future__ import annotations

from .mailbox import BoardError


class ExecutionController:
    def __init__(self, board, runtime, *, agent_id: str, run_id: str, owner: str):
        self.board, self.runtime = board, runtime
        self.agent_id, self.run_id, self.owner = agent_id, run_id, owner

    def start(self, bounds: dict | None = None) -> dict:
        capabilities = self.runtime.capabilities()
        if bounds is None:
            with self.board._lock:
                bounds = self.board._plan_unlocked(self.run_id).get("strategy", {}).get("bounds")
        return self.board.create_run(self.agent_id, self.run_id, capabilities["host"], capabilities, bounds)

    def dispatch_ready(self) -> list[dict]:
        outcomes = []
        for packet in self.board.dispatch_actions(self.agent_id, self.run_id):
            claimed = self.board.claim_dispatch(self.agent_id, packet["ticket_id"], self.owner)
            if not claimed["new_claim"]:
                continue
            result = {}
            try:
                result = self.runtime.dispatch(packet)
                outcomes.append(self.board.observe_runtime(self.agent_id, packet["ticket_id"], claimed["claim_token"], claimed["version"],
                    result["native_handle"], result.get("model") or "", "dispatched", "dispatch", {"reused": result.get("reused", False)}))
            except Exception as exc:
                outcomes.append(self.board.dispatch_uncertain(self.agent_id, packet["ticket_id"], claimed["claim_token"],
                    f"Dispatch reply is uncertain: {type(exc).__name__}: {exc}", result.get("native_handle", "")))
        return outcomes

    def observe(self, ticket_id: str, *, wait_ms: int = 10000) -> dict:
        with self.board._lock:
            ticket = self.board._runtime_unlocked(ticket_id)
            self.board._run_unlocked(ticket["run_id"], self.agent_id)
        if ticket["run_id"] != self.run_id or not ticket["native_handle"]:
            raise BoardError("observation requires this run's original runtime handle")
        if ticket["state"] not in ("dispatched", "started", "working", "waiting-input", "uncertain"):
            return self.board.runtime_status(self.agent_id, ticket_id)
        observation = self.runtime.wait(ticket["native_handle"], wait_ms)
        if observation.get("state") == "uncertain":
            return self.board.dispatch_uncertain(self.agent_id, ticket_id, ticket["claim_token"], observation.get("error", "runtime status uncertain"))
        state = observation["state"]
        try:
            result = self.board.observe_runtime(self.agent_id, ticket_id, ticket["claim_token"], ticket["version"],
                ticket["native_handle"], observation.get("model") or "", state, f"observe:{ticket['version'] + 1}",
                observation, observation.get("failure_kind", "worker" if state in ("failed", "cancelled") else ""))
        except BoardError as exc:
            return self.board.dispatch_uncertain(self.agent_id, ticket_id, ticket["claim_token"], str(exc))
        if state == "stopped" and isinstance(observation.get("report"), dict):
            # Submission creates a candidate; only the selected lead can accept it.
            return self.board.submit_runtime_result(self.agent_id, ticket_id, observation["report"])
        return result

    def cancel(self, ticket_id: str, note: str) -> dict:
        ticket = self.board.runtime_status(self.agent_id, ticket_id)
        if ticket["run_id"] != self.run_id or not ticket["native_handle"]:
            raise BoardError("cancellation requires this run's owned handle")
        observed = self.runtime.cancel(ticket["native_handle"])
        if observed.get("state") not in ("stopped", "cancelled", "failed"):
            raise BoardError("host did not confirm the original worker stopped")
        return self.board.reconcile_runtime(self.agent_id, ticket_id, ticket["native_handle"], True, note)
