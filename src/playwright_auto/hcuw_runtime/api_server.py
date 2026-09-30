from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .audit import AuditTimeline
from .config import RuntimeConfig
from .endpoint_inventory import EndpointInventory, EndpointInventoryError
from .legacy_audit import LegacyObservationAudit
from .state import RuntimeStateStore, StateStoreError
from .task_runner import RuntimeTaskRunner, RuntimeTaskRunnerError


class RuntimeApiServerError(RuntimeError):
    """Raised when the runtime-v2 API server cannot complete its owner work."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


@dataclass(frozen=True)
class RuntimeApiServer:
    """Owner of runtime-v2 JSON API routing and response envelopes."""

    config: RuntimeConfig
    state_store: RuntimeStateStore
    endpoint_inventory: EndpointInventory
    audit_timeline: AuditTimeline | None = None
    task_runner: RuntimeTaskRunner | None = None
    legacy_audit: LegacyObservationAudit | None = None

    def handle_get_path(self, path: str) -> ApiResponse:
        request_path = urlsplit(path).path
        self._record_action("api_request", f"GET {request_path}", {"path": request_path})
        try:
            return self._route_get_request(path, request_path)
        except (EndpointInventoryError, RuntimeTaskRunnerError, StateStoreError) as exc:
            return self._error_response(500, exc.reason, exc.message)
        except RuntimeError as exc:
            return self._error_response(500, "owner_error", str(exc))

    def handle_post_path(self, path: str, body: bytes) -> ApiResponse:
        request_path = urlsplit(path).path
        self._record_action("api_request", f"POST {request_path}", {"path": request_path})
        try:
            if request_path == "/api/tasks":
                runner = self._task_runner()
                record = runner.submit_task(_decode_json_body(body))
                return self._success_response(record, status=202)
            return self._error_response(
                404,
                "route_not_found",
                f"No runtime-v2 API route exists for {request_path}",
            )
        except RuntimeTaskRunnerError as exc:
            status = 400 if exc.reason == "invalid_task_request" else 500
            return self._error_response(status, exc.reason, exc.message)
        except ValueError as exc:
            return self._error_response(400, "invalid_json", str(exc))
        except RuntimeError as exc:
            return self._error_response(500, "owner_error", str(exc))

    def start(self) -> ThreadingHTTPServer:
        handler = self._build_request_handler()
        try:
            return ThreadingHTTPServer((self.config.api_host, self.config.api_port), handler)
        except OSError as exc:
            raise RuntimeApiServerError(
                "port_in_use",
                f"runtime-v2 API could not bind {self.config.api_host}:{self.config.api_port}",
            ) from exc

    def serve_forever(self) -> None:
        server = self.start()
        try:
            server.serve_forever()
        finally:
            server.server_close()

    def _route_get_request(self, path: str, request_path: str) -> ApiResponse:
        if request_path == "/api/health":
            return self._success_response(self._health_payload())
        if request_path == "/api/config":
            return self._success_response(self.config.to_config_payload())
        if request_path == "/api/runs":
            return self._success_response({"items": self.state_store.list_runs()})
        if request_path == "/api/tasks":
            return self._success_response({"items": self._projected_task_records()})
        if request_path == "/api/endpoints":
            result = self.endpoint_inventory.summarize_endpoints(probe=True)
            self._record_action(
                "endpoint_inventory",
                "Read-only endpoint inventory summarized",
                {"count": len(result.items)},
            )
            return self._success_response(
                {"items": result.items},
                diagnostics=result.diagnostics,
            )
        if request_path == "/api/audit/snapshot":
            timeline = self._audit_timeline()
            if self.legacy_audit is not None:
                self.legacy_audit.refresh(timeline, self._runtime_legacy_task_ids())
            return self._success_response(timeline.snapshot())
        if request_path == "/api/audit/events":
            since_id = _parse_since_event_id(urlsplit(path).query)
            return self._success_response(self._audit_timeline().events_since(since_id))
        return self._error_response(
            404,
            "route_not_found",
            f"No runtime-v2 API route exists for {request_path}",
        )

    def _projected_task_records(self) -> list[dict[str, Any]]:
        """Newest-first runtime-v2 records joined read-only with the legacy board."""
        records = sorted(
            self.state_store.list_task_records(),
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )
        board = self.legacy_audit.task_summaries() if self.legacy_audit is not None else {}
        items = []
        for index, record in enumerate(records):
            item = {
                **record,
                "latest": index == 0,
                "submission_status": record.get("status"),
                "updated_at": record.get("updated_at") or record.get("created_at"),
            }
            summary = board.get(str(record.get("legacy_task_id") or ""))
            if summary is not None:
                item.update(
                    status=summary.get("status"),
                    active_role=summary.get("active_role"),
                    block_code=summary.get("block_code"),
                    updated_at=summary.get("updated_at") or item["updated_at"],
                    legacy=summary,
                )
            items.append(item)
        return items

    def _runtime_legacy_task_ids(self) -> frozenset[str]:
        return frozenset(
            str(record["legacy_task_id"])
            for record in self.state_store.list_task_records()
            if record.get("legacy_task_id")
        )

    def _task_runner(self) -> RuntimeTaskRunner:
        if self.task_runner is None:
            raise RuntimeTaskRunnerError(
                "task_runner_unavailable",
                "runtime-v2 API was started without a task runner",
            )
        return self.task_runner

    def _audit_timeline(self) -> AuditTimeline:
        if self.audit_timeline is None:
            return AuditTimeline()
        return self.audit_timeline

    def _record_action(self, kind: str, message: str, payload: dict[str, Any]) -> None:
        if self.audit_timeline is None:
            return
        self.audit_timeline.record_event(
            source="actions",
            kind=kind,
            message=message,
            payload=payload,
        )

    def _health_payload(self) -> dict[str, Any]:
        return self.config.to_health_payload(
            started_at=datetime.now(timezone.utc).isoformat(),
            branch=_git_value(self.config.repository_root, "branch"),
            head=_git_value(self.config.repository_root, "head"),
        )

    def _success_response(
        self,
        data: dict[str, Any],
        *,
        diagnostics: list[dict[str, Any]] | None = None,
        status: int = 200,
    ) -> ApiResponse:
        payload = {
            "ok": True,
            "data": data,
            "diagnostics": diagnostics or [],
        }
        return ApiResponse(status=status, body=_json_bytes(payload))

    def _error_response(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> ApiResponse:
        payload = {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            },
            "diagnostics": [],
        }
        return ApiResponse(status=status, body=_json_bytes(payload))

    def _build_request_handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class RuntimeApiRequestHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                response = api.handle_get_path(self.path)
                self._write_response(response)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                body = self.rfile.read(_content_length(self.headers.get("Content-Length")))
                response = api.handle_post_path(self.path, body)
                self._write_response(response)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _write_response(self, response: ApiResponse) -> None:
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)

        return RuntimeApiRequestHandler


def _content_length(raw_value: str | None) -> int:
    try:
        return max(0, int(raw_value or "0"))
    except ValueError:
        return 0


def _decode_json_body(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("request body must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _git_value(repository_root, value_name: str) -> str | None:
    command = ["git", "branch", "--show-current"]
    if value_name == "head":
        command = ["git", "rev-parse", "HEAD"]
    try:
        completed = subprocess.run(
            command,
            cwd=repository_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = completed.stdout.strip()
    return text or None


def _parse_since_event_id(query: str) -> int:
    values = parse_qs(query).get("since", ["0"])
    try:
        return max(0, int(values[0]))
    except (TypeError, ValueError):
        return 0
