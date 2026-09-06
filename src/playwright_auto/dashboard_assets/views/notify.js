import {reportBody, workflowReportModel} from "./task_detail.js?v=20260906-listen-controls-v1";

const NOTIFY_STATUSES = new Set(["DONE", "BLOCKED", "PAUSED", "STOPPED"]);

function notifyTimestamp(task) {
  return task.elapsed_end_at || task.effective_activity_at || task.updated_at || "";
}

function timestampValue(task) {
  const value = Date.parse(notifyTimestamp(task));
  return Number.isFinite(value) ? value : 0;
}

export function selectNotifyTasks(tasks) {
  const latest = new Map();
  for (const task of tasks || []) {
    if (!task?.team || !NOTIFY_STATUSES.has(task.status)) continue;
    const current = latest.get(task.team);
    if (!current || timestampValue(task) > timestampValue(current)
      || (timestampValue(task) === timestampValue(current)
        && String(task.task_id).localeCompare(String(current.task_id)) > 0)) {
      latest.set(task.team, task);
    }
  }
  return [...latest.values()].sort((a, b) =>
    timestampValue(b) - timestampValue(a)
      || String(b.task_id).localeCompare(String(a.task_id)),
  );
}

function compactTime(value) {
  const date = new Date(value || "");
  if (!Number.isFinite(date.getTime())) return "—";
  return date.toLocaleString([], {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

export function renderNotify(root, state) {
  if (root.dataset.secondaryView === "notify-report") return;
  const list = document.createElement("div");
  list.className = "notify-list";
  const tasks = selectNotifyTasks(state.board.values());
  if (!tasks.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No recent team status.";
    list.append(empty);
  }
  for (const task of tasks) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "notify-row";
    row.setAttribute("data-notify-task-id", task.task_id);
    row.dataset.status = task.status;

    const team = document.createElement("strong");
    team.textContent = task.team;
    const meta = document.createElement("span");
    meta.className = "notify-meta";
    const status = document.createElement("span");
    status.className = "notify-status";
    status.textContent = task.status;
    const time = document.createElement("time");
    time.className = "notify-time";
    const at = notifyTimestamp(task);
    time.textContent = compactTime(at);
    if (at) time.dateTime = at;
    meta.append(status, time);
    row.append(team, meta);
    list.append(row);
  }
  root.replaceChildren(list);
  root.dataset.secondaryView = "notify";
}

export function notifyReportModel(detail) {
  const model = workflowReportModel(detail, detail.active_role);
  if (detail.status === "DONE" || model.selectedReport?._logicalRole === detail.active_role) return model;
  const reports = model.roles.flatMap(item => item.reports);
  const newest = reports.reduce((best, report) => {
    if (!best) return report;
    const reportAt = Date.parse(report.created_at || "");
    const bestAt = Date.parse(best.created_at || "");
    if (Number.isFinite(reportAt) && Number.isFinite(bestAt) && reportAt !== bestAt) {
      return reportAt > bestAt ? report : best;
    }
    return report._order > best._order ? report : best;
  }, null);
  return newest ? {...model, selectedReport: newest} : model;
}

export function renderNotifyReport(root, detail, reportBodies) {
  const wrapper = document.createElement("div");
  wrapper.className = "notify-report";
  const head = document.createElement("div");
  head.className = "notify-report-head";
  const back = document.createElement("button");
  back.type = "button";
  back.className = "button";
  back.setAttribute("data-notify-back", "");
  back.textContent = "Back";
  const identity = document.createElement("strong");
  identity.textContent = `${detail.team || detail.task_id} · ${detail.status || ""}`;
  head.append(back, identity);
  wrapper.append(head);

  const model = notifyReportModel(detail);
  const report = model.selectedReport;
  if (!report) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No report yet";
    wrapper.append(empty);
  } else {
    const reader = document.createElement("article");
    reader.className = "report-reader";
    const meta = document.createElement("strong");
    meta.textContent = `${report._logicalRole || report.role || report.physical_role || "Report"} · Turn ${report.turn ?? "—"}`;
    reader.append(meta);
    if (report.url) {
      const link = document.createElement("a");
      link.href = report.url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Open raw report";
      reader.append(link);
    }
    const body = document.createElement("pre");
    body.className = "task-text report-body";
    body.textContent = reportBody(report, reportBodies);
    reader.append(body);
    wrapper.append(reader);
  }
  root.replaceChildren(wrapper);
  root.dataset.secondaryView = "notify-report";
}
