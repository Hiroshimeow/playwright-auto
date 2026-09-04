from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from uuid import UUID

from .cdpa_browser_projection import inspect_page_metadata
from .cdpa_config import CDPAConfig
from .cdpa_independent import is_independent_task
from .chatgpt import (
    ChatGPTPage,
    ComposerConflictError,
    ManualInputPendingError,
    PageBinding,
    RateLimitBlockedError,
    action_delay,
    action_delay_multiplier,
    backend_conversation as read_backend_conversation,
    backend_search_conversations as read_backend_search_conversations,
    backend_stream_status as read_backend_stream_status,
    clear_composer,
    random_delay,
    rate_limit_dialogs,
    refresh_page,
)
from .workspace import ChatGPTWorkspace

_CHATGPT_HOSTS = frozenset({"chatgpt.com", "www.chatgpt.com"})
_NEW_BRANCH_ENDPOINT = "https://chatgpt.com/backend-api/conversation/new_branch"
_TRANSIENT_PAGE_LIFECYCLE_MARKERS = (
    "target crashed",
    "execution context was destroyed",
    "execution context destroyed",
    "cannot find context with specified id",
    "navigation interrupted",
    "navigation was interrupted",
    "target closed",
    "page has been closed",
)


def is_transient_page_lifecycle_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_PAGE_LIFECYCLE_MARKERS)


def _reopenable_conversation_identity(value: Any) -> str | None:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return None
    path = parsed.path.rstrip("/")
    conversation_id = path.rsplit("/", 1)[-1]
    if (
        (parsed.hostname or "").lower() not in _CHATGPT_HOSTS
        or not path.startswith("/c/")
        or len(path) <= 3
        or conversation_id.startswith("WEB:")
    ):
        return None
    return path


class BranchBootstrapError(RuntimeError):
    pass


class BranchTargetUnresolvedError(BranchBootstrapError):
    pass


class RoleOwnershipError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "role_ownership_ambiguous",
    ) -> None:
        super().__init__(message)
        self.code = code


class TeamCloseError(RuntimeError):
    def __init__(self, message: str, *, closed_tabs: int = 0) -> None:
        super().__init__(message)
        self.closed_tabs = max(0, int(closed_tabs))


@dataclass(frozen=True)
class AcquiredRole:
    client: ChatGPTPage
    page_id: str
    url: str
    created: bool
    new_chat: bool
    conversation_id: str | None = None


def _canonical_uuid_text(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise BranchBootstrapError(f"{field} must be canonical UUID text")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise BranchBootstrapError(f"{field} must be canonical UUID text") from exc
    if str(parsed) != value:
        raise BranchBootstrapError(f"{field} must be canonical UUID text")
    return value


def _canonical_conversation_uuid(value: Any) -> str | None:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in _CHATGPT_HOSTS:
        return None
    path = parsed.path.rstrip("/")
    parts = path.split("/")
    if len(parts) != 3 or parts[:2] != ["", "c"]:
        return None
    conversation_id = parts[2]
    try:
        parsed_id = UUID(conversation_id)
    except (ValueError, AttributeError):
        return None
    return conversation_id if str(parsed_id) == conversation_id else None


class CDPATabActions:
    def __init__(self, browser_context: Any, config: CDPAConfig) -> None:
        self.browser_context = browser_context
        self.config = config
        self._lifecycle_warning_emitted = False

    async def backend_stream_status(self, conversation_id: str) -> dict[str, Any]:
        return await read_backend_stream_status(self.browser_context, conversation_id)

    async def backend_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await read_backend_conversation(self.browser_context, conversation_id)

    async def backend_search_conversations(
        self, query: str, *, max_candidates: int = 25
    ) -> list[str]:
        return await read_backend_search_conversations(
            self.browser_context, query, max_candidates=max_candidates
        )

    async def _set_page_active(self, page: Any) -> None:
        try:
            session = await self.browser_context.new_cdp_session(page)
            try:
                await session.send("Page.setWebLifecycleState", {"state": "active"})
                await session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            finally:
                await session.detach()
        except Exception as exc:
            if not self._lifecycle_warning_emitted:
                self._lifecycle_warning_emitted = True
                warnings.warn(
                    f"CDPA page lifecycle activation failed: {type(exc).__name__}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )

    async def wake(self, acquired: AcquiredRole) -> None:
        await self._set_page_active(acquired.client.page)

    @staticmethod
    def _supported(page: Any) -> bool:
        try:
            return (urlparse(str(page.url)).hostname or "").lower() in _CHATGPT_HOSTS
        except ValueError:
            return False

    async def _matching_clients(
        self,
        manifest: Mapping[str, Any],
        logical_role: str,
    ) -> list[tuple[ChatGPTPage, Any]]:
        role_record = manifest["roles"][logical_role]
        physical = str(role_record["physical_role"])
        team = str(manifest["team"])
        task_id = str(manifest["task_id"])
        recorded_page_id = str(role_record.get("page_id") or "").strip()
        exact: list[tuple[ChatGPTPage, Any]] = []
        reusable: list[tuple[int, tuple[ChatGPTPage, Any]]] = []
        reusable_rank = {
            str(candidate): index
            for index, candidate in enumerate(manifest.get("reusable_teams") or ())
        }
        for page in self.browser_context.pages:
            if page.is_closed() or not self._supported(page):
                continue
            client = ChatGPTPage(
                page,
                timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
            )
            metadata = await inspect_page_metadata(page)
            if metadata.get("page_id") and metadata.get("role"):
                snapshot = SimpleNamespace(
                    page_id=metadata.get("page_id"),
                    page_role=metadata.get("role"),
                    page_team=metadata.get("team"),
                    page_task_id=metadata.get("task_id"),
                    url=metadata.get("url") or str(page.url),
                )
            else:
                try:
                    snapshot = await client.snapshot()
                except Exception:
                    continue
            if snapshot.page_role != physical or not snapshot.page_id:
                continue
            client.binding = PageBinding(str(snapshot.page_id), physical)
            item = (client, snapshot)
            if snapshot.page_team == team and snapshot.page_task_id == task_id:
                exact.append(item)
            elif (
                is_independent_task(manifest)
                and recorded_page_id
                and str(snapshot.page_id) == recorded_page_id
                and snapshot.page_team == team
            ):
                reusable.append((-1, item))
            elif not recorded_page_id and snapshot.page_team in reusable_rank:
                reusable.append((reusable_rank[str(snapshot.page_team)], item))
        if len(exact) > 1:
            raise RoleOwnershipError(
                f"multiple exact tabs match {physical!r} for team {team!r} task {task_id!r}"
            )
        if exact:
            return exact
        if reusable:
            best_rank = min(rank for rank, _item in reusable)
            best = [item for rank, item in reusable if rank == best_rank]
            if len(best) > 1:
                raise RoleOwnershipError(
                    f"multiple reusable terminal tabs match {physical!r} for team {team!r}"
                )
            return best
        if recorded_page_id:
            return []
        return []

    async def acquire_global_role(
        self, physical_role: str, *, allow_create: bool = True
    ) -> AcquiredRole:
        """Reuse exactly one role-only tab; optionally create it when absent."""
        physical = str(physical_role).strip().upper()
        matches: list[tuple[ChatGPTPage, Any]] = []
        for page in self.browser_context.pages:
            if page.is_closed() or not self._supported(page):
                continue
            client = ChatGPTPage(
                page,
                timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
            )
            try:
                snapshot = await client.snapshot()
            except Exception as exc:
                raise RoleOwnershipError(
                    "cannot inspect a supported ChatGPT page for the global role: "
                    f"{getattr(page, 'url', '<unknown>')!r}: {type(exc).__name__}: {exc}"
                ) from exc
            if snapshot.page_role != physical:
                continue
            if not snapshot.page_id:
                raise RoleOwnershipError("global role tab has no page identity")
            if snapshot.page_team or snapshot.page_task_id:
                raise RoleOwnershipError("global role tab must not own a team or task")
            client.binding = PageBinding(str(snapshot.page_id), physical)
            matches.append((client, snapshot))
        if len(matches) > 1:
            raise RoleOwnershipError(f"multiple global tabs match {physical!r}")
        if matches:
            client, snapshot = matches[0]
            await client.assert_ownership()
            return AcquiredRole(
                client=client,
                page_id=str(snapshot.page_id),
                url=str(snapshot.url),
                created=False,
                new_chat=False,
            )
        if not allow_create:
            raise RoleOwnershipError(f"global role {physical!r} has no open tab")
        await random_delay(action_delay_multiplier("open_tab"))
        workspace = ChatGPTWorkspace()
        client = await workspace.open_role(
            self.browser_context,
            physical,
            timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
        )
        snapshot = await client.assert_ownership()
        if snapshot.page_team or snapshot.page_task_id:
            await client.page.close()
            raise RoleOwnershipError("new global role tab unexpectedly owns a team or task")
        assert client.binding is not None
        return AcquiredRole(
            client=client,
            page_id=client.binding.page_id,
            url=str(snapshot.url),
            created=True,
            new_chat=False,
        )

    async def locate_owned_metadata(
        self,
        manifest: Mapping[str, Any],
        logical_role: str,
    ) -> AcquiredRole | None:
        """Locate the exact owned tab without falling back to a DOM snapshot."""
        role_record = manifest["roles"][logical_role]
        physical = str(role_record["physical_role"])
        team = str(manifest["team"])
        task_id = str(manifest["task_id"])
        matches: list[tuple[ChatGPTPage, Mapping[str, Any]]] = []
        for page in self.browser_context.pages:
            if page.is_closed() or not self._supported(page):
                continue
            metadata = await inspect_page_metadata(page)
            if (
                metadata.get("role") != physical
                or metadata.get("team") != team
                or metadata.get("task_id") != task_id
                or not metadata.get("page_id")
            ):
                continue
            client = ChatGPTPage(
                page,
                timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
            )
            client.binding = PageBinding(str(metadata["page_id"]), physical)
            matches.append((client, metadata))
        if len(matches) > 1:
            raise RoleOwnershipError(
                f"multiple exact tabs match {physical!r} for team {team!r} task {task_id!r}"
            )
        if not matches:
            return None
        client, metadata = matches[0]
        return AcquiredRole(
            client=client,
            page_id=str(metadata["page_id"]),
            url=str(metadata.get("url") or client.page.url),
            created=False,
            new_chat=False,
        )

    async def locate_owned(
        self,
        manifest: Mapping[str, Any],
        logical_role: str,
    ) -> AcquiredRole | None:
        matches = await self._matching_clients(manifest, logical_role)
        if not matches:
            return None
        client, snapshot = matches[0]
        await self._set_page_active(client.page)
        return AcquiredRole(
            client=client,
            page_id=str(snapshot.page_id),
            url=str(snapshot.url),
            created=False,
            new_chat=False,
        )

    async def _sanitize_new_branch_composer(
        self,
        client: ChatGPTPage,
        *,
        timeout_ms: int,
    ) -> None:
        async def inspect(phase: str) -> Any:
            try:
                return await client.assert_ownership()
            except Exception as exc:
                raise ComposerConflictError(
                    f"new branch composer {phase} inspection failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

        snapshot = await inspect("initial")
        limited = rate_limit_dialogs(snapshot)
        if limited:
            raise RateLimitBlockedError(
                f"request rate limit blocks branch composer cleanup: {list(limited)!r}"
            )
        if snapshot.attachment_markers:
            raise ManualInputPendingError(
                "new branch page contains attachments; automated composer cleanup blocked"
            )

        draft = snapshot.composer_text.strip()
        if not draft:
            return
        if (
            snapshot.requires_login
            or not snapshot.composer_present
            or not snapshot.composer_editable
            or snapshot.stop_visible
            or snapshot.blocking_dialogs
            or str(getattr(snapshot.state, "value", snapshot.state)) == "error"
        ):
            raise ManualInputPendingError(
                "new branch page has ambiguous state with composer text; automated cleanup blocked"
            )

        try:
            await clear_composer(client.page, timeout_ms=timeout_ms)
        except Exception as exc:
            raise ComposerConflictError(
                f"new branch composer clear failed: {type(exc).__name__}: {exc}"
            ) from exc

        immediate = await inspect("post-clear")
        if rate_limit_dialogs(immediate):
            raise RateLimitBlockedError("request rate limit appeared during branch composer cleanup")
        if immediate.attachment_markers:
            raise ManualInputPendingError(
                "attachments appeared during new branch composer cleanup"
            )
        if immediate.composer_text.strip():
            raise ComposerConflictError("new branch composer did not become empty")

        await asyncio.sleep(0.1)
        stable = await inspect("stable-empty")
        if rate_limit_dialogs(stable):
            raise RateLimitBlockedError("request rate limit appeared after branch composer cleanup")
        if stable.attachment_markers:
            raise ManualInputPendingError(
                "attachments appeared after new branch composer cleanup"
            )
        if stable.composer_text.strip():
            raise ComposerConflictError("new branch composer rehydrated after cleanup")

    @staticmethod
    def _watch_new_branch_response(page: Any) -> tuple[asyncio.Future[Any], Any]:
        future = asyncio.get_running_loop().create_future()

        def capture(response: Any) -> None:
            if future.done() or str(getattr(response, "url", "")) != _NEW_BRANCH_ENDPOINT:
                return
            future.set_result(response)

        page.on("response", capture)
        return future, capture

    @staticmethod
    async def _new_branch_conversation_id(
        future: asyncio.Future[Any], *, timeout_ms: int
    ) -> str:
        try:
            response = await asyncio.wait_for(future, timeout=max(timeout_ms, 1) / 1000)
        except asyncio.TimeoutError as exc:
            raise BranchTargetUnresolvedError(
                "new_branch backend response did not provide canonical conversation identity"
            ) from exc
        status = int(getattr(response, "status", 0) or 0)
        if status < 200 or status >= 300:
            raise BranchTargetUnresolvedError(
                f"new_branch backend response failed with HTTP {status}"
            )
        try:
            payload = await response.json()
            conversation = payload.get("conversation") if isinstance(payload, Mapping) else None
            candidate = conversation.get("conversation_id") if isinstance(conversation, Mapping) else None
            return _canonical_uuid_text(candidate, field="branch conversation id")
        except Exception as exc:
            raise BranchTargetUnresolvedError(
                "new_branch backend response did not contain canonical conversation identity"
            ) from exc

    async def validate_branch_target(
        self,
        client: ChatGPTPage,
        *,
        source_conversation_id: str,
        candidate_conversation_id: str,
        physical_role: str,
        task_id: str,
        team: str,
    ) -> str:
        source_id = _canonical_uuid_text(
            source_conversation_id,
            field="source conversation id",
        )
        candidate_id = _canonical_uuid_text(
            candidate_conversation_id,
            field="branch conversation id",
        )
        if candidate_id == source_id:
            raise BranchBootstrapError(
                "branch target canonicalized back to source/donor conversation"
            )

        timeout_ms = round(self.config.workspace_timeout_seconds * 1000)
        for page in tuple(self.browser_context.pages):
            if page is client.page or page.is_closed() or not self._supported(page):
                continue
            metadata = await inspect_page_metadata(page)
            owner_url = metadata.get("url") or getattr(page, "url", "")
            if _canonical_conversation_uuid(owner_url) != candidate_id:
                continue
            owner_role = str(metadata.get("role") or "")
            owner_task = str(metadata.get("task_id") or "")
            owner_team = str(metadata.get("team") or "")
            if not (owner_role and owner_task and owner_team):
                other = ChatGPTPage(page, timeout_ms=timeout_ms)
                try:
                    owner = await other.snapshot()
                except Exception:
                    continue
                owner_role = str(getattr(owner, "page_role", "") or "")
                owner_task = str(getattr(owner, "page_task_id", "") or "")
                owner_team = str(getattr(owner, "page_team", "") or "")
            if not (owner_role and owner_task and owner_team):
                continue
            if (owner_task, owner_role, owner_team) != (
                str(task_id),
                str(physical_role),
                str(team),
            ):
                raise BranchBootstrapError(
                    "branch target conversation already has a foreign writable owner"
                )
        return candidate_id

    async def branch_from_anchor(
        self,
        manifest: Mapping[str, Any],
        logical_role: str,
        *,
        source_conversation_id: str,
        assistant_message_id: str,
    ) -> AcquiredRole:
        try:
            logical_role = str(logical_role).upper()
            role_record = manifest["roles"][logical_role]
            physical = str(role_record["physical_role"])
            task_id = str(manifest["task_id"])
            team = str(manifest["team"])
        except Exception as exc:
            raise BranchBootstrapError("branch bootstrap target ownership is invalid") from exc

        source_id = _canonical_uuid_text(
            source_conversation_id,
            field="source conversation id",
        )
        message_id = _canonical_uuid_text(
            assistant_message_id,
            field="assistant message id",
        )
        branch_url = f"https://chatgpt.com/branch/{source_id}/{message_id}"
        timeout = round(self.config.workspace_timeout_seconds * 1000)
        client: ChatGPTPage | None = None
        page: Any | None = None
        response_listener = None
        try:
            await random_delay(action_delay_multiplier("open_tab"))
            workspace = ChatGPTWorkspace()
            page = await self.browser_context.new_page()
            branch_response, response_listener = self._watch_new_branch_response(page)
            await page.goto(branch_url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_url(
                lambda url: str(url).split("?", 1)[0].rstrip("/") != branch_url,
                wait_until="domcontentloaded",
                timeout=timeout,
            )
            await page.locator('[contenteditable="true"][role="textbox"]').first.wait_for(
                state="visible", timeout=timeout
            )
            client = await workspace.bind(
                physical,
                page,
                timeout_ms=timeout,
                force_new_page_id=True,
            )
            await self._sanitize_new_branch_composer(client, timeout_ms=timeout)
            await client.wait_until_clean_ready(timeout_ms=timeout)
            candidate_id = await self._new_branch_conversation_id(
                branch_response, timeout_ms=timeout
            )
            await self.validate_branch_target(
                client,
                source_conversation_id=source_id,
                candidate_conversation_id=candidate_id,
                physical_role=physical,
                task_id=task_id,
                team=team,
            )
            await client.bind_task_identity(task_id, team)
            snapshot = await client.assert_ownership()
            binding = client.binding
            if (
                binding is None
                or str(snapshot.page_id or "") != binding.page_id
                or str(snapshot.page_role or "") != physical
                or str(snapshot.page_task_id or "") != task_id
                or str(snapshot.page_team or "") != team
            ):
                raise BranchBootstrapError(
                    "branch bootstrap target ownership did not persist"
                )
            return AcquiredRole(
                client=client,
                page_id=binding.page_id,
                url=str(snapshot.url),
                created=True,
                new_chat=True,
                conversation_id=candidate_id,
            )
        except Exception as exc:
            target_page = client.page if client is not None else page
            if target_page is not None and not target_page.is_closed():
                try:
                    await target_page.close()
                except Exception:
                    pass
            if isinstance(
                exc,
                (
                    BranchBootstrapError,
                    ComposerConflictError,
                    ManualInputPendingError,
                    RateLimitBlockedError,
                ),
            ):
                raise
            raise BranchBootstrapError(
                f"branch bootstrap failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            target_page = client.page if client is not None else page
            if target_page is not None and response_listener is not None:
                target_page.remove_listener("response", response_listener)

    async def acquire(
        self,
        manifest: Mapping[str, Any],
        logical_role: str,
    ) -> AcquiredRole:
        logical_role = str(logical_role).upper()
        role_record = manifest["roles"][logical_role]
        physical = str(role_record["physical_role"])
        team = str(manifest["team"])
        task_id = str(manifest["task_id"])
        matches = await self._matching_clients(manifest, logical_role)
        created = not matches
        if created and role_record.get("page_id"):
            raise RoleOwnershipError(
                f"recorded {physical!r} tab is offline; use Open tab for controlled recovery",
                code="role_offline",
            )
        if created:
            await random_delay(action_delay_multiplier("open_tab"))
            workspace = ChatGPTWorkspace()
            client = await workspace.open_role(
                self.browser_context,
                physical,
                timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
            )
            snapshot = await client.assert_ownership()
            await client.bind_task_identity(task_id, team)
            new_chat = False
        else:
            client, snapshot = matches[0]
            await client.assert_ownership()
            force_new = bool(
                role_record.get("reset_requested")
                and role_record.get("reset_applied_generation")
                != role_record.get("conversation_generation")
            )
            if (
                not force_new
                and snapshot.page_task_id != task_id
                and snapshot.page_team in (manifest.get("reusable_teams") or ())
            ):
                await client.task_preflight(task_id)
                await client.bind_task_identity(task_id, team)
                new_chat = False
            else:
                prepared = await client.prepare_task(task_id, force_new_chat=force_new)
                await client.bind_task_identity(task_id, team)
                new_chat = not bool(prepared.get("reused"))
            snapshot = await client.assert_ownership()
        if snapshot.page_team != team or snapshot.page_task_id != task_id:
            snapshot = await client.assert_ownership()
        if snapshot.page_team != team or snapshot.page_task_id != task_id:
            raise RoleOwnershipError("task/team ownership did not persist on acquired role tab")
        assert client.binding is not None
        await self._set_page_active(client.page)
        return AcquiredRole(
            client=client,
            page_id=client.binding.page_id,
            url=str(snapshot.url),
            created=created,
            new_chat=new_chat,
        )

    async def reopen(
        self,
        manifest: Mapping[str, Any],
        logical_role: str,
        *,
        require_clean_ready: bool = True,
        foreground: bool = True,
    ) -> AcquiredRole:
        logical_role = str(logical_role).upper()
        role_record = manifest["roles"][logical_role]
        physical = str(role_record["physical_role"])
        page_id = str(role_record.get("page_id") or "").strip()
        active_hop = next(
            (
                hop
                for hop in manifest.get("hops") or ()
                if isinstance(hop, Mapping)
                and hop.get("hop_id") == manifest.get("active_hop_id")
                and str(hop.get("target_role") or "").upper() == logical_role
            ),
            None,
        )
        candidate_urls = (
            str((active_hop or {}).get("conversation_url") or "").strip(),
            str(role_record.get("page_url") or "").strip(),
        )
        page_url = next(
            (value for value in candidate_urls if _reopenable_conversation_identity(value) is not None),
            next((value for value in candidate_urls if value), ""),
        )
        if not page_id or not page_url:
            raise RoleOwnershipError("role has no recorded page identity to reopen")
        parsed_url = urlparse(page_url)
        if (parsed_url.hostname or "").lower() not in _CHATGPT_HOSTS:
            raise RoleOwnershipError(
                f"recorded role URL is not a ChatGPT conversation: {page_url!r}"
            )
        conversation_id = parsed_url.path.rstrip("/").rsplit("/", 1)[-1]
        if conversation_id.startswith("WEB:"):
            raise RoleOwnershipError(
                "temporary WEB conversation cannot be reopened exactly; use Restart role or Stop"
            )
        if not parsed_url.path.startswith("/c/"):
            raise RoleOwnershipError(
                f"recorded role URL is not an exact conversation URL: {page_url!r}"
            )

        existing = await self.locate_owned(manifest, logical_role)
        created = existing is None
        if created:
            await random_delay(action_delay_multiplier("open_tab"))
            page = await self.browser_context.new_page()
            client = ChatGPTPage(
                page,
                timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
            )
        else:
            client = existing.client
            page = client.page
        timeout = round(self.config.workspace_timeout_seconds * 1000)
        try:
            actual_url = urlparse(str(page.url))
            exact_url = (
                actual_url.scheme == parsed_url.scheme
                and actual_url.netloc == parsed_url.netloc
                and actual_url.path.rstrip("/") == parsed_url.path.rstrip("/")
            )
            if not exact_url:
                await page.goto(
                    page_url, wait_until="domcontentloaded", timeout=timeout
                )
                await page.locator("body").wait_for(
                    state="visible", timeout=timeout
                )
                actual_url = urlparse(str(page.url))
                if (
                    actual_url.scheme != parsed_url.scheme
                    or actual_url.netloc != parsed_url.netloc
                    or actual_url.path.rstrip("/") != parsed_url.path.rstrip("/")
                ):
                    raise RoleOwnershipError(
                        "conversation reopen redirected away from the recorded URL: "
                        f"{page.url!r}"
                    )
                await client.restore_identity(
                    page_id=page_id,
                    role=physical,
                    task_id=str(manifest["task_id"]),
                    team=str(manifest["team"]),
                )
            else:
                snapshot = await client.assert_ownership()
                if (
                    str(snapshot.page_id or "") != page_id
                    or str(snapshot.page_role or "") != physical
                    or str(snapshot.page_task_id or "") != str(manifest["task_id"])
                    or str(snapshot.page_team or "") != str(manifest["team"])
                ):
                    await client.restore_identity(
                        page_id=page_id,
                        role=physical,
                        task_id=str(manifest["task_id"]),
                        team=str(manifest["team"]),
                    )
            if require_clean_ready:
                await client.wait_until_clean_ready(timeout_ms=timeout)
            snapshot = await client.assert_ownership()
            if (
                str(snapshot.page_id or "") != page_id
                or str(snapshot.page_role or "") != physical
                or str(snapshot.page_task_id or "") != str(manifest["task_id"])
                or str(snapshot.page_team or "") != str(manifest["team"])
            ):
                raise RoleOwnershipError(
                    "conversation reopen did not restore exact role/task ownership"
                )
            if foreground:
                await self._set_page_active(page)
                await page.bring_to_front()
            return AcquiredRole(
                client=client,
                page_id=page_id,
                url=str(snapshot.url),
                created=created,
                new_chat=False,
            )
        except Exception:
            if created:
                await page.close()
            raise

    async def restart(
        self,
        manifest: Mapping[str, Any],
        logical_role: str,
        *,
        known_automated_draft: str | None = None,
    ) -> AcquiredRole:
        """Explicitly start a fresh conversation for a blocked/offline role."""
        logical_role = str(logical_role).upper()
        role_record = manifest["roles"][logical_role]
        physical = str(role_record["physical_role"])
        task_id = str(manifest["task_id"])
        team = str(manifest["team"])
        existing = await self.locate_owned(manifest, logical_role)
        if existing is not None:
            snapshot = await existing.client.assert_ownership()
            if snapshot.composer_text.strip():
                await existing.client.new_chat(
                    expected_draft_text=known_automated_draft,
                )
            else:
                await existing.client.prepare_task(task_id, force_new_chat=True)
            await existing.client.bind_task_identity(task_id, team)
            snapshot = await existing.client.assert_ownership()
            return AcquiredRole(
                client=existing.client,
                page_id=existing.page_id,
                url=str(snapshot.url),
                created=False,
                new_chat=True,
            )

        await random_delay(action_delay_multiplier("open_tab"))
        workspace = ChatGPTWorkspace()
        client = await workspace.open_role(
            self.browser_context,
            physical,
            timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
        )
        try:
            snapshot = await client.assert_ownership()
            if snapshot.composer_text.strip():
                await client.new_chat(
                    expected_draft_text=known_automated_draft,
                )
            await client.bind_task_identity(task_id, team)
            snapshot = await client.assert_ownership()
            assert client.binding is not None
            return AcquiredRole(
                client=client,
                page_id=client.binding.page_id,
                url=str(snapshot.url),
                created=True,
                new_chat=True,
            )
        except Exception:
            await client.page.close()
            raise

    async def fresh_chat(
        self,
        manifest: Mapping[str, Any],
        logical_role: str,
        *,
        url: str,
    ) -> AcquiredRole:
        selected = await self.preflight_team(manifest)
        if selected:
            await self.close_team(manifest, preflighted_pages=selected)
            if await self.preflight_team(manifest):
                raise TeamCloseError("fresh chat could not close the previous owned tab")
        logical_role = str(logical_role).upper()
        record = manifest["roles"][logical_role]
        physical = str(record["physical_role"])
        await random_delay(action_delay_multiplier("open_tab"))
        workspace = ChatGPTWorkspace()
        client = await workspace.open_role(
            self.browser_context,
            physical,
            url=url,
            timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
            force_new_page_id=True,
        )
        try:
            await client.bind_task_identity(str(manifest["task_id"]), str(manifest["team"]))
            snapshot = await client.assert_ownership()
            assert client.binding is not None
            await self._set_page_active(client.page)
            return AcquiredRole(
                client=client,
                page_id=client.binding.page_id,
                url=str(snapshot.url),
                created=True,
                new_chat=True,
            )
        except Exception:
            await client.page.close()
            raise

    async def new_chat(
        self,
        manifest: Mapping[str, Any],
        logical_role: str,
    ) -> AcquiredRole:
        acquired = await self.acquire(manifest, logical_role)
        await acquired.client.prepare_task(str(manifest["task_id"]), force_new_chat=True)
        await acquired.client.bind_task_identity(str(manifest["task_id"]), str(manifest["team"]))
        snapshot = await acquired.client.assert_ownership()
        return AcquiredRole(
            client=acquired.client,
            page_id=acquired.page_id,
            url=str(snapshot.url),
            created=acquired.created,
            new_chat=True,
        )

    async def refresh(
        self,
        acquired: AcquiredRole,
        *,
        manifest: Mapping[str, Any] | None = None,
        logical_role: str | None = None,
        recover: bool = False,
        skip_precheck: bool = False,
    ) -> AcquiredRole:
        timeout = max(float(self.config.workspace_timeout_seconds), 0.001)
        if skip_precheck and not recover:
            raise RoleOwnershipError("precheck bypass is only valid for recovery refresh")
        async with asyncio.timeout(timeout):
            target = acquired
            expected = None
            if recover:
                if manifest is None or logical_role is None:
                    raise RoleOwnershipError(
                        "recovery refresh requires exact manifest role ownership"
                    )
                expected = self._recovery_refresh_expected(
                    target, manifest, logical_role
                )
            precheck = None
            if skip_precheck:
                target = await self._recovery_refresh_target(
                    acquired, manifest, logical_role
                )
                expected = self._recovery_refresh_expected(
                    target, manifest, logical_role
                )
            else:
                try:
                    precheck = await target.client.assert_ownership()
                except Exception as exc:
                    if not recover or not is_transient_page_lifecycle_error(exc):
                        raise
                    target = await self._recovery_refresh_target(
                        acquired, manifest, logical_role
                    )
                    expected = self._recovery_refresh_expected(
                        target, manifest, logical_role
                    )
                if expected is not None and precheck is not None:
                    self._assert_recovery_snapshot(precheck, expected)

            await action_delay(
                target.client.page,
                "refresh",
                action_delay_multiplier("refresh"),
            )
            await refresh_page(
                target.client.page,
                timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
            )
            snapshot = await target.client.assert_ownership()
            if expected is not None:
                self._assert_recovery_snapshot(snapshot, expected)
            return target

    @staticmethod
    def _recovery_refresh_expected(
        acquired: AcquiredRole,
        manifest: Mapping[str, Any],
        logical_role: str,
    ) -> tuple[str, str, str, str]:
        role = str(logical_role).upper()
        roles = manifest.get("roles")
        record = roles.get(role) if isinstance(roles, Mapping) else None
        if not isinstance(record, Mapping):
            raise RoleOwnershipError(f"recovery role {role!r} is not present in manifest")
        page_id = str(record.get("page_id") or "").strip()
        physical = str(record.get("physical_role") or "").strip()
        task_id = str(manifest.get("task_id") or "").strip()
        team = str(manifest.get("team") or "").strip()
        binding = getattr(acquired.client, "binding", None)
        if (
            not page_id
            or not physical
            or not task_id
            or not team
            or acquired.page_id != page_id
            or binding is None
            or str(getattr(binding, "page_id", "")) != page_id
            or str(getattr(binding, "role", "")) != physical
        ):
            raise RoleOwnershipError(
                "recovery refresh durable/in-memory binding does not match exact role page"
            )
        return page_id, physical, task_id, team

    @staticmethod
    def _assert_recovery_snapshot(
        snapshot: Any,
        expected: tuple[str, str, str, str],
    ) -> None:
        page_id, physical, task_id, team = expected
        if (
            str(getattr(snapshot, "page_id", "") or "") != page_id
            or str(getattr(snapshot, "page_role", "") or "") != physical
            or str(getattr(snapshot, "page_task_id", "") or "") != task_id
            or str(getattr(snapshot, "page_team", "") or "") != team
        ):
            raise RoleOwnershipError(
                "recovery refresh post-reload ownership did not match exact page/role/task/team"
            )

    async def _recovery_refresh_target(
        self,
        acquired: AcquiredRole,
        manifest: Mapping[str, Any],
        logical_role: str,
    ) -> AcquiredRole:
        expected = self._recovery_refresh_expected(acquired, manifest, logical_role)
        page = acquired.client.page
        is_closed = getattr(page, "is_closed", None)
        if not callable(is_closed) or not is_closed():
            return acquired
        reacquired = await self.locate_owned_metadata(manifest, logical_role)
        if reacquired is None:
            raise RoleOwnershipError("closed recovery target could not be reacquired exactly")
        self._recovery_refresh_expected(reacquired, manifest, logical_role)
        if reacquired.page_id != expected[0]:
            raise RoleOwnershipError("closed recovery target page id changed during reacquire")
        return reacquired

    async def stop_if_active(self, acquired: AcquiredRole) -> bool:
        snapshot = await acquired.client.assert_ownership()
        if not snapshot.stop_visible:
            return False
        await acquired.client.stop()
        return True

    async def open_tab(self, acquired: AcquiredRole) -> None:
        await acquired.client.assert_ownership()
        await action_delay(
            acquired.client.page,
            "open_tab",
            action_delay_multiplier("open_tab"),
        )
        await acquired.client.page.bring_to_front()

    async def preflight_team(self, manifest: Mapping[str, Any]) -> list[Any]:
        team = str(manifest["team"])
        task_id = str(manifest["task_id"])
        assigned_roles = {
            str(record.get("physical_role"))
            for record in manifest.get("roles", {}).values()
            if isinstance(record, Mapping) and record.get("physical_role")
        }
        matches: dict[str, list[Any]] = {role: [] for role in assigned_roles}
        independent = is_independent_task(manifest)
        recorded_page_ids = {
            str(record.get("physical_role")): str(record.get("page_id") or "")
            for record in manifest.get("roles", {}).values()
            if isinstance(record, Mapping) and record.get("physical_role")
        }
        for page in tuple(self.browser_context.pages):
            if page.is_closed() or not self._supported(page):
                continue
            client = ChatGPTPage(page)
            try:
                snapshot = await client.snapshot()
            except Exception as exc:
                raise RoleOwnershipError(
                    "cannot inspect a supported ChatGPT page during exact-team preflight: "
                    f"{getattr(page, 'url', '<unknown>')!r}: {type(exc).__name__}: {exc}"
                ) from exc
            role = str(snapshot.page_role or "")
            if role not in assigned_roles or snapshot.page_team != team:
                continue
            exact_task = snapshot.page_task_id == task_id
            same_recorded_agent = (
                independent
                and bool(recorded_page_ids.get(role))
                and str(snapshot.page_id or "") == recorded_page_ids[role]
            )
            if exact_task or same_recorded_agent:
                matches[role].append(page)
        duplicates = [role for role, pages in matches.items() if len(pages) > 1]
        if duplicates:
            raise RoleOwnershipError(
                f"multiple exact tabs match team {team!r} task {task_id!r}: {duplicates!r}"
            )
        return [pages[0] for pages in matches.values() if pages]

    async def close_team(
        self,
        manifest: Mapping[str, Any],
        *,
        preflighted_pages: Sequence[Any] | None = None,
    ) -> int:
        selected = (
            list(preflighted_pages)
            if preflighted_pages is not None
            else await self.preflight_team(manifest)
        )
        closed = 0
        for page in selected:
            try:
                await action_delay(page, "close_tab", action_delay_multiplier("close_tab"))
                await page.close()
            except Exception as exc:
                if page.is_closed():
                    closed += 1
                raise TeamCloseError(
                    f"failed to close an assigned team tab: {type(exc).__name__}: {exc}",
                    closed_tabs=closed,
                ) from exc
            if not page.is_closed():
                raise TeamCloseError(
                    "assigned team tab did not report closed after page.close()",
                    closed_tabs=closed,
                )
            closed += 1
        return closed
