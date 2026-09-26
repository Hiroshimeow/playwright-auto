const MESSAGE_LIMIT = 100;
const AUDIT_LIMIT = 300;
const SEEN_LIMIT = 1024;

function eventTime(event) {
  const explicit = Number(event?.at_epoch || 0);
  if (Number.isFinite(explicit) && explicit > 0) return explicit;
  const parsed = Date.parse(event?.at || "");
  return Number.isFinite(parsed) ? parsed / 1000 : 0;
}

export function canonicalMessageKey(values = {}) {
  const messageId = String(values.message_id || "").trim();
  if (messageId) return messageId;
  const author = String(values.author || "").trim().toLowerCase();
  const turnId = String(values.turn_id || "").trim();
  return author && turnId ? author + ":" + turnId : "";
}

function rememberEvent(state, eventId) {
  if (!eventId || state.seen.has(eventId)) return false;
  state.seen.add(eventId);
  state.seenOrder.push(eventId);
  while (state.seenOrder.length > SEEN_LIMIT) {
    state.seen.delete(state.seenOrder.shift());
  }
  return true;
}

function transcriptFallbackKey(message = {}) {
  const author = String(message.author || "").trim().toLowerCase();
  const turnId = String(message.turnId || "").trim();
  return author && turnId ? author + ":" + turnId : "";
}

function trimMessages(state) {
  while (state.messageOrder.length > MESSAGE_LIMIT) {
    const key = state.messageOrder.shift();
    state.messages.delete(key);
    for (const [alias, target] of state.turnAliases) {
      if (target === key) {
        state.turnAliases.delete(alias);
        continue;
      }
      if (target === null) {
        const stillRepresented = [...state.messages.values()]
          .some(message => transcriptFallbackKey(message) === alias);
        if (!stillRepresented) state.turnAliases.delete(alias);
      }
    }
  }
}

function applyTranscript(state, event, seq = null) {
  const values = event.values || {};
  const author = String(values.author || "assistant").trim().toLowerCase();
  const messageId = String(values.message_id || "").trim();
  const turnId = String(values.turn_id || "").trim();
  const fallbackKey = author && turnId ? author + ":" + turnId : "";
  let key = canonicalMessageKey(values);
  const text = String(values.text || "");
  if (!key || !text) return;

  if (messageId) {
    key = messageId;
    if (fallbackKey) {
      const hasDistinctRealMessage = [...state.messages.values()].some(message => (
        message.messageId
        && message.messageId !== messageId
        && transcriptFallbackKey(message) === fallbackKey
      ));
      if (hasDistinctRealMessage) {
        state.turnAliases.set(fallbackKey, null);
      } else {
        const prior = state.messages.get(fallbackKey);
        if (prior && !prior.messageId) {
          const fallbackIndex = state.messageOrder.indexOf(fallbackKey);
          const realIndex = state.messageOrder.indexOf(messageId);
          state.messages.delete(fallbackKey);
          if (fallbackIndex >= 0) {
            if (realIndex >= 0) state.messageOrder.splice(fallbackIndex, 1);
            else state.messageOrder[fallbackIndex] = messageId;
          }
          if (!state.messages.has(messageId)) {
            state.messages.set(messageId, {...prior, key: messageId, messageId});
          }
          state.turnAliases.set(fallbackKey, messageId);
        } else if (state.turnAliases.has(fallbackKey)) {
          const alias = state.turnAliases.get(fallbackKey);
          if (alias !== null && alias !== messageId) {
            state.turnAliases.set(fallbackKey, null);
          }
        }
      }
    }
  } else if (fallbackKey && state.turnAliases.has(fallbackKey)) {
    const alias = state.turnAliases.get(fallbackKey);
    if (alias) key = alias;
  }

  const projectionSource = String(values.projection_source || "dom").toLowerCase();
  const authoritative = projectionSource === "listen" && values.complete === true;
  const existing = state.messages.get(key);
  if (existing?.authoritative && !authoritative) return;
  if (
    existing?.authoritative
    && authoritative
    && seq != null
    && existing.seq != null
    && seq < existing.seq
  ) return;
  const message = {
    key,
    author,
    messageId: messageId || existing?.messageId || null,
    turnId: turnId || existing?.turnId || null,
    text,
    textLength: Number(values.text_length || text.length),
    complete: values.complete !== false,
    projectionSource,
    authoritative,
    at: event.at || null,
    atEpoch: eventTime(event),
    seq,
    event,
  };
  if (!existing && !state.messageOrder.includes(key)) state.messageOrder.push(key);
  state.messages.set(key, message);
  trimMessages(state);
}

export function createLiveReducer() {
  const state = {
    epoch: null,
    lastSeq: null,
    messages: new Map(),
    messageOrder: [],
    turnAliases: new Map(),
    audit: [],
    seen: new Set(),
    seenOrder: [],
    resetRequired: false,
  };

  function applyDurable(event, seq = null) {
    const eventId = String(event?.event_id || "").trim();
    if (eventId && !rememberEvent(state, eventId)) return;
    if (
      String(event?.source || "").toUpperCase() === "TRANSCRIPT"
      && String(event?.kind || "") === "message"
    ) {
      applyTranscript(state, event, seq);
      return;
    }
    state.audit.push(event);
    if (state.audit.length > AUDIT_LIMIT) {
      state.audit.splice(0, state.audit.length - AUDIT_LIMIT);
    }
  }

  function applyEnvelope(envelope) {
    const epoch = String(envelope?.stream_epoch || "");
    const seq = Number(envelope?.seq);
    if (!epoch || !Number.isInteger(seq) || seq < 1) {
      state.resetRequired = true;
      return false;
    }
    if (state.epoch && epoch !== state.epoch) {
      state.resetRequired = true;
      return false;
    }
    if (state.lastSeq != null) {
      if (seq <= state.lastSeq) return true;
      if (seq !== state.lastSeq + 1) {
        state.resetRequired = true;
        return false;
      }
    }
    state.epoch = epoch;
    state.lastSeq = seq;
    applyDurable(envelope, seq);
    return true;
  }

  function applySnapshot(items, {epoch = null, headSeq = null} = {}) {
    state.messages.clear();
    state.messageOrder.length = 0;
    state.turnAliases.clear();
    state.audit.length = 0;
    state.seen.clear();
    state.seenOrder.length = 0;
    state.resetRequired = false;
    for (const event of items || []) applyDurable(event, null);
    state.epoch = epoch;
    state.lastSeq = Number.isInteger(headSeq) ? headSeq : null;
  }

  function viewModel() {
    const messages = state.messageOrder
      .map(key => state.messages.get(key))
      .filter(Boolean)
      .sort((a, b) => a.atEpoch - b.atEpoch);
    return {
      messages,
      audit: [...state.audit],
      status: "ready",
      error: null,
      epoch: state.epoch,
      lastSeq: state.lastSeq,
      resetRequired: state.resetRequired,
    };
  }

  return {state, applyDurable, applyEnvelope, applySnapshot, viewModel};
}

export function shouldLiveStream({
  hidden,
  task,
  selectedTab,
  focusMode,
  selectedRole,
}) {
  return Boolean(
    !hidden
    && task
    && task.status === "RUNNING"
    && selectedTab === "live"
    && focusMode === "FOLLOW"
    && task.active_role
    && selectedRole === task.active_role
  );
}

export function createLiveStreamController({
  snapshotLoader,
  onView,
  eventSourceFactory = url => new EventSource(url),
}) {
  let source = null;
  let scopeKey = null;
  let generation = 0;
  let bootstrapGeneration = 0;
  let reducer = createLiveReducer();
  let bootstrapping = false;
  let buffer = [];

  function close() {
    generation += 1;
    bootstrapGeneration += 1;
    if (source) source.close();
    source = null;
    scopeKey = null;
    bootstrapping = false;
    buffer = [];
  }

  function emit(extra = {}) {
    onView({...reducer.viewModel(), ...extra});
  }

  async function bootstrap(scope, control, seed = []) {
    const connectionGeneration = generation;
    const token = ++bootstrapGeneration;
    bootstrapping = true;
    buffer = [...seed];
    emit({status: "loading", error: null});
    try {
      const snapshot = await snapshotLoader(scope);
      if (
        connectionGeneration !== generation
        || token !== bootstrapGeneration
        || scopeKey !== scope.key
      ) return;
      reducer = createLiveReducer();
      const headSeq = Number(control.seq);
      reducer.applySnapshot(snapshot?.items || [], {
        epoch: control.stream_epoch || null,
        headSeq: Number.isInteger(headSeq) ? headSeq : null,
      });
      const pending = buffer
        .filter(item => String(item.stream_epoch || "") === String(control.stream_epoch || ""))
        .sort((a, b) => Number(a.seq) - Number(b.seq));
      bootstrapping = false;
      buffer = [];
      for (const item of pending) {
        if (Number(item.seq) <= headSeq) continue;
        if (!reducer.applyEnvelope(item)) {
          bootstrap(scope, {
            stream_epoch: item.stream_epoch,
            seq: Number(item.seq),
          }, [item]);
          return;
        }
      }
      emit();
    } catch (error) {
      if (
        connectionGeneration !== generation
        || token !== bootstrapGeneration
        || scopeKey !== scope.key
      ) return;
      bootstrapping = false;
      buffer = [];
      emit({status: "error", error: error?.message || String(error)});
    }
  }

  function sync(scope) {
    if (!scope) {
      close();
      return;
    }
    if (scopeKey === scope.key && source) return;
    close();
    scopeKey = scope.key;
    reducer = createLiveReducer();
    const localGeneration = generation;
    source = eventSourceFactory(scope.url);

    const current = () => localGeneration === generation && scopeKey === scope.key;
    const handleControl = event => {
      if (!current()) return;
      try { bootstrap(scope, JSON.parse(event.data)); } catch {}
    };
    const handleData = event => {
      if (!current()) return;
      let envelope;
      try { envelope = JSON.parse(event.data); }
      catch { return; }
      if (bootstrapping) {
        buffer.push(envelope);
        return;
      }
      if (!reducer.applyEnvelope(envelope)) {
        bootstrap(scope, {
          stream_epoch: envelope.stream_epoch,
          seq: Number(envelope.seq),
        }, [envelope]);
        return;
      }
      emit();
    };

    source.addEventListener("stream.ready", handleControl);
    source.addEventListener("stream.reset", handleControl);
    source.addEventListener("transcript.upsert", handleData);
    source.addEventListener("audit.append", handleData);
    source.onerror = () => {
      if (current()) emit({status: "reconnecting", error: null});
    };
  }

  return {
    sync,
    close,
    get source() { return source; },
    get scopeKey() { return scopeKey; },
  };
}
