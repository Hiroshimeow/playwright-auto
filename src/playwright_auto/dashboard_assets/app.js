import {APIClient} from "./api.js";
import {PollController} from "./polling.js";
import {createLiveReducer, createLiveStreamController, shouldLiveStream} from "./live_stream.js?v=20260925-live-sse-v1";
import {
  state, commit, subscribe, commandKey, persistViewState,
  detailCacheGet, detailCachePut, detailCacheInvalidate, pruneDetailCache,
} from "./store.js?v=20260914-goal2-ops-v1";
import {renderBoard, refreshElapsed} from "./views/board.js?v=20260915-operator-ux-v1";
import {renderBrowserManager, renderBrowserScope, syncCreateBrowserOptions, creationUsesTemporary} from "./views/browsers.js?v=20260915-endpoint-all-v1";
import {installSelectionResume, refreshTimelineTimes, renderTaskDetail} from "./views/task_detail.js?v=20261011-p0012-challenge-confirm-v1";
import {renderHistory} from "./views/history.js";
import {renderNotify, renderNotifyReport} from "./views/notify.js?v=20261011-p0012-challenge-confirm-v1";
import {renderRuntime} from "./views/runtime.js?v=20260906-listen-controls-v1";
import {
  applyReuseRoleSelection, renderCreateActions, renderResume, renderWorkflowAgentOptions,
  selectedDependencyIds, selectedResumeTeams, updateResumeButton,
} from "./views/dashboard_actions.js?v=20260731-add-agents-1";

const roots = {
  board: document.querySelector("#board"),
  detail: document.querySelector("#task-detail"),
  taskRail: document.querySelector("[data-task-rail]"),
  commands: document.querySelector("#commands"),
  services: document.querySelector("#service-status"),
  catalog: document.querySelector("#catalog-health"),
  browserScope: document.querySelector("#browser-scope"),
  browserDialog: document.querySelector("#browser-dialog"),
  browserManager: document.querySelector("#browser-manager"),
  secondaryDialog: document.querySelector("#secondary-dialog"),
  secondaryTitle: document.querySelector("#secondary-title"),
  secondaryContent: document.querySelector("#secondary-content"),
  dialog: document.querySelector("#create-dialog"),
  form: document.querySelector("#create-form"),
  changeGoalDialog: document.querySelector("#change-goal-dialog"),
  changeGoalForm: document.querySelector("#change-goal-form"),
  changeUrlDialog: document.querySelector("#change-url-dialog"),
  changeUrlForm: document.querySelector("#change-url-form"),
  dependencyOptions: document.querySelector("#dependency-options"),
  reuseTeam: document.querySelector('#create-form select[name="reuse_team"]'),
  requestedTeam: document.querySelector('#create-form input[name="requested_team"]'),
  requestedTeamField: document.querySelector('#create-form [data-requested-team-field]'),
  taskInput: document.querySelector('#create-form textarea[name="task"]'),
  taskDetails: document.querySelector('#create-form [data-create-section="task-details"]'),
  conversationUrl: document.querySelector('#create-form input[name="conversation_url"]'),
  browserEndpointSelect: document.querySelector('#create-form select[name="browser_endpoint_id"]'),
  browserEndpointField: document.querySelector('#create-form [data-create-browser-field]'),
  teamBrowserSlot: document.querySelector('#create-form [data-team-browser-slot]'),
  soloBrowserSlot: document.querySelector('#create-form [data-solo-browser-slot]'),
  workflowModeInputs: [...document.querySelectorAll('#create-form input[name="workflow_mode"]')],
  soloRole: document.querySelector('#create-form select[name="solo_role"]'),
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
  agentBrowserEndpointSelect: document.querySelector('#agent-form select[name="browser_endpoint_id"]'),
  independentSettings: document.querySelector("#independent-settings"),
  deleteAgent: document.querySelector("[data-delete-agent]"),
  agentCommandDialog: document.querySelector("#agent-command-dialog"),
  agentCommandForm: document.querySelector("#agent-command-form"),
  agentSettingsDialog: document.querySelector("#agent-settings-dialog"),
  agentSettingsForm: document.querySelector("#agent-settings-form"),
  agentSettingsBrowserEndpointSelect: document.querySelector('#agent-settings-form select[name="browser_endpoint_id"]'),
  agentSettingsCaption: document.querySelector("#agent-settings-caption"),
  agentSettingsTriggerHelp: document.querySelector("#independent-settings-trigger-help"),
  toast: document.querySelector("#toast"),
};
const client = new APIClient(state.etags, state.inflight);
const liveController = createLiveStreamController({
  snapshotLoader: async scope => {
    const response = await fetch(scope.snapshotUrl, {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data?.error?.message || `${response.status} ${response.statusText}`);
    return data;
  },
  onView: view => {
    const scope = liveController.scopeKey;
    if (!scope) return;
    commit(current => {
      current.liveEventsByScope.set(scope, view);
      current.liveEventsStatusByScope.set(scope, view.status || "ready");
      if (view.error) current.liveEventsErrorByScope.set(scope, view.error);
      else current.liveEventsErrorByScope.delete(scope);
    });
  },
});
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

function createWorkflowMode() {
  return roots.workflowModeInputs.find(input => input.checked)?.value === "solo" ? "solo" : "team";
}

function syncTeamStartRoleOptions() {
  const field = roots.form.elements.initial_role;
  const selected = field.value || "PLAN";
  const roles = selectedWorkflowRoles();
  field.replaceChildren(...roles.map(role => new Option(role, role)));
  field.value = roles.includes(selected) ? selected : (roles.includes("PLAN") ? "PLAN" : (roles[0] || ""));
}

function syncSoloRoleOptions() {
  const selected = roots.soloRole.value;
  const options = [new Option("Select role", "")];
  for (const input of roots.roleInputs) {
    options.push(new Option(input.value, input.value));
  }
  roots.soloRole.replaceChildren(...options);
  roots.soloRole.value = [...roots.soloRole.options].some(option => option.value === selected)
    ? selected
    : "";
}

function syncCreateMode() {
  const solo = createWorkflowMode() === "solo";
  const existingSolo = solo && Boolean(roots.conversationUrl.value.trim());
  for (const node of roots.form.querySelectorAll("[data-team-only]")) node.hidden = solo;
  for (const node of roots.form.querySelectorAll("[data-solo-only]")) node.hidden = !solo;
  if (solo) roots.soloBrowserSlot.append(roots.browserEndpointField);
  else roots.teamBrowserSlot.prepend(roots.browserEndpointField);
  roots.taskDetails.hidden = existingSolo;
  roots.requestedTeamField.hidden = existingSolo;
  roots.taskInput.required = !existingSolo;
  roots.taskInput.disabled = existingSolo;
  for (const input of roots.form.querySelectorAll('[name="governing_objective"], [name="governing_directives"]')) {
    input.required = !solo;
    input.disabled = solo;
  }
  roots.soloRole.required = solo;
  roots.browserEndpointSelect.required = solo;
  if (!solo) syncTeamStartRoleOptions();
  if (solo) roots.reuseTeam.value = "";
  roots.requestedTeam.disabled = existingSolo || (!solo && Boolean(roots.reuseTeam.value));
  syncBootstrapInlineFields();
  updateCreateSummary();
}

function basicTriggerSettings(values) {
  const settings = {
    recovery: false,
    interval_minutes: null,
    task_done: false,
    role_completed: [],
    teams: values.getAll("dependency_team").map(value => String(value).trim()).filter(Boolean),
    states: [],
    check_all: false,
  };
  const type = String(values.get("trigger_type") || "manual");
  if (type === "interval") {
    const minutes = Number(values.get("interval_minutes"));
    settings.interval_minutes = Number.isFinite(minutes) ? minutes : null;
  } else if (type === "task_done") {
    settings.task_done = true;
  } else if (type === "role_completed") {
    const role = String(values.get("trigger_role") || "").trim().toUpperCase();
    if (role) settings.role_completed = [role];
  } else if (type === "task_state") {
    const taskState = String(values.get("trigger_state") || "").trim().toUpperCase();
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
  merged.teams = basic.teams;
  if (selectedType === "interval") merged.interval_minutes = basic.interval_minutes;
  else if (selectedType === "task_done") {
    merged.task_done = true;
  } else if (selectedType === "role_completed") {
    merged.role_completed = basic.role_completed;
  } else if (selectedType === "task_state") {
    merged.states = basic.states;
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
  task_done: [],
  role_completed: ["role"],
  task_state: ["state"],
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
  bootstrapDefaultId = "";
  roots.bootstrapSelect.replaceChildren(new Option("Fresh context", ""));
  roots.bootstrapSelect.value = "";
  roots.bootstrapSelect.disabled = true;
  roots.bootstrapInline.hidden = true;
  updateCreateSummary();
}

function syncBootstrapInlineFields() {
  roots.bootstrapSelect.value = "";
  roots.bootstrapSelect.disabled = true;
  roots.bootstrapSelect.title = "Bootstrap branching is temporarily disabled; workflow tasks use Fresh context.";
  roots.bootstrapInline.hidden = true;
}

function resetBootstrapSelection() {
  roots.bootstrapSelect.value = "";
  syncBootstrapInlineFields();
}

function updateCreateSummary() {
  if (createWorkflowMode() === "solo") {
    const role = roots.soloRole.value || "Select role";
    const requested = roots.requestedTeam.value.trim();
    const existingSolo = Boolean(roots.conversationUrl.value.trim());
    const team = existingSolo ? "Existing conversation" : requested ? `Solo ${requested}` : "Solo automatic team";
    const endpoint = roots.browserEndpointSelect.selectedOptions[0]?.textContent || "Select browser";
    roots.createSummary.textContent = `${team} A� ${role} A� ${endpoint}`;
    return;
  }
  const roles = selectedWorkflowRoles();
  const dependencies = selectedDependencyIds(roots.dependencyOptions);
  const reuse = roots.reuseTeam.value.trim();
  const requested = roots.requestedTeam.value.trim();
  const team = reuse ? `Reuse ${reuse}` : requested ? `New ${requested}` : "Automatic team";
  const roleCopy = roles.length ? roles.join(" · ") : "No workflow agents";
  const dependencyCopy = dependencies.length ? `${dependencies.length} dependencies` : "No dependencies";
  roots.createSummary.textContent = `${team} · ${roleCopy} · ${dependencyCopy}`;
}

function syncDependencyGroupToggles(container) {
  for (const group of container.querySelectorAll("[data-dependency-state-group]")) {
    const toggle = group.querySelector("[data-dependency-state-toggle]");
    const inputs = [...group.querySelectorAll('input[name="dependency_team"]')];
    if (!toggle) continue;
    toggle.checked = inputs.length > 0 && inputs.every(input => input.checked);
    toggle.indeterminate = inputs.some(input => input.checked) && !toggle.checked;
    toggle.disabled = inputs.length === 0;
  }
}

function renderTriggerChoices(current) {
  const workflowTasks = [...current.board.values()]
    .filter(task => task.task_mode !== "independent");
  const activeWorkflowTasks = workflowTasks
    .filter(task => ["WAITING", "RUNNING"].includes(task.status))
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

  const dependencyStatusRank = {RUNNING: 0, BLOCKED: 1, PAUSED: 2};
  const dependencyTasks = workflowTasks
    .filter(task => Object.hasOwn(dependencyStatusRank, String(task.status || "").toUpperCase()))
    .sort((a, b) => {
      const statusA = String(a.status || "").toUpperCase();
      const statusB = String(b.status || "").toUpperCase();
      const statusDelta = dependencyStatusRank[statusA] - dependencyStatusRank[statusB];
      if (statusDelta) return statusDelta;
      const activityDelta = Date.parse(String(b.updated_at || "")) - Date.parse(String(a.updated_at || ""));
      if (Number.isFinite(activityDelta) && activityDelta) return activityDelta;
      return String(a.team || "").localeCompare(String(b.team || ""));
    });
  for (const picker of document.querySelectorAll("[data-dependency-team-picker]")) {
    const selected = new Set(
      [...picker.querySelectorAll('input[name="dependency_team"]:checked')].map(input => input.value),
    );
    const byTeam = new Map();
    for (const task of dependencyTasks) {
      const team = String(task.team || "").trim();
      if (!team || byTeam.has(team)) continue;
      byTeam.set(team, {
        status: String(task.status || "").toUpperCase(),
        updatedAt: String(task.updated_at || ""),
      });
    }
    for (const team of selected) {
      if (byTeam.has(team)) continue;
      const task = workflowTasks
        .filter(item => String(item.team || "") === team)
        .sort((a, b) => Date.parse(String(b.updated_at || "")) - Date.parse(String(a.updated_at || "")))[0];
      byTeam.set(team, {
        status: task ? `${String(task.status || "UNKNOWN").toUpperCase()} · saved` : "saved",
        updatedAt: String(task?.updated_at || ""),
      });
    }
    const entries = [...byTeam.entries()];
    const signature = JSON.stringify(entries);
    if (picker.dataset.signature === signature) continue;
    const groups = ["RUNNING", "BLOCKED", "PAUSED"].map(status => {
      const group = document.createElement("section");
      group.className = "dependency-team-group";
      group.dataset.dependencyStateGroup = status;

      const header = document.createElement("label");
      header.className = "dependency-team-group-header";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.dataset.dependencyStateToggle = status;
      const title = document.createElement("strong");
      title.textContent = status[0] + status.slice(1).toLowerCase();
      header.append(toggle, title);

      const row = document.createElement("div");
      row.className = "dependency-team-group-row";
      const matching = entries.filter(([, meta]) => meta.status === status);
      for (const [team, meta] of matching) {
        const label = document.createElement("label");
        label.className = "dependency-team-option";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = "dependency_team";
        input.value = team;
        input.checked = selected.has(team);
        const body = document.createElement("span");
        body.className = "dependency-team-copy";
        const name = document.createElement("strong");
        name.textContent = team;
        const metaLine = document.createElement("span");
        metaLine.className = "dependency-team-meta";
        const activity = meta.updatedAt ? new Date(meta.updatedAt).toLocaleString() : "No recent activity";
        metaLine.textContent = activity;
        body.append(name, metaLine);
        label.append(input, body);
        row.append(label);
      }
      toggle.checked = matching.length > 0 && matching.every(([team]) => selected.has(team));
      toggle.indeterminate = matching.some(([team]) => selected.has(team)) && !toggle.checked;
      toggle.disabled = matching.length === 0;
      group.append(header, row);
      return group;
    });
    const hiddenSelections = entries
      .filter(([team, meta]) => selected.has(team) && !["RUNNING", "BLOCKED", "PAUSED"].includes(meta.status))
      .map(([team]) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "dependency_team";
        input.value = team;
        return input;
      });
    picker.replaceChildren(...groups, ...hiddenSelections);
    picker.dataset.signature = signature;
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

function setConfiguredBrowserEndpoint(select, endpointId) {
  const configured = String(endpointId || "");
  select.dataset.configuredEndpointId = configured;
  select.dataset.initialEndpointId = configured;
  syncCreateBrowserOptions(select, state);
  select.value = configured;
}

function addEditedBrowserEndpoint(body, select, values) {
  const selected = String(values.get("browser_endpoint_id") || "").trim();
  const initial = String(select.dataset.initialEndpointId || "");
  if (selected !== initial) body.browser_endpoint_id = selected || null;
}

function resetAgentEditor() {
  const form = roots.agentForm;
  form.reset();
  form.elements.agent_kind.value = "new";
  form.elements.agent_id.value = "";
  form.elements.is_system.value = "false";
  form.elements.independent.disabled = false;
  form.elements.independent.checked = false;
  form.elements.temporary_chat.checked = true;
  form.elements.temporary_chat.closest("label").hidden = false;
  form.elements.trigger_type.value = "manual";
  form.elements.max_cycles.value = "0";
  setConfiguredBrowserEndpoint(roots.agentBrowserEndpointSelect, "");
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
  form.elements.temporary_chat.closest("label").hidden = true;
  roots.deleteAgent.hidden = Boolean(agent.is_system || agent.is_builtin);
  if (kind === "independent") {
    const settings = agent.trigger_settings || {};
    rememberTriggerSettings(form, settings);
    form.elements.trigger_type.value = triggerTypeFromSettings(settings);
    form.elements.max_cycles.value = Number.isFinite(Number(agent.max_cycles)) ? String(Number(agent.max_cycles)) : "0";
    setConfiguredBrowserEndpoint(roots.agentBrowserEndpointSelect, agent.browser_endpoint_id);
    form.elements.interval_minutes.value = settings.interval_minutes || "";
    for (const input of form.querySelectorAll('input[name="dependency_team"]')) {
      input.checked = (settings.teams || []).includes(input.value);
    }
    syncDependencyGroupToggles(form);
    form.elements.trigger_role.value = (settings.role_completed || [])[0] || "";
    form.elements.trigger_state.value = (settings.states || [])[0] || "";
  } else {
    form.elements.trigger_type.value = "manual";
    form.elements.max_cycles.value = "0";
    setConfiguredBrowserEndpoint(roots.agentBrowserEndpointSelect, "");
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
  syncSoloRoleOptions();
  syncTeamStartRoleOptions();
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
  setConfiguredBrowserEndpoint(roots.agentSettingsBrowserEndpointSelect, detail.agent?.browser_endpoint_id);
  form.elements.trigger_type.value = triggerTypeFromSettings(settings);
  form.elements.interval_minutes.value = settings.interval_minutes || "";
  for (const input of form.querySelectorAll('input[name="dependency_team"]')) {
    input.checked = (settings.teams || []).includes(input.value);
  }
  syncDependencyGroupToggles(form);
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

function selectedDetailTab(current) {
  return current.selectedIndependentTabByTask.get(current.selectedTaskId) || "overview";
}

function liveScopeKey(taskId, role) {
  return `${taskId}:${role || "all"}`;
}

function roleFocusMode(current, taskId = current.selectedTaskId) {
  return taskId ? (current.roleFocusModeByTask.get(taskId) || "FOLLOW") : "FOLLOW";
}

function selectedLiveState(current) {
  const taskId = current.selectedTaskId;
  if (!taskId) return {};
  const scope = liveScopeKey(taskId, selectedRole(current));
  const view = current.liveEventsByScope.get(scope);
  const status = current.liveEventsStatusByScope.get(scope);
  const error = current.liveEventsErrorByScope.get(scope);
  if (view && !Array.isArray(view)) {
    return {...view, status: status || view.status || "idle", error: error || view.error || null};
  }
  return {
    items: Array.isArray(view) ? view : [],
    status: status || "idle",
    error: error || null,
  };
}

function liveStreamScope(current = state) {
  const taskId = current.selectedTaskId;
  if (!taskId) return null;
  const task = current.board.get(taskId)
    || (current.selectedDetail?.task_id === taskId ? current.selectedDetail : null);
  const role = selectedRole(current);
  if (!shouldLiveStream({
    hidden: document.hidden,
    task,
    selectedTab: selectedDetailTab(current),
    focusMode: roleFocusMode(current, taskId),
    selectedRole: role,
  })) return null;
  const query = role ? `?role=${encodeURIComponent(role)}` : "";
  const snapshotQuery = role
    ? `?limit=300&role=${encodeURIComponent(role)}`
    : "?limit=300";
  return {
    key: liveScopeKey(taskId, role),
    url: `/api/tasks/${encodeURIComponent(taskId)}/live/stream${query}`,
    snapshotUrl: `/api/tasks/${encodeURIComponent(taskId)}/live${snapshotQuery}`,
  };
}

function syncLiveStream() {
  const scope = liveStreamScope(state);
  liveController.sync(scope);
  return scope;
}

function enterLiveView() {
  const scope = syncLiveStream();
  if (!scope && selectedDetailTab(state) === "live") loadLiveAudit();
}

function renderPageRail(current) {
  if (!roots.taskRail) return;
  roots.taskRail.hidden = !current.selectedTaskId;
  const activeTarget = current.selectedTaskJumpByTask.get(current.selectedTaskId)
    || selectedDetailTab(current);
  for (const node of document.querySelectorAll("[data-task-jump]")) {
    const active = node.dataset.taskJump === activeTarget;
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "location");
    else node.removeAttribute("aria-current");
  }
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
  const wasOpen = Boolean(existing?.querySelector("details")?.open);
  const section = existing || document.createElement("section");
  section.dataset.bootstrapContext = "";
  section.dataset.signature = signature;
  section.className = "detail-section detail-card bootstrap-context-card";
  const disclosure = document.createElement("details");
  disclosure.className = "bootstrap-context-disclosure";
  disclosure.open = wasOpen;
  const summary = document.createElement("summary");
  const title = document.createElement("strong");
  title.textContent = `Context · ${context.name || "Fresh context"}`;
  const hint = document.createElement("span");
  hint.className = "muted";
  hint.textContent = context.bootstrap_id ? `Bootstrap ${context.bootstrap_id}` : "Fresh context";
  summary.append(title, hint);
  const body = document.createElement("div");
  body.className = "bootstrap-context-body";
  const meta = document.createElement("p");
  meta.textContent = context.bootstrap_id ? `Bootstrap ${context.bootstrap_id}` : "Fresh context";
  const roles = document.createElement("p");
  roles.textContent = Object.entries(context.roles || {}).map(([role, value]) => {
    const fallback = value?.fallback && value.fallback !== "none" ? ` · ${value.fallback}` : "";
    return `${role}: ${value?.source || "pending"}${fallback}`;
  }).join(" · ");
  body.append(meta, roles);
  disclosure.append(summary, body);
  section.replaceChildren(disclosure);
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
    selectedDetailTab(current),
    selectedReportByTask.get(current.selectedTaskId) || null,
    reportBodies,
    current.agentReportsRevision,
    selectedLiveState(current),
    current.runtime?.browser?.endpoints || [],
  );
  renderPageRail(current);
  renderBootstrapContext(current.selectedDetail);
  renderCommands(current);
  renderRuntime(roots.services, current);
  renderBrowserScope(roots.browserScope, current);
  renderBrowserManager(roots.browserManager, current);
  syncCreateBrowserOptions(roots.browserEndpointSelect, current);
  syncCreateBrowserOptions(roots.agentBrowserEndpointSelect, current);
  syncCreateBrowserOptions(roots.agentSettingsBrowserEndpointSelect, current);
  syncBootstrapInlineFields();
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

function selectTask(taskId, cached = null, preferredRole = null) {
  const taskChanged = state.selectedTaskId !== taskId;
  const summary = state.board.get(taskId);
  const followRole = preferredRole
    || (summary?.status === "RUNNING" ? summary.active_role : null);
  if (taskChanged) selectedReportByTask.clear();
  commit(current => {
    current.selectedTaskId = taskId;
    if (taskChanged || !current.selectedTaskJumpByTask.has(taskId)) {
      current.selectedTaskJumpByTask.set(taskId, "overview");
    }
    if (followRole) {
      current.selectedRoleByTask.set(taskId, followRole);
      current.roleFocusModeByTask.set(taskId, "FOLLOW");
    }
    current.selectedDetail = cached;
    current.selectedDetailStatus = cached ? "ready" : "loading";
    current.selectedDetailError = null;
    current.timeline = cached?.timeline ? [...cached.timeline] : [];
    current.timelineCursor = null;
  });
  syncLiveStream();
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
    const follow = roleFocusMode(current, taskId) === "FOLLOW";
    if (follow && detail.status === "RUNNING" && detail.active_role) {
      current.selectedRoleByTask.set(taskId, detail.active_role);
    } else if (!existing || (!available[existing] && !(detail.roles || []).some(role => role.logical_role === existing))) {
      const fallback = detail.active_role || Object.keys(available)[0] || detail.roles?.[0]?.logical_role || null;
      if (fallback) current.selectedRoleByTask.set(taskId, fallback);
    }
  });
  if (state.selectedIndependentTabByTask.get(taskId) === "reports") loadReports(detail);
  if (state.selectedIndependentTabByTask.get(taskId) === "live") enterLiveView();
  else syncLiveStream();
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
      const selected = current.selectedTaskId
        ? current.board.get(current.selectedTaskId)
        : null;
      if (
        selected?.status === "RUNNING"
        && selected.active_role
        && roleFocusMode(current, selected.task_id) === "FOLLOW"
      ) {
        current.selectedRoleByTask.set(selected.task_id, selected.active_role);
      }
      pruneDetailCache(current.board.keys());
    });
    syncLiveStream();
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

export async function loadLiveAudit() {
  const taskId = state.selectedTaskId;
  const detail = state.selectedDetail;
  if (!taskId || !detail || detail.task_mode === "independent" || selectedDetailTab(state) !== "live") return null;
  const role = selectedRole(state);
  const suffix = role ? `?limit=300&role=${encodeURIComponent(role)}` : "?limit=300";
  const scope = liveScopeKey(taskId, role);
  const key = `live:${scope}`;
  if (!state.liveEventsByScope.has(scope)) {
    commit(current => {
      current.liveEventsStatusByScope.set(scope, "loading");
      current.liveEventsErrorByScope.delete(scope);
    });
  }
  try {
    const response = await client.request(key, `/api/tasks/${encodeURIComponent(taskId)}/live${suffix}`);
    if (state.selectedTaskId !== taskId || selectedDetailTab(state) !== "live") return response.data;
    const reducer = createLiveReducer();
    reducer.applySnapshot(response.data.items || []);
    const view = reducer.viewModel();
    commit(current => {
      current.liveEventsByScope.set(scope, view);
      current.liveEventsStatusByScope.set(scope, "ready");
      current.liveEventsErrorByScope.delete(scope);
    });
    return response.data;
  } catch (error) {
    if (state.selectedTaskId === taskId) {
      commit(current => {
        current.liveEventsStatusByScope.set(scope, "error");
        current.liveEventsErrorByScope.set(scope, error.message);
      });
    }
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
        if (["browser_endpoint_add", "browser_endpoint_update", "browser_endpoint_remove", "browser_endpoint_start", "browser_endpoint_stop"].includes(command.kind)) {
          if (response.data.status === "applied") {
            const endpointId = response.data.result?.endpoint_id;
            const form = command.kind === "browser_endpoint_add"
              ? roots.browserManager.querySelector("[data-browser-add]")
              : roots.browserManager.querySelector(`[data-browser-settings="${CSS.escape(endpointId || "")}"]`);
            if (form) {
              delete form.dataset.dirty;
              if (command.kind === "browser_endpoint_add") form.reset();
            }
            delete roots.browserManager.dataset.browserSignature;
          }
          await loadRuntime();
        }
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

let changeUrlDetail = null;

function refreshChangeUrlRole() {
  const form = roots.changeUrlForm;
  const roleKey = String(form.elements.role.value || "").toUpperCase();
  const role = (changeUrlDetail?.roles || []).find(item => item.logical_role === roleKey);
  const endpointId = String(
    role?.browser_endpoint_id
    || changeUrlDetail?.requested_browser_endpoint_id
    || changeUrlDetail?.browser_endpoint_id || "",
  ).trim();
  const browser = (state.runtime?.browser?.endpoints || [])
    .find(item => item.endpoint_id === endpointId);
  const name = browser?.label || endpointId || "Endpoint not assigned";
  form.elements.expected_browser_endpoint_id.value = endpointId;
  form.elements.current_url.value = role?.chat_url || role?.page_url || "";
  document.querySelector("#change-url-endpoint-name").textContent = name;
  document.querySelector("#change-url-endpoint-id").textContent = endpointId
    ? "Endpoint ID: " + endpointId
    : "No fixed endpoint";
  document.querySelector("#change-url-endpoint-address").textContent = browser?.cdp_url
    ? "CDP: " + browser.cdp_url
    : "Endpoint details unavailable; verify the selected task's browser.";
  const error = document.querySelector("#change-url-validation");
  const missing = !role || !endpointId || changeUrlDetail?.endpoint_pinned === false;
  error.hidden = !missing;
  error.textContent = missing ? "This role has no pinned endpoint. Change URL is unavailable." : "";
  form.querySelector('button[type="submit"]').disabled = missing;
}

function openChangeUrl(detail) {
  if (!detail || detail.task_mode === "independent"
      || ["DONE", "STOPPED"].includes(detail.status)
      || detail.conversation_mode === "temporary") return;
  const form = roots.changeUrlForm;
  changeUrlDetail = detail;
  form.elements.task_id.value = detail.task_id;
  form.elements.expected_task_version.value = String(detail.version || 0);
  const roles = (detail.roles || []).map(item => item.logical_role).filter(Boolean);
  form.elements.role.replaceChildren(...roles.map(role => new Option(role, role)));
  const initial = selectedRole(state) || detail.active_role || roles[0];
  form.elements.role.value = roles.includes(initial) ? initial : roles[0] || "";
  form.elements.url.value = "";
  refreshChangeUrlRole();
  roots.changeUrlDialog.showModal();
  form.elements.url.focus();
}

function closeChangeUrl() {
  if (roots.changeUrlDialog.open) roots.changeUrlDialog.close();
  changeUrlDetail = null;
}

function openBrowserManager(focusAdd = false) {
  if (!roots.browserDialog.open) roots.browserDialog.showModal();
  if (focusAdd) requestAnimationFrame(() => roots.browserManager.querySelector('[data-browser-add] input[name="address"]')?.focus());
}

function closeBrowserManager() {
  if (roots.browserDialog.open) roots.browserDialog.close();
}

function openCreate() {
  if (!state.modalOpen) pushOverlay("modal");
  commit(current => { current.modalOpen = true; });
  showCreateValidation();
  syncCreateMode();
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
    const preferredRole = summary?.status === "RUNNING" ? summary.active_role : null;
    selectTask(taskId, cached, preferredRole);
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

roots.detail.addEventListener("click", async event => {
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
  const changeRoleUrl = event.target.closest("[data-change-role-url]");
  if (changeRoleUrl) {
    openChangeUrl(state.selectedDetail);
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
    commit(current => {
      current.selectedIndependentTabByTask.set(taskId, tab);
      current.selectedTaskJumpByTask.set(taskId, tab);
    });
    if (tab === "reports") loadReports(state.selectedDetail);
    if (tab === "live") enterLiveView();
    else syncLiveStream();
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
  const copyRoleUrl = event.target.closest("[data-copy-role-url]");
  if (copyRoleUrl) {
    try {
      await navigator.clipboard.writeText(copyRoleUrl.dataset.copyRoleUrl);
      const originalLabel = copyRoleUrl.textContent;
      copyRoleUrl.textContent = "Copied ✓";
      copyRoleUrl.disabled = true;
      window.setTimeout(() => {
        if (!copyRoleUrl.isConnected) return;
        copyRoleUrl.textContent = originalLabel;
        copyRoleUrl.disabled = false;
      }, 1400);
      toast(`Copied ${copyRoleUrl.dataset.role || "role"} URL`);
    } catch (error) {
      toast(`Copy failed: ${error.message}`);
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
  const followActive = event.target.closest("[data-follow-active-role]");
  if (followActive) {
    const taskId = followActive.dataset.taskId;
    const activeRole = state.selectedDetail?.active_role || state.board.get(taskId)?.active_role;
    commit(current => {
      current.roleFocusModeByTask.set(taskId, "FOLLOW");
      if (activeRole) current.selectedRoleByTask.set(taskId, activeRole);
    });
    enterLiveView();
    return;
  }
  const role = event.target.closest("[data-role-select]");
  if (role) {
    selectedReportByTask.delete(role.dataset.taskId);
    commit(current => {
      current.selectedRoleByTask.set(role.dataset.taskId, role.dataset.roleSelect);
      current.roleFocusModeByTask.set(role.dataset.taskId, "PINNED");
    });
    if (selectedDetailTab(state) === "live") enterLiveView();
    else syncLiveStream();
    return;
  }
  const jumpLatest = event.target.closest("[data-live-jump-latest]");
  if (jumpLatest) {
    roots.detail.scrollTop = roots.detail.scrollHeight;
    jumpLatest.hidden = true;
    return;
  }
  const control = event.target.closest("[data-control]");
  if (control) {
    const taskId = control.dataset.taskId;
    const detail = state.selectedDetail;
    const isReset = control.dataset.control === "reset";
    const challengeApproval = control.dataset.challengeApproval === "true";
    const duplicateWarning = "The original Send may already have succeeded. Approving creates one new generation and risks duplicate output. Continue?";
    if (challengeApproval && !window.confirm(duplicateWarning)) return;
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
        confirmed: challengeApproval || ["stop", "clear_team", "archive_worktree"].includes(control.dataset.control),
        ...(challengeApproval ? {reason: "Operator acknowledged duplicate Send risk: original Send may have succeeded"} : {}),
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

document.addEventListener("click", async event => {
  const pageJump = event.target.closest("[data-page-jump]");
  if (pageJump) {
    document.getElementById(pageJump.dataset.pageJump)?.scrollIntoView({behavior: "smooth", block: "start"});
    return;
  }
  const taskJump = event.target.closest("[data-task-jump]");
  if (taskJump && state.selectedTaskId) {
    const target = taskJump.dataset.taskJump;
    const tab = target === "live" ? "live" : target === "reports" ? "reports" : "overview";
    commit(current => {
      current.selectedIndependentTabByTask.set(current.selectedTaskId, tab);
      current.selectedTaskJumpByTask.set(current.selectedTaskId, target);
    });
    if (tab === "reports") loadReports(state.selectedDetail);
    if (tab === "live") enterLiveView();
    else syncLiveStream();
    requestAnimationFrame(() => {
      const id = target === "roles" ? "task-roles"
        : target === "timeline" ? "task-timeline"
          : target === "live" ? "task-live"
            : target === "reports" ? "task-reports" : "task-overview";
      document.getElementById(id)?.scrollIntoView({behavior: "smooth", block: "start"});
    });
    return;
  }
  const browserScope = event.target.closest("[data-browser-scope]");
  if (browserScope) {
    commit(current => { current.selectedEndpointId = browserScope.dataset.browserScope; });
    return;
  }
  if (event.target.closest("[data-open-browser-add]")) {
    openBrowserManager(true);
    return;
  }
  if (event.target.closest("[data-open-browser-manager]")) {
    openBrowserManager(false);
    return;
  }
  if (event.target.closest("[data-close-browser-manager]")) {
    closeBrowserManager();
    return;
  }
  const browserAction = event.target.closest("[data-browser-start], [data-browser-stop], [data-browser-remove]");
  if (browserAction) {
    const operation = browserAction.dataset.browserStart ? "start"
      : browserAction.dataset.browserStop ? "stop" : "remove";
    const endpointId = browserAction.dataset.browserStart
      || browserAction.dataset.browserStop || browserAction.dataset.browserRemove;
    await queueCommand({
      kind: `browser_endpoint_${operation}`,
      taskId: `browser:${endpointId}`,
      endpoint: `/api/browser-endpoints/${encodeURIComponent(endpointId)}/${operation}`,
      body: {},
      label: `${operation} browser ${endpointId}`,
    });
    return;
  }
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

roots.browserManager.addEventListener("input", event => {
  const form = event.target.closest("form");
  if (form) form.dataset.dirty = "true";
});
roots.browserEndpointSelect.addEventListener("change", syncBootstrapInlineFields);
roots.form.elements.temporary.addEventListener("change", syncBootstrapInlineFields);

roots.browserManager.addEventListener("submit", async event => {
  const form = event.target.closest("form");
  if (!form) return;
  event.preventDefault();
  const values = new FormData(form);
  if (form.matches("[data-browser-add]")) {
    const mode = String(values.get("management_mode") || "external");
    const body = {
      address: String(values.get("address") || "").trim(),
      management_mode: mode,
      max_active_tabs: Number(values.get("max_active_tabs") || 2),
      temporary_default: values.get("temporary_default") === "on",
    };
    const label = String(values.get("label") || "").trim();
    const rateLimitScope = String(values.get("rate_limit_scope") || "").trim();
    if (label) body.label = label;
    if (rateLimitScope) body.rate_limit_scope = rateLimitScope;
    if (mode === "managed-local") {
      body.profile_path = String(values.get("profile_path") || "").trim();
      const executablePath = String(values.get("executable_path") || "").trim();
      if (executablePath) body.executable_path = executablePath;
      const cloneFrom = String(values.get("clone_from_endpoint_id") || "").trim();
      if (cloneFrom) body.clone_from_endpoint_id = cloneFrom;
      if (values.get("start") === "on") body.start = true;
    }
    await queueCommand({
      kind: "browser_endpoint_add",
      taskId: `browser:add:${body.address}`,
      endpoint: "/api/browser-endpoints",
      body,
      label: `Add browser ${body.address}`,
    });
    return; // Keep the draft until the probe/registration command actually succeeds.
  }
  const endpointId = form.dataset.browserSettings;
  if (!endpointId) return;
  const body = {
    max_active_tabs: Number(values.get("max_active_tabs") || 2),
    ...(values.get("is_default") === "on" ? {is_default: true} : {}),
    temporary_default: values.get("temporary_default") === "on",
  };
  const label = String(values.get("label") || "").trim();
  const rateLimitScope = String(values.get("rate_limit_scope") || "").trim();
  if (label) body.label = label;
  if (rateLimitScope) body.rate_limit_scope = rateLimitScope;
  await queueCommand({
    kind: "browser_endpoint_update",
    taskId: `browser:${endpointId}`,
    endpoint: `/api/browser-endpoints/${encodeURIComponent(endpointId)}/settings`,
    body,
    label: `Update browser ${endpointId}`,
  });
});

function parseGoverningDirectives(value) {
  const parsed = JSON.parse(String(value ?? ""));
  if (!Array.isArray(parsed)) throw new Error("Governing directives must be a JSON array.");
  return parsed;
}

roots.form.addEventListener("submit", event => {
  event.preventDefault();
  syncCreateMode();
  showCreateValidation();
  if (!roots.form.reportValidity()) {
    showCreateValidation("Complete the required task details before queueing.");
    return;
  }
  const values = new FormData(roots.form);
  const mode = createWorkflowMode();
  const conversationUrl = String(values.get("conversation_url") || "").trim();
  const existingSolo = mode === "solo" && Boolean(conversationUrl);
  const task = String(values.get("task") || "");
  if (!task.trim() && !existingSolo) {
    showCreateValidation("Task details are required.");
    return;
  }
  const requestedTeam = String(values.get("requested_team") || "").trim();
  const browserEndpointId = String(values.get("browser_endpoint_id") || "").trim();
  let body;
  if (mode === "solo") {
    const role = String(values.get("solo_role") || "").trim();
    if (!role || !browserEndpointId) {
      showCreateValidation("Solo Task requires one role and one browser endpoint.");
      return;
    }
    body = {
      workflow_mode: "solo",
      role,
      goal: String(values.get("goal") || ""),
      browser_endpoint_id: browserEndpointId,
    };
    if (existingSolo) body.conversation_url = conversationUrl;
    else {
      body.task = task;
      if (requestedTeam) body.requested_team = requestedTeam;
    }
  } else {
    const manualDependencies = String(values.get("depends_on_task_ids") || "")
      .split(",").map(value => value.trim()).filter(Boolean);
    const dependencies = [...new Set([
      ...selectedDependencyIds(roots.dependencyOptions),
      ...manualDependencies,
    ])];
    const reuseTeam = String(values.get("reuse_team") || "").trim();
    let governingDirectives;
    try {
      governingDirectives = parseGoverningDirectives(values.get("governing_directives"));
    } catch (error) {
      showCreateValidation(error.message);
      return;
    }
    body = {
      task,
      governing_objective: String(values.get("governing_objective") || ""),
      governing_directives: governingDirectives,
      repository: String(values.get("repository") || "").trim() || null,
      depends_on_task_ids: dependencies,
    };
    body.roles = selectedWorkflowRoles();
    const startRole = String(values.get("initial_role") || "PLAN").trim().toUpperCase();
    const roleUrls = {};
    const putUrl = (role, url) => {
      if (!body.roles.includes(role)) throw new Error(`Select workflow role ${role} before attaching its URL.`);
      if (roleUrls[role]) throw new Error(`Duplicate URL assignment for ${role}.`);
      roleUrls[role] = url;
    };
    try {
      const startUrl = String(values.get("initial_role_url") || "").trim();
      if (!body.roles.includes(startRole)) throw new Error("Initial role must be selected.");
      if (startUrl) putUrl(startRole, startUrl);
      for (const line of String(values.get("role_url_lines") || "").split(/\r?\n/)) {
        if (!line.trim()) continue;
        const sep = line.indexOf("=");
        if (sep < 1) throw new Error("Role URLs must use ROLE=https://chatgpt.com/.../c/...");
        putUrl(line.slice(0, sep).trim().toUpperCase(), line.slice(sep + 1).trim());
      }
    } catch (error) {
      showCreateValidation(error.message);
      return;
    }
    body.initial_role = startRole;
    if (Object.keys(roleUrls).length) {
      if (!browserEndpointId) {
        showCreateValidation("Choose a browser endpoint before attaching an existing conversation.");
        return;
      }
      if (values.get("temporary") === "on") {
        showCreateValidation("Existing conversations cannot use Temporary chat.");
        return;
      }
      body.role_conversation_urls = roleUrls;
    }
    if (browserEndpointId) body.browser_endpoint_id = browserEndpointId;
    if (values.get("temporary") === "on") body.temporary = true;
    if (reuseTeam) body.reuse_team = reuseTeam;
    else if (requestedTeam) body.requested_team = requestedTeam;
  }
  queueCommand({
    kind: "create_task",
    taskId: "new",
    endpoint: "/api/tasks",
    body,
    label: task.split("\n")[0] || `Solo ${String(values.get("solo_role") || "task").trim()}`,
  });
  roots.form.reset();
  resetBootstrapSelection();
  roots.requestedTeam.disabled = false;
  applyReuseRoleSelection(roots.roleInputs, null, {reset: true});
  syncCreateMode();
  closeCreate();
});

roots.form.addEventListener("input", event => {
  if (event.target.matches('input[name="conversation_url"], input[name="roles"]')) syncCreateMode();
  if (event.target.matches('input[name="roles"], input[name="initial_role_url"], textarea[name="role_url_lines"], select[name="initial_role"], input[name="depends_on_task_ids"], textarea[name="task"], input[name="requested_team"], select[name="solo_role"], select[name="browser_endpoint_id"], input[name="conversation_url"]')) {
    showCreateValidation();
    updateCreateSummary();
  }
});
for (const input of roots.workflowModeInputs) {
  input.addEventListener("change", () => {
    showCreateValidation();
    syncCreateMode();
  });
}
roots.soloRole.addEventListener("change", updateCreateSummary);
roots.form.addEventListener("change", event => {
  if (event.target.matches('input[name="roles"]')) syncTeamStartRoleOptions();
  if (event.target.matches('select[name="initial_role"]')) updateCreateSummary();
});
roots.bootstrapSelect.addEventListener("change", () => {
  syncBootstrapInlineFields();
  showCreateValidation();
  updateCreateSummary();
});
roots.dependencyOptions.addEventListener("change", updateCreateSummary);
roots.workflowAgentOptions.addEventListener("change", updateCreateSummary);

document.addEventListener("change", event => {
  const browserMode = event.target.closest('[data-browser-add] select[name="management_mode"]');
  if (browserMode) {
    const managed = browserMode.closest("form")?.querySelector("[data-managed-browser-fields]");
    if (managed) managed.hidden = browserMode.value !== "managed-local";
    return;
  }
  const stateToggle = event.target.closest("[data-dependency-state-toggle]");
  if (stateToggle) {
    const group = stateToggle.closest("[data-dependency-state-group]");
    for (const input of group?.querySelectorAll('input[name="dependency_team"]') || []) {
      input.checked = stateToggle.checked;
    }
    stateToggle.indeterminate = false;
    return;
  }
  const dependencyInput = event.target.closest('input[name="dependency_team"]');
  if (dependencyInput) {
    const group = dependencyInput.closest("[data-dependency-state-group]");
    const toggle = group?.querySelector("[data-dependency-state-toggle]");
    const inputs = [...(group?.querySelectorAll('input[name="dependency_team"]') || [])];
    if (toggle && inputs.length) {
      toggle.checked = inputs.every(input => input.checked);
      toggle.indeterminate = inputs.some(input => input.checked) && !toggle.checked;
    }
  }
});

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
        temporary_chat: values.has("temporary_chat"),
        max_cycles: Math.max(0, Number(values.get("max_cycles") || 0)),
        trigger_settings: basicTriggerSettings(values),
        browser_endpoint_id: String(values.get("browser_endpoint_id") || "").trim() || null,
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
    const body = {
      display_name: name,
      system_prompt: systemPrompt,
      max_cycles: Math.max(0, Number(values.get("max_cycles") || 0)),
      trigger_settings: configuredTriggerSettings(roots.agentForm, values),
    };
    addEditedBrowserEndpoint(body, roots.agentBrowserEndpointSelect, values);
    queueCommand({
      kind: "update_independent_agent",
      taskId: id,
      endpoint: `/api/independent-agents/${encodeURIComponent(id)}/settings`,
      body,
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
  addEditedBrowserEndpoint(body, roots.agentSettingsBrowserEndpointSelect, values);
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

roots.changeUrlForm.elements.role.addEventListener("change", () => {
  roots.changeUrlForm.elements.url.value = "";
  refreshChangeUrlRole();
});
roots.changeUrlForm.addEventListener("submit", async event => {
  event.preventDefault();
  const form = roots.changeUrlForm;
  const taskId = form.elements.task_id.value;
  const role = form.elements.role.value;
  const url = String(form.elements.url.value || "").trim();
  const endpointId = form.elements.expected_browser_endpoint_id.value;
  const info = document.querySelector("#change-url-validation");
  try {
    const parsed = new URL(url);
    const correctHost = parsed.protocol === "https:"
      && ["chatgpt.com", "www.chatgpt.com"].includes(parsed.hostname);
    const correctPath = parsed.pathname === "/"
      || new RegExp("^/(?:g/[^/]+/)?c/[0-9a-fA-F-]{36}$").test(parsed.pathname);
    if (!correctHost || !correctPath || parsed.search || parsed.hash) {
      throw new Error("Paste an exact HTTPS ChatGPT conversation URL ending in /c/<UUID>.");
    }
    if (!endpointId) throw new Error("Selected role has no fixed browser endpoint.");
  } catch (error) {
    info.textContent = error.message;
    info.hidden = false;
    return;
  }
  info.hidden = true;
  const pending = await queueCommand({
    kind: "change_role_url",
    taskId,
    endpoint: `/api/tasks/${encodeURIComponent(taskId)}/role-url`,
    body: {
      role, url,
      expected_browser_endpoint_id: endpointId,
      expected_task_version: Number(form.elements.expected_task_version.value),
    },
    label: `Change URL ${role} - ${changeUrlDetail?.team || taskId}`,
  });
  if (pending?.status === "failed") return;
  closeChangeUrl();
});
for (const close of document.querySelectorAll("[data-close-change-url]")) {
  close.addEventListener("click", closeChangeUrl);
}
roots.changeUrlDialog.addEventListener("click", event => {
  if (event.target === roots.changeUrlDialog) closeChangeUrl();
});
roots.changeUrlDialog.addEventListener("cancel", event => {
  event.preventDefault();
  closeChangeUrl();
});

roots.changeGoalForm.addEventListener("submit", event => {
  event.preventDefault();
  const taskId = roots.changeGoalForm.elements.task_id.value;
  const goal = roots.changeGoalForm.elements.goal.value;
  const governingObjective = roots.changeGoalForm.elements.governing_objective.value;
  const expected = Number(roots.changeGoalForm.elements.expected_task_version.value);
  if (!goal.trim()) { toast("Goal must not be blank."); return; }
  let governingDirectives;
  try {
    governingDirectives = parseGoverningDirectives(
      roots.changeGoalForm.elements.governing_directives.value,
    );
  } catch (error) {
    toast(error.message);
    return;
  }
  queueCommand({
    kind: "change_goal",
    taskId,
    endpoint: `/api/tasks/${encodeURIComponent(taskId)}/goal`,
    body: {
      goal,
      governing_objective: governingObjective,
      governing_directives: governingDirectives,
      expected_task_version: expected,
    },
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

roots.browserDialog.addEventListener("click", event => {
  if (event.target === roots.browserDialog) closeBrowserManager();
});
roots.browserDialog.addEventListener("cancel", event => {
  event.preventDefault();
  closeBrowserManager();
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
document.addEventListener("visibilitychange", () => {
  if (document.hidden) syncLiveStream();
  else enterLiveView();
});
window.addEventListener("beforeunload", () => {
  liveController.close();
  clearInterval(elapsedTimer);
  clearTimeout(viewSaveTimer);
  persistViewState();
  poller.destroy();
  systemPoller.destroy();
});
