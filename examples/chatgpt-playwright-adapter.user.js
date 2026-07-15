// ==UserScript==
// @name         ChatGPT Playwright Adapter
// @namespace    playwright-auto
// @version      0.3.0
// @description  Normalize ChatGPT DOM state for deterministic Playwright automation.
// @match        https://chatgpt.com/*
// @match        https://auth.openai.com/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==


(() => {
  const ROLE_KEY = "playwright-auto:role";
  const PAGE_ID_KEY = "playwright-auto:page-id";
  const TASK_ID_KEY = "playwright-auto:task-id";
  const ROLE_CHANGE_KEY = "playwright-auto:last-role-change";
  const BADGE_ID = "playwright-auto-role-badge-v3";
  const CONTROL_ID = "playwright-auto-role-control-v1";
  const LEGACY_BADGE_IDS = [
    "playwright-auto-role-badge",
    "playwright-auto-role-badge-v2",
  ];
  const WINDOW_NAME_PREFIX = "__PLAYWRIGHT_AUTO_BINDING__:";
  const PREFIX_RE = /^⟦[A-Za-z][A-Za-z0-9_-]{0,63}⟧\s*/;
  const ROLE_RE = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
  const ROLE_CHANGED_EVENT = "playwright-auto:role-changed";
  const DEFAULT_ROLES = [
    "PLAN", "DEV", "DEV1", "DEV2", "DEV3",
    "REVIEW", "REVIEW1", "REVIEW2", "TEST", "TEST1",
  ];

  const isChatGPT = () =>
    location.hostname === "chatgpt.com" || location.hostname === "www.chatgpt.com";

  const readWindowBinding = () => {
    if (!window.name || !window.name.startsWith(WINDOW_NAME_PREFIX)) return {};
    try {
      const value = JSON.parse(window.name.slice(WINDOW_NAME_PREFIX.length));
      return value && typeof value === "object" ? value : {};
    } catch (_) {
      return {};
    }
  };

  const writeWindowBinding = (role, pageId, taskId) => {
    if (!pageId) return;
    window.name = WINDOW_NAME_PREFIX + JSON.stringify({
      role: role || null,
      pageId,
      taskId: taskId || null,
    });
  };

  const readBinding = ({createPageId = true} = {}) => {
    const fallback = readWindowBinding();
    let role = null;
    let pageId = null;
    let taskId = null;
    try {
      role = sessionStorage.getItem(ROLE_KEY);
      pageId = sessionStorage.getItem(PAGE_ID_KEY);
      taskId = sessionStorage.getItem(TASK_ID_KEY);
    } catch (_) {
      // Cross-origin auth pages have a separate storage namespace.
    }
    role = role || fallback.role || null;
    pageId = pageId || fallback.pageId || null;
    taskId = taskId || fallback.taskId || null;
    if (!pageId && createPageId && isChatGPT()) pageId = crypto.randomUUID();
    if (isChatGPT()) {
      try {
        if (role) sessionStorage.setItem(ROLE_KEY, role);
        else sessionStorage.removeItem(ROLE_KEY);
        if (pageId) sessionStorage.setItem(PAGE_ID_KEY, pageId);
        if (taskId) sessionStorage.setItem(TASK_ID_KEY, taskId);
      } catch (_) {
        // Storage can be unavailable during early navigation.
      }
    }
    if (pageId) writeWindowBinding(role, pageId, taskId);
    return {role, pageId, taskId};
  };

  const currentPageState = () => {
    if (document.querySelector('button[data-testid="stop-button"]')) return "responding";
    const composer = document.querySelector('[contenteditable="true"][role="textbox"]');
    const text = (composer?.innerText || "").replace(/\s+/g, " ").trim();
    if (text) return "draft";
    if (composer) return "ready";
    if (location.hostname === "auth.openai.com") return "auth_required";
    return "unknown";
  };

  const hideLegacyBadges = () => {
    for (const id of LEGACY_BADGE_IDS) {
      const badge = document.getElementById(id);
      if (badge) badge.style.display = "none";
    }
  };

  const ensureDatalist = (panel) => {
    let datalist = panel.querySelector("datalist");
    if (!datalist) {
      datalist = document.createElement("datalist");
      datalist.id = "playwright-auto-role-options-v1";
      panel.appendChild(datalist);
    }
    const registry = window.__PLAYWRIGHT_AUTO_ROLE_REGISTRY__ || {};
    const roles = [...new Set([...DEFAULT_ROLES, ...(registry.roles || [])])];
    datalist.replaceChildren(...roles.map((role) => {
      const option = document.createElement("option");
      option.value = role;
      return option;
    }));
    return datalist;
  };

  const setPanelStatus = (message, kind = "info") => {
    const status = document.querySelector(`#${CONTROL_ID} [data-role-status]`);
    if (!status) return;
    status.textContent = message;
    status.dataset.kind = kind;
    status.style.color = kind === "error" ? "#fecaca" : kind === "warning" ? "#fde68a" : "#d1d5db";
  };

  const closePanel = () => {
    const panel = document.getElementById(CONTROL_ID);
    if (panel) panel.hidden = true;
  };

  const emitRoleChange = (oldRole, role, binding, source) => {
    const detail = {
      oldRole: oldRole || null,
      role: role || null,
      pageId: binding.pageId,
      taskId: binding.taskId || null,
      source,
      state: currentPageState(),
      changedAt: new Date().toISOString(),
    };
    try {
      sessionStorage.setItem(ROLE_CHANGE_KEY, JSON.stringify(detail));
    } catch (_) {
      // Storage may be unavailable on auth pages.
    }
    window.dispatchEvent(new CustomEvent(ROLE_CHANGED_EVENT, {detail}));
    window.postMessage({type: ROLE_CHANGED_EVENT, detail}, "*");
    return detail;
  };

  const setRole = (rawRole, {source = "manual-ui"} = {}) => {
    const role = String(rawRole || "").trim().toUpperCase();
    if (!ROLE_RE.test(role)) throw new Error("Role must match [A-Za-z][A-Za-z0-9_-]{0,63}");
    const before = readBinding();
    if (!before.pageId) throw new Error("Page identity is not available");
    if (isChatGPT()) sessionStorage.setItem(ROLE_KEY, role);
    writeWindowBinding(role, before.pageId, before.taskId);
    const detail = emitRoleChange(before.role, role, before, source);
    apply();
    return detail;
  };

  const releaseRole = ({source = "manual-ui"} = {}) => {
    const before = readBinding();
    if (!before.pageId) throw new Error("Page identity is not available");
    if (isChatGPT()) sessionStorage.removeItem(ROLE_KEY);
    writeWindowBinding(null, before.pageId, before.taskId);
    const detail = emitRoleChange(before.role, null, before, source);
    apply();
    return detail;
  };

  const ensureControlPanel = () => {
    if (!document.body) return null;
    let panel = document.getElementById(CONTROL_ID);
    if (!panel) {
      panel = document.createElement("div");
      panel.id = CONTROL_ID;
      panel.hidden = true;
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-label", "Automation role control");
      Object.assign(panel.style, {
        position: "fixed",
        top: "42px",
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: "2147483647",
        width: "min(430px, calc(100vw - 24px))",
        padding: "12px",
        borderRadius: "10px",
        background: "rgba(17, 24, 39, 0.98)",
        color: "#ffffff",
        border: "1px solid rgba(255,255,255,0.35)",
        boxShadow: "0 8px 30px rgba(0,0,0,0.5)",
        font: "13px/1.35 system-ui, sans-serif",
      });
      panel.innerHTML = `
        <div style="font-weight:700;margin-bottom:8px">Tab role</div>
        <div style="display:flex;gap:7px;align-items:center">
          <input data-role-input list="playwright-auto-role-options-v1" autocomplete="off"
            placeholder="PLAN, DEV, REVIEW1..."
            style="min-width:0;flex:1;padding:7px 9px;border-radius:7px;border:1px solid #6b7280;background:#111827;color:white" />
          <button data-role-apply type="button">Apply</button>
          <button data-role-release type="button">Release</button>
          <button data-role-close type="button">×</button>
        </div>
        <div data-role-status style="margin-top:8px;font-size:12px;color:#d1d5db"></div>
        <div style="margin-top:6px;font-size:11px;color:#9ca3af">
          Changing role keeps the current chat, task, draft, and page ID. A running workflow using the old role stops safely on ownership check.
        </div>`;
      for (const button of panel.querySelectorAll("button")) {
        Object.assign(button.style, {
          padding: "7px 9px",
          borderRadius: "7px",
          border: "1px solid #6b7280",
          background: "#374151",
          color: "white",
          cursor: "pointer",
        });
      }
      panel.addEventListener("click", (event) => event.stopPropagation());
      panel.querySelector("[data-role-close]").addEventListener("click", closePanel);
      panel.querySelector("[data-role-apply]").addEventListener("click", () => {
        const input = panel.querySelector("[data-role-input]");
        try {
          const detail = setRole(input.value, {source: "manual-ui"});
          setPanelStatus(`Changed ${detail.oldRole || "UNASSIGNED"} → ${detail.role}. Chat and task were preserved.`);
          window.setTimeout(closePanel, 700);
        } catch (error) {
          setPanelStatus(error?.message || String(error), "error");
        }
      });
      panel.querySelector("[data-role-release]").addEventListener("click", () => {
        try {
          const detail = releaseRole({source: "manual-ui"});
          setPanelStatus(`Released ${detail.oldRole || "UNASSIGNED"}. Chat and task were preserved.`, "warning");
          window.setTimeout(closePanel, 700);
        } catch (error) {
          setPanelStatus(error?.message || String(error), "error");
        }
      });
      panel.querySelector("[data-role-input]").addEventListener("keydown", (event) => {
        if (event.key === "Enter") panel.querySelector("[data-role-apply]").click();
        if (event.key === "Escape") closePanel();
      });
      document.body.appendChild(panel);
    }
    ensureDatalist(panel);
    return panel;
  };

  const openPanel = () => {
    const panel = ensureControlPanel();
    if (!panel) return;
    const binding = readBinding();
    const input = panel.querySelector("[data-role-input]");
    input.value = binding.role || "";
    panel.hidden = false;
    const warning = currentPageState() === "responding"
      ? "Response is currently streaming. Changing role will invalidate the old workflow binding."
      : binding.taskId
        ? `Current task ${binding.taskId} and chat will be kept.`
        : "This tab is not assigned to a task.";
    setPanelStatus(warning, currentPageState() === "responding" ? "warning" : "info");
    input.focus();
    input.select();
  };

  const ensureBadge = () => {
    if (!document.body) return null;
    let badge = document.getElementById(BADGE_ID);
    if (!badge) {
      badge = document.createElement("button");
      badge.id = BADGE_ID;
      badge.type = "button";
      badge.setAttribute("aria-label", "Automation role indicator and control");
      badge.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const panel = ensureControlPanel();
        if (panel?.hidden) openPanel();
        else closePanel();
      });
      document.body.appendChild(badge);
    }
    Object.assign(badge.style, {
      position: "fixed",
      top: "8px",
      left: "50%",
      transform: "translateX(-50%)",
      zIndex: "2147483647",
      padding: "5px 10px",
      borderRadius: "7px",
      background: badge.dataset.conflict === "true" ? "rgba(153, 27, 27, 0.97)" : "rgba(17, 24, 39, 0.94)",
      color: "#ffffff",
      border: "1px solid rgba(255,255,255,0.42)",
      boxShadow: "0 2px 10px rgba(0,0,0,0.35)",
      font: "700 12px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      letterSpacing: "0.04em",
      pointerEvents: "auto",
      cursor: "pointer",
      userSelect: "none",
    });
    return badge;
  };

  const apply = () => {
    hideLegacyBadges();
    const binding = readBinding();
    const role = binding.role;
    const pageId = binding.pageId || "no-page-id";
    const taskId = binding.taskId || "idle";
    const baseTitle = (document.title || "ChatGPT").replace(PREFIX_RE, "");
    if (role) {
      const expectedTitle = `⟦${role}⟧ ${baseTitle || "ChatGPT"}`;
      if (document.title !== expectedTitle) document.title = expectedTitle;
    } else if (document.title !== baseTitle) {
      document.title = baseTitle;
    }
    const badge = ensureBadge();
    ensureControlPanel();
    if (badge) {
      const compactTask = taskId.length > 28 ? `${taskId.slice(0, 25)}...` : taskId;
      const label = role
        ? `${role} · ${compactTask} · ${pageId.slice(0, 8)}`
        : `SET ROLE · ${compactTask} · ${pageId.slice(0, 8)}`;
      if (badge.textContent !== label) badge.textContent = label;
      badge.dataset.role = role || "";
      badge.dataset.pageId = pageId;
      badge.dataset.taskId = taskId;
      badge.title = role ? `Click to change role ${role}` : "Click to assign a role";
    }
    document.documentElement.dataset.playwrightAutoRole = role || "";
    document.documentElement.dataset.playwrightAutoPageId = pageId;
    document.documentElement.dataset.playwrightAutoTaskId = taskId;
    return {role, pageId, taskId, title: document.title, badge: badge?.textContent || null};
  };

  const setConflict = (message = null) => {
    const badge = ensureBadge();
    if (!badge) return;
    badge.dataset.conflict = message ? "true" : "false";
    badge.dataset.conflictMessage = message || "";
    badge.style.background = message ? "rgba(153, 27, 27, 0.97)" : "rgba(17, 24, 39, 0.94)";
    if (message) badge.title = message;
    const panel = document.getElementById(CONTROL_ID);
    if (message && panel && !panel.hidden) setPanelStatus(message, "error");
  };

  const setRegistry = (registry = {}) => {
    window.__PLAYWRIGHT_AUTO_ROLE_REGISTRY__ = registry;
    const panel = document.getElementById(CONTROL_ID);
    if (panel) ensureDatalist(panel);
  };

  window.__PLAYWRIGHT_AUTO_ROLE_INDICATOR__ = {
    apply,
    open: openPanel,
    close: closePanel,
    setRole,
    releaseRole,
    setConflict,
    setRegistry,
    readBinding,
  };
  if (!window.__PLAYWRIGHT_AUTO_ROLE_CONTROL_STARTED__) {
    window.__PLAYWRIGHT_AUTO_ROLE_CONTROL_STARTED__ = true;
    const start = () => {
      apply();
      if (document.head) {
        new MutationObserver(() => requestAnimationFrame(apply)).observe(
          document.head,
          {subtree: true, childList: true, characterData: true}
        );
      }
      window.setInterval(apply, 500);
    };
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start, {once: true});
    } else {
      start();
    }
  } else {
    apply();
  }
  return apply();
})()
// PLAYWRIGHT_AUTO_ROLE_CONTROL_END
(() => {
  "use strict";

  const ROLE_KEY = "playwright-auto:role";
  const PAGE_ID_KEY = "playwright-auto:page-id";
  const TASK_ID_KEY = "playwright-auto:task-id";
  const WINDOW_NAME_PREFIX = "__PLAYWRIGHT_AUTO_BINDING__:";
  const EVENT_STATE = "playwright-auto:state";
  const EVENT_COMMAND = "playwright-auto:command";
  const root = document.documentElement;

  const visible = (element) => Boolean(
    element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
  );
  const firstVisible = (selector) => [...document.querySelectorAll(selector)].find(visible) || null;
  const cleanText = (element) => (element?.innerText || "").replace(/\s+/g, " ").trim();

  function readWindowBinding() {
    if (!window.name || !window.name.startsWith(WINDOW_NAME_PREFIX)) return {};
    try {
      const value = JSON.parse(window.name.slice(WINDOW_NAME_PREFIX.length));
      return value && typeof value === "object" ? value : {};
    } catch (_) {
      return {};
    }
  }

  function writeWindowBinding(role, pageId, taskId) {
    if (!role || !pageId) return;
    window.name = WINDOW_NAME_PREFIX + JSON.stringify({
      role,
      pageId,
      taskId: taskId || null,
    });
  }

  function currentBinding({ createPageId = true } = {}) {
    const fallback = readWindowBinding();
    let role = null;
    let pageId = null;
    let taskId = null;
    try {
      role = sessionStorage.getItem(ROLE_KEY);
      pageId = sessionStorage.getItem(PAGE_ID_KEY);
      taskId = sessionStorage.getItem(TASK_ID_KEY);
    } catch (_) {
      // Cross-origin auth pages have a separate storage namespace.
    }
    role = role || fallback.role || null;
    pageId = pageId || fallback.pageId || null;
    taskId = taskId || fallback.taskId || null;

    const isChatGPT = location.hostname === "chatgpt.com" || location.hostname === "www.chatgpt.com";
    if (!pageId && createPageId && isChatGPT) pageId = crypto.randomUUID();
    if (isChatGPT) {
      try {
        if (role) sessionStorage.setItem(ROLE_KEY, role);
        if (pageId) sessionStorage.setItem(PAGE_ID_KEY, pageId);
        if (taskId) sessionStorage.setItem(TASK_ID_KEY, taskId);
      } catch (_) {
        // Storage can be unavailable during an early navigation phase.
      }
    }
    if (role && pageId) writeWindowBinding(role, pageId, taskId);
    return { role, pageId, taskId };
  }

  function ensurePageId() {
    return currentBinding().pageId;
  }

  function applyRoleIndicator(role, pageId, taskId = null) {
    taskId = taskId || currentBinding({ createPageId: false }).taskId || "idle";
    const badgeId = "playwright-auto-role-badge-v2";
    const legacyBadge = document.getElementById("playwright-auto-role-badge");
    if (legacyBadge) legacyBadge.style.display = "none";
    const prefixPattern = /^⟦[A-Za-z][A-Za-z0-9_-]{0,63}⟧\s*/;
    const baseTitle = (document.title || "ChatGPT").replace(prefixPattern, "");
    if (!role) {
      document.getElementById(badgeId)?.remove();
      document.title = baseTitle;
      return;
    }
    document.title = `⟦${role}⟧ ${baseTitle || "ChatGPT"}`;
    let badge = document.getElementById(badgeId);
    if (!badge && document.body) {
      badge = document.createElement("div");
      badge.id = badgeId;
      badge.setAttribute("aria-label", "Automation role indicator");
      Object.assign(badge.style, {
        position: "fixed",
        top: "8px",
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: "2147483647",
        padding: "5px 10px",
        borderRadius: "7px",
        background: "rgba(17, 24, 39, 0.94)",
        color: "#ffffff",
        border: "1px solid rgba(255,255,255,0.42)",
        boxShadow: "0 2px 10px rgba(0,0,0,0.35)",
        font: "700 12px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        letterSpacing: "0.04em",
        pointerEvents: "none",
        userSelect: "none",
      });
      document.body.appendChild(badge);
    }
    if (badge) {
      const compactTask = taskId.length > 28 ? `${taskId.slice(0, 25)}...` : taskId;
      badge.textContent = `${role} · ${compactTask} · ${pageId.slice(0, 8)}`;
      badge.dataset.role = role;
      badge.dataset.pageId = pageId;
      badge.dataset.taskId = taskId;
    }
  }

  function sessionId() {
    const parts = location.pathname.split("/").filter(Boolean);
    const index = parts.indexOf("c");
    return index >= 0 ? parts[index + 1] || null : null;
  }

  function messages() {
    return [...document.querySelectorAll('[data-message-author-role][data-message-id]')]
      .filter(visible)
      .map((element) => ({
        role: element.getAttribute("data-message-author-role") || "",
        messageId: element.getAttribute("data-message-id") || "",
        turnId: element.closest("[data-turn-id]")?.getAttribute("data-turn-id") || null,
        text: cleanText(element),
        actions: [...element.querySelectorAll("button[data-testid]")]
          .filter(visible)
          .map((button) => button.dataset.testid)
          .filter(Boolean),
      }));
  }

  function snapshot() {
    const binding = currentBinding();
    const composer = firstVisible('[contenteditable="true"][role="textbox"]');
    const send = firstVisible('button[data-testid="send-button"]');
    const stop = firstVisible('button[data-testid="stop-button"]');
    const login = firstVisible('[data-testid="login-button"]');
    const retry = firstVisible('[data-testid="regenerate-thread-error-button"]');
    const items = messages();
    const composerText = cleanText(composer);

    let state = "unknown";
    if (retry || location.pathname === "/auth/error") state = "error";
    else if (location.hostname === "auth.openai.com" || login) state = "auth_required";
    else if (stop) state = "responding";
    else if (composerText) state = "draft";
    else if (items.at(-1)?.role === "user") state = "submitting";
    else if (items.length) state = "waiting_prompt";
    else if (composer) state = "new_chat";

    return {
      state,
      role: binding.role,
      pageId: binding.pageId,
      taskId: binding.taskId,
      sessionId: sessionId(),
      url: location.href,
      composerText,
      composerEmpty: !composerText,
      composerEditable: Boolean(
        composer && composer.getAttribute("contenteditable") === "true" &&
        composer.getAttribute("aria-disabled") !== "true"
      ),
      sendVisible: Boolean(send),
      stopVisible: Boolean(stop),
      messages: items,
    };
  }

  let lastSerialized = "";
  function publish() {
    const current = snapshot();
    const serialized = JSON.stringify(current);
    if (serialized === lastSerialized) return current;
    lastSerialized = serialized;

    applyRoleIndicator(current.role, current.pageId, current.taskId);
    root.dataset.playwrightAutoState = current.state;
    root.dataset.playwrightAutoRole = current.role || "";
    root.dataset.playwrightAutoPageId = current.pageId || "";
    root.dataset.playwrightAutoTaskId = current.taskId || "";
    root.dataset.playwrightAutoSessionId = current.sessionId || "";
    root.dataset.playwrightAutoComposerEmpty = String(current.composerEmpty);
    window.dispatchEvent(new CustomEvent(EVENT_STATE, { detail: current }));
    return current;
  }

  function setRole(role) {
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(role)) {
      throw new Error("Invalid role");
    }
    const binding = currentBinding();
    sessionStorage.setItem(ROLE_KEY, role);
    writeWindowBinding(role, binding.pageId, binding.taskId);
    return publish();
  }

  function recentResponses(count = 1) {
    if (!Number.isInteger(count) || count < 1) throw new Error("count must be a positive integer");
    const selected = [];
    const seen = new Set();
    for (const item of snapshot().messages.toReversed()) {
      if (item.role !== "assistant") continue;
      const identity = item.turnId || item.messageId;
      if (seen.has(identity)) continue;
      seen.add(identity);
      selected.push(item);
      if (selected.length === count) break;
    }
    return selected.reverse();
  }

  function runCommand(command, payload = {}) {
    const current = snapshot();
    if (command === "set-role") return setRole(payload.role);
    if (command === "new-chat") {
      firstVisible('[data-testid="create-new-chat-button"]')?.click();
      return true;
    }
    if (command === "send" && current.state === "draft") {
      firstVisible('button[data-testid="send-button"]')?.click();
      return true;
    }
    if (command === "stop" && current.state === "responding") {
      firstVisible('button[data-testid="stop-button"]')?.click();
      return true;
    }
    if (command === "refresh") {
      location.reload();
      return true;
    }
    return false;
  }

  window.addEventListener(EVENT_COMMAND, (event) => {
    const detail = event.detail || {};
    runCommand(detail.command, detail.payload || {});
  });

  window.__PLAYWRIGHT_AUTO__ = Object.freeze({
    snapshot,
    publish,
    setRole,
    recentResponses,
    runCommand,
  });

  let scheduled = false;
  const schedulePublish = () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      publish();
    });
  };

  new MutationObserver(schedulePublish).observe(document.documentElement, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["data-testid", "data-message-id", "data-message-author-role", "contenteditable", "aria-disabled"],
  });
  window.addEventListener("popstate", schedulePublish);
  window.addEventListener("hashchange", schedulePublish);
  publish();
})();
