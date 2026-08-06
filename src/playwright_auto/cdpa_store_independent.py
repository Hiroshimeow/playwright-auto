from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from .cdpa_independent import (
    INDEPENDENT_COLUMN,
    INDEPENDENT_ROLE,
    TASK_MODE_INDEPENDENT,
    canonical_independent_events,
    independent_cycle_limit_reached,
    is_independent_task,
    normalize_agent_name,
    normalize_completion_request,
    normalize_event,
    normalize_manual_instruction,
    normalize_max_cycles,
    normalize_system_prompt,
    record_consumed_event,
    record_recovery_release,
    refresh_recovery_warmup_on_enable,
    validate_trigger_settings,
)
from .cdpa_safety import sanitize_text
from .cdpa_store_common import TERMINAL, _validate_task_id, slugify, utc_now
from .cdpa_team import physical_role
from .cdpa_workflow_agents import normalize_workflow_display_name
from .file_lock import exclusive_file_lock


class IndependentAgentStoreMixin:
    @staticmethod
    def _independent_task_id(agent_key: str, generation: int) -> str:
        digest = hashlib.sha256(
            f"independent:{agent_key}:generation:{generation}".encode("utf-8")
        ).hexdigest()[:24]
        return _validate_task_id(f"agent-{digest}-g{generation}")

    def _initial_independent_state(
        self,
        *,
        agent_name: str,
        agent_key: str,
        display_name: str,
        team: str,
        system_prompt: str,
        trigger_settings: Mapping[str, Any],
        task_id: str,
        generation: int,
        previous_task_id: str | None,
        repository_path: Path,
        target: Path,
        enabled: bool,
        max_cycles: int,
        max_cycles_explicit: bool,
        inherited_role: Mapping[str, Any] | None = None,
        watermarks: Mapping[str, Any] | None = None,
        occurrence_counts: Mapping[str, Any] | None = None,
        last_outcome: Mapping[str, Any] | None = None,
        new_chat_next_job: bool = False,
        new_chat_deferred_task_id: str | None = None,
        close_tab_when_idle: bool = False,
        now: str | None = None,
    ) -> dict[str, Any]:
        created = now or utc_now()
        text = f"Independent agent: {agent_name}"
        state = self._initial_task_state(
            text=text,
            requested_team=team,
            task_id=task_id,
            repository_path=repository_path,
            base=team,
            team=team,
            suffix=1,
            reusable_teams=[team],
            target=target,
            normalized_new=(),
            workflow_roles=tuple(self.config.roles),
            new_all=False,
            normalized_report_mode="file",
            normalized_dependencies=(),
            normalized_replaces=None,
            normalized_incident=None,
            normalized_attachments=(),
            readiness=SimpleNamespace(
                ready=True, waiting_on=(), missing=(), stopped=()
            ),
            queue_reuse=False,
            queue_blocked_by=None,
            now=created,
        )
        physical = physical_role(INDEPENDENT_ROLE, team, 1)
        previous_role = dict(inherited_role or {})
        inherited_watermarks = json.loads(
            json.dumps(dict(watermarks or {}), ensure_ascii=False, default=str)
        )
        inherited_watermarks["seen_event_keys"] = list(
            inherited_watermarks.get("seen_event_keys") or []
        )
        if trigger_settings.get("recovery") and not inherited_watermarks.get(
            "recovery_enabled_at"
        ):
            inherited_watermarks["recovery_enabled_at"] = created
        if inherited_watermarks.get("last_interval_slot") is not None:
            inherited_watermarks["last_interval_slot"] = int(
                inherited_watermarks["last_interval_slot"]
            )
        elif trigger_settings.get("interval_minutes") is not None:
            inherited_watermarks["last_interval_slot"] = int(
                datetime.now(timezone.utc).timestamp()
                // (int(trigger_settings["interval_minutes"]) * 60)
            )
        role = {
            "logical_role": INDEPENDENT_ROLE,
            "physical_role": physical,
            "status": "pending" if enabled else "paused",
            "turn": 0,
            "page_id": previous_role.get("page_id"),
            "page_url": previous_role.get("page_url"),
            "online": False,
            "conversation_generation": int(
                previous_role.get("conversation_generation") or 0
            ),
            "constructor_sent_generation": previous_role.get(
                "constructor_sent_generation"
            ),
            "attachments_uploaded_generation": None,
            "reset_requested": False,
            "reset_applied_generation": previous_role.get(
                "reset_applied_generation"
            ),
            "last_activity_at": None,
            "last_error": None,
        }
        hop = state["hops"][0]
        hop.update(
            target_role=INDEPENDENT_ROLE,
            physical_role=physical,
            kind="independent_job",
            handoff="Waiting for trigger",
            state="waiting_trigger",
        )
        state.update(
            task_mode=TASK_MODE_INDEPENDENT,
            status="WAITING" if enabled else "PAUSED",
            kanban_column=INDEPENDENT_COLUMN,
            active_role=INDEPENDENT_ROLE,
            active_hop_id=1,
            active_action="waiting_trigger" if enabled else "paused",
            pause_reason=None if enabled else "independent agent disabled",
            waiting_reason="waiting for trigger" if enabled else None,
            waiting_code="trigger" if enabled else None,
            waiting=(
                {
                    "reason": "trigger",
                    "waiting_on": [],
                    "stopped": [],
                    "missing": [],
                    "since": created,
                }
                if enabled
                else None
            ),
            roles={INDEPENDENT_ROLE: role},
            independent={
                "agent_name": agent_name,
                "agent_key": agent_key,
                "display_name": str(display_name or agent_name).strip(),
                "deleted_at": None,
                "agent_generation": generation,
                "previous_task_id": previous_task_id,
                "enabled": bool(enabled),
                "system_prompt": system_prompt,
                "trigger_settings": dict(trigger_settings),
                "watermarks": inherited_watermarks,
                "active_event": None,
                "occurrence_counts": dict(occurrence_counts or {}),
                "max_cycles": int(max_cycles),
                "max_cycles_explicit": bool(max_cycles_explicit),
                "cycle": 0,
                "completion_request": None,
                "continuation_request": None,
                "settings_reset_request": None,
                "job_history": [],
                "new_chat_next_job": bool(new_chat_next_job),
                "new_chat_deferred_task_id": new_chat_deferred_task_id,
                "close_tab_when_idle": bool(close_tab_when_idle),
                "idle_since": created if enabled else None,
                "tab_keep_open_until": None,
                "last_outcome": dict(last_outcome)
                if last_outcome is not None
                else None,
                "successor_task_id": None,
            },
        )
        state["depends_on_task_ids"] = []
        state["queue"] = None
        return state

    @staticmethod
    def _exclusive_recovery_owner(
        tasks: Sequence[Mapping[str, Any]],
        *,
        agent_key: str,
    ) -> Mapping[str, Any] | None:
        return next(
            (
                item
                for item in tasks
                if is_independent_task(item)
                and str(item.get("status") or "").upper() not in TERMINAL
                and isinstance(item.get("independent"), Mapping)
                and item["independent"].get("enabled") is True
                and item["independent"].get("agent_key") != agent_key
                and validate_trigger_settings(
                    item["independent"].get("trigger_settings")
                )["recovery"]
            ),
            None,
        )

    def assert_independent_enable_allowed(self, path: str | Path) -> None:
        target = Path(path).expanduser().resolve()
        with exclusive_file_lock(self.allocation_lock):
            tasks = self._discover_with_catalog_unlocked(
                self._load_catalog_unlocked(reconcile=False)
            )[0]
            current = next(
                (
                    item
                    for item in tasks
                    if Path(str(item.get("manifest_path") or "")).resolve() == target
                ),
                None,
            )
            if current is None or not is_independent_task(current):
                raise ValueError("task is not an independent agent")
            independent = current["independent"]
            if not validate_trigger_settings(
                independent.get("trigger_settings")
            )["recovery"]:
                return
            owner = self._exclusive_recovery_owner(
                tasks,
                agent_key=str(independent.get("agent_key") or ""),
            )
            if owner is not None:
                raise ValueError(
                    "exclusive recovery trigger is already owned by "
                    f"{owner['independent']['agent_name']!r}"
                )

    def _migrate_legacy_independent_defaults(
        self, path: str | Path
    ) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if not is_independent_task(state):
                return state
            independent = state["independent"]
            if independent.get("deleted_at") or "max_cycles_explicit" in independent:
                return state
            legacy_max_cycles = normalize_max_cycles(
                independent.get("max_cycles"), default=0
            )
            legacy_default = legacy_max_cycles in {1, 5}
            independent["max_cycles_explicit"] = not legacy_default
            if not legacy_default:
                return state
            independent["max_cycles"] = 0
            active_event = independent.get("active_event")
            if not isinstance(active_event, Mapping):
                independent["cycle"] = 0
                independent["settings_reset_request"] = None
                return state
            hop = self._active_independent_hop(state)
            accepted = hop.get("receipt") is not None or str(
                hop.get("state") or ""
            ) in {"sending", "sent", "waiting"}
            if accepted and hop.get("state") != "responded":
                independent["settings_reset_request"] = {
                    "event_key": active_event.get("event_key"),
                    "requested_at": utc_now(),
                    "max_cycles": 0,
                    "source_hop_id": hop.get("hop_id"),
                    "reason": "MIGRATED_LEGACY_DEFAULT",
                }
                return state
            return self._reset_independent_cycle_by_settings(state, now=utc_now())

        return self.update(path, mutate)

    def normalize_legacy_independent_agents(self) -> list[dict[str, Any]]:
        """Migrate old defaults and reactivate only the latest orphan identity."""
        tasks = self.discover()
        normalized: list[dict[str, Any]] = []
        migrated_tasks: list[dict[str, Any]] = []
        for state in tasks:
            independent = state.get("independent")
            if (
                is_independent_task(state)
                and isinstance(independent, Mapping)
                and not independent.get("deleted_at")
                and "max_cycles_explicit" not in independent
            ):
                migrated = self._migrate_legacy_independent_defaults(
                    state["manifest_path"]
                )
                if migrated != state:
                    normalized.append(migrated)
                state = migrated
            migrated_tasks.append(state)
        tasks = migrated_tasks
        grouped: dict[str, list[dict[str, Any]]] = {}
        for state in tasks:
            if not is_independent_task(state):
                continue
            independent = state.get("independent")
            if not isinstance(independent, Mapping) or independent.get("deleted_at"):
                continue
            grouped.setdefault(str(independent.get("agent_key") or ""), []).append(state)

        for states in grouped.values():
            nonterminal = [
                state
                for state in states
                if str(state.get("status") or "").upper() not in TERMINAL
            ]
            if nonterminal:
                continue
            latest = max(
                states,
                key=lambda state: int(
                    (state.get("independent") or {}).get("agent_generation") or 0
                ),
            )
            latest_path = Path(str(latest["manifest_path"])).expanduser().resolve()

            def migrate(current: dict[str, Any]) -> dict[str, Any]:
                if not is_independent_task(current):
                    return current
                independent = current["independent"]
                if independent.get("deleted_at"):
                    return current
                if str(current.get("status") or "").upper() not in TERMINAL:
                    return current
                if any(
                    str(item.get("task_id") or "")
                    == str(independent.get("successor_task_id") or "")
                    for item in tasks
                ):
                    return current
                now = utc_now()
                independent.setdefault("job_history", []).append(
                    {
                        "event_key": f"legacy:{current['task_id']}",
                        "trigger_type": "legacy",
                        "target_task_id": None,
                        "target_team": None,
                        "target_role": None,
                        "target_hop_id": None,
                        "cycle": int(independent.get("cycle") or 0),
                        "disposition": "MIGRATED_LEGACY",
                        "released_at": now,
                        "result": independent.get("last_outcome"),
                        "reason": "Collapsed terminal generation into long-lived identity",
                    }
                )
                if len(independent["job_history"]) > 200:
                    del independent["job_history"][:-200]
                independent.update(
                    active_event=None,
                    cycle=0,
                    completion_request=None,
                    continuation_request=None,
                    settings_reset_request=None,
                    successor_task_id=None,
                    idle_since=now,
                    tab_keep_open_until=None,
                )
                current.pop("completed_at", None)
                current.pop("stopped_at", None)
                current.pop("stop_reason", None)
                current["terminal_state"] = None
                current["kanban_column"] = INDEPENDENT_COLUMN
                current["block_code"] = None
                current["block_retryable"] = False
                current["block_reason"] = None
                self._append_independent_waiting_hop(current, now=now)
                if independent.get("enabled") is True:
                    current.update(
                        status="WAITING",
                        active_action="waiting_trigger",
                        pause_reason=None,
                        waiting_reason="waiting for trigger",
                        waiting_code="trigger",
                        waiting={
                            "reason": "trigger",
                            "waiting_on": [],
                            "stopped": [],
                            "missing": [],
                            "since": now,
                        },
                    )
                else:
                    current.update(
                        status="PAUSED",
                        active_action="paused",
                        pause_reason="independent agent disabled",
                        waiting=None,
                        waiting_reason=None,
                        waiting_code=None,
                    )
                return current

            migrated = self.update(latest_path, migrate)
            if migrated != latest:
                normalized.append(migrated)
        return normalized

    def create_independent_agent(
        self,
        agent_name: str,
        *,
        system_prompt: str,
        task_id: str | None = None,
        trigger_settings: Mapping[str, Any] | None = None,
        repository: str | Path | None = None,
        enabled: bool = True,
        max_cycles: int | None = None,
        external_command_id: str | None = None,
        _seed_only: bool = False,
    ) -> dict[str, Any]:
        display, agent_key, team = normalize_agent_name(agent_name)
        prompt = normalize_system_prompt(system_prompt)
        requested_settings = (
            validate_trigger_settings(trigger_settings)
            if trigger_settings is not None
            else None
        )
        if max_cycles is not None:
            normalize_max_cycles(max_cycles)
        requested_id = _validate_task_id(task_id) if task_id is not None else None
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            catalog = self._load_catalog_unlocked(reconcile=False)
            tasks, _errors = self._discover_with_catalog_unlocked(catalog)
            matching = [
                item
                for item in tasks
                if is_independent_task(item)
                and isinstance(item.get("independent"), Mapping)
                and item["independent"].get("agent_key") == agent_key
            ]
            current = [
                item
                for item in matching
                if str(item.get("status") or "").upper() not in TERMINAL
            ]
            if len(current) > 1:
                raise ValueError(
                    f"independent agent {display!r} has multiple nonterminal tasks"
                )
            if current:
                return current[0]
            previous = (
                max(
                    matching,
                    key=lambda item: int(
                        item["independent"].get("agent_generation") or 0
                    ),
                )
                if matching
                else None
            )
            if _seed_only and previous is not None:
                return previous
            if any(
                str(item.get("team") or "") == team and item not in matching
                for item in tasks
            ):
                raise ValueError(f"independent exact team collision: {team}")
            settings = (
                requested_settings
                if requested_settings is not None
                else (
                    validate_trigger_settings(
                        previous["independent"].get("trigger_settings")
                    )
                    if previous is not None
                    else validate_trigger_settings(None)
                )
            )
            effective_max_cycles = (
                normalize_max_cycles(max_cycles)
                if max_cycles is not None
                else (
                    normalize_max_cycles(
                        previous["independent"].get("max_cycles"), default=0
                    )
                    if previous is not None
                    else 0
                )
            )
            repository_path = Path(
                repository
                or (previous.get("repository") if previous is not None else None)
                or self.config.repository_root
            ).expanduser().resolve()
            if enabled and settings["recovery"]:
                owner = self._exclusive_recovery_owner(
                    tasks,
                    agent_key=agent_key,
                )
                if owner is not None:
                    raise ValueError(
                        "exclusive recovery trigger is already owned by "
                        f"{owner['independent']['agent_name']!r}"
                    )
            generation = (
                max(
                    int(item["independent"].get("agent_generation") or 1)
                    for item in matching
                )
                + 1
                if matching
                else 1
            )
            effective_id = (
                self._independent_task_id(agent_key, generation)
                if previous is not None
                else requested_id or self._independent_task_id(agent_key, generation)
            )
            if any(str(item.get("task_id") or "") == effective_id for item in tasks):
                raise ValueError(
                    f"task_id is already used by a valid manifest: {effective_id}"
                )
            title = f"Independent agent: {display}"
            target = (
                self.root / team / effective_id / f"{slugify(title)}.json"
            ).resolve()
            inherited_watermarks = (
                dict(previous["independent"].get("watermarks") or {})
                if previous is not None
                else None
            )
            if previous is not None:
                previous_settings = validate_trigger_settings(
                    previous["independent"].get("trigger_settings")
                )
                if (
                    previous_settings["interval_minutes"]
                    != settings["interval_minutes"]
                    and inherited_watermarks is not None
                ):
                    inherited_watermarks.pop("last_interval_slot", None)
            state = self._initial_independent_state(
                agent_name=display,
                agent_key=agent_key,
                display_name=(
                    str(previous["independent"].get("display_name") or display)
                    if previous is not None
                    else display
                ),
                team=team,
                system_prompt=prompt,
                trigger_settings=settings,
                task_id=effective_id,
                generation=generation,
                previous_task_id=(
                    str(previous.get("task_id")) if previous is not None else None
                ),
                repository_path=repository_path,
                target=target,
                enabled=enabled,
                max_cycles=effective_max_cycles,
                max_cycles_explicit=max_cycles is not None,
                inherited_role=(
                    previous["roles"][INDEPENDENT_ROLE]
                    if previous is not None
                    else None
                ),
                watermarks=inherited_watermarks,
                occurrence_counts=(
                    previous["independent"].get("occurrence_counts")
                    if previous is not None
                    else None
                ),
                last_outcome=(
                    previous["independent"].get("last_outcome")
                    if previous is not None
                    else None
                ),
                new_chat_next_job=bool(
                    previous["independent"].get("new_chat_next_job")
                )
                if previous is not None
                else False,
                new_chat_deferred_task_id=(
                    previous["independent"].get("new_chat_deferred_task_id")
                    if previous is not None
                    else None
                ),
            )
            # Start listening from creation time; recovery intentionally keeps
            # its existing ability to claim an already-blocked task.
            for event in canonical_independent_events(state, [*tasks, state]):
                if event["trigger_type"] != "recovery":
                    record_consumed_event(state["independent"], event)
            self._record_external_command(state, external_command_id)
            saved = self._save_unlocked(target, state)
            catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
            self._write_catalog_unlocked(catalog)
            return saved

    def seed_independent_agent(
        self,
        agent_name: str,
        *,
        system_prompt: str,
        trigger_settings: Mapping[str, Any] | None = None,
        max_cycles: int = 0,
    ) -> dict[str, Any]:
        return self.create_independent_agent(
            agent_name,
            system_prompt=system_prompt,
            trigger_settings=trigger_settings,
            max_cycles=max_cycles,
            _seed_only=True,
        )

    def request_independent_completion(
        self,
        path: str | Path,
        *,
        outcome: str,
        summary: str,
        target_task_id: str | None = None,
        repair_task_id: str | None = None,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        request = normalize_completion_request(
            {
                "outcome": outcome,
                "summary": summary,
                "target_task_id": target_task_id,
                "repair_task_id": repair_task_id,
                "requested_at": utc_now(),
            }
        )

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if not is_independent_task(state):
                raise ValueError("task is not independent")
            independent = state["independent"]
            if not isinstance(independent.get("active_event"), Mapping):
                raise ValueError("independent task has no active event")
            if str(state.get("status") or "").upper() in TERMINAL:
                raise ValueError("cannot complete a terminal independent task")
            if independent.get("continuation_request") is not None:
                raise ValueError("independent continuation is already requested")
            existing = independent.get("completion_request")
            if existing is not None:
                normalized_existing = normalize_completion_request(existing)
                comparable = {
                    key: value
                    for key, value in request.items()
                    if key != "requested_at"
                }
                existing_comparable = {
                    key: value
                    for key, value in normalized_existing.items()
                    if key != "requested_at"
                }
                if existing_comparable != comparable:
                    raise ValueError("a different independent completion is already requested")
            else:
                independent["completion_request"] = request
            self._record_external_command(state, external_command_id)
            return state

        return self.update(path, mutate)

    def request_independent_continuation(
        self,
        path: str | Path,
        *,
        reason: str,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = sanitize_text(reason, max_chars=1200).strip()
        if not normalized:
            raise ValueError("continuation reason must not be empty")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if not is_independent_task(state):
                raise ValueError("task is not independent")
            independent = state["independent"]
            if not isinstance(independent.get("active_event"), Mapping):
                raise ValueError("independent task has no active event")
            if str(state.get("status") or "").upper() in TERMINAL:
                raise ValueError("cannot continue a terminal independent task")
            if independent.get("completion_request") is not None:
                raise ValueError("independent completion is already requested")
            cycle = int(independent.get("cycle") or 0)
            if independent_cycle_limit_reached(
                cycle=cycle,
                max_cycles=normalize_max_cycles(
                    independent.get("max_cycles"), default=0
                ),
            ):
                raise ValueError("independent maximum cycles reached")
            existing = independent.get("continuation_request")
            if existing is not None and str(existing.get("reason") or "") != normalized:
                raise ValueError("a different independent continuation is already requested")
            if existing is None:
                independent["continuation_request"] = {
                    "reason": normalized,
                    "requested_at": utc_now(),
                    "cycle": cycle + 1,
                }
            self._record_external_command(state, external_command_id)
            return state

        return self.update(path, mutate)

    def record_independent_command(
        self,
        path: str | Path,
        command_id: str,
    ) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if not is_independent_task(state):
                raise ValueError("command source is not an independent task")
            self._record_external_command(state, command_id)
            return state

        return self.update(path, mutate)

    @staticmethod
    def _active_independent_hop(state: Mapping[str, Any]) -> dict[str, Any]:
        active_hop_id = state.get("active_hop_id")
        return next(
            item
            for item in state.get("hops") or []
            if isinstance(item, dict) and item.get("hop_id") == active_hop_id
        )

    @staticmethod
    def _append_independent_waiting_hop(
        state: dict[str, Any], *, now: str
    ) -> dict[str, Any]:
        try:
            previous = IndependentAgentStoreMixin._active_independent_hop(state)
        except StopIteration:
            previous = next(
                (
                    item
                    for item in reversed(state.get("hops") or [])
                    if isinstance(item, dict)
                ),
                None,
            )
            if previous is None:
                raise ValueError("independent task has no reusable hop template")
        next_hop_id = max(int(item["hop_id"]) for item in state["hops"]) + 1
        hop = json.loads(json.dumps(previous, ensure_ascii=False))
        hop.update(
            hop_id=next_hop_id,
            parent_hop_id=previous.get("hop_id"),
            source_role=INDEPENDENT_ROLE,
            target_role=INDEPENDENT_ROLE,
            turn=next_hop_id,
            kind="independent_job",
            handoff="Waiting for trigger",
            state="waiting_trigger",
            request_id=f"{state['task_id']}-hop{next_hop_id}",
            prompt=None,
            prompt_sha256=None,
            rendered_prompt_sha256=None,
            receipt=None,
            message_identity=None,
            response=None,
            response_sha256=None,
            report_path=None,
            report_sha256=None,
            report_size=None,
            route=None,
            repair_attempt=0,
            validation_error=None,
            wait={key: None for key in (previous.get("wait") or {})},
            timestamps={"created_at": now},
            errors=[],
        )
        hop["wait"].update(
            activity_length=0,
            transport_ui_active=False,
            last_stop_visible=False,
            refresh_count=0,
        )
        state["hops"].append(hop)
        state["active_hop_id"] = next_hop_id
        state["active_role"] = INDEPENDENT_ROLE
        return hop

    @staticmethod
    def _append_independent_job_history(
        independent: dict[str, Any],
        event: Mapping[str, Any],
        *,
        disposition: str,
        released_at: str,
        cycle: int,
        result: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        normalized = normalize_event(event)
        entry = {
            "event_key": normalized["event_key"],
            "trigger_type": normalized["trigger_type"],
            "target_task_id": normalized.get("target_task_id"),
            "target_team": normalized.get("target_team"),
            "target_role": normalized.get("target_role"),
            "target_hop_id": normalized.get("target_hop_id"),
            "cycle": int(cycle),
            "disposition": str(disposition),
            "released_at": released_at,
            "result": dict(result) if result is not None else None,
            "reason": sanitize_text(reason, max_chars=1200).strip() or None,
        }
        history = independent.setdefault("job_history", [])
        identity = (entry["event_key"], entry["disposition"], entry["released_at"])
        if any(
            (
                item.get("event_key"),
                item.get("disposition"),
                item.get("released_at"),
            )
            == identity
            for item in history
            if isinstance(item, Mapping)
        ):
            return
        history.append(entry)
        if len(history) > 200:
            del history[:-200]

    def _release_independent_job(
        self,
        state: dict[str, Any],
        *,
        disposition: str,
        now: str,
        completion: Mapping[str, Any] | None = None,
        reason: str | None = None,
        consume_event: bool = True,
        preserve_enabled: bool = False,
    ) -> dict[str, Any]:
        independent = state["independent"]
        event = independent.get("active_event")
        if not isinstance(event, Mapping):
            raise ValueError("independent task has no active event")
        cycle = int(independent.get("cycle") or 0)
        if consume_event:
            record_consumed_event(independent, event)
            record_recovery_release(
                independent,
                event,
                now=datetime.fromisoformat(now.replace("Z", "+00:00")),
            )
        self._append_independent_job_history(
            independent,
            event,
            disposition=disposition,
            released_at=now,
            cycle=cycle,
            result=completion,
            reason=reason,
        )
        if not preserve_enabled and disposition == "COMPLETED":
            settings = validate_trigger_settings(independent.get("trigger_settings"))
            recurring = settings["recovery"] or settings["interval_minutes"] is not None
            independent["enabled"] = bool(independent.get("enabled")) and recurring
        independent.update(
            active_event=None,
            cycle=0,
            completion_request=None,
            continuation_request=None,
            settings_reset_request=None,
            idle_since=now,
            tab_keep_open_until=(
                datetime.fromisoformat(now.replace("Z", "+00:00"))
                + timedelta(seconds=int(self.config.independent_idle_close_seconds))
            ).isoformat(),
            close_tab_when_idle=False,
            successor_task_id=None,
        )
        state.pop("completed_at", None)
        state.pop("stopped_at", None)
        state.pop("stop_reason", None)
        state["terminal_state"] = None
        state["kanban_column"] = INDEPENDENT_COLUMN
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None
        self._append_independent_waiting_hop(state, now=now)
        if independent.get("enabled") is True:
            state.update(
                status="WAITING",
                active_action="waiting_trigger",
                pause_reason=None,
                waiting_reason="waiting for trigger",
                waiting_code="trigger",
                waiting={
                    "reason": "trigger",
                    "waiting_on": [],
                    "stopped": [],
                    "missing": [],
                    "since": now,
                },
            )
        else:
            state.update(
                status="PAUSED",
                active_action="paused",
                pause_reason="independent agent disabled",
                waiting=None,
                waiting_reason=None,
                waiting_code=None,
            )
        return state

    def complete_independent_task(
        self,
        path: str | Path,
        *,
        outcome: str,
        summary: str,
        target_task_id: str | None = None,
        repair_task_id: str | None = None,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        request = normalize_completion_request(
            {
                "outcome": outcome,
                "summary": summary,
                "target_task_id": target_task_id,
                "repair_task_id": repair_task_id,
                "requested_at": utc_now(),
            }
        )

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if not is_independent_task(state):
                raise ValueError("task is not independent")
            independent = state["independent"]
            if external_command_id in state.get("applied_command_ids", []):
                return state
            active = independent.get("active_event")
            if not isinstance(active, Mapping):
                last = independent.get("last_outcome")
                history = independent.get("job_history") or []
                if (
                    isinstance(last, Mapping)
                    and {
                        key: value for key, value in normalize_completion_request(last).items()
                        if key != "requested_at"
                    }
                    == {
                        key: value for key, value in request.items()
                        if key != "requested_at"
                    }
                    and history
                    and history[-1].get("disposition") == "COMPLETED"
                ):
                    return state
                raise ValueError("independent task has no active event to complete")
            hop = self._active_independent_hop(state)
            if hop.get("state") != "responded":
                raise ValueError("independent completion requires a durable response")
            finalized_request = request
            pending = independent.get("completion_request")
            if pending is not None:
                normalized_pending = normalize_completion_request(pending)
                if {
                    key: value for key, value in normalized_pending.items()
                    if key != "requested_at"
                } != {
                    key: value for key, value in request.items()
                    if key != "requested_at"
                }:
                    raise ValueError("completion request changed before finalization")
                finalized_request = normalized_pending
            now = utc_now()
            response = str(hop.get("response") or "").strip()
            if response:
                report_id = f"{state['task_id']}-independent-hop{hop['hop_id']}"
                reports = state.setdefault("reports", [])
                if not any(item.get("report_id") == report_id for item in reports):
                    reports.append(
                        {
                            "report_id": report_id,
                            "role": INDEPENDENT_ROLE,
                            "hop_id": hop["hop_id"],
                            "created_at": now,
                            "content": response,
                            "sha256": hop.get("response_sha256"),
                            "outcome": finalized_request["outcome"],
                            "summary": finalized_request["summary"],
                        }
                    )
            independent["last_outcome"] = finalized_request
            self._record_external_command(state, external_command_id)
            return self._release_independent_job(
                state,
                disposition="COMPLETED",
                now=now,
                completion=finalized_request,
            )

        return self.update(path, mutate)

    def reset_independent_task(
        self,
        path: str | Path,
        *,
        reason: str,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_reason = sanitize_text(reason, max_chars=1200).strip()
        if not normalized_reason:
            raise ValueError("reset reason must not be empty")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if not is_independent_task(state):
                raise ValueError("task is not independent")
            if external_command_id in state.get("applied_command_ids", []):
                return state
            independent = state["independent"]
            event = independent.get("active_event")
            if not isinstance(event, Mapping):
                self._record_external_command(state, external_command_id)
                return state
            hop = self._active_independent_hop(state)
            if hop.get("state") not in {"responded", "routed", "abandoned"}:
                hop["state"] = "abandoned"
                hop.setdefault("timestamps", {})["abandoned_at"] = utc_now()
            self._record_external_command(state, external_command_id)
            return self._release_independent_job(
                state,
                disposition="RESET",
                now=utc_now(),
                reason=normalized_reason,
                preserve_enabled=True,
            )

        return self.update(path, mutate)

    def continue_independent_task(
        self,
        path: str | Path,
        *,
        reason: str,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = sanitize_text(reason, max_chars=1200).strip()
        if not normalized:
            raise ValueError("continuation reason must not be empty")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if not is_independent_task(state):
                raise ValueError("task is not independent")
            independent = state["independent"]
            cycle = int(independent.get("cycle") or 0)
            if independent_cycle_limit_reached(
                cycle=cycle,
                max_cycles=normalize_max_cycles(
                    independent.get("max_cycles"), default=0
                ),
            ):
                raise ValueError("independent maximum cycles reached")
            active_hop = next(
                item
                for item in state["hops"]
                if item.get("hop_id") == state["active_hop_id"]
            )
            if active_hop.get("state") != "responded":
                raise ValueError(
                    "independent continuation requires a durable response"
                )
            next_hop_id = max(int(item["hop_id"]) for item in state["hops"]) + 1
            next_cycle = cycle + 1
            hop = json.loads(json.dumps(active_hop, ensure_ascii=False))
            hop.update(
                hop_id=next_hop_id,
                parent_hop_id=active_hop["hop_id"],
                source_role=INDEPENDENT_ROLE,
                turn=next_hop_id,
                kind="independent_cycle",
                handoff=normalized,
                state="pre_send",
                request_id=f"{state['task_id']}-hop{next_hop_id}",
                prompt=None,
                prompt_sha256=None,
                rendered_prompt_sha256=None,
                receipt=None,
                message_identity=None,
                response=None,
                response_sha256=None,
                report_path=None,
                report_sha256=None,
                report_size=None,
                route=None,
                repair_attempt=0,
                validation_error=None,
                wait={key: None for key in active_hop["wait"]},
                timestamps={"created_at": utc_now()},
                errors=[],
            )
            hop["wait"].update(
                activity_length=0,
                transport_ui_active=False,
                last_stop_visible=False,
                refresh_count=0,
            )
            state["hops"].append(hop)
            state.update(
                active_hop_id=next_hop_id,
                active_role=INDEPENDENT_ROLE,
                status="RUNNING",
                kanban_column=INDEPENDENT_COLUMN,
                active_action="queued",
            )
            self._record_external_command(state, external_command_id)
            pending = independent.get("continuation_request")
            if pending is not None and str(pending.get("reason") or "") != normalized:
                raise ValueError("continuation request changed before finalization")
            independent.update(
                cycle=next_cycle,
                continuation_request=None,
                completion_request=None,
            )
            return state

        return self.update(path, mutate)

    def run_independent_now(
        self,
        path: str | Path,
        *,
        trigger_type: str = "manual",
        instruction: str | None = None,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_type = str(trigger_type or "manual").strip().lower()
        if normalized_type not in {"manual", "check_all"}:
            raise ValueError("run-now trigger_type must be manual or check_all")
        normalized_instruction = (
            normalize_manual_instruction(instruction) if instruction is not None else None
        )
        if normalized_instruction is not None and normalized_type != "manual":
            raise ValueError("instruction is valid only for a manual run")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if not is_independent_task(state):
                raise ValueError("task is not independent")
            independent = state["independent"]
            if independent.get("enabled") is not True:
                raise ValueError("independent agent is disabled")
            if independent.get("active_event") is not None:
                raise ValueError("independent agent already has an active job")
            if str(state.get("status") or "").upper() != "WAITING":
                raise ValueError("run now requires an idle WAITING agent")
            watermarks = independent.setdefault("watermarks", {})
            count = int(watermarks.get("manual_count") or 0) + 1
            watermarks["manual_count"] = count
            event = {
                "event_key": f"{normalized_type}:{independent['agent_key']}:{count}",
                "trigger_type": normalized_type,
                "occurred_at": utc_now(),
                "target_team": None,
                "target_task_id": None,
                "target_role": None,
                "target_hop_id": None,
                "failure_signature": None,
                "occurrence_count": count,
                "check_count": count,
            }
            if normalized_instruction is not None:
                event["instruction"] = normalized_instruction
            independent["active_event"] = event
            independent["cycle"] = 1
            independent["completion_request"] = None
            independent["continuation_request"] = None
            independent["idle_since"] = None
            state["status"] = "RUNNING"
            state["kanban_column"] = INDEPENDENT_COLUMN
            state["active_action"] = "queued"
            state["waiting"] = None
            state["waiting_reason"] = None
            state["waiting_code"] = None
            hop = next(
                item
                for item in state["hops"]
                if item.get("hop_id") == state.get("active_hop_id")
            )
            hop["state"] = "pre_send"
            hop["handoff"] = json.dumps(event, ensure_ascii=False, sort_keys=True)
            self._record_external_command(state, external_command_id)
            return state

        return self.update(path, mutate)

    def _reset_independent_cycle_by_settings(
        self,
        state: dict[str, Any],
        *,
        now: str,
    ) -> dict[str, Any]:
        independent = state["independent"]
        event = independent.get("active_event")
        if not isinstance(event, Mapping):
            independent["cycle"] = 0
            independent["settings_reset_request"] = None
            return state
        hop = self._active_independent_hop(state)
        if hop.get("state") not in {"responded", "routed", "abandoned"}:
            hop["state"] = "abandoned"
            hop.setdefault("timestamps", {})["abandoned_at"] = now
        self._append_independent_job_history(
            independent,
            event,
            disposition="RESET_BY_SETTINGS",
            released_at=now,
            cycle=int(independent.get("cycle") or 0),
            reason="Max turns per job changed",
        )
        next_hop = self._append_independent_waiting_hop(state, now=now)
        next_hop["state"] = "pre_send"
        next_hop["handoff"] = json.dumps(event, ensure_ascii=False, sort_keys=True)
        independent.update(
            cycle=1,
            completion_request=None,
            continuation_request=None,
            settings_reset_request=None,
            idle_since=None,
            tab_keep_open_until=None,
        )
        state.update(
            status="RUNNING",
            kanban_column=INDEPENDENT_COLUMN,
            active_action="queued",
            waiting=None,
            waiting_reason=None,
            waiting_code=None,
            pause_reason=None,
        )
        return state

    def finalize_independent_settings_reset(self, path: str | Path) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if not is_independent_task(state):
                raise ValueError("task is not independent")
            independent = state["independent"]
            if not isinstance(independent.get("settings_reset_request"), Mapping):
                return state
            hop = self._active_independent_hop(state)
            if hop.get("state") != "responded":
                raise ValueError("settings reset requires a durable response boundary")
            return self._reset_independent_cycle_by_settings(state, now=utc_now())

        return self.update(path, mutate)

    def update_independent_agent(
        self,
        path: str | Path,
        *,
        enabled: bool | None = None,
        display_name: str | None = None,
        system_prompt: str | None = None,
        trigger_settings: Mapping[str, Any] | None = None,
        max_cycles: int | None = None,
        new_chat_next_job: bool | None = None,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_display = (
            normalize_workflow_display_name(display_name)
            if display_name is not None
            else None
        )
        normalized_prompt = (
            normalize_system_prompt(system_prompt) if system_prompt is not None else None
        )
        normalized_settings = (
            validate_trigger_settings(trigger_settings)
            if trigger_settings is not None
            else None
        )
        normalized_max_cycles = (
            normalize_max_cycles(max_cycles) if max_cycles is not None else None
        )
        target = Path(path).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            catalog = self._load_catalog_unlocked(reconcile=False)
            tasks = self._discover_with_catalog_unlocked(catalog)[0]
            with exclusive_file_lock(self._lock_path(target)):
                current = self._load_current_manifest_unlocked(target)
                self._assert_manifest_mutable_unlocked(target, current)
                if not is_independent_task(current):
                    raise ValueError("task is not independent")
                prospective_enabled = (
                    bool(enabled)
                    if enabled is not None
                    else bool(current["independent"]["enabled"])
                )
                prospective_settings = (
                    normalized_settings
                    if normalized_settings is not None
                    else validate_trigger_settings(
                        current["independent"]["trigger_settings"]
                    )
                )
                if prospective_enabled and prospective_settings["recovery"]:
                    owner = self._exclusive_recovery_owner(
                        tasks,
                        agent_key=str(
                            current["independent"].get("agent_key") or ""
                        ),
                    )
                    if owner is not None:
                        raise ValueError(
                            "exclusive recovery trigger is already owned by "
                            f"{owner['independent']['agent_name']!r}"
                        )

                independent = current["independent"]
                previous_recovery_owner = bool(independent.get("enabled")) and bool(
                    validate_trigger_settings(
                        independent.get("trigger_settings")
                    )["recovery"]
                )
                transition_at = utc_now()
                if external_command_id in current.get("applied_command_ids", []):
                    return current
                if independent.get("deleted_at"):
                    raise ValueError("independent agent is deleted")
                if normalized_display is not None:
                    independent["display_name"] = normalized_display
                if normalized_prompt is not None:
                    independent["system_prompt"] = normalized_prompt
                    current["roles"][INDEPENDENT_ROLE][
                        "constructor_sent_generation"
                    ] = None
                if normalized_settings is not None:
                    previous_settings = validate_trigger_settings(
                        independent.get("trigger_settings")
                    )
                    previous_interval = previous_settings["interval_minutes"]
                    independent["trigger_settings"] = normalized_settings
                    next_interval = normalized_settings["interval_minutes"]
                    if next_interval is None:
                        independent.setdefault("watermarks", {}).pop(
                            "last_interval_slot", None
                        )
                    elif next_interval != previous_interval:
                        independent.setdefault("watermarks", {})[
                            "last_interval_slot"
                        ] = int(
                            datetime.now(timezone.utc).timestamp()
                            // (int(next_interval) * 60)
                        )
                if normalized_max_cycles is not None:
                    previous_max_cycles = normalize_max_cycles(
                        independent.get("max_cycles"), default=0
                    )
                    independent["max_cycles"] = normalized_max_cycles
                    independent["max_cycles_explicit"] = True
                    if normalized_max_cycles != previous_max_cycles:
                        active_event = independent.get("active_event")
                        if isinstance(active_event, Mapping):
                            hop = self._active_independent_hop(current)
                            accepted = hop.get("receipt") is not None or str(
                                hop.get("state") or ""
                            ) in {"sending", "sent", "waiting"}
                            if accepted and hop.get("state") != "responded":
                                independent["settings_reset_request"] = {
                                    "event_key": active_event.get("event_key"),
                                    "requested_at": utc_now(),
                                    "max_cycles": normalized_max_cycles,
                                    "source_hop_id": hop.get("hop_id"),
                                }
                            else:
                                self._reset_independent_cycle_by_settings(
                                    current, now=utc_now()
                                )
                        else:
                            independent["cycle"] = 0
                            independent["settings_reset_request"] = None
                if new_chat_next_job is not None:
                    independent["new_chat_next_job"] = bool(new_chat_next_job)
                if enabled is not None:
                    independent["enabled"] = bool(enabled)
                    active = independent.get("active_event") is not None
                    if enabled:
                        current["status"] = "RUNNING" if active else "WAITING"
                        current["kanban_column"] = INDEPENDENT_COLUMN
                        current["active_action"] = (
                            "resuming" if active else "waiting_trigger"
                        )
                        current["pause_reason"] = None
                        if not active:
                            now = transition_at
                            current["waiting"] = {
                                "reason": "trigger",
                                "waiting_on": [],
                                "stopped": [],
                                "missing": [],
                                "since": now,
                            }
                            current["waiting_reason"] = "waiting for trigger"
                            current["waiting_code"] = "trigger"
                            independent["idle_since"] = now
                            independent["tab_keep_open_until"] = None
                    else:
                        current["status"] = "PAUSED"
                        current["kanban_column"] = "PAUSED"
                        current["active_action"] = "paused"
                        current["pause_reason"] = "independent agent disabled"
                        current["waiting"] = None
                        current["waiting_reason"] = None
                        current["waiting_code"] = None
                refresh_recovery_warmup_on_enable(
                    independent,
                    was_owner=previous_recovery_owner,
                    is_owner=bool(independent.get("enabled"))
                    and bool(
                        validate_trigger_settings(
                            independent.get("trigger_settings")
                        )["recovery"]
                    ),
                    enabled_at=transition_at,
                )
                self._record_external_command(current, external_command_id)
                saved = self._save_unlocked(target, current)
            catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
            self._write_catalog_unlocked(catalog)
            return saved

    def delete_independent_agent(
        self,
        path: str | Path,
        *,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            catalog = self._load_catalog_unlocked(reconcile=False)
            with exclusive_file_lock(self._lock_path(target)):
                current = self._load_current_manifest_unlocked(target)
                self._assert_manifest_mutable_unlocked(target, current)
                if not is_independent_task(current):
                    raise ValueError("task is not independent")
                independent = current["independent"]
                agent_key = str(independent.get("agent_key") or "")
                if agent_key.casefold() in {"maintainers", "monitor"}:
                    raise ValueError("built-in independent agents cannot be deleted")
                if external_command_id in current.get("applied_command_ids", []):
                    return current
                if independent.get("deleted_at"):
                    self._record_external_command(current, external_command_id)
                    saved = self._save_unlocked(target, current)
                    catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
                    self._write_catalog_unlocked(catalog)
                    return saved
                if independent.get("active_event") is not None:
                    raise ValueError("independent agent has an active event")
                active_hop_id = current.get("active_hop_id")
                active_hop = next(
                    (
                        item
                        for item in current.get("hops") or []
                        if item.get("hop_id") == active_hop_id
                    ),
                    None,
                )
                if isinstance(active_hop, Mapping) and str(
                    active_hop.get("state") or ""
                ) in {"sending", "sent", "waiting", "responding", "refreshing"}:
                    raise ValueError(
                        "independent agent has an accepted or in-flight request"
                    )
                now = utc_now()
                independent["enabled"] = False
                independent["deleted_at"] = now
                current["status"] = "STOPPED"
                current["terminal_state"] = "STOPPED"
                current["kanban_column"] = "STOPPED"
                current["stopped_at"] = now
                current["stop_reason"] = "independent agent deleted"
                current["active_role"] = None
                current["active_hop_id"] = None
                current["active_action"] = "stopped"
                current["waiting"] = None
                current["waiting_reason"] = None
                current["waiting_code"] = None
                self._record_external_command(current, external_command_id)
                saved = self._save_unlocked(target, current)
            catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
            self._write_catalog_unlocked(catalog)
            return saved
