import * as Cesium from 'cesium';
import { createStandaloneApplication } from '../.upstream/src/standalone/application.js';
import { createLayerCatalog } from '../.upstream/src/app/catalog.js';
import { describeError } from '../.upstream/src/standalone/errors.js';
import { createTerritorialLayer, mountTerritorialPanel, seedBogotaView } from './territorialLayer.js';
import { mountAnalyst } from './analyst.js';
import { browserProviderConfig, mountProviderSettings } from './providerSettings.js';
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
    error.status = response.status; throw error;
  }
  return data;
}

let application, territorial, lifetime;
async function startApplication() {
  seedBogotaView(window.location, window.history);
  const browserConfig = browserProviderConfig(await request('/api/setup/browser'));
  application = createStandaloneApplication({
    ...browserConfig,
    allowQaRegistration: import.meta.env.DEV,
    extendCatalog(catalog, { signal }) {
      lifetime = signal;
      territorial = createTerritorialLayer({ Cesium, request, signal });
      const extension = createLayerCatalog([...catalog.layers, territorial], [...catalog.metadata, { id: territorial.id, disposition: 'local-only' }]);
      return Object.freeze({ ...catalog, ...extension });
    },
  });
  return application.start();
}

startApplication().then(async ({ data }) => {
  const panel = mountTerritorialPanel({ layer: territorial, request, signal: lifetime });
  mountAnalyst({ layer: territorial, request, showEvidence: panel.showEvidence, signal: lifetime });
  void mountProviderSettings({ request, signal: lifetime });
  await data.dataManager.setEnabled(territorial.id, true, { origin: 'user' });
}).catch((error) => {
  console.error("God's Eye View initialization failed:", error);
  const status = document.querySelector('#loading-screen .loader-status');
  if (status) { status.textContent = `Error: ${describeError(error)}`; status.style.color = '#ff4444'; }
});

window.addEventListener('pagehide', () => { void application?.destroy(); }, { once: true });
export { application };
