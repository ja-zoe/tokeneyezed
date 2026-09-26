"""Dashboard server: a read-only JSON API over Atlas plus the static UI. Standard library only.

    tokeneyezed-dashboard [--port 8765]

Runs are started from the CLI (`tokeneyezed run ...`); this only watches them. The page polls
`/api/runs/<id>` while a run is in progress, so the chart moves as attempts close.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv

from tokeneyezed.data import dashboard_reads as reads

# Vercel serves public/ on its own; locally this server does the same job.
STATIC = Path(__file__).resolve().parents[3] / "public"
_RUN = re.compile(r"^/api/runs/(?P<id>[^/]+)$")
_FEED = re.compile(r"^/api/runs/(?P<id>[^/]+)/feed$")


class App:
    """Routes a path to JSON. Kept apart from the HTTP handler so tests call it directly."""

    def __init__(self, db: Any = None) -> None:
        self.db = db  # None: dashboard_reads uses the Atlas connection from MONGODB_URI

    def api(self, path: str, query: dict[str, list[str]]) -> tuple[int, Any]:
        if path == "/api/runs":
            return 200, reads.list_runs(self.db)
        if path == "/api/compare":
            ids = [i for i in ",".join(query.get("ids", [])).split(",") if i]
            return 200, reads.compare_runs(ids or reads.default_compare_ids(self.db), self.db)
        if path == "/api/live":
            return 200, {"session_id": reads.running_session(self.db)}
        if match := _FEED.match(path):
            after = (query.get("after") or [None])[0]
            return 200, reads.run_feed(unquote(match["id"]), after, db=self.db)
        if match := _RUN.match(path):
            detail = reads.run_detail(unquote(match["id"]), self.db)
            return (200, detail) if detail else (404, {"error": "no such run"})
        return 404, {"error": "not found"}


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                try:
                    status, body = app.api(url.path, parse_qs(url.query))
                except Exception as exc:  # the UI shows the message; the console keeps the trace
                    self.log_error("api error: %r", exc)
                    status, body = 500, {"error": f"{type(exc).__name__}: {exc}"}
                return self._send(status, json.dumps(body).encode(), "application/json")
            name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            target = (STATIC / name).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self._send(404, b"not found", "text/plain")
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), kind)

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # the polling would drown the console

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tokeneyezed-dashboard", description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    load_dotenv()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(App()))
    print(f"dashboard on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
