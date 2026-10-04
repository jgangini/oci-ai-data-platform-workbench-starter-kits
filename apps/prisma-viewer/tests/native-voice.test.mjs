import test from 'node:test';
import assert from 'node:assert/strict';
import { createVoiceSession } from '../.upstream/src/voice/session.js';
import { createGevActionRunner } from '../.upstream/src/voice/gevActions.js';
import { GEV_ACTION_SCHEMAS } from '../.upstream/src/voice/actionSchemas.js';
import { createOciVoiceSession, selectedVoiceProvider, voiceActions } from '../native/ociVoice.js';
import { encodeVoiceWav, microphoneRecording, normalizedVoiceWav, VOICE_SAMPLE_RATE } from '../native/voiceAudio.js';

test('a fresh browser uses configured OCI automatically and preserves an existing explicit provider choice', () => {
  assert.equal(selectedVoiceProvider({ getItem: () => null }), 'openai');
  assert.equal(selectedVoiceProvider({ getItem: () => 'oci' }), 'oci');
  assert.equal(selectedVoiceProvider({ getItem() { throw Error('Storage unavailable'); } }), 'openai');
  assert.equal(selectedVoiceProvider({ getItem: () => null }, true), 'oci');
  assert.equal(selectedVoiceProvider({ getItem: () => 'openai' }, true), 'openai');
  assert.equal(selectedVoiceProvider({ getItem() { throw Error('Storage unavailable'); } }, true), 'oci');
});

test('WAV encoder writes bounded mono PCM16 and normalizer downmixes real decoded samples', async () => {
  const bytes = encodeVoiceWav(new Float32Array([-2, 0, 2])); const view = new DataView(bytes.buffer);
  assert.equal(new TextDecoder().decode(bytes.subarray(0, 4)), 'RIFF'); assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 1); assert.equal(view.getUint32(24, true), 16000); assert.equal(view.getUint16(34, true), 16);
  assert.deepEqual([view.getInt16(44, true), view.getInt16(46, true), view.getInt16(48, true)], [-32768, 0, 32767]);
  assert.equal(encodeVoiceWav(new Float32Array(30 * VOICE_SAMPLE_RATE)).length, 960044);
  assert.throws(() => encodeVoiceWav(new Float32Array(30 * VOICE_SAMPLE_RATE + 1)), /30 seconds/);
  const context = { decodeAudioData: async () => ({ duration: 1 / 16000, sampleRate: 48000, numberOfChannels: 2, getChannelData: (channel) => new Float32Array(channel ? [0, 0, 0] : [1, 1, 1]) }) };
  const normalized = await normalizedVoiceWav(new ArrayBuffer(0), context);
  assert.equal(new DataView(normalized.buffer).getInt16(44, true), 16384);
  context.decodeAudioData = async () => ({ duration: 30.01, sampleRate: 16000, numberOfChannels: 1, getChannelData: () => new Float32Array(480160) });
  await assert.rejects(normalizedVoiceWav(new ArrayBuffer(0), context), /30 seconds/);
  assert.equal((await normalizedVoiceWav(new ArrayBuffer(0), context, { trim: true })).length, 960044);
});

test('voice map actions use native schema limits and exclude queries, incident actions and unknown commands', () => {
  const fly = { name: 'fly_to_location', arguments: { latitude: 4.66, longitude: -74.08, rangeM: 38000, viewMode: 'overview' } };
  const zoom = { name: 'adjust_camera_zoom', arguments: { direction: 'in', amount: 'little' } };
  assert.deepEqual(voiceActions([fly, zoom, { name: 'zoom_to_globe', arguments: {} }]), [fly, zoom, { name: 'zoom_to_globe', arguments: {} }]);
  for (const value of [{ ...fly, arguments: { latitude: 91, longitude: 0 } }, { ...fly, arguments: { query: 'untrusted request' } }, { name: 'validate_incident', arguments: {} }, { ...zoom, arguments: { direction: 'in', amount: 'everything' } }]) assert.deepEqual(voiceActions([value]), []);
});

test('voice layer actions accept canonical native layers and Social networks without weakening the native schema', () => {
  const nativeIds = GEV_ACTION_SCHEMAS.find((schema) => schema.name === 'set_layer_visibility').parameters.properties.layerId.enum;
  assert.equal(nativeIds.includes('territorial-events'), false);
  for (const layerId of [...nativeIds, 'territorial-events', 'sensors']) {
    for (const enabled of [true, false]) {
      const action = { name: 'set_layer_visibility', arguments: { layerId, enabled } };
      assert.deepEqual(voiceActions([action]), [action]);
    }
  }
  for (const args of [
    { layerId: 'territorial-events', enabled: 'true' }, { layerId: 'territorial-events' },
    { layerId: 'unknown-layer', enabled: true }, { layerId: 'redes sociales', enabled: true },
    { layerId: 'territorial-events', enabled: true, query: 'untrusted request' },
  ]) assert.deepEqual(voiceActions([{ name: 'set_layer_visibility', arguments: args }]), []);
});

test('late microphone permission after OFF stops tracks without creating a recorder', async () => {
  const controller = new AbortController(); let resolve, stopped = 0;
  const pending = microphoneRecording({ signal: controller.signal, devices: { getUserMedia: () => new Promise((done) => { resolve = done; }) }, Recorder: class { constructor() { assert.fail('Recorder must not start after OFF'); } } });
  controller.abort(); resolve({ getTracks: () => [{ stop() { stopped++; } }] });
  await assert.rejects(pending, { name: 'AbortError' }); assert.equal(stopped, 1);
});

test('a device recording error stops the microphone and notifies the control immediately', async () => {
  let recorder, stopped = 0, failure;
  class Recorder { constructor() { recorder = this; this.state = 'inactive'; } start() { this.state = 'recording'; } stop() { this.state = 'inactive'; this.onstop(); } }
  const capture = await microphoneRecording({ signal: new AbortController().signal, context: microphoneContext(), devices: { getUserMedia: async () => ({ getTracks: () => [{ stop() { stopped++; } }] }) }, Recorder, onError: (error) => { failure = error; } });
  recorder.onerror(); assert.match(failure.message, /recording failed/); assert.ok(stopped > 0);
  await assert.rejects(capture.finish(), /recording failed/);
});

test('an unexpected microphone stop surfaces an error instead of leaving a listening session', async () => {
  let recorder, failure;
  class Recorder { constructor() { recorder = this; this.state = 'inactive'; } start() { this.state = 'recording'; } stop() { this.state = 'inactive'; this.onstop(); } }
  const capture = await microphoneRecording({ signal: new AbortController().signal, context: microphoneContext(),
    devices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) }, Recorder, onError: (error) => { failure = error; } });
  recorder.stop(); assert.match(failure.message, /Microphone disconnected/);
  await assert.rejects(capture.finish(), /Microphone disconnected/);
});

function microphoneContext(level = () => 0) {
  return { createMediaStreamSource: () => ({ connect() {}, disconnect() {} }),
    createAnalyser: () => ({ getFloatTimeDomainData(samples) { samples.fill(level()); }, disconnect() {} }) };
}

test('microphone detects a speech pause once, ignores silence, bounds long speech and cancels monitoring', async () => {
  for (const scenario of ['speech', 'silence', 'continuous', 'cancel']) {
    let time = 0, level = 0, poll, stopped = 0, cleared = 0;
    const boundaries = [], signal = new AbortController();
    class Recorder { constructor() { this.state = 'inactive'; } start() { this.state = 'recording'; } stop() { this.state = 'inactive'; this.onstop(); } }
    const recording = await microphoneRecording({ signal: signal.signal, context: microphoneContext(() => level), Recorder,
      devices: { getUserMedia: async () => ({ getTracks: () => [{ stop() { stopped++; } }] }) },
      now: () => time, schedule: (callback) => { poll = callback; return 1; }, unschedule: () => { cleared++; }, onSpeechEnd: (speech) => boundaries.push(speech) });
    const advance = (until) => { while (time < until) { time += 50; poll(); } };
    if (scenario === 'speech') {
      advance(500); assert.deepEqual(boundaries, []);
      level = .06; advance(800); level = 0; advance(1650); assert.deepEqual(boundaries, []);
      advance(1700); assert.deepEqual(boundaries, [true]); advance(30000); assert.equal(boundaries.length, 1);
    } else if (scenario === 'continuous') {
      level = .06; advance(29950); assert.deepEqual(boundaries, []); advance(30000); assert.deepEqual(boundaries, [true]);
    } else if (scenario === 'silence') {
      advance(30000); assert.deepEqual(boundaries, [false]);
    } else {
      signal.abort(); level = .06; advance(30000); assert.deepEqual(boundaries, []);
    }
    recording.cancel(); assert.ok(stopped > 0); assert.ok(cleared > 0);
  }
});

class Element extends EventTarget {
  constructor() { super(); this.nodes = new Map(); this.children = []; this.dataset = {}; this.hidden = false; }
  querySelector(selector) { if (!this.nodes.has(selector)) this.nodes.set(selector, new Element()); return this.nodes.get(selector); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  remove() {}
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const reply = { transcript: 'Zoom in a little.', answer: 'I will zoom in.', audio_base64: 'UklGRg==', mime: 'audio/wav', actions: [{ name: 'adjust_camera_zoom', arguments: { direction: 'in', amount: 'little' } }] };

function voiceHarness({ request, recordMicrophone, playback, audioContext, runner, askAgent } = {}) {
  const previous = globalThis.document; globalThis.document = { createElement: () => new Element() };
  const ui = { root: new Element(), helpDetail: new Element() }, events = [], calls = [], audio = [], recordings = [];
  const lifetime = new AbortController();
  const session = createVoiceSession({ signal: lifetime.signal, runner: async (name, args, options) => { calls.push({ name, args, options }); return runner ? runner(name, args, options) : { ok: true }; }, createAdapter: (hooks) => createOciVoiceSession({ ...hooks, ui, askAgent,
    request: request || (async () => reply), recordMicrophone: async (options) => { recordings.push(options); return recordMicrophone ? recordMicrophone(options) : { finish: async () => encodeVoiceWav(new Float32Array([.1, -.1])), cancel() {} }; },
    createAudio: () => { const player = { context: audioContext, unlock: async () => {}, play: playback || (async () => { player.played = true; }), close() { player.closed = true; } }; audio.push(player); return player; },
  }) });
  session.subscribe((event) => events.push(event));
  return { session, ui, events, calls, audio, recordings, finish: (hasSpeech = true) => recordings.at(-1).onSpeechEnd(hasSpeech),
    cleanup() { lifetime.abort(); if (previous === undefined) delete globalThis.document; else globalThis.document = previous; } };
}

test('a microphone turn automatically executes allowed native actions, plays audio and carries only its own bounded conversation', async () => {
  const requests = [];
  const harness = voiceHarness({ request: async (path, options) => { requests.push({ path, ...JSON.parse(options.body) }); return reply; } });
  try {
    await harness.session.start(); harness.finish(); await tick();
    assert.equal(requests[0].path, '/api/prisma/oci-voice/turn'); assert.equal(requests[0].mime, 'audio/wav'); assert.equal('context' in requests[0], false);
    assert.deepEqual(requests[0].history, []); assert.equal(harness.calls.length, 1); assert.equal(harness.audio[0].played, true);
    assert.equal(harness.events.find((event) => event.type === 'completion').source, 'microphone');
    assert.equal(harness.ui.root.dataset.microphone, 'active'); assert.equal(harness.session.state, 'listening');
    assert.equal(harness.recordings.length, 2); assert.equal(harness.audio.length, 1); assert.equal(harness.ui.root.children.length, 0);
    harness.finish(); await tick();
    assert.equal(requests[1].history.length, 2);
    harness.session.stop(); await harness.session.start(); harness.finish(); await tick();
    assert.deepEqual(requests[2].history, []);
  } finally { harness.cleanup(); }
});

test('a compound sensor command runs layer and camera actions before the real Agent Flow callback', async () => {
  const order = [], actions = [
    { name: 'set_layer_visibility', arguments: { layerId: 'sensors', enabled: true } },
    { name: 'set_layer_visibility', arguments: { layerId: 'territorial-events', enabled: true } },
    { name: 'adjust_camera_zoom', arguments: { direction: 'in', amount: 'little' } },
    { name: 'ask_aidp', arguments: { question: 'Compare the sensor readings with social evidence.' } },
  ];
  const harness = voiceHarness({ request: async () => ({ ...reply, actions }),
    runner: async (name, args) => { order.push(args.layerId || name); return { ok: true }; },
    askAgent: async (question, { signal }) => { assert.equal(signal.aborted, false); order.push(question); return { answer: 'Synthetic readings require human review.' }; },
    playback: async () => order.push('audio') });
  try {
    await harness.session.start(); harness.finish(); await tick();
    assert.deepEqual(order, ['sensors', 'territorial-events', 'adjust_camera_zoom', actions[3].arguments.question, 'audio']);
    assert.equal(harness.calls.length, 3); assert.equal(harness.session.state, 'listening');
  } finally { harness.cleanup(); }
  for (const args of [{ question: ' ' }, { question: 'x'.repeat(2001) }, { question: 'Query', validate: true }])
    assert.deepEqual(voiceActions([{ name: 'ask_aidp', arguments: args }]), []);
});

test('OFF cancels an Agent Flow question and its late reply cannot speak or resume recording', async () => {
  let complete, questionSignal;
  const harness = voiceHarness({ request: async () => ({ ...reply, actions: [{ name: 'ask_aidp', arguments: { question: 'What is the selected sensor status?' } }] }),
    askAgent: (_question, { signal }) => { questionSignal = signal; return new Promise((resolve) => { complete = resolve; }); } });
  try {
    await harness.session.start(); harness.finish(); await tick(); harness.session.stop();
    assert.equal(questionSignal.aborted, true);
    complete({ answer: 'Late sensor response' }); await tick();
    assert.equal(harness.session.state, 'idle'); assert.equal(harness.audio[0].played, undefined); assert.equal(harness.recordings.length, 1);
  } finally { harness.cleanup(); }
});

test('OCI voice toggles the registered Social networks layer through the native runner and stops on an unavailable layer', async () => {
  let enabled = false, nextEnabled = true, played = 0;
  const changes = [], layers = new Map([['territorial-events', { name: 'Social networks' }]]);
  const dataManager = { layers, isEnabled: () => enabled, getAll: () => [{ id: 'territorial-events', name: 'Social networks' }],
    setEnabled: async (id, value, options) => { changes.push({ id, value, options }); enabled = value; return true; } };
  const viewer = { camera: { moveEnd: { addEventListener() {} } }, clock: { onTick: { addEventListener() { return () => {}; } } }, scene: { canvas: new EventTarget() } };
  const harness = voiceHarness({ runner: createGevActionRunner({ viewer, dataManager }), playback: async () => { played++; },
    request: async () => ({ ...reply, transcript: nextEnabled ? 'Habilitar capa de datos de redes sociales' : 'Deshabilita redes sociales',
      actions: [{ name: 'set_layer_visibility', arguments: { layerId: 'territorial-events', enabled: nextEnabled } }] }) });
  try {
    await harness.session.start();
    for (const value of [true, false]) {
      nextEnabled = value; harness.finish(); await tick();
      assert.equal(enabled, value); assert.equal(harness.session.state, 'listening');
      assert.equal(harness.calls.at(-1).name, 'set_layer_visibility');
      assert.equal(changes.at(-1).id, 'territorial-events'); assert.equal(changes.at(-1).value, value);
      assert.equal(changes.at(-1).options.origin, 'voice'); assert.ok(changes.at(-1).options.signal instanceof AbortSignal);
    }
    assert.equal(played, 2);
    layers.clear(); nextEnabled = true; harness.finish(); await tick();
    assert.equal(harness.session.state, 'error'); assert.equal(changes.length, 2); assert.equal(played, 2);
    assert.equal(harness.ui.root.dataset.microphone, 'muted'); assert.equal(harness.audio[0].closed, true);
  } finally { harness.cleanup(); }
});

test('OFF during inference aborts the real request and discards late actions, text and audio', async () => {
  let resolve, requestSignal;
  const harness = voiceHarness({ request: (_path, options) => { requestSignal = options.signal; return new Promise((done) => { resolve = done; }); } });
  try {
    await harness.session.start(); harness.finish(); await tick(); harness.session.stop();
    assert.equal(requestSignal.aborted, true); resolve({ ...reply, actions: [{ name: 'set_layer_visibility', arguments: { layerId: 'territorial-events', enabled: true } }] }); await tick();
    assert.equal(harness.session.state, 'idle'); assert.equal(harness.calls.length, 0); assert.equal(harness.audio[0].played, undefined); assert.equal(harness.audio[0].closed, true);
    assert.equal(harness.events.some((event) => event.type === 'transcript'), false);
  } finally { harness.cleanup(); }
});

test('OFF closes playing audio and a late completion cannot re-open the native voice session', async () => {
  let finishAudio;
  const harness = voiceHarness({ playback: () => new Promise((resolve) => { finishAudio = resolve; }) });
  try {
    await harness.session.start(); harness.finish(); await tick(); assert.equal(harness.session.state, 'speaking');
    harness.session.stop(); finishAudio(); await tick();
    assert.equal(harness.audio[0].closed, true); assert.equal(harness.session.state, 'idle'); assert.equal(harness.events.some((event) => event.type === 'completion'), false);
  } finally { harness.cleanup(); }
});

test('a late reply cannot interrupt a new microphone turn', async () => {
  let resolve;
  const harness = voiceHarness({ request: () => new Promise((done) => { resolve = done; }) });
  try {
    await harness.session.start(); harness.finish(); await tick();
    assert.equal(harness.session.state, 'thinking');
    harness.session.stop(); await harness.session.start();
    resolve(reply); await tick();
    assert.equal(harness.ui.root.dataset.microphone, 'active');
    assert.equal(harness.session.state, 'listening');
    assert.equal(harness.calls.length, 0);
  } finally { harness.cleanup(); }
});

test('silence starts another local listening window without invoking OCI', async () => {
  let requests = 0;
  const harness = voiceHarness({ request: async () => { requests++; return reply; } });
  try {
    await harness.session.start(); harness.finish(false); await tick();
    assert.equal(requests, 0); assert.equal(harness.recordings.length, 2); assert.equal(harness.audio.length, 1);
    harness.session.stop(); harness.finish(); await tick(); assert.equal(requests, 0); assert.equal(harness.recordings.length, 2);
  } finally { harness.cleanup(); }
});

test('rejected inference remains an error and never fabricates a voice response', async () => {
  const harness = voiceHarness({ request: async () => { throw Error('OCI rate limit reached.'); } });
  try {
    await harness.session.start(); harness.finish(); await tick();
    assert.equal(harness.session.state, 'error'); assert.equal(harness.audio[0].closed, true); assert.equal(harness.audio[0].played, undefined);
    assert.equal(harness.calls.length, 0); assert.equal(harness.recordings.length, 1); assert.equal(harness.ui.root.dataset.microphone, 'muted');
  } finally { harness.cleanup(); }
});
