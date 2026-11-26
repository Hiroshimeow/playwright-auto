from __future__ import annotations

# ChatGPT may retain an offscreen zero-size <main> during history navigation.
# Both the sparse wait probe and full DOM projection must read the same live main.
ACTIVE_CHAT_ROOT_JS = r"""
(() => {
  const mains = [...document.querySelectorAll('main')];
  const live = mains.filter((main) => {
    const rect = main.getBoundingClientRect();
    const style = window.getComputedStyle(main);
    return main.isConnected && !main.closest('[inert],[aria-hidden="true"]') &&
      style.display !== 'none' && style.visibility !== 'hidden' &&
      style.visibility !== 'collapse' && rect.width > 0 && rect.height > 0;
  });
  return live.at(-1) || (mains.length ? document.createElement('main') : document.body);
})()
"""

RESPONSE_PROJECTION_JS = r"""
(() => {
  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const isVisible = (element) => Boolean(
    element && window.getComputedStyle(element).visibility !== 'hidden' &&
    (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
  );
  const turnId = (element) => {
    const turn = element?.closest('[data-turn-id]');
    if (turn?.getAttribute('data-turn-id')) return turn.getAttribute('data-turn-id');
    const keyed = element?.closest('[data-turn-key]');
    if (keyed?.getAttribute('data-turn-key')) return `group:assistant:${keyed.getAttribute('data-turn-key')}`;
    return element?.closest('[data-turn-id-container]')?.getAttribute('data-turn-id-container') || null;
  };
  const latestUser = [...chatRoot.querySelectorAll(
    '[data-message-author-role="user"], [data-chatgpt-search-unit-key$=":user"]'
  )].filter(isVisible).at(-1) || null;
  const userTurn = latestUser?.closest('[data-turn-id],[data-turn-key],[data-turn-id-container]') || latestUser;
  const roots = [...chatRoot.querySelectorAll('[data-turn-id],[data-turn-key],[data-turn-id-container]')]
    .filter(isVisible)
    .filter((root) => !userTurn || root === userTurn ||
      Boolean(userTurn.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING));
  const root = roots.filter((candidate) => candidate.querySelector(
    '[data-message-author-role="assistant"], [data-chatgpt-search-unit-key$=":assistant"], [data-streaming-response-status], [data-testid*="tool"], [data-testid*="status"], [data-testid*="thought"], .markdown'
  )).at(-1) || null;
  if (!root) return {turn_id: null, segments: [], active: false};

  const selector = [
    '[data-streaming-response-status]',
    '[data-message-author-role="assistant"]',
    '[data-chatgpt-search-unit-key$=":assistant"]',
    '[data-testid*="tool"]',
    '[data-testid*="status"]',
    '[data-testid*="thought"]',
    '.markdown'
  ].join(',');
  const candidates = [...root.querySelectorAll(selector)].filter(isVisible);
  const segments = [];
  let total = 0;
  const seen = new Set();
  for (const element of candidates) {
    if (segments.length >= 24 || total >= 24000) break;
    if (candidates.some((other) => other !== element && element.contains(other) && normalize(other.innerText || other.textContent))) {
      continue;
    }
    const value = normalize(element.innerText || element.textContent);
    if (!value) continue;
    const testid = normalize(element.getAttribute('data-testid')).toLowerCase();
    const kind = element.matches('[data-message-author-role="assistant"],[data-chatgpt-search-unit-key$=":assistant"],.markdown')
      ? 'answer'
      : testid.includes('tool') ? 'tool'
      : testid.includes('thought') ? 'reasoning'
      : 'status';
    const ordinal = segments.filter((item) => item.kind === kind).length;
    const key = `projection:${turnId(root) || 'current'}:${kind}:${ordinal}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const text = value.slice(0, Math.max(0, 24000 - total));
    total += text.length;
    const streamState = normalize(element.getAttribute('data-streaming-response-status')).toLowerCase();
    const state = normalize(element.getAttribute('data-state')).toLowerCase();
    const ariaBusy = normalize(element.getAttribute('aria-busy')).toLowerCase();
    const completed = /^(complete|completed|done|finished|closed|idle)$/.test(streamState) ||
      /^(complete|completed|done|finished|closed)$/.test(state) ||
      ariaBusy === 'false';
    const active = kind !== 'answer' && !completed && (
      (element.matches('[data-streaming-response-status]') && !streamState) ||
      /^(streaming|running|working|thinking|searching|calling|loading|open|pending|active)$/.test(streamState) ||
      ariaBusy === 'true' ||
      state === 'open' ||
      /running|working|thinking|searching|calling|loading/i.test(testid)
    );
    segments.push({key, kind, text, active});
  }
  return {
    turn_id: turnId(root),
    segments,
    active: segments.some((item) => item.active)
  };
})()
"""

RETRY_RESOLVER_JS = r"""
(() => {
  const visible = (element) => Boolean(
    element && window.getComputedStyle(element).visibility !== 'hidden' &&
    (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
  );
  const latestUser = [...chatRoot.querySelectorAll(
    '[data-message-author-role="user"], [data-chatgpt-search-unit-key$=":user"]'
  )].filter(visible).at(-1) || null;
  const userTurn = latestUser?.closest('[data-turn-id],[data-turn-key],[data-turn-id-container]') || latestUser;
  const exact = [...chatRoot.querySelectorAll('[data-testid="regenerate-thread-error-button"]')]
    .find((element) => {
      if (!visible(element) || element.disabled || element.getAttribute('aria-disabled') === 'true') return false;
      const root = element.closest('[data-turn-id],[data-turn-key],[data-turn-id-container]');
      if (!root || !root.querySelector(
        '[data-message-author-role="assistant"],[data-chatgpt-search-unit-key$=":assistant"],[data-streaming-response-status]'
      )) return false;
      return !userTurn || root === userTurn ||
        Boolean(userTurn.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING);
    });
  if (exact) return exact;
  const matches = [...chatRoot.querySelectorAll('button,[role="button"]')].filter((element) => {
    if (!visible(element) || element.disabled || element.getAttribute('aria-disabled') === 'true') return false;
    if (String(element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim() !== 'Retry') return false;
    const root = element.closest('[data-turn-id],[data-turn-key],[data-turn-id-container]');
    if (!root || !root.querySelector(
      '[data-message-author-role="assistant"],[data-chatgpt-search-unit-key$=":assistant"],[data-streaming-response-status]'
    )) return false;
    return !userTurn || root === userTurn ||
      Boolean(userTurn.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING);
  });
  return matches.length === 1 ? matches[0] : null;
})()
"""


BLANK_TURN_RETRY_MENU_JS = r"""
([storage, owner, url, userId, userTurnId, assistantId, assistantTurnId, triggerId, editPrompt]) => {
  if (location.href !== url || storage.some((key, i) => sessionStorage.getItem(key) !== owner[i])) {
    return {reason: 'ownership_changed'};
  }
  const visible = element => Boolean(element?.getClientRects().length &&
    getComputedStyle(element).visibility !== 'hidden');
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
  const hasId = (element, id) => id && normalize(element.getAttribute('data-chatgpt-search-message-ids'))
    .split(' ').includes(id);
  const composer = (__CDPA_COMPOSER_RESOLVER__)().element;
  const composerHost = composer?.closest('form') || composer?.parentElement;
  if (!composer || normalize(composer.innerText) || [...composerHost.querySelectorAll(
    '[data-filename], [data-file-name], [data-testid*=attachment], [data-testid*=file]'
  )].some(visible)) return {reason: 'composer_conflict'};
  if ([...document.querySelectorAll(
    'button[data-testid="stop-button"], main button[aria-label*="Stop"], ' +
    '[role="dialog"]:not(#playwright-auto-role-control-v1), [data-testid^="modal-"]'
  )].some(visible)) return {reason: 'page_busy'};

  if (editPrompt !== null && triggerId) {
    const group = [...document.querySelectorAll('main [data-turn-key]')].at(-1);
    if (group?.getAttribute('data-turn-key') !== triggerId) return {reason: 'edit_turn_changed'};
    const editors = [...group.querySelectorAll('textarea,[contenteditable="true"]')].filter(visible);
    const editor = editors[0];
    if (editors.length !== 1 || normalize(editor.value ?? editor.innerText) !== normalize(editPrompt)) {
      return {reason: 'repair_editor_content_changed'};
    }
    const send = [...group.querySelectorAll('button')].filter(e =>
      visible(e) && !e.disabled && e.getAttribute('aria-disabled') !== 'true' &&
      [normalize(e.innerText), normalize(e.getAttribute('aria-label'))].includes('Send'));
    if (send.length !== 1) return {reason: 'repair_editor_send_unavailable'};
    send[0].click();
    return {clicked: true};
  }

  const user = [...document.querySelectorAll('main [data-chatgpt-search-unit-key$=":user"]')]
    .filter(visible).at(-1);
  const group = user?.closest('[data-turn-key]');
  const key = group?.getAttribute('data-turn-key');
  if (!key || assistantTurnId !== `group:assistant:${key}` ||
      (userId ? !hasId(user, userId) : userTurnId !== `group:user:${key}`)) {
    return {reason: 'accepted_turn_changed'};
  }
  const assistants = [...group.querySelectorAll('[data-chatgpt-search-unit-key$=":assistant"]')]
    .filter(visible).filter(element => hasId(element, assistantId));
  if (assistants.length !== 1 || !['', 'ChatGPT said:'].includes(normalize(assistants[0].innerText)) ||
      [...assistants[0].querySelectorAll('img, video, canvas')].some(visible)) {
    return {reason: 'assistant_changed'};
  }
  if (editPrompt !== null) {
    const edits = [...group.querySelectorAll('button[aria-label="Edit message"]')]
      .filter(e => visible(e) && !e.disabled && e.getAttribute('aria-disabled') !== 'true');
    if (edits.length !== 1) return {reason: 'repair_edit_unavailable'};
    edits[0].click();
    return {edit_group: key};
  }
  const triggers = [...group.querySelectorAll('button[aria-label="More actions"][aria-haspopup="menu"]')]
    .filter(element => visible(element) && !element.disabled && element.getAttribute('aria-disabled') !== 'true');
  if (triggers.length !== 1 || !triggers[0].id) return {reason: 'retry_menu_unavailable'};
  const trigger = triggers[0];
  if (!triggerId) {
    if (trigger.getAttribute('aria-expanded') !== 'false') return {reason: 'retry_menu_already_open'};
    return {trigger_id: trigger.id};
  }
  if (trigger.id !== triggerId || trigger.getAttribute('aria-expanded') !== 'true') {
    return {reason: 'retry_menu_changed'};
  }
  const menu = document.getElementById(trigger.getAttribute('aria-controls'));
  if (!menu?.matches('[role="menu"]') || !visible(menu)) return {reason: 'retry_menu_changed'};
  const items = [...menu.querySelectorAll('[role="menuitem"]')].filter(element =>
    visible(element) && normalize(element.innerText) === 'Retry' &&
    element.getAttribute('aria-disabled') !== 'true' && !element.hasAttribute('data-disabled'));
  if (items.length !== 1) return {reason: 'retry_unavailable'};
  items[0].click();
  return {clicked: true};
}
"""
