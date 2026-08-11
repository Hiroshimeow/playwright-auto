import {APIClient} from "./api.js";
import {PollController} from "./polling.js";
import {
  state, commit, subscribe, commandKey, persistViewState,
  detailCacheGet, detailCachePut, detailCacheInvalidate, pruneDetailCache,
} from "./store.js";
import {renderBoard, refreshElapsed} from "./views/board.js";
import {installSelectionResume, refreshTimelineTimes, renderTaskDetail} from "./views/task_detail.js?v=20260809-compact-ui-v2";
import {renderHistory} from "./views/history.js";
import {renderNotify, renderNotifyReport} from "./views/notify.js?v=20260812-notify-v2";
import {renderRuntime} from "./views/runtime.js?v=20260728-system-status-1";
import {
  applyReuseRoleSelection, renderCreateActions, renderResume, renderWorkflowAgentOptions,
  selectedDependencyIds, selectedResumeTeams, updateResumeButton,
} from "./views/dashboard_actions.js?v=20260731-add-agents-1";

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
  bootstrapSelect: document.querySelector('#create-form select[name="bootstrap_id"]'),
  bootstrapInline: document.querySelector("#create-form [data-bootstrap-inline]"),
  workflowAgentOptions: document.querySelector("#workflow-agent-options"),
  createSummary: document.querySelector("#create-selection-summary"),
  createValidation: document.querySelector("#create-validation"),
  roleInputs: [],
  agentPanel: document.querySelector("#agents-panel"),
  agentOpener: document.querySelector("[data-open-agent]"),
  workflowTaskTeamOptions: document.querySelector("#workflow-task-team-options"),
  agentSelect: document.querySelector('#agents-panel select[name="agent_select"]'),
  agentForm: document.querySelector("#agent-form"),
  independentSettings: document.querySelector("#independent-settings"),
  deleteAgent: document.querySelector("[data-delete-agent]"),
  agentCommandDialog: document.querySelector("#agent-command-dialog"),
  agentCommandForm: document.querySelector("#agent-command-form"),
  agentSettingsDialog: document.querySelector("#agent-settings-dialog"),
  agentSettingsForm: document.querySelector("#agent-settings-form"),
  agentSettingsCaption: document.querySelector("#agent-settings-caption"),
  agentSettingsTriggerHelp: document.querySelector("#independent-settings-trigger-help"),
  toast: document.querySelector("#toast"),
};
const client = new APIClient(state.etags, state.inflight);
const activeStatuses = new Set(["submitting", "unknown", "queued", "running"]);
const OVERLAY_STATE = "cdpaOverlay";
const INLINE_BOOTSTRAP_VALUE = "__create_inline__";
let viewSaveTimer = null;
let closingOverlay = null;
let bootstrapDefaultId = "";
const reportBodies = new Map();
const selectedReportByTask = new Map();

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

function selectedWorkflowRoles() {
  return roots.roleInputs.filter(input => input.checked).map(input => input.value);
}

function basicTriggerSettings(values) {
  const settings = {
    recovery: false,
    interval_minutes: null,
    task_done: false,
    role_completed: [],
    teams: [],
    states: [],
    check_all: false,
  };
  const type = String(values.get("trigger_type") || "manual");
  const team = String(values.get("trigger_team") || "").trim();
  if (type === "interval") {
    const minutes = Number(values.get("interval_minutes"));
    settings.interval_minutes = Number.isFinite(minutes) ? minutes : null;
  } else if (type === "task_done") {
    settings.task_done = true;
    if (team) settings.teams = [team];
  } else if (type === "role_completed") {
    const role = String(values.get("trigger_role") || "").trim().toUpperCase();
    if (team) settings.teams = [team];
    if (role) settings.role_completed = [role];
  } else if (type === "task_state") {
    const taskState = String(values.get("trigger_state") || "").trim().toUpperCase();
    if (team) settings.teams = [team];
    if (taskState) settings.states = [taskState];
  } else if (type === "check_all") {
    settings.check_all = true;
    const minutes = Number(values.get("interval_minutes"));
    settings.interval_minutes = Number.isFinite(minutes) ? minutes : null;
  }
  else if (type === "recovery") settings.recovery = true;
  return settings;
}

function normalizedTriggerSettings(settings = {}) {
  return {
    recovery: Boolean(settings.recovery),
    interval_minutes: settings.interval_minutes == null ? null : Number(settings.interval_minutes),
    task_done: Boolean(settings.task_done),
    role_completed: [...(settings.role_completed || [])],
    teams: [...(settings.teams || [])],
    states: [...(settings.states || [])],
    check_all: Boolean(settings.check_all),
  };
}

function rememberTriggerSettings(form, settings = {}) {
  const normalized = normalizedTriggerSettings(settings);
  form.dataset.originalTriggerSettings = JSON.stringify(normalized);
  form.dataset.originalTriggerType = triggerTypeFromSettings(normalized);
}

function configuredTriggerSettings(form, values) {
  const selectedType = String(values.get("trigger_type") || "manual");
  const basic = basicTriggerSettings(values);
  if (form.dataset.originalTriggerType !== selectedType) return basic;
  let original;
  try {
    original = JSON.parse(form.dataset.originalTriggerSettings || "{}");
  } catch {
    return basic;
  }
  const merged = normalizedTriggerSettings(original);
  if (selectedType === "manual") return basic;
  if (selectedType === "interval") merged.interval_minutes = basic.interval_minutes;
  else if (selectedType === "task_done") {
    merged.task_done = true;
    merged.teams = basic.teams;
  } else if (selectedType === "role_completed") {
    merged.role_completed = basic.role_completed;
    merged.teams = basic.teams;
  } else if (selectedType === "task_state") {
    merged.states = basic.states;
    merged.teams = basic.teams;
  } else if (selectedType === "check_all") {
    merged.check_all = true;
    merged.interval_minutes = basic.interval_minutes;
  }
  else if (selectedType === "recovery") merged.recovery = true;
  return merged;
}

function triggerTypeFromSettings(settings = {}) {
  if (settings.recovery) return "recovery";
  if (settings.check_all) return "check_all";
  if (settings.interval_minutes != null) return "interval";
  if (settings.task_done) return "task_done";
  if ((settings.role_completed || []).length) return "role_completed";
  if ((settings.states || []).length) return "task_state";
  return "manual";
}

const triggerFieldsByType = {
  interval: ["interval"],
  task_done: ["task_team"],
  role_completed: ["role_team", "role"],
  task_state: ["state_team", "state"],
  check_all: ["interval"],
};

const triggerHelpByType = {
  manual: "Runs only when started manually.",
  interval: "Runs on the saved interval.",
  task_done: "Runs when a workflow task reaches DONE.",
  role_completed: "Runs when the selected workflow agent completes for the selected team.",
  task_state: "Runs when the exact team enters the selected state.",
  check_all: "Runs on the interval and reviews all active workflow tasks.",
  recovery: "Claims exclusive recovery events.",
};

function updateTriggerFields(container, type) {
  const visible = new Set(triggerFieldsByType[type] || []);
  for (const label of container.querySelectorAll("[data-trigger-field]")) {
    const tokens = label.dataset.triggerField.split(/\s+/).filter(Boolean);
    const shown = tokens.some(token => visible.has(token));
    label.hidden = !shown;
    for (const control of label.querySelectorAll("input, select, textarea")) {
      control.disabled = !shown;
    }
  }
}

function updateBasicTriggerFields() {
  const form = roots.agentForm;
  const independent = form.elements.independent.checked;
  roots.independentSettings.hidden = !independent;
  const type = String(form.elements.trigger_type.value || "manual");
  updateTriggerFields(roots.independentSettings, type);
}

function updateAgentSettingsTriggerFields() {
  const type = String(roots.agentSettingsForm.elements.trigger_type.value || "manual");
  updateTriggerFields(roots.agentSettingsForm, type);
  roots.agentSettingsTriggerHelp.textContent = triggerHelpByType[type] || triggerHelpByType.manual;
}

function showCreateValidation(message = "") {
  roots.createValidation.textContent = message;
  roots.createValidation.hidden = !message;
}

async function loadBootstrapOptions() {
  try {
    const response = await client.request("bootstraps", "/api/bootstraps");
    if (response.notModified) return;
    const data = response.data || {};
    const items = Array.isArray(data.items) ? data.items : [];
    const options = [new Option("None / Fresh context", "")];
    for (const item of items) {
      const option = new Option(item.name || item.bootstrap_id, item.bootstrap_id);
      option.title = item.description || "";
      options.push(option);
    }
    options.push(new Option("Create bootstrap inline…", INLINE_BOOTSTRAP_VALUE));
    roots.bootstrapSelect.replaceChildren(...options);
    bootstrapDefaultId = String(data.default_id || "");
    roots.bootstrapSelect.value = [...roots.bootstrapSelect.options].some(
      option => option.value === bootstrapDefaultId,
    ) ? bootstrapDefaultId : "";
    syncBootstrapInlineFields();
    updateCreateSummary();
  } catch (error) {
    bootstrapDefaultId = "";
    roots.bootstrapSelect.replaceChildren(
      new Option("None / Fresh context", ""),
      new Option("Create bootstrap inline…", INLINE_BOOTSTRAP_VALUE),
    );
    syncBootstrapInlineFields();
    toast(error.message);
  }
}

function syncBootstrapInlineFields() {
  roots.bootstrapInline.hidden = roots.bootstrapSelect.value !== INLINE_BOOTSTRAP_VALUE;
}

function resetBootstrapSelection() {
  roots.bootstrapSelect.value = [...roots.bootstrapSelect.options].some(
    option => option.value === bootstrapDefaultId,
  ) ? bootstrapDefaultId : "";
  syncBootstrapInlineFields();
}

function updateCreateSummary() {
  const roles = selectedWorkflowRoles();
  const dependencies = selectedDependencyIds(roots.dependencyOptions);
  const reuse = roots.reuseTeam.value.trim();
  const requested = roots.requestedTeam.value.trim();
  const team = reuse ? `Reuse ${reuse}` : requested ? `New ${requested}` : "Automatic team";
  const roleCopy = roles.length ? roles.join(" · ") : "No workflow agents";
  const dependencyCopy = dependencies.length ? `${dependencies.length} dependencies` : "No dependencies";
  roots.createSummary.textContent = `${team} · ${roleCopy} · ${dependencyCopy}`;
}

function renderTriggerChoices(current) {
  const activeWorkflowTasks = [...current.board.values()]
    .filter(task => task.task_mode !== "independent" && ["WAITING", "RUNNING"].includes(task.status))
    .sort((a, b) => String(a.team).localeCompare(String(b.team)));
  const taskSignature = JSON.stringify(activeWorkflowTasks.map(task => [
    task.team, task.task_id, task.status, task.task_title,
  ]));
  if (roots.workflowTaskTeamOptions.dataset.signature !== taskSignature) {
    const seen = new Set();
    const options = [];
    for (const task of activeWorkflowTasks) {
      const value = task.team || task.task_id;
      if (!value || seen.has(value)) continue;
      seen.add(value);
      const option = document.createElement("option");
      option.value = value;
      option.label = `${task.status} · ${task.task_id} · ${task.task_title || task.team || task.task_id}`;
      options.push(option);
    }
    roots.workflowTaskTeamOptions.replaceChildren(...options);
    roots.workflowTaskTeamOptions.dataset.signature = taskSignature;
  }

  const workflowAgents = (current.agents?.workflow || []).filter(agent => !agent.deleted_at);
  const agentSignature = JSON.stringify(workflowAgents.map(agent => [agent.route_key, agent.display_name]));
  for (const select of document.querySelectorAll("[data-workflow-agent-select]")) {
    if (select.dataset.signature === agentSignature) continue;
    const selected = select.value;
    const options = workflowAgents.map(agent => new Option(agent.display_name || agent.route_key, agent.route_key));
    select.replaceChildren(...options);
    select.dataset.signature = agentSignature;
    if ([...select.options].some(option => option.value === selected)) select.value = selected;
  }
}

function setAgentPanelOpen(open, {restoreFocus = false} = {}) {
  roots.agentPanel.hidden = !open;
  roots.agentOpener.setAttribute("aria-expanded", String(open));
  if (open) fetchAgents();
  else if (restoreFocus) roots.agentOpener.focus();
}

function resetAgentEditor() {
  const form = roots.agentForm;
  form.reset();
  form.elements.agent_kind.value = "new";
  form.elements.agent_id.value = "";
  form.elements.is_system.value = "false";
  form.elements.independent.disabled = false;
  form.elements.independent.checked = false;
  form.elements.trigger_type.value = "manual";
  form.elements.max_cycles.value = "0";
  rememberTriggerSettings(form, {});
  roots.deleteAgent.hidden = true;
  document.querySelector("#agent-editor-help").textContent =
    "New agents are Custom Workflow Agents unless Independent agent is enabled.";
  updateBasicTriggerFields();
}

function selectAgentRecord(value) {
  const [kind, id] = String(value || "").split(":", 2);
  if (kind === "workflow") {
    const agent = (state.agents.workflow || []).find(item => item.route_key === id);
    return agent ? {kind, id, agent} : null;
  }
  if (kind === "independent") {
    const agent = (state.agents.independent || []).find(item => item.task_id === id);
    return agent ? {kind, id, agent} : null;
  }
  return null;
}

function loadAgentEditor(value) {
  const selected = selectAgentRecord(value);
  if (!selected) { resetAgentEditor(); return; }
  const {kind, id, agent} = selected;
  const form = roots.agentForm;
  form.elements.agent_kind.value = kind;
  form.elements.agent_id.value = id;
  form.elements.name.value = kind === "workflow" ? agent.display_name : agent.name;
  form.elements.system_prompt.value = agent.system_prompt || "";
  form.elements.is_system.value = String(Boolean(agent.is_system || agent.is_builtin));
  form.elements.independent.checked = kind === "independent";
  form.elements.independent.disabled = true;
  roots.deleteAgent.hidden = Boolean(agent.is_system || agent.is_builtin);
  if (kind === "independent") {
    const settings = agent.trigger_settings || {};
    rememberTriggerSettings(form, settings);
    form.elements.trigger_type.value = triggerTypeFromSettings(settings);
    form.elements.max_cycles.value = Number.isFinite(Number(agent.max_cycles)) ? String(Number(agent.max_cycles)) : "0";
    form.elements.interval_minutes.value = settings.interval_minutes || "";
    form.elements.trigger_team.value = (settings.teams || [])[0] || "";
    form.elements.trigger_role.value = (settings.role_completed || [])[0] || "";
    form.elements.trigger_state.value = (settings.states || [])[0] || "";
  } else {
    form.elements.trigger_type.value = "manual";
    form.elements.max_cycles.value = "0";
    rememberTriggerSettings(form, {});
  }
  document.querySelector("#agent-editor-help").textContent = agent.is_system
    ? "System route identity is immutable. Changes apply only to future task snapshots."
    : kind === "workflow"
      ? "Workflow identity is immutable. Delete is blocked while a nonterminal task depends on it."
      : "Independent identity is immutable. This editor uses the existing independent runtime.";
  updateBasicTriggerFields();
}

function renderAgents(current) {
  renderWorkflowAgentOptions(roots.workflowAgentOptions, current.agents?.workflow || []);
  roots.roleInputs = [...roots.workflowAgentOptions.querySelectorAll('input[name="roles"]')];
  const selected = roots.agentSelect.value;
  const signature = JSON.stringify([
    current.agents?.workflow || [], current.agents?.independent || [],
  ]);
  if (roots.agentSelect.dataset.signature === signature) return;
  const placeholder = new Option("Select an agent", "");
  const workflowGroup = document.createElement("optgroup");
  workflowGroup.label = "Workflow agents";
  for (const agent of current.agents?.workflow || []) {
    workflowGroup.append(new Option(agent.display_name, `workflow:${agent.route_key}`));
  }
  const independentGroup = document.createElement("optgroup");
  independentGroup.label = "Independent agents";
  for (const agent of current.agents?.independent || []) {
    independentGroup.append(new Option(agent.name, `independent:${agent.task_id}`));
  }
  roots.agentSelect.replaceChildren(placeholder, workflowGroup, independentGroup);
  roots.agentSelect.dataset.signature = signature;
  roots.agentSelect.value = [...roots.agentSelect.options].some(option => option.value === selected)
    ? selected : "";
  if (selected && !roots.agentSelect.value) resetAgentEditor();
}

async function loadReports(detail) {
  const reports = [
    ...(detail?.reports || []),
    ...(detail?.task_mode === "independent" ? (detail?.maintenance_reports || []) : []),
  ];
  await Promise.all(reports.map(async report => {
    if (!report.url || reportBodies.has(report.url)) return;
    reportBodies.set(report.url, {status: "loading"});
    try {
      const response = await fetch(report.url, {headers: {Accept: "text/markdown"}});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      reportBodies.set(report.url, {status: "ready", body: await response.text()});
    } catch (error) {
      reportBodies.set(report.url, {status: "error", error: error.message});
    }
    if (state.selectedDetail?.task_id === detail?.task_id) {
      commit(current => { current.agentReportsRevision += 1; });
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
  const name = detail.agent?.name || detail.team || detail.task_id;
  form.elements.task_id.value = detail.task_id;
  form.elements.display_name.value = detail.agent?.name || "";
  form.elements.enabled.checked = detail.agent?.enabled !== false;
  form.elements.system_prompt.value = detail.agent?.system_prompt || "";
  form.elements.max_cycles.value = Number.isFinite(Number(detail.agent?.max_cycles)) ? String(Number(detail.agent.max_cycles)) : "0";
  form.elements.trigger_type.value = triggerTypeFromSettings(settings);
  form.elements.interval_minutes.value = settings.interval_minutes || "";
  form.elements.trigger_team.value = (settings.teams || [])[0] || "";
  form.elements.trigger_role.value = (settings.role_completed || [])[0] || "";
  form.elements.trigger_state.value = (settings.states || [])[0] || "RUNNING";
  rememberTriggerSettings(form, settings);
  roots.agentSettingsCaption.textContent = name;
  updateAgentSettingsTriggerFields();
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
  const signature = JSON.stringify([entries.length, entries.slice(0, 12).map(command => [
    command.kind, command.taskId, command.label, command.status, command.error, command.result,
  ])]);
  if (roots.commands.dataset.signature !== signature) {
    const header = document.createElement("header");
    const title = document.createElement("h2");
    title.textContent = "Commands";
    const count = document.createElement("span");
    count.className = "lane-count";
    count.textContent = String(entries.length);
    header.append(title, count);

    const list = document.createElement("div");
    list.className = "lane-list command-list";
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "muted command-empty";
      empty.textContent = "No pending operations.";
      list.append(empty);
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
      list.append(row);
    }
    roots.commands.replaceChildren(header, list);
    roots.commands.dataset.signature = signature;
  }

  for (const button of document.querySelectorAll("[data-control], [data-independent-action]")) {
    const action = button.dataset.control || `independent_${button.dataset.independentAction}`;
    const key = commandKey(action, button.dataset.taskId);
    const pending = current.pendingCommands.get(key);
    button.disabled = button.dataset.renderDisabled === "true"
      || Boolean(pending && activeStatuses.has(pending.status));
  }
}

function renderBootstrapContext(detail) {
  const context = detail?.bootstrap_context;
  const existing = roots.detail.querySelector("[data-bootstrap-context]");
  if (!context) {
    existing?.remove();
    return;
  }
  const signature = JSON.stringify(context);
  if (existing?.dataset.signature === signature) return;
  const section = existing || document.createElement("section");
  section.dataset.bootstrapContext = "";
  section.dataset.signature = signature;
  section.className = "detail-section";
  const title = document.createElement("strong");
  title.textContent = `Context · ${context.name || "Fresh context"}`;
  const meta = document.createElement("p");
  meta.textContent = context.bootstrap_id ? `Bootstrap ${context.bootstrap_id}` : "Fresh context";
  const roles = document.createElement("p");
  roles.textContent = Object.entries(context.roles || {}).map(([role, value]) => {
    const fallback = value?.fallback && value.fallback !== "none" ? ` · ${value.fallback}` : "";
    return `${role}: ${value?.source || "pending"}${fallback}`;
  }).join(" · ");
  section.replaceChildren(title, meta, roles);
  if (!existing) roots.detail.append(section);
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
    current.selectedIndependentTabByTask.get(current.selectedTaskId) || "overview",
    selectedReportByTask.get(current.selectedTaskId) || null,
    reportBodies,
    current.agentReportsRevision,
  );
  renderBootstrapContext(current.selectedDetail);
  renderCommands(current);
  renderRuntime(roots.services, current);
  renderAgents(current);
  renderTriggerChoices(current);
  renderCreateActions(
    roots.dependencyOptions,
    roots.reuseTeam,
    current,
    roots.roleInputs,
  );
  updateCreateSummary();

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
    const title = current.drawer === "resume" ? "Resume teams" : current.drawer === "notify" ? "Notify" : "History";
    if (roots.secondaryTitle.textContent !== title) roots.secondaryTitle.textContent = title;
    if (current.drawer === "history") renderHistory(roots.secondaryContent, current);
    if (current.drawer === "resume") renderResume(roots.secondaryContent, current);
    if (current.drawer === "notify") renderNotify(roots.secondaryContent, current);
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
  if (state.selectedTaskId !== taskId) selectedReportByTask.clear();
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
  if (state.selectedDetail?.task_id === taskId) return;
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
  if (state.selectedIndependentTabByTask.get(taskId) === "reports") loadReports(detail);
  return true;
}

export async function loadBoard() {
  try {
    const response = await client.request("board", "/api/tasks");
    if (response.notModified) return;
    const items = (response.data.items || []).filter(item => !item.agent?.deleted_at);
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

export async function fetchAgents() {
  if (state.agentsStatus === "idle") {
    commit(current => { current.agentsStatus = "loading"; current.agentsError = null; });
  }
  try {
    const response = await client.request("agents", "/api/agents");
    commit(current => {
      if (!response.notModified) current.agents = response.data;
      current.agentsStatus = "ready";
      current.agentsError = null;
    });
    return response.notModified ? state.agents : response.data;
  } catch (error) {
    commit(current => { current.agentsStatus = "error"; current.agentsError = error.message; });
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
        if ([
          "create_workflow_agent", "update_workflow_agent", "delete_workflow_agent",
          "create_independent_agent", "update_independent_agent", "delete_independent_agent",
          "independent_settings",
        ].includes(command.kind)) await fetchAgents();
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
  await Promise.all([loadBoard(), loadRuntime(), fetchAgents(), pollCommands()]);
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
  if (view === "notify") delete roots.secondaryContent.dataset.secondaryView;
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
  showCreateValidation();
  updateCreateSummary();
  loadDashboardActions();
  loadBootstrapOptions();
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
  const removeParent = event.target.closest("[data-remove-parent]");
  if (removeParent) {
    const taskId = removeParent.dataset.taskId;
    const parentTaskId = removeParent.dataset.removeParent;
    if (window.confirm(`Remove parent ${parentTaskId} from ${taskId}?`)) {
      queueCommand({
        kind: "remove_parent_dependency",
        taskId,
        endpoint: `/api/tasks/${encodeURIComponent(taskId)}/parents/${encodeURIComponent(parentTaskId)}/remove`,
        body: {expected_task_version: Number(removeParent.dataset.version)},
        label: `Remove parent · ${parentTaskId}`,
      });
    }
    return;
  }
  const changeGoal = event.target.closest("[data-change-goal]");
  if (changeGoal) {
    openChangeGoal(state.selectedDetail);
    return;
  }
  const detailTab = event.target.closest("[data-detail-tab]");
  if (detailTab) {
    const taskId = detailTab.dataset.taskId;
    const tab = detailTab.dataset.detailTab;
    commit(current => { current.selectedIndependentTabByTask.set(taskId, tab); });
    if (tab === "reports") loadReports(state.selectedDetail);
    return;
  }
  const independent = event.target.closest("[data-independent-action]");
  if (independent) {
    const action = independent.dataset.independentAction;
    const taskId = independent.dataset.taskId;
    if (action === "run-task") {
      openAgentCommand(state.selectedDetail);
    } else if (action === "settings") {
      openAgentSettings(state.selectedDetail);
    } else if (action === "delete") {
      const name = state.selectedDetail?.agent?.name || taskId;
      if (window.confirm(`Delete ${name}? Existing task history will be retained.`)) {
        queueCommand({
          kind: "delete_independent_agent",
          taskId,
          endpoint: `/api/independent-agents/${encodeURIComponent(taskId)}/delete`,
          body: {},
          label: `Delete agent · ${name}`,
        });
      }
    }
    return;
  }
  const report = event.target.closest("[data-report-select]");
  if (report) {
    const taskId = report.dataset.taskId;
    selectedReportByTask.set(taskId, report.dataset.reportSelect);
    commit(current => {
      if (report.dataset.reportRole) current.selectedRoleByTask.set(taskId, report.dataset.reportRole);
      current.agentReportsRevision += 1;
    });
    return;
  }
  const role = event.target.closest("[data-role-select]");
  if (role) {
    selectedReportByTask.delete(role.dataset.taskId);
    commit(current => { current.selectedRoleByTask.set(role.dataset.taskId, role.dataset.roleSelect); });
    return;
  }
  const control = event.target.closest("[data-control]");
  if (control) {
    const taskId = control.dataset.taskId;
    const detail = state.selectedDetail;
    const isReset = control.dataset.control === "reset";
    queueCommand({
      kind: control.dataset.control,
      taskId,
      endpoint: isReset
        ? `/api/independent-agents/${encodeURIComponent(taskId)}/reset`
        : `/api/tasks/${encodeURIComponent(taskId)}/controls`,
      body: isReset ? {reason: "Operator reset"} : {
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

async function openNotifyReport(taskId) {
  const detail = await loadTaskDetail(taskId);
  if (!detail || state.drawer !== "notify") return;
  await loadReports(detail);
  if (state.drawer === "notify") renderNotifyReport(roots.secondaryContent, detail, reportBodies);
}

roots.secondaryContent.addEventListener("click", async event => {
  if (event.target.closest("[data-resume-selected]")) {
    submitResumeBatch();
    return;
  }
  if (event.target.closest("[data-notify-back]")) {
    delete roots.secondaryContent.dataset.secondaryView;
    renderNotify(roots.secondaryContent, state);
    return;
  }
  const notify = event.target.closest("[data-notify-task-id]");
  if (notify) {
    await openNotifyReport(notify.dataset.notifyTaskId);
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
  if (event.target.closest("[data-open-agent]")) {
    setAgentPanelOpen(roots.agentPanel.hidden, {restoreFocus: !roots.agentPanel.hidden});
    return;
  }
  if (event.target.closest("[data-close-agent]")) {
    setAgentPanelOpen(false, {restoreFocus: true});
    return;
  }
  if (!roots.agentPanel.hidden && !roots.agentPanel.contains(event.target)) {
    setAgentPanelOpen(false);
  }
  if (event.target.closest("[data-new-agent]")) {
    roots.agentSelect.value = "";
    resetAgentEditor();
    roots.agentForm.elements.name.focus();
  }
  if (event.target.closest("[data-close-agent-command]")) roots.agentCommandDialog.close();
  if (event.target.closest("[data-close-agent-settings]")) roots.agentSettingsDialog.close();
  const view = event.target.closest("[data-view]")?.dataset.view;
  if (view) openDrawer(view);
});

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !roots.agentPanel.hidden) {
    setAgentPanelOpen(false, {restoreFocus: true});
    return;
  }
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
  showCreateValidation();
  updateCreateSummary();
});
roots.requestedTeam.addEventListener("input", () => {
  if (roots.requestedTeam.value.trim() && roots.reuseTeam.value) {
    roots.reuseTeam.value = "";
    applyReuseRoleSelection(roots.roleInputs, null, {reset: true});
  }
  roots.requestedTeam.disabled = false;
  showCreateValidation();
  updateCreateSummary();
});

roots.form.addEventListener("submit", event => {
  event.preventDefault();
  showCreateValidation();
  if (!roots.form.reportValidity()) {
    showCreateValidation("Complete the required task details before queueing.");
    return;
  }
  const values = new FormData(roots.form);
  const task = String(values.get("task") || "").trim();
  if (!task) {
    showCreateValidation("Task details are required.");
    return;
  }
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
  const bootstrapId = String(values.get("bootstrap_id") || "");
  if (bootstrapId === INLINE_BOOTSTRAP_VALUE) {
    const bootstrapNewId = String(values.get("bootstrap_new_id") || "").trim();
    const bootstrapName = String(values.get("bootstrap_new_name") || "").trim();
    const bootstrapSource = String(values.get("bootstrap_new_source") || "").trim() || null;
    const bootstrapPrewarm = String(values.get("bootstrap_new_prewarm_prompt") || "").trim() || null;
    const bootstrapMaxBackups = Number(values.get("bootstrap_new_max_backups") || 7);
    if (!bootstrapNewId || !bootstrapName) {
      showCreateValidation("Inline bootstrap ID and name are required.");
      return;
    }
    if (!bootstrapSource && !bootstrapPrewarm) {
      showCreateValidation("Inline bootstrap requires a source or prewarm prompt.");
      return;
    }
    body.bootstrap_definition = {
      bootstrap_id: bootstrapNewId,
      name: bootstrapName,
      source: bootstrapSource,
      prewarm_prompt: bootstrapPrewarm,
      max_backups: bootstrapMaxBackups,
    };
  } else {
    body.bootstrap_id = bootstrapId || null;
  }
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
  resetBootstrapSelection();
  roots.requestedTeam.disabled = false;
  applyReuseRoleSelection(roots.roleInputs, null, {reset: true});
  updateCreateSummary();
  closeCreate();
});

roots.form.addEventListener("input", event => {
  if (event.target.matches('input[name="roles"], input[name="depends_on_task_ids"], textarea[name="task"]')) {
    showCreateValidation();
    updateCreateSummary();
  }
});
roots.bootstrapSelect.addEventListener("change", () => {
  syncBootstrapInlineFields();
  showCreateValidation();
  updateCreateSummary();
});
roots.dependencyOptions.addEventListener("change", updateCreateSummary);
roots.workflowAgentOptions.addEventListener("change", updateCreateSummary);

roots.agentSelect.addEventListener("change", () => loadAgentEditor(roots.agentSelect.value));
roots.agentForm.elements.independent.addEventListener("change", updateBasicTriggerFields);
roots.agentForm.elements.trigger_type.addEventListener("change", updateBasicTriggerFields);
roots.agentSettingsForm.elements.trigger_type.addEventListener("change", updateAgentSettingsTriggerFields);

roots.agentForm.addEventListener("submit", event => {
  event.preventDefault();
  const values = new FormData(roots.agentForm);
  const kind = String(values.get("agent_kind") || "new");
  const id = String(values.get("agent_id") || "");
  const name = String(values.get("name") || "").trim();
  const systemPrompt = String(values.get("system_prompt") || "").trim();
  if (!name || !systemPrompt) return;
  if (kind === "new" && values.has("independent")) {
    queueCommand({
      kind: "create_independent_agent",
      taskId: "agent-catalog",
      endpoint: "/api/independent-agents",
      body: {
        name, system_prompt: systemPrompt, mode: "Independent",
        max_cycles: Math.max(0, Number(values.get("max_cycles") || 0)),
        trigger_settings: basicTriggerSettings(values),
      },
      label: `Create independent agent · ${name}`,
    });
  } else if (kind === "new") {
    queueCommand({
      kind: "create_workflow_agent",
      taskId: "agent-catalog",
      endpoint: "/api/workflow-agents",
      body: {name, system_prompt: systemPrompt},
      label: `Create workflow agent · ${name}`,
    });
  } else if (kind === "workflow") {
    queueCommand({
      kind: "update_workflow_agent",
      taskId: id,
      endpoint: `/api/workflow-agents/${encodeURIComponent(id)}/settings`,
      body: {name, system_prompt: systemPrompt},
      label: `Save workflow agent · ${name}`,
    });
  } else {
    queueCommand({
      kind: "update_independent_agent",
      taskId: id,
      endpoint: `/api/independent-agents/${encodeURIComponent(id)}/settings`,
      body: {
        display_name: name,
        system_prompt: systemPrompt,
        max_cycles: Math.max(0, Number(values.get("max_cycles") || 0)),
        trigger_settings: configuredTriggerSettings(roots.agentForm, values),
      },
      label: `Save independent agent · ${name}`,
    });
  }
});

roots.deleteAgent.addEventListener("click", () => {
  const selected = selectAgentRecord(roots.agentSelect.value);
  if (!selected || selected.agent.is_system || selected.agent.is_builtin) return;
  const name = selected.kind === "workflow" ? selected.agent.display_name : selected.agent.name;
  if (!window.confirm(`Delete ${name}? Existing task history will be retained.`)) return;
  const workflow = selected.kind === "workflow";
  queueCommand({
    kind: workflow ? "delete_workflow_agent" : "delete_independent_agent",
    taskId: selected.id,
    endpoint: workflow
      ? `/api/workflow-agents/${encodeURIComponent(selected.id)}/delete`
      : `/api/independent-agents/${encodeURIComponent(selected.id)}/delete`,
    body: {},
    label: `Delete agent · ${name}`,
  });
});

roots.agentCommandForm.addEventListener("submit", event => {
  event.preventDefault();
  const values = new FormData(roots.agentCommandForm);
  const taskId = String(values.get("task_id") || "");
  const instruction = String(values.get("instruction") || "").trim();
  if (!taskId || !instruction) return;
  queueCommand({
    kind: "independent_run",
    taskId,
    endpoint: `/api/independent-agents/${encodeURIComponent(taskId)}/run`,
    body: {trigger_type: "manual", instruction},
    label: `Run task · ${state.selectedDetail?.agent?.name || taskId}`,
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
    display_name: String(values.get("display_name") || "").trim(),
    system_prompt: systemPrompt,
    max_cycles: Math.max(0, Number(values.get("max_cycles") || 0)),
    trigger_settings: configuredTriggerSettings(roots.agentSettingsForm, values),
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
