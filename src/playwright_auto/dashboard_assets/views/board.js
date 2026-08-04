const COLUMNS = ["RUNNING", "WAITING", "BLOCKED", "PAUSED", "DONE", "STOPPED", "INDEPENDENT_AGENTS"];
const COLUMN_LABELS = {INDEPENDENT_AGENTS: "AGENTS"};
const roleClocks = new Map();

function text(tag, value, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = value ?? "";
  return node;
}

function durationSeconds(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const whole = Math.floor(seconds);
  const days = Math.floor(whole / 86400);
  const hours = Math.floor((whole % 86400) / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${whole}s`;
}

function elapsed(value, now = Date.now()) {
  const started = Date.parse(value || "");
  if (!Number.isFinite(started)) return "—";
  return durationSeconds((now - started) / 1000);
}

function fixedDuration(startedAt, completedAt) {
  const started = Date.parse(startedAt || "");
  const completed = Date.parse(completedAt || "");
  if (!Number.isFinite(started) || !Number.isFinite(completed) || completed < started) return null;
  return durationSeconds((completed - started) / 1000);
}

function activeRoleStartedAt(task, now = Date.now()) {
  if (task.task_mode === "independent" || task.status !== "RUNNING" || !task.active_role) {
    roleClocks.delete(task.task_id);
    return null;
  }
  const identity = `${task.active_role}:${task.active_hop_id ?? "none"}`;
  const previous = roleClocks.get(task.task_id);
  if (previous?.identity === identity) return previous.startedAt;

  const projectedValue = task.effective_activity_at || task.updated_at || null;
  const projected = Date.parse(projectedValue || "");
  const value = Number.isFinite(projected) && projected <= now
    ? projectedValue
    : new Date(now).toISOString();
  roleClocks.set(task.task_id, {identity, startedAt: value});
  return value;
}

function field(label, value, className = "task-field") {
  const node = document.createElement("span");
  node.className = className;
  node.append(text("b", label), text("span", value || "—"));
  return node;
}

function waitingOrderField(task) {
  const order = task.waiting_order || {};
  const rank = Number.isInteger(order.rank) && order.rank > 0 ? String(order.rank) : "—";
  const node = field("Order", rank, "task-field task-order");
  if (rank === "—" && order.intervention) {
    node.title = order.intervention;
    node.setAttribute("aria-label", `Execution order unavailable: ${order.intervention}`);
  }
  return node;
}

function waitingOrderSort(a, b) {
  const aRank = Number.isInteger(a.waiting_order?.rank) && a.waiting_order.rank > 0
    ? a.waiting_order.rank
    : Number.MAX_SAFE_INTEGER;
  const bRank = Number.isInteger(b.waiting_order?.rank) && b.waiting_order.rank > 0
    ? b.waiting_order.rank
    : Number.MAX_SAFE_INTEGER;
  const aFifo = Number.isInteger(a.waiting_order?.fifo) && a.waiting_order.fifo >= 0
    ? a.waiting_order.fifo
    : Number.MAX_SAFE_INTEGER;
  const bFifo = Number.isInteger(b.waiting_order?.fifo) && b.waiting_order.fifo >= 0
    ? b.waiting_order.fifo
    : Number.MAX_SAFE_INTEGER;
  return aRank - bRank
    || aFifo - bFifo
    || String(a.task_id).localeCompare(String(b.task_id));
}

function waitingDisplay(task, board) {
  if (task.status !== "WAITING") return null;
  const raw = task.waiting_reason || task.primary_problem?.message || "";
  const match = raw.match(/^Waiting for dependencies(?: and exact-team ownership)?:\s*(.+)$/i);
  if (!match) return raw || null;
  const teams = [];
  for (const taskId of match[1].split(",").map(value => value.trim()).filter(Boolean)) {
    const team = board.get(taskId)?.team;
    const label = team || taskId;
    if (!teams.includes(label)) teams.push(label);
  }
  return teams.length ? `Waiting for ${teams.join(", ")}` : raw;
}

function taskSignature(task, board) {
  return JSON.stringify([
    task.projection_sha256,
    task.version,
    task.team,
    task.task_title,
    task.status,
    task.active_role,
    task.active_hop_id,
    task.active_action,
    task.started_at,
    task.completed_at,
    task.created_at,
    task.effective_activity_at,
    task.updated_at,
    task.primary_problem?.code,
    task.primary_problem?.message,
    task.waiting_reason,
    task.waiting_order,
    waitingDisplay(task, board),
    task.task_mode,
    task.agent,
    (task.roles || []).map(role => [role.logical_role, role.status, role.online]),
  ]);
}

function appendAgentTags(root, tags) {
  const values = Array.isArray(tags) ? tags.filter(Boolean) : [];
  if (!values.length) return;
  const list = document.createElement("span");
  list.className = "agent-tags";
  for (const tag of values) list.append(text("span", tag, "agent-tag"));
  root.append(list);
}

function card(task, selected, board) {
  const node = document.createElement("article");
  node.className = `task-card${selected ? " selected" : ""}`;
  node.dataset.taskId = task.task_id;
  node.dataset.signature = taskSignature(task, board);
  const agent = task.task_mode === "independent" ? (task.agent || {}) : null;

  const head = document.createElement("div");
  head.className = "task-card-head";
  const select = document.createElement("button");
  select.type = "button";
  select.className = "task-select";
  select.dataset.selectTask = task.task_id;
  select.setAttribute("aria-label", `Select ${task.team || task.task_id}`);
  const cardName = agent
    ? (agent.name || task.team || task.task_id)
    : (task.team || task.task_id);
  if (agent) select.append(text("span", "Independent Agent", "task-agent-context"));
  select.append(text("strong", cardName, "task-team"));
  if (agent) appendAgentTags(select, agent.tags);

  const roleGroup = document.createElement("span");
  roleGroup.className = "task-role-group";
  if (agent) {
    roleGroup.append(text("span", agent.trigger_type ? "ACTIVE" : "IDLE", "task-role"));
  } else {
    roleGroup.append(text("span", task.active_role || "—", "task-role"));
    const roleTimestamp = activeRoleStartedAt(task);
    if (roleTimestamp) {
      const roleClock = text("span", elapsed(roleTimestamp), "task-role-clock");
      roleClock.dataset.elapsedAt = roleTimestamp;
      roleClock.dataset.roleTimer = `${task.active_role}:${task.active_hop_id ?? "none"}`;
      roleGroup.append(roleClock);
    }
  }
  head.append(select, roleGroup);
  node.append(head);

  const summary = document.createElement("button");
  summary.type = "button";
  summary.className = "task-summary";
  summary.dataset.selectTask = task.task_id;
  const agentTarget = agent?.target_team
    ? `Working on ${agent.target_team}`
    : agent?.trigger_type ? "Active job" : "Waiting for trigger";
  summary.append(text(
    "span",
    agent ? agentTarget : (task.task_title || task.task_id),
    "task-title",
  ));
  if (agent?.trigger_type) {
    const maxTurns = agent.max_cycles === 0 ? "unlimited turns" : `turn ${agent.cycle || 0}/${agent.max_cycles}`;
    const trigger = [
      agent.trigger_type,
      agent.target_task_id,
      agent.occurrence_count ? `occurrence ${agent.occurrence_count}` : null,
      agent.check_count ? `check ${agent.check_count}` : null,
      maxTurns,
    ].filter(Boolean).join(" · ");
    summary.append(text("span", trigger, "task-agent-context"));
  }
  const problem = agent
    ? (task.primary_problem?.message || null)
    : (waitingDisplay(task, board) || task.primary_problem?.message || task.waiting_reason);
  if (problem) {
    summary.classList.add("has-problem");
    summary.append(text("span", problem, "task-problem"));
  }
  node.append(summary);

  const identity = document.createElement("div");
  identity.className = "task-identity";
  const id = text("code", task.task_id, "task-id");
  id.title = task.task_id;
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "copy-task-id";
  copy.setAttribute("data-copy-task-id", task.task_id);
  copy.setAttribute("aria-label", `Copy task ID ${task.task_id}`);
  copy.textContent = "Copy";
  identity.append(id, copy);
  node.append(identity);

  const meta = document.createElement("div");
  meta.className = "task-meta";
  if (agent) {
    meta.append(field("Status", task.status));
    meta.append(field("Runs", `${agent.run_count ?? 0}${agent.run_count_truncated ? "+" : ""}`));
    meta.append(field("Last", agent.last_run_at ? new Date(agent.last_run_at).toLocaleString() : "—"));
    meta.append(field("Action", task.active_action));
    meta.append(field("Tab", agent.tab_open ? "Open" : "Closed"));
  } else {
    const normalWaiting = task.status === "WAITING";
    meta.append(normalWaiting ? waitingOrderField(task) : field("Status", task.status));
    meta.append(field("Action", task.active_action));
    const timestamp = task.started_at || task.created_at;
    if (task.status === "RUNNING") {
      const running = field("Total", elapsed(timestamp), "task-field task-elapsed");
      running.dataset.elapsedAt = timestamp || "";
      meta.append(running);
    } else {
      const duration = fixedDuration(timestamp, task.elapsed_end_at);
      if (duration) meta.append(field("Total", duration, "task-field task-elapsed"));
    }
  }
  node.append(meta);
  return node;
}

function ensureLane(root, column) {
  let section = root.querySelector(`[data-column="${column}"]`);
  if (section) return section;
  section = document.createElement("section");
  section.className = "lane";
  section.dataset.column = column;
  const heading = document.createElement("header");
  heading.append(text("h2", COLUMN_LABELS[column] || column));
  heading.append(text("span", "0", "lane-count"));
  const list = document.createElement("div");
  list.className = "lane-list";
  section.append(heading, list);
  root.append(section);
  return section;
}

export function refreshElapsed(root, now = Date.now()) {
  const selection = window.getSelection();
  if (selection && !selection.isCollapsed && root.contains(selection.anchorNode)) return;
  for (const node of root.querySelectorAll("[data-elapsed-at]")) {
    const value = node.matches(".task-role-clock") ? node : node.querySelector("span");
    if (value) value.textContent = elapsed(node.dataset.elapsedAt, now);
  }
}

export function renderBoard(root, state) {
  for (const list of root.querySelectorAll(".lane-list")) {
    const column = list.closest(".lane")?.dataset.column;
    if (column) state.scrollState.laneScroll[column] = list.scrollTop;
  }

  const grouped = new Map(COLUMNS.map(column => [column, []]));
  for (const task of state.board.values()) {
    const column = task.task_mode === "independent" ? "INDEPENDENT_AGENTS"
      : grouped.has(task.column) ? task.column : task.status;
    if (grouped.has(column)) grouped.get(column).push(task);
  }
  for (const [column, tasks] of grouped.entries()) {
    tasks.sort(column === "WAITING"
      ? waitingOrderSort
      : (a, b) => String(b.updated_at).localeCompare(String(a.updated_at)) || String(a.task_id).localeCompare(String(b.task_id)));
  }

  for (const column of COLUMNS) {
    const section = ensureLane(root, column);
    const count = section.querySelector(".lane-count");
    const nextCount = String(grouped.get(column).length);
    if (count.textContent !== nextCount) count.textContent = nextCount;
    const list = section.querySelector(".lane-list");
    const scrollTop = state.scrollState.laneScroll[column] ?? list.scrollTop;
    const expected = new Set();
    let cursor = list.firstElementChild;

    for (const task of grouped.get(column)) {
      expected.add(task.task_id);
      const selector = `[data-task-id="${CSS.escape(task.task_id)}"]`;
      let current = list.querySelector(selector);
      const selected = task.task_id === state.selectedTaskId;
      const signature = taskSignature(task, state.board);
      const occupiedCursor = Boolean(current) && current === cursor;
      if (!current || current.dataset.signature !== signature) {
        const replacement = card(task, selected, state.board);
        if (current) current.replaceWith(replacement);
        current = replacement;
        if (occupiedCursor) cursor = current;
      } else {
        current.classList.toggle("selected", selected);
      }
      if (current !== cursor) list.insertBefore(current, cursor);
      cursor = current.nextElementSibling;
    }

    for (const current of [...list.querySelectorAll("[data-task-id]")]) {
      if (!expected.has(current.dataset.taskId)) current.remove();
    }
    list.scrollTop = scrollTop;
  }
}
