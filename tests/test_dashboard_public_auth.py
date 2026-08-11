from __future__ import annotations

import dataclasses
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest

from playwright_auto import dashboard as dashboard_module
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.dashboard import create_server

PUBLIC_HOST = "cdpa.hcu-lab.me"
PUBLIC_ORIGIN = f"https://{PUBLIC_HOST}"
FIXTURE_PASSWORD = "fixture-secret"


class UpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def _handle(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size) if size else b""
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "headers": dict(self.headers.items()),
            }
        )
        payload = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "body": body.decode(),
                "idempotency": self.headers.get("Idempotency-Key"),
            }
        ).encode()
        self.send_response(207)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", '"security-upstream"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def start_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def start_frontend(tmp_path: Path, *, api_port: int, auth_password: str | None = FIXTURE_PASSWORD):
    config = load_cdpa_config(None, repository_root=tmp_path)
    config = dataclasses.replace(config, dashboard_api_port=api_port)
    server = create_server(config, host="127.0.0.1", port=0, auth_password=auth_password)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def request(server, method: str, path: str, *, body: bytes | None = None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, data


def login(server, *, password: str = FIXTURE_PASSWORD, extra_headers=None):
    headers = {
        "Host": PUBLIC_HOST,
        "Origin": PUBLIC_ORIGIN,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    headers.update(extra_headers or {})
    return request(server, "POST", "/auth/login", body=urlencode({"password": password}).encode(), headers=headers)


def session_cookie(headers: dict[str, str]) -> str:
    return headers["Set-Cookie"].split(";", 1)[0]


def set_clock(monkeypatch, value: list[float]) -> None:
    monkeypatch.setattr(dashboard_module, "_monotonic", lambda: value[0], raising=False)


def stop_pair(frontend, frontend_thread, upstream, upstream_thread):
    frontend.shutdown()
    upstream.shutdown()
    frontend_thread.join(timeout=5)
    upstream_thread.join(timeout=5)


def test_public_login_rate_limit_uses_tcp_peer_and_releases_after_window(tmp_path: Path, monkeypatch):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    clock = [100.0]
    set_clock(monkeypatch, clock)
    try:
        for attempt in range(5):
            status, _headers, _body = login(
                server,
                password="wrong",
                extra_headers={"X-Forwarded-For": f"203.0.113.{attempt + 1}"},
            )
            assert status == 401

        status, headers, _body = login(
            server,
            password=FIXTURE_PASSWORD,
            extra_headers={"X-Forwarded-For": "198.51.100.250"},
        )
        assert status == 429
        assert int(headers["Retry-After"]) >= 1

        clock[0] += 301
        status, headers, _body = login(server, password="wrong")
        assert status == 401
        assert "Retry-After" not in headers
        assert upstream.requests == []
    finally:
        stop_pair(server, thread, upstream, upstream_thread)


def test_successful_public_login_clears_failure_bucket(tmp_path: Path, monkeypatch):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    clock = [200.0]
    set_clock(monkeypatch, clock)
    try:
        for _ in range(4):
            assert login(server, password="wrong")[0] == 401
        assert login(server)[0] == 303
        for _ in range(5):
            assert login(server, password="wrong")[0] == 401
        assert login(server, password="wrong")[0] == 429
    finally:
        stop_pair(server, thread, upstream, upstream_thread)


def test_public_sessions_are_unique_expire_and_set_browser_ttl(tmp_path: Path, monkeypatch):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    clock = [1000.0]
    set_clock(monkeypatch, clock)
    try:
        status, first_headers, _body = login(server)
        assert status == 303
        status, second_headers, _body = login(server)
        assert status == 303

        first_cookie = session_cookie(first_headers)
        second_cookie = session_cookie(second_headers)
        assert first_cookie != second_cookie
        for headers in (first_headers, second_headers):
            cookie = headers["Set-Cookie"]
            assert "Max-Age=43200" in cookie
            assert "Path=/" in cookie
            assert "HttpOnly" in cookie
            assert "Secure" in cookie
            assert "SameSite=Strict" in cookie

        clock[0] = 1000.0 + 43199
        for cookie in (first_cookie, second_cookie):
            status, _headers, _body = request(
                server,
                "GET",
                "/api/state",
                headers={"Host": PUBLIC_HOST, "Cookie": cookie},
            )
            assert status == 207

        clock[0] = 1000.0 + 43200
        status, _headers, body = request(
            server,
            "GET",
            "/api/state",
            headers={"Host": PUBLIC_HOST, "Cookie": first_cookie},
        )
        assert status == 401
        assert json.loads(body)["error"]["code"] == "authentication_required"
    finally:
        stop_pair(server, thread, upstream, upstream_thread)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_public_mutations_require_same_origin_before_proxy(tmp_path: Path, method: str):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    try:
        status, headers, _body = login(server)
        assert status == 303
        cookie = session_cookie(headers)
        base = {"Host": PUBLIC_HOST, "Cookie": cookie, "Content-Type": "application/json"}

        status, _headers, _body = request(server, method, "/api/tasks", body=b"{}", headers=base)
        assert status == 403
        assert upstream.requests == []

        status, _headers, _body = request(
            server,
            method,
            "/api/tasks",
            body=b"{}",
            headers={**base, "Origin": "https://foreign.invalid"},
        )
        assert status == 403
        assert upstream.requests == []

        status, _headers, _body = request(
            server,
            method,
            "/api/tasks",
            body=b"{}",
            headers={**base, "Origin": PUBLIC_ORIGIN},
        )
        assert status == 207
        assert [entry["method"] for entry in upstream.requests] == [method]
    finally:
        stop_pair(server, thread, upstream, upstream_thread)


def test_public_mutation_accepts_exact_referer_only_when_origin_absent(tmp_path: Path):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    try:
        status, headers, _body = login(server)
        assert status == 303
        cookie = session_cookie(headers)
        base = {"Host": PUBLIC_HOST, "Cookie": cookie, "Content-Type": "application/json"}

        status, _headers, _body = request(
            server,
            "POST",
            "/api/tasks",
            body=b"{}",
            headers={**base, "Referer": f"{PUBLIC_ORIGIN}/dashboard?view=all"},
        )
        assert status == 207
        accepted_count = len(upstream.requests)

        for referer in (
            "http://cdpa.hcu-lab.me/",
            "https://cdpa.hcu-lab.me:443/",
            "https://user@cdpa.hcu-lab.me/",
            "https://cdpa.hcu-lab.me.evil.invalid/",
            "https://[invalid",
        ):
            status, _headers, _body = request(
                server,
                "POST",
                "/api/tasks",
                body=b"{}",
                headers={**base, "Referer": referer},
            )
            assert status == 403
            assert len(upstream.requests) == accepted_count

        status, _headers, _body = request(
            server,
            "POST",
            "/api/tasks",
            body=b"{}",
            headers={
                **base,
                "Origin": "https://foreign.invalid",
                "Referer": f"{PUBLIC_ORIGIN}/dashboard",
            },
        )
        assert status == 403
        assert len(upstream.requests) == accepted_count
    finally:
        stop_pair(server, thread, upstream, upstream_thread)


def test_public_login_requires_same_origin_before_form_processing(tmp_path: Path):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    try:
        status, _headers, _body = request(
            server,
            "POST",
            "/auth/login",
            body=b"not-a-form",
            headers={"Host": PUBLIC_HOST, "Content-Type": "application/json"},
        )
        assert status == 403

        status, _headers, _body = request(
            server,
            "POST",
            "/auth/login",
            body=urlencode({"password": FIXTURE_PASSWORD}).encode(),
            headers={
                "Host": PUBLIC_HOST,
                "Origin": "https://foreign.invalid",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert status == 403

        assert login(server)[0] == 303
        assert upstream.requests == []
    finally:
        stop_pair(server, thread, upstream, upstream_thread)


def test_same_origin_public_proxy_preserves_mutation_semantics_and_strips_auth_cookie(tmp_path: Path):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    try:
        status, headers, _body = login(server)
        assert status == 303
        cookie = session_cookie(headers)
        status, headers, body = request(
            server,
            "POST",
            "/api/tasks?view=compact",
            body=b'{"task":"x"}',
            headers={
                "Host": PUBLIC_HOST,
                "Origin": PUBLIC_ORIGIN,
                "Content-Type": "application/json",
                "Content-Length": "12",
                "Idempotency-Key": "same-key",
                "Cookie": f"other=keep; {cookie}",
            },
        )
        payload = json.loads(body)
        assert status == 207
        assert headers["Content-Type"] == "application/json"
        assert headers["ETag"] == '"security-upstream"'
        assert payload == {
            "method": "POST",
            "path": "/api/tasks?view=compact",
            "body": '{"task":"x"}',
            "idempotency": "same-key",
        }
        forwarded = upstream.requests[-1]["headers"]
        assert forwarded.get("Cookie") == "other=keep"
        assert forwarded.get("Origin") == PUBLIC_ORIGIN
    finally:
        stop_pair(server, thread, upstream, upstream_thread)


def test_public_get_and_head_auth_behavior_is_unchanged(tmp_path: Path):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    try:
        for method in ("GET", "HEAD"):
            assert request(server, method, "/api/state", headers={"Host": PUBLIC_HOST})[0] == 401

        status, headers, _body = login(server)
        assert status == 303
        cookie = session_cookie(headers)
        for method in ("GET", "HEAD"):
            assert request(
                server,
                method,
                "/api/state",
                headers={"Host": PUBLIC_HOST, "Cookie": cookie},
            )[0] == 207
    finally:
        stop_pair(server, thread, upstream, upstream_thread)


def test_non_public_mutation_remains_trusted_without_origin(tmp_path: Path):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    try:
        status, _headers, _body = request(
            server,
            "POST",
            "/api/tasks",
            body=b"{}",
            headers={"Host": "127.0.0.1", "Content-Type": "application/json"},
        )
        assert status == 207
        assert len(upstream.requests) == 1
    finally:
        stop_pair(server, thread, upstream, upstream_thread)
