#!/usr/bin/env python3
"""fusion/scripts/dashboard_server.py — localhost config dashboard backend.

Stdlib-only (http.server), no new dependencies, no build step. Binds to
127.0.0.1 ONLY — never 0.0.0.0 — because this serves live discovery of what
CLIs/models are configured on this machine and allows editing
config/defaults.env; it must never be reachable from the network.

Security posture (see python/fusion_swarm/config_store.py and discovery.py
for the layers this composes):
  - GET endpoints are read-only and side-effect-free.
  - Mutating endpoints (POST /api/config) require BOTH a per-process random
    session token (X-Fusion-Token header, embedded in the served page — never
    persisted to disk) AND an Origin header that matches this server's own
    origin. Neither alone is sufficient: a token leak without origin binding,
    or origin-checking without a token, are each bypassable in isolation.
  - Every JSON response is passed through a secrets-pattern redaction filter
    before serialization — defense in depth even though defaults.env itself
    documents no secrets (it's model/effort/timeout config only).
  - Discovery/config reads never block a request beyond the discovery
    module's own per-provider timeout ceiling (see discovery.PROBE_TIMEOUT).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

FUSION_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FUSION_ROOT / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for dashboard_schema

from fusion_swarm import adapter, config_store, discovery, modes_catalog, mode_roster, tui  # noqa: E402
from fusion_swarm.board import HiveBoard, BoardError, wait_for_board_change  # noqa: E402
from fusion_swarm.supervision import redact as redact_run  # noqa: E402
import dashboard_schema  # noqa: E402

STATIC_HTML = Path(__file__).resolve().parent / "dashboard_static.html"

SESSION_TOKEN = secrets.token_urlsafe(32)
HOST = "127.0.0.1"

aug = r"[_-]?"  # helper for the compound-word patterns below
# Deliberately NOT a bare \bkey\b or \btoken\b — this app's own data model
# uses a field literally named "key" to hold a config KEY NAME (e.g.
# "FUSION_AGY_MODEL"), not a secret; a bare-word match redacted every row in
# the config table. Only match field names that specifically look like they
# hold a credential.
_SECRET_KEY_RE = re.compile(
    rf"(?i)(api{aug}key|secret{aug}key|access{aug}token|auth{aug}token|"
    rf"client{aug}secret|credential|password|passwd)"
)


def _redact(obj):
    """Defense-in-depth: strip anything key/token/secret/credential-shaped
    out of any JSON payload before it leaves this process, even though the
    data models here (config_store/discovery) don't carry such fields today."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = "«redacted»"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port found near %d" % preferred)


def _providers_report() -> dict:
    live = adapter.metaloop_availability()
    detect_json = {}
    try:
        _, out, _ = adapter._bash([adapter._fs(adapter.DETECT_SH), "--json"], timeout=15)
        detect_json = json.loads(out.strip().splitlines()[-1])
    except Exception:
        pass
    roster = {}
    for p in (adapter.OPERATOR_PROVIDERS + adapter.WORKER_PROVIDERS + adapter.ADVISOR_PROVIDERS):
        meta = {k: v for k, v in adapter.PROVIDER_META.get(p, {}).items()}
        roster[p] = {
            **meta,
            "sessions": adapter.WORKER_SESSIONS.get(p, 1),
            "available": bool(live.get(p)),
            "presence": detect_json.get("metaloop", {}).get(p) or detect_json.get("providers", {}).get(p, "unknown"),
            "cache_age_seconds": discovery.cache_age_seconds(p),
            "selectable_as_worker": p in adapter.WORKER_PROVIDERS,
        }
    # copilot rides the base panel, not the MetaLoop roster, but the dashboard
    # should still show it since it's a real configured provider.
    if "copilot" not in roster:
        meta = {k: v for k, v in adapter.PROVIDER_META.get("copilot", {}).items()}
        roster["copilot"] = {
            **meta, "sessions": 1,
            "available": detect_json.get("providers", {}).get("copilot") == "available",
            "presence": detect_json.get("providers", {}).get("copilot", "unknown"),
            "cache_age_seconds": discovery.cache_age_seconds("copilot"),
            "selectable_as_worker": False,
        }
    return {"providers": roster, "correlation_groups": adapter.CORRELATION_GROUPS}


OPENCODE_FAMILY_NOTE = (
    "OpenCode blends two billing families: 'opencode-go/*' models ride the "
    "OpenCode Go subscription; anything else (e.g. 'meta/muse-*') is direct-API "
    "and key-dependent."
)


def _annotate_opencode(result: dict) -> dict:
    """Tag opencode models with their billing family + group label, server-side,
    so the browser never has to know the opencode-go/ convention."""
    if result.get("provider") != "opencode":
        return result
    out = dict(result)
    fams = []
    for m in result.get("models", []):
        fam = discovery.opencode_model_family(m)
        fams.append({
            "model": m, "family": fam,
            "group": "Subscription (opencode-go)" if fam == "subscription"
                     else "Direct API (billed per use)",
        })
    out["models_annotated"] = fams
    out["family_note"] = OPENCODE_FAMILY_NOTE
    return out


def _discovery_report(provider: str | None) -> dict:
    """CACHE-ONLY model report — never spawns a probe (initial-load safe). Use
    POST /api/discovery/refresh to re-probe one provider live."""
    if provider:
        return {provider: _annotate_opencode(discovery.cached(provider))}
    return {p: _annotate_opencode(r) for p, r in discovery.cached_all().items()}


def _schema_report() -> dict:
    """The declarative control schema + current effective values + provenance.
    The single source the HTML renders every control from."""
    effective = {v["key"]: v for v in config_store.all_effective()}
    return {
        "schema": dashboard_schema.build_schema(),
        "groups": dashboard_schema.group_order(),
        "values": effective,
        "precedence": "explicit env > per-mode key > defaults.env > plugin config > baked default",
        "caveat": config_store.PLUGIN_CONFIG_CAVEAT,
        "opencode_family_note": OPENCODE_FAMILY_NOTE,
    }


def _modes_resolved_report() -> dict:
    return {"modes": mode_roster.resolve_modes()}


def _refresh_provider(provider: str) -> dict:
    """The ONLY new mutating endpoint's worker: probe ONE provider live. A
    failure returns honest degraded/missing status; it never fabricates a
    selectable verified model list."""
    if provider not in discovery.PROVIDER_PROBES:
        raise config_store.ConfigError(f"unknown provider '{provider}'")
    return _annotate_opencode(discovery.discover(provider, force=True))


def _run_binding():
    path, lead, run = (os.environ.get(key) for key in ("FUSION_BOARD", "FUSION_LEAD", "FUSION_RUN"))
    if not all((path, lead, run)):
        return None
    if not Path(path).is_absolute() or not Path(path).is_file():
        raise BoardError("FUSION_BOARD must identify an existing absolute board path")
    return path, lead, run


def _run_report(export=False):
    binding = _run_binding()
    if not binding:
        return {"available": False, "reason": "Set FUSION_BOARD, FUSION_LEAD and FUSION_RUN to supervise a prepared execution run."}
    path, lead, run = binding
    with HiveBoard(path) as board:
        result = board.export_run(lead, run) if export else board.supervision(lead, run)
    return {"available": True, **redact_run(result)}


def _diff_report(task_id: str | None = None, workspace: str | None = None) -> dict:
    ws_candidate = workspace or os.environ.get("FUSION_WORKSPACE")
    if not ws_candidate:
        binding = _run_binding()
        if binding:
            board_file = Path(binding[0]).resolve(strict=False)
            ws_candidate = str(board_file.parent.parent if board_file.parent.name == ".fusion" else board_file.parent)
        else:
            ws_candidate = str(FUSION_ROOT)
    ws_path = Path(ws_candidate).resolve(strict=False)
    diff_text = tui.collect_workspace_diff(ws_path)
    return {
        "available": True,
        "task_id": task_id or "",
        "workspace": str(ws_path),
        "diff": diff_text,
    }


_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class Handler(BaseHTTPRequestHandler):
    server_version = "FusionDashboard/1.0"

    def log_message(self, fmt, *args):  # quieter default logging
        sys.stderr.write("[dashboard] " + (fmt % args) + "\n")

    def _host_ok(self) -> bool:
        """Pillar 1A: Defeat DNS-rebinding by validating the Host header."""
        raw_host = (self.headers.get("Host") or "").strip()
        if not raw_host:
            return True
        if raw_host.startswith("["):
            host_only = raw_host.split("]")[0] + "]"
        else:
            host_only = raw_host.split(":")[0]
        return host_only.lower() in _ALLOWED_HOSTS

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # same-origin navigations/curl send no Origin header
        allowed = {f"http://{HOST}:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        return origin in allowed

    def _send_json(self, payload, status=200):
        body = json.dumps(_redact(payload), indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text: str, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, once: bool = False):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close" if once else "keep-alive")
        self.end_headers()
        binding = _run_binding()
        board_path = Path(binding[0]).resolve(strict=False) if binding else None
        last_gen = None
        try:
            initial = json.dumps(_redact(_run_report()), ensure_ascii=False)
            self.wfile.write(f"event: snapshot\ndata: {initial}\n\n".encode("utf-8"))
            self.wfile.flush()
            if once:
                self.close_connection = True
                return
            while True:
                if board_path is not None:
                    res = wait_for_board_change(f"{board_path}.wakeup", timeout=15.0, last_gen=last_gen)
                    last_gen = res.get("generation", last_gen)
                    if res.get("changed"):
                        payload = json.dumps(_redact(_run_report()), ensure_ascii=False)
                        self.wfile.write(f"event: board_wakeup\ndata: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        continue
                else:
                    time.sleep(15.0)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if not self._host_ok():
            self._send_json({"error": "misdirected request: invalid Host header"}, status=421)
            return
        if not self._origin_ok():
            self._send_json({"error": "origin not allowed"}, status=403)
            return
        try:
            if parsed.path == "/":
                html = STATIC_HTML.read_text(encoding="utf-8")
                html = html.replace("__FUSION_TOKEN__", SESSION_TOKEN)
                self._send_html(html)
            elif parsed.path == "/api/providers":
                self._send_json(_providers_report())
            elif parsed.path == "/api/schema":
                self._send_json(_schema_report())
            elif parsed.path == "/api/discovery":
                provider = (qs.get("provider") or [None])[0]
                self._send_json(_discovery_report(provider))
            elif parsed.path == "/api/modes/resolved":
                self._send_json(_modes_resolved_report())
            elif parsed.path in ("/api/run", "/api/run/export"):
                self._send_json(_run_report(export=parsed.path.endswith("/export")))
            elif parsed.path == "/api/diff":
                task_id = (qs.get("task_id") or [None])[0]
                ws = (qs.get("workspace") or [None])[0]
                self._send_json(_diff_report(task_id=task_id, workspace=ws))
            elif parsed.path == "/api/events":
                once = (qs.get("once") or ["0"])[0] in ("1", "true", "yes")
                self._send_sse(once=once)
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as exc:  # never leak a stack trace to the browser
            self._send_json({"error": str(exc)}, status=500)

    def do_POST(self):
        if not self._host_ok():
            self._send_json({"error": "misdirected request: invalid Host header"}, status=421)
            return
        if not self.headers.get("Origin") or not self._origin_ok():
            self._send_json({"error": "origin not allowed"}, status=403)
            return
        token = self.headers.get("X-Fusion-Token", "")
        if not secrets.compare_digest(token, SESSION_TOKEN):
            self._send_json({"error": "missing or invalid session token"}, status=403)
            return
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send_json({"error": "invalid JSON body"}, status=400)
            return
        try:
            if parsed.path == "/api/config":
                key = str(data.get("key", ""))
                value = str(data.get("value", ""))
                # Re-validate against the SAME schema the UI renders from before
                # the (unchanged) config_store performs the sole write. A key not
                # in the schema — i.e. not in the allowlist — is refused here too.
                ok, err, is_custom = dashboard_schema.validate_value(key, value)
                if not ok:
                    self._send_json({"error": err}, status=400)
                    return
                result = config_store.set_value(key, value)
                self._send_json({"ok": True, "result": result, "custom": is_custom})
            elif parsed.path == "/api/discovery/refresh":
                provider = str(data.get("provider", ""))
                self._send_json({"ok": True, "result": _refresh_provider(provider)})
            elif parsed.path == "/api/run/control":
                if not self.headers.get("Origin"):
                    self._send_json({"error": "run control requires the dashboard origin"}, status=403)
                    return
                binding = _run_binding()
                if not binding:
                    raise BoardError("no execution run is bound to this dashboard")
                if not isinstance(data, dict) or set(data) != {"state", "note", "expected_version"}:
                    raise BoardError("control needs state, note and expected_version")
                path, lead, run = binding
                with HiveBoard(path) as board:
                    result = board.control_run(lead, run, **data)
                self._send_json({"ok": True, "result": redact_run(result)})
            else:
                self._send_json({"error": "not found"}, status=404)
        except (config_store.ConfigError, BoardError) as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)


def main():
    preferred = int(os.environ.get("FUSION_DASHBOARD_PORT", "8765"))
    port = _free_port(preferred)
    server = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"
    print(f"[fusion] dashboard listening on {url}")
    print(f"[fusion] session token (do not share): {SESSION_TOKEN[:8]}...")
    if os.environ.get("FUSION_DASHBOARD_NO_OPEN") != "1":
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[fusion] dashboard stopped")


if __name__ == "__main__":
    main()
