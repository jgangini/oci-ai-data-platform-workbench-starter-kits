export const VOICE_SECONDS = 30;
export const VOICE_SAMPLE_RATE = 16000;

/** PCM16 mono WAV, with an explicit duration bound before allocating the payload. */
export function encodeVoiceWav(samples, sampleRate = VOICE_SAMPLE_RATE) {
  if (!samples.length || samples.length > sampleRate * VOICE_SECONDS) throw new Error('Record between a moment and 30 seconds of audio.');
  const bytes = new Uint8Array(44 + samples.length * 2), view = new DataView(bytes.buffer);
  const ascii = (offset, value) => [...value].forEach((letter, index) => view.setUint8(offset + index, letter.charCodeAt(0)));
  ascii(0, 'RIFF'); view.setUint32(4, bytes.length - 8, true); ascii(8, 'WAVE'); ascii(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  ascii(36, 'data'); view.setUint32(40, samples.length * 2, true);
  for (let index = 0; index < samples.length; index++) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(44 + index * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
  }
  return bytes;
}

export function voiceBase64(bytes) {
  let binary = '';
  for (let index = 0; index < bytes.length; index += 8192) binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
  return btoa(binary);
}

export async function normalizedVoiceWav(buffer, context, { trim = false } = {}) {
  const decoded = await context.decodeAudioData(buffer);
  if (!(decoded.duration > 0) || (!trim && decoded.duration > VOICE_SECONDS)) throw new Error('Audio must contain at most 30 seconds.');
  const samples = new Float32Array(Math.floor(Math.min(decoded.duration, VOICE_SECONDS) * VOICE_SAMPLE_RATE));
  // Web Audio decodes/resamples to the context rate; handle a device that chooses another rate.
  for (let channel = 0; channel < decoded.numberOfChannels; channel++) {
    const values = decoded.getChannelData(channel);
    for (let index = 0; index < samples.length; index++) samples[index] += values[Math.min(values.length - 1, Math.floor(index * decoded.sampleRate / VOICE_SAMPLE_RATE))] / decoded.numberOfChannels;
  }
  return encodeVoiceWav(samples);
}

function watchSpeech(analyser, signal, onSpeechEnd, now, schedule, unschedule) {
  const samples = new Float32Array(analyser.fftSize), started = now();
  let previous = started, lastSpeech = started, speechMs = 0, ended = false;
  // ponytail: amplitude-based turn detection; noisy rooms need server VAD when OCI realtime is validated.
  const timer = schedule(() => {
    if (ended || signal.aborted) return;
    const time = now(); analyser.getFloatTimeDomainData(samples);
    const rms = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
    if (rms >= .015) { speechMs += Math.min(time - previous, 100); lastSpeech = time; }
    previous = time;
    if ((speechMs >= 250 && time - lastSpeech >= 900) || time - started >= VOICE_SECONDS * 1000) {
      ended = true; unschedule(timer); onSpeechEnd(speechMs >= 250);
    }
  }, 50);
  return () => { ended = true; unschedule(timer); };
}

export async function microphoneRecording({ signal, context, onSpeechEnd = () => {}, onError = () => {}, devices = globalThis.navigator?.mediaDevices, Recorder = globalThis.MediaRecorder,
  now = () => performance.now(), schedule = setInterval, unschedule = clearInterval }) {
  if (!devices?.getUserMedia || !Recorder) throw new Error('This browser does not support microphone recording.');
  const stream = await devices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  const stopTracks = () => stream.getTracks().forEach((track) => track.stop());
  if (signal.aborted) { stopTracks(); signal.throwIfAborted(); }
  let recorder, input, analyser, stopMonitoring;
  const disconnect = () => { stopMonitoring?.(); input?.disconnect(); analyser?.disconnect(); };
  try {
    recorder = new Recorder(stream);
    input = context.createMediaStreamSource(stream); analyser = context.createAnalyser(); analyser.fftSize = 1024; input.connect(analyser);
  } catch (error) { disconnect(); stopTracks(); throw error; }
  const chunks = [];
  let resolve, reject, stopping = false;
  const complete = new Promise((done, fail) => { resolve = done; reject = fail; });
  complete.catch(() => {}); // Cancellation may happen before the user requests finish().
  const cancel = () => { stopping = true; disconnect(); signal.removeEventListener('abort', cancel); recorder.ondataavailable = null; if (recorder.state !== 'inactive') recorder.stop(); stopTracks(); reject(new DOMException('Recording cancelled', 'AbortError')); };
  signal.addEventListener('abort', cancel, { once: true });
  recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
  recorder.onerror = () => { const error = new Error('The microphone recording failed.'); reject(error); cancel(); onError(error); };
  recorder.onstop = () => {
    disconnect(); stopTracks(); signal.removeEventListener('abort', cancel);
    if (!stopping) { const error = new Error('Microphone disconnected. Turn voice on to reconnect.'); reject(error); onError(error); }
    else resolve(new Blob(chunks, { type: recorder.mimeType }));
  };
  try { recorder.start(); } catch (error) { cancel(); throw error; }
  stopMonitoring = watchSpeech(analyser, signal, onSpeechEnd, now, schedule, unschedule);
  return {
    cancel,
    async finish() {
      stopping = true;
      if (recorder.state !== 'inactive') recorder.stop();
      const blob = await complete; signal.throwIfAborted();
      // Encoder padding and background timer jitter must never send more than 30 seconds.
      const wav = await normalizedVoiceWav(await blob.arrayBuffer(), context, { trim: true }); signal.throwIfAborted(); return wav;
    },
  };
}

/** One context is unlocked by the user's gesture, then reused for decoding and playback. */
export function voiceAudioContext(Context = globalThis.AudioContext || globalThis.webkitAudioContext) {
  if (!Context) throw new Error('Web Audio is unavailable in this browser.');
  const context = new Context({ sampleRate: VOICE_SAMPLE_RATE });
  let source;
  return {
    context,
    unlock: () => context.resume(),
    async play(base64, mime, signal) {
      if (mime !== 'audio/wav' || typeof base64 !== 'string' || base64.length > 6000000) throw new Error('OCI returned unsupported audio.');
      const bytes = Uint8Array.from(atob(base64), (character) => character.charCodeAt(0));
      const buffer = await context.decodeAudioData(bytes.buffer); signal.throwIfAborted();
      source = context.createBufferSource(); source.buffer = buffer; source.connect(context.destination);
      const stopped = () => { try { source?.stop(); } catch { /* Already ended. */ } };
      await new Promise((resolve) => { source.onended = resolve; signal.addEventListener('abort', stopped, { once: true }); source.start(); });
      signal.removeEventListener('abort', stopped); signal.throwIfAborted();
    },
    close() { try { source?.stop(); } catch { /* Already ended. */ } void context.close().catch(() => {}); },
  };
}
