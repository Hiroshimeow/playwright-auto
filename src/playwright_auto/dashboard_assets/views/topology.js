function endpointPort(endpoint) {
  try {
    return new URL(endpoint?.cdp_url || "").port || null;
  } catch {
    return null;
  }
}

export function endpointOperationalState(endpoint = {}) {
  if (endpoint.removed) return {key: "removed", label: "Removed"};
  if (endpoint.draining) return {key: "draining", label: "Draining"};
  const health = String(endpoint.health || "").toLowerCase();
  if (endpoint.connected === false || ["offline", "disconnected", "error"].includes(health)) {
    return {key: "offline", label: "Offline"};
  }
  if (endpoint.chatgpt_ready === false) return {key: "not-ready", label: "Not ready"};
  if (endpoint.cooldown) return {key: "cooldown", label: "Cooldown"};
  const active = Number(endpoint.active_count || 0);
  const capacity = Number(endpoint.capacity || endpoint.max_active_tabs || 0);
  const available = endpoint.available_slots == null
    ? (capacity > 0 ? capacity - active : null)
    : Number(endpoint.available_slots);
  if (available != null && Number.isFinite(available) && available <= 0) {
    return {key: "full", label: "Full"};
  }
  if (endpoint.connected === true || health === "online") return {key: "ready", label: "Ready"};
  return {key: "unknown", label: "Unknown"};
}

export function endpointIdentity(endpointId, endpoints = []) {
  const id = String(endpointId || "").trim();
  if (!id) return null;
  const endpoint = endpoints.find(item => item?.endpoint_id === id) || null;
  if (!endpoint) {
    return {
      id,
      label: id,
      address: null,
      port: null,
      state: {key: "unknown", label: "Unknown"},
    };
  }
  return {
    id,
    label: endpoint.label || endpointPort(endpoint) || id,
    address: endpoint.cdp_url || null,
    port: endpointPort(endpoint),
    state: endpointOperationalState(endpoint),
  };
}

function roleEndpoint(task = {}) {
  const roles = Array.isArray(task.roles) ? task.roles : [];
  const active = roles.find(role => role?.logical_role === task.active_role && role?.browser_endpoint_id);
  if (active) return active.browser_endpoint_id;
  const allocated = [...new Set(roles.map(role => role?.browser_endpoint_id).filter(Boolean))];
  return allocated.length === 1 ? allocated[0] : null;
}

export function taskPlacement(task = {}, endpoints = []) {
  const independent = task.task_mode === "independent";
  const requestedId = independent ? null : String(task.requested_browser_endpoint_id || "").trim() || null;
  const affinityId = independent ? String(task.agent?.browser_endpoint_id || "").trim() || null : null;
  const actualId = String(roleEndpoint(task) || "").trim() || null;
  const requested = endpointIdentity(requestedId, endpoints);
  const affinity = endpointIdentity(affinityId, endpoints);
  const actual = endpointIdentity(actualId, endpoints);
  return {
    requested,
    affinity,
    actual,
    relocated: Boolean(requested && actual && requested.id !== actual.id),
  };
}
