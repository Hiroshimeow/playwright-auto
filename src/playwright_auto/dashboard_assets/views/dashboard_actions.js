function textNode(className, text) {
  const node = document.createElement("span");
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

function emptyMessage(message, className = "muted") {
  const node = document.createElement("p");
  node.className = className;
  node.textContent = message;
  return node;
}

function actionTaskCopy(task) {
  const copy = document.createElement("span");
  copy.className = "action-option-copy";
  const title = document.createElement("strong");
  title.textContent = task.title || task.task_id;
  const status = textNode("", task.status || "UNKNOWN");
  const taskId = document.createElement("code");
  taskId.textContent = task.task_id;
  copy.append(title, status, taskId);
  return copy;
}

const DEFAULT_WORKFLOW_ROLES = new Set(["PLAN", "DEV", "REVIEW"]);

export function renderWorkflowAgentOptions(root, agents = []) {
  // Independent agents must not appear in Create Task.
  const workflow = Array.isArray(agents) ? agents.filter(agent => !agent.deleted_at) : [];
  const signature = JSON.stringify(workflow.map(agent => [
    agent.route_key, agent.display_name, agent.is_system,
  ]));
  if (root.dataset.signature === signature) return;
  const selected = new Set(
    [...root.querySelectorAll('input[name="roles"]:checked')].map(input => input.value),
  );
  const fragment = document.createDocumentFragment();
  for (const agent of workflow) {
    const label = document.createElement("label");
    label.className = "workflow-agent-card";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "roles";
    input.value = agent.route_key;
    input.checked = selected.size
      ? selected.has(agent.route_key)
      : DEFAULT_WORKFLOW_ROLES.has(agent.route_key);
    if (agent.route_key === "PLAN") {
      input.checked = true;
      input.disabled = true;
    }
    const copy = document.createElement("span");
    copy.className = "workflow-agent-copy";
    const name = document.createElement("strong");
    name.textContent = agent.display_name || agent.route_key;
    const meta = document.createElement("small");
    meta.textContent = agent.is_system
      ? `${agent.route_key} · built-in`
      : `${agent.route_key} · custom`;
    copy.append(name, meta);
    label.append(input, copy);
    fragment.append(label);
  }
  root.replaceChildren(fragment);
  root.dataset.signature = signature;
}

export function applyReuseRoleSelection(roleInputs, item = null, {reset = false} = {}) {
  const locked = Boolean(item);
  const selected = locked
    ? new Set(Array.isArray(item.roles) ? item.roles : [])
    : reset
      ? DEFAULT_WORKFLOW_ROLES
      : new Set([...roleInputs].filter(input => input.checked).map(input => input.value));
  for (const input of roleInputs) {
    if (locked || reset) input.checked = selected.has(input.value);
    if (input.value === "PLAN") input.checked = true;
    input.disabled = locked || input.value === "PLAN";
  }
}

export function selectedDependencyIds(root) {
  return [...root.querySelectorAll("input[data-dependency-task-id]:checked")]
    .map(input => input.dataset.dependencyTaskId)
    .filter(Boolean);
}

export function renderCreateActions(dependencyRoot, reuseSelect, current, roleInputs = []) {
  const actions = current.dashboardActions;
  const signature = JSON.stringify([
    current.dashboardActionsStatus,
    current.dashboardActionsError,
    actions?.degraded,
    actions?.dependency_teams || [],
    actions?.reuse_teams || [],
  ]);
  if (dependencyRoot.dataset.signature === signature) return;

  const selected = new Set(selectedDependencyIds(dependencyRoot));
  const selectedReuse = reuseSelect.value;
  const reuseOptions = [new Option("Do not reuse", "")];
  for (const item of actions?.reuse_teams || []) {
    reuseOptions.push(new Option(`${item.team} · ${item.status}`, item.team));
  }
  reuseSelect.replaceChildren(...reuseOptions);
  reuseSelect.value = [...reuseSelect.options].some(option => option.value === selectedReuse)
    ? selectedReuse : "";
  const selectedReuseItem = (actions?.reuse_teams || [])
    .find(item => item.team === reuseSelect.value) || null;
  applyReuseRoleSelection(roleInputs, selectedReuseItem, {
    reset: Boolean(selectedReuse && !selectedReuseItem),
  });

  const fragment = document.createDocumentFragment();
  if (current.dashboardActionsStatus === "loading" && !actions) {
    fragment.append(emptyMessage("Loading worker-published dependency options…"));
  } else if (current.dashboardActionsError) {
    fragment.append(emptyMessage(current.dashboardActionsError, "action-warning"));
  } else if (actions?.degraded) {
    fragment.append(emptyMessage(
      "Worker eligibility is unavailable. Use exact task IDs only after the catalog recovers.",
      "action-warning",
    ));
  } else if (!(actions?.dependency_teams || []).length) {
    fragment.append(emptyMessage("No eligible dependency tasks."));
  } else {
    for (const group of actions.dependency_teams) {
      const wrapper = document.createElement(group.ambiguous ? "div" : "label");
      wrapper.className = `action-option${group.ambiguous ? " ambiguous" : ""}`;
      wrapper.dataset.dependencyTeam = group.team;
      const input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.dependencyTeamSelect = group.team;
      const copy = document.createElement("span");
      copy.className = "action-option-copy";
      const team = document.createElement("strong");
      team.textContent = group.team;
      copy.append(team);
      if (group.ambiguous) {
        input.disabled = true;
        const warning = textNode(
          "action-warning",
          "Ambiguous team: enter one exact task ID in Advanced.",
        );
        const candidates = document.createElement("span");
        candidates.className = "action-candidates";
        for (const task of group.tasks || []) {
          const row = document.createElement("span");
          row.textContent = `${task.title || task.task_id} · ${task.status} · ${task.task_id}`;
          candidates.append(row);
        }
        copy.append(warning, candidates);
      } else {
        const task = (group.tasks || [])[0];
        if (!task) continue;
        input.dataset.dependencyTaskId = task.task_id;
        input.checked = selected.has(task.task_id);
        copy.append(...actionTaskCopy(task).childNodes);
      }
      wrapper.append(input, copy);
      fragment.append(wrapper);
    }
  }
  dependencyRoot.replaceChildren(fragment);
  dependencyRoot.dataset.signature = signature;
}

export function selectedResumeTeams(root) {
  return [...root.querySelectorAll("input[data-resume-team]:checked")]
    .map(input => input.dataset.resumeTeam)
    .filter(Boolean);
}

export function updateResumeButton(root) {
  const button = root.querySelector("[data-resume-selected]");
  if (button) {
    button.disabled = root.dataset.resumeBatchLocked === "true"
      || selectedResumeTeams(root).length === 0;
  }
}

function resumeCommandLabel(command) {
  if (!command) return null;
  if (command.result?.reason_code === "stale_worker") return "stale worker";
  if (command.status === "applied" && command.result?.outcome === "continued") return "continued";
  if (command.status === "recovery_required") return "recovery required";
  if (command.status === "failed") return "failed";
  if (["submitting", "queued"].includes(command.status)) return "queued";
  return command.status;
}

function resumeBatchSummary(result, commands) {
  if (!result?.teams?.length) {
    return "Each selected exact team is submitted independently.";
  }
  const counts = {continued: 0, queued: 0, recoveryRequired: 0, failed: 0, staleWorker: 0, uncertain: 0};
  for (const team of result.teams) {
    const label = resumeCommandLabel(commands.get(`resume_team:${team}`));
    if (label === "continued") counts.continued += 1;
    else if (["queued", "running"].includes(label)) counts.queued += 1;
    else if (label === "recovery required") counts.recoveryRequired += 1;
    else if (label === "stale worker") counts.staleWorker += 1;
    else if (label === "failed") counts.failed += 1;
    else counts.uncertain += 1;
  }
  const parts = [
    `${counts.continued} continued`,
    `${counts.queued} queued`,
    `${counts.recoveryRequired} recovery required`,
    `${counts.failed} failed`,
  ];
  if (counts.staleWorker) parts.push(`${counts.staleWorker} stale worker`);
  if (counts.uncertain) parts.push(`${counts.uncertain} uncertain`);
  return parts.join(" · ");
}

export function renderResume(root, current) {
  const actions = current.dashboardActions;
  const trackedTeams = new Set([
    ...(actions?.resume_teams || []).map(item => item.team),
    ...(current.resumeBatchResult?.teams || []),
  ]);
  const pending = [...trackedTeams].map(team => {
    const command = current.pendingCommands.get(`resume_team:${team}`);
    return [team, resumeCommandLabel(command), command?.error || null, command?.result || null];
  });
  const signature = JSON.stringify([
    current.dashboardActionsStatus,
    current.dashboardActionsError,
    actions?.degraded,
    actions?.resume_teams || [],
    current.resumeBatchResult,
    pending,
  ]);
  if (root.dataset.secondaryView === "resume" && root.dataset.resumeSignature === signature) {
    return;
  }

  const selected = new Set(selectedResumeTeams(root));
  const fragment = document.createDocumentFragment();
  const guidance = document.createElement("p");
  guidance.className = "resume-guidance";
  guidance.textContent = "STOPPED teams cannot be resumed. Create a replacement/reuse task instead.";
  fragment.append(guidance);

  if (current.dashboardActionsStatus === "loading" && !actions) {
    fragment.append(emptyMessage("Loading worker-published Resume options…"));
  } else if (current.dashboardActionsError) {
    fragment.append(emptyMessage(current.dashboardActionsError, "action-warning"));
  } else if (actions?.degraded) {
    fragment.append(emptyMessage(
      "Resume eligibility is unavailable while the worker catalog is degraded.",
      "action-warning",
    ));
  } else if (!(actions?.resume_teams || []).length) {
    fragment.append(emptyMessage("No exact teams are currently eligible for Resume."));
  } else {
    const options = document.createElement("div");
    options.className = "resume-options";
    for (const item of actions.resume_teams) {
      const label = document.createElement("label");
      label.className = "resume-option";
      label.dataset.resumeTeam = item.team;
      const input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.resumeTeam = item.team;
      input.checked = selected.has(item.team);
      const copy = document.createElement("span");
      copy.className = "resume-option-copy";
      const team = document.createElement("strong");
      team.textContent = item.team;
      const command = current.pendingCommands.get(`resume_team:${item.team}`);
      const commandLabel = resumeCommandLabel(command);
      const status = `${item.status} · ${item.reason}${commandLabel ? ` · ${commandLabel}` : ""}`;
      const task = textNode("", `${item.title || item.task_id} · ${status}`);
      const taskId = document.createElement("code");
      taskId.textContent = item.task_id;
      copy.append(team, task, taskId);
      label.append(input, copy);
      options.append(label);
    }
    fragment.append(options);
  }

  const footer = document.createElement("div");
  footer.className = "resume-actions";
  const summary = document.createElement("p");
  summary.className = "resume-batch-summary";
  summary.textContent = resumeBatchSummary(
    current.resumeBatchResult,
    current.pendingCommands,
  );
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button primary";
  button.dataset.resumeSelected = "";
  button.textContent = "Resume selected";
  footer.append(summary, button);
  fragment.append(footer);

  root.replaceChildren(fragment);
  root.dataset.secondaryView = "resume";
  root.dataset.resumeBatchLocked = current.resumeBatchResult?.teams?.length
    ? "true" : "false";
  root.dataset.resumeSignature = signature;
  delete root.dataset.historySignature;
  updateResumeButton(root);
}
