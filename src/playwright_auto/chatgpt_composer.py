"""Single composer ownership semantics shared by edit, DOM inspection, and Send."""

from __future__ import annotations

import asyncio
from typing import Any

from .chatgpt_errors import ComposerConflictError
from .chatgpt_graph import visible_text_matches

COMPOSER_RESOLVER_JS = r"""() => {
  const eligible = element => {
    const style = getComputedStyle(element), rect = element.getBoundingClientRect();
    return element.isConnected && element.isContentEditable &&
      element.getAttribute('aria-disabled') !== 'true' && !element.closest('[inert]') &&
      !element.closest('[data-message-author-role], [data-turn-id]') &&
      style.visibility !== 'hidden' && style.visibility !== 'collapse' &&
      style.display !== 'none' && style.pointerEvents !== 'none' &&
      rect.width > 0 && rect.height > 0;
  };
  const tiers = [
    ['owned', 'div#prompt-textarea[contenteditable="true"][role="textbox"], [data-testid="composer"] [contenteditable="true"][role="textbox"]'],
    ['form', 'form:has(button[data-testid="send-button"], button[aria-label="Send prompt"]) [contenteditable="true"][role="textbox"]'],
    ['generic', '[contenteditable="true"][role="textbox"]'],
  ];
  for (const [source, selector] of tiers) {
    const nodes = [...document.querySelectorAll(selector)].filter(eligible);
    if (nodes.length) {
      return {
        element: nodes.length === 1 ? nodes[0] : null,
        ambiguous: nodes.length > 1,
        source,
        candidate_count: nodes.length,
      };
    }
  }
  return {element:null, ambiguous:false, source:'none', candidate_count:0};
}"""


COMPOSER_REPLACE_TEXT_JS = r"""targetText => {
  const resolve = __CDPA_COMPOSER_RESOLVER__;
  const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
  const result = (owner, status, text = null) => ({
    status,
    text,
    owner_source: owner.source || 'none',
    candidate_count: Number(owner.candidate_count || 0),
  });
  const owner = resolve();
  if (owner.ambiguous) return result(owner, 'ambiguous');
  const element = owner.element;
  if (!element) return result(owner, 'missing');
  const before = normalize(element.innerText);
  element.focus();
  const afterFocus = resolve();
  if (!element.isConnected || afterFocus.element !== element) {
    return result(afterFocus, 'owner_changed');
  }
  if (document.activeElement !== element && !element.contains(document.activeElement)) {
    return result(afterFocus, 'focus_stolen', before);
  }
  if (normalize(element.innerText) !== before) {
    return result(afterFocus, 'content_changed', normalize(element.innerText));
  }
  const expected = normalize(targetText);
  if (before === expected) return result(afterFocus, 'unchanged', before);
  const selection = document.getSelection(), range = document.createRange();
  range.selectNodeContents(element);
  selection.removeAllRanges();
  selection.addRange(range);
  // Native editing emits the editor's input events. Resolution, focus validation,
  // mutation and readback stay in one callback, so no later keyboard event can
  // be redirected to another editable node.
  const edited = document.execCommand(targetText ? 'insertText' : 'delete', false, targetText);
  return result(afterFocus, edited ? 'edited' : 'unsupported', normalize(element.innerText));
}""".replace('__CDPA_COMPOSER_RESOLVER__', COMPOSER_RESOLVER_JS)


async def replace_owned_composer_text(
    page: Any, text: str, *, timeout_ms: int
) -> None:
    """Atomically resolve, focus, edit and verify the one owned composer."""
    last_state = "status=unobserved source=unknown candidates=unknown"
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            mutation_retry_used = False
            while True:
                result = await page.evaluate(COMPOSER_REPLACE_TEXT_JS, text)
                status = str((result or {}).get("status") or "")
                actual = str((result or {}).get("text") or "")
                source = str((result or {}).get("owner_source") or "unknown")
                candidate_count = int((result or {}).get("candidate_count") or 0)
                last_state = (
                    f"status={status or 'unknown'} source={source} "
                    f"candidates={candidate_count}"
                )
                if status in {"edited", "unchanged"}:
                    if visible_text_matches(actual, text):
                        return
                    if not mutation_retry_used and not actual.strip():
                        mutation_retry_used = True
                        await asyncio.sleep(0)
                        continue
                    raise ComposerConflictError(
                        "composer text changed after native edit; refusing to replay "
                        f"({last_state})"
                    )
                if status == "missing":
                    await asyncio.sleep(0.05)
                    continue
                if status in {"owner_changed", "focus_stolen", "content_changed"}:
                    if not mutation_retry_used:
                        mutation_retry_used = True
                        await asyncio.sleep(0)
                        continue
                    detail = {
                        "owner_changed": "composer owner changed before edit",
                        "focus_stolen": "composer focus was stolen before edit",
                        "content_changed": "composer content changed before edit",
                    }[status]
                    raise ComposerConflictError(f"{detail} ({last_state})")
                if status == "ambiguous":
                    raise ComposerConflictError(
                        f"multiple eligible live composer owners ({last_state})"
                    )
                if status == "unsupported":
                    raise ComposerConflictError(
                        f"browser refused native composer edit ({last_state})"
                    )
                raise ComposerConflictError(
                    f"composer edit produced unexpected state ({last_state})"
                )
    except TimeoutError as exc:
        raise TimeoutError(
            f"composer fill timed out at composer_edit ({last_state})"
        ) from exc
