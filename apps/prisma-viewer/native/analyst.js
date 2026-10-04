import { createPrismaSession } from '../src/chat.js';
import { allowedActions, validateSnapshot } from '../src/model.js';
import { text } from './territorialLayer.js';
import { bindPanelDisclosure, collapsePanelOnEscape } from '../.upstream/src/ui/panelDisclosure.js';

export function createAgentFlowLayer() {
  let enabled = false;
  const listeners = new Set();
  const publish = (value) => { enabled = value; for (const listener of listeners) listener(enabled); };
  return {
    id: 'agent-flow', name: 'Agent Flow', icon: '◉', source: 'OCI AIDP · Workflow',
    init() {}, update() {}, enable() { publish(true); }, disable() { publish(false); },
    destroy() { publish(false); listeners.clear(); },
    isEnabled: () => enabled,
    subscribe(listener) { listeners.add(listener); listener(enabled); return () => listeners.delete(listener); },
  };
}

export function chatContext(state, sensorContext = {}) {
  if (!state.snapshot.version) throw new Error('Wait for a published territorial snapshot before asking AIDP.');
  return { version: state.snapshot.version, filters: { ...state.filters }, ...(state.selectedId ? { incident_id: state.selectedId } : {}),
    ...(sensorContext.enabled !== false && sensorContext.sensor_id ? { sensor_id: sensorContext.sensor_id } : {}) };
}

function renderAidpReply(reply, snapshot, { message, layer, state, status, showEvidence, showSensor }) {
  const entry = message('assistant', reply.answer);
  entry.append(text('small', `${reply.runtime === 'aidp' ? 'AIDP agent' : 'Local fixture response · no model inference'} · Publication ${reply.version}`));
  const evidenceIds = new Set(snapshot.evidence.map((item) => item.id));
  for (const id of reply.evidence_ids || []) {
    if (!evidenceIds.has(id)) continue;
    const button = text('button', `Evidence ${id}`); button.type = 'button';
    button.addEventListener('click', () => { if (state().snapshot.version !== reply.version) { status.textContent = 'This answer belongs to an earlier publication. Ask again to inspect current evidence.'; return; } showEvidence(id); }); entry.append(button);
  }
  const sensorIds = new Set((snapshot.sensors || []).map((item) => item.id));
  for (const id of reply.sensor_evidence_ids || []) {
    if (!sensorIds.has(id) || !showSensor) continue;
    const button = text('button', `Sensor evidence ${id}`); button.type = 'button';
    button.addEventListener('click', () => { if (state().snapshot.version !== reply.version) { status.textContent = 'This answer belongs to an earlier publication. Ask again to inspect current sensor evidence.'; return; } showSensor(id); }); entry.append(button);
  }
  for (const action of allowedActions(reply.actions, snapshot)) {
    const button = text('button', action.type === 'focus_incident' ? 'Focus event on map' : 'Apply suggested filters'); button.type = 'button';
    button.addEventListener('click', () => {
      if (state().snapshot.version !== reply.version) { status.textContent = 'The publication changed. Ask again before applying this action.'; return; }
      if (action.type === 'focus_incident') layer.select(action.incident_id);
      else layer.setFilters(action.filters);
    }); entry.append(button);
  }
}

export function mountAnalyst({ layer, agentFlow, request, showEvidence, showSensor, sensorContext = () => ({}), refreshSensors, signal, setPanelCollapsed }) {
  const panel = document.createElement('section'); panel.id = 'territorial-analyst'; panel.className = 'tc-agent-panel panel-collapsible collapsed'; panel.dataset.panelId = panel.id;
  panel.innerHTML = `<div class="panel-glow"></div><div class="global-context-panel-inner"><div class="panel-header">
    <span class="panel-title">AGENT FLOW</span><span class="panel-divider"></span>
    <button class="tc-new-conversation" type="button" data-new aria-label="New conversation" title="New conversation"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M12 5H5v14h14v-7M15 4h5v5m-9 4 9-9" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
    <button class="panel-collapse-btn" data-dock-toggle-target="territorial-analyst" type="button" aria-expanded="false" aria-label="Expand Agent Flow" title="Expand Agent Flow"><span aria-hidden="true">▶</span></button>
    </div><div class="tc-panel-body data-toggle-list" data-rail-scroller>
    <div id="tc-aidp-log" class="tc-chat-log data-toggle-list" role="log" aria-label="Agent Flow conversation" tabindex="0" data-rail-scroller></div>
    <p data-chat-status role="status" aria-live="polite"></p><button type="button" data-cancel hidden>Cancel request</button>
    <form class="tc-composer"><label class="tc-sr-only" for="tc-question">Ask Agent Flow</label><textarea id="tc-question" maxlength="2000" rows="3" placeholder="Ask Agent Flow…" required></textarea><span data-turns aria-label="Submitted questions">0 questions</span><button type="submit" class="tc-send" aria-label="Send question"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></button></form></div></div>`;
  document.getElementById('global-context-panel').after(panel);
  const onChange = (collapsed) => setPanelCollapsed(panel.id, collapsed, { explicit: true, persist: false, syncShare: false });
  const disclosure = bindPanelDisclosure({ panel, buttons: [panel.querySelector('.panel-collapse-btn')], onChange, onEscape: (event) => collapsePanelOnEscape(event, { panel, onChange }) });
  const form = panel.querySelector('form'); const textarea = form.querySelector('textarea'); const status = panel.querySelector('[data-chat-status]');
  const log = panel.querySelector('#tc-aidp-log');
  let busy = false, aidpSnapshot, aidpSession, lastReply, lastError, chatSnapshot, publicationRequest, questionGeneration = 0;
  const setBusy = (value) => { busy = value; form.querySelector('button').disabled = value; form.setAttribute('aria-busy', String(value)); panel.querySelector('[data-cancel]').hidden = !value; };
  const message = (role, content) => { const entry = text('article', '', `tc-message tc-message-${role}`); entry.append(text('strong', role === 'user' ? 'You' : 'Agent Flow'), text('p', content)); log.append(entry); entry.scrollIntoView({ block: 'nearest' }); return entry; };
  const contextState = () => {
    const state = layer.state(), sensor = sensorContext();
    const sensorSelected = sensor.enabled !== false && Boolean(sensor.sensor_id);
    const useSensors = sensor.enabled !== false && sensor.snapshot?.version && (sensorSelected || state.enabled === false || !state.snapshot.version);
    const visibleSnapshot = useSensors ? sensor.snapshot : state.snapshot;
    const needsPublication = (state.enabled === false && sensor.enabled === false) || !visibleSnapshot.version;
    const snapshot = needsPublication ? chatSnapshot || visibleSnapshot : visibleSnapshot;
    return { ...state, snapshot, needsPublication, filters: sensorSelected || state.enabled === false ? {} : state.filters,
      selectedId: state.enabled !== false && snapshot.incidents.some((item) => item.id === state.selectedId) ? state.selectedId : null };
  };

  function newAidpSession() {
    return createPrismaSession({ request, context: () => { const state = contextState(); aidpSnapshot = state.snapshot; return chatContext(state, sensorContext()); },
      onReply: (reply) => { lastReply = reply; renderAidpReply(reply, aidpSnapshot, { message, layer, state: contextState, status, showEvidence, showSensor }); }, onBusy: setBusy, onSubmitted: (value) => { panel.querySelector('[data-turns]').textContent = `${value} ${value === 1 ? 'question' : 'questions'}`; },
      onError: (error) => { lastError = error; status.textContent = error.status === 409 ? 'The publication changed. Your question is preserved; review the refreshed data and send it again.' : error.message; if (error.status === 409) void Promise.allSettled([layer.update(), refreshSensors?.()]); },
    });
  }
  aidpSession = newAidpSession(); void aidpSession.start();

  function cancel() { questionGeneration++; publicationRequest?.abort(); publicationRequest = undefined; aidpSession.stop(); setBusy(false); }
  async function ask(question, { signal: turnSignal } = {}) {
    if (busy) throw new Error('Agent Flow is already processing a question.');
    turnSignal?.throwIfAborted(); signal.throwIfAborted();
    if (!agentFlow.isEnabled()) throw new DOMException('Agent Flow is off', 'AbortError');
    const generation = ++questionGeneration;
    onChange(false); status.textContent = ''; lastReply = undefined; lastError = undefined;
    message('user', question); setBusy(true);
    turnSignal?.addEventListener('abort', cancel, { once: true });
    try {
      if (contextState().needsPublication) {
        const pending = new AbortController(); publicationRequest = pending;
        const requestSignal = AbortSignal.any([signal, pending.signal, ...[turnSignal].filter(Boolean)]);
        const snapshot = validateSnapshot(await request('/api/prisma/snapshot', { signal: requestSignal }));
        requestSignal.throwIfAborted();
        if (generation !== questionGeneration) throw new DOMException('Question cancelled', 'AbortError');
        chatSnapshot = snapshot; publicationRequest = undefined;
      }
      chatContext(contextState(), sensorContext());
      await aidpSession.start(); turnSignal?.throwIfAborted(); signal.throwIfAborted();
      if (generation !== questionGeneration) throw new DOMException('Question cancelled', 'AbortError');
      await aidpSession.sendText(question);
      turnSignal?.throwIfAborted(); signal.throwIfAborted();
      if (generation !== questionGeneration) throw new DOMException('Question cancelled', 'AbortError');
      if (lastError) throw lastError;
      if (!lastReply) throw new Error('Agent Flow did not return an answer.');
      return lastReply;
    } finally { turnSignal?.removeEventListener('abort', cancel); if (generation === questionGeneration) { publicationRequest = undefined; setBusy(false); } }
  }
  panel.querySelector('[data-cancel]').addEventListener('click', () => { cancel(); status.textContent = 'Request cancelled.'; }, { signal });
  panel.querySelector('[data-new]').addEventListener('click', () => { cancel(); log.replaceChildren(); status.textContent = ''; textarea.value = ''; aidpSession.destroy(); aidpSession = newAidpSession(); void aidpSession.start(); textarea.focus(); }, { signal });
  textarea.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    if (!event.repeat) form.requestSubmit();
  }, { signal });
  form.addEventListener('submit', (event) => {
    event.preventDefault(); const question = textarea.value.trim(); if (!question || busy) return;
    void ask(question).catch((error) => { if (!signal.aborted && error.name !== 'AbortError') status.textContent = error.message; });
  }, { signal });
  let wasEnabled = false;
  const unsubscribe = agentFlow.subscribe((enabled) => {
    panel.hidden = !enabled;
    if (enabled) onChange(false);
    else if (wasEnabled) { cancel(); onChange(true); }
    wasEnabled = enabled;
  });
  signal.addEventListener('abort', () => { unsubscribe(); cancel(); aidpSession.destroy(); disclosure.destroy(); panel.remove(); }, { once: true });
  return { panel, ask };
}
