from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .chatgpt import (
    ChatGPTPage,
    MessageSnapshot,
    attachment_names_match,
    capture_message_baseline,
    unique_new_user_message,
    visible_text_matches,
)
from .durable import (
    DurableRecoveryState,
    DurableRequestError,
    RequestLedger,
    RequestStatus,
    classify_recovery_state,
    receipt_from_record,
)
from .upload import (
    FileIdentity,
    FileSnapshot,
    UploadIdentityChangedError,
    UploadReceipt,
    collect_file_snapshots,
)
from .workflow import WorkflowBlock, WorkflowContext, resolve

PromptSource = str | Callable[
    [WorkflowContext[ChatGPTPage]], str | Awaitable[str]
]
PathsSource = Sequence[str] | Callable[
    [WorkflowContext[ChatGPTPage]], Sequence[str] | Awaitable[Sequence[str]]
]
ValueSource = Any | Callable[[WorkflowContext[ChatGPTPage]], Any | Awaitable[Any]]


async def _resolve_value(value: ValueSource, context: WorkflowContext[ChatGPTPage]) -> Any:
    raw = value(context) if callable(value) else value
    return await resolve(raw)


def _upload_anchor(record: Any) -> str:
    return record.marker if record.marker in record.rendered_prompt else record.rendered_prompt


def _validate_upload_receipt(
    record: Any,
    identities: tuple[FileIdentity, ...],
) -> UploadReceipt:
    if not record.upload_receipt:
        raise DurableRequestError("upload-ready durable request is missing its upload receipt")
    try:
        receipt = UploadReceipt.from_dict(record.upload_receipt)
    except Exception as exc:
        raise DurableRequestError("persisted upload receipt is invalid") from exc
    if (
        receipt.request_marker != _upload_anchor(record)
        or receipt.files != identities
        or receipt.attachment_count != len(identities)
        or not receipt.ownership_token
    ):
        raise DurableRequestError("persisted upload receipt does not match durable files")
    return receipt


class DurableSendBlock(WorkflowBlock[ChatGPTPage]):
    """Idempotent Send + optional upload + response wait backed by a file ledger.

    The block never re-clicks after the ledger enters SENDING unless transcript
    evidence proves the request marker was accepted. Ambiguous crash states fail
    closed and require explicit operator reconciliation.
    """

    retry_safe = False

    def __init__(
        self,
        prompt: PromptSource,
        *,
        ledger_path: str | Path = ".runtime/chatgpt-request-ledger.json",
        files: PathsSource = (),
        expected_file_identities: ValueSource = None,
        source_context: ValueSource = None,
        role_prompt_hash: ValueSource = "",
        request_id: ValueSource = None,
        render_request_marker: bool = True,
        wait_for_response: bool = True,
        wait_for_stop: bool = True,
        max_attempts: int = 2,
        recovery_reload: bool = True,
        response_timeout_ms: int | None = None,
        stable_ms: int = 1_000,
        poll_ms: int = 100,
        active_reload_after_ms: int | None = None,
        candidate_validator: Callable[[MessageSnapshot], None] | None = None,
        minimum_samples: int = 1,
        invalid_grace_ms: int | None = None,
        max_total_bytes: int = 20 * 1024 * 1024,
        record_key: str = "durable_request",
        receipt_key: str = "send_receipt",
        response_key: str = "response",
        recovery_key: str = "durable_recovery_state",
        block_id: str = "durable_send",
    ) -> None:
        super().__init__(block_id)
        self.prompt = prompt
        self.ledger_path = Path(ledger_path)
        self.files = files
        self.expected_file_identities = expected_file_identities
        self.source_context = source_context
        self.role_prompt_hash = role_prompt_hash
        self.request_id = request_id
        self.render_request_marker = bool(render_request_marker)
        self.wait_for_response_enabled = wait_for_response
        self.wait_for_stop = wait_for_stop
        self.max_attempts = max_attempts
        self.recovery_reload = recovery_reload
        self.response_timeout_ms = response_timeout_ms
        self.stable_ms = stable_ms
        self.poll_ms = poll_ms
        self.active_reload_after_ms = active_reload_after_ms
        self.candidate_validator = candidate_validator
        self.minimum_samples = minimum_samples
        self.invalid_grace_ms = invalid_grace_ms
        self.max_total_bytes = max_total_bytes
        self.record_key = record_key
        self.receipt_key = receipt_key
        self.response_key = response_key
        self.recovery_key = recovery_key

    async def _complete_from_receipt(
        self,
        context: WorkflowContext[ChatGPTPage],
        ledger: RequestLedger,
        record,
        receipt,
        *,
        cached: bool = False,
        expected_assistant_turn_id: str | None = None,
        expected_assistant_message_id: str | None = None,
    ) -> dict[str, Any]:
        context.variables[self.receipt_key] = receipt
        if not self.wait_for_response_enabled:
            context.variables[self.record_key] = record
            return {
                "cached": cached,
                "record": record.to_dict(),
                "receipt": receipt.to_dict(),
                "response": None,
            }
        try:
            response = await context.client.wait_for_response(
                receipt,
                timeout_ms=self.response_timeout_ms,
                stable_ms=self.stable_ms,
                poll_ms=self.poll_ms,
                active_reload_after_ms=self.active_reload_after_ms,
                candidate_validator=self.candidate_validator,
                minimum_samples=self.minimum_samples,
                invalid_grace_ms=self.invalid_grace_ms,
                expected_assistant_turn_id=expected_assistant_turn_id,
                expected_assistant_message_id=expected_assistant_message_id,
            )
        except Exception as exc:
            ledger.update(
                record.request_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        record = ledger.update(
            record.request_id,
            status=RequestStatus.COMPLETED,
            response=response.to_dict(),
            error=None,
        )
        context.variables[self.record_key] = record
        context.variables[self.response_key] = response
        return {
            "cached": cached,
            "record": record.to_dict(),
            "receipt": receipt.to_dict(),
            "response": response.to_dict(),
        }

    @staticmethod
    def _raise_ambiguous(record, recovery: DurableRecoveryState) -> None:
        raise DurableRequestError(
            f"durable request {record.request_id} is in ambiguous state "
            f"{recovery.value}; refusing to send again"
        )

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        if context.client.binding is None:
            raise DurableRequestError(
                "DurableSendBlock requires a bound ChatGPTPage; run SetRoleBlock first"
            )
        prompt = str(await _resolve_value(self.prompt, context)).strip()
        if not prompt:
            raise ValueError("durable prompt must not be empty")
        raw_paths = await _resolve_value(self.files, context)
        paths = [str(path) for path in raw_paths]
        source_context = await _resolve_value(self.source_context, context)
        role_prompt_hash = str(
            await _resolve_value(self.role_prompt_hash, context)
        )
        raw_request_id = await _resolve_value(self.request_id, context)
        request_id = str(raw_request_id).strip() if raw_request_id is not None else None
        ledger = RequestLedger(self.ledger_path)
        existing = (
            ledger.get(request_id)
            if request_id and ledger.path.exists()
            else None
        )
        crossed_send_boundary = existing is not None and existing.status in {
            RequestStatus.SENDING,
            RequestStatus.SENT,
            RequestStatus.COMPLETED,
        }
        if crossed_send_boundary:
            file_snapshots = ()
            identities = existing.files
        else:
            file_snapshots = (
                collect_file_snapshots(paths, max_total_bytes=self.max_total_bytes)
                if paths
                else ()
            )
            identities = tuple(snapshot.identity for snapshot in file_snapshots)
            raw_expected = await _resolve_value(self.expected_file_identities, context)
            if raw_expected is not None:
                expected = tuple(
                    item
                    if isinstance(item, FileIdentity)
                    else FileIdentity.from_dict(dict(item))
                    for item in raw_expected
                )
                if identities != expected:
                    raise UploadIdentityChangedError(
                        "upload source identity changed before durable ledger bind"
                    )
        record = ledger.begin(
            role=context.client.binding.role,
            prompt=prompt,
            source_context=source_context,
            role_prompt_hash=role_prompt_hash,
            files=identities,
            request_id=request_id or None,
            render_request_marker=self.render_request_marker,
        )
        with ledger.request_lock(record.request_id):
            current = ledger.get(record.request_id)
            if current is None:
                raise DurableRequestError(
                    f"durable request {record.request_id} disappeared from ledger"
                )
            return await self._run_record(
                context,
                ledger,
                current,
                paths,
                identities,
                file_snapshots,
            )

    async def _run_record(
        self,
        context: WorkflowContext[ChatGPTPage],
        ledger: RequestLedger,
        record,
        paths: list[str],
        identities: tuple[FileIdentity, ...],
        file_snapshots: tuple[FileSnapshot, ...],
    ) -> dict[str, Any]:
        context.variables[self.record_key] = record
        context.variables[f"{self.record_key}_marker"] = record.marker
        context.variables[f"{self.record_key}_prompt"] = record.rendered_prompt

        if record.status is RequestStatus.COMPLETED:
            if not record.response or not record.receipt:
                raise DurableRequestError(
                    "completed durable request is missing cached receipt/response"
                )
            from .chatgpt import SendReceipt

            receipt = SendReceipt.from_dict(record.receipt)
            response = MessageSnapshot.from_dict(record.response)
            if self.candidate_validator is not None:
                try:
                    self.candidate_validator(response)
                except Exception:
                    if not response.turn_id and not response.message_id:
                        raise DurableRequestError(
                            "invalid cached response has no assistant identity; refusing unbound reread"
                        )
                    return await self._complete_from_receipt(
                        context,
                        ledger,
                        record,
                        receipt,
                        cached=True,
                        expected_assistant_turn_id=response.turn_id,
                        expected_assistant_message_id=response.message_id,
                    )
            context.variables[self.receipt_key] = receipt
            context.variables[self.response_key] = response
            context.variables[self.record_key] = record
            return {
                "cached": True,
                "record": record.to_dict(),
                "receipt": receipt.to_dict(),
                "response": response.to_dict(),
            }
        if record.status is RequestStatus.FAILED_FINAL:
            raise DurableRequestError(
                f"durable request {record.request_id} is final-failed: {record.error}"
            )
        if record.status is RequestStatus.FAILED_RETRYABLE:
            raise DurableRequestError(
                f"durable request {record.request_id} requires explicit retry approval: "
                f"{record.error}"
            )

        snapshot = await context.client.assert_ownership()
        recovery = classify_recovery_state(record, snapshot)
        context.variables[self.recovery_key] = recovery

        if recovery is DurableRecoveryState.SENT_WAITING_RESPONSE:
            accepted_user = (
                unique_new_user_message(snapshot.messages, record.baseline)
                if record.baseline is not None
                else None
            )
            if record.status is RequestStatus.SENDING:
                if accepted_user is None:
                    self._raise_ambiguous(record, DurableRecoveryState.SENT_MARKER_MISSING)
                if snapshot.manual_input_pending:
                    self._raise_ambiguous(record, DurableRecoveryState.MANUAL_COMPOSER_DIRTY)
                if identities:
                    _validate_upload_receipt(record, identities)
            receipt = receipt_from_record(record, accepted_user=accepted_user)
            if record.status is RequestStatus.SENDING:
                record = ledger.update(
                    record.request_id,
                    status=RequestStatus.SENT,
                    accepted_at=record.accepted_at or time.time(),
                    receipt=receipt.to_dict(),
                    error=None,
                )
            elif record.status is RequestStatus.SENT:
                if record.receipt != receipt.to_dict():
                    record = ledger.update(
                        record.request_id,
                        receipt=receipt.to_dict(),
                        error=None,
                    )
            else:
                raise DurableRequestError(
                    f"transcript contains accepted request but ledger status is "
                    f"{record.status.value}; manual reconciliation required"
                )
            return await self._complete_from_receipt(
                context, ledger, record, receipt
            )

        if recovery in {
            DurableRecoveryState.SENT_MARKER_MISSING,
            DurableRecoveryState.COMPOSER_ATTACHMENTS_WITHOUT_MARKER,
            DurableRecoveryState.MANUAL_COMPOSER_DIRTY,
            DurableRecoveryState.COMPOSER_PROMPT_AND_ATTACHMENTS_PENDING,
        }:
            self._raise_ambiguous(record, recovery)
        if (
            record.status is RequestStatus.UPLOADING
            and recovery is not DurableRecoveryState.UPLOAD_READY_NOT_SENT
        ):
            self._raise_ambiguous(record, recovery)

        if recovery is DurableRecoveryState.RECOVERY_MARKER_NOT_FOUND:
            await context.client.set_text(record.rendered_prompt)
            if record.status is RequestStatus.NEW:
                record = ledger.update(
                    record.request_id,
                    status=RequestStatus.PROMPT_SET,
                    error=None,
                )
        elif recovery in {
            DurableRecoveryState.COMPOSER_PROMPT_ONLY_PENDING,
            DurableRecoveryState.COMPOSER_PROMPT_MISSING_ATTACHMENTS,
        }:
            if not visible_text_matches(snapshot.composer_text, record.rendered_prompt):
                raise DurableRequestError(
                    "durable marker exists but composer text is not the exact persisted prompt"
                )
            if record.status is RequestStatus.NEW:
                record = ledger.update(
                    record.request_id,
                    status=RequestStatus.PROMPT_SET,
                    error=None,
                )
        elif recovery is DurableRecoveryState.UPLOAD_READY_NOT_SENT:
            if not visible_text_matches(snapshot.composer_text, record.rendered_prompt):
                raise DurableRequestError(
                    "upload-ready composer does not match the exact persisted prompt"
                )
            if record.status is RequestStatus.UPLOADING:
                ownership_token = await context.client.current_attachment_ownership_token(
                    expected_files=identities
                )
                if not ownership_token:
                    raise DurableRequestError(
                        "recovered upload has no live attachment ownership token"
                    )
                recovered = UploadReceipt(
                    request_marker=_upload_anchor(record),
                    method="recovered",
                    files=identities,
                    attachment_count=len(identities),
                    ownership_token=ownership_token,
                )
                record = ledger.update(
                    record.request_id,
                    status=RequestStatus.UPLOAD_READY,
                    upload_receipt=recovered.to_dict(),
                    error=None,
                )
            elif record.status is RequestStatus.UPLOAD_READY:
                _validate_upload_receipt(record, identities)
            else:
                raise DurableRequestError(
                    f"attachment readiness is unowned in durable status {record.status.value}"
                )

        if record.status is RequestStatus.UPLOAD_READY and recovery is not DurableRecoveryState.UPLOAD_READY_NOT_SENT:
            self._raise_ambiguous(record, recovery)

        if identities and recovery is not DurableRecoveryState.UPLOAD_READY_NOT_SENT:
            if record.status is not RequestStatus.PROMPT_SET:
                raise DurableRequestError(
                    f"cannot start upload from durable status {record.status.value}"
                )
            record = ledger.update(
                record.request_id,
                status=RequestStatus.UPLOADING,
                error=None,
            )
            try:
                upload_receipt = await context.client.upload_files(
                    paths,
                    request_marker=_upload_anchor(record),
                    max_total_bytes=self.max_total_bytes,
                    expected_files=identities,
                    file_snapshots=file_snapshots,
                    exact_prompt=not self.render_request_marker,
                )
            except Exception as exc:
                ledger.update(
                    record.request_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            record = ledger.update(
                record.request_id,
                status=RequestStatus.UPLOAD_READY,
                upload_receipt=upload_receipt.to_dict(),
                error=None,
            )

        before_send = await context.client.assert_ownership()
        source_task_id: str | None = None
        source_team: str | None = None
        if isinstance(record.source_context, Mapping):
            raw_task_id = record.source_context.get("task_id")
            raw_team = record.source_context.get("team")
            if raw_task_id is not None or raw_team is not None:
                provenance_task_id = str(raw_task_id or "").strip() or None
                provenance_team = str(raw_team or "").strip() or None
                if provenance_task_id is None or provenance_team is None:
                    raise DurableRequestError(
                        "durable source context has incomplete task/team ownership"
                    )
                if context.client.binding.role != "MAINTAINERS":
                    source_task_id = provenance_task_id
                    source_team = provenance_team
                    if (
                        before_send.page_task_id != source_task_id
                        or before_send.page_team != source_team
                    ):
                        raise DurableRequestError(
                            "durable task/team ownership does not match before send"
                        )
        if not visible_text_matches(before_send.composer_text, record.rendered_prompt):
            raise DurableRequestError(
                "exact durable prompt ownership was lost before send"
            )
        expected_markers = tuple(item.name for item in identities)
        if not attachment_names_match(before_send.attachment_markers, expected_markers):
            raise DurableRequestError(
                "durable attachment identity changed before send"
            )
        validated_upload_receipt = (
            _validate_upload_receipt(record, identities) if identities else None
        )
        baseline = capture_message_baseline(before_send.messages)
        record = ledger.update(
            record.request_id,
            status=RequestStatus.SENDING,
            attempts=record.attempts + 1,
            binding=context.client.binding,
            baseline=baseline,
            session_id_before=before_send.session_id,
            error=None,
        )
        send_ownership: dict[str, str] = {}
        if source_task_id is not None and source_team is not None:
            send_ownership = {
                "expected_task_id": source_task_id,
                "expected_team": source_team,
            }
        if validated_upload_receipt is not None:
            send_ownership["expected_attachment_ownership_token"] = str(
                validated_upload_receipt.ownership_token
            )
        try:
            receipt = await context.client.send(
                record.rendered_prompt,
                wait_for_stop=self.wait_for_stop,
                max_attempts=self.max_attempts,
                recovery_reload=self.recovery_reload,
                expected_attachment_count=len(identities),
                expected_attachment_names=expected_markers,
                **send_ownership,
            )
        except Exception as exc:
            # Keep SENDING. A crash/error after a click is ambiguous, so the next
            # process must prove acceptance from the transcript before proceeding.
            ledger.update(
                record.request_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        if (
            not self.render_request_marker
            and not receipt.user_message_id
            and not receipt.user_turn_id
        ):
            error = (
                "markerless send reached transport progress without accepted "
                "user-message identity"
            )
            ledger.update(record.request_id, error=error)
            raise DurableRequestError(error)
        record = ledger.update(
            record.request_id,
            status=RequestStatus.SENT,
            accepted_at=record.accepted_at or time.time(),
            receipt=receipt.to_dict(),
            binding=receipt.binding,
            baseline=receipt.baseline,
            session_id_before=receipt.session_id_before,
            error=None,
        )
        context.variables[self.receipt_key] = receipt
        return await self._complete_from_receipt(
            context, ledger, record, receipt
        )
