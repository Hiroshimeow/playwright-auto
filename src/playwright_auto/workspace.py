from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .chatgpt import ChatGPTPage, PageBinding, validate_page_role


class WorkspaceBindingError(RuntimeError):
    pass


class RouteValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RoleBinding:
    role: str
    page_id: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "page_id": self.page_id, "url": self.url}


class ChatGPTWorkspace:
    """Immutable logical-role to physical-tab registry."""

    def __init__(self) -> None:
        self._clients: dict[str, ChatGPTPage] = {}
        self._page_ids: dict[str, str] = {}

    @property
    def active_roles(self) -> tuple[str, ...]:
        return tuple(self._clients)

    def get(self, role: str) -> ChatGPTPage:
        role = validate_page_role(role)
        try:
            return self._clients[role]
        except KeyError as exc:
            raise WorkspaceBindingError(
                f"logical role {role!r} is not bound; active roles={list(self.active_roles)!r}"
            ) from exc

    async def bind(
        self,
        role: str,
        page: Any,
        *,
        timeout_ms: int = 15_000,
        allow_rebind: bool = False,
        force_new_page_id: bool = False,
    ) -> ChatGPTPage:
        role = validate_page_role(role)
        existing = self._clients.get(role)
        if existing is not None and existing.page is not page and not allow_rebind:
            raise WorkspaceBindingError(
                f"role {role!r} is already bound to another physical tab"
            )
        for bound_role, bound_client in self._clients.items():
            if bound_client.page is page and bound_role != role:
                raise WorkspaceBindingError(
                    f"physical tab is already bound to role {bound_role!r}"
                )

        client = existing if existing is not None and existing.page is page else ChatGPTPage(
            page, timeout_ms=timeout_ms
        )
        preflight = await client.snapshot()
        if preflight.page_id:
            other_role = self._page_ids.get(preflight.page_id)
            if other_role is not None:
                bound_client = self._clients[other_role]
                if bound_client.page is page and other_role != role:
                    raise WorkspaceBindingError(
                        f"physical page_id {preflight.page_id!r} is already bound to role {other_role!r}"
                    )
                if bound_client.page is not page:
                    # A duplicated browser tab can inherit sessionStorage. Give the
                    # new physical tab a new identity before registering it.
                    force_new_page_id = True
        assigned = await client.set_role(
            role,
            allow_rebind=allow_rebind,
            force_new_page_id=force_new_page_id,
        )
        page_id = assigned["page_id"]
        other_role = self._page_ids.get(page_id)
        if other_role is not None and other_role != role:
            raise WorkspaceBindingError(
                f"physical page_id {page_id!r} is already bound to role {other_role!r}"
            )

        if existing is not None and existing.page is not page and allow_rebind:
            assert existing.binding is not None
            self._page_ids.pop(existing.binding.page_id, None)
        self._clients[role] = client
        self._page_ids[page_id] = role
        return client

    async def open_role(
        self,
        browser_context: Any,
        role: str,
        *,
        url: str = "https://chatgpt.com/",
        timeout_ms: int = 15_000,
        force_new_page_id: bool = False,
    ) -> ChatGPTPage:
        if role in self._clients:
            raise WorkspaceBindingError(f"role {role!r} is already open")
        page = await browser_context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.locator(
                '[contenteditable="true"][role="textbox"]'
            ).first.wait_for(state="visible", timeout=timeout_ms)
            return await self.bind(
                role,
                page,
                timeout_ms=timeout_ms,
                force_new_page_id=force_new_page_id,
            )
        except Exception:
            await page.close()
            raise

    async def attach_existing(
        self,
        pages: Iterable[Any],
        *,
        allowed_roles: Iterable[str] | None = None,
        timeout_ms: int = 15_000,
    ) -> tuple[RoleBinding, ...]:
        allowed = (
            {validate_page_role(role) for role in allowed_roles}
            if allowed_roles is not None
            else None
        )
        attached: list[RoleBinding] = []
        for page in pages:
            client = ChatGPTPage(page, timeout_ms=timeout_ms)
            snapshot = await client.snapshot()
            if not snapshot.page_role or not snapshot.page_id:
                continue
            role = validate_page_role(snapshot.page_role)
            if allowed is not None and role not in allowed:
                continue
            if role in self._clients:
                raise WorkspaceBindingError(
                    f"duplicate existing logical role {role!r} across tabs"
                )
            if snapshot.page_id in self._page_ids:
                raise WorkspaceBindingError(
                    f"duplicate physical page_id {snapshot.page_id!r} across tabs"
                )
            client.binding = PageBinding(snapshot.page_id, role)
            await client.assert_ownership(snapshot)
            self._clients[role] = client
            self._page_ids[snapshot.page_id] = role
            attached.append(RoleBinding(role, snapshot.page_id, snapshot.url))
        return tuple(attached)

    async def bindings(self) -> tuple[RoleBinding, ...]:
        result: list[RoleBinding] = []
        for role, client in self._clients.items():
            snapshot = await client.assert_ownership()
            assert client.binding is not None
            result.append(RoleBinding(role, client.binding.page_id, snapshot.url))
        return tuple(result)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _json_object_without_duplicate_keys(source: str) -> dict[str, Any]:
    def build(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RouteValidationError(f"duplicate route target {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(source, object_pairs_hook=build)
    except json.JSONDecodeError as exc:
        raise RouteValidationError(f"invalid route JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise RouteValidationError("route JSON must be an object")
    return value


def parse_route_map(text: str, active_roles: Iterable[str]) -> dict[str, str]:
    """Parse an exact JSON role->prompt map and fail closed on unknown roles."""
    source = text.strip()
    matches = list(_JSON_FENCE.finditer(source))
    if matches:
        if len(matches) != 1:
            raise RouteValidationError("route response must contain exactly one JSON fence")
        match = matches[0]
        surrounding = (source[: match.start()] + source[match.end() :]).strip()
        if surrounding:
            raise RouteValidationError("route JSON fence must not have surrounding prose")
        source = match.group(1).strip()
    elif not (source.startswith("{") and source.endswith("}")):
        raise RouteValidationError("route response must be a JSON object or one JSON fence")

    value = _json_object_without_duplicate_keys(source)
    if not value:
        raise RouteValidationError("route JSON must be a non-empty object")

    allowed = {validate_page_role(role) for role in active_roles}
    if not allowed:
        raise RouteValidationError("no active roles are available for routing")
    result: dict[str, str] = {}
    for raw_role, raw_prompt in value.items():
        if not isinstance(raw_role, str):
            raise RouteValidationError("route keys must be strings")
        role = validate_page_role(raw_role)
        if role not in allowed:
            raise RouteValidationError(
                f"unknown route target {role!r}; active roles={sorted(allowed)!r}"
            )
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            raise RouteValidationError(f"route prompt for {role!r} must be non-empty text")
        result[role] = raw_prompt.strip()
    return result


RepairCallback = Callable[[str, str], str | Awaitable[str]]
