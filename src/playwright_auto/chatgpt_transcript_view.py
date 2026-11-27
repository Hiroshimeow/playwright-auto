"""Reveal one proven saved user message without changing conversation contents."""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .chatgpt_errors import UnsafePageStateError
from .chatgpt_graph import normalize_visible_text, rendered_user_text_matches


class SavedTranscriptViewError(UnsafePageStateError):
    """The exact saved transcript could not be safely revealed and verified."""


_COLLAPSED_USER_VIEW_TEMPLATE = r"""
(args) => {
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim().replace(/\u0060/g, '');
  const visible = e => {
    if(!e || !e.isConnected) return false;
    const r=e.getBoundingClientRect();
    if(!(r.width>0 && r.height>0)) return false;
    for(let n=e; n; n=n.parentElement) {
      const s=getComputedStyle(n);
      if(n.hasAttribute('inert') || n.getAttribute('aria-hidden')==='true' ||
         s.display==='none' || s.visibility==='hidden' || s.visibility==='collapse' ||
         s.contentVisibility==='hidden' || Number(s.opacity)===0) return false;
    }
    return true;
  };
  const blocked = reason => ({status:'blocked',reason});
  const chatRoot=(__ACTIVE_CHAT_ROOT__);
  const mains=[...document.querySelectorAll('main')].filter(visible);
  if(document.readyState!=='complete' || location.href!==args.document_url ||
     mains.length!==1 || mains[0]!==chatRoot) return blocked('document_identity_changed');
  try {
    for(const [field,key] of [
      ['page_id','playwright-auto:page-id'], ['page_role','playwright-auto:role'],
      ['page_task_id','playwright-auto:task-id'], ['page_team','playwright-auto:team']
    ]) if(sessionStorage.getItem(key)!==args.owner[field]) return blocked('page_owner_changed');
  } catch(_) { return blocked('page_owner_unreadable'); }
  if(window._cf_chl_opt || new URL(location.href).searchParams.has('__cf_chl_rt_tk') ||
     document.querySelector('#challenge-form,#challenge-running,#challenge-stage'))
    return blocked('provider_challenge');
  const composerOwner=(__COMPOSER_RESOLVER__)();
  const composer=composerOwner.element;
  if(!composer || composerOwner.ambiguous ||
     (__COMPOSER_TEXT__)(composer).trim()) return blocked('composer_not_clean');
  const composerHost=composer.closest('form') || composer.parentElement;
  if([...composerHost.querySelectorAll(
    '[data-filename],[data-file-name],[data-testid*="attachment"],' +
    '[aria-label^="Remove file"],[aria-label^="Remove attachment"]'
  )].some(visible)) return blocked('attachment_present');
  if([...document.querySelectorAll(
    '[data-testid="login-button"],[role="alert"],[role="dialog"],[data-testid^="modal-"]'
  )].some(e=>e.id!=='playwright-auto-role-control-v1' && visible(e)))
    return blocked('page_not_clean');
  if([...chatRoot.querySelectorAll(
    'button[data-testid="stop-button"],button[aria-label*="Stop"],' +
    '[data-streaming-response-status="streaming"],[data-streaming-response-status="running"]'
  )].some(visible)) return blocked('response_active');
  __MCP_PERMISSION_SURFACE__
  if(cdpaMcpSurfaceKey || cdpaMcpNodeCount || cdpaMcpAllowCount)
    return blocked('permission_pending');
  const projection=(__RESPONSE_PROJECTION__);
  if(projection.active) return blocked('response_active');
  __CONNECTION_INTERRUPTION__
  if(connectionInterruptedVisible) return blocked('connection_interrupted');

  const units=[...chatRoot.querySelectorAll('[data-chatgpt-search-unit-key$=":user"]')]
    .filter(e=>String(e.getAttribute('data-chatgpt-search-message-ids')||'')
      .trim().split(/\s+/).includes(args.user_id));
  if(units.length===0) return {status:'not_collapsed'};
  if(units.length!==1) return blocked('ambiguous_user_unit');
  const unit=units[0], ids=String(unit.getAttribute('data-chatgpt-search-message-ids')||'')
    .trim().split(/\s+/);
  const group=unit.closest('[data-turn-key]');
  if(!visible(unit) || ids.length!==1 || ids[0]!==args.user_id ||
     !group || group.getAttribute('data-turn-key')!==args.user_id)
    return blocked('user_unit_identity_changed');
  const bubbles=[...unit.querySelectorAll('[data-user-message-bubble][data-user-message-has-prompt]')];
  if(bubbles.length!==1) return {status:'not_collapsed'};
  const bubble=bubbles[0];
  const buttons=[...unit.querySelectorAll('button')].filter(b=>normalize(b.innerText)==='Show more');
  if(buttons.length===0) return {status:'not_collapsed'};
  if(buttons.length!==1) return blocked('ambiguous_show_more_control');
  const button=buttons[0];
  if(!bubble.contains(button) || !visible(button) || button.disabled ||
     button.getAttribute('aria-disabled')==='true' || button.getAttribute('type')!=='button' ||
     button.getAttribute('aria-expanded')!=='false' ||
     !button.hasAttribute('data-thread-find-skip') || !button.hasAttribute('data-markdown-copy'))
    return blocked('unclassified_show_more_control');
  const bodies=[...bubble.querySelectorAll('[data-search-result-target]')];
  if(bodies.length!==1 || !visible(bodies[0])) return blocked('ambiguous_prompt_body');
  const body=normalize(bodies[0].innerText);
  const expected=normalize(args.expected_prompt);
  if(!body || body!==args.visible_prefix || body.length>=expected.length ||
     !expected.startsWith(body)) return blocked('collapsed_body_not_exact_saved_prefix');
  if(!args.click) return {status:'eligible'};
  // Revalidated in this one DOM boundary. This is a view-only button, never Send.
  button.click();
  return {status:'clicked'};
}
"""


def _collapsed_visible_prefix(text: str) -> str:
    value = normalize_visible_text(text).replace(chr(96), "")
    if value.endswith(" Show more"):
        value = value[:-len(" Show more")].rstrip()
        if value.endswith(" …"):
            value = value[:-2].rstrip()
    return value


def _validate_snapshot(
    snapshot: Any, *, owner: Mapping[str, str], canonical: str, user_id: str,
) -> Any | None:
    from .cdpa_conversations import canonical_conversation

    if any(getattr(snapshot, field) != value for field, value in owner.items()):
        raise SavedTranscriptViewError("The saved transcript page owner changed")
    try:
        same_conversation = canonical_conversation(snapshot.url) == canonical
    except ValueError:
        same_conversation = False
    if not same_conversation or snapshot.session_id != canonical:
        raise SavedTranscriptViewError("The saved transcript conversation changed")
    if (
        snapshot.state.value != "waiting_prompt" or snapshot.requires_login
        or not snapshot.composer_present or not snapshot.composer_editable
        or snapshot.composer_ambiguous or snapshot.manual_input_pending
        or snapshot.choice_prompt_pending or snapshot.choice_prompt_labels
        or snapshot.blocking_dialogs or snapshot.error_texts or snapshot.retry_visible
        or snapshot.stop_visible or snapshot.response_activity_turn_id
        or snapshot.response_projection_active or snapshot.connection_interrupted_visible
        or snapshot.mcp_permission_allow_count or snapshot.mcp_permission_node_count
        or snapshot.mcp_permission_surface_key or snapshot.mcp_provider_risk
    ):
        raise SavedTranscriptViewError("The saved transcript page is not clean and idle")
    users = [message for message in snapshot.messages if message.role == "user"]
    matches = [message for message in users if message.message_id == user_id]
    if len(matches) > 1 or (matches and users[-1].message_id != user_id):
        raise SavedTranscriptViewError("The saved user message identity is ambiguous")
    return matches[0] if matches else None


async def expand_saved_user_message(
    client: Any,
    *,
    expected_user_id: str,
    expected_prompt: str,
    expected_owner: Mapping[str, str],
    canonical: str,
    identity_guard: Callable[[], Awaitable[None]],
    timeout_seconds: float,
) -> Any | None:
    """Expand at most one exact accepted user bubble, then prove full equality.

    The caller pins the endpoint, physical browser and exact CDP target in
    identity_guard. Its durable receipt remains authoritative. The returned
    snapshot is evidence only; this function never restores a writer or sends.
    None means no qualifying collapsed control was present. Cancellation or
    uncertainty after a view click propagates; it never triggers another click.
    """
    from .chatgpt import _CONNECTION_INTERRUPTION_JS, _MCP_PERMISSION_SURFACE_JS
    from .chatgpt_composer import COMPOSER_RESOLVER_JS, COMPOSER_TEXT_JS
    from .chatgpt_response_projection import ACTIVE_CHAT_ROOT_JS, RESPONSE_PROJECTION_JS

    owner_fields = {"page_id", "page_role", "page_task_id", "page_team"}
    if (
        set(expected_owner) != owner_fields
        or not all(isinstance(v, str) and v for v in expected_owner.values())
        or not isinstance(expected_user_id, str) or not expected_user_id
        or not isinstance(expected_prompt, str) or not expected_prompt
        or not isinstance(canonical, str) or not canonical
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        raise SavedTranscriptViewError("Exact saved transcript context is required")
    owner = dict(expected_owner)
    deadline = time.monotonic() + timeout_seconds
    script = (_COLLAPSED_USER_VIEW_TEMPLATE
              .replace("__ACTIVE_CHAT_ROOT__", ACTIVE_CHAT_ROOT_JS)
              .replace("__COMPOSER_RESOLVER__", COMPOSER_RESOLVER_JS)
              .replace("__COMPOSER_TEXT__", COMPOSER_TEXT_JS)
              .replace("__MCP_PERMISSION_SURFACE__", _MCP_PERMISSION_SURFACE_JS)
              .replace("__RESPONSE_PROJECTION__", RESPONSE_PROJECTION_JS)
              .replace("__CONNECTION_INTERRUPTION__", _CONNECTION_INTERRUPTION_JS))

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise SavedTranscriptViewError("Saved transcript view verification timed out")
        return value

    async def guard() -> None:
        remaining()
        if await identity_guard() is not None:
            raise SavedTranscriptViewError("The physical page identity guard did not verify")
        remaining()

    async def observe() -> tuple[Any, Any | None]:
        await guard()
        snapshot = await client.snapshot()
        await guard()
        return snapshot, _validate_snapshot(
            snapshot, owner=owner, canonical=canonical, user_id=expected_user_id,
        )

    try:
        async with asyncio.timeout(timeout_seconds):
            async with client.mutation_guard():
                snapshot, message = await observe()
                if message is None:
                    return None
                if rendered_user_text_matches(message.text, expected_prompt):
                    return snapshot
                payload = {
                    "document_url": snapshot.url, "owner": owner,
                    "user_id": expected_user_id, "expected_prompt": expected_prompt,
                    "visible_prefix": _collapsed_visible_prefix(message.text), "click": False,
                }
                probe = await client.page.evaluate(script, payload)
                remaining()
                if probe.get("status") == "not_collapsed":
                    return None
                if probe.get("status") != "eligible":
                    raise SavedTranscriptViewError(
                        "The saved prompt cannot be expanded: " + str(probe.get("reason", "unknown"))
                    )
                # Repeat clean native and outer identity checks before the atomic DOM click.
                snapshot, message = await observe()
                if message is None:
                    raise SavedTranscriptViewError("The saved user message disappeared")
                if rendered_user_text_matches(message.text, expected_prompt):
                    return snapshot
                payload.update(
                    document_url=snapshot.url,
                    visible_prefix=_collapsed_visible_prefix(message.text), click=True,
                )
                result = await client.page.evaluate(script, payload)
                remaining()
                if result.get("status") != "clicked":
                    raise SavedTranscriptViewError(
                        "The saved prompt changed before view expansion: "
                        + str(result.get("reason", result.get("status", "unknown")))
                    )
                while True:
                    snapshot, message = await observe()
                    if message is None:
                        raise SavedTranscriptViewError("The expanded saved message disappeared")
                    if rendered_user_text_matches(message.text, expected_prompt):
                        return snapshot
                    await asyncio.sleep(min(0.05, remaining()))
    except TimeoutError as exc:
        raise SavedTranscriptViewError("Saved transcript view verification timed out") from exc
