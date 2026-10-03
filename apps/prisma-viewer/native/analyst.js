import { createPrismaSession } from '../src/chat.js';
import { allowedActions } from '../src/model.js';
import { text } from './territorialLayer.js';

export function chatContext(state) {
  if (!state.snapshot.version) throw new Error('Wait for a published territorial snapshot before asking AIDP.');
  return { version: state.snapshot.version, filters: { ...state.filters }, ...(state.selectedId ? { incident_id: state.selectedId } : {}) };
}

export function ociPayload(question, history, state) {
  // Native OCI is a general assistant, not an alternative source of incident evidence.
  return { question, history: history.slice(-10).map(({ role, content }) => ({ role, content: content.slice(0, 2000) })), context: { version: state.snapshot.version || '', ...(state.filters.locality ? { locality: state.filters.locality } : {}), ...(state.selectedId ? { incident_id: state.selectedId } : {}) } };
}

function renderAidpReply(reply, snapshot, { message, layer, status, showEvidence }) {
  const entry = message('aidp', 'assistant', reply.answer);
  entry.append(text('small', `${reply.runtime === 'aidp' ? 'AIDP agent' : 'Local fixture response · no model inference'} · Publication ${reply.version}`));
  const evidenceIds = new Set(snapshot.evidence.map((item) => item.id));
  for (const id of reply.evidence_ids || []) {
    if (!evidenceIds.has(id)) continue;
    const button = text('button', `Evidence ${id}`); button.type = 'button';
    button.addEventListener('click', () => { if (layer.state().snapshot.version !== reply.version) { status.textContent = 'This answer belongs to an earlier publication. Ask again to inspect current evidence.'; return; } showEvidence(id); }); entry.append(button);
  }
  for (const action of allowedActions(reply.actions, snapshot)) {
    const button = text('button', action.type === 'focus_incident' ? 'Focus event on map' : 'Apply suggested filters'); button.type = 'button';
    button.addEventListener('click', () => {
      if (layer.state().snapshot.version !== reply.version) { status.textContent = 'The publication changed. Ask again before applying this action.'; return; }
      if (action.type === 'focus_incident') layer.select(action.incident_id);
      else layer.setFilters(action.filters);
    }); entry.append(button);
  }
}

function bindAssistantTabs(panel, selectProvider, signal) {
  for (const button of panel.querySelectorAll('[role="tab"]')) {
    button.addEventListener('click', () => selectProvider(button.dataset.provider), { signal });
    button.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const provider = button.dataset.provider;
      const next = event.key === 'Home' ? 'aidp' : event.key === 'End' ? 'oci' : provider === 'aidp' ? 'oci' : 'aidp';
      selectProvider(next); panel.querySelector(`[data-provider="${next}"]`).focus();
    }, { signal });
  }
}

function renderAssistantTab(panel, logs, provider) {
  for (const button of panel.querySelectorAll('[role="tab"]')) { const selected = button.dataset.provider === provider; button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1; }
  for (const [key, log] of Object.entries(logs)) log.hidden = key !== provider;
  panel.querySelector('[data-scope]').textContent = provider === 'aidp' ? 'AIDP answers from the selected publication and filters. Map actions require your confirmation.' : 'OCI general text assistant. Its answers are not verified incident evidence. Native voice remains a separate provider.';
}

async function sendOciQuestion(question, conversation, { request, layer, signal, setBusy, message, status }) {
  const turn = new AbortController(); conversation.pending = turn; setBusy(true);
  try {
    const reply = await request('/api/prisma/oci-chat', { method: 'POST', body: JSON.stringify(ociPayload(question, conversation.history, layer.state())), signal: AbortSignal.any([signal, turn.signal]) });
    if (turn.signal.aborted || conversation.pending !== turn) return;
    if (typeof reply.answer !== 'string' || !reply.answer.trim()) throw new Error('OCI did not return a text answer.');
    message('oci', 'assistant', reply.answer).append(text('small', `OCI · ${reply.model_id || 'Configured model'} · General text`));
    conversation.history.push({ role: 'user', content: question }, { role: 'assistant', content: reply.answer }); conversation.history = conversation.history.slice(-10);
  } catch (error) { if (!turn.signal.aborted) status.textContent = error.message; }
  finally { if (conversation.pending === turn) { conversation.pending = undefined; setBusy(false); } }
}

export function mountAnalyst({ layer, request, showEvidence, signal }) {
  const panel = document.createElement('details'); panel.id = 'territorial-analyst'; panel.className = 'tc-panel tc-analyst';
  panel.innerHTML = `<summary>AI assistants</summary><div class="tc-panel-body">
    <div class="tc-tabs" role="tablist" aria-label="Assistant provider"><button role="tab" id="tc-aidp-tab" aria-controls="tc-aidp-log" aria-selected="true" data-provider="aidp">AIDP Analyst</button><button role="tab" id="tc-oci-tab" aria-controls="tc-oci-log" aria-selected="false" tabindex="-1" data-provider="oci">OCI Assistant</button></div>
    <p data-scope>AIDP answers from the selected publication and filters. Map actions require your confirmation.</p>
    <div id="tc-aidp-log" class="tc-chat-log" role="tabpanel" aria-labelledby="tc-aidp-tab" tabindex="0"></div><div id="tc-oci-log" class="tc-chat-log" role="tabpanel" aria-labelledby="tc-oci-tab" tabindex="0" hidden></div>
    <p data-chat-status role="status" aria-live="polite"></p><div class="tc-actions"><button type="button" data-new>New conversation</button><button type="button" data-cancel hidden>Cancel request</button></div>
    <form class="tc-composer"><label class="tc-sr-only" for="tc-question">Ask the selected assistant</label><textarea id="tc-question" maxlength="2000" rows="3" placeholder="Ask about Bogotá…" required></textarea><span data-turns aria-label="Submitted questions">0 questions</span><button type="submit" class="tc-send" aria-label="Send question"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></button></form></div>`;
  document.body.append(panel);
  panel.addEventListener('keydown', (event) => { if (event.key === 'Escape' && panel.open) { event.preventDefault(); event.stopPropagation(); panel.open = false; panel.querySelector('summary').focus(); } }, { signal });
  const form = panel.querySelector('form'); const textarea = form.querySelector('textarea'); const status = panel.querySelector('[data-chat-status]');
  const logs = { aidp: panel.querySelector('#tc-aidp-log'), oci: panel.querySelector('#tc-oci-log') };
  const questions = { aidp: 0, oci: 0 }; const oci = { pending: undefined, history: [] };
  let provider = 'aidp', busy = false, aidpSnapshot, aidpSession;
  const count = () => { panel.querySelector('[data-turns]').textContent = `${questions[provider]} questions`; };
  const setBusy = (value) => { busy = value; form.querySelector('button').disabled = value; form.setAttribute('aria-busy', String(value)); panel.querySelector('[data-cancel]').hidden = !value; };
  const message = (channel, role, content) => { const entry = text('article', '', `tc-message tc-message-${role}`); entry.append(text('strong', role === 'user' ? 'You' : channel === 'aidp' ? 'AIDP Analyst' : 'OCI Assistant'), text('p', content)); logs[channel].append(entry); entry.scrollIntoView({ block: 'nearest' }); return entry; };

  function newAidpSession() {
    return createPrismaSession({ request, context: () => { const state = layer.state(); aidpSnapshot = state.snapshot; return chatContext(state); },
      onReply: (reply) => renderAidpReply(reply, aidpSnapshot, { message, layer, status, showEvidence }), onBusy: setBusy, onSubmitted: (value) => { questions.aidp = value; count(); },
      onError: (error) => { status.textContent = error.status === 409 ? 'The publication changed. Your question is preserved; review the refreshed data and send it again.' : error.message; if (error.status === 409) void layer.update(); },
    });
  }
  aidpSession = newAidpSession(); void aidpSession.start();

  function cancel() { oci.pending?.abort(); oci.pending = undefined; aidpSession.stop(); setBusy(false); }
  function selectProvider(value) {
    cancel(); provider = value; status.textContent = ''; count();
    renderAssistantTab(panel, logs, provider);
    if (provider === 'aidp') void aidpSession.start();
  }
  bindAssistantTabs(panel, selectProvider, signal);
  panel.querySelector('[data-cancel]').addEventListener('click', () => { cancel(); status.textContent = 'Request cancelled.'; }, { signal });
  panel.querySelector('[data-new]').addEventListener('click', () => { cancel(); logs[provider].replaceChildren(); status.textContent = ''; textarea.value = ''; if (provider === 'aidp') { aidpSession.destroy(); aidpSession = newAidpSession(); void aidpSession.start(); } else { oci.history = []; questions.oci = 0; count(); } }, { signal });
  form.addEventListener('submit', (event) => {
    event.preventDefault(); const question = textarea.value.trim(); if (!question || busy) return;
    status.textContent = '';
    try { if (provider === 'aidp') chatContext(layer.state()); }
    catch (error) { status.textContent = error.message; return; }
    message(provider, 'user', question);
    if (provider === 'aidp') { void aidpSession.start(); void aidpSession.sendText(question); }
    else { questions.oci++; count(); void sendOciQuestion(question, oci, { request, layer, signal, setBusy, message, status }); }
  }, { signal });
  signal.addEventListener('abort', () => { cancel(); aidpSession.destroy(); panel.remove(); }, { once: true });
  return { panel };
}
