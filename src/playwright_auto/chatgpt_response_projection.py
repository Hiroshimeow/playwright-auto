from __future__ import annotations

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
  const latestUser = [...document.querySelectorAll(
    '[data-message-author-role="user"], [data-chatgpt-search-unit-key$=":user"]'
  )].filter(isVisible).at(-1) || null;
  const userTurn = latestUser?.closest('[data-turn-id],[data-turn-key],[data-turn-id-container]') || latestUser;
  const roots = [...document.querySelectorAll('[data-turn-id],[data-turn-key],[data-turn-id-container]')]
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
  const exact = [...document.querySelectorAll('[data-testid="regenerate-thread-error-button"]')]
    .find((element) => visible(element) && !element.disabled && element.getAttribute('aria-disabled') !== 'true');
  if (exact) return exact;
  const latestUser = [...document.querySelectorAll(
    '[data-message-author-role="user"], [data-chatgpt-search-unit-key$=":user"]'
  )].filter(visible).at(-1) || null;
  const userTurn = latestUser?.closest('[data-turn-id],[data-turn-key],[data-turn-id-container]') || latestUser;
  const matches = [...document.querySelectorAll('button,[role="button"]')].filter((element) => {
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
