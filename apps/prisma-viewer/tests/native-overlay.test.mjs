import test from 'node:test';
import assert from 'node:assert/strict';
import CesiumEvent from '../.upstream/node_modules/@cesium/engine/Source/Core/Event.js';
import TextureAtlas from '../.upstream/node_modules/@cesium/engine/Source/Renderer/TextureAtlas.js';
import Cartesian3 from '../.upstream/node_modules/@cesium/engine/Source/Core/Cartesian3.js';
import { claimPointer, releasePointer, pointerOwner } from '../.upstream/src/data/inputOwnership.js';
import { createTerritorialLayer, mountTerritorialPanel, seedBogotaView, TERRITORIAL_LAYER_ID } from '../native/territorialLayer.js';
import { ShareLinkManager } from '../.upstream/src/sharelink.js';
import { PanelChrome } from '../.upstream/src/ui/panelChrome.js';
import { layoutRightPanelRail } from '../.upstream/src/ui/rightPanelRail.js';
import { chatContext, createAgentFlowLayer, mountAnalyst } from '../native/analyst.js';
import { LayerLifecycle } from '../.upstream/src/data/lifecycle.js';
import { browserProviderConfig, managedProviderLabel, mountProviderSettings, ociModelLabel, ociProviderPresentation } from '../native/providerSettings.js';

test.beforeEach((t) => {
  t.mock.timers.enable({ apis: ['Date'], now: new Date('2026-10-03T01:00:00Z').getTime() });
  const previous = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class {
    constructor(callback) { this.callback = callback; }
    observe(target) { target.resizeObserver = this; }
    disconnect() { this.disconnected = true; }
  };
  t.after(() => { if (previous === undefined) delete globalThis.ResizeObserver; else globalThis.ResizeObserver = previous; });
});

const publication = (version = 'v1') => ({ version, published_at: '2026-10-03T00:00:00Z', incidents: [
  { id: 'flood', category: 'inundacion', locality: 'Kennedy', severity: 'high', lat: 4.62, lon: -74.14, created_at: '2026-10-03T00:00:00Z', evidence_ids: ['x1'] },
  { id: 'rain', category: 'lluvia', locality: 'Usaquén', severity: 'low', lat: 4.7, lon: -74.02, created_at: '2026-10-03T00:00:00Z', evidence_ids: ['f1'] },
  { id: 'unknown', category: 'lluvia', locality: 'Sin localizar', severity: 'low', lat: null, lon: null, created_at: '2026-10-03T00:00:00Z', evidence_ids: [] },
], evidence: [{ id: 'x1', platform: 'x' }, { id: 'f1', platform: 'facebook' }] });

class PanelElement extends EventTarget {
  constructor(tag = 'div') {
    super(); this.tagName = tag.toUpperCase(); this.dataset = {}; this.style = { setProperty(name, value) { this[name] = value; }, getPropertyValue(name) { return this[name] || ''; }, removeProperty(name) { delete this[name]; } }; this.nodes = new Map(); this.children = []; this.classes = new Set();
    this.clientWidth = 1200; this.clientHeight = 800; this.offsetWidth = 360; this.offsetHeight = 300;
    this.classList = { contains: (name) => this.classes.has(name), add: (...names) => names.forEach(name => this.classes.add(name)), remove: (...names) => names.forEach(name => this.classes.delete(name)), toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name) };
    this.elements = { namedItem: (name) => this.querySelector(`[name="${name}"]`) };
    Object.defineProperty(this.elements, 'bbox', { get: () => this.elements.namedItem('bbox') });
  }
  set className(value) { this.classes = new Set(value.split(' ')); }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  matches(selector) { return selector === '[data-panel-id]' && Object.hasOwn(this.dataset, 'panelId'); }
  getBoundingClientRect() { return { left: 0, top: 0, right: this.clientWidth, bottom: this.clientHeight, width: this.clientWidth, height: this.clientHeight }; }
  get offsetWidth() { return Math.min(this.contentWidth, parseFloat(this.style.width) || Infinity); }
  set offsetWidth(value) { this.contentWidth = value; }
  get offsetHeight() { return Math.min(this.contentHeight, parseFloat(this.style.maxHeight) || Infinity); }
  set offsetHeight(value) { this.contentHeight = value; }
  querySelector(selector) {
    if (selector.startsWith('[data-dock-toggle-target=') || selector.startsWith('[data-collapse-target=')) selector = '.panel-collapse-btn';
    const found = this.querySelectorAll(selector)[0]; if (found) return found;
    if (!this.nodes.has(selector)) { const node = new PanelElement(); node.parentNode = this; this.nodes.set(selector, node); }
    return this.nodes.get(selector);
  }
  querySelectorAll(selector) {
    return [...this.children, ...this.nodes.values()].flatMap((node) => {
      const named = /^\[name="([^"]+)"\]$/.exec(selector);
      const matches = named ? node.name === named[1] : selector.startsWith('.') ? node.classList.contains(selector.slice(1)) : selector.startsWith('#') ? node.id === selector.slice(1) : node.tagName === selector.toUpperCase();
      return [...(matches ? [node] : []), ...node.querySelectorAll(selector)];
    });
  }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { for (const child of this.children) child.parentNode = null; this.children = []; this.append(...nodes); }
  after(node) { const index = this.parentNode.children.indexOf(this); this.parentNode.children.splice(index + 1, 0, node); node.parentNode = this.parentNode; }
  contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
  closest() { return this; }
  focus() { if (!this.disabled) { this.focused = true; if (globalThis.document) globalThis.document.activeElement = this; } }
  get isConnected() { return this === globalThis.document?.body || !!this.parentNode?.isConnected; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  scrollIntoView() { this.scrolled = true; }
  requestSubmit() { this.submitRequests = (this.submitRequests || 0) + 1; this.dispatchEvent(new Event('submit', { cancelable: true })); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this); this.parentNode = null; }
}

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

function harness(request, initialized = true) {
  let destroyed = false; const actions = new Map();
  const Cesium = {
    CustomDataSource: class { constructor() { this.entities = { values: [], removeAll() { this.values = []; }, add(value) { this.values.push(value); } }; } },
    ScreenSpaceEventHandler: class { setInputAction(handler, type) { actions.set(type, handler); } destroy() { destroyed = true; } },
    ScreenSpaceEventType: { LEFT_CLICK: 1, LEFT_DOWN: 2, MOUSE_MOVE: 3, LEFT_UP: 4 }, Cartesian3: { fromDegrees: (...args) => Object.defineProperties(args, { x: { value: args[0] }, y: { value: args[1] }, z: { value: args[2] || 0 } }) },
    Cartographic: { fromDegrees: (lon, lat) => ({ lon, lat }) }, SceneMode: { SCENE3D: 3 },
    EllipsoidalOccluder: class { isPointVisible() { return viewer.aboveHorizon !== false; } },
    Color: { WHITE: 'white', fromCssColorString: (value) => value }, HeightReference: { CLAMP_TO_GROUND: 1 }, Math: { toDegrees: (value) => value },
  };
  const viewer = { sources: [], removed: [], container: new PanelElement(), screen: { x: 600, y: 400 }, worldPick: Cartesian3.fromDegrees(-74.13, 4.63), scene: { mode: 3, screenSpaceCameraController: { enableInputs: true }, canvas: new PanelElement('canvas'), globe: { ellipsoid: {}, getHeight: () => 0 }, cartesianToCanvasCoordinates: () => viewer.screen, postRender: new CesiumEvent(), requestRender() { this.renderRequested = true; }, pick: () => ({ id: { id: 'territorial:flood' } }) },
    camera: { positionWC: {}, pickEllipsoid: () => viewer.worldPick, flyTo(value) { this.lastFlight = value; }, computeViewRectangle: () => ({ west: -74.2, south: 4.5, east: -74.1, north: 4.65 }) },
    entities: { removeAll() { assert.fail('Must not remove native layer entities'); } },
  };
  viewer.dataSources = { add(value) { viewer.sources.push(value); }, remove(value, destroy) { viewer.removed.push([value, destroy]); } };
  const layer = createTerritorialLayer({ Cesium, request });
  if (initialized) { layer.init(viewer); layer.enable(); }
  return { layer, viewer, click: () => actions.get(1)({ position: {} }), input: (type, event = {}) => actions.get(Cesium.ScreenSpaceEventType[type])(event), destroyed: () => destroyed };
}

test('native layer owns one data source, keeps unresolved events and filters the same publication', async () => {
  const { layer, viewer, click, destroyed } = harness(async () => publication());
  assert.equal(layer.id, TERRITORIAL_LAYER_ID); assert.equal(await layer.update(), true);
  assert.equal(layer.name, 'Social networks'); assert.equal(layer.source, 'OCI AIDP · Workflow'); assert.ok(layer.getStats().lastUpdate > 0);
  assert.equal(viewer.sources.length, 1); assert.equal(viewer.sources[0].entities.values.length, 2);
  assert.equal(layer.state().items.length, 3);
  layer.setFilters({ platform: 'x', bbox: layer.visibleArea() });
  assert.deepEqual(layer.state().items.map((item) => item.id), ['flood']);
  assert.equal(viewer.sources[0].entities.values.length, 1);
  click(); assert.equal(layer.state().selectedId, 'flood');
  assert.deepEqual(chatContext(layer.state()), { version: 'v1', incident_id: 'flood', filters: { platform: 'x', bbox: '-74.200000,4.500000,-74.100000,4.650000', date_from: '2026-10-02T01:00:00.000Z', date_to: '2026-10-03T01:00:00.000Z' } });
  layer.setFilters({ locality: 'Usaquén' }); assert.equal(layer.state().selectedId, undefined);
  assert.equal(layer.select('flood'), false);
  layer.disable(); assert.equal(viewer.sources[0].show, false);
  layer.destroy(); assert.equal(destroyed(), true); assert.deepEqual(viewer.removed, [[viewer.sources[0], true]]);
});

test('snapshot polling starts with a rolling day, fixes edited dates, and ignores an older period response', async (t) => {
  const requests = [], { layer } = harness((path, options) => new Promise((resolve) => requests.push({ path, signal: options.signal, resolve })));
  t.after(() => layer.destroy());
  const period = (request) => Object.fromEntries(new URL(request.path, 'https://example.test').searchParams);
  t.mock.timers.tick(123);
  const first = layer.update(); assert.deepEqual(period(requests[0]), { date_from: '2026-10-02T01:00:00.000Z', date_to: '2026-10-03T01:00:00.000Z' });
  requests[0].resolve(publication()); await first;
  layer.setFilters({ ...layer.state().filters, platform: 'x' });
  t.mock.timers.tick(60_000); const old = layer.update(); assert.equal(period(requests[1]).date_to, '2026-10-03T01:01:00.000Z');
  const fixed = { date_from: '2026-10-02T12:00:00Z', date_to: '2026-10-03T00:30:00Z' };
  layer.setFilters(fixed); assert.deepEqual(period(requests[2]), fixed); assert.equal(requests[1].signal.aborted, true); assert.equal(layer.state().isRefreshing, true);
  requests[2].resolve(publication('fixed')); await new Promise(setImmediate);
  requests[1].resolve(publication('old-period')); await old; assert.equal(layer.state().snapshot.version, 'fixed');
  t.mock.timers.tick(3600_000); const refresh = layer.update(); assert.deepEqual(period(requests[3]), fixed);
  requests[3].resolve(publication('fixed-next')); await refresh;
  layer.setFilters({}); assert.equal(period(requests[4]).date_to, '2026-10-03T02:01:00.000Z'); requests[4].resolve(publication()); await new Promise(setImmediate);
});

test('unvalidated marker dragging preserves unsaved coordinates and always restores camera and pointer ownership', async (t) => {
  const previousDocument = globalThis.document, document = new EventTarget(); globalThis.document = document;
  const data = publication(); data.can_review = true; data.incidents[0].review_status = 'pending';
  const requests = [], { layer, viewer, input } = harness(async (path) => { requests.push(path); return structuredClone(data); });
  t.after(() => { layer.destroy(); if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument; });
  await layer.update(); layer.select('flood', false);
  const camera = viewer.scene.screenSpaceCameraController;
  const other = claimPointer('another-tool'); input('LEFT_DOWN', { position: { x: 600, y: 400 } }); assert.equal(camera.enableInputs, true); releasePointer(other);
  input('LEFT_DOWN', { position: { x: 600, y: 400 } }); assert.equal(camera.enableInputs, false); assert.equal(pointerOwner(), TERRITORIAL_LAYER_ID);
  input('MOUSE_MOVE', { endPosition: { x: 610, y: 410 } });
  assert.deepEqual([layer.state().items[0].lat, layer.state().items[0].lon], [4.63, -74.13]);
  assert.equal(layer.state().snapshot.incidents[0].lat, 4.62, 'Moving the marker is only a draft');
  assert.equal(keydown(document, 'Escape').defaultPrevented, true); assert.equal(camera.enableInputs, true); assert.equal(pointerOwner(), null);
  assert.equal(layer.state().items[0].lat, 4.62);
  input('LEFT_DOWN', { position: {} }); input('MOUSE_MOVE', { endPosition: { x: 610, y: 410 } }); input('LEFT_UP');
  await layer.update(); assert.equal(layer.state().items[0].lat, 4.63); assert.equal(camera.enableInputs, true); assert.ok(requests.every((path) => path.startsWith('/api/prisma/snapshot?')));
  for (const [type, flags] of [['pointerup', { shiftKey: true }], ['pointerup', { ctrlKey: true }], ['pointerup', { altKey: true }], ['pointercancel', {}]]) {
    input('LEFT_DOWN', { position: {} }); document.dispatchEvent(Object.assign(new Event(type), flags));
    assert.equal(camera.enableInputs, true); assert.equal(pointerOwner(), null, `${type} must release even without Cesium's unmodified LEFT_UP`);
  }
  viewer.worldPick = null; input('LEFT_DOWN', { position: {} }); input('MOUSE_MOVE', { endPosition: { x: 20, y: 20 } }); assert.equal(layer.state().items[0].lat, 4.63);
  layer.disable(); assert.equal(camera.enableInputs, true); assert.equal(pointerOwner(), null); layer.enable();
  data.incidents[0].review_status = 'validated'; await layer.update(); assert.equal(layer.state().items[0].lat, 4.62);
  input('LEFT_DOWN', { position: {} }); assert.equal(camera.enableInputs, true); assert.throws(() => layer.setLocation('flood', 4.64, -74.12), /unvalidated/);
  data.incidents[0].review_status = 'pending'; await layer.update();
  camera.enableInputs = false; input('LEFT_DOWN', { position: {} }); input('LEFT_UP'); assert.equal(camera.enableInputs, false); camera.enableInputs = true;
  input('LEFT_DOWN', { position: {} }); layer.setFilters({ locality: 'Usaquén' }); assert.equal(camera.enableInputs, true); assert.equal(pointerOwner(), null);
  layer.setFilters({}); layer.select('flood', false); input('LEFT_DOWN', { position: {} }); layer.destroy(); assert.equal(camera.enableInputs, true); assert.equal(pointerOwner(), null);
});

test('selected ground markers schedule a settling frame for Cesium deferred textures without a camera move', async () => {
  const { layer, viewer } = harness(async () => publication());
  const atlas = new TextureAtlas(); let loading;
  try {
    await layer.update(); layer.select('flood', false);
    const { scene } = viewer;
    const selected = viewer.sources[0].entities.values.find((entity) => entity.id === 'territorial:flood');
    assert.equal(selected.point.pixelSize, 17); assert.equal(selected.point.heightReference, 1);
    assert.equal(viewer.camera.lastFlight, undefined);
    assert.equal(scene.renderRequested, true); assert.equal(scene.postRender.numberOfListeners, 1);
    scene.renderRequested = false;
    // The installed Cesium atlas queues even locally generated point images asynchronously.
    loading = atlas.addImage('selected-point', () => ({ width: selected.point.pixelSize, height: selected.point.pixelSize }));
    assert.equal(atlas._imagesToAddQueue.length, 0);
    scene.postRender.raiseEvent(scene);
    await Promise.resolve();
    assert.equal(atlas._imagesToAddQueue.length, 1);
    assert.equal(scene.renderRequested, true, 'The queued texture must get another frame without zoom input');
    assert.equal(scene.postRender.numberOfListeners, 0);
    scene.renderRequested = false; scene.postRender.raiseEvent(scene);
    assert.equal(scene.renderRequested, false, 'Settling must not turn into continuous rendering');
    layer.setFilters({ platform: 'x' }); assert.equal(scene.postRender.numberOfListeners, 1);
    layer.destroy(); scene.renderRequested = false; scene.postRender.raiseEvent(scene);
    assert.equal(scene.postRender.numberOfListeners, 0); assert.equal(scene.renderRequested, false);
  } finally { atlas.destroy(); await loading; }
});

test('social panel belongs below Context, follows its layer and reuses native disclosure without losing filters', async () => {
  const Element = PanelElement;
  const saved = { document: globalThis.document, MutationObserver: globalThis.MutationObserver, Option: globalThis.Option, FormData: globalThis.FormData };
  const rail = new Element(), context = new Element(), toggles = new Element(), group = new Element(), observers = [], changes = [];
  rail.append(context); toggles.append(group); group.textContent = 'Other layers'; group.className = 'data-layer-group-heading';
  let loadError;
  const data = publication();
  data.evidence.push(...['instagram', 'tiktok', 'sensor', 'linea123'].map(platform => ({ id: platform, platform })));
  const lifetime = new AbortController(); const { layer, viewer } = harness(async () => { if (loadError) throw loadError; return structuredClone(data); }, false);
  try {
    globalThis.document = { getElementById: (id) => ({ 'global-context-panel': context, 'data-toggles': toggles })[id], createElement: () => new Element() };
    globalThis.Option = class extends Element { constructor(label, value) { super(); this.textContent = label; this.value = value; } };
    globalThis.FormData = class extends Map { constructor(form) { super(['locality', 'platform', 'category', 'severity', 'date_from', 'date_to', 'bbox'].map(name => [name, form.elements.namedItem(name).value])); } };
    globalThis.MutationObserver = class { constructor(callback) { this.callback = callback; observers.push(this); } observe() {} disconnect() { this.disconnected = true; } };
    const { panel, popup } = mountTerritorialPanel({ layer, signal: lifetime.signal, request: async () => ({}), setPanelCollapsed(id, collapsed, options) {
      changes.push({ id, collapsed, options }); panel.classList.toggle('collapsed', collapsed);
      PanelChrome.prototype._syncPanelCollapseButton.call({}, panel);
    } });
    assert.deepEqual(rail.children, [context, panel]); assert.equal(panel.hidden, true);
    assert.equal(popup.parentNode, undefined, 'Native main mounts the panel before the layer has a viewer');
    layer.init(viewer); assert.equal(popup.parentNode, viewer.container);
    assert.match(panel.innerHTML, /class="panel-title">SOCIAL NETWORKS</); assert.doesNotMatch(panel.innerHTML, /data-count|<summary/);
    assert.match(panel.innerHTML, /data-dock-toggle-target="territorial-panel"[^>]*><span aria-hidden="true">▶<\/span>/);
    assert.match(panel.innerHTML, /class="tc-panel-body data-toggle-list"/); assert.match(panel.innerHTML, /class="tc-event-list scene-shot-list"/);
    assert.doesNotMatch(panel.innerHTML, /Clear filters|type="reset"/);
    const arrow = panel.querySelector('.panel-collapse-btn'); arrow.textContent = '▶';
    panel.querySelector('.panel-title, .location-toolbar-label').textContent = 'Social networks';
    assert.equal(group.textContent, 'Custom layers'); group.textContent = 'Other layers'; observers[0].callback(); assert.equal(group.textContent, 'Custom layers');
    layer.enable(); assert.equal(panel.hidden, false); assert.equal(panel.classList.contains('collapsed'), false);
    assert.equal(arrow.textContent, '▶'); assert.equal(arrow['aria-expanded'], 'true'); assert.equal(arrow['aria-label'], 'Collapse Social networks');
    panel.querySelector('.panel-collapse-btn').dispatchEvent(new Event('click'));
    assert.equal(panel.classList.contains('collapsed'), true);
    assert.equal(arrow.textContent, '▶'); assert.equal(arrow['aria-expanded'], 'false'); assert.equal(arrow['aria-label'], 'Expand Social networks');
    const status = panel.querySelector('[data-status]'); const refresh = layer.update();
    assert.equal(status.textContent, 'Loading publications…'); assert.equal(status.hidden, false);
    await refresh; assert.equal(panel.classList.contains('collapsed'), true);
    assert.equal(status.textContent, ''); assert.equal(status.hidden, true);
    assert.equal(layer.updateInterval, 60000);
    const backgroundRefresh = layer.update();
    assert.equal(status.hidden, true, 'Background polling retains the published view without a loading message');
    await backgroundRefresh;
    const network = panel.querySelector('form').elements.namedItem('platform');
    assert.deepEqual(network.children.map(option => [option.value, option.textContent]), [['', 'All'], ['facebook', 'Facebook'], ['instagram', 'Instagram'], ['tiktok', 'TikTok'], ['x', 'X']]);
    assert.equal(layer.state().snapshot.evidence.some(item => item.platform === 'sensor'), true, 'Only the filter choices exclude non-social evidence');
    layer.setFilters({ platform: 'instagram' }); data.evidence = data.evidence.filter(item => item.platform !== 'instagram'); await layer.update();
    assert.equal(network.value, 'instagram'); assert.ok(network.children.some(option => option.value === 'instagram'), 'Keep a selected social network when its posts leave the period');
    data.evidence = []; await layer.update();
    assert.deepEqual(network.children.map(option => option.value), ['', 'facebook', 'instagram', 'tiktok', 'x'], 'All four social networks remain available without any evidence');
    assert.equal(network.value, 'instagram'); data.evidence = publication().evidence;
    loadError = new Error('Connection lost'); await layer.update();
    assert.match(status.textContent, /Publication unavailable: Connection lost.*Showing the last successful publication/); assert.equal(status.hidden, false);
    loadError = undefined; await layer.update(); assert.equal(status.textContent, ''); assert.equal(status.hidden, true);
    layer.setFilters({ locality: 'Kennedy' }); layer.select('flood'); assert.equal(panel.classList.contains('collapsed'), false);
    const area = panel.querySelector('[data-area]'), originalFilters = layer.state().filters;
    area.dispatchEvent(new Event('click')); assert.ok(layer.state().filters.bbox); assert.equal(area.textContent, 'Clear map area');
    area.dispatchEvent(new Event('click')); assert.equal(area.textContent, 'Filter'); assert.deepEqual(layer.state().filters, originalFilters);
    layer.disable(); assert.equal(panel.hidden, true); assert.equal(panel.classList.contains('collapsed'), true);
    assert.deepEqual(changes.at(-1), { id: 'territorial-panel', collapsed: true, options: { explicit: true, persist: false, syncShare: false } });
    layer.enable(); assert.equal(panel.hidden, false); assert.equal(panel.classList.contains('collapsed'), false);
    assert.equal(layer.state().filters.locality, 'Kennedy'); assert.equal(layer.state().selectedId, 'flood');
    const escape = new Event('keydown', { cancelable: true }); escape.key = 'Escape'; panel.dispatchEvent(escape);
    assert.equal(escape.defaultPrevented, true); assert.equal(panel.classList.contains('collapsed'), true);
    const disclosure = panel.querySelector('.panel-collapse-btn'); assert.equal(disclosure.focused, true); disclosure.dispatchEvent(new Event('click'));
    assert.deepEqual(changes.at(-1), { id: 'territorial-panel', collapsed: false, options: { explicit: true, persist: false, syncShare: false } });
    const before = changes.length; lifetime.abort(); disclosure.dispatchEvent(new Event('click'));
    assert.equal(changes.length, before); assert.equal(observers[0].disconnected, true); assert.deepEqual(rail.children, [context]);
  } finally {
    lifetime.abort(); layer.destroy();
    for (const [key, value] of Object.entries(saved)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; }
  }
});

test('map popup navigates every captured ID, including repeated text, preserving selection and review drafts', async (t) => {
  const saved = { document: globalThis.document, Option: globalThis.Option };
  const rail = new PanelElement(), context = new PanelElement(), lifetime = new AbortController(); rail.append(context);
  const data = publication(); data.can_review = true;
  const evidence = Array.from({ length: 21 }, (_, index) => ({ id: `post-${index + 1}`, platform: index % 2 ? 'facebook' : 'x', text: `Publication ${index + 1}`, username: `person${index + 1}`, mode: 'Synthetic', location_method: 'fixture-location', created_at: data.published_at, observed_at: data.published_at }));
  evidence[0].attachments = [{ id: 'post-0001-image-01', type: 'image', mime_type: 'image/png', dataset_path: 'media/kennedy-flood.png', origin: 'ai_generated', sha256: 'b'.repeat(64) }];
  evidence[0].observed_at = '2026-10-03T02:00:00Z';
  data.incidents[0] = { ...data.incidents[0], evidence_ids: evidence.map((item) => item.id), review_note: 'Saved', mode: 'Synthetic', confidence: .7 };
  data.incidents[1].review_note = 'Rain note'; data.evidence.push(...evidence);
  const { layer, viewer } = harness(async () => data); layer.disable();
  globalThis.document = { createElement: (tag) => new PanelElement(tag), getElementById: (id) => id === 'global-context-panel' ? context : [...rail.querySelectorAll(`#${id}`), ...viewer.container.querySelectorAll(`#${id}`)][0] };
  globalThis.Option = class extends PanelElement { constructor(label, value) { super('option'); this.textContent = label; this.value = value; } };
  t.after(() => { lifetime.abort(); layer.destroy(); for (const [key, value] of Object.entries(saved)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; } });
  const { panel, popup, showEvidence } = mountTerritorialPanel({ layer, signal: lifetime.signal, request: async () => ({}), setPanelCollapsed(_id, collapsed) { panel.classList.toggle('collapsed', collapsed); } });
  layer.enable(); await layer.update(); layer.select('flood', false);
  const detail = panel.querySelector('.tc-event-detail'), display = popup.querySelector('.tc-popup-content');
  const cards = () => display.querySelectorAll('article');
  const pager = (label) => display.querySelectorAll('button').find((button) => button['aria-label'] === `${label} publication`);
  const count = () => detail.querySelector('#tc-review-count').textContent;
  const contents = (node) => [node.textContent || '', ...node.children.map(contents)].join(' ');
  assert.equal(popup.hidden, false); assert.equal(popup.focused, true); assert.equal(viewer.container.children[0], popup);
  assert.equal(cards().length, 1); assert.equal(cards()[0].id, 'tc-evidence-post-1'); assert.equal(pager('Previous').disabled, true);
  assert.equal(detail.querySelectorAll('article').length, 0, 'Publications are not repeated in the sidebar');
  assert.match(contents(cards()[0]), /X · @person1/); assert.match(contents(cards()[0]), /Synthetic/); assert.doesNotMatch(contents(cards()[0]), /Bogotá time/);
  assert.match(contents(cards()[0]), /2026-10-02 19:00:00/); assert.doesNotMatch(contents(cards()[0]), /21:00:00/);
  assert.equal(display.querySelector('.tc-evidence-page').querySelector('h4').textContent, 'Publications (1/21)');
  const image = cards()[0].querySelector('img');
  assert.equal(image.src, '/api/gods-eye-view/media/post-0001/image-01.png');
  assert.equal(image.title, 'AI-generated Synthetic image'); assert.equal(image.alt, image.title);
  assert.equal(display.querySelector('.tc-severity').textContent, 'High'); assert.equal(display.querySelector('.tc-severity').dataset.severity, 'high');
  assert.doesNotMatch(contents(detail), /SIMULATED|Provenance:|Classification confidence:|fixture-location|post-\d+/);
  assert.equal(detail.querySelector('textarea').maxLength, 1000); assert.equal(count(), '5 / 1000');
  const note = detail.querySelector('textarea'); note.value = 'x'.repeat(1000); note.dispatchEvent(new Event('input'));
  assert.equal(count(), '1000 / 1000');
  pager('Next').dispatchEvent(new Event('click'));
  assert.equal(cards().length, 1); assert.equal(cards()[0].id, 'tc-evidence-post-2'); assert.equal(pager('Previous').disabled, false);
  pager('Next').focus(); pager('Next').dispatchEvent(new Event('click')); assert.equal(document.activeElement, pager('Next'));
  assert.equal(keydown(display.querySelector('.tc-evidence-page'), 'End').defaultPrevented, true);
  assert.equal(cards().length, 1); assert.equal(cards()[0].id, 'tc-evidence-post-21'); assert.equal(pager('Next').disabled, true);
  assert.equal(document.activeElement, display.querySelector('.tc-evidence-page'));
  keydown(display.querySelector('.tc-evidence-page'), 'Home'); keydown(display.querySelector('.tc-evidence-page'), 'ArrowRight');
  assert.equal(cards()[0].id, 'tc-evidence-post-2');
  const repeated = { ...evidence[1], id: 'post-repeat', created_at: '2026-10-03T01:00:00Z' };
  data.evidence.unshift({ ...evidence[0], id: 'new-first', text: 'New first publication' }); data.evidence.push(repeated);
  data.incidents[0].evidence_ids.push('post-repeat', 'new-first');
  data.incidents[0].summary = 'Refreshed incident summary'; await layer.update();
  assert.equal(cards().length, 1); assert.equal(cards()[0].id, 'tc-evidence-post-2');
  assert.doesNotMatch(contents(cards()[0]), /publications with matching text/);
  assert.equal(display.querySelector('.tc-evidence-page').querySelector('h4').textContent, 'Publications (3/23)');
  assert.equal(detail.querySelector('textarea').value, 'x'.repeat(1000)); assert.equal(count(), '1000 / 1000');
  assert.match(contents(display), /Refreshed incident summary/); assert.doesNotMatch(contents(detail), /Refreshed incident summary/);
  layer.select('rain', false); assert.equal(cards().length, 1); assert.equal(cards()[0].id, 'tc-evidence-f1');
  assert.equal(cards()[0].querySelectorAll('button').length, 0);
  assert.equal(display.querySelector('.tc-severity').textContent, 'Low');
  assert.equal(detail.querySelector('textarea').value, 'Rain note'); assert.equal(count(), '9 / 1000');
  layer.select('flood', false); assert.equal(cards()[0].id, 'tc-evidence-new-first'); assert.equal(count(), '5 / 1000');
  showEvidence('post-21'); assert.equal(cards().length, 1); assert.equal(cards()[0].id, 'tc-evidence-post-21'); assert.equal(cards()[0].scrolled, true);
  assert.equal(layer.state().selectedId, 'flood'); assert.equal(panel.classList.contains('collapsed'), false);
  showEvidence('post-repeat'); assert.equal(cards()[0].id, 'tc-evidence-post-repeat'); assert.equal(cards()[0].scrolled, true);
  assert.equal(keydown(popup, 'Escape').defaultPrevented, true); assert.equal(popup.hidden, true);
  viewer.scene.postRender.raiseEvent(); assert.equal(popup.hidden, true, 'Closed cards stay closed through camera frames');
  layer.select('flood', false); assert.equal(popup.hidden, false);
  popup.querySelector('.tc-popup-close').dispatchEvent(new Event('click')); assert.equal(popup.hidden, true);
  layer.select('unknown', false); assert.equal(popup.hidden, true); assert.equal(cards().length, 0);
  assert.match(contents(detail), /Location unresolved: no map position available/);
  assert.equal(detail.querySelectorAll('.tc-evidence-page').length, 1, 'An unlocated event keeps its carousel in the sidebar');
  assert.match(panel.innerHTML, />From<input name="date_from" aria-label="From · Bogotá time"/);
  assert.match(panel.innerHTML, />To<input name="date_to" aria-label="To · Bogotá time"/);
});

test('selected map card follows projection and content resizing, stays inside the viewport and cleans up', async (t) => {
  const previousDocument = globalThis.document, panels = new Map();
  globalThis.document = { getElementById: (id) => panels.get(id) };
  t.after(() => { if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument; });
  const { layer, viewer } = harness(async () => publication()), popup = new PanelElement('section');
  const detach = layer.attachPopup(popup); t.after(() => { detach(); layer.destroy(); });
  await layer.update(); layer.select('flood', false); viewer.scene.postRender.raiseEvent();
  assert.equal(popup.hidden, false); assert.equal(popup.style.left, '420px'); assert.equal(popup.style.top, '82px');
  assert.equal(popup.dataset.placement, 'above');
  viewer.screen = { x: 1199, y: 799 }; viewer.scene.postRender.raiseEvent();
  assert.equal(popup.style.left, '832px'); assert.equal(popup.style.top, '481px');
  popup.offsetHeight = 460; popup.resizeObserver.callback(); assert.equal(popup.style.top, '321px', 'New slide or photo height repositions while Cesium is idle');
  viewer.screen = { x: 1, y: 1 }; viewer.scene.postRender.raiseEvent(); assert.equal(popup.style.left, '8px'); assert.equal(popup.style.top, '19px'); assert.equal(popup.dataset.placement, 'below');
  viewer.aboveHorizon = false; viewer.scene.postRender.raiseEvent(); assert.equal(popup.hidden, true);
  viewer.aboveHorizon = true; viewer.screen.x = -1; viewer.scene.postRender.raiseEvent(); assert.equal(popup.hidden, true);
  viewer.screen.x = 100; viewer.scene.postRender.raiseEvent(); assert.equal(popup.hidden, false);
  viewer.container.clientHeight = viewer.scene.canvas.clientHeight = 768; viewer.screen = { x: 600, y: 384 }; popup.offsetHeight = 480;
  viewer.scene.postRender.raiseEvent();
  assert.equal(popup.dataset.placement, 'above'); assert.equal(popup.style.maxHeight, '358px'); assert.equal(popup.style.top, '8px');
  assert.equal(parseFloat(popup.style.top) + popup.offsetHeight, 366, 'A tall card stops 18 pixels before its marker');
  layer.disable(); assert.equal(popup.hidden, true); layer.enable(); assert.equal(popup.hidden, false);
  layer.setFilters({ locality: 'Usaquén' }); assert.equal(popup.hidden, true);
  const resize = popup.resizeObserver; detach(); assert.equal(resize.disconnected, true); assert.equal(popup.parentNode, null);
  viewer.scene.postRender.raiseEvent(); assert.equal(viewer.scene.postRender.numberOfListeners, 0);
});

test('map card clears the native rails and dock, retaining usable controls on narrow screens', async (t) => {
  const previousDocument = globalThis.document;
  const rects = { 'left-panel-stack': { left: 20, right: 260, top: 80, bottom: 700, height: 620 },
    'right-context-rail': { left: 800, right: 1180, top: 80, bottom: 700, height: 620 },
    'top-center-actions': { top: 20, bottom: 60, height: 40 }, 'command-dock': { top: 700, bottom: 768, height: 68 } };
  const panels = Object.fromEntries(Object.entries(rects).map(([id, rect]) => [id, { getBoundingClientRect: () => rect }]));
  globalThis.document = { getElementById: (id) => panels[id] };
  const { layer, viewer } = harness(async () => publication()), popup = new PanelElement('section');
  const detach = layer.attachPopup(popup);
  t.after(() => { detach(); layer.destroy(); if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument; });
  viewer.container.clientHeight = viewer.scene.canvas.clientHeight = 768; viewer.screen = { x: 790, y: 384 }; popup.offsetHeight = 480;
  await layer.update(); layer.select('flood', false);
  assert.equal(popup.dataset.placement, 'above'); assert.equal(popup.style.top, '68px'); assert.equal(popup.style.maxHeight, '298px');
  assert.equal(popup.style.left, '432px'); assert.equal(parseFloat(popup.style.left) + popup.offsetWidth, 792);
  assert.equal(parseFloat(popup.style.top) + popup.offsetHeight, 366); assert.equal(popup.style.zIndex, '');
  viewer.screen.y = 120; viewer.scene.postRender.raiseEvent();
  assert.equal(popup.dataset.placement, 'below'); assert.ok(parseFloat(popup.style.top) + popup.offsetHeight < rects['command-dock'].top);
  viewer.container.clientWidth = viewer.scene.canvas.clientWidth = 640; viewer.screen.x = 320; rects['right-context-rail'].left = 390;
  panels['right-context-rail'].resizeObserver.callback();
  assert.equal(popup.style.zIndex, '120'); assert.equal(popup.style.width, '360px'); assert.equal(popup.style.left, '140px');
});

async function reviewHarness(t) {
  const previous = { document: globalThis.document, Option: globalThis.Option };
  const body = new PanelElement('body'), context = new PanelElement(), lifetime = new AbortController(), requests = [];
  body.append(context);
  const data = publication(); data.can_review = true;
  data.incidents[0] = { ...data.incidents[0], review_status: 'pending', review_note: 'Saved note', reviewed_evidence_ids: ['x1'] };
  const { layer } = harness(async () => structuredClone(data)); layer.disable();
  globalThis.document = { body, createElement: (tag) => new PanelElement(tag), getElementById: (id) => id === 'global-context-panel' ? context : body.querySelectorAll(`#${id}`)[0] };
  globalThis.Option = class extends PanelElement { constructor(label, value) { super('option'); this.textContent = label; this.value = value; } };
  const { panel } = mountTerritorialPanel({ layer, signal: lifetime.signal,
    request: (path, options) => new Promise((resolve, reject) => requests.push({ path, ...options, resolve, reject })),
    setPanelCollapsed(_id, collapsed) { panel.classList.toggle('collapsed', collapsed); },
  });
  t.after(() => { lifetime.abort(); layer.destroy(); for (const [key, value] of Object.entries(previous)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; } });
  layer.enable(); await layer.update(); layer.select('flood', false);
  const detail = panel.querySelector('.tc-event-detail');
  const form = () => detail.querySelector('.tc-review');
  const submit = (status) => {
    const event = new Event('submit', { cancelable: true });
    for (const name of ['validated', 'rejected']) form().querySelector(`[name="${name}"]`).checked = name === status;
    event.submitter = form().querySelectorAll('button').find((button) => button.textContent === 'Save');
    form().dispatchEvent(event); return event.submitter;
  };
  const dialog = () => body.querySelectorAll('dialog')[0];
  return { data, layer, lifetime, requests, detail, form, dialog, submit,
    note: () => form().querySelector('textarea'), feedback: () => form().querySelector('.tc-review-status'),
    dialogButton: (label) => dialog().querySelectorAll('button').find((button) => button.textContent === label),
    settle: () => new Promise(setImmediate),
  };
}

test('review confirmation focuses Cancel and Cancel or Escape never sends a mutation', async t => {
  const view = await reviewHarness(t);
  const origin = view.submit('validated');
  assert.equal(view.requests.length, 0); assert.equal(view.dialog().open, true);
  assert.equal(view.dialog()['aria-label'], 'Validate event?');
  assert.equal(view.dialog()['aria-describedby'], 'tc-review-confirm-description');
  assert.equal(view.dialogButton('Cancel').focused, true);
  view.dialogButton('Cancel').dispatchEvent(new Event('click')); await view.settle();
  assert.equal(view.dialog(), undefined); assert.equal(view.requests.length, 0); assert.equal(origin.focused, true);
  assert.ok(view.form().querySelectorAll('button').every(button => !button.disabled));
  view.submit('rejected'); const cancel = new Event('cancel', { cancelable: true }); view.dialog().dispatchEvent(cancel); await view.settle();
  assert.equal(cancel.defaultPrevented, true); assert.equal(view.requests.length, 0); assert.equal(view.dialog(), undefined);
  view.form().dispatchEvent(new Event('submit', { cancelable: true }));
  assert.equal(view.requests.length, 0, 'Enter on the Save form must still confirm before a mutation');
  view.dialogButton('Cancel').dispatchEvent(new Event('click')); await view.settle();
});

test('each review decision confirms once, sends its frozen note and evidence, and survives publication repaint', async t => {
  const view = await reviewHarness(t);
  for (const [status, label] of [['validated', 'validate'], ['rejected', 'reject'], ['pending', 'pending']]) {
    const note = `Decision ${status}`; view.note().value = note;
    view.submit(status); view.submit(status);
    assert.equal(globalThis.document.body.querySelectorAll('dialog').length, 1);
    const confirm = view.dialogButton(`Confirm ${label}`);
    confirm.dispatchEvent(new Event('click')); confirm.dispatchEvent(new Event('click')); await view.settle();
    const request = view.requests.at(-1);
    assert.equal(view.requests.length, ['validated', 'rejected', 'pending'].indexOf(status) + 1);
    assert.equal(request.path, '/api/prisma/incidents/flood/review'); assert.equal(request.method, 'POST');
    assert.deepEqual(JSON.parse(request.body), { status, note, expected_evidence_ids: ['x1'], lat: 4.62, lon: -74.14 });
    assert.match(view.feedback().textContent, /Saving review/); assert.equal(view.form()['aria-busy'], 'true');
    view.data.incidents[0].summary = `Updated while saving ${status}`; await view.layer.update();
    assert.equal(request.signal.aborted, false, 'A repaint must not abort an accepted review request');
    view.submit(status); assert.equal(view.dialog(), undefined); assert.equal(view.requests.length, ['validated', 'rejected', 'pending'].indexOf(status) + 1);
    Object.assign(view.data.incidents[0], { review_status: status, review_note: note, reviewed_evidence_ids: ['x1'] });
    request.resolve({ ...view.data.incidents[0], review_pending_publication: false }); await view.settle();
    assert.match(view.feedback().textContent, new RegExp(`Review saved: ${status[0].toUpperCase()}${status.slice(1)}`));
    assert.equal(view.form()['aria-busy'], 'false'); assert.equal(view.note().value, note);
    await view.layer.update(); assert.match(view.feedback().textContent, /Review saved:/);
  }
});

test('confirmation expires when its evidence changes, with no POST and the draft preserved', async t => {
  const view = await reviewHarness(t);
  view.note().value = 'My draft'; view.submit('validated');
  view.data.incidents[0].evidence_ids.push('new-post'); view.data.evidence.push({ id: 'new-post', platform: 'x' });
  await view.layer.update(); await view.settle();
  assert.equal(view.dialog(), undefined); assert.equal(view.requests.length, 0); assert.equal(view.note().value, 'My draft');
  view.submit('validated'); view.dialogButton('Confirm validate').dispatchEvent(new Event('click')); await view.settle();
  assert.deepEqual(JSON.parse(view.requests[0].body).expected_evidence_ids, ['x1', 'new-post']);
  view.requests[0].reject(Object.assign(new Error('Conflict'), { status: 409 })); await view.settle();
  assert.equal(view.feedback().role, 'alert'); assert.match(view.feedback().textContent, /Event evidence or its saved location changed/); assert.equal(view.note().value, 'My draft');
  assert.equal(view.requests.length, 1, 'A conflict never retries without another confirmation');
});

test('accepted AIDP review stays pending until status, note and reviewed evidence are published together', async t => {
  const view = await reviewHarness(t);
  Object.assign(view.data.incidents[0], { review_status: 'validated', evidence_ids: ['x1', 'f1'] }); await view.layer.update();
  view.submit('validated'); view.dialogButton('Confirm validate').dispatchEvent(new Event('click')); await view.settle();
  view.requests[0].resolve({ review_status: 'validated', review_note: 'Saved note', reviewed_evidence_ids: ['x1', 'f1'], review_pending_publication: true,
    publication_error: 'The publication job could not be started.' }); await view.settle();
  assert.match(view.feedback().textContent, /Review saved: Validated.*Waiting for.*could not be started/); assert.equal(view.feedback().role, 'alert');
  view.data.incidents[0].summary = 'Same decision, previous evidence set'; await view.layer.update();
  assert.match(view.feedback().textContent, /Waiting for/);
  view.data.incidents[0].reviewed_evidence_ids = ['f1', 'x1']; await view.layer.update();
  assert.equal(view.feedback().textContent, 'Review saved: Validated.'); assert.equal(view.feedback().role, 'status');
  assert.equal(view.layer.state().snapshot.version, 'v1', 'A matching publication need not have a different version');
});

test('Save confirms draft coordinates, preserves them through polling, and waits for the published location', async t => {
  const view = await reviewHarness(t), coordinate = (name) => view.form().querySelector(`[name="${name}"]`);
  view.data.incidents[0].evidence_ids.push('older-evidence-outside-window'); await view.layer.update();
  view.note().value = 'Move to the reported corner'; coordinate('lat').value = '4.64'; coordinate('lon').value = '-74.12'; coordinate('lon').dispatchEvent(new Event('change'));
  assert.equal(view.layer.state().items[0].lat, 4.64); assert.equal(view.layer.state().snapshot.incidents[0].lat, 4.62);
  view.form().querySelector('[name="validated"]').checked = true;
  view.data.incidents[0].summary = 'New data while editing'; await view.layer.update();
  assert.equal(coordinate('lat').value, '4.64'); assert.equal(coordinate('lon').value, '-74.12'); assert.equal(view.note().value, 'Move to the reported corner');
  assert.equal(view.form().querySelector('[name="validated"]').checked, true);
  view.submit('validated'); assert.equal(view.requests.length, 0);
  view.dialogButton('Cancel').dispatchEvent(new Event('click')); await view.settle(); assert.equal(view.requests.length, 0); assert.equal(coordinate('lat').disabled, false);
  view.submit('validated'); view.dialogButton('Confirm validate').dispatchEvent(new Event('click')); await view.settle();
  assert.deepEqual(JSON.parse(view.requests[0].body), { status: 'validated', note: 'Move to the reported corner', expected_evidence_ids: ['x1', 'older-evidence-outside-window'], lat: 4.64, lon: -74.12 });
  view.requests[0].resolve({ review_status: 'validated', review_note: 'Move to the reported corner', reviewed_evidence_ids: [...view.data.incidents[0].evidence_ids], lat: 4.64, lon: -74.12, review_pending_publication: true }); await view.settle();
  assert.match(view.feedback().textContent, /Waiting for/); assert.equal(coordinate('lat').disabled, true);
  Object.assign(view.data.incidents[0], { review_status: 'validated', review_note: 'Move to the reported corner', reviewed_evidence_ids: [...view.data.incidents[0].evidence_ids] }); await view.layer.update();
  assert.match(view.feedback().textContent, /Waiting for/, 'UI draft coordinates cannot stand in for persisted coordinates');
  Object.assign(view.data.incidents[0], { lat: 4.64, lon: -74.12 }); await view.layer.update();
  assert.equal(view.feedback().textContent, 'Review saved: Validated.'); assert.equal(coordinate('lat').disabled, true);
  view.form().querySelector('[name="validated"]').checked = false;
  assert.equal(coordinate('lat').disabled, true, 'Unchecking validation alone does not unlock a persisted validated marker');
  view.submit('pending'); view.dialogButton('Confirm pending').dispatchEvent(new Event('click')); await view.settle();
  assert.equal(JSON.parse(view.requests[1].body).lat, 4.64);
  view.data.incidents[0].review_status = 'pending'; view.requests[1].resolve({ ...view.data.incidents[0], review_pending_publication: false }); await view.settle();
  assert.equal(coordinate('lat').disabled, false); assert.equal(view.layer.canMove('flood'), true);
  const validated = view.form().querySelector('[name="validated"]'), rejected = view.form().querySelector('[name="rejected"]');
  validated.checked = true; validated.dispatchEvent(new Event('change')); rejected.checked = true; rejected.dispatchEvent(new Event('change')); assert.equal(validated.checked, false);
});

test('failed saves remain readable and editable, and untouched notes track persisted changes', async t => {
  const view = await reviewHarness(t);
  view.data.incidents[0].review_note = 'From another reviewer'; await view.layer.update();
  assert.equal(view.note().value, 'From another reviewer');
  view.note().value = 'Draft survives failure'; view.submit('rejected');
  view.dialogButton('Confirm reject').dispatchEvent(new Event('click')); await view.settle();
  view.requests[0].reject(new Error('Review service unavailable')); await view.settle();
  assert.match(view.feedback().textContent, /could not be confirmed: Review service unavailable/); assert.equal(view.feedback().role, 'alert');
  assert.equal(view.note().value, 'Draft survives failure'); assert.ok(view.form().querySelectorAll('button').every(button => !button.disabled));
  view.data.incidents[0].review_note = 'Another saved note'; await view.layer.update();
  assert.equal(view.note().value, 'Draft survives failure'); assert.match(view.feedback().textContent, /could not be confirmed/);
});

test('a later published decision retires earlier success feedback and updates an untouched note', async t => {
  const view = await reviewHarness(t);
  view.data.incidents[0].title = 'Flooding · Kennedy'; await view.layer.update();
  view.submit('validated');
  assert.equal(view.dialog().querySelector('#tc-review-confirm-description').textContent, 'Save this review for Flooding · Kennedy?');
  view.dialogButton('Confirm validate').dispatchEvent(new Event('click')); await view.settle();
  view.data.incidents[0].review_status = 'validated';
  view.requests[0].resolve({ ...view.data.incidents[0], review_pending_publication: false }); await view.settle();
  assert.equal(view.feedback().textContent, 'Review saved: Validated.');
  Object.assign(view.data.incidents[0], { review_status: 'rejected', review_note: 'Another reviewer rejected it' }); await view.layer.update();
  assert.equal(view.feedback().textContent, ''); assert.equal(view.note().value, 'Another reviewer rejected it');
});

test('a matching publication resolves a lost-response error without claiming that the POST succeeded', async t => {
  const view = await reviewHarness(t);
  view.note().value = 'Decision despite lost response'; view.submit('rejected');
  view.dialogButton('Confirm reject').dispatchEvent(new Event('click')); await view.settle();
  Object.assign(view.data.incidents[0], { review_status: 'rejected', review_note: 'Decision despite lost response', reviewed_evidence_ids: ['x1'] });
  await view.layer.update(); assert.equal(view.feedback().textContent, 'Saving review…', 'A snapshot never releases an in-flight save');
  view.requests[0].reject(new Error('Response lost')); await view.settle();
  assert.equal(view.feedback().textContent, 'The published review matches this decision.'); assert.equal(view.feedback().role, 'status');
  Object.assign(view.data.incidents[0], { review_status: 'pending', review_note: '' }); await view.layer.update();
  assert.equal(view.feedback().textContent, '');
});

test('panel shutdown cancels confirmation and ignores a late save acknowledgment', async t => {
  const view = await reviewHarness(t);
  view.submit('validated'); view.lifetime.abort(); await view.settle();
  assert.equal(view.dialog(), undefined); assert.equal(view.requests.length, 0);
});

test('panel shutdown aborts a submitted review without publishing success from its late result', async t => {
  const view = await reviewHarness(t);
  view.submit('validated'); view.dialogButton('Confirm validate').dispatchEvent(new Event('click')); await view.settle();
  const feedback = view.feedback(), request = view.requests[0];
  view.lifetime.abort(); assert.equal(request.signal.aborted, true);
  request.resolve({ review_status: 'validated', review_note: 'Saved note', reviewed_evidence_ids: ['x1'] }); await view.settle();
  assert.equal(feedback.textContent, 'Saving review…'); assert.equal(view.layer.state().items[0].review_status, 'pending');
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

test('Agent Flow requires a published snapshot before asking AIDP', () => {
  assert.throws(() => chatContext({ snapshot: { version: '' } }), /published/);
});

function analystHarness(t, request, options = {}) {
  const previous = globalThis.document, lifetime = new AbortController();
  const { startEnabled = true, agentFlow = createAgentFlowLayer(), ...settings } = options;
  const rail = new PanelElement(), context = new PanelElement(), social = new PanelElement(), sensors = new PanelElement(), changes = [];
  rail.append(context, social, sensors);
  globalThis.document = { getElementById: (id) => ({ 'global-context-panel': context, 'territorial-panel': social, 'sensors-panel': sensors })[id] || null,
    createElement: (tag) => new PanelElement(tag), body: { append() { assert.fail('Agent Flow belongs in the native Context rail'); } } };
  t.after(() => { lifetime.abort(); if (previous === undefined) delete globalThis.document; else globalThis.document = previous; });
  const { panel, ask } = mountAnalyst({ request, agentFlow, signal: lifetime.signal, showEvidence() {},
    layer: { state: () => ({ snapshot: publication(), filters: { locality: 'Kennedy' }, selectedId: 'flood' }) },
    setPanelCollapsed(id, collapsed, options) {
      changes.push({ id, collapsed, options }); panel.classList.toggle('collapsed', collapsed);
      PanelChrome.prototype._syncPanelCollapseButton.call({}, panel);
    }, ...settings });
  if (startEnabled) agentFlow.enable();
  const form = panel.querySelector('form');
  return { panel, form, textarea: form.querySelector('textarea'), log: panel.querySelector('#tc-aidp-log'),
    status: panel.querySelector('[data-chat-status]'), count: panel.querySelector('[data-turns]'),
    rail, context, social, sensors, changes, lifetime, ask, agentFlow };
}

function keydown(target, key, options = {}) {
  const event = Object.assign(new Event('keydown', { cancelable: true }), { key, ...options });
  target.dispatchEvent(event); return event;
}

test('exclusive Agent Flow receives the whole right rail and restores siblings when collapsed or hidden', () => {
  const allocation = '--right-panel-allocated-height';
  function rail(withSiblings) {
    const stack = new PanelElement(), chat = new PanelElement(), siblings = withSiblings ? [new PanelElement(), new PanelElement()] : [];
    chat.id = 'territorial-analyst'; chat.dataset.railExclusive = '';
    siblings.forEach((panel, index) => { panel.id = `sibling-${index}`; panel.classList.toggle('collapsed', index === 1); panel.style.setProperty(allocation, '111px'); });
    for (const panel of [...siblings, chat]) {
      panel.dataset.panelId = panel.id; panel.hidden = false;
      panel.getBoundingClientRect = () => ({ left: 900, right: 1200, top: 150, bottom: 150 + (panel.classList.contains('collapsed') ? 42 : panel === chat ? 900 : 400), width: 300, height: panel.classList.contains('collapsed') ? 42 : panel === chat ? 900 : 400 });
      stack.append(panel);
    }
    return { stack, chat, siblings };
  }
  const state = panel => ({ classes: [...panel.classes], hidden: panel.hidden, ariaHidden: panel['aria-hidden'], allocation: panel.style.getPropertyValue(allocation) });
  for (const variant of ['minimal', 'operator', 'tactical']) for (const mobile of [false, true]) {
    const run = ({ stack, chat }) => layoutRightPanelRail({ stack, obstacles: [], hud: { visible: true, variant }, preferredPanelId: chat.id,
      windowRef: { innerHeight: 900, matchMedia: () => ({ matches: mobile }) }, getComputedStyle: () => ({ rowGap: '8px' }),
      leftStack: { getBoundingClientRect: () => ({ top: 150 }) }, documentRef: { activeElement: null },
      readDisplayScrollTop: () => 0, onCollapse() {}, onRetry() {},
    });
    const actual = rail(true), alone = rail(false), before = actual.siblings.map(state);
    run(actual); run(alone);
    assert.deepEqual(actual.siblings.map(state), before, `${variant}/${mobile}: exclusive chat must not mutate siblings`);
    assert.equal(actual.chat.style.getPropertyValue(allocation), alone.chat.style.getPropertyValue(allocation));
    assert.equal(actual.stack.dataset.requiredHeight, alone.stack.dataset.requiredHeight);
    assert.equal(actual.stack.dataset.expandedCount, alone.stack.dataset.expandedCount);
    assert.equal(actual.stack.dataset.layoutMode, mobile ? 'mobile' : 'focus');
    for (const mode of ['collapsed', 'hidden']) {
      const ordinary = rail(true); delete ordinary.chat.dataset.railExclusive;
      for (const fixture of [actual, ordinary]) { fixture.chat.classList.toggle('collapsed', mode === 'collapsed'); fixture.chat.hidden = mode === 'hidden'; }
      run(actual); run(ordinary);
      assert.deepEqual(actual.siblings.map(state), ordinary.siblings.map(state), `${variant}/${mobile}/${mode}: restore normal sibling layout`);
      assert.equal(actual.stack.dataset.requiredHeight, ordinary.stack.dataset.requiredHeight);
      assert.equal(actual.stack.dataset.expandedCount, ordinary.stack.dataset.expandedCount);
    }
  }
});

test('Agent Flow is one native Context panel and Enter submits without taking Shift, IME or repeats', async (t) => {
  const requests = [];
  const view = analystHarness(t, async (path, options) => {
    requests.push({ path, body: JSON.parse(options.body) });
    return { answer: 'Evidence from Kennedy.', version: 'v1', session_id: 'first-session', runtime: 'aidp', actions: [], evidence_ids: [] };
  });
  const { panel, form, textarea, rail, context, social, sensors, changes, lifetime } = view;
  assert.equal(panel.tagName, 'SECTION'); assert.equal(panel.id, 'territorial-analyst');
  assert.equal(panel.dataset.railExclusive, '');
  assert.deepEqual(rail.children, [context, panel, social, sensors]); assert.equal(panel.hidden, false); assert.equal(panel.classList.contains('collapsed'), false);
  assert.match(panel.innerHTML, /class="panel-title">AI Assistant</i);
  assert.match(panel.innerHTML, /id="tc-aidp-log"[^>]*role="log"[^>]*aria-label="AI Assistant conversation"/);
  assert.match(panel.innerHTML, /id="tc-aidp-log" class="tc-chat-log scene-shot-list"/);
  assert.doesNotMatch(panel.innerHTML, /<summary|role="tab|tc-oci|data-provider=/);
  const header = panel.innerHTML.split('tc-panel-body')[0];
  assert.match(header, /data-new/); assert.match(header, /aria-label="New conversation"/); assert.match(header, /<svg/);
  const disclosure = panel.querySelector('.panel-collapse-btn');
  panel.querySelector('.panel-title, .location-toolbar-label').textContent = 'AI Assistant';
  disclosure.dispatchEvent(new Event('click'));
  disclosure.dispatchEvent(new Event('click'));
  assert.equal(panel.classList.contains('collapsed'), false); assert.equal(disclosure['aria-expanded'], 'true');
  const escape = keydown(panel, 'Escape');
  assert.equal(escape.defaultPrevented, true); assert.equal(panel.classList.contains('collapsed'), true); assert.equal(disclosure.focused, true);
  assert.deepEqual(changes.at(-1), { id: 'territorial-analyst', collapsed: true, options: { explicit: true, persist: false, syncShare: false } });
  textarea.value = '  What happened in Kennedy?  ';
  for (const flags of [{ shiftKey: true }, { isComposing: true }]) {
    assert.equal(keydown(textarea, 'Enter', flags).defaultPrevented, false);
  }
  assert.equal(keydown(textarea, 'Enter', { repeat: true }).defaultPrevented, true);
  assert.equal(requests.length, 0); assert.equal(form.submitRequests, undefined);
  assert.equal(keydown(textarea, 'Enter').defaultPrevented, true);
  assert.equal(textarea.value, '', 'An accepted submission clears the composer immediately');
  await new Promise(setImmediate);
  assert.equal(form.submitRequests, 1); assert.equal(requests.length, 1);
  assert.deepEqual(requests[0], { path: '/api/prisma/chat', body: { question: 'What happened in Kennedy?', version: 'v1', filters: { locality: 'Kennedy' }, incident_id: 'flood' } });
  assert.match(view.count.textContent, /^1 questions?$/); assert.equal(view.log.children.length, 2);
  assert.equal(textarea.value, '');
  const changesBefore = changes.length; lifetime.abort(); disclosure.dispatchEvent(new Event('click'));
  assert.equal(changes.length, changesBefore); assert.deepEqual(rail.children, [context, social, sensors]);
});

for (const [phase, errorStatus, edit] of [
  ['snapshot', 500, 'untouched'], ['chat', 409, 'untouched'], ['chat', 500, 'untouched'],
  ['chat', 500, 'new draft'], ['chat', 500, 'cleared'],
]) test(`composer respects ${edit} input after ${phase} error ${errorStatus}`, async (t) => {
  const requests = []; let reject;
  const view = analystHarness(t, (path, options) => {
    requests.push({ path, options }); return new Promise((_resolve, fail) => { reject = fail; });
  }, phase === 'snapshot' ? { layer: { state: () => ({ enabled: false, snapshot: publication(), filters: {} }) }, sensorContext: () => ({ enabled: false }) } : {});
  const original = '  Compare <sensor> with social reports.\n';
  view.textarea.value = original; view.form.requestSubmit();
  assert.equal(view.textarea.value, '', 'Clear before either the snapshot GET or chat POST finishes');
  assert.equal(view.log.children[0].querySelector('p').textContent, original.trim());
  await new Promise(setImmediate);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, phase === 'snapshot' ? '/api/prisma/snapshot' : '/api/prisma/chat');
  if (phase === 'chat') assert.equal(JSON.parse(requests[0].options.body).question, original.trim());
  if (edit !== 'untouched') {
    view.textarea.value = 'Next question'; view.textarea.dispatchEvent(new Event('input'));
    if (edit === 'cleared') { view.textarea.value = ''; view.textarea.dispatchEvent(new Event('input')); }
  }
  reject(Object.assign(new Error('Request failed'), { status: errorStatus })); await new Promise(setImmediate);
  assert.equal(view.textarea.value, edit === 'untouched' ? original : edit === 'cleared' ? '' : 'Next question');
  assert.equal(view.form['aria-busy'], 'false'); assert.equal(view.form.querySelector('button')['aria-label'], 'Send question');
  assert.equal(requests.length, 1, 'An error never resubmits automatically');
});

for (const phase of ['snapshot', 'chat']) for (const action of ['stop', 'new']) test(`late ${phase} errors after ${action} cannot restore the submitted draft`, async (t) => {
  let reject, requestSignal;
  const view = analystHarness(t, (_path, options) => {
    requestSignal = options.signal; return new Promise((_resolve, fail) => { reject = fail; });
  }, phase === 'snapshot' ? { layer: { state: () => ({ enabled: false, snapshot: publication(), filters: {} }) }, sensorContext: () => ({ enabled: false }) } : {});
  view.textarea.value = 'Abandoned question'; view.form.requestSubmit(); await new Promise(setImmediate);
  assert.equal(view.textarea.value, '');
  const button = action === 'new' ? view.panel.querySelector('[data-new]') : view.form.querySelector('button');
  button.dispatchEvent(new Event('click', { cancelable: true }));
  assert.equal(requestSignal.aborted, true);
  const status = view.status.textContent, messages = [...view.log.children];
  reject(Object.assign(new Error('Late request failure'), { status: 500 })); await new Promise(setImmediate);
  assert.equal(view.textarea.value, ''); assert.equal(view.status.textContent, status);
  assert.deepEqual(view.log.children, messages); assert.equal(view.form['aria-busy'], 'false');
});

test('chat timestamps record send and reply time locally, preserve a new draft and omit the real-agent publication footer', async (t) => {
  let resolve;
  const view = analystHarness(t, () => new Promise(done => { resolve = done; }));
  const sentAt = new Date(); view.textarea.value = 'Sensor status?'; view.form.requestSubmit();
  assert.equal(view.textarea.value, ''); await new Promise(setImmediate);
  view.textarea.value = 'My next question'; view.textarea.dispatchEvent(new Event('input'));
  t.mock.timers.tick(65_000); const receivedAt = new Date();
  resolve({ answer: 'Current evidence.', version: 'v1', runtime: 'aidp', evidence_ids: [], actions: [] }); await new Promise(setImmediate);
  assert.equal(view.textarea.value, 'My next question'); assert.equal(view.log.children.length, 2);
  const formatter = new Intl.DateTimeFormat('es-CO', { hour: 'numeric', minute: '2-digit', hour12: true });
  for (const [index, at] of [sentAt, receivedAt].entries()) {
    const entry = view.log.children[index], times = entry.querySelectorAll('time');
    assert.equal(times.length, 1); assert.equal(times[0].classList.contains('tc-message-time'), true);
    assert.equal(times[0].dateTime, at.toISOString());
    assert.equal(times[0].textContent.replace(/\s/g, ''), formatter.format(at).replace(/\s/g, ''));
    assert.equal(entry.querySelectorAll('small').length, 0, 'Real-agent bubbles contain no technical publication footer');
  }
});

test('Agent Flow follows the native layer toggle, cancels GET and POST, and preserves its conversation', async (t) => {
  const agentFlow = createAgentFlowLayer(), manager = new LayerLifecycle({});
  manager.register(agentFlow); manager.finalizeRegistrations([{ id: agentFlow.id, disposition: 'local-only' }]);
  t.after(() => manager.destroyAll());
  const requests = [], pending = [];
  const view = analystHarness(t, (path, options) => {
    requests.push({ path, signal: options.signal }); return new Promise(resolve => pending.push(resolve));
  }, { agentFlow, startEnabled: false,
    layer: { state: () => ({ enabled: false, snapshot: publication(), filters: {}, selectedId: null }) },
    sensorContext: () => ({ enabled: false }) });
  assert.equal(view.panel.hidden, true); assert.equal(manager.isEnabled(agentFlow.id), false);
  await assert.rejects(view.ask('While off'), { name: 'AbortError' }); assert.equal(requests.length, 0);
  assert.equal(await manager.setEnabled(agentFlow.id, true, { origin: 'user' }), true);
  assert.equal(view.panel.hidden, false); assert.equal(view.panel.classList.contains('collapsed'), false);
  view.textarea.value = 'Preserve my draft';
  const first = view.ask('First question');
  await manager.setEnabled(agentFlow.id, false, { origin: 'user' });
  assert.equal(requests[0].path, '/api/prisma/snapshot'); assert.equal(requests[0].signal.aborted, true);
  pending.shift()(publication()); await assert.rejects(first, { name: 'AbortError' });
  assert.equal(view.panel.hidden, true); assert.equal(view.panel.classList.contains('collapsed'), true);
  assert.equal(view.textarea.value, 'Preserve my draft'); assert.equal(view.log.children.length, 1);
  await manager.setEnabled(agentFlow.id, true, { origin: 'voice' });
  const second = view.ask('Second question'); pending.shift()(publication()); await new Promise(setImmediate);
  assert.equal(requests[2].path, '/api/prisma/chat');
  await manager.setEnabled(agentFlow.id, false, { origin: 'user' });
  assert.equal(requests[2].signal.aborted, true);
  pending.shift()({ answer: 'Late answer', version: 'v1', evidence_ids: [], actions: [] });
  await assert.rejects(second, { name: 'AbortError' });
  await manager.setEnabled(agentFlow.id, true, { origin: 'user' });
  assert.equal(view.log.children.length, 2); assert.equal(view.panel.hidden, false);
  assert.equal(view.textarea.value, 'Preserve my draft'); assert.equal(view.form['aria-busy'], 'false');
});

test('Agent Flow toggles one Send/Stop button, cancels with an empty draft and ignores late replies across conversations', async (t) => {
  const requests = [], pending = [];
  const { panel, form, textarea, log, status, count } = analystHarness(t, (path, options) => {
    requests.push({ path, body: JSON.parse(options.body), signal: options.signal });
    return new Promise((resolve) => pending.push(resolve));
  });
  const finish = async (sessionId) => { await new Promise(setImmediate); pending.shift()({ answer: 'AIDP evidence.', version: 'v1', session_id: sessionId, runtime: 'aidp', actions: [], evidence_ids: [] }); await new Promise(setImmediate); };
  const send = form.querySelector('button'), sendIcon = send.innerHTML;
  assert.doesNotMatch(panel.innerHTML, /data-cancel|Cancel request/);
  assert.equal(send.type, 'submit'); assert.equal(send['aria-label'], 'Send question'); assert.equal(send.title, 'Send question');
  assert.match(sendIcon, /M10\.3009 13\.6949L20\.102 3\.89742/); assert.match(sendIcon, /stroke="currentColor"/);
  textarea.value = 'First question'; form.requestSubmit(); await finish('first-session');
  textarea.value = 'Follow up'; form.requestSubmit(); await new Promise(setImmediate);
  assert.equal(requests[1].body.session_id, 'first-session'); assert.match(count.textContent, /^2 questions$/);
  assert.equal(form['aria-busy'], 'true'); assert.equal(form.querySelector('button'), send);
  assert.equal(send.type, 'button'); assert.notEqual(send.disabled, true); assert.equal(send['aria-label'], 'Stop request'); assert.equal(send.title, 'Stop request');
  assert.match(send.innerHTML, /fill-rule="evenodd"/); assert.match(send.innerHTML, /fill="currentColor"/); assert.notEqual(send.innerHTML, sendIcon);
  form.requestSubmit(); assert.equal(requests.length, 2);
  textarea.value = '';
  const stop = new Event('click', { cancelable: true }); send.dispatchEvent(stop);
  assert.equal(stop.defaultPrevented, true, 'Stopping cannot activate Submit after the button returns to send mode');
  assert.equal(requests[1].signal.aborted, true); assert.equal(form['aria-busy'], 'false');
  assert.equal(send.type, 'submit'); assert.equal(send['aria-label'], 'Send question'); assert.equal(send.title, 'Send question'); assert.equal(send.innerHTML, sendIcon);
  assert.equal(requests.length, 2); assert.match(status.textContent, /cancelled/i);
  const messagesBefore = log.children.length; await finish('cancelled-session');
  assert.equal(log.children.length, messagesBefore);
  panel.querySelector('[data-new]').dispatchEvent(new Event('click'));
  assert.equal(count.textContent, '0 questions'); assert.equal(log.children.length, 0); assert.equal(textarea.value, ''); assert.equal(status.textContent, '');
  textarea.value = 'New question'; form.requestSubmit(); await new Promise(setImmediate);
  assert.equal(requests[2].body.session_id, undefined); assert.match(count.textContent, /^1 questions?$/);
  panel.querySelector('[data-new]').dispatchEvent(new Event('click'));
  assert.equal(requests[2].signal.aborted, true); await finish('abandoned-session');
  assert.equal(count.textContent, '0 questions'); assert.equal(log.children.length, 0);
  textarea.value = 'Fresh question'; form.requestSubmit(); await new Promise(setImmediate);
  assert.equal(requests[3].body.session_id, undefined); await finish('fresh-session');
  assert.equal(log.children.length, 2); assert.match(count.textContent, /^1 questions?$/);
});

test('voice questions keep sensor context and valid citations leave exclusive chat while stale citations keep focus', async (t) => {
  const requests = [], selected = [], snapshot = { ...publication(), sensors: [{ id: 'reading-1', sensor_id: 'station-1' }] };
  const show = (id) => { assert.equal(view.panel.classList.contains('collapsed'), true, 'Collapse before opening the destination'); selected.push(id); };
  const view = analystHarness(t, async (path, options) => {
    requests.push(JSON.parse(options.body));
    return { answer: 'Synthetic: no real observation or automatic incident validation.', version: 'v1', session_id: 'sensor-session',
      runtime: 'aidp', evidence_ids: ['x1'], sensor_evidence_ids: ['reading-1'], actions: [] };
  }, { layer: { state: () => ({ snapshot, filters: {}, selectedId: null }) }, sensorContext: () => ({ sensor_id: 'station-1' }), showEvidence: show, showSensor: show });
  await view.ask('Compare this sensor with social reports.', { signal: new AbortController().signal });
  await view.ask('What changed?');
  assert.equal(requests[0].sensor_id, 'station-1'); assert.equal(requests[1].session_id, 'sensor-session');
  assert.equal(view.panel.classList.contains('collapsed'), false);
  const citations = view.log.children[1].children.filter((item) => item.tagName === 'BUTTON'), conversation = [...view.log.children];
  assert.equal(citations.length, 2);
  for (const [index, citation] of citations.entries()) {
    view.agentFlow.enable(); citation.focus(); citation.dispatchEvent(new Event('click'));
    assert.equal(document.activeElement, [view.social, view.sensors][index].querySelector('.panel-collapse-btn'));
    assert.deepEqual(view.log.children, conversation, 'Opening evidence preserves the conversation');
  }
  assert.deepEqual(selected, ['x1', 'reading-1']);
  snapshot.version = 'v2'; view.agentFlow.enable(); const changes = view.changes.length;
  for (const citation of citations) {
    citation.focus(); citation.dispatchEvent(new Event('click'));
    assert.equal(document.activeElement, citation); assert.equal(view.panel.classList.contains('collapsed'), false);
    assert.equal(view.changes.length, changes); assert.match(view.status.textContent, /earlier publication/);
  }
  assert.deepEqual(selected, ['x1', 'reading-1']); assert.deepEqual(view.log.children, conversation);
});

test('Agent Flow can use the Sensors publication when Social networks has never loaded', async (t) => {
  const snapshot = { ...publication(), sensors: [{ id: 'reading-1', sensor_id: 'station-1' }] }, requests = [];
  const view = analystHarness(t, async (_path, options) => { requests.push(JSON.parse(options.body)); return {
    answer: 'Synthetic sensor reading.', version: 'v1', evidence_ids: [], sensor_evidence_ids: ['reading-1'], actions: [] }; },
  { layer: { state: () => ({ enabled: false, snapshot: { version: '', incidents: [], evidence: [] }, filters: {}, selectedId: null }) },
    sensorContext: () => ({ snapshot, sensor_id: 'station-1' }), showSensor() {} });
  await view.ask('Sensor status?');
  assert.equal(requests[0].version, 'v1'); assert.equal(requests[0].sensor_id, 'station-1');
  assert.equal(view.log.children.length, 2);
});

test('Agent Flow focus actions release the sibling panel but filtering keeps chat open', async (t) => {
  const snapshot = publication(), selected = [], filters = [];
  const view = analystHarness(t, async () => ({ answer: 'Inspect the event.', version: 'v1', evidence_ids: [], actions: [
    { type: 'focus_incident', incident_id: 'flood' }, { type: 'filter_incidents', filters: { locality: 'Kennedy' } },
  ] }), { layer: { state: () => ({ snapshot, filters: {}, selectedId: null }),
    select(id) { assert.equal(view.panel.classList.contains('collapsed'), true); selected.push(id); },
    setFilters(value) { assert.equal(view.panel.classList.contains('collapsed'), false); filters.push(value); },
  } });
  await view.ask('What should I inspect?');
  const [focus, filter] = view.log.children[1].children.filter(item => item.tagName === 'BUTTON');
  filter.focus(); filter.dispatchEvent(new Event('click')); assert.deepEqual(filters, [{ locality: 'Kennedy' }]); assert.equal(document.activeElement, filter);
  focus.focus(); focus.dispatchEvent(new Event('click')); assert.deepEqual(selected, ['flood']); assert.equal(view.log.children.length, 2);
  assert.equal(document.activeElement, view.social.querySelector('.panel-collapse-btn'));
  snapshot.version = 'v2'; view.agentFlow.enable(); focus.focus(); focus.dispatchEvent(new Event('click'));
  assert.deepEqual(selected, ['flood']); assert.equal(view.panel.classList.contains('collapsed'), false); assert.equal(document.activeElement, focus);
  assert.match(view.status.textContent, /publication changed/);
});

test('Agent Flow fetches a fresh publication for each question while both layers remain off', async (t) => {
  const requests = [], state = { enabled: false, snapshot: { version: '', incidents: [], evidence: [] }, filters: { locality: 'Kennedy' }, selectedId: 'flood' };
  let version = 0;
  const view = analystHarness(t, async (path, options) => {
    requests.push({ path, body: options.body && JSON.parse(options.body) });
    if (path === '/api/prisma/snapshot') return publication(`v${++version}`);
    return { answer: 'Local evidence.', version: `v${version}`, session_id: 'same-session', runtime: 'local_fixture', evidence_ids: [], actions: [] };
  }, { layer: { state: () => state, update() { assert.fail('Chat must not toggle or refresh the hidden map layer'); } },
    sensorContext: () => ({ enabled: false, snapshot: publication('stale'), sensor_id: 'stale-sensor' }) });
  await view.ask('What is happening?');
  await view.ask('What changed in the latest publication?');
  assert.deepEqual(requests.map(item => item.path), ['/api/prisma/snapshot', '/api/prisma/chat', '/api/prisma/snapshot', '/api/prisma/chat']);
  assert.deepEqual(requests[1].body, { question: 'What is happening?', version: 'v1', filters: {} });
  assert.deepEqual(requests[3].body, { question: 'What changed in the latest publication?', version: 'v2', filters: {}, session_id: 'same-session' });
  assert.equal(state.enabled, false); assert.equal(state.snapshot.version, '');
  assert.match(view.log.children[1].children.find(item => item.tagName === 'SMALL').textContent, /Local fixture response · no model inference/);
  assert.doesNotMatch(view.log.children[1].children.find(item => item.tagName === 'SMALL').textContent, /Publication/);
});

for (const cancellation of ['button', 'voice']) test(`Agent Flow ${cancellation} cancellation stops an initial snapshot and rejects a late result`, async (t) => {
  const requests = [], turn = new AbortController(); let resolve;
  const view = analystHarness(t, (path, options) => { requests.push({ path, signal: options.signal }); return new Promise(done => { resolve = done; }); },
    { layer: { state: () => ({ enabled: false, snapshot: { version: '', incidents: [], evidence: [] }, filters: {}, selectedId: null }) },
      sensorContext: () => ({ enabled: false }) });
  const pending = view.ask('Ask without a visible layer.', { signal: turn.signal });
  await new Promise(setImmediate);
  assert.equal(view.form['aria-busy'], 'true');
  await assert.rejects(view.ask('Duplicate question'), /already processing/);
  if (cancellation === 'voice') turn.abort();
  else {
    const send = view.form.querySelector('button'); assert.equal(send.type, 'button'); assert.equal(send['aria-label'], 'Stop request');
    send.dispatchEvent(new Event('click', { cancelable: true }));
  }
  assert.equal(requests[0].signal.aborted, true); assert.equal(view.form['aria-busy'], 'false');
  assert.equal(view.form.querySelector('button')['aria-label'], 'Send question');
  resolve(publication());
  await assert.rejects(pending, { name: 'AbortError' });
  assert.deepEqual(requests.map(item => item.path), ['/api/prisma/snapshot']);
  assert.equal(view.count.textContent, '0 questions'); assert.equal(view.log.children.length, 1);
});

test('external voice cancellation aborts Agent Flow and ignores a late response', async (t) => {
  let resolve, requestSignal;
  const view = analystHarness(t, (_path, options) => { requestSignal = options.signal; return new Promise((done) => { resolve = done; }); });
  const turn = new AbortController();
  const pending = view.ask('Sensor status?', { signal: turn.signal });
  await new Promise(setImmediate); turn.abort(); assert.equal(requestSignal.aborted, true);
  resolve({ answer: 'Late response', version: 'v1', evidence_ids: [], actions: [] });
  await assert.rejects(pending, { name: 'AbortError' }); assert.equal(view.log.children.length, 1);
});

for (const enabled of [true, false]) test(`selected Sensors context excludes stale social filters and selections (Social enabled: ${enabled})`, async (t) => {
    const requests = [], sensorSnapshot = { ...publication('v2'), incidents: [], sensors: [{ id: 'reading-2', sensor_id: 'station-1' }] };
    const view = analystHarness(t, async (_path, options) => { requests.push(JSON.parse(options.body)); return {
      answer: 'Synthetic sensor reading.', version: 'v2', evidence_ids: [], sensor_evidence_ids: ['reading-2'], actions: [] }; },
    { layer: { state: () => ({ enabled, snapshot: publication(), filters: { locality: 'Kennedy' }, selectedId: 'flood' }) },
      sensorContext: () => ({ enabled: true, snapshot: sensorSnapshot, sensor_id: 'station-1' }), showSensor() {} });
    await view.ask('Compare the selected sensor with social reports.');
    assert.deepEqual(requests[0], { question: 'Compare the selected sensor with social reports.', version: 'v2', filters: {}, sensor_id: 'station-1' });
});

test('publication conflicts restore Send for retry, refresh both layers and exclude inactive Sensors', async (t) => {
  const refreshed = [], requests = [];
  const view = analystHarness(t, async (_path, options) => {
    requests.push(JSON.parse(options.body));
    if (requests.length === 1) throw Object.assign(new Error('Publication changed'), { status: 409 });
    return { answer: 'Current evidence.', version: 'v1', evidence_ids: [], actions: [] };
  },
    { layer: { state: () => ({ enabled: true, snapshot: publication(), filters: {}, selectedId: 'flood' }), update: async () => refreshed.push('social') },
      sensorContext: () => ({ enabled: false, snapshot: publication('v2'), sensor_id: 'station-1' }), refreshSensors: async () => refreshed.push('sensors') });
  await assert.rejects(view.ask('What changed?'), { status: 409 });
  assert.equal(requests[0].version, 'v1'); assert.equal(requests[0].sensor_id, undefined);
  assert.deepEqual(refreshed, ['social', 'sensors']); assert.match(view.status.textContent, /question is preserved/);
  const send = view.form.querySelector('button'), sendIcon = send.innerHTML;
  assert.equal(send.type, 'submit'); assert.equal(send['aria-label'], 'Send question'); assert.equal(view.form['aria-busy'], 'false');
  const retry = view.ask('What changed?'); assert.equal(send.type, 'button'); assert.equal(send['aria-label'], 'Stop request');
  assert.equal((await retry).answer, 'Current evidence.'); assert.equal(requests.length, 2);
  assert.equal(send.type, 'submit'); assert.equal(send['aria-label'], 'Send question'); assert.equal(send.innerHTML, sendIcon); assert.equal(view.form['aria-busy'], 'false');
});

test('managed provider labels distinguish configuration from a verified connection', () => {
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

test('OCI native configuration indicator stays configured when inference fails and never displays an OCID as its model name', () => {
  const saved = { configured: true, model_id: 'ocid1.generativeaimodel.example', model_name: 'Grok 4', model_vendor: 'xAI', last_test: { status: 'error', error: { code: 'oci_rate_limited', message: 'OCI rate limit reached.' } } };
  assert.deepEqual(ociProviderPresentation(saved), { set: 'true', label: 'Configured · server managed', model: 'Grok 4 · xAI' });
  assert.equal(ociProviderPresentation({ ...saved, configured: false }).set, 'false');
  assert.deepEqual(ociProviderPresentation({ ...saved, last_test: { status: 'success' } }), ociProviderPresentation(saved));
  assert.equal(ociProviderPresentation({ ...saved, model_name: undefined, model_vendor: undefined }).model, 'OCI conversational model');
  assert.equal(ociModelLabel(saved.model_id), 'OCI conversational model');
  assert.equal(ociProviderPresentation({ configured: false }).model, 'No model selected');
});

test('OCI status rows stay below native providers without configuration controls or write requests', async () => {
  class Element {
    constructor() { this.dataset = {}; this.nodes = new Map(); this.children = []; }
    setAttribute(name, value) { this[name] = value; }
    querySelector(selector) {
      if (selector === '.key-setup-external') selector = '[data-oci-configured]';
      if (!this.nodes.has(selector)) this.nodes.set(selector, new Element());
      return this.nodes.get(selector);
    }
    querySelectorAll() { return this.querySelector('[data-key-setup-rows]').children; }
    append(node) { node.parentNode = this; this.children.push(node); }
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this); this.parentNode = null; }
  }
  const saved = { document: globalThis.document, MutationObserver: globalThis.MutationObserver, localStorage: globalThis.localStorage };
  const dialog = new Element(), chip = new Element(), native = new Element(), requests = [], observers = [];
  const host = dialog.querySelector('[data-key-setup-rows]'); native.dataset.keyId = 'openai'; host.append(native);
  const lifetime = new AbortController();
  try {
    globalThis.document = { getElementById: (id) => id === 'key-setup' ? dialog : chip, createElement: () => new Element() };
    globalThis.MutationObserver = class { constructor(callback) { this.callback = callback; observers.push(this); } observe() {} disconnect() { this.disconnected = true; } };
    globalThis.localStorage = { getItem: () => 'oci', setItem() { assert.fail('Status display must not change the chosen provider'); } };
    await mountProviderSettings({ signal: lifetime.signal, request: async (path, options) => {
      requests.push({ path, method: options.method || 'GET' });
      return { can_configure: true, configured: true, region: 'us-chicago-1', model_id: 'saved-model', model_name: 'Friendly model', model_vendor: 'Vendor',
        tts_model: 'xai.grok-tts', voice: 'ara', last_test: { status: 'error', error: { message: 'OCI blocked the model response; no answer was returned' } } };
    } });
    assert.deepEqual(requests, [{ path: '/api/prisma/oci-provider', method: 'GET' }, { path: '/api/prisma/oci-voice', method: 'GET' }]);
    const links = dialog.querySelector('.key-setup-footer').children;
    assert.equal(links.length, 1); assert.equal(links[0].href, '/admin/gods-eye-view#parameters');
    assert.equal(links[0].target, '_blank'); assert.equal(links[0].rel, 'noopener');
    assert.deepEqual(host.children.map((row) => row.dataset.keyId), ['openai', 'oci', 'oci-voice']);
    const rows = host.children.slice(1);
    for (const row of rows) {
      assert.equal(row.dataset.set, 'true');
      assert.match(row.querySelector('[data-oci-model]').textContent, /Friendly model · Vendor/);
      assert.doesNotMatch(row.innerHTML, /data-oci-status|inference/i);
      assert.equal((row.innerHTML.match(/<p\b/g) || []).length, 1);
      assert.doesNotMatch(row.innerHTML, /<(?:input|select|button|form)\b/);
      row.parentNode = null;
    }
    host.children = [native]; observers[0].callback();
    assert.deepEqual(host.children, [native, ...rows]);
    lifetime.abort(); assert.equal(observers[0].disconnected, true); assert.deepEqual(host.children, [native]);
    assert.equal(dialog.querySelector('.key-setup-footer').children.length, 0);
    const failedLifetime = new AbortController();
    try {
      await mountProviderSettings({ signal: failedLifetime.signal, request: async () => { throw new Error('Configuration service unavailable'); } });
      observers[1].callback();
      for (const row of host.children.slice(1)) {
        assert.equal(row.querySelector('[data-oci-configured]').textContent, 'Configuration unavailable');
        assert.equal(row.querySelector('[data-oci-model]').textContent, 'Configuration service unavailable');
      }
    } finally { failedLifetime.abort(); }
  } finally {
    lifetime.abort();
    for (const [key, value] of Object.entries(saved)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; }
  }
});
