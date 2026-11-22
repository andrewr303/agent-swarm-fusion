"""Loopback HTTP subset of the vercel-board protocol on top of `BoardStore` (design/v4-spec.md section 3).

stdlib `ThreadingHTTPServer` bound to loopback only. The stock `vercel board --url ...` / `vb` CLIs can talk to it:
discovery document (audience == URL the client used), bearer token (generated per server, also written to the stock
CLI's loopback token file), `x-board-profile` / `x-board-can` headers, the route table of b3 section 6, long-poll and
SSE `/events`. Everything outside the local subset answers 501 `not_supported`.

Security notes: tokens are never logged (request logging is disabled entirely); the `Host` header must be loopback
(DNS-rebinding guard); the bind host must be loopback; request bodies are capped at 8 MiB.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import VERSION
from .model import EVENTS_WAIT_MAX_S, PERMISSIONS, Caller, VBoardError, normalize_profile
from .store import BoardStore

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
BODY_LIMIT = 8 * 1024 * 1024
CAN_HEADER_MAX = 4000
FUSION_EXTENSION = {"conditional_needs": True, "priority": True, "defer": True, "batch_max": 100}

# Path families that exist in the reference protocol but not in the local engine -> 501 not_supported.
_UNSUPPORTED_FIRST = {"grants", "schemas", "schedules", "attachments", "watchers", "wake", "inbox", "sync", "sources",
                      "webhooks", "avatars", "openapi.json", "links"}
_UNSUPPORTED_MESSAGE_ACTIONS = {"sync", "typing"}
_PROFILE_ITEM = re.compile(r"^(?:\"(?P<q>[^\"]*)\"|(?P<t>[A-Za-z*][A-Za-z0-9:/_.*-]*))\s*(?P<params>(?:;.*)?)$")
_PARAM = re.compile(r";\s*([A-Za-z*][A-Za-z0-9_.*-]*)(?:=(\"[^\"]*\"|[^;\s]+))?")


# --------------------------------------------------------------------------------------------- header parsing
def parse_profile_header(raw: str | None) -> tuple[str | None, str | None]:
    """RFC 8941 item `name` or `name;avatar="https://..."` -> (profile or None, avatar or None).

    Invalid -> VBoardError invalid_profile (400)."""
    if raw is None or not raw.strip():
        return None, None
    text = raw.strip()
    m = _PROFILE_ITEM.match(text)
    if not m:
        raise VBoardError("invalid_profile", "x-board-profile is a profile name, optionally with ;avatar=\"https://...\"")
    name = m.group("q") if m.group("q") is not None else m.group("t")
    avatar = None
    for key, val in _PARAM.findall(m.group("params") or ""):
        if key == "avatar":
            if not val.startswith('"') or not val.endswith('"') or len(val) < 2:
                raise VBoardError("invalid_profile", "the avatar parameter is a quoted https URL")
            avatar = val[1:-1]
            if not (avatar.startswith("https://") or avatar.startswith("http://localhost")
                    or avatar.startswith("http://127.0.0.1")):
                raise VBoardError("invalid_profile", "the avatar must be an https URL")
    return normalize_profile(name), avatar


def parse_can_header(raw: str | None) -> list[tuple[str, frozenset[str]]] | None:
    """`pattern=perm,perm;pattern=perm` (or bare `perm,perm` = `*`) -> [(pattern, perms)]; None when absent."""
    if raw is None or not raw.strip():
        return None
    if len(raw) > CAN_HEADER_MAX:
        raise VBoardError("invalid_request", f"x-board-can is at most {CAN_HEADER_MAX} characters")
    entries: list[tuple[str, frozenset[str]]] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        pattern, sep, perms_raw = part.partition("=")
        if not sep:
            pattern, perms_raw = "*", part
        perms: set[str] = set()
        for perm in perms_raw.split(","):
            perm = perm.strip().lower()
            if not perm:
                continue
            if perm == "sync":          # hosted-only permission, accepted and ignored
                continue
            if perm not in PERMISSIONS:
                raise VBoardError("invalid_request", f"x-board-can: unknown permission {perm[:20]!r}")
            perms.add(perm)
        entries.append((pattern.strip(), frozenset(perms)))
    return entries


def _narrow(entries: list[tuple[str, frozenset[str]]] | None, channel: str | None) -> frozenset[str] | None:
    """Effective permission set for a request touching `channel` (None = unnarrowed)."""
    if entries is None:
        return None
    if channel is None:
        out: set[str] = set()
        for _pat, perms in entries:
            out |= perms
        return frozenset(out)
    out2: set[str] = set()
    for pat, perms in entries:
        if pat == "*" or pat == channel or (pat.endswith("/") and (channel.startswith(pat) or channel == pat[:-1])):
            out2 |= perms
    return frozenset(out2)


# --------------------------------------------------------------------------------------------- body/query helpers
def _bad(msg: str) -> VBoardError:
    return VBoardError("invalid_request", msg)


def _q_one(q: dict[str, list[str]], key: str) -> str | None:
    vals = q.get(key)
    return vals[-1] if vals else None


def _q_int(q: dict[str, list[str]], key: str) -> int | None:
    val = _q_one(q, key)
    if val is None or val == "":
        return None
    try:
        return int(val)
    except ValueError:
        raise _bad(f"{key}: expected a whole number") from None


def _q_num(q: dict[str, list[str]], key: str) -> float | None:
    val = _q_one(q, key)
    if val is None or val == "":
        return None
    try:
        out = float(val)
    except ValueError:
        raise _bad(f"{key}: expected a number") from None
    if out != out:
        raise _bad(f"{key}: expected a number")
    return out


def _q_bool(q: dict[str, list[str]], key: str) -> bool:
    val = _q_one(q, key)
    return val is not None and val.lower() in ("", "1", "true", "yes", "on") and key in q


def _b_get(body: dict, key: str, kind: type | tuple, *, required: bool = False) -> Any:
    if key not in body or body[key] is None:
        if required:
            raise _bad(f"{key} is required")
        return None
    val = body[key]
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if isinstance(val, bool) and bool not in kinds:
        raise _bad(f"{key} has the wrong type")
    if not isinstance(val, kinds):
        raise _bad(f"{key} has the wrong type")
    return val


def _b_int(body: dict, key: str, *, required: bool = False) -> int | None:
    val = _b_get(body, key, int, required=required)
    return val


def _b_num(body: dict, key: str) -> float | None:
    val = _b_get(body, key, (int, float))
    return None if val is None else float(val) if not isinstance(val, int) else val


def _strip_outcome(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("outcome", None)
    return doc


# --------------------------------------------------------------------------------------------- server
class BoardHttpServer:
    def __init__(self, store: BoardStore, *, host: str = "127.0.0.1", port: int = 0, mount: str = "/api/board",
                 auth: bool = True, token: str | None = None, user: str | None = None,
                 ping_s: float = 15.0, stream_max_s: float = 280.0, hold_s: float = 25.0,
                 idle_timeout_s: float = 5.0):
        if host not in LOOPBACK_HOSTS:
            raise ValueError(f"vboard http binds to loopback only, not {host!r}")
        mount = "/" + mount.strip("/") if mount.strip("/") else ""
        self.store = store
        self.host = host
        self.mount = mount
        self.auth = bool(auth)
        self.token = token or secrets.token_urlsafe(32)
        self.principal = Caller.local(user=user).principal
        self.user = self.principal.rsplit(":", 1)[-1]
        self.ping_s = float(ping_s)
        self.stream_max_s = float(stream_max_s)
        self.hold_s = float(hold_s)
        self.idle_timeout_s = float(idle_timeout_s)
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._token_files: list[Path] = []
        self._lock = threading.Lock()

        server_self = self

        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            request_queue_size = 128
            address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

            def handle_error(self, request, client_address):  # noqa: D401 - silence tracebacks on dropped clients
                return

        handler = type("BoardHandler", (_Handler,), {"app": server_self, "timeout": self.idle_timeout_s or None})
        bind_host = "::1" if host == "::1" else "127.0.0.1" if host == "localhost" else host
        self._httpd = _Server((bind_host, port), handler)
        self.port = self._httpd.server_address[1]
        shown = f"[{host}]" if ":" in host else host
        self.url = f"http://{shown}:{self.port}{self.mount}"

    # -- lifecycle
    def start(self) -> "BoardHttpServer":
        if self._thread is None:
            self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                            name="vboard-http", daemon=True)
            self._thread.start()
        return self

    def stop(self, *, remove_token_files: bool = True) -> None:
        self._stopping.set()
        thread = self._thread
        if thread is not None:
            self._httpd.shutdown()
            thread.join(timeout=5)
            self._thread = None
        try:
            self._httpd.server_close()
        except OSError:
            pass
        if remove_token_files:
            for path in self._token_files:
                try:
                    path.unlink()
                except OSError:
                    pass
            self._token_files = []

    def __enter__(self) -> "BoardHttpServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- token file (the stock CLI auto-loads this for loopback --url / BOARD_URL)
    def write_token_file(self, config_home: str | os.PathLike | None = None) -> Path:
        base = Path(config_home) if config_home else (
            Path(os.environ["XDG_CONFIG_HOME"]) if os.environ.get("XDG_CONFIG_HOME")
            else Path.home() / ".config")
        directory = base / "board" / "local"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:32] + ".json"
        path = directory / name
        payload = json.dumps({"url": self.url, "token": self.token, "handle": self.user})
        tmp = directory / f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        if path not in self._token_files:
            self._token_files.append(path)
        return path

    # -- discovery
    def discovery(self, url: str) -> dict:
        doc: dict[str, Any] = {"version": VERSION, "url": url, "audience": url, "team_id": None,
                               "project_id": None, "sync": [], "wake_by_path": False,
                               "fusion": dict(FUSION_EXTENSION)}
        if self.store.name:
            doc["name"] = self.store.name
        return doc


def serve(store: BoardStore, **kw: Any) -> BoardHttpServer:
    """Construct and start a loopback server."""
    return BoardHttpServer(store, **kw).start()


# --------------------------------------------------------------------------------------------- request handler
class _Handler(BaseHTTPRequestHandler):
    app: BoardHttpServer            # injected by the subclass created in BoardHttpServer.__init__
    protocol_version = "HTTP/1.1"
    server_version = "fusion-vboard"
    sys_version = ""

    # request logging is disabled: nothing about a request (and never a token) is written anywhere
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    # -- verbs
    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    # -- plumbing
    def _handle(self, method: str) -> None:
        self._body_consumed = False
        self._streaming = False
        try:
            self._dispatch(method)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True
        except VBoardError as exc:
            self._send_error(exc)
        except sqlite3.OperationalError:
            self._send_error(VBoardError("unavailable", "the board database is busy; retry"))
        except Exception:  # noqa: BLE001 - never leak internals over the wire
            self._send_error(VBoardError("internal", "internal error"))

    def _send_json(self, status: int, doc: Any, extra: dict[str, str] | None = None) -> None:
        data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if not self._body_consumed and int(self.headers.get("Content-Length") or 0) > 0:
            self.close_connection = True      # an unread request body would corrupt keep-alive framing
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_error(self, exc: VBoardError) -> None:
        if self._streaming:
            self.close_connection = True
            return
        extra: dict[str, str] = {}
        if exc.retry_after is not None:
            extra["Retry-After"] = str(max(1, int(exc.retry_after + 0.999)))
        if exc.code == "unauthorized":
            extra["WWW-Authenticate"] = "Bearer"
        try:
            self._send_json(exc.status, exc.to_http(), extra)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True

    def _read_body(self) -> dict:
        self._body_consumed = True
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            raw = self._read_chunked()
        else:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise _bad("Content-Length is not a number") from None
            if length < 0:
                raise _bad("Content-Length is negative")
            if length > BODY_LIMIT:
                self.close_connection = True
                raise VBoardError("too_large", "request body is limited to 8 MiB")
            raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _bad("the request body is not valid JSON") from None
        if not isinstance(doc, dict):
            raise _bad("the request body must be a JSON object")
        return doc

    def _read_chunked(self) -> bytes:
        out = bytearray()
        while True:
            line = self.rfile.readline(1024).strip()
            try:
                size = int(line.split(b";", 1)[0], 16)
            except ValueError:
                self.close_connection = True
                raise _bad("bad chunked encoding") from None
            if size == 0:
                while self.rfile.readline(1024).strip():
                    pass
                return bytes(out)
            if len(out) + size > BODY_LIMIT:
                self.close_connection = True
                raise VBoardError("too_large", "request body is limited to 8 MiB")
            out += self.rfile.read(size)
            self.rfile.readline(4)

    # -- host / auth
    def _host_ok(self) -> tuple[bool, str]:
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return False, ""
        if raw.startswith("["):
            end = raw.find("]")
            hostname = raw[1:end] if end > 0 else ""
        else:
            hostname = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
        return hostname.lower() in LOOPBACK_HOSTS, raw

    def _authenticate(self) -> None:
        if not self.app.auth:
            return
        header = self.headers.get("Authorization") or ""
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented.strip():
            raise VBoardError("unauthorized", "a bearer token is required", hint="send Authorization: Bearer <token>")
        if not hmac.compare_digest(presented.strip().encode("utf-8"), self.app.token.encode("utf-8")):
            raise VBoardError("unauthorized", "the bearer token was not accepted")

    def _caller(self, can_entries: list | None, channel: str | None = None) -> Caller:
        profile, _avatar = parse_profile_header(self.headers.get("x-board-profile"))
        can = _narrow(can_entries, channel)
        return Caller(principal=self.app.principal, profile=profile, can=can)

    # -- routing
    def _dispatch(self, method: str) -> None:
        ok, host_header = self._host_ok()
        if not ok:
            raise VBoardError("forbidden", "requests must use a loopback Host header")
        split = urlsplit(self.path)
        path = split.path
        mount = self.app.mount
        if mount and not (path == mount or path.startswith(mount + "/")):
            raise VBoardError("not_found", "no such route")
        rest = path[len(mount):]
        segs = [s for s in rest.split("/") if s]
        q = parse_qs(split.query, keep_blank_values=True)

        if not segs:                                    # discovery, no auth
            if method != "GET":
                raise VBoardError("not_found", "no such route")
            url = f"http://{host_header}{mount}"
            self._send_json(200, self.app.discovery(url))
            return

        self._authenticate()
        can_entries = parse_can_header(self.headers.get("x-board-can"))
        first = segs[0]
        if first in _UNSUPPORTED_FIRST or (first == "messages" and len(segs) == 3
                                           and segs[2] in _UNSUPPORTED_MESSAGE_ACTIONS):
            raise VBoardError("not_supported", f"{'/'.join(segs[:2])} is not supported by the local board engine")

        route = _ROUTES.get((method, first, len(segs)))
        handler: Callable | None = None
        if route is not None:
            handler = route
        elif first == "messages" and len(segs) == 3 and method == "POST" and segs[2] in _MESSAGE_ACTIONS:
            handler = _msg_action
        elif first == "tasks" and len(segs) == 3 and method == "POST" and segs[2] in _TASK_ACTIONS:
            handler = _task_action
        if handler is None:
            raise VBoardError("not_found", "no such route")
        handler(self, method, segs, q, can_entries)

    # -- events (long-poll and SSE)
    def _events_args(self, q: dict[str, list[str]]) -> dict[str, Any]:
        last = _q_int(q, "last")
        out: dict[str, Any] = {
            "after": _q_one(q, "after") or None, "last": last, "all": _q_bool(q, "all"),
            "channel": _q_one(q, "channel") or None, "re": _q_one(q, "re") or None, "by": _q_one(q, "by") or None,
            "mentions": _q_one(q, "mentions") or None, "label": list(q.get("label") or []),
            "source": (_q_one(q, "from") or _q_one(q, "source")) or None, "text": _q_one(q, "text") or None,
        }
        return out

    def _handle_events(self, q: dict, can_entries: list | None) -> None:
        args = self._events_args(q)
        caller = self._caller(can_entries, args["channel"])
        wait = _q_num(q, "wait") or 0.0
        accept = (self.headers.get("Accept") or "").lower()
        if "text/event-stream" in accept:
            self._sse(caller, args)
            return
        if not 0 <= wait <= EVENTS_WAIT_MAX_S:
            raise _bad("wait: 0 to 300 seconds")
        hold = min(wait, self.app.hold_s) if wait > 0 else 0.0
        doc = self.app.store.events(caller, wait=hold, should_stop=self.app._stopping.is_set, **args)
        self._send_json(200, doc)

    def _sse(self, caller: Caller, args: dict[str, Any]) -> None:
        app = self.app
        store = app.store
        args = dict(args)
        if args["after"] is None and args["last"] is None:
            args["after"] = self.headers.get("Last-Event-ID") or None
        if args["last"] is not None:
            first = store.events(caller, **args)
        else:
            first = store.events(caller, wait=0.0, **args)      # validates the cursor before any byte is streamed
        cursor = first["cursor"]
        filt = {k: v for k, v in args.items() if k not in ("after", "last")}

        self._streaming = True
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        write = self.wfile.write
        flush = self.wfile.flush
        write(b": connected\n\n")
        flush()
        sent_cursor = self._emit(first, None if first.get("events") else cursor) or cursor
        mono = time.monotonic
        started = mono()
        last_write = started
        while not app._stopping.is_set():
            now = mono()
            if now - started >= app.stream_max_s:
                break
            slice_s = max(0.01, min(1.0, app.ping_s - (now - last_write) if app.ping_s > 0 else 1.0,
                                    app.stream_max_s - (now - started)))
            try:
                page = store.events(caller, after=cursor, wait=slice_s, should_stop=app._stopping.is_set, **filt)
            except VBoardError:
                break
            cursor = page["cursor"]
            wrote = self._emit(page, sent_cursor)
            if wrote is not None:
                sent_cursor = wrote
                last_write = mono()
            elif app.ping_s > 0 and mono() - last_write >= app.ping_s:
                write(b": ping\n\n")
                flush()
                last_write = mono()

    def _emit(self, page: dict, sent_cursor: str | None) -> str | None:
        """Write event frames for a page; a bare `id:` frame when the cursor advanced past the last event.
        Returns the last cursor written, or None if nothing was written."""
        out = bytearray()
        last_written: str | None = None
        for ev in page.get("events") or []:
            data = json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
            out += f"id: {ev['cursor']}\nevent: board\ndata: {data}\n\n".encode("utf-8")
            last_written = ev["cursor"]
        cursor = page.get("cursor")
        if cursor is not None and cursor != (last_written or sent_cursor):
            out += f"id: {cursor}\n\n".encode("utf-8")
            last_written = cursor
        if not out:
            return None
        self.wfile.write(bytes(out))
        self.wfile.flush()
        return last_written


# --------------------------------------------------------------------------------------------- route handlers
def _store(h: _Handler) -> BoardStore:
    return h.app.store


def _r_me(h: _Handler, method: str, segs: list[str], q: dict, can: list | None) -> None:
    h._send_json(200, _store(h).me(h._caller(can)))


def _r_channels_get(h, method, segs, q, can) -> None:
    doc = _store(h).channels(h._caller(can), prefix=_q_one(q, "prefix") or None, after=_q_one(q, "after") or None,
                             limit=_q_int(q, "limit") or 500)
    h._send_json(200, doc)


def _r_channels_post(h, method, segs, q, can) -> None:
    body = h._read_body()
    path = _b_get(body, "path", str, required=True)
    doc = _store(h).create_channel(h._caller(can, path), path, private=bool(_b_get(body, "private", bool)))
    h._send_json(201 if doc.get("created") else 200, doc)


def _r_actors(h, method, segs, q, can) -> None:
    ids = list(q.get("id") or [])
    if len(ids) > 100:
        raise _bad("id: at most 100 ids")
    h._send_json(200, _store(h).actors(h._caller(can), ids))


def _r_messages_get(h, method, segs, q, can) -> None:
    channel = _q_one(q, "channel")
    if not channel:
        raise _bad("channel is required")
    doc = _store(h).messages(h._caller(can, channel), channel, after=_q_one(q, "after") or None,
                             limit=_q_int(q, "limit") or 50)
    h._send_json(200, doc)


def _r_thread(h, method, segs, q, can) -> None:
    doc = _store(h).thread(h._caller(can), segs[1], after=_q_one(q, "after") or None, limit=_q_int(q, "limit"),
                           label=list(q.get("label") or []))
    h._send_json(200, doc)


def _r_threads(h, method, segs, q, can) -> None:
    channel = _q_one(q, "channel") or None
    doc = _store(h).threads(
        h._caller(can, channel), channel=channel, re=_q_one(q, "re") or None, by=_q_one(q, "by") or None,
        mentions=_q_one(q, "mentions") or None, label=list(q.get("label") or []),
        source=(_q_one(q, "from") or _q_one(q, "source")) or None, text=_q_one(q, "text") or None,
        after=_q_one(q, "after") or None, before=_q_one(q, "before") or None, limit=_q_int(q, "limit") or 50)
    h._send_json(200, doc)


def _r_post(h, method, segs, q, can) -> None:
    body = h._read_body()
    channel = _b_get(body, "channel", str)
    re_ = _b_get(body, "re", str)
    text = _b_get(body, "text", str, required=True)
    doc = _store(h).post(h._caller(can, channel), text=text, channel=channel, re=re_,
                         labels=_b_get(body, "labels", dict), client_id=_b_get(body, "id", str),
                         after=_b_get(body, "after", str), no_sync=bool(_b_get(body, "noSync", bool)))
    h._send_json(201, doc)


def _r_posted(h, method, segs, q, can) -> None:
    h._send_json(200, _store(h).posted(h._caller(can), segs[1]))


def _msg_action(h, method, segs, q, can) -> None:
    body = h._read_body()
    mid, action = segs[1], segs[2]
    caller = h._caller(can)
    st = _store(h)
    if action == "edit":
        doc = st.edit(caller, mid, _b_get(body, "text", str, required=True), revision=_b_int(body, "revision"))
    elif action == "delete":
        doc = st.delete(caller, mid, revision=_b_int(body, "revision"))
    elif action == "reactions":
        doc = st.react(caller, mid, _b_get(body, "emoji", str, required=True),
                       remove=bool(_b_get(body, "remove", bool)))
    else:  # labels
        unset = _b_get(body, "unset", list) or []
        doc = st.label(caller, mid, set=_b_get(body, "set", dict), unset=unset, if_=_b_get(body, "if", dict))
    h._send_json(200, {"ok": True, **_strip_outcome(doc)})


def _r_tasks_get(h, method, segs, q, can) -> None:
    channel = _q_one(q, "channel") or None
    doc = _store(h).list_tasks(
        h._caller(can, channel), channel=channel, state=_q_one(q, "state") or "open", label=list(q.get("label") or []),
        by=_q_one(q, "by") or None, owner=_q_one(q, "owner") or None, for_=_q_one(q, "for") or None,
        needs=_q_one(q, "needs") or None, after=_q_one(q, "after") or None, limit=_q_int(q, "limit") or 50,
        latest=_q_bool(q, "latest"))
    h._send_json(200, doc)


def _r_tasks_post(h, method, segs, q, can) -> None:
    body = h._read_body()
    channel = _b_get(body, "channel", str)
    doc = _store(h).create_task(
        h._caller(can, channel), channel=channel, text=_b_get(body, "text", str), re=_b_get(body, "re", str),
        labels=_b_get(body, "labels", dict), needs=_b_get(body, "needs", list) or (),
        for_=_b_get(body, "for", str), claim=bool(_b_get(body, "claim", bool)), lease=_b_num(body, "lease"),
        token=_b_get(body, "token", str), client_id=_b_get(body, "id", str),
        no_sync=bool(_b_get(body, "noSync", bool)))
    h._send_json(201, _strip_outcome(doc))


def _r_batch_post(h, method, segs, q, can) -> None:
    body = h._read_body()
    channel = _b_get(body, "channel", str, required=True)
    tasks = _b_get(body, "tasks", list, required=True)
    doc = _store(h).create_tasks(h._caller(can, channel), channel, tasks, client_id=_b_get(body, "id", str))
    h._send_json(201, doc)


def _r_batch_get(h, method, segs, q, can) -> None:
    h._send_json(200, _store(h).batch(h._caller(can), segs[2]))


def _r_claim_next(h, method, segs, q, can) -> None:
    body = h._read_body()
    channel = _b_get(body, "channel", str, required=True)
    wait = _b_num(body, "wait") or 0.0
    doc = _store(h).claim_next(
        h._caller(can, channel), channel, label=_b_get(body, "label", dict), from_=_b_get(body, "from", list) or (),
        lease=_b_num(body, "lease"), wait=wait, token=_b_get(body, "token", str),
        should_stop=h.app._stopping.is_set)
    h._send_json(200, doc)


def _task_action(h, method, segs, q, can) -> None:
    body = h._read_body()
    tid, action = segs[1], segs[2]
    caller = h._caller(can)
    st = _store(h)

    def fence() -> tuple[int, str]:
        epoch = _b_int(body, "epoch", required=True)
        token = _b_get(body, "token", str, required=True)
        return epoch, token                                    # type: ignore[return-value]

    if action == "claim":
        doc = st.claim(caller, tid, lease=_b_num(body, "lease"), token=_b_get(body, "token", str))
    elif action == "renew":
        epoch, token = fence()
        doc = st.renew(caller, tid, epoch=epoch, token=token, lease=_b_num(body, "lease"))
    elif action == "release":
        epoch, token = fence()
        doc = st.release(caller, tid, epoch=epoch, token=token)
    elif action == "complete":
        epoch, token = fence()
        doc = st.complete(caller, tid, epoch=epoch, token=token, result=_b_get(body, "result", str))
    elif action == "fail":
        epoch, token = fence()
        doc = st.fail(caller, tid, epoch=epoch, token=token, reason=_b_get(body, "reason", str, required=True))
    elif action == "block":
        epoch = _b_int(body, "epoch")
        token = _b_get(body, "token", str)
        if (epoch is None) != (token is None):
            raise _bad("epoch and token go together")
        doc = st.block(caller, tid, reason=_b_get(body, "reason", str, required=True),
                       kind=_b_get(body, "kind", str), epoch=epoch, token=token)
    elif action == "unblock":
        doc = st.unblock(caller, tid)
    elif action == "reopen":
        doc = st.reopen(caller, tid)
    elif action == "assign":
        doc = st.assign(caller, tid, _b_get(body, "for", str))
    elif action == "needs":
        doc = st.set_needs(caller, tid, _b_get(body, "needs", list, required=True))
    else:  # cancel
        doc = st.cancel(caller, tid, reason=_b_get(body, "reason", str, required=True))
    h._send_json(200, _strip_outcome(doc))


def _r_events(h, method, segs, q, can) -> None:
    h._handle_events(q, can)


_MESSAGE_ACTIONS = {"edit", "delete", "reactions", "labels"}
_TASK_ACTIONS = {"claim", "renew", "release", "complete", "fail", "block", "unblock", "reopen", "assign", "needs",
                 "cancel"}

def _tasks_post_two(h, method, segs, q, can) -> None:
    if segs[1] == "batch":
        _r_batch_post(h, method, segs, q, can)
    elif segs[1] == "claim":
        _r_claim_next(h, method, segs, q, can)
    else:
        raise VBoardError("not_found", "no such route")


def _tasks_get_three(h, method, segs, q, can) -> None:
    if segs[1] == "batch":
        _r_batch_get(h, method, segs, q, can)
    else:
        raise VBoardError("not_found", "no such route")


_ROUTES: dict[tuple[str, str, int], Callable] = {
    ("GET", "me", 1): _r_me,
    ("GET", "channels", 1): _r_channels_get,
    ("POST", "channels", 1): _r_channels_post,
    ("GET", "actors", 1): _r_actors,
    ("GET", "messages", 1): _r_messages_get,
    ("GET", "messages", 2): _r_thread,
    ("GET", "threads", 1): _r_threads,
    ("POST", "messages", 1): _r_post,
    ("GET", "posts", 2): _r_posted,
    ("GET", "tasks", 1): _r_tasks_get,
    ("POST", "tasks", 1): _r_tasks_post,
    ("POST", "tasks", 2): _tasks_post_two,
    ("GET", "tasks", 3): _tasks_get_three,
    ("GET", "events", 1): _r_events,
}
# POST /tasks/:id/<action> has 3 segments and is resolved by _task_action in _dispatch.

__all__ = ["BoardHttpServer", "serve", "parse_profile_header", "parse_can_header", "LOOPBACK_HOSTS", "BODY_LIMIT"]
