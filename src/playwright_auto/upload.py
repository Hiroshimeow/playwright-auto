from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from .chatgpt import (
    ATTACHMENT_OWNERSHIP_WINDOW_KEY,
    ChoicePromptBlockedError,
    ComposerConflictError,
    PageOwnershipError,
    SELECTORS,
    UnsafePageStateError,
    attachment_names_match,
    visible_text_matches,
)

if TYPE_CHECKING:
    from .chatgpt import ChatGPTPage, ChatGPTSnapshot


class UploadError(RuntimeError):
    pass


class UploadTransportError(UploadError):
    pass


class UploadIdentityChangedError(UploadError):
    pass


class UploadReadinessError(UploadError):
    pass


async def establish_attachment_ownership(
    page: Any,
    *,
    expected_files: Sequence["FileIdentity"],
    drop_provenance_token: str | None = None,
) -> str:
    files = tuple(expected_files)
    if not files:
        raise ValueError("expected files must be non-empty")
    names = tuple(item.name for item in files)
    expected = [
        {
            "name": item.name,
            "size": item.size,
            "sha256": item.sha256,
            "mime_type": item.mime_type,
        }
        for item in files
    ]
    token = secrets.token_urlsafe(24)
    result = await page.evaluate(
        r"""async ([ownershipKey, token, dropToken, expectedNames, expectedFiles, composerSelector]) => {
          if (!globalThis.crypto?.subtle) {
            return {ok: false, reason: 'secure_hashing_unavailable'};
          }
          const visible = (element) => {
            const style = element ? window.getComputedStyle(element) : null;
            return Boolean(
              element && style && style.visibility !== 'hidden' && style.visibility !== 'collapse' &&
              (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
            );
          };
          const text = (element) => (element?.innerText || '').replace(/\s+/g, ' ').trim();
          const composer = document.querySelector(composerSelector) ||
            document.querySelector('div#prompt-textarea');
          const composerHost = composer?.closest('form') || composer?.parentElement || null;
          if (!composer || !composerHost) {
            return {ok: false, reason: 'composer_missing'};
          }

          const previous = window[ownershipKey];
          const liveInputRecords = () => [...document.querySelectorAll('input[type="file"]')]
            .filter((input) => input.files && input.files.length > 0)
            .map((input) => ({input, files: [...input.files]}));
          let method;
          let inputRecords;
          let fileRecords;
          if (dropToken) {
            if (liveInputRecords().length || previous?.phase !== 'pending_drop' ||
                previous.token !== dropToken || previous.valid !== true ||
                !Array.isArray(previous.fileRecords)) {
              return {ok: false, reason: 'drop_provenance_changed'};
            }
            method = 'drop';
            inputRecords = [];
            fileRecords = [...previous.fileRecords];
          } else {
            method = 'input';
            inputRecords = liveInputRecords();
            fileRecords = inputRecords.flatMap((item) => item.files);
          }
          if (fileRecords.length !== expectedFiles.length) {
            return {ok: false, reason: 'browser_file_count_changed'};
          }
          for (let index = 0; index < fileRecords.length; index += 1) {
            const file = fileRecords[index];
            const expected = expectedFiles[index];
            if (!(file instanceof File) || file.name !== expected.name ||
                file.size !== expected.size || file.type !== expected.mime_type) {
              return {ok: false, reason: 'browser_file_identity_changed'};
            }
          }
          const referencesMatch = () => {
            if (method === 'drop') {
              const current = window[ownershipKey];
              return Boolean(
                current === previous && current?.phase === 'pending_drop' &&
                current.token === dropToken && current.valid === true &&
                Array.isArray(current.fileRecords) &&
                current.fileRecords.length === fileRecords.length &&
                current.fileRecords.every((file, index) => file === fileRecords[index]) &&
                liveInputRecords().length === 0
              );
            }
            const live = liveInputRecords();
            return live.length === inputRecords.length &&
              inputRecords.every((record, index) => {
                const current = live[index];
                return record.input === current.input && current.input.isConnected &&
                  record.files.length === current.files.length &&
                  record.files.every((file, fileIndex) => file === current.files[fileIndex]);
              });
          };
          if (!referencesMatch()) {
            return {ok: false, reason: 'browser_file_identity_changed'};
          }
          for (let index = 0; index < fileRecords.length; index += 1) {
            const file = fileRecords[index];
            const buffer = await file.arrayBuffer();
            if (!referencesMatch()) {
              return {ok: false, reason: 'browser_file_identity_changed'};
            }
            const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', buffer));
            if (!referencesMatch()) {
              return {ok: false, reason: 'browser_file_identity_changed'};
            }
            const sha256 = [...digest]
              .map((byte) => byte.toString(16).padStart(2, '0')).join('');
            if (sha256 !== expectedFiles[index].sha256) {
              return {ok: false, reason: 'browser_file_sha256_changed'};
            }
          }

          const filenameFromLabel = (value) => {
            const label = String(value || '').replace(/\s+/g, ' ').trim();
            const lower = label.toLowerCase();
            for (const prefix of [
              'remove file', 'remove attachment', 'open image',
              'attached file', 'file uploaded', 'uploading'
            ]) {
              const index = lower.indexOf(prefix);
              if (index < 0) continue;
              const candidate = label.slice(index + prefix.length)
                .replace(/^[\s:–—-]+/, '').trim();
              const indexed = candidate.match(/^\d+\s*:\s*(.+)$/);
              if (indexed) return indexed[1].trim();
              if (candidate) return candidate;
            }
            return '';
          };
          const directFilename = (element) => {
            for (const attribute of ['data-filename', 'data-file-name']) {
              const candidate = (element.getAttribute?.(attribute) || '').trim();
              if (candidate) return candidate;
            }
            return filenameFromLabel(element.getAttribute?.('aria-label'));
          };
          const hasAttachmentToken = (element) => {
            const tokens = (element.getAttribute?.('data-testid') || '')
              .toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
            return tokens.includes('attachment') || tokens.includes('file');
          };
          const leafFilename = (root) => {
            const candidates = [];
            for (const element of [root, ...root.querySelectorAll('*')]) {
              if (!visible(element) || element.matches('button,[role="button"],svg,path')) continue;
              if ([...element.children].some(visible)) continue;
              const candidate = text(element);
              if (candidate && !['remove', 'open', 'attached', 'uploading'].includes(candidate.toLowerCase())) {
                candidates.push(candidate);
              }
            }
            return candidates.length === 1 ? candidates[0] : '';
          };
          const attachmentRecords = [];
          const seenAttachmentItems = new Set();
          for (const candidate of composerHost.querySelectorAll(
            '[data-filename], [data-file-name], [aria-label]'
          )) {
            if (!visible(candidate)) continue;
            const explicitItem = candidate.closest('[data-filename], [data-file-name]');
            const item = explicitItem && explicitItem !== composerHost &&
              composerHost.contains(explicitItem) && visible(explicitItem)
              ? explicitItem : candidate;
            const filename = directFilename(item);
            if (!filename || seenAttachmentItems.has(item)) continue;
            seenAttachmentItems.add(item);
            attachmentRecords.push({element: item, filename});
          }
          for (const root of composerHost.querySelectorAll('[data-testid]')) {
            if (!visible(root) || !hasAttachmentToken(root)) continue;
            const hasFilenameEvidence = [root, ...root.querySelectorAll(
              '[data-filename], [data-file-name], [aria-label]'
            )].some((element) => visible(element) && Boolean(directFilename(element)));
            const hasNestedAttachmentRoot = [...root.querySelectorAll('[data-testid]')]
              .some((element) => element !== root && visible(element) && hasAttachmentToken(element));
            if (hasFilenameEvidence || hasNestedAttachmentRoot || seenAttachmentItems.has(root)) continue;
            seenAttachmentItems.add(root);
            attachmentRecords.push({
              element: root,
              filename: leafFilename(root) || '\u0000unidentified attachment',
            });
          }
          attachmentRecords.sort((left, right) => {
            if (left.element === right.element) return 0;
            const position = left.element.compareDocumentPosition(right.element);
            if (position & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
            if (position & Node.DOCUMENT_POSITION_PRECEDING) return 1;
            return 0;
          });
          const platformNameMatches = (actual, expected) => {
            if (actual === expected) return true;
            const dot = expected.lastIndexOf('.');
            const split = dot > 0 ? dot : expected.length;
            const stem = expected.slice(0, split);
            const suffix = expected.slice(split);
            if (!actual.startsWith(stem) || !actual.endsWith(suffix)) return false;
            const middle = actual.slice(stem.length, actual.length - suffix.length);
            return /^\([1-9]\d*\)$/.test(middle);
          };
          const actualNames = attachmentRecords.map((item) => item.filename);
          if (actualNames.length !== expectedNames.length ||
              !actualNames.every((name, index) => platformNameMatches(name, expectedNames[index]))) {
            return {ok: false, reason: 'attachment_names_changed', actualNames};
          }
          if (!referencesMatch()) {
            return {ok: false, reason: 'browser_file_identity_changed'};
          }

          try { previous?.observer?.disconnect?.(); } catch (_) {}
          try { previous?.abortController?.abort?.(); } catch (_) {}
          const abortController = new AbortController();
          const ownership = {
            phase: 'owned',
            token,
            valid: true,
            method,
            identities: expectedFiles.map((item) => ({...item})),
            names: [...expectedNames],
            actualNames: [...actualNames],
            attachmentElements: attachmentRecords.map((item) => item.element),
            inputRecords,
            fileRecords,
            abortController,
            observer: null,
          };
          const invalidate = () => {
            const current = window[ownershipKey];
            if (current?.token === token) current.valid = false;
          };
          document.addEventListener('change', (event) => {
            if (event.target?.matches?.('input[type="file"]')) invalidate();
          }, {capture: true, signal: abortController.signal});
          document.addEventListener('drop', invalidate, {
            capture: true,
            signal: abortController.signal,
          });
          const ownedElements = new Set(ownership.attachmentElements);
          const observer = new MutationObserver((mutations) => {
            for (const mutation of mutations) {
              if (mutation.type === 'attributes') {
                if (mutation.target?.matches?.('input[type="file"]') ||
                    ownedElements.has(mutation.target)) {
                  invalidate();
                  return;
                }
                continue;
              }
              if (mutation.type !== 'childList') continue;
              const changed = [...mutation.addedNodes, ...mutation.removedNodes];
              if (changed.some((node) => {
                if (!(node instanceof Element)) return false;
                if (ownedElements.has(node)) return true;
                return [...ownedElements].some((element) =>
                  node.contains(element) || element.contains(node)
                ) || node.matches?.('input[type="file"], [data-testid*="attachment"], [data-testid*="file"]') ||
                  Boolean(node.querySelector?.('input[type="file"], [data-testid*="attachment"], [data-testid*="file"]'));
              })) {
                invalidate();
                return;
              }
            }
          });
          observer.observe(composerHost, {
            subtree: true,
            childList: true,
            attributes: true,
            attributeFilter: ['data-filename', 'data-file-name', 'aria-label', 'value'],
          });
          ownership.observer = observer;
          window[ownershipKey] = ownership;
          return {ok: true, token};
        }""",
        [
            ATTACHMENT_OWNERSHIP_WINDOW_KEY,
            token,
            drop_provenance_token,
            list(names),
            expected,
            SELECTORS["composer"],
        ],
    )
    if not result.get("ok"):
        reason = str(result.get("reason") or "unknown")
        if reason == "secure_hashing_unavailable":
            raise UploadReadinessError(
                "attachment ownership requires secure browser SHA-256 support"
            )
        if reason.startswith("drop_provenance"):
            raise UploadReadinessError(
                f"drop provenance changed before ownership: {reason}"
            )
        raise UploadReadinessError(
            f"browser file identity changed before attachment ownership: {reason}"
        )
    return token


async def current_attachment_ownership_token(
    page: Any,
    *,
    expected_files: Sequence["FileIdentity"],
) -> str | None:
    files = tuple(expected_files)
    expected = [
        {
            "name": item.name,
            "size": item.size,
            "sha256": item.sha256,
            "mime_type": item.mime_type,
        }
        for item in files
    ]
    return await page.evaluate(
        r"""([ownershipKey, expectedFiles]) => {
          const ownership = window[ownershipKey];
          if (!ownership || ownership.phase !== 'owned' || ownership.valid !== true ||
              typeof ownership.token !== 'string') {
            return null;
          }
          if (!Array.isArray(ownership.identities) ||
              ownership.identities.length !== expectedFiles.length ||
              !ownership.identities.every((item, index) => {
                const expected = expectedFiles[index];
                return item.name === expected.name && item.size === expected.size &&
                  item.mime_type === expected.mime_type && item.sha256 === expected.sha256;
              }) || !Array.isArray(ownership.fileRecords) ||
              ownership.fileRecords.length !== expectedFiles.length ||
              !ownership.fileRecords.every((file) => file instanceof File) ||
              !Array.isArray(ownership.attachmentElements) ||
              !ownership.attachmentElements.every((element) => element?.isConnected)) {
            return null;
          }
          const liveInputs = [...document.querySelectorAll('input[type="file"]')]
            .filter((input) => input.files && input.files.length > 0);
          if (ownership.method === 'drop') {
            return liveInputs.length === 0 && Array.isArray(ownership.inputRecords) &&
              ownership.inputRecords.length === 0 ? ownership.token : null;
          }
          if (ownership.method !== 'input' || !Array.isArray(ownership.inputRecords) ||
              ownership.inputRecords.length !== liveInputs.length) {
            return null;
          }
          const liveFiles = [];
          const inputsMatch = ownership.inputRecords.every((record, index) => {
            const input = liveInputs[index];
            const files = [...(input.files || [])];
            liveFiles.push(...files);
            return record.input === input && input.isConnected &&
              Array.isArray(record.files) && record.files.length === files.length &&
              record.files.every((file, fileIndex) => file === files[fileIndex]);
          });
          return inputsMatch && liveFiles.length === ownership.fileRecords.length &&
            ownership.fileRecords.every((file, index) => file === liveFiles[index])
            ? ownership.token : null;
        }""",
        [ATTACHMENT_OWNERSHIP_WINDOW_KEY, expected],
    )


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
class FileSnapshot:
    identity: FileIdentity
    data: bytes

    def input_payload(self) -> dict[str, Any]:
        return {
            "name": self.identity.name,
            "mimeType": self.identity.mime_type,
            "buffer": self.data,
        }


@dataclass(frozen=True)
class UploadReceipt:
    request_marker: str
    method: str
    files: tuple[FileIdentity, ...]
    attachment_count: int
    ownership_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_marker": self.request_marker,
            "method": self.method,
            "files": [item.to_dict() for item in self.files],
            "attachment_count": self.attachment_count,
            "ownership_token": self.ownership_token,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UploadReceipt":
        return cls(
            request_marker=str(value["request_marker"]),
            method=str(value["method"]),
            files=tuple(FileIdentity.from_dict(item) for item in value.get("files", [])),
            attachment_count=int(value["attachment_count"]),
            ownership_token=(
                str(value.get("ownership_token") or "").strip() or None
            ),
        )


def collect_file_snapshots(
    paths: Sequence[str | Path], *, max_total_bytes: int = 20 * 1024 * 1024
) -> tuple[FileSnapshot, ...]:
    if not paths:
        raise ValueError("at least one upload path is required")
    if max_total_bytes < 0:
        raise ValueError("max_total_bytes must not be negative")
    snapshots: list[FileSnapshot] = []
    seen_paths: set[str] = set()
    total = 0
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        path_value = str(path)
        if path_value in seen_paths:
            raise ValueError(f"duplicate upload path: {path.name}")
        seen_paths.add(path_value)
        try:
            with path.open("rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise FileNotFoundError(path)
                data = bytearray()
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > max_total_bytes:
                        raise ValueError(
                            f"upload payload exceeds max_total_bytes={max_total_bytes}"
                        )
                    data.extend(chunk)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise FileNotFoundError(path) from exc
        payload = bytes(data)
        identity = FileIdentity(
            path=path_value,
            name=path.name,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            mime_type=mimetypes.guess_type(path.name)[0]
            or "application/octet-stream",
        )
        snapshots.append(FileSnapshot(identity=identity, data=payload))
    return tuple(snapshots)


def collect_file_identities(
    paths: Sequence[str | Path], *, max_total_bytes: int = 20 * 1024 * 1024
) -> tuple[FileIdentity, ...]:
    return tuple(
        snapshot.identity
        for snapshot in collect_file_snapshots(
            paths,
            max_total_bytes=max_total_bytes,
        )
    )


def _validate_expected_identities(
    snapshots: Sequence[FileSnapshot],
    expected_files: Sequence[FileIdentity] | None,
) -> tuple[FileIdentity, ...]:
    identities = tuple(snapshot.identity for snapshot in snapshots)
    if expected_files is None:
        return identities
    expected = tuple(expected_files)
    if identities != expected:
        actual_names = tuple(item.name for item in identities)
        expected_names = tuple(item.name for item in expected)
        raise UploadIdentityChangedError(
            "upload source identity changed before browser mutation: "
            f"expected {expected_names!r}, found {actual_names!r}"
        )
    return identities


def _request_anchor_matches(actual: str, expected: str, *, exact_prompt: bool) -> bool:
    if exact_prompt:
        return visible_text_matches(actual, expected)
    return expected in actual


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


async def _upload_via_input(page: Any, files: Sequence[FileSnapshot]) -> bool:
    inputs = page.locator('input#upload-files[type="file"]')
    count = await inputs.count()
    if count < 1:
        inputs = page.locator('input[type="file"]:not([accept*="image"])')
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
        inputs = page.locator('input#upload-files[type="file"]')
        count = await inputs.count()
        if count < 1:
            inputs = page.locator('input[type="file"]:not([accept*="image"])')
            count = await inputs.count()
    if count < 1:
        return False
    await inputs.first.set_input_files(
        [snapshot.input_payload() for snapshot in files]
    )
    return True


async def _upload_via_drop(
    page: Any,
    files: Sequence[FileSnapshot],
) -> str | None:
    payload = [
        {
            "name": snapshot.identity.name,
            "type": snapshot.identity.mime_type,
            "base64": base64.b64encode(snapshot.data).decode("ascii"),
        }
        for snapshot in files
    ]
    token = secrets.token_urlsafe(24)
    return await page.evaluate(
        r"""([selector, payload, ownershipKey, token]) => {
          const composer = document.querySelector(selector);
          const target = composer?.closest('form') || composer;
          if (!target) return null;
          const transfer = new DataTransfer();
          for (const item of payload) {
            const binary = atob(item.base64);
            const bytes = new Uint8Array(binary.length);
            for (let index = 0; index < binary.length; index += 1) {
              bytes[index] = binary.charCodeAt(index);
            }
            transfer.items.add(new File([bytes], item.name, {type: item.type}));
          }
          const fileRecords = [...transfer.files];
          for (const type of ['dragenter', 'dragover', 'drop']) {
            target.dispatchEvent(new DragEvent(type, {
              bubbles: true,
              cancelable: true,
              dataTransfer: transfer,
            }));
          }
          const previous = window[ownershipKey];
          try { previous?.observer?.disconnect?.(); } catch (_) {}
          try { previous?.abortController?.abort?.(); } catch (_) {}
          const abortController = new AbortController();
          const pending = {
            phase: 'pending_drop',
            token,
            valid: true,
            method: 'drop',
            names: fileRecords.map((file) => file.name),
            fileRecords,
            inputRecords: [],
            abortController,
            observer: null,
          };
          const invalidate = () => {
            const current = window[ownershipKey];
            if (current?.token === token) current.valid = false;
          };
          document.addEventListener('change', (event) => {
            if (event.target?.matches?.('input[type="file"]')) invalidate();
          }, {capture: true, signal: abortController.signal});
          document.addEventListener('drop', invalidate, {
            capture: true,
            signal: abortController.signal,
          });
          window[ownershipKey] = pending;
          return token;
        }""",
        [
            SELECTORS["composer"],
            payload,
            ATTACHMENT_OWNERSHIP_WINDOW_KEY,
            token,
        ],
    )


async def wait_upload_ready(
    client: "ChatGPTPage",
    *,
    request_marker: str,
    exact_prompt: bool = False,
    expected_names: Sequence[str] | None = None,
    expected_count: int | None = None,
    timeout_ms: int,
    poll_ms: int = 100,
) -> "ChatGPTSnapshot":
    names = tuple(str(item) for item in expected_names or ())
    if names:
        expected = len(names)
    elif expected_count is not None and expected_count > 0:
        expected = int(expected_count)
    else:
        raise ValueError("expected_names or positive expected_count is required")
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
        if text and not _request_anchor_matches(
            text,
            request_marker,
            exact_prompt=exact_prompt,
        ):
            raise ComposerConflictError(
                "composer no longer contains the durable request marker"
            )
        if (
            snapshot.composer_present
            and snapshot.composer_editable
            and _request_anchor_matches(
                text,
                request_marker,
                exact_prompt=exact_prompt,
            )
            and (
                attachment_names_match(snapshot.attachment_markers, names)
                if names
                else len(snapshot.attachment_markers) == expected
            )
            and not snapshot.stop_visible
            and snapshot.send_visible
            and snapshot.send_enabled
        ):
            return snapshot
        await __import__("asyncio").sleep(poll_ms / 1000)

    if last_snapshot is not None:
        if not last_snapshot.composer_present:
            reason = "upload_composer_missing"
        elif not _request_anchor_matches(
            last_snapshot.composer_text,
            request_marker,
            exact_prompt=exact_prompt,
        ):
            reason = "upload_text_missing"
        elif not last_snapshot.attachment_markers:
            reason = "upload_attachments_missing"
        elif names and not attachment_names_match(
            last_snapshot.attachment_markers,
            names,
        ):
            reason = "upload_attachment_identity_mismatch"
        elif len(last_snapshot.attachment_markers) != expected:
            reason = "upload_attachment_identity_mismatch"
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
    expected_files: Sequence[FileIdentity] | None = None,
    file_snapshots: Sequence[FileSnapshot] | None = None,
    exact_prompt: bool = False,
) -> UploadReceipt:
    snapshots = tuple(file_snapshots) if file_snapshots is not None else collect_file_snapshots(
        paths,
        max_total_bytes=max_total_bytes,
    )
    resolved_paths = tuple(str(Path(path).expanduser().resolve()) for path in paths)
    if tuple(snapshot.identity.path for snapshot in snapshots) != resolved_paths:
        raise UploadIdentityChangedError(
            "upload source identity changed before browser mutation: snapshot paths do not match"
        )
    files = _validate_expected_identities(snapshots, expected_files)
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
        if not _request_anchor_matches(
            snapshot.composer_text,
            request_marker,
            exact_prompt=exact_prompt,
        ):
            raise ComposerConflictError(
                "upload requires the exact durable request marker in the composer"
            )
        if snapshot.attachment_markers:
            raise ComposerConflictError(
                "unowned attachments already exist; refusing automated upload"
            )

        method = "input"
        drop_provenance_token: str | None = None
        if not await _upload_via_input(client.page, snapshots):
            method = "drop"
            drop_provenance_token = await _upload_via_drop(client.page, snapshots)
            if not drop_provenance_token:
                raise UploadTransportError("no usable file input or drop target")
        expected_names = tuple(item.name for item in files)
        ready = await wait_upload_ready(
            client,
            request_marker=request_marker,
            exact_prompt=exact_prompt,
            expected_names=expected_names,
            timeout_ms=timeout_ms,
        )
        ownership_token = await establish_attachment_ownership(
            client.page,
            expected_files=files,
            drop_provenance_token=drop_provenance_token,
        )
        return UploadReceipt(
            request_marker=request_marker,
            method=method,
            files=files,
            attachment_count=len(ready.attachment_markers),
            ownership_token=ownership_token,
        )
