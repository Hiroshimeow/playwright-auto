const STORAGE_KEY = "cdpa-dashboard-v2";
const CACHE_LIMIT = 20;
const saved = (() => {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); }
  catch { return {}; }
})();

function savedMap(value) {
  return new Map(Array.isArray(value) ? value : []);
}

export const state = {
  board: new Map(),
  counts: {},
  catalog: null,
  selectedTaskId: saved.selectedTaskId || null,
  selectedRoleByTask: savedMap(saved.selectedRoleByTask),
  selectedDetail: null,
  selectedDetailStatus: "idle",
  selectedDetailError: null,
  detailCache: new Map(),
  timeline: [],
  timelineCursor: null,
  history: [],
  historyCursor: null,
  runtime: null,
  dashboardActions: null,
  dashboardActionsStatus: "idle",
  dashboardActionsError: null,
  resumeBatchResult: null,
  pendingCommands: savedMap(saved.pendingCommands),
  etags: new Map(),
  inflight: new Map(),
  drawer: saved.drawer === "runtime" ? null : saved.drawer || null,
  modalOpen: Boolean(saved.modalOpen),
  scrollState: {
    boardLeft: Number(saved.scrollState?.boardLeft || 0),
    laneScroll: {...(saved.scrollState?.laneScroll || {})},
    detailTop: Number(saved.scrollState?.detailTop || 0),
  },
  apiError: null,
};

const listeners = new Set();

function persist() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    selectedTaskId: state.selectedTaskId,
    selectedRoleByTask: [...state.selectedRoleByTask],
    pendingCommands: [...state.pendingCommands],
    drawer: state.drawer,
    modalOpen: state.modalOpen,
    scrollState: state.scrollState,
  }));
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function commit(change) {
  change(state);
  persist();
  for (const listener of listeners) listener(state);
}

export function persistViewState() {
  persist();
}

export function commandKey(kind, taskId = "runtime") {
  return `${kind}:${taskId}`;
}

export function pendingFor(kind, taskId = "runtime") {
  return state.pendingCommands.get(commandKey(kind, taskId)) || null;
}

export function detailCacheGet(taskId, version, projectionSha256 = null) {
  const entry = state.detailCache.get(taskId);
  if (!entry) return null;
  if (
    Number(entry.version) !== Number(version)
    || String(entry.projectionSha256 || "") !== String(projectionSha256 || "")
  ) {
    state.detailCache.delete(taskId);
    return null;
  }
  state.detailCache.delete(taskId);
  state.detailCache.set(taskId, entry);
  return entry.detail;
}

export function detailCachePut(taskId, version, projectionSha256, detail) {
  state.detailCache.delete(taskId);
  state.detailCache.set(taskId, {
    version: Number(version),
    projectionSha256: projectionSha256 || null,
    detail,
  });
  while (state.detailCache.size > CACHE_LIMIT) {
    state.detailCache.delete(state.detailCache.keys().next().value);
  }
}

export function detailCacheInvalidate(taskId) {
  state.detailCache.delete(taskId);
  state.etags.delete(`detail:${taskId}`);
}

export function pruneDetailCache(taskIds) {
  const retained = new Set(taskIds);
  for (const taskId of state.detailCache.keys()) {
    if (!retained.has(taskId)) state.detailCache.delete(taskId);
  }
}
