from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from .chatgpt import (
    ChoicePromptBlockedError,
    ComposerConflictError,
    PageOwnershipError,
    SELECTORS,
    UnsafePageStateError,
)

if TYPE_CHECKING:
    from .chatgpt import ChatGPTPage, ChatGPTSnapshot


class UploadError(RuntimeError):
    pass


class UploadTransportError(UploadError):
    pass


class UploadReadinessError(UploadError):
    pass


@dataclass(frozen=True)
class FileIdentity:
    path: str
    name: str
    size: int
    sha256: str
    mime_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FileIdentity":
        return cls(
            path=str(value["path"]),
            name=str(value["name"]),
            size=int(value["size"]),
            sha256=str(value["sha256"]),
            mime_type=str(value.get("mime_type") or "application/octet-stream"),
        )


@dataclass(frozen=True)
class UploadReceipt:
    request_marker: str
    method: str
    files: tuple[FileIdentity, ...]
    attachment_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_marker": self.request_marker,
            "method": self.method,
            "files": [item.to_dict() for item in self.files],
            "attachment_count": self.attachment_count,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UploadReceipt":
        return cls(
            request_marker=str(value["request_marker"]),
            method=str(value["method"]),
            files=tuple(FileIdentity.from_dict(item) for item in value.get("files", [])),
            attachment_count=int(value["attachment_count"]),
        )


def collect_file_identities(
    paths: Sequence[str | Path], *, max_total_bytes: int = 20 * 1024 * 1024
) -> tuple[FileIdentity, ...]:
    if not paths:
        raise ValueError("at least one upload path is required")
    identities: list[FileIdentity] = []
    total = 0
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        total += size
        if total > max_total_bytes:
            raise ValueError(
                f"upload payload exceeds max_total_bytes={max_total_bytes}"
            )
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        identities.append(
            FileIdentity(
                path=str(path),
                name=path.name,
                size=size,
                sha256=digest.hexdigest(),
                mime_type=mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
            )
        )
    return tuple(identities)


async def dismiss_stale_upload_overlay(page: Any) -> tuple[str, ...]:
    return tuple(
        await page.evaluate(
            r"""() => {
              const visible = (element) => Boolean(
                element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
              );
              const staleMarkers = ['duplicate file', 'add anything', 'drop any file'];
              const safeClose = ['cancel', 'close', 'dismiss', 'ok', 'okay'];
              const dismissed = [];
              for (const dialog of document.querySelectorAll('[role="dialog"], [data-testid^="modal-"]')) {
                if (!visible(dialog)) continue;
                const dialogText = (dialog.innerText || dialog.textContent || '').toLowerCase();
                if (!staleMarkers.some((marker) => dialogText.includes(marker))) continue;
                const button = [...dialog.querySelectorAll('button,[role="button"]')].find((item) => {
                  if (!visible(item) || item.disabled || item.getAttribute('aria-disabled') === 'true') return false;
                  const label = [
                    item.innerText || item.textContent || '',
                    item.getAttribute('aria-label') || ''
                  ].join(' ').trim().toLowerCase();
                  return safeClose.some((marker) => label === marker || label.includes(marker));
                });
                if (button) {
                  dismissed.push((button.innerText || button.getAttribute('aria-label') || 'close').trim());
                  button.click();
                }
              }
              return dismissed;
            }"""
        )
    )


async def _upload_via_input(page: Any, paths: Sequence[str]) -> bool:
    inputs = page.locator('input[type="file"]')
    count = await inputs.count()
    if count < 1:
        await page.evaluate(
            r"""() => {
              const visible = (element) => Boolean(
                element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
              );
              const buttons = [...document.querySelectorAll('button,[role="button"]')];
              const target = buttons.find((button) => {
                const label = [
                  button.innerText || button.textContent || '',
                  button.getAttribute('aria-label') || '',
                  button.getAttribute('data-testid') || ''
                ].join(' ').toLowerCase();
                return visible(button) && !button.disabled &&
                  (label.includes('add files and more') || label.includes('composer-plus-btn'));
              });
              if (!target) return false;
              target.click();
              return true;
            }"""
        )
        try:
            await page.wait_for_selector('input[type="file"]', state="attached", timeout=1_500)
        except Exception:
            return False
        inputs = page.locator('input[type="file"]')
        count = await inputs.count()
    if count < 1:
        return False
    await inputs.nth(count - 1).set_input_files(list(paths))
    return True


async def _upload_via_drop(
    page: Any,
    files: Sequence[FileIdentity],
    *,
    max_total_bytes: int,
) -> bool:
    payload = []
    total = 0
    for identity in files:
        data = Path(identity.path).read_bytes()
        total += len(data)
        if total > max_total_bytes:
            raise ValueError(
                f"drop payload exceeds max_total_bytes={max_total_bytes}"
            )
        payload.append(
            {
                "name": identity.name,
                "type": identity.mime_type,
                "base64": base64.b64encode(data).decode("ascii"),
            }
        )
    return bool(
        await page.evaluate(
            r"""([selector, payload]) => {
              const composer = document.querySelector(selector);
              const target = composer?.closest('form') || composer;
              if (!target) return false;
              const transfer = new DataTransfer();
              for (const item of payload) {
                const binary = atob(item.base64);
                const bytes = new Uint8Array(binary.length);
                for (let index = 0; index < binary.length; index += 1) {
                  bytes[index] = binary.charCodeAt(index);
                }
                transfer.items.add(new File([bytes], item.name, {type: item.type}));
              }
              for (const type of ['dragenter', 'dragover', 'drop']) {
                target.dispatchEvent(new DragEvent(type, {
                  bubbles: true,
                  cancelable: true,
                  dataTransfer: transfer,
                }));
              }
              return true;
            }""",
            [SELECTORS["composer"], payload],
        )
    )


async def wait_upload_ready(
    client: "ChatGPTPage",
    *,
    request_marker: str,
    expected_count: int,
    timeout_ms: int,
    poll_ms: int = 100,
) -> "ChatGPTSnapshot":
    if expected_count < 1:
        raise ValueError("expected_count must be at least 1")
    deadline = time.monotonic() + timeout_ms / 1000
    last_snapshot = None
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            snapshot = await client.assert_ownership()
        except PageOwnershipError:
            raise
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(poll_ms / 1000)
            continue
        last_snapshot = snapshot
        if snapshot.choice_prompt_pending:
            raise ChoicePromptBlockedError(
                f"upload is blocked by choice prompt: {list(snapshot.choice_prompt_labels)!r}"
            )
        text = snapshot.composer_text.strip()
        if text and request_marker not in text:
            raise ComposerConflictError(
                "composer no longer contains the durable request marker"
            )
        if (
            snapshot.composer_present
            and snapshot.composer_editable
            and request_marker in text
            and len(snapshot.attachment_markers) >= expected_count
            and not snapshot.stop_visible
            and snapshot.send_visible
            and snapshot.send_enabled
        ):
            return snapshot
        await __import__("asyncio").sleep(poll_ms / 1000)

    if last_snapshot is not None:
        if not last_snapshot.composer_present:
            reason = "upload_composer_missing"
        elif request_marker not in last_snapshot.composer_text:
            reason = "upload_text_missing"
        elif len(last_snapshot.attachment_markers) < expected_count:
            reason = "upload_attachments_missing"
        elif last_snapshot.stop_visible:
            reason = "upload_waiting_active_response"
        else:
            reason = "upload_send_not_ready"
        raise UploadReadinessError(reason)
    if last_error is not None:
        raise UploadReadinessError(
            f"upload readiness failed after transient errors: {type(last_error).__name__}: {last_error}"
        ) from last_error
    raise UploadReadinessError("upload_waiting")


async def upload_files(
    client: "ChatGPTPage",
    paths: Sequence[str],
    *,
    request_marker: str,
    timeout_ms: int,
    max_total_bytes: int = 20 * 1024 * 1024,
) -> UploadReceipt:
    files = collect_file_identities(paths, max_total_bytes=max_total_bytes)
    async with client.mutation_guard():
        snapshot = await client.assert_ownership()
        if snapshot.stop_visible:
            raise UnsafePageStateError("cannot upload while a response is active")
        if snapshot.choice_prompt_pending:
            raise ChoicePromptBlockedError(
                f"upload is blocked by choice prompt: {list(snapshot.choice_prompt_labels)!r}"
            )
        if snapshot.blocking_dialogs:
            await dismiss_stale_upload_overlay(client.page)
            snapshot = await client.assert_ownership()
            if snapshot.blocking_dialogs:
                raise UnsafePageStateError(
                    f"blocking dialog remains before upload: {list(snapshot.blocking_dialogs)!r}"
                )
        if not snapshot.composer_present or not snapshot.composer_editable:
            raise UnsafePageStateError("upload composer is missing or not editable")
        if request_marker not in snapshot.composer_text:
            raise ComposerConflictError(
                "upload requires the exact durable request marker in the composer"
            )
        if snapshot.attachment_markers:
            if len(snapshot.attachment_markers) >= len(files):
                ready = await wait_upload_ready(
                    client,
                    request_marker=request_marker,
                    expected_count=len(files),
                    timeout_ms=timeout_ms,
                )
                return UploadReceipt(
                    request_marker=request_marker,
                    method="already_ready",
                    files=files,
                    attachment_count=len(ready.attachment_markers),
                )
            raise ComposerConflictError(
                "partial or unowned attachments already exist; refusing to duplicate upload"
            )

        path_values = [identity.path for identity in files]
        method = "input"
        if not await _upload_via_input(client.page, path_values):
            method = "drop"
            if not await _upload_via_drop(
                client.page, files, max_total_bytes=max_total_bytes
            ):
                raise UploadTransportError("no usable file input or drop target")
        ready = await wait_upload_ready(
            client,
            request_marker=request_marker,
            expected_count=len(files),
            timeout_ms=timeout_ms,
        )
        return UploadReceipt(
            request_marker=request_marker,
            method=method,
            files=files,
            attachment_count=len(ready.attachment_markers),
        )
