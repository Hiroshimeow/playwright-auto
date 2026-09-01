function serviceChip(name) {
  const chip = document.createElement("span");
  chip.className = "service-chip";
  chip.dataset.service = name;
  return chip;
}

export function renderRuntime(root, state) {
  const runtime = state.runtime || {};
  const domOnly = root.querySelector("[data-dom-only]");
  if (domOnly && domOnly.dataset.pending !== "true") {
    domOnly.checked = runtime.settings?.dom_only === true;
    domOnly.disabled = Boolean(state.apiError) || typeof runtime.settings?.dom_only !== "boolean";
  }
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
  for (const [key, label] of [["cpu", "CPU —"], ["disk", "Disk —"]]) {
    let chip = root.querySelector(`[data-service="${key}"]`);
    if (!chip) {
      chip = serviceChip(key);
      chip.textContent = label;
      root.append(chip);
    }
  }
}
