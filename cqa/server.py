"""HTTP JSON API over the command registry (standard library only).

  GET  /api/v1/commands                 the contract (every command, method, MC id, parameters)
  GET  /api/v1/<command>?k=v&actor=…    read commands
  POST /api/v1/<command>  {json body}   any command; actor in body or header X-CQA-Actor
  GET  /health

Envelope: {"ok": true, "data": …, "events": [ids appended]} / {"ok": false, "error": {"code", "message"}}.
Stateless, no cookies, CORS enabled (config/platform.json → api.cors_origins) so an HTML web resource of
Dynamics 365 or any other page can call it from its own origin.
"""

from __future__ import annotations

import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import api, common
from .common import CQAError

STATUS = {"bad_request": 400, "forbidden": 403, "not_found": 404, "conflict": 409, "not_implemented": 501}


class Handler(BaseHTTPRequestHandler):
    server_version = "cqa/1"

    def _cors(self):
        origins = common.setting("api.cors_origins", ["*"])
        origin = self.headers.get("Origin")
        allow = "*" if "*" in origins else (origin if origin in origins else None)
        if allow:
            self.send_header("Access-Control-Allow-Origin", allow)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CQA-Actor")

    def _send(self, status: int, body: dict):
        raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):  # quieter
        pass

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _dispatch(self, method: str):
        url = urlparse(self.path)
        if url.path in ("/", "/health"):
            return self._send(200, {"ok": True, "data": {"service": "cqa", "api": "/api/v1/commands"}})
        if not url.path.startswith("/api/v1/"):
            return self._send(404, {"ok": False, "error": {"code": "not_found", "message": "use /api/v1/<command>"}})
        name = url.path[len("/api/v1/"):]
        params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(url.query).items()}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except json.JSONDecodeError as e:
                    return self._send(400, {"ok": False, "error": {"code": "bad_request", "message": f"bad JSON: {e}"}})
                if not isinstance(body, dict):
                    return self._send(400, {"ok": False, "error": {"code": "bad_request", "message": "body must be an object"}})
                params.update(body)
        cmd = api.COMMANDS.get(name)
        if cmd and method == "GET" and cmd["method"] != "GET":
            return self._send(405, {"ok": False, "error": {"code": "method_not_allowed",
                                                           "message": f"{name} changes state: use POST"}})
        actor = params.pop("actor", None) or self.headers.get("X-CQA-Actor")
        try:
            return self._send(200, api.call(name, actor, params))
        except CQAError as e:
            return self._send(STATUS.get(e.code, 400), {"ok": False, "error": {"code": e.code, "message": e.message}})
        except (ValueError, KeyError, TypeError) as e:
            return self._send(400, {"ok": False, "error": {"code": "bad_request", "message": str(e)}})
        except Exception as e:  # pragma: no cover
            traceback.print_exc()
            return self._send(500, {"ok": False, "error": {"code": "internal", "message": str(e)}})

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")


def make_server(host: str = None, port: int = None) -> ThreadingHTTPServer:
    host = host or common.setting("api.host", "127.0.0.1")
    port = int(port if port is not None else common.setting("api.port", 8765))
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = None, port: int = None) -> None:
    srv = make_server(host, port)
    print(f"CQA API: http://{srv.server_address[0]}:{srv.server_address[1]}/api/v1/commands")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
