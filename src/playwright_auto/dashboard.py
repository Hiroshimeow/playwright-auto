from __future__ import annotations

import argparse
import hmac
import http.client
import json
import math
import mimetypes
import os
import secrets
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .cdpa_config import CDPAConfig, load_cdpa_config

DASHBOARD_HTML_PATH = Path(__file__).with_name("dashboard.html")
DASHBOARD_LOGIN_HTML_PATH = Path(__file__).with_name("dashboard_login.html")
ASSET_ROOT = Path(__file__).with_name("dashboard_assets")
_AUTH_ENV_NAME = "CDPA_DASHBOARD_PASSWORD"
_SESSION_COOKIE = "cdpa_session"
_PUBLIC_HOST = "cdpa.hcu-lab.me"
_PUBLIC_ORIGIN = f"https://{_PUBLIC_HOST}"
_LOGIN_FAILURE_LIMIT = 5
_LOGIN_FAILURE_WINDOW_SECONDS = 300
_SESSION_TTL_SECONDS = 43200
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_monotonic = time.monotonic
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


def _load_dashboard_password(repository_root: Path) -> str | None:
    try:
        lines = (repository_root / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    prefix = f"{_AUTH_ENV_NAME}="
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            value = stripped[len(prefix) :].strip()
            return value or None
    return None


class FrontendApplication:
    def __init__(self, config: CDPAConfig, *, auth_password: str | None = None) -> None:
        self.config = config
        self.auth_password = auth_password or None
        self.login_failures: dict[str, list[float]] = {}
        self.sessions: dict[str, float] = {}
        self.auth_lock = threading.Lock()
        self.started_at = _monotonic()


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

    def _is_public_request(self) -> bool:
        host = self.headers.get("Host", "").strip().casefold()
        public_host = host == _PUBLIC_HOST or host.startswith(f"{_PUBLIC_HOST}:")
        return public_host or bool(self.headers.get("Cf-Ray", "").strip())

    def _prune_auth_state_locked(self, now: float) -> None:
        cutoff = now - _LOGIN_FAILURE_WINDOW_SECONDS
        for peer, failures in list(self.application.login_failures.items()):
            retained = [failed_at for failed_at in failures if failed_at > cutoff]
            if retained:
                self.application.login_failures[peer] = retained
            else:
                self.application.login_failures.pop(peer, None)
        for token, expiry in list(self.application.sessions.items()):
            if expiry <= now:
                self.application.sessions.pop(token, None)

    def _same_origin_request(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None:
            return origin.strip() == _PUBLIC_ORIGIN
        referer = self.headers.get("Referer")
        if not referer:
            return False
        try:
            parsed = urlsplit(referer.strip())
        except ValueError:
            return False
        return parsed.scheme == "https" and parsed.netloc == _PUBLIC_HOST

    def _session_is_valid(self) -> bool:
        parsed = cookies.SimpleCookie()
        try:
            parsed.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return False
        morsel = parsed.get(_SESSION_COOKIE)
        if not morsel:
            return False
        now = _monotonic()
        with self.application.auth_lock:
            self._prune_auth_state_locked(now)
            expiry = self.application.sessions.get(morsel.value)
            return bool(expiry and expiry > now)

    def _serve_login(self, *, status: int = 200, error: bool = False) -> None:
        marker = b"<!--AUTH_ERROR-->"
        error_html = "<p class=\"auth-error\">パスワードが違います。</p>".encode() if error else b""
        self._send(
            status,
            DASHBOARD_LOGIN_HTML_PATH.read_bytes().replace(marker, error_html),
            content_type="text/html; charset=utf-8",
        )

    def _login(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400)
            return
        if size < 0 or size > 4096:
            self._send(413)
            return
        if "application/x-www-form-urlencoded" not in self.headers.get("Content-Type", ""):
            self._send(400)
            return
        body = self.rfile.read(size).decode("utf-8", errors="replace") if size else ""
        submitted = parse_qs(body, keep_blank_values=True).get("password", [""])[0]
        configured = self.application.auth_password
        authenticated = bool(configured and hmac.compare_digest(submitted, configured))
        peer = self.client_address[0]
        now = _monotonic()
        retry_after: int | None = None
        token: str | None = None
        with self.application.auth_lock:
            self._prune_auth_state_locked(now)
            failures = self.application.login_failures.get(peer, [])
            if len(failures) >= _LOGIN_FAILURE_LIMIT:
                retry_after = max(
                    1,
                    math.ceil(failures[0] + _LOGIN_FAILURE_WINDOW_SECONDS - now),
                )
            elif not authenticated:
                self.application.login_failures.setdefault(peer, []).append(now)
            else:
                self.application.login_failures.pop(peer, None)
                token = secrets.token_urlsafe(32)
                self.application.sessions[token] = now + _SESSION_TTL_SECONDS
        if retry_after is not None:
            self._send(429, headers={"Retry-After": str(retry_after)})
            return
        if not authenticated:
            self._serve_login(status=401, error=True)
            return
        if not token:
            self._send(503)
            return
        self._send(
            303,
            headers={
                "Location": "/",
                "Set-Cookie": (
                    f"{_SESSION_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; "
                    f"Max-Age={_SESSION_TTL_SECONDS}"
                ),
            },
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
        forwarded: dict[str, str] = {}
        for name, value in self.headers.items():
            folded = name.casefold()
            if folded in _HOP_BY_HOP | {"host", "content-length"}:
                continue
            if folded == "cookie":
                value = "; ".join(
                    part.strip()
                    for part in value.split(";")
                    if part.strip().partition("=")[0].strip() != _SESSION_COOKIE
                )
                if not value:
                    continue
            forwarded[name] = value
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
        public_request = self._is_public_request()
        if public_request:
            if self.command in _STATE_CHANGING_METHODS and not self._same_origin_request():
                self._send(403)
                return
            if not self.application.auth_password:
                if path.startswith("/api/"):
                    self._json(
                        503,
                        {"error": {"code": "authentication_unavailable", "message": "authentication unavailable"}},
                    )
                else:
                    self._send(503, b"Service unavailable", content_type="text/plain; charset=utf-8")
                return
            if path == "/auth/login" and self.command == "POST":
                self._login()
                return
            if not self._session_is_valid():
                if path.startswith("/api/"):
                    self._json(
                        401,
                        {"error": {"code": "authentication_required", "message": "authentication required"}},
                    )
                else:
                    self._serve_login(status=200 if self.command in {"GET", "HEAD"} else 401)
                return
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
    auth_password: str | None = None,
) -> DashboardServer:
    server = DashboardServer((host, config.dashboard_port if port is None else port), DashboardHandler)
    server.application = FrontendApplication(config, auth_password=auth_password)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the static CDPA dashboard frontend")
    parser.add_argument("--config", default=None)
    parser.add_argument("--repository", default=".")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    repository_root = Path(args.repository).expanduser().resolve()
    config = load_cdpa_config(args.config, repository_root=repository_root)
    server = create_server(
        config,
        host=args.host,
        port=args.port,
        auth_password=_load_dashboard_password(repository_root),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
