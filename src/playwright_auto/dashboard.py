from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from .cdpa_config import CDPAConfig, load_cdpa_config

DASHBOARD_HTML_PATH = Path(__file__).with_name("dashboard.html")
ASSET_ROOT = Path(__file__).with_name("dashboard_assets")
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


class FrontendApplication:
    def __init__(self, config: CDPAConfig) -> None:
        self.config = config
        self.started_at = time.monotonic()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CDPAFrontend/1"

    @property
    def application(self) -> FrontendApplication:
        return self.server.application  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(
        self,
        status: int,
        body: bytes = b"",
        *,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        if status != 304:
            self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            if name.casefold() not in _HOP_BY_HOP and name.casefold() not in {
                "content-length",
                "content-type",
            }:
                self.send_header(name, value)
        self.end_headers()
        if body and status != 304 and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, value: Mapping[str, Any]) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
            content_type="application/json; charset=utf-8",
        )

    def _serve_file(self, path: Path, *, root: Path) -> None:
        root = root.resolve()
        lexical = Path(os.path.abspath(path))
        try:
            relative = lexical.relative_to(root)
        except ValueError:
            self._json(403, {"error": {"code": "asset_escape", "message": "asset path escapes root"}})
            return
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                self._json(403, {"error": {"code": "asset_symlink", "message": "asset symlinks are forbidden"}})
                return
        if not lexical.is_file():
            self._json(404, {"error": {"code": "not_found", "message": "asset does not exist"}})
            return
        content_type = mimetypes.guess_type(lexical.name)[0] or "application/octet-stream"
        if lexical.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif lexical.suffix in {".html", ".css"}:
            content_type = f"{content_type}; charset=utf-8"
        self._send(200, lexical.read_bytes(), content_type=content_type)

    def _proxy(self) -> None:
        config = self.application.config
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": {"code": "invalid_request", "message": "invalid Content-Length"}})
            return
        if size < 0 or size > 10 * 1024 * 1024:
            self._json(413, {"error": {"code": "request_too_large", "message": "request body exceeds 10 MiB"}})
            return
        body = self.rfile.read(size) if size else None
        forwarded = {
            name: value
            for name, value in self.headers.items()
            if name.casefold() not in _HOP_BY_HOP | {"host", "content-length"}
        }
        connection = http.client.HTTPConnection(
            config.dashboard_api_host,
            config.dashboard_api_port,
            timeout=10,
        )
        try:
            connection.request(self.command, self.path, body=body, headers=forwarded)
            response = connection.getresponse()
            response_body = response.read()
            response_headers = {
                name: value
                for name, value in response.getheaders()
                if name.casefold() not in _HOP_BY_HOP
            }
        except (OSError, http.client.HTTPException) as exc:
            self._json(
                503,
                {
                    "error": {
                        "code": "api_unavailable",
                        "message": f"CDPA API is unavailable: {type(exc).__name__}",
                    }
                },
            )
            return
        finally:
            connection.close()
        content_type = response_headers.pop("Content-Type", None)
        response_headers.pop("Content-Length", None)
        self._send(
            response.status,
            response_body,
            content_type=content_type,
            headers=response_headers,
        )

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._json(
                200,
                {
                    "ok": True,
                    "service": "frontend",
                    "pid": os.getpid(),
                    "uptime_seconds": round(
                        time.monotonic() - self.application.started_at, 3
                    ),
                },
            )
            return
        if path.startswith("/api/"):
            self._proxy()
            return
        if self.command not in {"GET", "HEAD"}:
            self._json(405, {"error": {"code": "method_not_allowed", "message": "method not allowed"}})
            return
        if path == "/favicon.ico":
            self._send(204)
            return
        if path in {"/", "/index.html"}:
            self._serve_file(DASHBOARD_HTML_PATH, root=DASHBOARD_HTML_PATH.parent)
            return
        if path.startswith("/assets/"):
            relative = unquote(path[len("/assets/") :])
            self._serve_file(ASSET_ROOT / relative, root=ASSET_ROOT)
            return
        self._json(404, {"error": {"code": "not_found", "message": "route does not exist"}})

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    application: FrontendApplication


def create_server(
    config: CDPAConfig,
    *,
    host: str = "0.0.0.0",
    port: int | None = None,
) -> DashboardServer:
    server = DashboardServer((host, config.dashboard_port if port is None else port), DashboardHandler)
    server.application = FrontendApplication(config)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the static CDPA dashboard frontend")
    parser.add_argument("--config", default=None)
    parser.add_argument("--repository", default=".")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    config = load_cdpa_config(
        args.config,
        repository_root=Path(args.repository).expanduser().resolve(),
    )
    server = create_server(config, host=args.host, port=args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
