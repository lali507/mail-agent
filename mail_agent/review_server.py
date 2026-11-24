"""Local approval UI (stdlib only). Bound to 127.0.0.1 and protected by a one-time token.
The browser talks to a ReviewSession, which resumes the paused LangGraph run."""
from __future__ import annotations

import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from langgraph.types import Command

log = logging.getLogger(__name__)


class ReviewSession:
    def __init__(self, graph, thread_id: str, max_concurrency: int = 6):
        self.graph = graph
        self.cfg = {"configurable": {"thread_id": thread_id}, "max_concurrency": max_concurrency}
        self.lock = threading.Lock()
        self.finished = threading.Event()

    def start(self, resume: bool = False) -> dict:
        with self.lock:
            if not (resume and self._interrupt()):
                self.graph.invoke({}, self.cfg)
            return self._view()

    def _interrupt(self):
        snap = self.graph.get_state(self.cfg)
        for task in snap.tasks:
            if task.interrupts:
                return task.interrupts[0].value
        return None

    def _view(self) -> dict:
        value = self._interrupt()
        if value is not None:
            return {"status": "review", **value}
        self.finished.set()
        stats = self.graph.get_state(self.cfg).values.get("stats", {}) or {}
        return {"status": "done", "stats": stats}

    def view(self) -> dict:
        with self.lock:
            return self._view()

    def submit(self, decisions: dict) -> dict:
        with self.lock:
            if self._interrupt() is None:
                return self._view()
            self.graph.invoke(Command(resume=decisions), self.cfg)
            return self._view()


def _page() -> bytes:
    return resources.files("mail_agent").joinpath("review.html").read_bytes()


def make_server(session: ReviewSession, port: int) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        def _authorized(self) -> bool:
            q = parse_qs(urlparse(self.path).query)
            return secrets.compare_digest((q.get("t") or [""])[0], token)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: dict, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            if not self._authorized():
                return self._send(403, b"forbidden", "text/plain")
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, _page(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(session.view())
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._authorized() or urlparse(self.path).path != "/api/submit":
                return self._send(403, b"forbidden", "text/plain")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 2_000_000:
                    return self._send(413, b"too large", "text/plain")
                data = json.loads(self.rfile.read(length) or b"{}")
                self._json(session.submit(data.get("decisions", {})))
            except Exception as ex:
                log.exception("submit failed")
                self._json({"status": "error", "message": str(ex)}, 500)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    return server, url


def serve_until_done(session: ReviewSession, port: int, timeout_min: int, on_ready=None) -> None:
    server, url = make_server(session, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("review UI at %s", url)
    if on_ready:
        on_ready(url)
    finished = session.finished.wait(timeout=timeout_min * 60)
    if finished:
        session.finished.wait(0)
        threading.Event().wait(5)  # let the browser fetch the summary
    else:
        log.info("review timed out; undecided drafts stay pending for the next round")
    server.shutdown()
