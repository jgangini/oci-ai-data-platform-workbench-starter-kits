import test from 'node:test';
import assert from 'node:assert/strict';
import { createTerritorialLayer, seedBogotaView, TERRITORIAL_LAYER_ID } from '../native/territorialLayer.js';
import { ShareLinkManager } from '../.upstream/src/sharelink.js';
import { chatContext, ociPayload } from '../native/analyst.js';
import { browserProviderConfig, managedProviderLabel, selectableModels } from '../native/providerSettings.js';

const publication = (version = 'v1') => ({ version, published_at: '2026-10-03T00:00:00Z', incidents: [
  { id: 'flood', category: 'inundacion', locality: 'Kennedy', severity: 'high', lat: 4.62, lon: -74.14, created_at: '2026-10-03T00:00:00Z', evidence_ids: ['x1'] },
  { id: 'rain', category: 'lluvia', locality: 'Usaquén', severity: 'low', lat: 4.7, lon: -74.02, created_at: '2026-10-03T00:00:00Z', evidence_ids: ['f1'] },
  { id: 'unknown', category: 'lluvia', locality: 'Sin localizar', severity: 'low', lat: null, lon: null, created_at: '2026-10-03T00:00:00Z', evidence_ids: [] },
], evidence: [{ id: 'x1', platform: 'x' }, { id: 'f1', platform: 'facebook' }] });

test('fresh entry seeds a native Bogotá share view and preserves an existing user view unchanged', () => {
  let changed;
  const navigationState = { existing: true };
  const history = { state: navigationState, replaceState(state, title, href) { changed = { state, title, href }; } };
  assert.equal(seedBogotaView(new URL('https://example.test/gods-eye-view/?setup=1'), history), true);
  assert.equal(changed.state, navigationState); assert.equal(new URL(changed.href).search, '?setup=1');
  const previousWindow = globalThis.window;
  const manager = new ShareLinkManager({ camera: { changed: { addEventListener: () => () => {} } } });
  try {
    globalThis.window = { location: new URL(changed.href) };
    const parsed = manager.parseInitialHash();
    assert.deepEqual([parsed.lat, parsed.lon, parsed.alt, parsed.heading, parsed.pitch, parsed.roll], [4.66, -74.085, 38000, 0, -90, 0]);
    assert.equal(parsed.layerState, null); // Camera-only restore does not prescribe private layer state.
  } finally { manager.destroy(); if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow; }
  for (const hash of ['#lat=30.267&lon=-97.74&alt=1200&map=osm', '#style=thermal']) {
    changed = undefined;
    assert.equal(seedBogotaView(new URL(`https://example.test/gods-eye-view/${hash}`), history), false);
    assert.equal(changed, undefined);
  }
});

function harness(request) {
  let destroyed = false, click;
  const Cesium = {
    CustomDataSource: class { constructor() { this.entities = { values: [], removeAll() { this.values = []; }, add(value) { this.values.push(value); } }; } },
    ScreenSpaceEventHandler: class { setInputAction(handler) { click = handler; } destroy() { destroyed = true; } },
    ScreenSpaceEventType: { LEFT_CLICK: 1 }, Cartesian3: { fromDegrees: (...args) => args },
    Color: { WHITE: 'white', fromCssColorString: (value) => value }, HeightReference: { CLAMP_TO_GROUND: 1 }, Math: { toDegrees: (value) => value },
  };
  const viewer = { sources: [], removed: [], scene: { canvas: {}, globe: { ellipsoid: {} }, requestRender() {}, pick: () => ({ id: { id: 'territorial:flood' } }) },
    camera: { flyTo(value) { this.lastFlight = value; }, computeViewRectangle: () => ({ west: -74.2, south: 4.5, east: -74.1, north: 4.65 }) },
    entities: { removeAll() { assert.fail('Must not remove native layer entities'); } },
  };
  viewer.dataSources = { add(value) { viewer.sources.push(value); }, remove(value, destroy) { viewer.removed.push([value, destroy]); } };
  const layer = createTerritorialLayer({ Cesium, request }); layer.init(viewer); layer.enable();
  return { layer, viewer, click: () => click({ position: {} }), destroyed: () => destroyed };
}

test('native layer owns one data source, keeps unresolved events and filters the same publication', async () => {
  const { layer, viewer, click, destroyed } = harness(async () => publication());
  assert.equal(layer.id, TERRITORIAL_LAYER_ID); assert.equal(await layer.update(), true);
  assert.equal(viewer.sources.length, 1); assert.equal(viewer.sources[0].entities.values.length, 2);
  assert.equal(layer.state().items.length, 3);
  layer.setFilters({ platform: 'x', bbox: layer.visibleArea() });
  assert.deepEqual(layer.state().items.map((item) => item.id), ['flood']);
  assert.equal(viewer.sources[0].entities.values.length, 1);
  click(); assert.equal(layer.state().selectedId, 'flood');
  assert.deepEqual(chatContext(layer.state()), { version: 'v1', incident_id: 'flood', filters: { platform: 'x', bbox: '-74.200000,4.500000,-74.100000,4.650000' } });
  layer.setFilters({ locality: 'Usaquén' }); assert.equal(layer.state().selectedId, undefined);
  assert.equal(layer.select('flood'), false);
  layer.disable(); assert.equal(viewer.sources[0].show, false);
  layer.destroy(); assert.equal(destroyed(), true); assert.deepEqual(viewer.removed, [[viewer.sources[0], true]]);
});

test('failed and malformed refreshes retain the previous publication with an explicit error', async () => {
  let response = publication(); const { layer } = harness(async () => response);
  await layer.update(); response = { version: 'bad' };
  assert.equal(await layer.update(), false); assert.equal(layer.state().snapshot.version, 'v1'); assert.match(layer.getStats().lastError, /invalid/);
  assert.throws(() => layer.setFilters({ bbox: '180,0,-180,1' }), /out of range/);
  assert.throws(() => layer.setFilters({ date_from: '2026-10-04T00:00:00Z', date_to: '2026-10-03T00:00:00Z' }), /period/);
});

test('a superseded or disabled refresh cannot overwrite current native layer data', async () => {
  const resolvers = []; const { layer } = harness(() => new Promise((resolve) => resolvers.push(resolve)));
  const first = layer.update(); const second = layer.update(); resolvers[1](publication('v2')); await second;
  resolvers[0](publication('v1')); await first; assert.equal(layer.state().snapshot.version, 'v2');
  const third = layer.update(); layer.disable(); resolvers[2](publication('v3')); await third;
  assert.equal(layer.state().snapshot.version, 'v2');
});

test('native lifecycle cancellation aborts its request and ignores an uncancellable late result', async () => {
  let resolve, requestSignal;
  const { layer } = harness(async (_path, options) => { requestSignal = options.signal; return new Promise((done) => { resolve = done; }); });
  const operation = new AbortController(); const update = layer.update(undefined, { signal: operation.signal });
  operation.abort(); assert.equal(requestSignal.aborted, true);
  resolve(publication()); assert.equal(await update, false);
  assert.equal(layer.state().snapshot.version, ''); assert.equal(layer.getStats().lastError, null);
});

test('AIDP requires a publication; OCI uses separate bounded history and no automatic incident evidence', () => {
  assert.throws(() => chatContext({ snapshot: { version: '' } }), /published/);
  const history = Array.from({ length: 12 }, (_, index) => ({ role: index % 2 ? 'assistant' : 'user', content: 'a'.repeat(2001), secret: 'never copy' }));
  const result = ociPayload('Explain rain', history, { snapshot: publication(), filters: { locality: 'Kennedy', bbox: '1,2,3,4' }, selectedId: 'flood' });
  assert.equal(result.history.length, 10); assert.equal(result.history[0].content.length, 2000); assert.equal('secret' in result.history[0], false);
  assert.deepEqual(result.context, { version: 'v1', locality: 'Kennedy', incident_id: 'flood' });
  assert.equal('evidence' in result, false); assert.equal('session_id' in result, false);
});

test('OCI selection includes only explicitly selectable server catalog entries', () => {
  assert.deepEqual(selectableModels({ items: [{ id: 'active', selectable: true }, { id: 'inactive', selectable: false }, { id: 'unknown' }, { id: 42, selectable: true }] }).map((item) => item.id), ['active']);
  assert.deepEqual(selectableModels(null), []);
  assert.equal(managedProviderLabel(false), 'Not configured · server managed');
  assert.equal(managedProviderLabel(true), 'Configured · server managed');
});

test('bootstrap accepts only the two explicit browser provider keys and never guesses configuration', () => {
  assert.deepEqual(browserProviderConfig({ googleApiKey: 'restricted-google', cesiumToken: 'scoped-cesium' }), { googleApiKey: 'restricted-google', cesiumToken: 'scoped-cesium' });
  assert.deepEqual(browserProviderConfig({ googleApiKey: '', cesiumToken: '' }), { googleApiKey: undefined, cesiumToken: undefined });
  for (const value of [null, [], {}, { googleApiKey: 'present' }, { googleApiKey: null, cesiumToken: '' }, { googleApiKey: '', cesiumToken: '', private_key: 'must-not-be-a-client-field' }]) {
    assert.throws(() => browserProviderConfig(value), /configuration/);
  }
});
