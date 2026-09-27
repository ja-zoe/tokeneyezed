"""Loopback observer HTTP service with an injectable event writer."""

from __future__ import annotations

import argparse
import hmac
import json
import os
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .core import PreGate, validate_event
from .postcheck import INTENT_HEADER, PostChecker, decode_intent_header


def make_server(
    gate: PreGate,
    token: str,
    insert_event: Callable[[dict], None],
    port=8765,
    post_checker: PostChecker | None = None,
):
    """Accept a callable event writer; MongoEventWriter adapts data.insert_event."""
    if not token:
        raise ValueError("observer token must be nonempty")
    post_checker = post_checker or PostChecker()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/event":
                self.send_error(404)
                return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                self.send_error(401)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_048_576:
                    self.send_error(413)
                    return
                self.connection.settimeout(3)
                event = json.loads(self.rfile.read(size))
                validate_event(event)
                if event["phase"] == "post":
                    intent = decode_intent_header(self.headers.get(INTENT_HEADER, ""))
                    decision = post_checker.check(event, intent)
                else:
                    decision = gate.check(event)
                event["verdict"] = decision.action + (
                    f": {decision.reason}" if decision.reason else ""
                )
                insert_event(event)
                if event["phase"] == "stop":
                    post_checker.finish_attempt(event)
            except Exception:
                self.send_error(503, "observer could not evaluate or persist event")
                return
            body = json.dumps(decision.to_dict()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    return HTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--audit-log", type=Path)
    storage.add_argument("--mongo", action="store_true")
    parser.add_argument("--protect", action="append", default=[], type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error("workspace must be an existing directory")
    if args.audit_log and args.audit_log.resolve().is_relative_to(workspace):
        parser.error("audit log must live outside the task workspace")

    def write_event(event):
        with args.audit_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    writer = write_event
    active_rules = ()
    if args.mongo:
        from tokeneyezed.data.rules import load_active_rules

        from .storage import MongoEventWriter

        if not os.environ.get("MONGODB_URI"):
            parser.error("--mongo requires MONGODB_URI in the service environment")
        writer = MongoEventWriter()
        active_rules = load_active_rules()

    server = make_server(
        PreGate(workspace, tuple(args.protect), rules=active_rules),
        os.environ.get("TOKENEYEZED_OBSERVER_TOKEN", ""),
        writer,
        args.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
