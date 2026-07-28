const COLUMNS = ["RUNNING", "WAITING", "BLOCKED", "PAUSED", "DONE", "STOPPED", "INDEPENDENT_AGENTS"];
const COLUMN_LABELS = {INDEPENDENT_AGENTS: "INDEPENDENT AGENTS"};
const roleClocks = new Map();

function text(tag, value, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = value ?? "";
  return node;
}

function elapsed(value, now = Date.now()) {
  const started = Date.parse(value || "");
  if (!Number.isFinite(started)) return "—";
  const seconds = Math.max(0, Math.floor((now - started) / 1000));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${seconds}s`;
}

function activeRoleStartedAt(task, now = Date.now()) {
  if (task.status !== "RUNNING" || !task.active_role) {
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

function card(task, selected, board) {
  const node = document.createElement("article");
  node.className = `task-card${selected ? " selected" : ""}`;
  node.dataset.taskId = task.task_id;
  node.dataset.signature = taskSignature(task, board);

  const head = document.createElement("div");
  head.className = "task-card-head";
  const select = document.createElement("button");
  select.type = "button";
  select.className = "task-select";
  select.dataset.selectTask = task.task_id;
  select.setAttribute("aria-label", `Select ${task.team || task.task_id}`);
  const cardName = task.task_mode === "independent"
    ? (task.agent?.name || task.team || task.task_id)
    : (task.team || task.task_id);
  select.append(text("strong", cardName, "task-team"));
  const roleGroup = document.createElement("span");
  roleGroup.className = "task-role-group";
  roleGroup.append(text("span", task.active_role || "—", "task-role"));
  const roleTimestamp = activeRoleStartedAt(task);
  if (roleTimestamp) {
    const roleClock = text("span", elapsed(roleTimestamp), "task-role-clock");
    roleClock.dataset.elapsedAt = roleTimestamp;
    roleClock.dataset.roleTimer = `${task.active_role}:${task.active_hop_id ?? "none"}`;
    roleGroup.append(roleClock);
  }
  head.append(select, roleGroup);
  node.append(head);

  const summary = document.createElement("button");
  summary.type = "button";
  summary.className = "task-summary";
  summary.dataset.selectTask = task.task_id;
  const agent = task.task_mode === "independent" ? task.agent : null;
  const agentTarget = agent?.target_team
    ? `RUNNING for ${agent.target_team}`
    : task.status === "WAITING" ? "Waiting for trigger" : task.status;
  summary.append(text(
    "span",
    agent ? agentTarget : (task.task_title || task.task_id),
    "task-title",
  ));
  if (agent?.trigger_type) {
    const trigger = [
      agent.trigger_type,
      agent.target_task_id,
      agent.occurrence_count ? `occurrence ${agent.occurrence_count}` : null,
      agent.check_count ? `check ${agent.check_count}` : null,
      agent.max_cycles ? `cycle ${agent.cycle}/${agent.max_cycles}` : null,
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
  const normalWaiting = task.status === "WAITING" && task.task_mode !== "independent";
  meta.append(normalWaiting ? waitingOrderField(task) : field("Status", task.status));
  meta.append(field("Action", task.active_action));
  const taskStartedAt = task.started_at || task.created_at;
  const timestamp = task.status === "WAITING"
    ? (task.effective_activity_at || task.updated_at || task.created_at)
    : taskStartedAt;
  const running = field("Total", elapsed(timestamp), "task-field task-elapsed");
  running.dataset.elapsedAt = timestamp || "";
  meta.append(running);
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
    const column = grouped.has(task.column) ? task.column : task.status;
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
