from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from .cdpa_runtime_db import IdempotencyConflict, RuntimeDB


_STATE_NAME = "conversation_runtime"
_TERMINAL_ATTEMPT_STATES = frozenset({"accepted", "reconciled_accepted", "reconciled_absent"})
_ATTEMPT_TRANSITIONS = {
    "intent_recorded": frozenset({"submitted", "accepted", "unconfirmed", "reconciled_accepted", "reconciled_absent"}),
    "submitted": frozenset({"accepted", "unconfirmed", "reconciled_accepted", "reconciled_absent"}),
    "unconfirmed": frozenset({"accepted", "reconciled_accepted", "reconciled_absent"}),
    "accepted": frozenset(),
    "reconciled_accepted": frozenset(),
    "reconciled_absent": frozenset(),
}


class ConversationLeaseError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retryable = retryable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(prefix: str, *parts: object) -> str:
    raw = "\n".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def canonical_conversation(url: str) -> str:
    """Return the exact ChatGPT canonical conversation id or fail closed."""
    raw = str(url or "").strip()
    if raw != url or not raw:
        raise ConversationLeaseError(
            "CONVERSATION_IDENTITY_MISMATCH",
            "conversation URL is empty or contains surrounding whitespace",
        )
    parsed = urlparse(raw)
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "chatgpt.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise ConversationLeaseError(
            "CONVERSATION_IDENTITY_MISMATCH",
            "conversation URL is not an exact ChatGPT conversation",
        )
    pieces = [piece for piece in parsed.path.split("/") if piece]
    if len(pieces) == 2 and pieces[0] == "c" and pieces[1]:
        conversation_id = pieces[1]
    elif (
        len(pieces) == 4
        and pieces[0] == "g"
        and pieces[1]
        and pieces[2] == "c"
        and pieces[3]
    ):
        # ChatGPT Project conversations render under /g/<project>/c/<conversation>.
        # The project segment is routing context; the canonical conversation
        # identity remains the exact final /c/<id> segment.
        conversation_id = pieces[3]
    else:
        raise ConversationLeaseError(
            "CONVERSATION_IDENTITY_MISMATCH",
            "canonical conversation id is unavailable",
        )
    if unquote(conversation_id).lower().startswith("local-chatgpt:"):
        raise ConversationLeaseError(
            "CONVERSATION_IDENTITY_MISMATCH",
            "local ChatGPT placeholder is provisional, not a canonical conversation id",
        )
    return conversation_id


def _local_chatgpt_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    pieces = [piece for piece in parsed.path.split("/") if piece]
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() == "chatgpt.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443}
        and not parsed.fragment
        and len(pieces) == 2
        and pieces[0] == "c"
        and unquote(pieces[1]).lower().startswith("local-chatgpt:")
    )


def _new_chat_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() == "chatgpt.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443}
        and parsed.path in {"", "/"}
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def _temporary_chat_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() == "chatgpt.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443}
        and parsed.path in {"", "/"}
        and parsed.query == "temporary-chat=true"
        and not parsed.fragment
    )


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "bindings": {},
        "page_claims": {},
        "leases": {},
        "lease_epochs": {},
        "quarantine": {},
    }


class ConversationWriterLease:
    """ConversationBinding + writer-lease owner backed by the existing RuntimeDB.

    Binding/lease state is stored in RuntimeDB's existing snapshot table. Browser
    SendAttempt identity is stored in the existing command ledger. The send
    boundary verifies writer key + lease_epoch and inserts the intent in the same
    SQLite transaction, so a stale owner fails before composer mutation.
    """

    def __init__(self, db: RuntimeDB) -> None:
        self.db = db
        self.db.ensure_schema()

    @staticmethod
    def _decode_state(row: Any | None) -> dict[str, Any]:
        if row is None:
            return _default_state()
        try:
            state = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConversationLeaseError(
                "CONVERSATION_STATE_INVALID", "stored conversation state is malformed"
            ) from exc
        if not isinstance(state, dict):
            raise ConversationLeaseError(
                "CONVERSATION_STATE_INVALID", "stored conversation state is not an object"
            )
        for key in ("bindings", "page_claims", "leases", "lease_epochs", "quarantine"):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        state.setdefault("version", 1)
        return state

    @staticmethod
    def _load_state(connection: Any) -> tuple[dict[str, Any], int]:
        row = connection.execute(
            "SELECT version, payload_json FROM runtime_snapshot WHERE name=?",
            (_STATE_NAME,),
        ).fetchone()
        return (
            ConversationWriterLease._decode_state(row),
            int(row["version"]) if row is not None else 0,
        )

    @staticmethod
    def _store_state(connection: Any, state: Mapping[str, Any], version: int) -> None:
        encoded = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection.execute(
            """
            INSERT INTO runtime_snapshot(name, version, updated_at, payload_json)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                version=excluded.version,
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json
            """,
            (_STATE_NAME, version + 1, _utc_now(), encoded),
        )

    @staticmethod
    def _page_key(
        provider_id: str, physical_browser_id: str, page_target_identity: str
    ) -> str:
        return f"{provider_id}:{physical_browser_id}:{page_target_identity}"

    @staticmethod
    def _canonical_key(provider_id: str, conversation_id: str) -> str:
        return f"conversation:{provider_id}:{conversation_id}"

    @staticmethod
    def _provisional_key(
        provider_id: str, physical_browser_id: str, page_binding_id: str
    ) -> str:
        return f"page:{provider_id}:{physical_browser_id}:{page_binding_id}"

    @staticmethod
    def _binding_projection(binding: Mapping[str, Any]) -> dict[str, Any]:
        return dict(binding)

    @staticmethod
    def _lease_projection(
        binding: Mapping[str, Any], lease: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            **dict(binding),
            "writer_identity_key": lease["writer_identity_key"],
            "lease_epoch": int(lease["lease_epoch"]),
            "owner_binding_id": lease["owner_binding_id"],
            "lease_state": lease.get("state", "active"),
        }

    def _binding_or_error(
        self, state: Mapping[str, Any], binding_id: str
    ) -> dict[str, Any]:
        binding = state["bindings"].get(binding_id)
        if not isinstance(binding, dict):
            raise ConversationLeaseError(
                "CONVERSATION_BINDING_NOT_FOUND", f"unknown binding {binding_id!r}"
            )
        return binding

    @staticmethod
    def _quarantine_error(record: Mapping[str, Any]) -> ConversationLeaseError:
        permanent = bool(record.get("permanent"))
        return ConversationLeaseError(
            "WRITER_LEASE_QUARANTINED",
            "canonical conversation is quarantined pending reconciliation",
            retryable=not permanent,
        )

    def bind(
        self,
        *,
        binding_id: str,
        provider_id: str,
        endpoint_id: str,
        endpoint_cdp_url: str,
        physical_browser_id: str,
        page_target_identity: str,
        conversation_url: str,
        verified_new_chat: bool,
        temporary_chat: bool = False,
        temporary_document_id: str | None = None,
        task_id: str | None = None,
        role_key: str | None = None,
    ) -> dict[str, Any]:
        for label, value in (
            ("binding_id", binding_id),
            ("provider_id", provider_id),
            ("endpoint_id", endpoint_id),
            ("endpoint_cdp_url", endpoint_cdp_url),
            ("physical_browser_id", physical_browser_id),
            ("page_target_identity", page_target_identity),
        ):
            if not str(value or "").strip():
                raise ConversationLeaseError(
                    "CONVERSATION_BINDING_INVALID", f"{label} is required"
                )

        if temporary_chat:
            if not verified_new_chat or not _temporary_chat_url(conversation_url):
                raise ConversationLeaseError(
                    "CONVERSATION_IDENTITY_MISMATCH",
                    "temporary chat requires an exact verified Temporary Chat URL",
                )
            if not str(temporary_document_id or "").strip():
                raise ConversationLeaseError(
                    "CONVERSATION_BINDING_INVALID",
                    "temporary_document_id is required for Temporary Chat",
                )
            canonical_id: str | None = None
        else:
            try:
                canonical_id = canonical_conversation(conversation_url)
            except ConversationLeaseError:
                if verified_new_chat and (
                    _new_chat_url(conversation_url) or _local_chatgpt_url(conversation_url)
                ):
                    canonical_id = None
                else:
                    raise

        page_binding_id = _digest(
            "page", provider_id, physical_browser_id, page_target_identity
        )
        page_key = self._page_key(
            provider_id, physical_browser_id, page_target_identity
        )
        writer_key = (
            f"temporary:{provider_id}:{physical_browser_id}:{str(temporary_document_id).strip()}"
            if temporary_chat
            else self._canonical_key(provider_id, canonical_id)
            if canonical_id is not None
            else self._provisional_key(
                provider_id, physical_browser_id, page_binding_id
            )
        )
        now = _utc_now()

        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            existing = state["bindings"].get(binding_id)
            if isinstance(existing, dict):
                requested = {
                    "provider_id": provider_id,
                    "endpoint_id": endpoint_id,
                    "endpoint_cdp_url": endpoint_cdp_url,
                    "physical_browser_id": physical_browser_id,
                    "page_target_identity": page_target_identity,
                    "conversation_url": conversation_url,
                    "temporary_chat": bool(temporary_chat),
                    "temporary_document_id": str(temporary_document_id or "") or None,
                    "task_id": task_id,
                    "role_key": role_key,
                }
                if all(
                    existing.get(key) == value for key, value in requested.items()
                ):
                    connection.commit()
                    return self._binding_projection(existing)
                connection.rollback()
                raise ConversationLeaseError(
                    "CONVERSATION_IDENTITY_MISMATCH",
                    "binding id is already owned by different evidence",
                )

            claimed = state["page_claims"].get(page_key)
            if claimed and claimed != binding_id:
                other = state["bindings"].get(claimed)
                if (
                    isinstance(other, dict)
                    and other.get("binding_status") != "sealed"
                ):
                    connection.rollback()
                    raise ConversationLeaseError(
                        "WRITER_LEASE_BUSY",
                        "physical page target already has a live writer binding",
                        retryable=True,
                    )

            binding = {
                "binding_id": binding_id,
                "provider_id": provider_id,
                "endpoint_id": endpoint_id,
                "endpoint_cdp_url": endpoint_cdp_url,
                "physical_browser_id": physical_browser_id,
                "page_target_identity": page_target_identity,
                "page_binding_id": page_binding_id,
                "conversation_url": conversation_url,
                "canonical_conversation_id": canonical_id,
                "verified_new_chat": bool(verified_new_chat),
                "temporary_chat": bool(temporary_chat),
                "temporary_document_id": str(temporary_document_id or "") or None,
                "writer_identity_key": writer_key,
                "provisional_writer_identity_key": (
                    writer_key if canonical_id is None else None
                ),
                "task_id": task_id,
                "role_key": role_key,
                "binding_status": "idle",
                "reconciliation_only": False,
                "baseline_required": False,
                "lease_epoch": None,
                "created_at": now,
                "updated_at": now,
            }
            state["bindings"][binding_id] = binding
            state["page_claims"][page_key] = binding_id
            self._store_state(connection, state, version)
            connection.commit()
            return self._binding_projection(binding)

    def get_binding(self, binding_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            state, _ = self._load_state(connection)
        return self._binding_projection(self._binding_or_error(state, binding_id))

    def list_bindings(self, *, include_sealed: bool = False) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            state, _ = self._load_state(connection)
        result = [
            self._binding_projection(binding)
            for binding in state["bindings"].values()
            if isinstance(binding, dict)
            and (include_sealed or binding.get("binding_status") != "sealed")
        ]
        result.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("binding_id") or "")))
        return result

    def binding_for_task_role(
        self, task_id: str, role_key: str
    ) -> dict[str, Any] | None:
        task = str(task_id or "").strip()
        role = str(role_key or "").strip().upper()
        matches = [
            binding
            for binding in self.list_bindings()
            if str(binding.get("task_id") or "") == task
            and str(binding.get("role_key") or "").upper() == role
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ConversationLeaseError(
                "CONVERSATION_BINDING_AMBIGUOUS",
                f"task/role has {len(matches)} live bindings",
            )
        return matches[0]

    def update_dom_projection(
        self, binding_id: str, projection: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            binding["dom_projection"] = json.loads(
                json.dumps(dict(projection), ensure_ascii=False)
            )
            binding["dom_projection_version"] = int(
                binding.get("dom_projection_version") or 0
            ) + 1
            binding["dom_projection_updated_at"] = _utc_now()
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return self._binding_projection(binding)

    def endpoint_in_use(self, endpoint_id: str) -> bool:
        with self.db.connection() as connection:
            state, _ = self._load_state(connection)
        return any(
            isinstance(binding, dict)
            and binding.get("endpoint_id") == endpoint_id
            and binding.get("binding_status") != "sealed"
            for binding in state["bindings"].values()
        )

    def acquire(
        self, binding_id: str, *, require_idle: bool = False
    ) -> dict[str, Any]:
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            if require_idle and binding.get("binding_status") != "idle":
                connection.rollback()
                raise ConversationLeaseError(
                    "WRITER_LEASE_BUSY",
                    "binding is not idle for an operator-owned send",
                    retryable=True,
                )
            if binding.get("binding_status") in {"sealed", "blocked"}:
                connection.rollback()
                raise ConversationLeaseError(
                    "WRITER_LEASE_LOST", "binding is not writable"
                )
            key = str(binding["writer_identity_key"])
            quarantine = state["quarantine"].get(key)
            if isinstance(quarantine, dict):
                connection.rollback()
                raise self._quarantine_error(quarantine)
            lease = state["leases"].get(key)
            if isinstance(lease, dict):
                if lease.get("owner_binding_id") != binding_id:
                    connection.rollback()
                    raise ConversationLeaseError(
                        "WRITER_LEASE_BUSY",
                        "writer identity is owned by another binding",
                        retryable=True,
                    )
                if str(lease.get("state") or "active") == "reserved_async_wait":
                    connection.rollback()
                    raise ConversationLeaseError(
                        "WRITER_LEASE_RESERVED",
                        "writer identity is reserved for this binding's async wake",
                        retryable=True,
                    )
            else:
                epoch = int(state["lease_epochs"].get(key) or 0) + 1
                state["lease_epochs"][key] = epoch
                lease = {
                    "writer_identity_key": key,
                    "owner_binding_id": binding_id,
                    "lease_epoch": epoch,
                    "state": "active",
                    "acquired_at": _utc_now(),
                }
                state["leases"][key] = lease
            binding["binding_status"] = "active"
            binding["lease_epoch"] = int(lease["lease_epoch"])
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return self._lease_projection(binding, lease)

    def reserve_async_wait(self, binding_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            key = str(binding["writer_identity_key"])
            lease = state["leases"].get(key)
            if (
                not isinstance(lease, dict)
                or lease.get("owner_binding_id") != binding_id
                or binding.get("binding_status") != "active"
                or str(lease.get("state") or "active") != "active"
            ):
                connection.rollback()
                raise ConversationLeaseError(
                    "WRITER_LEASE_LOST",
                    "async WAIT may reserve only the active writer lease owner",
                )
            lease["state"] = "reserved_async_wait"
            lease["reserved_at"] = _utc_now()
            binding["binding_status"] = "reserved_async_wait"
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return self._lease_projection(binding, lease)

    def resume_async_wait(self, binding_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            key = str(binding["writer_identity_key"])
            quarantine = state["quarantine"].get(key)
            if isinstance(quarantine, dict):
                connection.rollback()
                raise self._quarantine_error(quarantine)
            lease = state["leases"].get(key)
            if (
                not isinstance(lease, dict)
                or lease.get("owner_binding_id") != binding_id
                or binding.get("binding_status") != "reserved_async_wait"
                or str(lease.get("state") or "") != "reserved_async_wait"
            ):
                connection.rollback()
                raise ConversationLeaseError(
                    "WRITER_LEASE_LOST",
                    "async wake no longer owns its reserved writer lease",
                )
            lease["state"] = "active"
            lease["resumed_at"] = _utc_now()
            binding["binding_status"] = "active"
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return self._lease_projection(binding, lease)

    def release_async_wait(
        self, binding_id: str, *, seal: bool = False
    ) -> dict[str, Any]:
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            key = str(binding["writer_identity_key"])
            lease = state["leases"].get(key)
            if isinstance(lease, dict) and lease.get("owner_binding_id") == binding_id:
                if str(lease.get("state") or "") not in {
                    "reserved_async_wait",
                    "active",
                }:
                    connection.rollback()
                    raise ConversationLeaseError(
                        "WRITER_LEASE_LOST",
                        "writer reservation is not releasable",
                    )
                state["leases"].pop(key, None)
            if seal:
                self._release_page_claim(state, binding)
                binding["binding_status"] = "sealed"
            else:
                binding["binding_status"] = "idle"
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return self._binding_projection(binding)

    @staticmethod
    def _attempt_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        attempt = payload.get("send_attempt")
        if not isinstance(attempt, dict):
            raise ConversationLeaseError(
                "SEND_ATTEMPT_INVALID", "browser_send ledger payload is malformed"
            )
        return dict(attempt)

    def record_send_intent(
        self,
        lease_view: Mapping[str, Any],
        *,
        idempotency_key: str,
        text: str,
        baseline_last_user_message_id: str | None,
        baseline_user_turn_count: int,
    ) -> tuple[dict[str, Any], bool]:
        binding_id = str(lease_view.get("binding_id") or "")
        writer_key = str(lease_view.get("writer_identity_key") or "")
        try:
            lease_epoch = int(lease_view.get("lease_epoch"))
        except (TypeError, ValueError) as exc:
            raise ConversationLeaseError(
                "WRITER_LEASE_LOST", "lease epoch is missing"
            ) from exc
        if not idempotency_key.strip():
            raise ValueError("idempotency key is required")

        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            if binding.get("binding_status") != "active":
                connection.rollback()
                raise ConversationLeaseError(
                    "WRITER_LEASE_LOST",
                    "binding no longer owns an active writer lease",
                )
            if isinstance(state["quarantine"].get(writer_key), dict):
                connection.rollback()
                raise self._quarantine_error(state["quarantine"][writer_key])
            current = state["leases"].get(writer_key)
            if (
                not isinstance(current, dict)
                or current.get("owner_binding_id") != binding_id
                or int(current.get("lease_epoch") or -1) != lease_epoch
                or binding.get("writer_identity_key") != writer_key
            ):
                connection.rollback()
                raise ConversationLeaseError(
                    "WRITER_LEASE_LOST", "writer lease owner or epoch changed"
                )

            attempt_id = _digest("send", idempotency_key)
            baseline = {
                "last_user_message_id": baseline_last_user_message_id,
                "user_turn_count": int(baseline_user_turn_count),
            }
            core = {
                "attempt_id": attempt_id,
                "writer_identity_key": writer_key,
                "owner_binding_id": binding_id,
                "lease_epoch": lease_epoch,
                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "baseline": baseline,
            }
            existing = connection.execute(
                "SELECT * FROM command_queue WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                try:
                    payload = json.loads(existing["payload_json"])
                except json.JSONDecodeError as exc:
                    connection.rollback()
                    raise ConversationLeaseError(
                        "SEND_ATTEMPT_INVALID", "stored send attempt is malformed"
                    ) from exc
                prior = self._attempt_from_payload(payload)
                same = existing["kind"] == "browser_send" and all(
                    prior.get(key) == value for key, value in core.items()
                )
                connection.commit()
                if not same:
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different browser send"
                    )
                return prior, False

            attempt = {
                **core,
                "state": "intent_recorded",
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
            }
            payload = json.dumps(
                {"send_attempt": attempt},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO command_queue(
                    command_id, idempotency_key, kind, task_id, status,
                    expected_task_version, created_at, payload_json
                ) VALUES(?, ?, 'browser_send', ?, 'recovery_required', NULL, ?, ?)
                """,
                (
                    attempt_id,
                    idempotency_key,
                    binding.get("task_id"),
                    _utc_now(),
                    payload,
                ),
            )
            binding["baseline_required"] = False
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return dict(attempt), True

    def _command_attempt(
        self, connection: Any, attempt_id: str
    ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        row = connection.execute(
            "SELECT * FROM command_queue WHERE command_id=?", (attempt_id,)
        ).fetchone()
        if row is None or row["kind"] != "browser_send":
            raise ConversationLeaseError(
                "SEND_ATTEMPT_NOT_FOUND", f"unknown send attempt {attempt_id!r}"
            )
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            raise ConversationLeaseError(
                "SEND_ATTEMPT_INVALID", "stored send attempt is malformed"
            ) from exc
        if not isinstance(payload, dict):
            raise ConversationLeaseError(
                "SEND_ATTEMPT_INVALID", "stored send payload is not an object"
            )
        return row, payload, self._attempt_from_payload(payload)

    def transition_attempt(
        self,
        attempt_id: str,
        desired_state: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        desired = str(desired_state)
        updates = dict(metadata or {})
        protected = {
            "attempt_id",
            "state",
            "owner_binding_id",
            "writer_identity_key",
            "lease_epoch",
            "baseline",
            "text_hash",
            "created_at",
        }
        if protected.intersection(updates):
            raise ConversationLeaseError(
                "SEND_ATTEMPT_INVALID",
                "send attempt metadata cannot replace durable identity fields",
            )
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            _, payload, attempt = self._command_attempt(connection, attempt_id)
            current = str(attempt.get("state") or "")
            if desired not in _ATTEMPT_TRANSITIONS.get(current, frozenset()):
                connection.rollback()
                raise ConversationLeaseError(
                    "SEND_ATTEMPT_TRANSITION",
                    f"invalid browser send transition {current!r} -> {desired!r}",
                )
            attempt.update(
                json.loads(json.dumps(updates, ensure_ascii=False))
            )
            attempt["state"] = desired
            attempt["updated_at"] = _utc_now()
            payload["send_attempt"] = attempt
            terminal = desired in _TERMINAL_ATTEMPT_STATES
            connection.execute(
                """
                UPDATE command_queue
                SET status=?, finished_at=?, payload_json=?
                WHERE command_id=?
                """,
                (
                    "applied" if terminal else "recovery_required",
                    _utc_now() if terminal else None,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    attempt_id,
                ),
            )
            if desired in {"reconciled_absent", "reconciled_accepted"}:
                self._resolve_quarantine_after_attempt(
                    state, str(attempt["owner_binding_id"])
                )
            self._store_state(connection, state, version)
            connection.commit()
            return dict(attempt)

    def reconcile_attempt(
        self, attempt_id: str, evidence: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self.db.connection() as connection:
            _, _, attempt = self._command_attempt(connection, attempt_id)
        baseline = (
            attempt.get("baseline")
            if isinstance(attempt.get("baseline"), Mapping)
            else {}
        )
        try:
            observed_count = int(evidence.get("user_turn_count") or 0)
            baseline_count = int(baseline.get("user_turn_count") or 0)
        except (TypeError, ValueError):
            observed_count = baseline_count = 0
        observed_id = evidence.get("last_user_message_id")
        baseline_id = baseline.get("last_user_message_id")
        accepted = (
            observed_count > baseline_count
            and bool(observed_id)
            and observed_id != baseline_id
        )
        desired = "reconciled_accepted" if accepted else "unconfirmed"
        current = str(attempt.get("state") or "")
        if current == desired:
            return attempt
        return self.transition_attempt(attempt_id, desired)


    def release_writer(self, binding_id: str) -> dict[str, Any]:
        """Release an idle/pre-send writer only after every SendAttempt is terminal."""
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            unresolved = [
                attempt
                for attempt in self._attempts_for_binding(connection, binding_id)
                if attempt.get("state") not in _TERMINAL_ATTEMPT_STATES
            ]
            if unresolved:
                connection.rollback()
                raise ConversationLeaseError(
                    "WRITER_LEASE_BUSY",
                    "writer has unresolved send evidence",
                    retryable=True,
                )
            key = str(binding["writer_identity_key"])
            lease = state["leases"].get(key)
            if isinstance(lease, dict):
                if lease.get("owner_binding_id") != binding_id:
                    connection.rollback()
                    raise ConversationLeaseError(
                        "WRITER_LEASE_BUSY",
                        "writer identity is owned by another binding",
                        retryable=True,
                    )
                state["leases"].pop(key, None)
            binding["binding_status"] = "idle"
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return self._binding_projection(binding)

    def finish_turn(
        self,
        binding_id: str,
        *,
        assistant_terminal: bool,
        turn_committed: bool,
    ) -> dict[str, Any]:
        if not assistant_terminal or not turn_committed:
            raise ConversationLeaseError(
                "TURN_NOT_TERMINAL",
                "writer lease spans assistant terminal state and route/turn commit",
            )
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            key = str(binding["writer_identity_key"])
            lease = state["leases"].get(key)
            if (
                isinstance(lease, dict)
                and lease.get("owner_binding_id") == binding_id
            ):
                state["leases"].pop(key, None)
            provisional = binding.get("provisional_writer_identity_key")
            if provisional and provisional != key:
                p_lease = state["leases"].get(provisional)
                if (
                    isinstance(p_lease, dict)
                    and p_lease.get("owner_binding_id") == binding_id
                ):
                    state["leases"].pop(provisional, None)
            binding["binding_status"] = "idle"
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return self._binding_projection(binding)

    def _attempts_for_binding(
        self, connection: Any, binding_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT payload_json FROM command_queue WHERE kind='browser_send'"
        ).fetchall()
        attempts: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            attempt = payload.get("send_attempt")
            if (
                isinstance(attempt, dict)
                and attempt.get("owner_binding_id") == binding_id
            ):
                attempts.append(dict(attempt))
        return attempts

    @staticmethod
    def _release_page_claim(
        state: dict[str, Any], binding: Mapping[str, Any]
    ) -> None:
        key = ConversationWriterLease._page_key(
            str(binding["provider_id"]),
            str(binding["physical_browser_id"]),
            str(binding["page_target_identity"]),
        )
        if state["page_claims"].get(key) == binding.get("binding_id"):
            state["page_claims"].pop(key, None)

    @staticmethod
    def _release_binding_leases(
        state: dict[str, Any], binding: Mapping[str, Any]
    ) -> None:
        binding_id = binding.get("binding_id")
        for key in {
            binding.get("writer_identity_key"),
            binding.get("provisional_writer_identity_key"),
        }:
            if not key:
                continue
            lease = state["leases"].get(key)
            if (
                isinstance(lease, dict)
                and lease.get("owner_binding_id") == binding_id
            ):
                state["leases"].pop(key, None)

    def close_binding(
        self, binding_id: str, *, abandon: bool = False
    ) -> dict[str, Any]:
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            unresolved = [
                attempt
                for attempt in self._attempts_for_binding(connection, binding_id)
                if attempt.get("state") not in _TERMINAL_ATTEMPT_STATES
            ]
            key = str(binding["writer_identity_key"])
            if abandon and unresolved:
                state["quarantine"][key] = {
                    "permanent": True,
                    "winner_binding_id": None,
                    "loser_binding_ids": [binding_id],
                    "reason": "unresolved_abandon",
                    "updated_at": _utc_now(),
                }
            self._release_binding_leases(state, binding)
            self._release_page_claim(state, binding)
            binding["binding_status"] = "sealed"
            binding["reconciliation_only"] = bool(unresolved)
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return self._binding_projection(binding)

    def validate_binding(
        self, binding_id: str, observed: Mapping[str, Any]
    ) -> dict[str, Any]:
        mismatch = False
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            for field in (
                "endpoint_id",
                "endpoint_cdp_url",
                "physical_browser_id",
                "page_target_identity",
                "temporary_chat",
                "temporary_document_id",
            ):
                if observed.get(field) != binding.get(field):
                    mismatch = True
                    break
            if not mismatch and binding.get("canonical_conversation_id"):
                try:
                    observed_id = canonical_conversation(
                        str(observed.get("conversation_url") or "")
                    )
                except ConversationLeaseError:
                    mismatch = True
                else:
                    mismatch = (
                        observed_id != binding.get("canonical_conversation_id")
                    )
            if mismatch:
                binding["binding_status"] = "blocked"
                binding["reconciliation_only"] = bool(
                    self._attempts_for_binding(connection, binding_id)
                )
                binding["updated_at"] = _utc_now()
                self._store_state(connection, state, version)
                connection.commit()
            else:
                connection.commit()
                return self._binding_projection(binding)
        raise ConversationLeaseError(
            "CONVERSATION_IDENTITY_MISMATCH",
            "binding evidence changed before browser send",
        )

    def rekey(self, binding_id: str, conversation_url: str) -> dict[str, Any]:
        current = self.get_binding(binding_id)
        if current.get("temporary_chat") is True:
            raise ConversationLeaseError(
                "CONVERSATION_IDENTITY_MISMATCH",
                "Temporary Chat uses a document-scoped writer identity and must not be canonicalized",
            )
        canonical_id = canonical_conversation(conversation_url)
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            target = self._canonical_key(str(binding["provider_id"]), canonical_id)
            current_key = str(binding["writer_identity_key"])
            if current_key == target:
                connection.commit()
                return self._binding_projection(binding)
            existing_quarantine = state["quarantine"].get(target)
            if isinstance(existing_quarantine, dict):
                connection.commit()
                raise self._quarantine_error(existing_quarantine)
            target_lease = state["leases"].get(target)
            if (
                isinstance(target_lease, dict)
                and target_lease.get("owner_binding_id") != binding_id
            ):
                winner = str(target_lease["owner_binding_id"])
                state["quarantine"][target] = {
                    "permanent": False,
                    "winner_binding_id": winner,
                    "loser_binding_ids": [binding_id],
                    "reason": "canonical_rekey_collision",
                    "updated_at": _utc_now(),
                }
                binding["canonical_conversation_id"] = canonical_id
                binding["conversation_url"] = conversation_url
                binding["pending_writer_identity_key"] = target
                binding["binding_status"] = "blocked"
                binding["reconciliation_only"] = True
                binding["updated_at"] = _utc_now()
                self._store_state(connection, state, version)
                connection.commit()
                raise ConversationLeaseError(
                    "WRITER_LEASE_QUARANTINED",
                    "canonical re-key collided with an active writer",
                    retryable=True,
                )

            old_lease = state["leases"].get(current_key)
            epoch = int(state["lease_epochs"].get(target) or 0) + 1
            state["lease_epochs"][target] = epoch
            if (
                isinstance(old_lease, dict)
                and old_lease.get("owner_binding_id") == binding_id
            ):
                state["leases"][target] = {
                    "writer_identity_key": target,
                    "owner_binding_id": binding_id,
                    "lease_epoch": epoch,
                    "state": "active",
                    "acquired_at": _utc_now(),
                }
            binding["canonical_conversation_id"] = canonical_id
            binding["conversation_url"] = conversation_url
            binding["provisional_writer_identity_key"] = current_key
            binding["writer_identity_key"] = target
            binding["lease_epoch"] = epoch
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return self._binding_projection(binding)

    def _resolve_quarantine_after_attempt(
        self, state: dict[str, Any], binding_id: str
    ) -> None:
        binding = state["bindings"].get(binding_id)
        if not isinstance(binding, dict):
            return
        pending_key = binding.get("pending_writer_identity_key")
        if not pending_key:
            return
        quarantine = state["quarantine"].get(pending_key)
        if not isinstance(quarantine, dict) or quarantine.get("permanent"):
            return
        losers = [
            value
            for value in quarantine.get("loser_binding_ids") or []
            if value != binding_id
        ]
        self._release_binding_leases(state, binding)
        self._release_page_claim(state, binding)
        binding["binding_status"] = "sealed"
        binding["updated_at"] = _utc_now()
        if losers:
            quarantine["loser_binding_ids"] = losers
            quarantine["updated_at"] = _utc_now()
            return
        winner_id = quarantine.get("winner_binding_id")
        state["quarantine"].pop(pending_key, None)
        winner = state["bindings"].get(winner_id)
        if isinstance(winner, dict):
            winner["baseline_required"] = True
            winner["updated_at"] = _utc_now()
