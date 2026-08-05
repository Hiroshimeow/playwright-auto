from __future__ import annotations

import warnings
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .cdpa_browser_projection import inspect_page_metadata
from .cdpa_config import CDPAConfig
from .cdpa_independent import is_independent_task
from .chatgpt import (
    ChatGPTPage,
    PageBinding,
    action_delay,
    action_delay_multiplier,
    backend_conversation as read_backend_conversation,
    backend_stream_status as read_backend_stream_status,
    random_delay,
    refresh_page,
)
from .workspace import ChatGPTWorkspace

_CHATGPT_HOSTS = frozenset({"chatgpt.com", "www.chatgpt.com"})


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


class CDPATabActions:
    def __init__(self, browser_context: Any, config: CDPAConfig) -> None:
        self.browser_context = browser_context
        self.config = config
        self._lifecycle_warning_emitted = False

    async def backend_stream_status(self, conversation_id: str) -> dict[str, Any]:
        return await read_backend_stream_status(self.browser_context, conversation_id)

    async def backend_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await read_backend_conversation(self.browser_context, conversation_id)

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

    async def acquire_global_role(self, physical_role: str) -> AcquiredRole:
        """Reuse exactly one role-only tab or lazily open it without task ownership."""
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

    async def refresh(self, acquired: AcquiredRole) -> None:
        await acquired.client.assert_ownership()
        await action_delay(
            acquired.client.page,
            "refresh",
            action_delay_multiplier("refresh"),
        )
        await refresh_page(
            acquired.client.page,
            timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
        )
        await acquired.client.assert_ownership()

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
