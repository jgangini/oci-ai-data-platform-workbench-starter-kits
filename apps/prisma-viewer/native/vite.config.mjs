import { fileURLToPath } from 'node:url';
import { createBrowserViteConfig } from '../.upstream/build/vite.js';
import { localProviderPlugins } from '../.upstream/server/providers/local.js';
import { standaloneVoiceTools } from '../.upstream/server/standalone/voiceTools.js';
import { apiNotFoundPlugin } from '../.upstream/server/standalone/api-not-found.js';
import { keySetupStatus } from '../.upstream/src/keySetupCore.mjs';

export const upstreamRoot = fileURLToPath(new URL('../.upstream/', import.meta.url));

export function browserConfiguration(environment = process.env) {
  return { googleApiKey: environment.GOOGLE_MAPS_API_KEY || '', cesiumToken: environment.CESIUM_ION_TOKEN || '' };
}

/** Keep the original providers and browser headers behind the authenticated bridge. */
export default function nativeConfig({ command = 'serve' } = {}) {
  const health = {
    name: 'native-runtime-health',
    configurePreviewServer({ middlewares }) {
      middlewares.use('/__native_health', (_req, res) => {
        res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        res.end(JSON.stringify({ status: 'ok', runtime: 'gods-eye-view' }));
      });
      middlewares.use('/__native_setup', (_req, res) => {
        res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(keySetupStatus(process.env)));
      });
      middlewares.use('/__native_browser', (_req, res) => {
        res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(browserConfiguration()));
      });
    },
  };
  const config = createBrowserViteConfig({
    command,
    plugins: [health, ...localProviderPlugins({ realtime: { tools: standaloneVoiceTools() } })
      .filter((plugin) => plugin.name !== 'gev-key-setup'), apiNotFoundPlugin()],
    host: '127.0.0.1',
    port: 4173,
  });
  return {
    ...config,
    configFile: false,
    root: upstreamRoot,
    resolve: { ...config.resolve, dedupe: ['cesium'] },
    // The external bridge removes this prefix; the preview serves its built files at /.
    base: command === 'build' ? '/gods-eye-view/' : '/',
    preview: { ...config.preview, host: '127.0.0.1', port: 4173, strictPort: true,
      allowedHosts: ['127.0.0.1', 'localhost'] },
  };
}
