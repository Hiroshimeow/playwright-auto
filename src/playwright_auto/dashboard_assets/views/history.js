function signature(state) {
  return JSON.stringify([
    state.historyCursor,
    state.history.map(task => [task.task_id, task.team, task.status, task.updated_at]),
  ]);
}

export function renderHistory(root, state) {
  const nextSignature = signature(state);
  if (root.dataset.secondaryView === "history" && root.dataset.historySignature === nextSignature) return;
  const fragment = document.createDocumentFragment();
  const list = document.createElement("div");
  list.className = "history-list";
  for (const task of state.history) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "history-row";
    row.dataset.taskId = task.task_id;
    const title = document.createElement("strong");
    title.textContent = task.team || task.task_id;
    const meta = document.createElement("span");
    meta.textContent = `${task.status} · ${task.updated_at || "—"}`;
    row.append(title, meta);
    list.append(row);
  }
  fragment.append(list);
  if (state.historyCursor) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "button";
    more.dataset.loadHistory = "more";
    more.textContent = "Load more";
    fragment.append(more);
  }
  root.replaceChildren(fragment);
  root.dataset.secondaryView = "history";
  root.dataset.historySignature = nextSignature;
}
