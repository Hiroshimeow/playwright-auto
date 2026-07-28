function el(tag, value, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = value;
  return node;
}

function button(action, label, task) {
  const node = el("button", label);
  node.type = "button";
  node.dataset.control = action;
  node.dataset.taskId = task.task_id;
  node.dataset.version = String(task.version || 0);
  return node;
}

function independentButton(action, label, task) {
  const node = el("button", label);
  node.type = "button";
  node.dataset.independentAction = action;
  node.dataset.taskId = task.task_id;
  node.dataset.version = String(task.version || 0);
  return node;
}

function selectedInput(detail, selectedRole) {
  const role = selectedRole || detail.active_role || null;
  return (role && detail.role_inputs?.[role]) || detail.active_input || null;
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

function build(detail, timeline, selectedRole) {
  const fragment = document.createDocumentFragment();
  const head = el("div", null, "detail-head");
  const identity = el("div");
  identity.append(el("p", detail.status, "eyebrow"), el("h2", detail.team || detail.task_id));
  identity.append(el("p", detail.task_title || detail.task_id, "detail-subtitle"));
  head.append(identity, el("span", detail.active_role || "No active role", "status-badge"));
  fragment.append(head);

  const taskSection = el("section", null, "detail-section");
  taskSection.append(el("h3", detail.task_mode === "independent" ? "Independent agent" : "Task"));
  taskSection.append(el("pre", detail.task_text || detail.task_title || detail.task_id, "task-text"));
  if (detail.task_mode === "independent") {
    const agent = detail.agent || {};
    const context = el("div", null, "agent-detail-grid");
    context.append(
      el("span", `Name: ${agent.name || detail.team}`),
      el("span", `Generation: ${agent.generation || 1}`),
      el("span", `Trigger: ${agent.trigger_type || "waiting"}`),
      el("span", `Target: ${agent.target_team || "—"}`),
      el("span", `Task: ${agent.target_task_id || "—"}`),
      el("span", `Cycle: ${agent.cycle || 0}/${agent.max_cycles || 1}`),
    );
    taskSection.append(context);
  }
  fragment.append(taskSection);

  if (detail.primary_problem) {
    const problem = el("section", null, "problem-box");
    problem.append(el("strong", detail.primary_problem.code || detail.primary_problem.kind));
    problem.append(el("p", detail.primary_problem.message));
    fragment.append(problem);
  }

  const controls = el("div", null, "control-grid");
  if (detail.task_mode === "independent") {
    const enabled = detail.agent?.enabled !== false;
    const active = Boolean(detail.agent?.trigger_type);
    const terminal = ["DONE", "STOPPED"].includes(detail.status);
    const inFlight = ["sending", "sent", "waiting"].includes(detail.active_hop?.state);
    const idle = !active && ["WAITING", "PAUSED"].includes(detail.status);
    if (idle && enabled) {
      controls.append(independentButton("run", "Run once", detail));
      controls.append(independentButton("command", "Command", detail));
    }
    if (!terminal) {
      controls.append(button(enabled ? "pause" : "resume", enabled ? "Pause" : "Enable", detail));
      if (active) controls.append(button("stop", "Stop current job", detail));
      if (active && detail.status === "BLOCKED") controls.append(button("retry", "Retry", detail));
      controls.append(button("open_tab", "Open tab", detail));
      if (!inFlight) controls.append(button("close_tab", "Close tab", detail));
      if (idle && !inFlight) controls.append(button("new_chat", "Renew", detail));
      controls.append(independentButton("settings", "Settings", detail));
    }
    controls.append(independentButton("history", "History", detail));
    controls.append(independentButton("reports", "Reports", detail));
  } else {
    for (const [action, label] of [
      ["pause", "Pause"], ["resume", "Resume"], ["retry", "Retry hop"],
      ["restart_role", "Restart role"], ["new_chat", "New chat"],
      ["open_tab", "Open tab"], ["route_plan", "Route PLAN"],
      ["stop", "Stop"], ["clear_team", "Clear team"],
    ]) controls.append(button(action, label, detail));
  }
  fragment.append(controls);

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
  fragment.append(roles);

  const timelineSection = el("section", null, "detail-section");
  const title = el("div", null, "section-inline");
  title.append(el("h3", "Timeline"));
  if (detail.timeline_total > timeline.length) {
    const more = el("button", "Load older");
    more.type = "button";
    more.dataset.loadTimeline = detail.task_id;
    title.append(more);
  }
  timelineSection.append(title);
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
  timelineSection.append(list);
  fragment.append(timelineSection);
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
  const signature = JSON.stringify([detail.projection_sha256, detail.version, timeline, selectedRole]);
  if (root.dataset.signature === signature) return;
  const sameTask = root.dataset.taskId === selectedTaskId;
  if (!sameTask) root._pendingRender = null;
  const selection = window.getSelection();
  if (sameTask && selection && !selection.isCollapsed && root.contains(selection.anchorNode)) {
    root._pendingRender = () => renderTaskDetail(
      root, detail, timeline, selectedRole, selectedTaskId, status, error,
    );
    return;
  }
  const scroll = root.scrollTop;
  root.replaceChildren(build(detail, timeline, selectedRole));
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
