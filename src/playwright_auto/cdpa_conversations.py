from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from .cdpa_runtime_db import IdempotencyConflict, RuntimeDB
from .chatgpt_graph import rendered_user_text_matches


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
    if len(pieces) == 4 and pieces[0] == "g" and pieces[1].startswith("g-p-"):
        pieces = pieces[2:]
    if len(pieces) != 2 or pieces[0] != "c" or not pieces[1]:
        raise ConversationLeaseError(
            "CONVERSATION_IDENTITY_MISMATCH",
            "canonical conversation id is unavailable",
        )
    conversation_id = pieces[1]
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


def validate_stopped_writer_proof(
    *,
    command: Mapping[str, Any],
    task: Mapping[str, Any],
    task_version: int,
    immutable_task: bool,
    runtime_state: Mapping[str, Any],
    attempts: list[Mapping[str, Any]],
    request_records: Mapping[str, Mapping[str, Any]],
    observations: list[Mapping[str, Any]],
    now_epoch: float,
) -> dict[str, Any]:
    """Validate evidence for releasing one stopped controller writer; never mutate.

    Inputs must come from the command/task/runtime owners and a read-only collector
    attached only to the command's pre-existing diagnostic target. Two stable
    snapshots prove agreement of the observed transcript, not full history loading.
    The collector must separately establish the current history tail; unknown is
    deliberately insufficient. This proof says nothing about assistant work DONE.
    A future seal owner must revalidate these inputs under its transaction/locks.
    """
    import math

    from .durable import normalize_prompt
    from .chatgpt_history_tail import validate_native_history_tail

    def require(condition: bool, detail: str) -> None:
        if not condition:
            raise ConversationLeaseError("STOPPED_WRITER_PROOF_REQUIRED", detail)

    def sha(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    payload = command.get("payload")
    require(isinstance(payload, Mapping), "maintenance command payload is missing")
    require(
        all(isinstance(runtime_state.get(key), Mapping)
            for key in ("bindings", "leases", "page_claims", "quarantine")),
        "runtime writer authority containers are unknown",
    )
    require(
        command.get("kind") == "reconcile_stopped_writer"
        and command.get("status") == "running"
        and text(command.get("command_id"))
        and text(command.get("idempotency_key"))
        and type(task_version) is int
        and task_version > 0
        and type(command.get("expected_task_version")) is int
        and command["expected_task_version"] == task_version,
        "command authority or original expected version does not match",
    )
    task_id = task.get("task_id")
    require(
        type(immutable_task) is bool
        and text(task_id)
        and command.get("task_id") == task_id
        and task.get("status") == "STOPPED"
        and task.get("active_hop_id") is None
        and task.get("conversation_mode") == "persistent",
        "only a stopped persistent task without an active hop is eligible",
    )
    cleanup = task.get("cleanup") or {}
    controls = [
        control for control in task.get("controls", [])
        if control.get("control_id") == payload.get("stop_control_id")
    ]
    require(
        len(controls) == 1
        and cleanup.get("state") == "CLEARING"
        and cleanup.get("phase") == "writer_stop_pending"
        and cleanup.get("stop_control_id") == payload.get("stop_control_id"),
        "the exact historical Stop hold is required",
    )
    stop = controls[0]
    require(
        stop.get("action") == "stop"
        and stop.get("status") == "applied"
        and stop.get("command_state") == "APPLIED"
        and stop.get("confirmed") is True
        and stop.get("origin") == "operator"
        and text(stop.get("external_command_id"))
        and (stop.get("command") or {}).get("task_id") == task_id
        and (stop.get("result") or {}).get("writer_cleanup_pending") is True,
        "the original confirmed operator Stop is not applied",
    )
    binding_id = payload.get("binding_id")
    binding = (runtime_state.get("bindings") or {}).get(binding_id) or {}
    role_key = binding.get("role_key")
    role = (task.get("roles") or {}).get(role_key) or {}
    require(
        text(binding_id)
        and binding.get("binding_id") == binding_id
        and binding.get("task_id") == task_id
        and binding.get("binding_status") in {"active", "blocked", "idle"}
        and binding.get("provider_id") == "chatgpt_web"
        and binding.get("temporary_chat") is False
        and not binding.get("pending_identity")
        and role.get("conversation_binding_id") == binding_id
        and role.get("page_id") == binding.get("page_target_identity")
        and text(role.get("physical_role"))
        and text(task.get("team"))
        and stop.get("role") in {None, role_key},
        "persistent task-bound writer ownership does not match",
    )
    canonical = binding.get("canonical_conversation_id")
    require(
        text(canonical)
        and binding.get("writer_identity_key") == ConversationWriterLease._canonical_key("chatgpt_web", canonical)
        and type(binding.get("lease_epoch")) is int and binding["lease_epoch"] > 0,
        "saved canonical writer identity or lease epoch is missing",
    )
    for url in (binding.get("conversation_url"), role.get("page_url")):
        require(canonical_conversation(url) == canonical, "saved canonical conversation changed")
    for field in ("endpoint_id", "endpoint_cdp_url"):
        require(text(payload.get(field)) and payload[field] == binding.get(field), "endpoint/profile changed")
    require(
        role.get("browser_endpoint_id") == binding.get("endpoint_id")
        and text(payload.get("physical_browser_id"))
        and text(payload.get("target_id")),
        "the exact existing diagnostic target must be pinned",
    )
    leases = runtime_state.get("leases") or {}
    for key in (binding.get("writer_identity_key"), binding.get("provisional_writer_identity_key")):
        if key:
            require(key not in (runtime_state.get("quarantine") or {}), "writer identity is quarantined")
            require(
                key not in leases or (
                    isinstance(leases[key], Mapping)
                    and leases[key].get("owner_binding_id") == binding_id
                ),
                "writer lease has another or unknown owner",
            )
    require(
        bool(attempts)
        and all(attempt.get("owner_binding_id") == binding_id and attempt.get("state") in _TERMINAL_ATTEMPT_STATES for attempt in attempts),
        "all SendAttempts must belong to this binding and be terminal",
    )
    role_hops = [hop for hop in task.get("hops", []) if hop.get("target_role") == role_key]
    require(bool(role_hops) and all(type(hop.get("hop_id")) is int for hop in role_hops), "role execution history is unknown")
    latest = max(role_hops, key=lambda hop: hop["hop_id"])
    require(
        latest.get("physical_role") == role.get("physical_role")
        and latest.get("conversation_binding_id") == binding_id
        and latest.get("conversation_generation") == role.get("conversation_generation")
        and canonical_conversation(latest.get("conversation_url")) == canonical,
        "latest role execution belongs to another conversation/generation",
    )
    accepted: dict[str, str] = {}
    accounted_attempts: dict[str, str] = {}
    latest_record = None
    for request_id, record in request_records.items():
        if record.get("role") != role["physical_role"]:
            continue
        require(record.get("status") in {"sent", "completed"}, "role has unresolved ledger evidence")
        receipt = record.get("receipt") or {}
        saved_binding = {"page_id": role["page_id"], "role": role["physical_role"]}
        if record.get("binding") != saved_binding:
            continue
        matching = [hop for hop in role_hops if hop.get("request_id") == request_id]
        require(
            len(matching) == 1
            and record.get("request_id") == request_id
            and (record.get("source_context") or {}).get("task_id") == task_id
            and (record.get("source_context") or {}).get("team") == task["team"]
            and (record.get("source_context") or {}).get("hop_id") == matching[0]["hop_id"]
            and matching[0].get("receipt") == receipt
            and receipt.get("binding") == saved_binding,
            "accepted ledger/hop provenance differs",
        )
        user_id, prompt = receipt.get("user_message_id"), record.get("rendered_prompt")
        require(
            text(user_id) and not user_id.startswith("DOM:")
            and text(prompt) and receipt.get("prompt") == prompt
            and receipt.get("prompt_sha256") == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            and receipt.get("conversation_id") in {None, canonical}
            and receipt.get("session_id_before") in {None, canonical}
            and user_id not in accepted,
            "accepted user ID/text is absent, ambiguous, or belongs to another conversation",
        )
        saved_hop = matching[0]
        raw_prompt = saved_hop.get("prompt")
        require(
            text(raw_prompt)
            and saved_hop.get("prompt_sha256") == hashlib.sha256(raw_prompt.encode("utf-8")).hexdigest()
            and normalize_prompt(raw_prompt) == record.get("prompt") == prompt
            and saved_hop.get("rendered_prompt_sha256") == receipt["prompt_sha256"],
            "raw hop and normalized accepted receipt do not share durable provenance",
        )
        send_attempt_id = saved_hop.get("send_attempt_id")
        require(
            text(send_attempt_id) and send_attempt_id not in accounted_attempts,
            "accepted ledger request has no unique SendAttempt identity",
        )
        accounted_attempts[send_attempt_id] = saved_hop["prompt_sha256"]
        accepted[user_id] = prompt
        if request_id == latest.get("request_id"):
            latest_record = record
    require(latest_record is not None, "latest role request has no matching accepted ledger record")
    require(
        all(text(attempt.get("attempt_id")) for attempt in attempts)
        and len({attempt["attempt_id"] for attempt in attempts}) == len(attempts)
        and {
            attempt["attempt_id"]: attempt.get("text_hash") for attempt in attempts
            if attempt["state"] in {"accepted", "reconciled_accepted"}
        } == accounted_attempts,
        "accepted SendAttempts and saved ledger/hop receipts are not fully reconciled",
    )
    receipt = latest_record["receipt"]
    latest_attempts = [attempt for attempt in attempts if attempt.get("attempt_id") == latest.get("send_attempt_id")]
    require(
        len(latest_attempts) == 1
        and latest_attempts[0].get("state") in {"accepted", "reconciled_accepted"}
        and latest_attempts[0].get("text_hash") == latest["prompt_sha256"]
        and latest_attempts[0].get("lease_epoch") == binding["lease_epoch"],
        "latest accepted SendAttempt does not match its receipt",
    )
    require(len(observations) == 2, "two distinct current observations are required")
    times = [observation.get("observed_at") for observation in observations]
    require(
        all(type(value) in {int, float} and math.isfinite(value) and value > 0 for value in times)
        and times[0] < times[1] <= now_epoch
        and 0 <= now_epoch - times[0] <= 5,
        "observation timing is missing, stale, or out of order",
    )
    signatures = []
    for observation in observations:
        require(
            all(observation.get(field) == payload[field] for field in ("endpoint_id", "endpoint_cdp_url", "physical_browser_id", "target_id")),
            "diagnostic target or physical browser changed",
        )
        require(
            observation.get("document_ready_state") == "complete"
            and observation.get("storage_readable") is True
            and observation.get("provider_challenge") is False,
            "readiness or current history tail is unknown",
        )
        snapshot = observation.get("snapshot") or {}
        tail = validate_native_history_tail(
            observation.get("history_tail"), snapshot,
            latest_accepted_user_id=receipt["user_message_id"],
        )
        require(tail.get("verified") is True, "current history tail is unknown or does not match its native snapshot")
        require(
            canonical_conversation(snapshot.get("url")) == canonical
            and snapshot.get("session_id") == canonical,
            "diagnostic canonical conversation does not match",
        )
        owner = tuple(snapshot.get(key) for key in ("page_id", "page_task_id", "page_team", "page_role"))
        require(
            all(value in {None, ""} for value in owner)
            or owner == (role["page_id"], task_id, task["team"], role["physical_role"]),
            "diagnostic target is bound to another or incomplete owner",
        )
        if snapshot.get("page_id"):
            page_key = ConversationWriterLease._page_key("chatgpt_web", payload["physical_browser_id"], snapshot["page_id"])
            require((runtime_state.get("page_claims") or {}).get(page_key) in {None, binding_id}, "diagnostic page is claimed by another binding")
        require(
            snapshot.get("state") == "waiting_prompt"
            and snapshot.get("requires_login") is False
            and snapshot.get("composer_present") is True
            and snapshot.get("composer_editable") is True
            and snapshot.get("composer_ambiguous") is False
            and isinstance(snapshot.get("composer_text"), str)
            and not snapshot["composer_text"].strip()
            and all(snapshot.get(key) in ([], ()) for key in ("attachment_markers", "blocking_dialogs", "error_texts", "choice_prompt_labels"))
            and snapshot.get("retry_visible") is False
            and snapshot.get("mcp_provider_risk") is False
            and snapshot.get("connection_interrupted_visible") is False
            and snapshot.get("mcp_permission_allow_count") == 0
            and snapshot.get("mcp_permission_node_count") == 0,
            "diagnostic page is not positively ready and clean",
        )
        require(
            snapshot.get("stop_visible") is False
            and "response_activity_turn_id" in snapshot
            and snapshot["response_activity_turn_id"] in {None, ""}
            and snapshot.get("response_projection_active") is False,
            "positive idle evidence is missing",
        )
        messages = snapshot.get("messages")
        require(isinstance(messages, (list, tuple)), "observed transcript is unknown")
        users = [message for message in messages if message.get("role") == "user"]
        ids = [message.get("message_id") for message in users]
        require(
            bool(users)
            and len(set(ids)) == len(ids)
            and ids[-1] == receipt["user_message_id"]
            and all(user_id in accepted for user_id in ids)
            and all(rendered_user_text_matches(message.get("text"), accepted[message["message_id"]]) for message in users),
            "observed users do not exactly match the accepted ledger through its latest user",
        )
        signatures.append(sha(messages))
    require(signatures[0] == signatures[1], "observed transcript is still changing")
    proof = {
        "kind": "stopped_controller_writer_release",
        "immutable_history": immutable_task,
        "command_id": command["command_id"],
        "binding_id": binding_id,
        "task_id": task_id,
        "role_key": role_key,
        "stop_control_id": stop["control_id"],
        "target_id": payload["target_id"],
        "physical_browser_id": payload["physical_browser_id"],
        "canonical_conversation_id": canonical,
        "latest_request_id": latest["request_id"],
        "latest_user_message_id": receipt["user_message_id"],
        "observed_at": times[1],
        "command_sha256": sha({key: command.get(key) for key in ("command_id", "kind", "task_id", "idempotency_key", "expected_task_version", "payload")}),
        "task_sha256": sha(task),
        "binding_sha256": sha(binding),
        "attempts_sha256": sha(sorted(attempts, key=lambda item: str(item.get("attempt_id")))),
        "ledger_sha256": sha(request_records),
        "observations_sha256": sha(observations),
    }
    return {**proof, "evidence_sha256": sha(proof)}


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
            if binding.get("binding_status") == "sealed":
                connection.commit()
                return self._binding_projection(binding)
            self._release_binding_leases(state, binding)
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

    @staticmethod
    def _load_stopped_writer_state(connection: Any) -> tuple[dict[str, Any], int]:
        """Read maintenance authority without normalizing unknown containers."""
        row = connection.execute(
            "SELECT version, payload_json FROM runtime_snapshot WHERE name=?",
            (_STATE_NAME,),
        ).fetchone()
        try:
            state = json.loads(row["payload_json"]) if row is not None else None
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConversationLeaseError(
                "STOPPED_WRITER_PROOF_REQUIRED", "stored writer authority is malformed",
            ) from exc
        if (
            not isinstance(state, dict)
            or any(not isinstance(state.get(key), dict)
                   for key in ("bindings", "leases", "page_claims", "lease_epochs", "quarantine"))
            or any(not isinstance(binding, dict) or binding.get("binding_id") != key
                   for key, binding in state["bindings"].items())
            or any(not isinstance(lease, dict)
                   or not isinstance(lease.get("owner_binding_id"), str)
                   or not lease["owner_binding_id"].strip()
                   for lease in state["leases"].values())
            or any(not isinstance(owner, str) or not owner.strip()
                   for owner in state["page_claims"].values())
            or any(not isinstance(record, dict) for record in state["quarantine"].values())
        ):
            raise ConversationLeaseError(
                "STOPPED_WRITER_PROOF_REQUIRED", "stored writer authority is incomplete or has unknown owners",
            )
        return state, int(row["version"])

    def _stopped_writer_attempts(
        self, connection: Any, state: Mapping[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        """Keep command-row provenance with each raw SendAttempt during maintenance."""
        result: dict[str, list[dict[str, Any]]] = {
            binding_id: [] for binding_id in state["bindings"]
        }
        rows = connection.execute(
            "SELECT * FROM command_queue WHERE kind='browser_send'"
        ).fetchall()
        for row in rows:
            try:
                command = self.db._command_row(row)
                payload = command.get("payload")
                attempt = payload.get("send_attempt") if isinstance(payload, Mapping) else None
                owner_id = attempt.get("owner_binding_id") if isinstance(attempt, dict) else None
                binding = state["bindings"].get(owner_id) if isinstance(owner_id, str) else None
                key = command.get("idempotency_key")
                terminal = isinstance(attempt, dict) and attempt.get("state") in _TERMINAL_ATTEMPT_STATES
                valid = (
                    isinstance(attempt, dict) and isinstance(binding, Mapping)
                    and command.get("command_id") == attempt.get("attempt_id")
                    and isinstance(key, str) and bool(key.strip())
                    and attempt.get("attempt_id") == _digest("send", key)
                    and command.get("task_id") == binding.get("task_id")
                    and command.get("expected_task_version") is None
                    and attempt.get("state") in _ATTEMPT_TRANSITIONS
                    and command.get("status") == ("applied" if terminal else "recovery_required")
                    and bool(command.get("finished_at")) is terminal
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ConversationLeaseError(
                    "STOPPED_WRITER_PROOF_REQUIRED", "SendAttempt command provenance is malformed",
                ) from exc
            if not valid:
                raise ConversationLeaseError(
                    "STOPPED_WRITER_PROOF_REQUIRED", "SendAttempt command identity, task owner, or terminal status does not match",
                )
            result[str(owner_id)].append(dict(attempt))
        return result

    @staticmethod
    def _stopped_writer_digest(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _stopped_writer_command(self, connection: Any, command_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM command_queue WHERE command_id=?", (command_id,)
        ).fetchone()
        if row is None:
            raise ConversationLeaseError("STOPPED_WRITER_PROOF_REQUIRED", "maintenance command is missing")
        command = self.db._command_row(row)
        if command["kind"] != "reconcile_stopped_writer" or not command.get("task_id"):
            raise ConversationLeaseError("STOPPED_WRITER_PROOF_REQUIRED", "command is not task-bound writer maintenance")
        return command

    def _stopped_writer_replay_result(
        self, state: Mapping[str, Any], binding: Mapping[str, Any],
        command: Mapping[str, Any], attempts: list[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        if binding.get("binding_status") != "sealed":
            return None
        proof = binding.get("stopped_writer_release")
        payload = command["payload"]
        digest = self._stopped_writer_digest
        expected_command = digest({key: command.get(key) for key in (
            "command_id", "kind", "task_id", "idempotency_key", "expected_task_version", "payload"
        )})
        keys = {binding.get("writer_identity_key"), binding.get("provisional_writer_identity_key")} - {None, ""}
        if (
            not isinstance(proof, Mapping)
            or proof.get("kind") != "stopped_controller_writer_release"
            or proof.get("command_id") != command.get("command_id")
            or proof.get("command_sha256") != expected_command
            or proof.get("evidence_sha256") != digest({key: value for key, value in proof.items() if key != "evidence_sha256"})
            or proof.get("binding_id") != binding.get("binding_id")
            or proof.get("binding_id") != payload.get("binding_id")
            or proof.get("task_id") != binding.get("task_id")
            or proof.get("task_id") != command.get("task_id")
            or proof.get("role_key") != binding.get("role_key")
            or proof.get("canonical_conversation_id") != binding.get("canonical_conversation_id")
            or proof.get("physical_browser_id") != payload.get("physical_browser_id")
            or proof.get("target_id") != payload.get("target_id")
            or proof.get("stop_control_id") != payload.get("stop_control_id")
            or payload.get("endpoint_id") != binding.get("endpoint_id")
            or payload.get("endpoint_cdp_url") != binding.get("endpoint_cdp_url")
            or proof.get("attempts_sha256") != digest(sorted(attempts, key=lambda item: str(item.get("attempt_id"))))
            or any(attempt.get("state") not in _TERMINAL_ATTEMPT_STATES for attempt in attempts)
            or any(lease.get("owner_binding_id") == binding["binding_id"] for lease in state["leases"].values())
            or binding["binding_id"] in state["page_claims"].values()
            or keys.intersection(state["quarantine"])
        ):
            raise ConversationLeaseError(
                "STOPPED_WRITER_PROOF_REQUIRED",
                "sealed binding does not carry this exact command's unchanged release proof",
            )
        return {
            "controller_writer_released": True, "task_id": binding["task_id"],
            "binding_id": binding["binding_id"], "proof": dict(proof), "reconciled": True,
        }

    def replay_stopped_writer_release(self, command_id: str) -> dict[str, Any] | None:
        """Recognize only this command's committed seal; never refresh browser proof."""
        with self.db.connection() as connection:
            connection.execute("BEGIN")
            command = self._stopped_writer_command(connection, command_id)
            state, _ = self._load_stopped_writer_state(connection)
            binding = self._binding_or_error(state, str(command["payload"].get("binding_id") or ""))
            result = self._stopped_writer_replay_result(
                state, binding, command, self._stopped_writer_attempts(connection, state)[binding["binding_id"]]
            )
            connection.commit()
            return result

    def seal_stopped_writer(
        self, command_id: str, *, task: Mapping[str, Any], immutable_task: bool,
        request_records: Mapping[str, Mapping[str, Any]], observations: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Seal one stopped task writer from locked task/ledger and fresh target evidence.

        The caller holds the existing task/manifest/ledger read locks and endpoint
        admission. This owner re-reads command/version/binding/attempts together.
        It does not complete the command or alter any task/accepted request.
        """
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            command = self._stopped_writer_command(connection, command_id)
            state, version = self._load_stopped_writer_state(connection)
            binding_id = str(command["payload"].get("binding_id") or "")
            binding = self._binding_or_error(state, binding_id)
            attempts = self._stopped_writer_attempts(connection, state)[binding_id]
            replay = self._stopped_writer_replay_result(state, binding, command, attempts)
            if replay is not None:
                connection.commit()
                return replay
            if command.get("status") != "running" or binding.get("stopped_writer_release") is not None:
                raise ConversationLeaseError("STOPPED_WRITER_PROOF_REQUIRED", "maintenance is not the current running command")
            proof = validate_stopped_writer_proof(
                command=command, task=task,
                task_version=self.db.get_task_version(str(command["task_id"]), connection=connection),
                immutable_task=immutable_task, runtime_state=state, attempts=attempts,
                request_records=request_records, observations=observations,
                now_epoch=datetime.now(timezone.utc).timestamp(),
            )
            allowed_leases = {binding.get("writer_identity_key"), binding.get("provisional_writer_identity_key")} - {None, ""}
            page_key = self._page_key(
                binding["provider_id"], binding["physical_browser_id"], binding["page_target_identity"]
            )
            if (
                any(key not in allowed_leases and lease.get("owner_binding_id") == binding_id for key, lease in state["leases"].items())
                or any(key != page_key and owner == binding_id for key, owner in state["page_claims"].items())
                or state["page_claims"].get(page_key) not in {None, binding_id}
            ):
                raise ConversationLeaseError("STOPPED_WRITER_PROOF_REQUIRED", "binding has unexplained page or writer claims")
            self._release_binding_leases(state, binding)
            self._release_page_claim(state, binding)
            binding["binding_status"] = "sealed"
            binding["stopped_writer_release"] = proof
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return {
                "controller_writer_released": True, "task_id": binding["task_id"],
                "binding_id": binding_id, "proof": proof,
            }

    def stopped_writer_maintenance_evidence(self) -> dict[str, Any]:
        """Return one coherent, read-only owner snapshot for maintenance projection."""
        with self.db.connection() as connection:
            connection.execute("BEGIN")
            state, _ = self._load_stopped_writer_state(connection)
            commands: dict[str, Any] = {}
            attempts = self._stopped_writer_attempts(connection, state)
            rows = connection.execute(
                "SELECT * FROM command_queue WHERE kind='reconcile_stopped_writer'"
            ).fetchall()
            for row in rows:
                command = self.db._command_row(row)
                commands[command["command_id"]] = command
            result = {
                "bindings": [dict(binding) for binding in state["bindings"].values()],
                "commands_by_id": commands, "attempts_by_binding": {
                    binding_id: attempts.get(binding_id, []) for binding_id in state["bindings"]
                },
                "leases": state["leases"], "page_claims": state["page_claims"],
                "quarantine": state["quarantine"],
            }
            connection.commit()
            return result

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
            if unresolved:
                connection.rollback()
                raise ConversationLeaseError(
                    "WRITER_LEASE_BUSY",
                    "writer has unresolved send evidence",
                    retryable=True,
                )
            if binding.get("binding_status") == "sealed":
                connection.commit()
                return self._binding_projection(binding)
            self._release_binding_leases(state, binding)
            self._release_page_claim(state, binding)
            binding["binding_status"] = "sealed"
            binding["updated_at"] = _utc_now()
            self._store_state(connection, state, version)
            connection.commit()
            return self._binding_projection(binding)

    @staticmethod
    def _binding_matches(binding: Mapping[str, Any], observed: Mapping[str, Any]) -> bool:
        fields = (
            "endpoint_id", "endpoint_cdp_url", "physical_browser_id",
            "page_target_identity", "temporary_chat", "temporary_document_id",
        )
        if any(observed.get(field) != binding.get(field) for field in fields):
            return False
        expected = binding.get("canonical_conversation_id")
        if not expected:
            return True
        try:
            return canonical_conversation(str(observed.get("conversation_url") or "")) == expected
        except ValueError:
            return False

    def validate_binding(
        self, binding_id: str, observed: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            if not self._binding_matches(binding, observed):
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

    def restore_persistent_writer(
        self, binding_id: str, observed: Mapping[str, Any], *, saved_url: str,
    ) -> str:
        """Reconcile the saved owner before admitting another Send."""
        conversation = canonical_conversation(saved_url)
        with self.db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state, version = self._load_state(connection)
            binding = self._binding_or_error(state, binding_id)
            physical_id = str(observed.get("physical_browser_id") or "")
            expected = {
                **binding, "physical_browser_id": physical_id, "canonical_conversation_id": conversation,
            }
            if (
                not physical_id or binding.get("temporary_chat")
                or binding.get("canonical_conversation_id") not in {None, conversation}
                or not self._binding_matches(expected, observed)
            ):
                raise ConversationLeaseError(
                    "CONVERSATION_IDENTITY_MISMATCH", "The saved writer does not match the exact live role",
                )
            provider = str(binding["provider_id"])
            page_id = str(binding["page_target_identity"])
            writer_key = self._canonical_key(provider, conversation)
            page_key = self._page_key(provider, physical_id, page_id)
            same_binding = self._binding_matches(binding, observed) and bool(binding["canonical_conversation_id"])
            restored_id = binding_id if same_binding else _digest("reopened", binding_id, physical_id, conversation)
            if binding.get("binding_status") == "sealed":
                existing = state["bindings"].get(restored_id)
                if (
                    existing and existing.get("restored_from_binding_id") == binding_id
                    and existing.get("binding_status") == "idle"
                    and not existing.get("pending_writer_identity_key")
                    and writer_key not in state["quarantine"] and writer_key not in state["leases"]
                    and state["page_claims"].get(page_key) == restored_id
                    and self._binding_matches(existing, observed)
                ):
                    connection.commit()
                    return restored_id
            if (
                binding.get("binding_status") not in {"blocked", "idle"}
                or (not same_binding and restored_id in state["bindings"])
                or state["page_claims"].get(page_key) not in {None, binding_id}
                or binding.get("pending_writer_identity_key")
                or any(attempt.get("state") not in _TERMINAL_ATTEMPT_STATES
                       for attempt in self._attempts_for_binding(connection, binding_id))
            ):
                raise ConversationLeaseError("WRITER_LEASE_BUSY", "Writer evidence still needs reconciliation")
            for key in {str(binding["writer_identity_key"]), writer_key}:
                quarantine = state["quarantine"].get(key)
                if isinstance(quarantine, dict):
                    raise self._quarantine_error(quarantine)
                lease = state["leases"].get(key)
                if lease and (lease.get("owner_binding_id") != binding_id or lease.get("state") == "reserved_async_wait"):
                    raise ConversationLeaseError("WRITER_LEASE_BUSY", "The conversation has another writer lease")
            self._release_binding_leases(state, binding)
            now = _utc_now()
            if not same_binding:
                self._release_page_claim(state, binding)
                binding.update(binding_status="sealed", updated_at=now)
                expected.update(
                    binding_id=restored_id, restored_from_binding_id=binding_id,
                    page_binding_id=_digest("page", provider, physical_id, page_id),
                    conversation_url=observed["conversation_url"], writer_identity_key=writer_key,
                    provisional_writer_identity_key=None, lease_epoch=None, verified_new_chat=False,
                    created_at=now,
                )
                for field in ("dom_projection", "dom_projection_version", "dom_projection_updated_at"):
                    expected.pop(field, None)
                binding = expected
                state["bindings"][restored_id] = binding
            state["page_claims"][page_key] = restored_id
            binding.update(binding_status="idle", reconciliation_only=False, updated_at=now)
            self._store_state(connection, state, version)
            connection.commit()
            return restored_id

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
