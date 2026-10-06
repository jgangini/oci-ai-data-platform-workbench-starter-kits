import { createGodsEyeViewSession } from '../src/chat.js';
import { allowedActions, validateSnapshot } from '../src/model.js';
import { text } from './godsEyeViewLayer.js';
import { bindPanelDisclosure, collapsePanelOnEscape } from '../.upstream/src/ui/panelDisclosure.js';

const messageTime = new Intl.DateTimeFormat('es-CO', { hour: 'numeric', minute: '2-digit', hour12: true });
const questionLimit = 500;
const sendIcon = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M10.3009 13.6949L20.102 3.89742M10.5795 14.1355L12.8019 18.5804C13.339 19.6545 13.6075 20.1916 13.9458 20.3356C14.2394 20.4606 14.575 20.4379 14.8492 20.2747C15.1651 20.0866 15.3591 19.5183 15.7472 18.3818L19.9463 6.08434C20.2845 5.09409 20.4535 4.59896 20.3378 4.27142C20.2371 3.98648 20.013 3.76234 19.7281 3.66167C19.4005 3.54595 18.9054 3.71502 17.9151 4.05315L5.61763 8.2523C4.48114 8.64037 3.91289 8.83441 3.72478 9.15032C3.56153 9.42447 3.53891 9.76007 3.66389 10.0536C3.80791 10.3919 4.34498 10.6605 5.41912 11.1975L9.86397 13.42C10.041 13.5085 10.1295 13.5527 10.2061 13.6118C10.2742 13.6643 10.3352 13.7253 10.3876 13.7933C10.4468 13.87 10.491 13.9585 10.5795 14.1355Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const stopIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill-rule="evenodd" clip-rule="evenodd" d="M12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2C6.47715 2 2 6.47715 2 12C2 17.5228 6.47715 22 12 22ZM8.58579 8.58579C8 9.17157 8 10.1144 8 12C8 13.8856 8 14.8284 8.58579 15.4142C9.17157 16 10.1144 16 12 16C13.8856 16 14.8284 16 15.4142 15.4142C16 14.8284 16 13.8856 16 12C16 10.1144 16 9.17157 15.4142 8.58579C14.8284 8 13.8856 8 12 8C10.1144 8 9.17157 8 8.58579 8.58579Z" fill="currentColor"/></svg>';

export function createAgentFlowLayer() {
  let enabled = false;
  const listeners = new Set();
  const publish = (value) => { enabled = value; for (const listener of listeners) listener(enabled); };
  return {
    id: 'agent-flow', name: 'AI Assistant', icon: '◉', source: 'OCI AIDP · Agent Flow',
    init() {}, update() {}, enable() { publish(true); }, disable() { publish(false); },
    destroy() { publish(false); listeners.clear(); },
    isEnabled: () => enabled,
    subscribe(listener) { listeners.add(listener); listener(enabled); return () => listeners.delete(listener); },
  };
}

export function chatContext(state, sensorContext = {}) {
  if (!state.snapshot.version) throw new Error('Wait for a published God’s Eye View snapshot before asking AIDP.');
  return { version: state.snapshot.version, filters: { ...state.filters }, ...(state.selectedId ? { incident_id: state.selectedId } : {}),
    ...(sensorContext.enabled !== false && sensorContext.sensor_id ? { sensor_id: sensorContext.sensor_id } : {}) };
}

function renderAidpReply(reply, snapshot, { message, layer, state, status, showEvidence, showSensor, onChange }) {
  const entry = message('assistant', reply.answer);
  if (reply.runtime !== 'aidp') entry.append(text('small', 'Local fixture response · no model inference'));
  const evidenceIds = new Set(snapshot.evidence.map((item) => item.id));
  for (const id of reply.evidence_ids || []) {
    if (!evidenceIds.has(id)) continue;
    const button = text('button', `Evidence ${id}`); button.type = 'button';
    button.addEventListener('click', () => { if (state().snapshot.version !== reply.version) { status.textContent = 'This answer belongs to an earlier publication. Ask again to inspect current evidence.'; return; } onChange(true); showEvidence(id); document.getElementById('gods-eye-view-panel')?.querySelector('.panel-collapse-btn')?.focus(); }); entry.append(button);
  }
  const sensorIds = new Set((snapshot.sensors || []).map((item) => item.id));
  for (const id of reply.sensor_evidence_ids || []) {
    if (!sensorIds.has(id) || !showSensor) continue;
    const button = text('button', `Sensor evidence ${id}`); button.type = 'button';
    button.addEventListener('click', () => { if (state().snapshot.version !== reply.version) { status.textContent = 'This answer belongs to an earlier publication. Ask again to inspect current sensor evidence.'; return; } onChange(true); showSensor(id); document.getElementById('sensors-panel')?.querySelector('.panel-collapse-btn')?.focus(); }); entry.append(button);
  }
  for (const action of allowedActions(reply.actions, snapshot)) {
    const button = text('button', action.type === 'focus_incident' ? 'Focus event on map' : 'Apply suggested filters'); button.type = 'button';
    button.addEventListener('click', () => {
      if (state().snapshot.version !== reply.version) { status.textContent = 'The publication changed. Ask again before applying this action.'; return; }
      if (action.type === 'focus_incident') { onChange(true); layer.select(action.incident_id); document.getElementById('gods-eye-view-panel')?.querySelector('.panel-collapse-btn')?.focus(); }
      else layer.setFilters(action.filters);
    }); entry.append(button);
  }
}

export function mountAnalyst({ layer, agentFlow, request, showEvidence, showSensor, sensorContext = () => ({}), refreshSensors, signal, setPanelCollapsed }) {
  const panel = document.createElement('section'); panel.id = 'gods-eye-view-analyst'; panel.className = 'gev-agent-panel panel-collapsible collapsed'; panel.dataset.panelId = panel.id; panel.dataset.railExclusive = '';
  panel.innerHTML = `<div class="panel-glow"></div><div class="global-context-panel-inner"><div class="panel-header">
    <span class="panel-title">AI ASSISTANT</span><span class="panel-divider"></span>
    <button class="gev-new-conversation" type="button" data-new aria-label="New conversation" title="New conversation"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M12 5H5v14h14v-7M15 4h5v5m-9 4 9-9" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
    <button class="panel-collapse-btn" data-dock-toggle-target="gods-eye-view-analyst" type="button" aria-expanded="false" aria-label="Expand AI Assistant" title="Expand AI Assistant"><span aria-hidden="true">▶</span></button>
    </div><div class="gev-panel-body data-toggle-list" data-rail-scroller>
    <div id="gev-aidp-log" class="gev-chat-log scene-shot-list" role="log" aria-label="AI Assistant conversation" tabindex="0" data-rail-scroller></div>
    <p data-chat-status role="status" aria-live="polite"></p>
    <form class="gev-composer"><label class="gev-sr-only" for="gev-question">Ask AI Assistant</label><textarea id="gev-question" maxlength="${questionLimit}" rows="3" placeholder="Ask AI Assistant…" aria-describedby="gev-question-count" required></textarea><span id="gev-question-count" aria-live="polite">0/${questionLimit}</span><button type="submit" class="gev-send" aria-label="Send question" title="Send question">${sendIcon}</button></form></div></div>`;
  document.getElementById('global-context-panel').after(panel);
  const onChange = (collapsed) => setPanelCollapsed(panel.id, collapsed, { explicit: true, persist: false, syncShare: false });
  const disclosure = bindPanelDisclosure({ panel, buttons: [panel.querySelector('.panel-collapse-btn')], onChange, onEscape: (event) => collapsePanelOnEscape(event, { panel, onChange }) });
  const form = panel.querySelector('form'); const textarea = form.querySelector('textarea'); const send = form.querySelector('button'); const status = panel.querySelector('[data-chat-status]');
  const log = panel.querySelector('#gev-aidp-log');
  const count = panel.querySelector('#gev-question-count');
  const updateCount = () => { count.textContent = `${textarea.value.length}/${questionLimit}`; };
  updateCount();
  let busy = false, aidpSnapshot, aidpSession, lastReply, lastError, chatSnapshot, publicationRequest, questionGeneration = 0, draftRevision = 0;
  const setBusy = (value) => {
    busy = value; send.type = value ? 'button' : 'submit'; send.title = value ? 'Stop request' : 'Send question';
    send.setAttribute('aria-label', send.title); send.innerHTML = value ? stopIcon : sendIcon; form.setAttribute('aria-busy', String(value));
  };
  setBusy(false);
  const message = (role, content) => {
    const now = new Date(), time = text('time', messageTime.format(now), 'gev-message-time'); time.dateTime = now.toISOString();
    const entry = text('article', '', `scene-shot-row gev-message gev-message-${role}`);
    entry.append(text('strong', role === 'user' ? 'You' : 'AI Assistant'), text('p', content), time);
    log.append(entry); entry.scrollIntoView({ block: 'nearest' }); return entry;
  };
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
    return createGodsEyeViewSession({ request, context: () => { const state = contextState(); aidpSnapshot = state.snapshot; return chatContext(state, sensorContext()); },
      onReply: (reply) => { lastReply = reply; renderAidpReply(reply, aidpSnapshot, { message, layer, state: contextState, status, showEvidence, showSensor, onChange }); }, onBusy: setBusy,
      onError: (error) => { lastError = error; status.textContent = error.status === 409 ? 'The publication changed. Your question is preserved; review the refreshed data and send it again.' : error.message; if (error.status === 409) void Promise.allSettled([layer.update(), refreshSensors?.()]); },
    });
  }
  aidpSession = newAidpSession(); void aidpSession.start();

  function cancel() { questionGeneration++; publicationRequest?.abort(); publicationRequest = undefined; aidpSession.stop(); setBusy(false); }
  async function ask(question, { signal: turnSignal } = {}) {
    if (busy) throw new Error('AI Assistant is already processing a question.');
    turnSignal?.throwIfAborted(); signal.throwIfAborted();
    if (!agentFlow.isEnabled()) throw new DOMException('AI Assistant is off', 'AbortError');
    const generation = ++questionGeneration;
    onChange(false); status.textContent = ''; lastReply = undefined; lastError = undefined;
    message('user', question); setBusy(true);
    turnSignal?.addEventListener('abort', cancel, { once: true });
    try {
      if (contextState().needsPublication) {
        const pending = new AbortController(); publicationRequest = pending;
        const requestSignal = AbortSignal.any([signal, pending.signal, ...[turnSignal].filter(Boolean)]);
        const snapshot = validateSnapshot(await request('/api/gods-eye-view/snapshot', { signal: requestSignal }));
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
      if (!lastReply) throw new Error('AI Assistant did not return an answer.');
      return lastReply;
    } finally { turnSignal?.removeEventListener('abort', cancel); if (generation === questionGeneration) { publicationRequest = undefined; setBusy(false); } }
  }
  send.addEventListener('click', (event) => { if (busy) { event.preventDefault(); cancel(); status.textContent = 'Request cancelled.'; } }, { signal });
  panel.querySelector('[data-new]').addEventListener('click', () => { cancel(); log.replaceChildren(); status.textContent = ''; textarea.value = ''; updateCount(); aidpSession.destroy(); aidpSession = newAidpSession(); void aidpSession.start(); textarea.focus(); }, { signal });
  textarea.addEventListener('input', () => { draftRevision++; updateCount(); }, { signal });
  textarea.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    if (!event.repeat) form.requestSubmit();
  }, { signal });
  form.addEventListener('submit', (event) => {
    event.preventDefault(); const draft = textarea.value, question = draft.trim(); if (!question || busy) return;
    if (draft.length > questionLimit) { status.textContent = `Keep your question within ${questionLimit} characters.`; return; }
    const pending = ask(question), generation = questionGeneration, revision = draftRevision;
    textarea.value = ''; updateCount();
    void pending.catch((error) => {
      if (signal.aborted || error.name === 'AbortError' || generation !== questionGeneration) return;
      if (revision === draftRevision && !textarea.value) { textarea.value = draft; updateCount(); }
      if (!lastError) status.textContent = error.message;
    });
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
