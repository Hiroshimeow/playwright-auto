function el(tag, value, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = value;
  return node;
}

function button(action, label, task, {disabled = false, emphasized = false, reason = null} = {}) {
  const node = el("button", label);
  node.type = "button";
  node.dataset.control = action;
  node.dataset.taskId = task.task_id;
  node.dataset.version = String(task.version || 0);
  node.dataset.renderDisabled = String(Boolean(disabled));
  node.disabled = Boolean(disabled);
  if (reason) {
    node.title = String(reason);
    node.dataset.disabledReason = String(reason);
  }
  if (emphasized) node.classList.add("control-emphasis");
  return node;
}

function changeGoalButton(task) {
  const node = el("button", "Change goal");
  node.type = "button";
  node.dataset.changeGoal = task.task_id;
  node.dataset.version = String(task.version || 0);
  return node;
}

function independentButton(action, label, task, {disabled = false, emphasized = false} = {}) {
  const node = el("button", label);
  node.type = "button";
  node.dataset.independentAction = action;
  node.dataset.taskId = task.task_id;
  node.dataset.version = String(task.version || 0);
  node.dataset.renderDisabled = String(Boolean(disabled));
  node.disabled = Boolean(disabled);
  if (emphasized) node.classList.add("control-emphasis");
  return node;
}

function selectedInput(detail, selectedRole) {
  const role = selectedRole || detail.active_role || null;
  return (role && detail.role_inputs?.[role]) || detail.active_input || null;
}

export function taskGoalBlocks(detail = {}) {
  const task = detail.task_text || detail.task_title || detail.task_id || "";
  const goal = detail.effective_goal || detail.task_text || detail.task_id || "";
  if (task === goal) {
    return [{key: "task-goal", label: "Task / Effective goal", text: task}];
  }
  return [
    {key: "task", label: "Task", text: task},
    {key: "effective-goal", label: "Effective goal", text: goal},
  ];
}

function reportSort(a, b) {
  const turn = Number(b.turn || 0) - Number(a.turn || 0);
  return turn || b._order - a._order;
}

export function workflowReportModel(detail = {}, selectedRole = null, selectedReportUrl = null) {
  const roles = detail.roles || [];
  const physicalRoles = new Map(roles.map(role => [role.physical_role, role.logical_role]));
  const logicalRoles = new Set(roles.map(role => role.logical_role));
  const reports = (detail.reports || []).map((report, _order) => ({
    ...report,
    _order,
    _logicalRole: report.role || physicalRoles.get(report.physical_role) || null,
  })).filter(report => report._logicalRole && logicalRoles.has(report._logicalRole));

  const roleModels = roles.map(role => {
    const roleReports = reports.filter(report => report._logicalRole === role.logical_role).sort(reportSort);
    return {
      role: role.logical_role,
      physicalRole: role.physical_role,
      latest: roleReports[0] || null,
      reports: roleReports,
    };
  });
  const latestOrders = new Set(roleModels.filter(item => item.latest).map(item => item.latest._order));
  const olderReports = reports.filter(report => !latestOrders.has(report._order)).sort(reportSort);
  const newest = [...reports].sort(reportSort)[0] || null;
  const explicit = selectedReportUrl ? reports.find(report => report.url === selectedReportUrl) : null;
  const selectedReport = explicit || (detail.status === "DONE"
    ? roleModels.find(item => item.role === "PLAN")?.latest || newest
    : roleModels.find(item => item.role === selectedRole)?.latest || newest);
  const coverage = `${roleModels.filter(item => item.latest).length}/${roleModels.length}`;
  return {roles: roleModels, olderReports, selectedReport, coverage};
}

function relativeTime(value, now = Date.now()) {
  const at = Date.parse(value || "");
  if (!Number.isFinite(at)) return "—";
  const seconds = Math.round((now - at) / 1000);
  const future = seconds < 0;
  const absolute = Math.abs(seconds);
  const [amount, unit] = absolute < 60
    ? [absolute, "s"]
    : absolute < 3600
      ? [Math.floor(absolute / 60), "m"]
      : absolute < 86400
        ? [Math.floor(absolute / 3600), "h"]
        : [Math.floor(absolute / 86400), "d"];
  return future ? `in ${amount}${unit}` : `${amount}${unit} ago`;
}

function timelineTime(value, now = Date.now()) {
  const date = new Date(value || "");
  if (!Number.isFinite(date.getTime())) return "—";
  const current = new Date(now);
  const sameDay = date.getFullYear() === current.getFullYear()
    && date.getMonth() === current.getMonth()
    && date.getDate() === current.getDate();
  const local = sameDay
    ? date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false})
    : date.toLocaleString([], {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false});
  return `${local} · ${relativeTime(value, now)}`;
}

function fixedDuration(startedAt, endedAt) {
  const started = Date.parse(startedAt || "");
  const ended = Date.parse(endedAt || "");
  if (!Number.isFinite(started) || !Number.isFinite(ended) || ended < started) return null;
  const seconds = Math.floor((ended - started) / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${seconds}s`;
}

function tags(values) {
  const list = el("div", null, "agent-tags detail-agent-tags");
  for (const value of Array.isArray(values) ? values.filter(Boolean) : []) {
    list.append(el("span", value, "agent-tag"));
  }
  return list;
}

function rolesSection(detail, selectedRole) {
  const roles = el("section", null, "detail-section");
  roles.append(el("h3", "Roles / tabs"));
  const roleList = el("div", null, "role-list");
  for (const role of detail.roles || []) {
    const row = el("button", null, `role-row${role.logical_role === selectedRole ? " selected" : ""}`);
    row.type = "button";
    row.dataset.roleSelect = role.logical_role;
    row.dataset.taskId = detail.task_id;
    row.append(el("strong", role.logical_role), el("span", role.physical_role));
    const availability = role.online == null ? "unknown" : role.online ? "online" : "offline";
    row.append(el("span", availability, availability));
    roleList.append(row);
  }
  roles.append(roleList);

  const input = selectedInput(detail, selectedRole);
  const inputSection = el("div", null, "role-input-section");
  inputSection.append(el("h3", `${input?.logical_role || selectedRole || detail.active_role || "Role"} input / handoff`));
  if (input?.input) inputSection.append(el("pre", input.input, "role-input"));
  else inputSection.append(el("p", "No input is available for this role.", "muted"));
  if (input?.handoff) inputSection.append(el("p", `Handoff: ${input.handoff}`, "handoff-label"));
  roles.append(inputSection);
  return roles;
}

function timelineSection(detail, timeline) {
  const section = el("section", null, "detail-section");
  const title = el("div", null, "section-inline");
  title.append(el("h3", "Timeline"));
  if (detail.timeline_total > timeline.length) {
    const more = el("button", "Load older");
    more.type = "button";
    more.dataset.loadTimeline = detail.task_id;
    title.append(more);
  }
  section.append(title);
  const list = el("ol", null, "timeline");
  for (const item of timeline) {
    const row = el("li");
    const time = el("time", timelineTime(item.at));
    if (item.at) {
      time.dateTime = item.at;
      time.title = item.at;
      time.dataset.timeAt = item.at;
    }
    row.append(time, el("strong", item.kind || item.level || item.status || "EVENT"));
    row.append(el("p", item.message || item.status || ""));
    list.append(row);
  }
  section.append(list);
  return section;
}

export function independentDeleteDisabled(agent = {}, active = false) {
  return Boolean(agent.is_builtin) || Boolean(active);
}

function independentControls(detail) {
  const controls = el("div", null, "control-grid independent-controls");
  const agent = detail.agent || {};
  const enabled = agent.enabled !== false;
  const active = Boolean(agent.trigger_type);
  const inFlight = ["sending", "sent", "waiting"].includes(detail.active_hop?.state);
  const tabOpen = Boolean(agent.tab_open);

  controls.append(independentButton("run-task", "Run task", detail, {disabled: active || !enabled}));
  controls.append(button(enabled ? "pause" : "resume", enabled ? "Pause" : "Enable", detail));
  controls.append(button("reset", "Reset", detail, {disabled: !active}));
  controls.append(independentButton("settings", "Settings", detail));
  controls.append(independentButton("delete", "Delete", detail, {
    disabled: independentDeleteDisabled(agent, active),
  }));
  controls.append(button("open_tab", "Open tab", detail, {disabled: tabOpen, emphasized: !tabOpen}));
  controls.append(button("close_tab", "Close tab", detail, {disabled: !tabOpen || inFlight, emphasized: tabOpen && !inFlight}));
  return controls;
}

function independentOverview(detail, timeline, selectedRole) {
  const fragment = document.createDocumentFragment();
  const agent = detail.agent || {};
  fragment.append(independentControls(detail));
  const section = el("section", null, "detail-section independent-overview");
  section.append(el("h3", "Independent Agent"));
  const prompt = agent.system_prompt || detail.task_text || "No system prompt projected.";
  section.append(el("pre", prompt, "task-text"));
  if ((agent.tags || []).length) section.append(tags(agent.tags));

  const maxTurns = Number(agent.max_cycles) === 0 ? "Unlimited" : String(agent.max_cycles ?? 0);
  const context = el("div", null, "agent-detail-grid");
  context.append(
    el("span", `Name: ${agent.name || detail.team}`),
    el("span", `Generation: ${agent.generation || 1}`),
    el("span", `Trigger: ${agent.trigger_type || "waiting"}`),
    el("span", `Target: ${agent.target_team || "—"}`),
    el("span", `Task: ${agent.target_task_id || "—"}`),
    el("span", `Max turns per job: ${maxTurns}`),
  );
  section.append(context);
  fragment.append(section);

  if (detail.primary_problem) {
    const problem = el("section", null, "problem-box");
    problem.append(el("strong", detail.primary_problem.code || detail.primary_problem.kind));
    problem.append(el("p", detail.primary_problem.message));
    fragment.append(problem);
  }

  const tabState = el("p", null, "tab-state-help");
  if (agent.tab_open) {
    const keepOpen = agent.tab_keep_open_until
      ? ` Idle keep-open until ${agent.tab_keep_open_until}.`
      : " The tab remains available for the configured idle keep-open window.";
    tabState.textContent = `Tab is open.${keepOpen} Close tab acts immediately when no send is in flight.`;
  } else {
    tabState.textContent = "Tab is closed. Open tab keeps it available for up to 10 idle minutes when the backend contract is active.";
  }
  fragment.append(tabState);
  fragment.append(rolesSection(detail, selectedRole), timelineSection(detail, timeline));
  return fragment;
}

function independentHistory(detail) {
  const section = el("section", null, "detail-section independent-history");
  const items = detail.independent_history || [];
  if (!items.length) {
    section.append(el("p", "No lifecycle records for this agent.", "muted"));
    return section;
  }
  const list = el("div", null, "history-list");
  for (const item of items) {
    const article = el("article", null, "history-card");
    article.append(el("strong", `Generation ${item.generation || "—"} · ${item.status || "UNKNOWN"}`));
    const endedAt = item.completed_at || item.stopped_at || null;
    const duration = fixedDuration(item.started_at, endedAt);
    const meta = [item.task_id, endedAt || item.updated_at || item.created_at, duration ? `Duration ${duration}` : null]
      .filter(Boolean).join(" · ");
    article.append(el("p", meta || "—", "muted"));
    const outcome = item.last_outcome;
    if (outcome?.summary || outcome?.outcome) {
      article.append(el("p", [outcome.outcome, outcome.summary].filter(Boolean).join(" · ")));
    }
    list.append(article);
  }
  section.append(list);
  return section;
}

export function reportBody(report, reportBodies) {
  if (report?.availability === "remote_unmirrored") {
    return "Report stored on remote execution host / not mirrored.";
  }
  if (report?.availability === "unavailable") {
    return "Report is unavailable on this dashboard host.";
  }
  const loaded = report?.url ? reportBodies.get(report.url) : null;
  if (loaded?.status === "ready") return loaded.body;
  if (loaded?.status === "error") return `Report load failed: ${loaded.error}`;
  if (report?.content || report?.message) return report.content || report.message;
  return "Loading report body…";
}

function reportLink(report) {
  if (!report?.url) return null;
  const link = el("a", "Open raw report");
  link.href = report.url;
  link.target = "_blank";
  link.rel = "noopener";
  return link;
}

function independentReports(detail, reportBodies) {
  const section = el("section", null, "detail-section independent-reports");
  const reports = [...(detail.reports || []), ...(detail.maintenance_reports || [])];
  if (!reports.length) {
    section.append(el("p", "No reports for this agent.", "muted"));
    return section;
  }
  const list = el("div", null, "history-list");
  for (const report of reports) {
    const article = el("article", null, "history-card");
    article.append(el("strong", report.summary || report.outcome || report.role || "Report"));
    const link = reportLink(report);
    if (link) article.append(link);
    article.append(el("pre", reportBody(report, reportBodies), "task-text"));
    list.append(article);
  }
  section.append(list);
  return section;
}

function detailTabs(detail, selectedTab, items, ariaLabel, independent = false) {
  const tabs = el("div", null, "independent-tabs detail-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", ariaLabel);
  for (const [id, label] of items) {
    const tab = el("button", label);
    tab.type = "button";
    tab.dataset.detailTab = id;
    if (independent) tab.dataset.independentTab = id;
    tab.dataset.taskId = detail.task_id;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(selectedTab === id));
    if (selectedTab === id) tab.classList.add("selected");
    tabs.append(tab);
  }
  return tabs;
}

function independentTabs(detail, selectedTab) {
  return detailTabs(
    detail,
    selectedTab,
    [["overview", "Overview"], ["history", "History"], ["reports", "Reports"]],
    "Independent agent detail",
    true,
  );
}

function workflowTabs(detail, selectedTab) {
  const model = workflowReportModel(detail);
  const tabs = detailTabs(
    detail,
    selectedTab,
    [["overview", "Overview"], ["reports", `Reports ${model.coverage}`]],
    "Workflow task detail",
  );
  tabs.classList.add("workflow-detail-tabs");
  return tabs;
}

function workflowReports(detail, selectedRole, selectedReportUrl, reportBodies) {
  const section = el("section", null, "detail-section workflow-reports");
  const model = workflowReportModel(detail, selectedRole, selectedReportUrl);
  const layout = el("div", null, "workflow-report-layout");
  const navigator = el("div", null, "report-role-nav");
  const content = el("div", null, "workflow-report-content");
  for (const item of model.roles) {
    const role = el("button", null, `report-role${item.role === model.selectedReport?._logicalRole ? " selected" : ""}`);
    role.type = "button";
    role.disabled = !item.latest;
    role.dataset.taskId = detail.task_id;
    role.append(el("strong", item.role));
    role.append(el("span", item.latest ? `Turn ${item.latest.turn ?? "—"}` : "No report"));
    if (item.latest?.url) {
      role.dataset.reportSelect = item.latest.url;
      role.dataset.reportRole = item.role;
    }
    navigator.append(role);
  }
  layout.append(navigator, content);
  section.append(layout);

  const report = model.selectedReport;
  if (!report) {
    content.append(el("p", "No workflow reports are available yet.", "muted"));
    return section;
  }

  const reader = el("article", null, "report-reader");
  const meta = [report._logicalRole || report.role, `Turn ${report.turn ?? "—"}`, report.created_at].filter(Boolean).join(" · ");
  reader.append(el("strong", meta || "Report"));
  const link = reportLink(report);
  if (link) reader.append(link);
  reader.append(el("pre", reportBody(report, reportBodies), "task-text report-body"));
  content.append(reader);

  if (model.olderReports.length) {
    const older = el("details", null, "older-reports");
    older.append(el("summary", `Older reports (${model.olderReports.length})`));
    const list = el("div", null, "older-report-list");
    for (const item of model.olderReports) {
      const select = el("button", null, "older-report");
      select.type = "button";
      select.dataset.taskId = detail.task_id;
      select.dataset.reportSelect = item.url || "";
      select.dataset.reportRole = item._logicalRole || "";
      select.append(el("strong", `${item._logicalRole} · Turn ${item.turn ?? "—"}`));
      if (item.created_at) select.append(el("span", item.created_at));
      list.append(select);
    }
    older.append(list);
    content.append(older);
  }
  return section;
}

function dependencySection(detail) {
  const dependencies = detail.dependencies || [];
  if (!dependencies.length) return null;
  const section = el("section", null, "detail-section");
  section.append(el("h3", dependencies.length === 1 ? "Parent" : "Parents"));
  const list = el("div", null, "history-list");
  for (const dependency of dependencies) {
    const row = el("article", null, "history-card");
    row.append(el("strong", dependency.team || dependency.task_id));
    const remove = el("button", "Remove");
    remove.type = "button";
    remove.dataset.removeParent = dependency.task_id;
    remove.dataset.taskId = detail.task_id;
    remove.dataset.version = String(detail.version || 0);
    row.append(remove);
    list.append(row);
  }
  section.append(list);
  return section;
}

function workflowOverview(detail, timeline, selectedRole) {
  const fragment = document.createDocumentFragment();
  const controls = el("div", null, "control-grid");
  if (detail.status === "RUNNING") controls.append(changeGoalButton(detail));
  for (const [action, label] of [
    ["pause", "Pause"], ["resume", "Resume"], ["retry", "Retry hop"],
    ["restart_role", "Restart role"], ["new_chat", "New chat"],
    ["open_tab", "Open tab"], ["route_plan", "Route PLAN"],
    ["stop", "Stop"], ["clear_team", "Clear team"],
  ]) {
    const eligibility = detail.control_eligibility?.[action] || {
      eligible: false,
      reason: "Control eligibility is unavailable; refresh task state.",
    };
    controls.append(button(action, label, detail, {
      disabled: eligibility.eligible !== true,
      reason: eligibility.reason || null,
    }));
  }
  fragment.append(controls);
  const dependencies = dependencySection(detail);
  if (dependencies) fragment.append(dependencies);

  const taskSection = el("section", null, "detail-section task-goal-section");
  for (const block of taskGoalBlocks(detail)) {
    const details = el("details", null, "task-disclosure");
    details.dataset.disclosureKey = block.key;
    const summary = el("summary", null, "task-disclosure-summary");
    summary.append(
      el("strong", block.label, "task-disclosure-label"),
      el("span", block.text, "task-disclosure-preview"),
    );
    const toggle = el("span", null, "task-disclosure-toggle");
    toggle.append(
      el("span", "Show more", "task-disclosure-more"),
      el("span", "Show less", "task-disclosure-less"),
    );
    summary.append(toggle);
    details.append(summary, el("pre", block.text, "task-text"));
    taskSection.append(details);
  }
  const revisions = detail.goal_revisions || [];
  if (revisions.length) {
    const history = el("div", null, "goal-revision-list");
    history.append(el("h3", "Goal revisions"));
    for (const revision of revisions) {
      const item = el("article", null, "history-card");
      item.append(el("strong", `Revision ${revision.revision} · from hop ${revision.applies_from_hop_id}`));
      item.append(el("p", revision.changed_at || "", "muted"));
      item.append(el("pre", revision.goal || "", "task-text"));
      history.append(item);
    }
    taskSection.append(history);
  }
  fragment.append(taskSection);

  if (detail.primary_problem) {
    const problem = el("section", null, "problem-box");
    problem.append(el("strong", detail.primary_problem.code || detail.primary_problem.kind));
    problem.append(el("p", detail.primary_problem.message));
    fragment.append(problem);
  }

  fragment.append(rolesSection(detail, selectedRole), timelineSection(detail, timeline));
  return fragment;
}

function build(detail, timeline, selectedRole, selectedTab, selectedReportUrl, reportBodies) {
  const fragment = document.createDocumentFragment();
  const agent = detail.task_mode === "independent" ? (detail.agent || {}) : null;
  const displayTitle = agent
    ? `Independent Agent · ${agent.name || detail.team || detail.task_id}`
    : (detail.task_title || detail.task_id);
  const head = el("div", null, "detail-head");
  const identity = el("div");
  identity.append(el("p", detail.status, "eyebrow"), el("h2", detail.team || detail.task_id));
  identity.append(el("p", displayTitle, "detail-subtitle"));
  head.append(identity, el("span", agent ? (agent.trigger_type ? "Active job" : "Idle") : (detail.active_role || "No active role"), "status-badge"));
  fragment.append(head);

  if (!agent) {
    const tab = ["overview", "reports"].includes(selectedTab) ? selectedTab : "overview";
    fragment.append(workflowTabs(detail, tab));
    const panel = el("div", null, "independent-tab-panel");
    panel.setAttribute("role", "tabpanel");
    panel.append(tab === "reports"
      ? workflowReports(detail, selectedRole, selectedReportUrl, reportBodies)
      : workflowOverview(detail, timeline, selectedRole));
    fragment.append(panel);
    return fragment;
  }

  const tab = ["overview", "history", "reports"].includes(selectedTab) ? selectedTab : "overview";
  fragment.append(independentTabs(detail, tab));
  const panel = el("div", null, "independent-tab-panel");
  panel.setAttribute("role", "tabpanel");
  if (tab === "history") panel.append(independentHistory(detail));
  else if (tab === "reports") panel.append(independentReports(detail, reportBodies));
  else panel.append(independentOverview(detail, timeline, selectedRole));
  fragment.append(panel);
  return fragment;
}

function unavailable(taskId, status, error) {
  const state = status === "error" ? "error" : taskId ? "loading" : "empty";
  const wrapper = el("div", null, "detail-state");
  wrapper.dataset.detailState = state;
  if (taskId) wrapper.dataset.taskId = taskId;
  wrapper.append(el("p", "Task detail", "eyebrow"));
  if (state === "error") {
    wrapper.append(el("h2", "Detail unavailable"));
    wrapper.append(el("p", error || "The selected task detail could not be loaded."));
  } else if (state === "loading") {
    wrapper.append(el("h2", "Loading selected task"));
    wrapper.append(el("p", taskId));
  } else {
    wrapper.append(el("h2", "Select a task"));
    wrapper.append(el("p", "Task detail is loaded only after selection and remains stable while the dashboard polls."));
  }
  return wrapper;
}

export function renderTaskDetail(
  root,
  detail,
  timeline,
  selectedRole,
  selectedTaskId = null,
  status = "idle",
  error = null,
  selectedTab = "overview",
  selectedReportUrl = null,
  reportBodies = new Map(),
  reportRevision = 0,
) {
  if (!detail || detail.task_id !== selectedTaskId) {
    const signature = JSON.stringify(["unavailable", selectedTaskId, status, error]);
    if (root.dataset.signature === signature) return;
    root._pendingRender = null;
    root.replaceChildren(unavailable(selectedTaskId, status, error));
    root.className = "surface task-detail empty-state";
    root.dataset.signature = signature;
    root.dataset.taskId = selectedTaskId || "";
    root.scrollTop = 0;
    return;
  }
  const signature = JSON.stringify([
    detail.projection_sha256, detail.version, timeline, selectedRole,
    selectedTab, reportRevision,
  ]);
  if (root.dataset.signature === signature) return;
  const sameTask = root.dataset.taskId === selectedTaskId;
  if (!sameTask) root._pendingRender = null;
  const selection = window.getSelection();
  if (sameTask && selection && !selection.isCollapsed && root.contains(selection.anchorNode)) {
    root._pendingRender = () => renderTaskDetail(
      root, detail, timeline, selectedRole, selectedTaskId, status, error,
      selectedTab, selectedReportUrl, reportBodies, reportRevision,
    );
    return;
  }
  const openDisclosureKeys = sameTask ? new Set(
    [...root.querySelectorAll("details[data-disclosure-key][open]")].map(details => details.dataset.disclosureKey),
  ) : new Set();
  const scroll = root.scrollTop;
  root.replaceChildren(build(detail, timeline, selectedRole, selectedTab, selectedReportUrl, reportBodies));
  for (const details of root.querySelectorAll("details[data-disclosure-key]")) {
    details.open = openDisclosureKeys.has(details.dataset.disclosureKey);
  }
  root.className = "panel";
  root.dataset.signature = signature;
  root.dataset.taskId = detail.task_id;
  root.scrollTop = scroll;
}

export function refreshTimelineTimes(root, now = Date.now()) {
  for (const node of root.querySelectorAll("[data-time-at]")) {
    node.textContent = timelineTime(node.dataset.timeAt, now);
  }
}

export function installSelectionResume(root) {
  document.addEventListener("selectionchange", () => {
    const selection = window.getSelection();
    if ((!selection || selection.isCollapsed) && root._pendingRender) {
      const render = root._pendingRender;
      root._pendingRender = null;
      render();
    }
  });
}
