import {APIClient} from "./api.js";
import {PollController} from "./polling.js";
import {
  state, commit, subscribe, commandKey, persistViewState,
  detailCacheGet, detailCachePut, detailCacheInvalidate, pruneDetailCache,
} from "./store.js";
import {renderBoard, refreshElapsed} from "./views/board.js";
import {installSelectionResume, refreshTimelineTimes, renderTaskDetail} from "./views/task_detail.js";
import {renderHistory} from "./views/history.js";
import {renderRuntime} from "./views/runtime.js?v=20260728-system-status-1";
import {
  applyReuseRoleSelection, renderCreateActions, renderResume, selectedDependencyIds,
  selectedResumeTeams, updateResumeButton,
} from "./views/dashboard_actions.js?v=20260730-workflow-roles-1";

const roots = {
  board: document.querySelector("#board"),
  detail: document.querySelector("#task-detail"),
  commands: document.querySelector("#commands"),
  services: document.querySelector("#service-status"),
  catalog: document.querySelector("#catalog-health"),
  secondaryDialog: document.querySelector("#secondary-dialog"),
  secondaryTitle: document.querySelector("#secondary-title"),
  secondaryContent: document.querySelector("#secondary-content"),
  dialog: document.querySelector("#create-dialog"),
  form: document.querySelector("#create-form"),
  changeGoalDialog: document.querySelector("#change-goal-dialog"),
  changeGoalForm: document.querySelector("#change-goal-form"),
  dependencyOptions: document.querySelector("#dependency-options"),
  reuseTeam: document.querySelector('#create-form select[name="reuse_team"]'),
  requestedTeam: document.querySelector('#create-form input[name="requested_team"]'),
  roleInputs: [...document.querySelectorAll('#create-form input[name="roles"]')],
  agentDialog: document.querySelector("#agent-dialog"),
  agentForm: document.querySelector("#agent-form"),
  agentCommandDialog: document.querySelector("#agent-command-dialog"),
  agentCommandForm: document.querySelector("#agent-command-form"),
  agentSettingsDialog: document.querySelector("#agent-settings-dialog"),
  agentSettingsForm: document.querySelector("#agent-settings-form"),
  toast: document.querySelector("#toast"),
};
const client = new APIClient(state.etags, state.inflight);
const activeStatuses = new Set(["submitting", "unknown", "queued", "running"]);
const OVERLAY_STATE = "cdpaOverlay";
let viewSaveTimer = null;
let closingOverlay = null;
const agentReportBodies = new Map();

function idempotencyKey() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
}

function cursorFor(index) {
  return btoa(String(index)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function toast(message) {
  roots.toast.textContent = message;
  roots.toast.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { roots.toast.hidden = true; }, 3500);
}

function commaValues(value) {
  return String(value || "").split(",").map(item => item.trim()).filter(Boolean);
}

function independentTriggerSettings(values) {
  const intervalRaw = String(values.get("interval_minutes") || "").trim();
  return {
    recovery: values.has("recovery"),
    interval_minutes: intervalRaw ? Number(intervalRaw) : null,
    task_done: values.has("task_done"),
    role_completed: commaValues(values.get("role_completed")).map(value => value.toUpperCase()),
    teams: commaValues(values.get("teams")),
    states: commaValues(values.get("states")).map(value => value.toUpperCase()),
    check_all: values.has("check_all"),
  };
}

function selectedWorkflowRoles() {
  return roots.roleInputs.filter(input => input.checked).map(input => input.value);
}

function renderAgentHistory(root, detail) {
  const fragment = document.createDocumentFragment();
  const historyItems = detail?.independent_history || [];
  if (!historyItems.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No lifecycle records for this agent.";
    fragment.append(empty);
  }
  for (const item of historyItems) {
    const article = document.createElement("article");
    article.className = "history-card";
    const title = document.createElement("strong");
    title.textContent = `Generation ${item.generation} · ${item.status}`;
    const meta = document.createElement("p");
    meta.className = "muted";
    meta.textContent = `${item.task_id} · ${item.completed_at || item.updated_at || item.created_at || "—"}`;
    article.append(title, meta);
    const outcome = item.last_outcome;
    if (outcome?.summary || outcome?.outcome) {
      const summary = document.createElement("p");
      summary.textContent = [outcome.outcome, outcome.summary].filter(Boolean).join(" · ");
      article.append(summary);
    }
    fragment.append(article);
  }
  root.replaceChildren(fragment);
  root.dataset.secondaryView = "agent-history";
}

function renderAgentReports(root, detail) {
  const fragment = document.createDocumentFragment();
  const reports = [...(detail?.reports || []), ...(detail?.maintenance_reports || [])];
  if (!reports.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No reports for this agent.";
    fragment.append(empty);
  }
  for (const report of reports) {
    const article = document.createElement("article");
    article.className = "history-card";
    const title = document.createElement("strong");
    title.textContent = report.summary || report.outcome || report.role || "Report";
    article.append(title);
    if (report.url) {
      const link = document.createElement("a");
      link.href = report.url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = report.url;
      article.append(link);
    }
    const loaded = report.url ? agentReportBodies.get(report.url) : null;
    const body = document.createElement("pre");
    if (loaded?.status === "ready") body.textContent = loaded.body;
    else if (loaded?.status === "error") body.textContent = `Report load failed: ${loaded.error}`;
    else if (report.content || report.message) body.textContent = report.content || report.message;
    else body.textContent = "Loading report body…";
    article.append(body);
    fragment.append(article);
  }
  root.replaceChildren(fragment);
  root.dataset.secondaryView = "reports";
}

async function loadAgentReports(detail) {
  const reports = [...(detail?.reports || []), ...(detail?.maintenance_reports || [])];
  await Promise.all(reports.map(async report => {
    if (!report.url || agentReportBodies.has(report.url)) return;
    agentReportBodies.set(report.url, {status: "loading"});
    try {
      const response = await fetch(report.url, {headers: {Accept: "text/markdown"}});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      agentReportBodies.set(report.url, {status: "ready", body: await response.text()});
    } catch (error) {
      agentReportBodies.set(report.url, {status: "error", error: error.message});
    }
    if (state.drawer === "reports" && state.selectedDetail?.task_id === detail?.task_id) {
      renderAgentReports(roots.secondaryContent, state.selectedDetail);
    }
  }));
}


function openAgentCommand(detail) {
  if (!detail || detail.task_mode !== "independent") return;
  const form = roots.agentCommandForm;
  form.elements.task_id.value = detail.task_id;
  form.elements.instruction.value = "";
  roots.agentCommandDialog.showModal();
  form.elements.instruction.focus();
}

function openAgentSettings(detail) {
  if (!detail || detail.task_mode !== "independent") return;
  const form = roots.agentSettingsForm;
  const settings = detail.agent?.trigger_settings || {};
  form.elements.task_id.value = detail.task_id;
  form.elements.enabled.checked = detail.agent?.enabled !== false;
  form.elements.system_prompt.value = detail.agent?.system_prompt || "";
  form.elements.recovery.checked = Boolean(settings.recovery);
  form.elements.task_done.checked = Boolean(settings.task_done);
  form.elements.check_all.checked = Boolean(settings.check_all);
  form.elements.interval_minutes.value = settings.interval_minutes || "";
  form.elements.teams.value = (settings.teams || []).join(", ");
  form.elements.states.value = (settings.states || []).join(", ");
  form.elements.role_completed.value = (settings.role_completed || []).join(", ");
  roots.agentSettingsDialog.showModal();
}

function scheduleViewSave() {
  clearTimeout(viewSaveTimer);
  viewSaveTimer = setTimeout(persistViewState, 120);
}

function selectedRole(current) {
  const taskId = current.selectedTaskId;
  if (!taskId) return null;
  return current.selectedRoleByTask.get(taskId) || current.selectedDetail?.active_role || null;
}

function commandPresentation(command) {
  const result = command.result || {};
  if (result.reason_code === "stale_worker") {
    return {
      label: "stale worker",
      className: "recovery_required",
      detail: [result.reason, result.next_safe_action].filter(Boolean).join(" · "),
    };
  }
  if (command.status === "applied" && result.outcome === "continued") {
    return {
      label: "continued",
      className: "applied",
      detail: [result.action, result.postcondition].filter(Boolean).join(" · "),
    };
  }
  if (command.status === "recovery_required") {
    return {
      label: "recovery required",
      className: "recovery_required",
      detail: [result.reason, result.next_safe_action].filter(Boolean).join(" · "),
    };
  }
  if (command.status === "failed") {
    return {label: "failed", className: "failed", detail: command.error || result.reason || ""};
  }
  if (["submitting", "queued", "running", "unknown"].includes(command.status)) {
    return {label: command.status === "submitting" ? "queued" : command.status, className: command.status, detail: ""};
  }
  return {label: command.status, className: command.status, detail: command.error || ""};
}

function renderCommands(current) {
  const entries = [...current.pendingCommands.values()].sort(
    (a, b) => String(b.createdAt).localeCompare(String(a.createdAt)),
  );
  const signature = JSON.stringify(entries.slice(0, 12).map(command => [
    command.kind, command.taskId, command.label, command.status, command.error, command.result,
  ]));
  if (roots.commands.dataset.signature !== signature) {
    const fragment = document.createDocumentFragment();
    const title = document.createElement("h3");
    title.textContent = "Commands";
    fragment.append(title);
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No pending operations.";
      fragment.append(empty);
    }
    for (const command of entries.slice(0, 12)) {
      const row = document.createElement("div");
      row.className = "command-row";
      const label = document.createElement("strong");
      label.textContent = command.label || command.kind;
      const presentation = commandPresentation(command);
      const status = document.createElement("span");
      status.className = `command-${presentation.className}`;
      status.textContent = presentation.label;
      row.append(label, status);
      if (presentation.detail) {
        const detail = document.createElement("p");
        detail.textContent = presentation.detail;
        row.append(detail);
      }
      fragment.append(row);
    }
    roots.commands.replaceChildren(fragment);
    roots.commands.dataset.signature = signature;
  }

  for (const button of document.querySelectorAll("[data-control], [data-independent-action]")) {
    const action = button.dataset.control || `independent_${button.dataset.independentAction}`;
    const key = commandKey(action, button.dataset.taskId);
    const pending = current.pendingCommands.get(key);
    button.disabled = Boolean(pending && activeStatuses.has(pending.status));
  }
  const reload = document.querySelector('[data-action="reload_catalog"]');
  const pendingReload = current.pendingCommands.get(commandKey("reload_catalog"));
  if (reload) reload.disabled = Boolean(pendingReload && activeStatuses.has(pendingReload.status));
}

function render(current) {
  renderBoard(roots.board, current);
  renderTaskDetail(
    roots.detail,
    current.selectedDetail,
    current.timeline,
    selectedRole(current),
    current.selectedTaskId,
    current.selectedDetailStatus,
    current.selectedDetailError,
  );
  renderCommands(current);
  renderRuntime(roots.services, current);
  renderCreateActions(
    roots.dependencyOptions,
    roots.reuseTeam,
    current,
    roots.roleInputs,
  );

  const catalog = current.catalog;
  const catalogText = catalog?.complete
    ? `Catalog ${catalog.discovered_at || "ready"}`
    : `Catalog degraded · ${(catalog?.errors || []).length} error(s)`;
  const catalogClass = catalog?.complete ? "catalog-health" : "catalog-health degraded";
  if (roots.catalog.textContent !== catalogText) roots.catalog.textContent = catalogText;
  if (roots.catalog.className !== catalogClass) roots.catalog.className = catalogClass;

  const historyButton = document.querySelector('[data-view="history"]');
  const terminalCount = Number(current.counts?.DONE || 0) + Number(current.counts?.STOPPED || 0);
  const historyLabel = terminalCount ? `History (${terminalCount})` : "History";
  if (historyButton && historyButton.textContent !== historyLabel) historyButton.textContent = historyLabel;

  if (current.drawer) {
    const title = current.drawer === "resume"
      ? "Resume teams"
      : current.drawer === "reports"
        ? "Reports"
        : current.drawer === "agent-history" ? "Agent history" : "History";
    if (roots.secondaryTitle.textContent !== title) roots.secondaryTitle.textContent = title;
    if (current.drawer === "history") renderHistory(roots.secondaryContent, current);
    if (current.drawer === "agent-history") renderAgentHistory(roots.secondaryContent, current.selectedDetail);
    if (current.drawer === "resume") renderResume(roots.secondaryContent, current);
    if (current.drawer === "reports") renderAgentReports(roots.secondaryContent, current.selectedDetail);
    if (!roots.secondaryDialog.open) roots.secondaryDialog.showModal();
  } else if (roots.secondaryDialog.open) {
    roots.secondaryDialog.close();
  }

  if (current.modalOpen && !roots.dialog.open) roots.dialog.showModal();
  if (!current.modalOpen && roots.dialog.open) roots.dialog.close();
}

subscribe(render);
installSelectionResume(roots.detail);

function selectTask(taskId, cached = null) {
  commit(current => {
    current.selectedTaskId = taskId;
    current.selectedDetail = cached;
    current.selectedDetailStatus = cached ? "ready" : "loading";
    current.selectedDetailError = null;
    current.timeline = cached?.timeline ? [...cached.timeline] : [];
    current.timelineCursor = null;
  });
}

function beginDetailLoad(taskId) {
  if (state.selectedTaskId !== taskId) return;
  commit(current => {
    if (current.selectedTaskId !== taskId) return;
    current.selectedDetail = null;
    current.selectedDetailStatus = "loading";
    current.selectedDetailError = null;
    current.timeline = [];
    current.timelineCursor = null;
  });
}

function failDetailLoad(taskId, error) {
  if (state.selectedTaskId !== taskId) return;
  commit(current => {
    if (current.selectedTaskId !== taskId) return;
    current.selectedDetail = null;
    current.selectedDetailStatus = "error";
    current.selectedDetailError = error;
    current.timeline = [];
    current.timelineCursor = null;
  });
}

function applyDetail(taskId, detail) {
  if (state.selectedTaskId !== taskId) return false;
  commit(current => {
    if (current.selectedTaskId !== taskId) return;
    current.selectedDetail = detail;
    current.selectedDetailStatus = "ready";
    current.selectedDetailError = null;
    current.timeline = [...(detail.timeline || [])];
    current.timelineCursor = detail.timeline_total > current.timeline.length
      ? cursorFor(current.timeline.length) : null;
    const available = detail.role_inputs || {};
    const existing = current.selectedRoleByTask.get(taskId);
    if (!existing || (!available[existing] && !(detail.roles || []).some(role => role.logical_role === existing))) {
      const fallback = detail.active_role || Object.keys(available)[0] || detail.roles?.[0]?.logical_role || null;
      if (fallback) current.selectedRoleByTask.set(taskId, fallback);
    }
  });
  return true;
}

export async function loadBoard() {
  try {
    const response = await client.request("board", "/api/tasks");
    if (response.notModified) return;
    const items = response.data.items || [];
    commit(current => {
      current.board = new Map(items.map(item => [item.task_id, item]));
      current.counts = response.data.counts;
      current.catalog = response.data.catalog;
      current.apiError = null;
      pruneDetailCache(current.board.keys());
    });
    const taskId = state.selectedTaskId;
    const summary = taskId ? state.board.get(taskId) : null;
    if (!summary) return;
    const cached = detailCacheGet(taskId, summary.version, summary.projection_sha256);
    if (cached) {
      if (state.selectedDetail !== cached) applyDetail(taskId, cached);
    } else {
      await loadTaskDetail(taskId);
    }
  } catch (error) {
    commit(current => { current.apiError = error.message; });
  }
}

export async function loadTaskDetail(taskId, {force = false} = {}) {
  if (!taskId) return;
  const summary = state.board.get(taskId);
  const version = Number(summary?.version || 0);
  const projectionSha256 = summary?.projection_sha256 || null;
  if (force) detailCacheInvalidate(taskId);
  const cached = !force ? detailCacheGet(taskId, version, projectionSha256) : null;
  if (cached) {
    applyDetail(taskId, cached);
    return cached;
  }
  if (!state.detailCache.has(taskId)) state.etags.delete(`detail:${taskId}`);
  beginDetailLoad(taskId);
  try {
    const response = await client.request(`detail:${taskId}`, `/api/tasks/${encodeURIComponent(taskId)}`);
    if (response.notModified) {
      const unchanged = detailCacheGet(taskId, version, projectionSha256);
      if (unchanged) applyDetail(taskId, unchanged);
      return unchanged;
    }
    const detail = response.data;
    detailCachePut(taskId, detail.version, projectionSha256, detail);
    applyDetail(taskId, detail);
    return detail;
  } catch (error) {
    failDetailLoad(taskId, error.message);
    toast(error.message);
    return null;
  }
}

export async function loadHistory(append = false) {
  const cursor = append ? state.historyCursor : null;
  const suffix = cursor ? `?limit=50&cursor=${encodeURIComponent(cursor)}` : "?limit=50";
  try {
    const response = await client.request(`history:${cursor || "first"}`, `/api/history${suffix}`);
    commit(current => {
      current.history = append ? [...current.history, ...response.data.items] : response.data.items;
      current.historyCursor = response.data.next_cursor;
    });
  } catch (error) { toast(error.message); }
}

export async function loadTimeline() {
  const taskId = state.selectedTaskId;
  if (!taskId || !state.timelineCursor) return;
  try {
    const response = await client.request(
      `timeline:${taskId}:${state.timelineCursor}`,
      `/api/tasks/${encodeURIComponent(taskId)}/timeline?limit=50&before=${encodeURIComponent(state.timelineCursor)}`,
    );
    commit(current => {
      current.timeline = [...current.timeline, ...response.data.items];
      current.timelineCursor = response.data.next_cursor;
    });
  } catch (error) { toast(error.message); }
}

export async function loadDashboardActions() {
  if (!state.dashboardActions) {
    commit(current => {
      current.dashboardActionsStatus = "loading";
      current.dashboardActionsError = null;
    });
  }
  try {
    const response = await client.request("dashboard-actions", "/api/dashboard-actions");
    if (response.notModified) {
      commit(current => {
        current.dashboardActionsStatus = "ready";
        current.dashboardActionsError = null;
      });
      return state.dashboardActions;
    }
    commit(current => {
      current.dashboardActions = response.data;
      current.dashboardActionsStatus = "ready";
      current.dashboardActionsError = null;
    });
    return response.data;
  } catch (error) {
    commit(current => {
      current.dashboardActionsStatus = "error";
      current.dashboardActionsError = error.message;
    });
    return null;
  }
}

async function loadRuntime() {
  try {
    const response = await client.request("state", "/api/state");
    if (!response.notModified) commit(current => { current.runtime = response.data; });
  } catch (error) {
    commit(current => { current.apiError = error.message; });
  }
}

function updateSystemChip(name, text) {
  const chip = roots.services.querySelector(`[data-service="${name}"]`);
  if (chip && chip.textContent !== text) chip.textContent = text;
}

async function loadSystem() {
  try {
    const response = await fetch("/api/system", {
      headers: {Accept: "application/json"},
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const system = await response.json();
    const cpu = system.cpu_percent == null ? Number.NaN : Number(system.cpu_percent);
    const free = system.disk_free_bytes == null ? Number.NaN : Number(system.disk_free_bytes);
    updateSystemChip("cpu", Number.isFinite(cpu) ? `CPU ${cpu.toFixed(1)}%` : "CPU —");
    updateSystemChip(
      "disk",
      Number.isFinite(free) ? `Disk ${(free / 1000000000).toFixed(1)} GB free` : "Disk —",
    );
  } catch {
    updateSystemChip("cpu", "CPU —");
    updateSystemChip("disk", "Disk —");
  }
}

async function pollCommands() {
  for (const [key, command] of [...state.pendingCommands]) {
    if (!command.commandId || !activeStatuses.has(command.status)) continue;
    try {
      const response = await client.request(`command:${command.commandId}`, `/api/commands/${encodeURIComponent(command.commandId)}`);
      commit(current => {
        const value = current.pendingCommands.get(key);
        if (!value) return;
        value.status = response.data.status;
        value.error = response.data.error;
        value.result = response.data.result;
      });
      if (["applied", "failed", "recovery_required"].includes(response.data.status)) {
        if (command.taskId && command.taskId !== "runtime" && command.taskId !== "new") {
          detailCacheInvalidate(command.taskId);
        }
        await loadBoard();
        if (["create_task", "resume_team"].includes(command.kind)) {
          await loadDashboardActions();
        }
        if (response.data.status === "applied" && state.selectedTaskId === command.taskId) {
          await loadTaskDetail(command.taskId, {force: true});
        }
      }
    } catch (error) {
      commit(current => {
        const value = current.pendingCommands.get(key);
        if (value) { value.status = "unknown"; value.error = error.message; }
      });
    }
  }
}

async function queueCommand({kind, taskId = "runtime", endpoint, body, label}) {
  const key = commandKey(kind, taskId);
  let pending = state.pendingCommands.get(key);
  if (pending && activeStatuses.has(pending.status)) return pending;
  const reuseKey = pending?.status === "unknown" ? pending.idempotencyKey : idempotencyKey();
  pending = {
    kind, taskId, label, idempotencyKey: reuseKey, status: "submitting",
    commandId: pending?.commandId || null, createdAt: new Date().toISOString(), error: null,
  };
  commit(current => { current.pendingCommands.set(key, pending); });
  try {
    const response = await client.json(`mutation:${key}`, endpoint, body, reuseKey);
    commit(current => {
      const value = current.pendingCommands.get(key);
      Object.assign(value, {commandId: response.data.command_id, status: response.data.status});
    });
    return pending;
  } catch (error) {
    commit(current => {
      const value = current.pendingCommands.get(key);
      Object.assign(value, {status: error.status ? "failed" : "unknown", error: error.message});
    });
    toast(error.message);
    return pending;
  }
}

async function refresh() {
  await Promise.all([loadBoard(), loadRuntime(), pollCommands()]);
}

function overlayName() {
  return history.state?.[OVERLAY_STATE] || null;
}

function pushOverlay(name) {
  if (overlayName() === name) return;
  history.pushState({...history.state, [OVERLAY_STATE]: name}, "");
}

function openDrawer(view) {
  if (!state.drawer) pushOverlay("drawer");
  commit(current => {
    current.drawer = view;
    if (view === "resume") current.resumeBatchResult = null;
  });
  if (view === "history" && !state.history.length) loadHistory();
  if (view === "resume") loadDashboardActions();
}

function closeDrawer() {
  if (!state.drawer || closingOverlay === "drawer") return;
  closingOverlay = "drawer";
  if (overlayName() === "drawer") history.back();
  else {
    commit(current => { current.drawer = null; });
    closingOverlay = null;
  }
}

function openChangeGoal(detail) {
  if (!detail || detail.status !== "RUNNING" || detail.task_mode === "independent") return;
  roots.changeGoalForm.elements.task_id.value = detail.task_id;
  roots.changeGoalForm.elements.expected_task_version.value = String(detail.version || 0);
  roots.changeGoalForm.elements.goal.value = detail.effective_goal || detail.task_text || "";
  roots.changeGoalDialog.showModal();
  roots.changeGoalForm.elements.goal.focus();
}

function closeChangeGoal() {
  if (roots.changeGoalDialog.open) roots.changeGoalDialog.close();
}

function openCreate() {
  if (!state.modalOpen) pushOverlay("modal");
  commit(current => { current.modalOpen = true; });
  loadDashboardActions();
}

function closeCreate() {
  if (!state.modalOpen || closingOverlay === "modal") return;
  closingOverlay = "modal";
  if (overlayName() === "modal") history.back();
  else {
    commit(current => { current.modalOpen = false; });
    closingOverlay = null;
  }
}

async function submitResumeBatch() {
  if (state.resumeBatchResult?.teams?.length) return;
  const teams = selectedResumeTeams(roots.secondaryContent);
  if (!teams.length) return;
  commit(current => { current.resumeBatchResult = {teams}; });
  await Promise.allSettled(teams.map(team => queueCommand({
    kind: "resume_team",
    taskId: team,
    endpoint: "/api/tasks/resume",
    body: {team, reason: "dashboard exact-team resume"},
    label: `Resume · ${team}`,
  })));
  await loadDashboardActions();
}

roots.board.addEventListener("click", async event => {
  const copy = event.target.closest("[data-copy-task-id]");
  if (copy) {
    event.stopPropagation();
    const taskId = copy.dataset.copyTaskId;
    try {
      await navigator.clipboard.writeText(taskId);
      toast("Task ID copied.");
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(copy.previousElementSibling);
      selection.removeAllRanges();
      selection.addRange(range);
      toast("Task ID selected for copying.");
    }
    return;
  }
  const select = event.target.closest("[data-select-task]");
  if (select) {
    const taskId = select.dataset.selectTask;
    const summary = state.board.get(taskId);
    const cached = summary
      ? detailCacheGet(taskId, summary.version, summary.projection_sha256)
      : null;
    selectTask(taskId, cached);
    if (!cached) await loadTaskDetail(taskId);
  }
});

roots.board.addEventListener("scroll", event => {
  const list = event.target.closest?.(".lane-list");
  if (!list) return;
  const column = list.closest(".lane")?.dataset.column;
  if (column) state.scrollState.laneScroll[column] = list.scrollTop;
  scheduleViewSave();
}, {capture: true, passive: true});

roots.detail.addEventListener("scroll", () => {
  state.scrollState.detailTop = roots.detail.scrollTop;
  scheduleViewSave();
}, {passive: true});

roots.detail.addEventListener("click", event => {
  const changeGoal = event.target.closest("[data-change-goal]");
  if (changeGoal) {
    openChangeGoal(state.selectedDetail);
    return;
  }
  const independent = event.target.closest("[data-independent-action]");
  if (independent) {
    const action = independent.dataset.independentAction;
    const taskId = independent.dataset.taskId;
    if (action === "run") {
      queueCommand({
        kind: "independent_run",
        taskId,
        endpoint: `/api/independent-agents/${encodeURIComponent(taskId)}/run`,
        body: {trigger_type: "manual"},
        label: `Run once · ${state.selectedDetail?.agent?.name || taskId}`,
      });
    } else if (action === "command") {
      openAgentCommand(state.selectedDetail);
    } else if (action === "settings") {
      openAgentSettings(state.selectedDetail);
    } else if (action === "history") {
      openDrawer("agent-history");
    } else if (action === "reports") {
      openDrawer("reports");
      loadAgentReports(state.selectedDetail);
    }
    return;
  }
  const role = event.target.closest("[data-role-select]");
  if (role) {
    commit(current => { current.selectedRoleByTask.set(role.dataset.taskId, role.dataset.roleSelect); });
    return;
  }
  const control = event.target.closest("[data-control]");
  if (control) {
    const taskId = control.dataset.taskId;
    const detail = state.selectedDetail;
    queueCommand({
      kind: control.dataset.control,
      taskId,
      endpoint: `/api/tasks/${encodeURIComponent(taskId)}/controls`,
      body: {
        action: control.dataset.control,
        role: selectedRole(state),
        expected_task_version: Number(control.dataset.version),
        confirmed: ["stop", "clear_team"].includes(control.dataset.control),
      },
      label: `${control.textContent} · ${detail?.team || taskId}`,
    });
  }
  if (event.target.closest("[data-load-timeline]")) loadTimeline();
});

roots.secondaryContent.addEventListener("click", event => {
  if (event.target.closest("[data-resume-selected]")) {
    submitResumeBatch();
    return;
  }
  const task = event.target.closest("[data-task-id]");
  if (task) {
    const taskId = task.dataset.taskId;
    const summary = state.board.get(taskId);
    const cached = summary
      ? detailCacheGet(taskId, summary.version, summary.projection_sha256)
      : null;
    selectTask(taskId, cached);
    if (!cached) loadTaskDetail(taskId);
  }
  if (event.target.closest("[data-load-history]")) loadHistory(true);
});
roots.secondaryContent.addEventListener("change", event => {
  if (event.target.matches("input[data-resume-team]")) {
    updateResumeButton(roots.secondaryContent);
  }
});

roots.secondaryDialog.addEventListener("click", event => {
  if (event.target === roots.secondaryDialog || event.target.closest("[data-close-secondary]")) closeDrawer();
});
roots.secondaryDialog.addEventListener("cancel", event => {
  event.preventDefault();
  closeDrawer();
});
roots.secondaryDialog.addEventListener("close", () => {
  if (state.drawer && overlayName() !== "drawer") commit(current => { current.drawer = null; });
});

document.addEventListener("click", event => {
  if (event.target.closest("[data-open-create]")) openCreate();
  if (event.target.closest("[data-close-create]")) closeCreate();
  if (event.target.closest("[data-open-agent]")) roots.agentDialog.showModal();
  if (event.target.closest("[data-close-agent]")) roots.agentDialog.close();
  if (event.target.closest("[data-close-agent-command]")) roots.agentCommandDialog.close();
  if (event.target.closest("[data-close-agent-settings]")) roots.agentSettingsDialog.close();
  const view = event.target.closest("[data-view]")?.dataset.view;
  if (view) openDrawer(view);
  if (event.target.closest('[data-action="reload_catalog"]')) {
    queueCommand({
      kind: "reload_catalog", endpoint: "/api/runtime/reload", body: {},
      label: "Reload catalog",
    });
  }
});

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && state.modalOpen) closeCreate();
});

window.addEventListener("popstate", () => {
  if (state.modalOpen) commit(current => { current.modalOpen = false; });
  else if (state.drawer) commit(current => { current.drawer = null; });
  closingOverlay = null;
});

roots.reuseTeam.addEventListener("change", () => {
  const selectedItem = (state.dashboardActions?.reuse_teams || [])
    .find(item => item.team === roots.reuseTeam.value) || null;
  const reuse = Boolean(selectedItem);
  if (reuse) roots.requestedTeam.value = "";
  roots.requestedTeam.disabled = reuse;
  applyReuseRoleSelection(roots.roleInputs, selectedItem, {reset: !selectedItem});
});
roots.requestedTeam.addEventListener("input", () => {
  if (roots.requestedTeam.value.trim() && roots.reuseTeam.value) {
    roots.reuseTeam.value = "";
    applyReuseRoleSelection(roots.roleInputs, null, {reset: true});
  }
  roots.requestedTeam.disabled = false;
});

roots.form.addEventListener("submit", event => {
  event.preventDefault();
  const values = new FormData(roots.form);
  const task = String(values.get("task") || "").trim();
  if (!task) return;
  const manualDependencies = String(values.get("depends_on_task_ids") || "")
    .split(",").map(value => value.trim()).filter(Boolean);
  const dependencies = [...new Set([
    ...selectedDependencyIds(roots.dependencyOptions),
    ...manualDependencies,
  ])];
  const requestedTeam = String(values.get("requested_team") || "").trim();
  const reuseTeam = String(values.get("reuse_team") || "").trim();
  const body = {
    task,
    repository: String(values.get("repository") || "").trim() || null,
    depends_on_task_ids: dependencies,
  };
  body.roles = selectedWorkflowRoles();
  if (reuseTeam) body.reuse_team = reuseTeam;
  else if (requestedTeam) body.requested_team = requestedTeam;
  queueCommand({
    kind: "create_task",
    taskId: "new",
    endpoint: "/api/tasks",
    body,
    label: task.split("\n")[0],
  });
  roots.form.reset();
  roots.requestedTeam.disabled = false;
  applyReuseRoleSelection(roots.roleInputs, null, {reset: true});
  closeCreate();
});

roots.agentForm.addEventListener("submit", event => {
  event.preventDefault();
  const values = new FormData(roots.agentForm);
  const name = String(values.get("name") || "").trim();
  const systemPrompt = String(values.get("system_prompt") || "").trim();
  if (!name || !systemPrompt) return;
  queueCommand({
    kind: "create_independent_agent",
    taskId: "new-agent",
    endpoint: "/api/independent-agents",
    body: {
      name,
      system_prompt: systemPrompt,
      mode: "Independent",
      trigger_settings: independentTriggerSettings(values),
    },
    label: `Create custom agent · ${name}`,
  });
  roots.agentForm.reset();
  roots.agentDialog.close();
});

roots.agentCommandForm.addEventListener("submit", event => {
  event.preventDefault();
  const values = new FormData(roots.agentCommandForm);
  const taskId = String(values.get("task_id") || "");
  const instruction = String(values.get("instruction") || "").trim();
  if (!taskId || !instruction) return;
  queueCommand({
    kind: "independent_command",
    taskId,
    endpoint: `/api/independent-agents/${encodeURIComponent(taskId)}/run`,
    body: {trigger_type: "manual", instruction},
    label: `Command · ${state.selectedDetail?.agent?.name || taskId}`,
  });
  roots.agentCommandForm.reset();
  roots.agentCommandDialog.close();
});

roots.agentSettingsForm.addEventListener("submit", event => {
  event.preventDefault();
  const values = new FormData(roots.agentSettingsForm);
  const taskId = String(values.get("task_id") || "");
  if (!taskId) return;
  const systemPrompt = String(values.get("system_prompt") || "").trim();
  if (!systemPrompt) return;
  const body = {
    enabled: values.has("enabled"),
    system_prompt: systemPrompt,
    trigger_settings: independentTriggerSettings(values),
  };
  queueCommand({
    kind: "independent_settings",
    taskId,
    endpoint: `/api/independent-agents/${encodeURIComponent(taskId)}/settings`,
    body,
    label: `Settings · ${state.selectedDetail?.agent?.name || taskId}`,
  });
  roots.agentSettingsDialog.close();
});

roots.agentDialog.addEventListener("click", event => {
  if (event.target === roots.agentDialog) roots.agentDialog.close();
});
roots.agentCommandDialog.addEventListener("click", event => {
  if (event.target === roots.agentCommandDialog) roots.agentCommandDialog.close();
});
roots.agentSettingsDialog.addEventListener("click", event => {
  if (event.target === roots.agentSettingsDialog) roots.agentSettingsDialog.close();
});

roots.changeGoalForm.addEventListener("submit", event => {
  event.preventDefault();
  const taskId = roots.changeGoalForm.elements.task_id.value;
  const goal = roots.changeGoalForm.elements.goal.value;
  const expected = Number(roots.changeGoalForm.elements.expected_task_version.value);
  if (!goal.trim()) { toast("Goal must not be blank."); return; }
  queueCommand({
    kind: "change_goal",
    taskId,
    endpoint: `/api/tasks/${encodeURIComponent(taskId)}/goal`,
    body: {goal, expected_task_version: expected},
    label: `Change goal · ${state.selectedDetail?.team || taskId}`,
  });
  closeChangeGoal();
});
for (const close of document.querySelectorAll("[data-close-change-goal]")) {
  close.addEventListener("click", closeChangeGoal);
}
roots.changeGoalDialog.addEventListener("click", event => {
  if (event.target === roots.changeGoalDialog) closeChangeGoal();
});

roots.dialog.addEventListener("click", event => {
  if (event.target === roots.dialog) closeCreate();
});
roots.dialog.addEventListener("cancel", event => {
  event.preventDefault();
  closeCreate();
});
roots.dialog.addEventListener("close", () => {
  if (state.modalOpen && overlayName() !== "modal") commit(current => { current.modalOpen = false; });
});

const restoredOverlay = overlayName();
if ((state.drawer && restoredOverlay !== "drawer") || (state.modalOpen && restoredOverlay !== "modal")) {
  commit(current => {
    if (restoredOverlay !== "drawer") current.drawer = null;
    if (restoredOverlay !== "modal") current.modalOpen = false;
  });
}
render(state);
roots.detail.scrollTop = state.scrollState.detailTop;
const poller = new PollController(refresh, 1000);
poller.start();
const systemPoller = new PollController(loadSystem, 2000);
systemPoller.start();
const elapsedTimer = setInterval(() => {
  refreshElapsed(roots.board);
  refreshTimelineTimes(roots.detail);
}, 1000);
window.addEventListener("beforeunload", () => {
  clearInterval(elapsedTimer);
  clearTimeout(viewSaveTimer);
  persistViewState();
  poller.destroy();
  systemPoller.destroy();
});
