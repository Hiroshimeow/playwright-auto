const METRICS = [
  ["cpu", "CPU"],
  ["load", "Load 1m"],
  ["memory", "Memory"],
  ["disk", "Disk"],
  ["network-rx", "Network RX total"],
  ["network-tx", "Network TX total"],
];

function serviceChip(name) {
  const chip = document.createElement("span");
  chip.className = "service-chip";
  chip.dataset.service = name;
  return chip;
}

function renderServices(root, state) {
  const runtime = state.runtime || {};
  const services = [
    ["api", "API", !state.apiError, state.apiError ? "unavailable" : "online"],
    ["worker", "Worker", Boolean(runtime.worker_online), runtime.worker_stale ? "stale" : "online"],
    ["cdp", "CDP", Boolean(runtime.browser?.connected), runtime.browser?.connected ? "connected" : "disconnected"],
  ];
  for (const [key, name, ok, label] of services) {
    let chip = root.querySelector(`[data-service="${key}"]`);
    if (!chip) {
      chip = serviceChip(key);
      root.append(chip);
    }
    chip.className = `service-chip ${ok ? "service-ok" : "service-bad"}`;
    const next = `${name} ${label}`;
    if (chip.textContent !== next) chip.textContent = next;
  }
}

function metricNode(key, label) {
  const node = document.createElement("div");
  node.className = "metric";
  node.dataset.metric = key;
  const strong = document.createElement("strong");
  const span = document.createElement("span");
  span.textContent = label;
  node.append(strong, span);
  return node;
}

function metricValues(state) {
  const host = state.system?.host || {};
  return {
    cpu: host.cpu_percent == null ? "—" : `${host.cpu_percent}%`,
    load: host.load_1 ?? "—",
    memory: host.memory_used_bytes == null ? "—" : `${Math.round(host.memory_used_bytes / 1048576)} MiB`,
    disk: host.disk_used_bytes == null ? "—" : `${Math.round(host.disk_used_bytes / 1073741824)} GiB`,
    "network-rx": host.network_rx_bytes ?? "—",
    "network-tx": host.network_tx_bytes ?? "—",
  };
}

function renderMetrics(root, state) {
  if (root.dataset.secondaryView !== "runtime") {
    root.replaceChildren();
    root.dataset.secondaryView = "runtime";
    delete root.dataset.historySignature;
  }
  let grid = root.querySelector(".metric-grid");
  if (!grid) {
    grid = document.createElement("div");
    grid.className = "metric-grid";
    for (const [key, label] of METRICS) grid.append(metricNode(key, label));
    root.replaceChildren(grid);
  }
  const values = metricValues(state);
  for (const [key] of METRICS) {
    const strong = grid.querySelector(`[data-metric="${key}"] strong`);
    const next = String(values[key]);
    if (strong.textContent !== next) strong.textContent = next;
  }
}

export function renderRuntime(statusRoot, secondaryRoot, state) {
  renderServices(statusRoot, state);
  if (state.drawer === "runtime") renderMetrics(secondaryRoot, state);
}
