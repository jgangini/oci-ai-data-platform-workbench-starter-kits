import { fileURLToPath } from 'node:url';
import { access, readFile, rename } from 'node:fs/promises';

// Provider caches use process.cwd(); keep that path identical in build, preview and rollback.
// Set it before importing providers, which capture their cache paths at module load.
process.chdir(fileURLToPath(new URL('../.upstream/', import.meta.url)));
const { build, preview } = await import('../.upstream/node_modules/vite/dist/node/index.js');
const { default: nativeConfig } = await import('./vite.config.mjs');
const building = process.argv.includes('--build');
if (building) {
  await build(nativeConfig({ command: 'build' }));
  // The upstream plugin copies base into its output path; the bridge strips base once.
  await rename('dist/gods-eye-view/cesium', 'dist/cesium');
}
const html = await readFile('dist/index.html', 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*\bsrc="\/gods-eye-view\/([^"]+)"/g)].map((match) => match[1]);
if (!scripts.some((path) => path.startsWith('assets/'))) throw new Error('Native entry bundle is missing');
await Promise.all([...scripts, 'cesium/Cesium.js', 'cesium/Widgets/widgets.css'].map((path) => access(`dist/${path}`)));
if (!building) {
  const server = await preview(nativeConfig());
  const close = () => server.httpServer.close(() => process.exit(0));
  process.once('SIGTERM', close);
  process.once('SIGINT', close);
}
