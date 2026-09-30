from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .audit import AuditTimeline
from .config import RuntimeConfig


@dataclass
class LegacyObservationAudit:
    """Read-only bridge from legacy CDPA runtime evidence into audit sources."""

    config: RuntimeConfig
    _previous_board_signature: str | None = field(default=None, init=False)

    def refresh(self, timeline: AuditTimeline, runtime_task_ids: frozenset[str] = frozenset()) -> None:
        board = self._read_legacy_board()
        self._publish_dom_source(timeline, board, runtime_task_ids)
        self._publish_mutation_source(timeline, board)
        self._publish_listen_source(timeline)

    def task_summaries(self) -> dict[str, dict[str, Any]]:
        """Read-only legacy board summaries keyed by legacy task id; empty when unavailable."""
        board = self._read_legacy_board()
        return {
            str(item.get("task_id")): self._task_summary(item)
            for item in board.get("items", [])
            if isinstance(item, dict) and item.get("task_id")
        }

    def _read_legacy_board(self) -> dict[str, Any]:
        try:
            return self._read_json(f"http://127.0.0.1:{self.config.legacy_api_port}/api/tasks")
        except RuntimeError as exc:
            return {"error": str(exc), "items": []}

    def _publish_dom_source(
        self,
        timeline: AuditTimeline,
        board: dict[str, Any],
        runtime_task_ids: frozenset[str],
    ) -> None:
        if board.get("error"):
            timeline.update_source_state(
                source="dom",
                status="unavailable",
                message="Legacy task board is unavailable.",
                payload={"error": board.get("error")},
            )
            return
        items = [item for item in board.get("items", []) if isinstance(item, dict)]
        active_items = [
            {**self._task_summary(item), "runtime_v2": item.get("task_id") in runtime_task_ids}
            for item in items
            if item.get("status") not in {"DONE", "STOPPED"}
        ]
        # Runtime-v2 verification tasks first so bounded output never hides them behind old tasks.
        active_items.sort(key=lambda item: not item["runtime_v2"])
        blocked = [item for item in active_items if item.get("status") == "BLOCKED"]
        runtime_active = [item for item in active_items if item["runtime_v2"]]
        runtime_blocked = [item for item in runtime_active if item.get("status") == "BLOCKED"]
        timeline.update_source_state(
            source="dom",
            status="active",
            message=(
                f"Legacy task board shows {len(active_items)} active task(s), {len(blocked)} blocked; "
                f"runtime-v2 tasks: {len(runtime_active)} active, {len(runtime_blocked)} blocked."
            ),
            payload={
                "generation": board.get("generation"),
                "counts": board.get("counts"),
                "active_tasks": active_items[:8],
            },
        )

    def _publish_mutation_source(self, timeline: AuditTimeline, board: dict[str, Any]) -> None:
        if board.get("error"):
            timeline.update_source_state(
                source="mutation",
                status="unavailable",
                message="Legacy mutation summary is unavailable because board fetch failed.",
                payload={"error": board.get("error")},
            )
            return
        signature = self._board_signature(board)
        changed = self._previous_board_signature is not None and signature != self._previous_board_signature
        self._previous_board_signature = signature
        timeline.update_source_state(
            source="mutation",
            status="active" if changed else "idle",
            message="Legacy board changed since previous audit snapshot." if changed else "No legacy board change since previous audit snapshot.",
            payload={
                "signature": signature,
                "generation": board.get("generation"),
                "changed": changed,
            },
        )

    def _publish_listen_source(self, timeline: AuditTimeline) -> None:
        events = self._read_live_events_tail()
        log_tail = self._read_runtime_log_tail()
        status = "active" if events or log_tail else "idle"
        timeline.update_source_state(
            source="listen",
            status=status,
            message="Legacy live events/log tail available." if status == "active" else "No legacy live event/log evidence available yet.",
            payload={
                "live_events_tail": events,
                "legacy_runtime_log_tail": log_tail,
            },
        )

    def _read_live_events_tail(self) -> list[dict[str, Any]]:
        path = self.config.repository_root / ".runtime" / "live-events.jsonl"
        if not path.exists():
            return []
        lines = self._tail_lines(path, limit=12)
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records[-8:]

    def _read_runtime_log_tail(self) -> list[str]:
        path = self.config.state_root / "task-runner" / "legacy-runtime.log"
        if not path.exists():
            return []
        return self._tail_lines(path, limit=8)

    def _read_json(self, url: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(str(exc)) from exc
        if not isinstance(value, dict):
            raise RuntimeError("legacy API returned a non-object response")
        return value

    def _task_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        roles = item.get("roles") if isinstance(item.get("roles"), list) else []
        return {
            "task_id": item.get("task_id"),
            "team": item.get("team"),
            "task_mode": item.get("task_mode"),
            "status": item.get("status"),
            "column": item.get("column"),
            "active_role": item.get("active_role"),
            "active_action": item.get("active_action"),
            "block_code": item.get("block_code"),
            "updated_at": item.get("updated_at"),
            "roles": [self._role_summary(role) for role in roles],
        }

    def _role_summary(self, role: Any) -> dict[str, Any]:
        if not isinstance(role, dict):
            return {}
        return {
            "logical_role": role.get("logical_role"),
            "physical_role": role.get("physical_role"),
            "status": role.get("status"),
            "online": role.get("online"),
            "turn": role.get("turn"),
            "page_id": role.get("page_id"),
            "page_url": role.get("page_url"),
            "page_url_kind": _page_url_kind(role.get("page_url")),
        }

    def _board_signature(self, board: dict[str, Any]) -> str:
        items = board.get("items") if isinstance(board.get("items"), list) else []
        material = json.dumps(
            [self._task_summary(item) for item in items if isinstance(item, dict)],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _tail_lines(self, path: Path, *, limit: int) -> list[str]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-limit:]


def _page_url_kind(value: Any) -> str | None:
    if not value:
        return None
    path = urlsplit(str(value)).path
    if not path.startswith("/c/"):
        return "root"
    return "provisional" if unquote(path[3:]).lower().startswith("local-chatgpt:") else "canonical"
