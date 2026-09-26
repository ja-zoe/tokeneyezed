"""Vercel entry point: every /api/* request lands here (see vercel.json).

It reuses the same routing as the local server (`App.api`), so `tokeneyezed-dashboard` and the
deployed dashboard can't disagree. MONGODB_URI (and optionally TOKENEYEZED_DB) come from the
Vercel project's environment variables. It only ever serves what is in Atlas.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tokeneyezed.dashboard.server import App  # noqa: E402

_app = App()  # get_db() keeps one MongoClient per warm function instance


class handler(BaseHTTPRequestHandler):  # noqa: N801  (Vercel looks for this exact name)
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        # vercel.json passes the original path as ?p= in case the rewrite hides it from us.
        path = query.pop("p", [url.path])[0]
        try:
            status, body = _app.api(path, query)
        except Exception as exc:
            status, body = 500, {"error": f"{type(exc).__name__}: {exc}"}
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
