import {reportBody, reportMatchesHop, workflowReportModel} from "./task_detail.js?v=cdpa-accepted-report-v1";

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

function isHandoff(report) {
  return report?.material_type === "handoff" || report?.source_kind === "route_json";
}

export function notifyReportModel(detail) {
  let model = workflowReportModel(detail, detail.active_role);
  const reports = (detail.reports || []).filter(report => !isHandoff(report));
  if (isHandoff(model.selectedReport)) {
    const linkedId = model.selectedReport.linked_report_id;
    const linked = linkedId && reports.find(report => report.report_id === linkedId);
    if (linked) return {...model, selectedReport: linked};
    model = workflowReportModel({...detail, reports}, detail.active_role);
  }
  if (model.selectedExpectedMissing || (model.selectedReport
    && (detail.status === "DONE" || model.selectedReport._logicalRole === detail.active_role))) return model;
  const newest = reports.reduce((best, report) => {
    if (!best) return report;
    const reportAt = Date.parse(report.created_at || "");
    const bestAt = Date.parse(best.created_at || "");
    if (Number.isFinite(reportAt) && Number.isFinite(bestAt) && reportAt !== bestAt) {
      return reportAt > bestAt ? report : best;
    }
    return report;
  }, null);
  return newest ? {...model, selectedReport: newest} : model;
}

function renderNotifyReason(detail) {
  const status = detail.status;
  if (!NOTIFY_STATUSES.has(status) || status === "DONE") return null;
  const section = document.createElement("section");
  section.className = "notify-reason";
  const heading = document.createElement("strong");
  const problem = detail.primary_problem;
  const code = status === "BLOCKED" ? (problem?.code || detail.block_code) : null;
  const latestError = detail.timeline?.find(item => item.kind === "error")?.message;
  const control = (detail.controls || []).findLast(item =>
    status === "BLOCKED" ? item.status === "recovery_required"
      : status === "STOPPED" && item.action === "stop" && item.status === "applied"
  );
  const result = control?.result;
  const activeHop = detail.active_hop;
  const role = problem?.role || activeHop?.logical_role || detail.active_role || detail.cleanup?.active_role;
  heading.textContent = role
    ? [role, activeHop?.turn ? `Turn ${activeHop.turn}` : null].filter(Boolean).join(" · ")
    : {BLOCKED: "Block details", PAUSED: "Pause details", STOPPED: "Stop details"}[status];
  section.append(heading);

  const message = document.createElement("p");
  if (status === "BLOCKED") {
    message.textContent = problem?.message || latestError || "No block reason was recorded.";
  } else if (status === "STOPPED") {
    message.textContent = detail.stop_reason || control?.reason || "No stop reason was recorded.";
  } else {
    message.textContent = detail.pause_reason || "No pause reason was recorded.";
  }
  section.append(message);
  if (code) {
    const errorCode = document.createElement("code");
    errorCode.textContent = code;
    section.append(errorCode);
  }
  if (status === "STOPPED" && (detail.cleanup?.last_error || latestError)) {
    const error = document.createElement("p");
    error.textContent = detail.cleanup?.last_error
      ? `Cleanup error — ${detail.cleanup.last_error}`
      : `Last recorded error — ${latestError}`;
    section.append(error);
  }
  if (status === "BLOCKED" && result?.next_safe_action
    && (result.reason_code === code || result.reason === message.textContent)) {
    const next = document.createElement("p");
    next.textContent = `Next step — ${result.next_safe_action}`;
    section.append(next);
  }
  return section;
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
  if (detail.status === "BLOCKED" || detail.status === "PAUSED") {
    const openTask = document.createElement("button");
    openTask.type = "button";
    openTask.className = "button";
    openTask.dataset.taskId = detail.task_id;
    openTask.dataset.notifyOpenTask = "";
    openTask.textContent = "Open task";
    head.append(openTask);
  }
  wrapper.append(head);

  const model = notifyReportModel(detail);
  const report = model.selectedReport;
  const pausedByReport = detail.status === "PAUSED"
    && !detail.pause_reason
    && detail.active_hop?.state === "routed"
    && reportMatchesHop(report, detail.active_hop);
  const reason = pausedByReport ? null : renderNotifyReason(detail);
  if (reason) wrapper.append(reason);
  if (!report && (!reason || model.selectedExpectedMissing)) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = model.selectedMissingReason;
    wrapper.append(empty);
  } else if (report) {
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
    body.setAttribute("data-notify-report-body", "");
    body.textContent = reportBody(report, reportBodies);
    reader.append(body);
    if (reason) {
      const history = document.createElement("details");
      history.className = "task-disclosure notify-prior-report";
      const summary = document.createElement("summary");
      summary.textContent = "Report details";
      history.append(summary, reader);
      wrapper.append(history);
    } else {
      wrapper.append(reader);
    }
  }
  root.replaceChildren(wrapper);
  root.dataset.secondaryView = "notify-report";
  return report;
}
