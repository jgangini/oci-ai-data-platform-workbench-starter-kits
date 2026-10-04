import { GEV_ACTION_SCHEMAS } from '../.upstream/src/voice/actionSchemas.js';
import { validateValue } from '../.upstream/src/tools/schema.js';
import { microphoneRecording, voiceAudioContext, voiceBase64 } from './voiceAudio.js';
import { TERRITORIAL_LAYER_ID } from './territorialLayer.js';

const PROVIDER_KEY = 'gev-native-voice-provider';
const ACTIONS = new Set(['fly_to_location', 'adjust_camera_zoom', 'zoom_to_globe', 'set_layer_visibility', 'ask_aidp']);
const LAYER_PARAMETERS = structuredClone(GEV_ACTION_SCHEMAS.find((item) => item.name === 'set_layer_visibility').parameters);
LAYER_PARAMETERS.properties.layerId.enum.push(TERRITORIAL_LAYER_ID, 'sensors');
const AIDP_PARAMETERS = { type: 'object', required: ['question'], additionalProperties: false,
  properties: { question: { type: 'string', minLength: 1, maxLength: 2000, pattern: '\\S' } } };

export function selectedVoiceProvider(storage = globalThis.localStorage, configuredOci = false) {
  const fallback = configuredOci ? 'oci' : 'openai';
  try { const saved = storage?.getItem(PROVIDER_KEY); return ['oci', 'openai'].includes(saved) ? saved : fallback; } catch { return fallback; }
}

export function voiceActions(actions) {
  if (!Array.isArray(actions)) return [];
  return actions.slice(0, 4).filter((action) => {
    if (!ACTIONS.has(action?.name)) return false;
    if (action.name === 'ask_aidp') return validateValue(AIDP_PARAMETERS, action.arguments).length === 0;
    const schema = GEV_ACTION_SCHEMAS.find((item) => item.name === action.name);
    if (validateValue(action.name === 'set_layer_visibility' ? LAYER_PARAMETERS : schema.parameters, action.arguments).length) return false;
    if (action.name !== 'fly_to_location') return true;
    return Number.isFinite(action.arguments.latitude) && Number.isFinite(action.arguments.longitude) && !('query' in action.arguments) && !('locationId' in action.arguments);
  });
}

async function runVoiceActions(reply, { turn, runAction, askAgent, emit }) {
  for (const action of voiceActions(reply.actions)) {
    turn.signal.throwIfAborted();
    emit({ type: 'state', state: 'executing', detail: action.name === 'ask_aidp' ? 'Consulting Agent Flow' : `Map: ${action.name.replaceAll('_', ' ')}` });
    if (action.name === 'ask_aidp' && !askAgent) throw new Error('Agent Flow is not available.');
    const result = action.name === 'ask_aidp'
      ? await askAgent(action.arguments.question, { signal: turn.signal })
      : await runAction(action.name, action.arguments, { signal: turn.signal });
    turn.signal.throwIfAborted();
    if (result?.ok === false) throw new Error('The requested map action could not be completed.');
  }
}

/** OCI turn adapter; native session owns UI lifetime, action cancellation and ON/OFF. */
export function createOciVoiceSession(options) { return new OciVoiceSession(options); }

class OciVoiceSession {
  capabilities = { costControls: false, pushToTalk: false };

  constructor({ ui, emit, runAction, askAgent, signal, request, recordMicrophone = microphoneRecording, createAudio = voiceAudioContext }) {
    Object.assign(this, { ui, emit, runAction, askAgent, signal, request, recordMicrophone, createAudio });
    this.state = { turn: null, recording: null, audio: null, history: [], finishing: false };
    signal.addEventListener('abort', () => this.clearTurn(), { once: true });
  }

  current(turn) { return this.state.turn === turn && !turn.signal.aborted && !this.signal.aborted; }
  status(value, detail) { this.emit({ type: 'state', state: value, detail }); }

  clearTurn(closeAudio = true) {
    const { state, ui } = this;
    state.turn?.abort(); state.recording?.cancel();
    if (closeAudio) { state.audio?.close(); state.audio = null; }
    state.recording = null; state.turn = null; state.finishing = false;
    ui.root.dataset.microphone = 'muted'; ui.root.dataset.speaker = 'idle';
  }

  async submit(wav, turn) {
    const { state, request, signal, emit, runAction, askAgent, ui } = this;
    this.status('thinking', 'OCI is processing audio');
    const reply = await request('/api/prisma/oci-voice/turn', { method: 'POST', body: JSON.stringify({ audio_base64: voiceBase64(wav), mime: 'audio/wav', history: state.history.slice(-10) }), signal: AbortSignal.any([signal, turn.signal]) });
    if (!this.current(turn)) return;
    if (typeof reply.transcript !== 'string' || !reply.transcript.trim() || typeof reply.answer !== 'string' || !reply.answer.trim()) throw new Error('OCI did not return a complete voice turn.');
    state.history.push({ role: 'user', content: reply.transcript.slice(0, 2000) }, { role: 'assistant', content: reply.answer.slice(0, 2000) }); state.history = state.history.slice(-10);
    emit({ type: 'transcript', role: 'user', text: reply.transcript, source: 'microphone' });
    emit({ type: 'transcript', role: 'assistant', text: reply.answer });
    await runVoiceActions(reply, { turn, runAction, askAgent, emit }); if (!this.current(turn)) return;
    this.status('speaking', 'Playing OCI generated voice'); ui.root.dataset.speaker = 'ai';
    await state.audio.play(reply.audio_base64, reply.mime, turn.signal);
    if (this.current(turn)) { ui.root.dataset.speaker = 'idle'; emit({ type: 'completion', source: 'microphone' }); }
  }

  failure(error, turn) {
    if (!this.current(turn)) return;
    this.clearTurn(); this.status('error', error.message);
  }

  async finish(hasSpeech) {
    const { state, ui } = this;
    const turn = state.turn;
    if (!state.recording || state.finishing) return;
    state.finishing = true;
    try {
      // Silence-only windows recycle locally and never incur an inference request.
      const wav = hasSpeech ? await state.recording.finish() : (state.recording.cancel(), null);
      if (!this.current(turn)) return;
      state.recording = null; ui.root.dataset.microphone = 'muted'; ui.root.dataset.speaker = 'idle';
      if (wav) await this.submit(wav, turn);
      if (this.current(turn)) await this.beginRecording();
    } catch (error) { this.failure(error, turn); }
  }

  async beginRecording() {
    const { state, ui, signal } = this;
    this.clearTurn(false);
    const turn = new AbortController(); state.turn = turn;
    this.status('connecting', 'Requesting microphone');
    try {
      state.audio ||= this.createAudio(); await state.audio.unlock(); if (!this.current(turn)) return;
      const recording = await this.recordMicrophone({ signal: AbortSignal.any([signal, turn.signal]), context: state.audio.context,
        onSpeechEnd: (hasSpeech) => { if (this.current(turn)) void this.finish(hasSpeech); }, onError: (error) => this.failure(error, turn) });
      if (!this.current(turn)) { recording.cancel(); return; }
      state.recording = recording; ui.root.dataset.microphone = 'active'; ui.root.dataset.speaker = 'user';
      this.status('listening', 'Listening · pause to send · OFF to end');
    } catch (error) { this.failure(error, turn); }
  }

  start() { return this.beginRecording(); }
  stop() { this.clearTurn(); this.state.history = []; }
  sendText() { throw new Error('OCI voice requires recorded audio. Use OCI Assistant for text.'); }
  sendMapEvent() {}
  bindControls() {
    this.ui.root.dataset.provider = 'oci';
    const tag = this.ui.root.querySelector('.gev-voice-kicker');
    tag.textContent = 'OCI Voice'; tag.className = 'gev-voice-tier-btn gev-voice-provider';
    this.ui.tierButton.before(tag);
    this.ui.helpDetail.textContent = 'ON starts listening · Pause to send · Listening resumes after the reply · OFF ends the conversation';
  }
}
