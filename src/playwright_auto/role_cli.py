from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlparse

from .chatgpt import assign_page_role, inspect_chatgpt_page, validate_page_role
from .connection import connected_browser

CHATGPT_HOSTS = frozenset({"chatgpt.com", "www.chatgpt.com"})


class RoleSelectionRequired(ValueError):
    pass


@dataclass(frozen=True)
class TabCandidate:
    index: int
    title: str
    url: str
    current_role: str | None
    page_id: str | None


@dataclass(frozen=True)
class RoleAssignment:
    tab: TabCandidate
    role: str


def build_assignment_plan(
    tabs: Sequence[TabCandidate],
    roles: Sequence[str],
    *,
    selected_indices: Sequence[int] | None = None,
) -> list[RoleAssignment]:
    normalized_roles = [validate_page_role(role) for role in roles]
    if not normalized_roles:
        raise ValueError("at least one role is required")
    if len(set(normalized_roles)) != len(normalized_roles):
        raise ValueError("role names must be unique")
    if len(tabs) < len(normalized_roles):
        raise ValueError(
            f"only {len(tabs)} ChatGPT tabs are open for {len(normalized_roles)} roles"
        )

    if selected_indices is None:
        if len(tabs) != len(normalized_roles):
            raise RoleSelectionRequired(
                f"{len(tabs)} ChatGPT tabs are open for {len(normalized_roles)} roles; "
                "select tabs interactively or pass --tabs"
            )
        selected_indices = [tab.index for tab in tabs]

    selected = list(selected_indices)
    if len(selected) != len(normalized_roles):
        raise ValueError(
            f"--tabs must contain exactly {len(normalized_roles)} tab indices"
        )
    if len(set(selected)) != len(selected):
        raise ValueError("the same tab was selected more than once")

    by_index = {tab.index: tab for tab in tabs}
    missing = [index for index in selected if index not in by_index]
    if missing:
        raise ValueError(f"unknown ChatGPT tab index: {missing[0]}")

    return [
        RoleAssignment(tab=by_index[index], role=role)
        for index, role in zip(selected, normalized_roles, strict=True)
    ]


def parse_tab_indices(value: str) -> list[int]:
    try:
        values = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise ValueError("--tabs must be a comma-separated list such as 1,3") from exc
    if not values:
        raise ValueError("--tabs must not be empty")
    return values


def format_tab(tab: TabCandidate) -> str:
    role = tab.current_role or "unassigned"
    page_id = (tab.page_id or "-")[:8]
    title = " ".join(tab.title.split()) or "ChatGPT"
    return f"[{tab.index}] role={role:<12} page={page_id:<8} title={title}"


def prompt_for_tab_indices(
    tabs: Sequence[TabCandidate],
    roles: Sequence[str],
    *,
    input_fn=input,
    output_fn=print,
) -> list[int]:
    available = {tab.index for tab in tabs}
    selected: list[int] = []
    for role in roles:
        while True:
            raw = input_fn(f"Select tab for {role}: ").strip()
            try:
                index = int(raw)
            except ValueError:
                output_fn("Enter one tab number from the list above.")
                continue
            if index not in available:
                output_fn(f"Tab {index} is not a ChatGPT tab in this list.")
                continue
            if index in selected:
                output_fn(f"Tab {index} was already selected for another role.")
                continue
            selected.append(index)
            break
    return selected


async def collect_chatgpt_tabs(browser: Any) -> list[tuple[Any, TabCandidate]]:
    rows: list[tuple[Any, TabCandidate]] = []
    for context in browser.contexts:
        for page in context.pages:
            hostname = (urlparse(page.url).hostname or "").lower()
            if hostname not in CHATGPT_HOSTS or page.is_closed():
                continue
            title = await page.title()
            current_role: str | None = None
            page_id: str | None = None
            try:
                snapshot = await inspect_chatgpt_page(page)
                current_role = snapshot.page_role
                page_id = snapshot.page_id
            except Exception:
                # Role assignment itself will report a concrete page error if selected.
                pass
            candidate = TabCandidate(
                index=len(rows),
                title=title,
                url=page.url,
                current_role=current_role,
                page_id=page_id,
            )
            rows.append((page, candidate))
    return rows


async def async_main(args: argparse.Namespace) -> int:
    async with connected_browser(args.cdp) as browser:
        rows = await collect_chatgpt_tabs(browser)
        candidates = [candidate for _, candidate in rows]
        if not candidates:
            raise RuntimeError("no open ChatGPT tabs were found on the CDP browser")

        print("Open ChatGPT tabs:")
        for candidate in candidates:
            print("  " + format_tab(candidate))

        if args.list or not args.roles:
            if not args.roles:
                print("\nAssign roles with: playwright-roles REVIEW REVIEW1")
            return 0

        selected = parse_tab_indices(args.tabs) if args.tabs else None
        if selected is None and len(candidates) != len(args.roles):
            if not sys.stdin.isatty():
                raise RoleSelectionRequired(
                    f"{len(candidates)} ChatGPT tabs are open for {len(args.roles)} roles; "
                    "rerun with --tabs INDEX,INDEX"
                )
            selected = prompt_for_tab_indices(candidates, args.roles)

        plan = build_assignment_plan(
            candidates,
            args.roles,
            selected_indices=selected,
        )
        page_by_index = {candidate.index: page for page, candidate in rows}
        print("\nRole changes:")
        for assignment in plan:
            result = await assign_page_role(
                page_by_index[assignment.tab.index],
                assignment.role,
            )
            previous = assignment.tab.current_role or "unassigned"
            print(
                f"  [{assignment.tab.index}] {previous} -> {assignment.role} "
                f"(page {result['page_id'][:8]})"
            )
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Assign arbitrary roles to ChatGPT tabs already open on CDP 9222"
    )
    parser.add_argument("roles", nargs="*", metavar="ROLE")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument(
        "--tabs",
        metavar="INDEX,INDEX",
        help="non-interactive tab selection using indices printed by --list",
    )
    parser.add_argument("--list", action="store_true", help="list ChatGPT tabs only")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(async_main(args))
    except (RoleSelectionRequired, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
