function endpointLabel(endpoint) {
  return endpoint.label || endpoint.endpoint_id || endpoint.cdp_url || "Browser";
}

function button(label, attrs = {}) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = attrs.className || "button";
  node.textContent = label;
  for (const [name, value] of Object.entries(attrs)) {
    if (name !== "className") node.dataset[name] = value;
  }
  return node;
}

export function visibleBrowserEndpoints(state) {
  return (state.runtime?.browser?.endpoints || []).filter(endpoint => !endpoint.removed);
}

export function creationUsesTemporary(state, endpointId, forced = false) {
  const browser = state.runtime?.browser || {};
  const selected = endpointId || browser.default_endpoint_id;
  return Boolean(forced || visibleBrowserEndpoints(state).find(item =>
    selected ? item.endpoint_id === selected : item.is_default)?.temporary_default);
}

export function renderBrowserScope(root, state) {
  if (!root) return;
  const endpoints = visibleBrowserEndpoints(state);
  const requested = state.selectedEndpointId || "all";
  const selected = requested === "all" || endpoints.some(endpoint => endpoint.endpoint_id === requested)
    ? requested : "all";
  const scopes = document.createElement("div");
  scopes.className = "browser-scope-options";
  const all = button("All", {browserScope: "all", className: "browser-scope-button"});
  all.setAttribute("aria-pressed", String(selected === "all"));
  scopes.append(all);
  for (const endpoint of endpoints) {
    const label = `${endpointLabel(endpoint)} - ${endpoint.health || "unknown"} ${endpoint.active_count || 0}/${endpoint.capacity || endpoint.max_active_tabs || 2}`;
    const node = button(label, {browserScope: endpoint.endpoint_id, className: "browser-scope-button"});
    node.setAttribute("aria-pressed", String(selected === endpoint.endpoint_id));
    if (endpoint.draining) node.title = "Draining: no new allocations";
    scopes.append(node);
  }
  const actions = document.createElement("div");
  actions.className = "browser-scope-actions";
  actions.append(button("Add browser", {openBrowserAdd: "true"}), button("Manage browsers", {openBrowserManager: "true"}));
  root.replaceChildren(scopes, actions);
}

export function syncCreateBrowserOptions(select, state) {
  if (!select) return;
  const current = select.value;
  const endpoints = visibleBrowserEndpoints(state).filter(endpoint => !endpoint.draining);
  const signature = JSON.stringify(endpoints.map(item => [item.endpoint_id, item.label]));
  if (select.dataset.browserSignature === signature) return;
  select.dataset.browserSignature = signature;
  select.replaceChildren(new Option("Pool default", ""));
  for (const endpoint of endpoints) select.append(new Option(endpointLabel(endpoint), endpoint.endpoint_id));
  select.value = endpoints.some(endpoint => endpoint.endpoint_id === current) ? current : "";
}

function field(label, value) {
  const node = document.createElement("span");
  node.className = "browser-meta-field";
  const strong = document.createElement("strong");
  strong.textContent = label;
  const text = document.createElement("span");
  text.textContent = value == null || value === "" ? "—" : String(value);
  node.append(strong, text);
  return node;
}

function checkbox(name, label, checked) {
  const wrapper = document.createElement("label");
  wrapper.className = "check-row";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.name = name;
  input.checked = Boolean(checked);
  wrapper.append(input, document.createTextNode(` ${label}`));
  return wrapper;
}

function inputField(name, label, value, options = {}) {
  const wrapper = document.createElement("label");
  wrapper.textContent = label;
  const input = document.createElement("input");
  input.name = name;
  input.value = value ?? "";
  if (options.type) input.type = options.type;
  if (options.min != null) input.min = String(options.min);
  wrapper.append(input);
  return wrapper;
}

function addBrowserForm(endpoints) {
  const form = document.createElement("form");
  form.className = "browser-add-form";
  form.dataset.browserAdd = "true";
  form.dataset.browserAddForm = "true";
  const heading = document.createElement("div");
  heading.className = "browser-manager-section-heading";
  const title = document.createElement("strong");
  title.textContent = "Add browser";
  const hint = document.createElement("span");
  hint.textContent = "Attach an existing CDP endpoint or launch an isolated local profile.";
  heading.append(title, hint);

  const fields = document.createElement("div");
  fields.className = "browser-add-grid";
  fields.append(
    inputField("address", "CDP address", ""),
    inputField("label", "Label", ""),
  );
  const mode = document.createElement("label");
  mode.textContent = "Management";
  const select = document.createElement("select");
  select.name = "management_mode";
  select.append(new Option("External / attach only", "external"), new Option("Managed local", "managed-local"));
  mode.append(select);
  fields.append(mode, inputField("max_active_tabs", "Max active tabs", 2, {type: "number", min: 1}));
  fields.append(inputField("rate_limit_scope", "Rate-limit scope", ""));

  const managed = document.createElement("div");
  managed.className = "browser-managed-fields";
  managed.dataset.managedBrowserFields = "true";
  managed.hidden = true;
  const clone = document.createElement("label");
  clone.textContent = "Clone stopped profile";
  const cloneSelect = document.createElement("select");
  cloneSelect.name = "clone_from_endpoint_id";
  cloneSelect.append(new Option("Do not clone", ""));
  for (const endpoint of endpoints.filter(item => item.management_mode === "managed-local" && !item.managed_running)) {
    cloneSelect.append(new Option(endpointLabel(endpoint), endpoint.endpoint_id));
  }
  clone.append(cloneSelect);
  managed.append(
    inputField("profile_path", "Profile path", ""),
    inputField("executable_path", "Chromium executable (optional)", ""),
    clone,
    checkbox("start", "Start after adding", false),
  );
  const actions = document.createElement("div");
  actions.className = "browser-card-actions";
  actions.append(checkbox("temporary_default", "Temporary by default", false));
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "button primary";
  submit.textContent = "Add browser";
  actions.append(submit);
  form.append(heading, fields, managed, actions);
  return form;
}

export function renderBrowserManager(root, state) {
  if (!root) return;
  const endpoints = visibleBrowserEndpoints(state);
  const signature = JSON.stringify(endpoints.map(endpoint => [
    endpoint.endpoint_id, endpoint.label, endpoint.cdp_url, endpoint.health,
    endpoint.auth_state || endpoint.auth, endpoint.capacity, endpoint.active_count,
    endpoint.max_active_tabs, endpoint.cooldown, endpoint.draining,
    endpoint.is_default, endpoint.temporary_default, endpoint.rate_limit_scope,
    endpoint.management_mode, endpoint.managed_running, endpoint.process_error,
    endpoint.running_executions,
  ]));
  if (root.dataset.browserSignature === signature && root.childElementCount) return;
  const active = document.activeElement;
  if (
    root.childElementCount
    && active
    && root.contains(active)
    && active.matches?.("input,select,textarea")
  ) return;
  const drafts = new Map();
  for (const form of root.querySelectorAll("form")) {
    if (form.dataset.dirty || form.dataset.browserAdd) {
      drafts.set(form.dataset.browserSettings || "add", [...form.elements].filter(item => item.name)
        .map(item => ({name: item.name, value: item.value, checked: item.checked})));
    }
  }
  root.dataset.browserSignature = signature;
  const fragment = document.createDocumentFragment();
  fragment.append(addBrowserForm(endpoints));
  for (const endpoint of endpoints) {
    const card = document.createElement("form");
    card.className = "browser-card";
    card.dataset.browserSettings = endpoint.endpoint_id;
    const heading = document.createElement("div");
    heading.className = "browser-card-heading";
    const title = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = endpointLabel(endpoint);
    const address = document.createElement("code");
    address.textContent = endpoint.cdp_url || endpoint.endpoint_id;
    title.append(name, address);
    const flags = document.createElement("span");
    flags.className = "browser-card-flags";
    flags.textContent = [endpoint.is_default ? "default" : null, endpoint.draining ? "draining" : null]
      .filter(Boolean).join(" · ") || endpoint.management_mode || "external";
    heading.append(title, flags);

    const meta = document.createElement("div");
    meta.className = "browser-meta-grid";
    const running = Number(endpoint.active_count || 0);
    const capacity = `${running}/${endpoint.capacity || endpoint.max_active_tabs || 2}`;
    meta.append(
      field("Health", endpoint.health || (endpoint.connected ? "online" : "offline")),
      field("Auth", endpoint.auth_state || endpoint.auth || "unknown"),
      field("Capacity", capacity),
      field("Cooldown", endpoint.cooldown ? "active" : "none"),
      field("Running", running),
      field("Process", endpoint.management_mode === "managed-local"
        ? (endpoint.managed_running == null ? "ownership unverified" : endpoint.managed_running ? "running" : "stopped") : "external"),
    );

    const executions = document.createElement("div");
    executions.className = "browser-running-executions";
    for (const execution of endpoint.running_executions || []) {
      executions.append(field("Executing", `${execution.team || execution.task_id} / ${execution.role || "allocating"}`));
    }
    if (endpoint.process_error) executions.append(field("Process error", endpoint.process_error));

    const settings = document.createElement("div");
    settings.className = "browser-settings-grid";
    settings.append(
      inputField("label", "Label", endpoint.label || ""),
      inputField("max_active_tabs", "Max active tabs", endpoint.max_active_tabs || 2, {type: "number", min: 1}),
      inputField("rate_limit_scope", "Rate-limit scope", endpoint.rate_limit_scope || ""),
      checkbox("is_default", "Pool default", endpoint.is_default),
      checkbox("temporary_default", "Temporary by default", endpoint.temporary_default),
    );

    const defaultInput = settings.querySelector('[name="is_default"]');
    defaultInput.disabled = Boolean(endpoint.is_default || endpoint.draining);
    defaultInput.title = endpoint.is_default ? "Select another browser to change the pool default" : "";
    const actions = document.createElement("div");
    actions.className = "browser-card-actions";
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "button";
    save.textContent = "Save settings";
    actions.append(save);
    if (endpoint.management_mode === "managed-local") {
      actions.append(button(endpoint.managed_running ? "Stop" : "Start", endpoint.managed_running
        ? {browserStop: endpoint.endpoint_id}
        : {browserStart: endpoint.endpoint_id}));
    }
    const remove = button(endpoint.draining ? "Draining" : "Remove", {
      browserRemove: endpoint.endpoint_id,
      className: "button danger",
    });
    remove.disabled = Boolean(endpoint.draining);
    actions.append(remove);
    card.append(heading, meta, executions, settings, actions);
    fragment.append(card);
  }
  if (!endpoints.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No browser endpoints are registered.";
    fragment.append(empty);
  }
  root.replaceChildren(fragment);
  for (const form of root.querySelectorAll("form")) {
    const draft = drafts.get(form.dataset.browserSettings || "add");
    if (!draft) continue;
    for (const saved of draft) {
      const input = form.elements.namedItem(saved.name);
      if (input) {
        input.value = saved.value;
        if (input.type === "checkbox") input.checked = saved.checked;
      }
    }
    form.dataset.dirty = "true";
    const managed = form.querySelector("[data-managed-browser-fields]");
    if (managed) managed.hidden = form.elements.management_mode.value !== "managed-local";
  }
}

export const browserEndpointOperations = [
  "browser_endpoint_add",
  "browser_endpoint_update",
  "browser_endpoint_remove",
  "browser_endpoint_start",
  "browser_endpoint_stop",
];
