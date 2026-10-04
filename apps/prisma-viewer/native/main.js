import * as Cesium from 'cesium';
import { createStandaloneApplication } from '../.upstream/src/standalone/application.js';
import { createLayerCatalog } from '../.upstream/src/app/catalog.js';
import { describeError } from '../.upstream/src/standalone/errors.js';
import { createTerritorialLayer, mountTerritorialPanel, seedBogotaView } from './territorialLayer.js';
import { createSensorsLayer, mountSensorsPanel } from './sensorsLayer.js';
import { createAgentFlowLayer, mountAnalyst } from './analyst.js';
import { browserProviderConfig, mountProviderSettings } from './providerSettings.js';
import { createOciVoiceSession, selectedVoiceProvider } from './ociVoice.js';
import './styles.css';

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...options.headers },
    signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(110000)]) : AbortSignal.timeout(15000),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : data.detail?.message;
    const error = new Error(response.status === 401 ? 'Your session expired. Sign in again through your workspace.' : detail || `Service unavailable (${response.status}).`);
    error.status = response.status; error.code = data.detail?.code; error.retryAfter = response.headers.get('Retry-After'); throw error;
  }
  return data;
}

let voiceProvider;
let application, territorial, sensors, agentFlow, analyst, lifetime;
async function askAgent(question, settings = {}) {
  const manager = application.getComponents().data.dataManager;
  const enabled = await manager.setEnabled(agentFlow.id, true, { origin: 'voice', signal: settings.signal });
  settings.signal?.throwIfAborted();
  if (!enabled || !manager.isEnabled(agentFlow.id)) throw new DOMException('Agent Flow activation cancelled', 'AbortError');
  return analyst.ask(question, settings);
}
async function startApplication() {
  seedBogotaView(window.location, window.history);
  const [browserPayload, voiceStatus] = await Promise.all([request('/api/setup/browser'), request('/api/prisma/oci-voice').catch(() => null)]);
  const browserConfig = browserProviderConfig(browserPayload);
  voiceProvider = selectedVoiceProvider(undefined, voiceStatus?.configured === true);
  application = createStandaloneApplication({
    ...browserConfig,
    ...(voiceProvider === 'oci' ? { voice: { createSession: (options) => createOciVoiceSession({ ...options, request, askAgent }) } } : {}),
    allowQaRegistration: import.meta.env.DEV,
    extendCatalog(catalog, { signal }) {
      lifetime = signal;
      territorial = createTerritorialLayer({ Cesium, request, signal });
      sensors = createSensorsLayer({ Cesium, request, signal });
      agentFlow = createAgentFlowLayer();
      const extension = createLayerCatalog([...catalog.layers, territorial, sensors, agentFlow], [...catalog.metadata, { id: territorial.id, disposition: 'local-only' }, { id: sensors.id, disposition: 'local-only' }, { id: agentFlow.id, disposition: 'local-only' }]);
      return Object.freeze({ ...catalog, ...extension });
    },
  });
  return application.start();
}

startApplication().then(async ({ controls }) => {
  if (voiceProvider === 'openai') document.querySelector('#gev-voice-control .gev-voice-kicker').textContent = 'OPENAI VOICE';
  const panel = mountTerritorialPanel({ layer: territorial, request, signal: lifetime,
    setPanelCollapsed: (...args) => controls.styleManager.setPanelCollapsed(...args) });
  mountSensorsPanel({ layer: sensors, request, signal: lifetime, setPanelCollapsed: (...args) => controls.styleManager.setPanelCollapsed(...args) });
  analyst = mountAnalyst({ layer: territorial, agentFlow, request, showEvidence: panel.showEvidence, signal: lifetime,
    sensorContext: () => { const state = sensors.state(), selected = state.items.find(item => item.id === state.selectedId); return { snapshot: state.snapshot, enabled: state.enabled, ...(selected ? { sensor_id: selected.sensor_id } : {}) }; },
    refreshSensors: () => sensors.update(),
    showSensor: (eventId) => sensors.select(eventId),
    setPanelCollapsed: (...args) => controls.styleManager.setPanelCollapsed(...args) });
  void mountProviderSettings({ request, signal: lifetime });
}).catch((error) => {
  console.error("God's Eye View initialization failed:", error);
  const status = document.querySelector('#loading-screen .loader-status');
  if (status) { status.textContent = `Error: ${describeError(error)}`; status.style.color = '#ff4444'; }
});

window.addEventListener('pagehide', () => { void application?.destroy(); }, { once: true });
export { application };
