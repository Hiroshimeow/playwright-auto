from __future__ import annotations

import weakref
from typing import Any

ROLE_BADGE_ID = "playwright-auto-role-badge-v3"
ROLE_CONTROL_ID = "playwright-auto-role-control-v1"
WINDOW_NAME_PREFIX = "__PLAYWRIGHT_AUTO_BINDING__:"
_INITIALIZED: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()

ROLE_INDICATOR_SCRIPT = r"""
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
"""


async def ensure_role_indicator(
    page: Any,
    *,
    expected_role: str | None = None,
    expected_page_id: str | None = None,
    expected_task_id: str | None = None,
) -> dict[str, str | None]:
    if not hasattr(page, "add_init_script") or not hasattr(page, "evaluate"):
        return {
            "role": expected_role,
            "pageId": expected_page_id,
            "taskId": expected_task_id or "idle",
            "title": f"⟦{expected_role}⟧ test" if expected_role else "test",
            "badge": (
                f"{expected_role} · {expected_task_id or 'idle'} · "
                f"{(expected_page_id or 'test')[:8]}"
                if expected_role
                else f"SET ROLE · {expected_task_id or 'idle'} · {(expected_page_id or 'test')[:8]}"
            ),
        }
    if not _INITIALIZED.get(page):
        await page.add_init_script(script=ROLE_INDICATOR_SCRIPT)
        _INITIALIZED[page] = True
    result = await page.evaluate(ROLE_INDICATOR_SCRIPT)

    if expected_role is not None:
        actual_role = result.get("role")
        if actual_role != expected_role:
            raise RuntimeError(
                f"visible role mismatch: expected {expected_role!r}, got {actual_role!r}"
            )
        if not str(result.get("title") or "").startswith(f"⟦{expected_role}⟧ "):
            raise RuntimeError("browser tab title does not contain the visible role")
        if not str(result.get("badge") or "").startswith(f"{expected_role} · "):
            raise RuntimeError("in-page role badge is missing or incorrect")

    if expected_page_id is not None and result.get("pageId") != expected_page_id:
        raise RuntimeError(
            f"visible page id mismatch: expected {expected_page_id!r}, "
            f"got {result.get('pageId')!r}"
        )
    if expected_task_id is not None and result.get("taskId") != expected_task_id:
        raise RuntimeError(
            f"visible task id mismatch: expected {expected_task_id!r}, "
            f"got {result.get('taskId')!r}"
        )
    return result
