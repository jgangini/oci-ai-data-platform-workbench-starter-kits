import test from 'node:test';
import assert from 'node:assert/strict';
import { createSensorsLayer, mountSensorsPanel, validateSensors } from '../native/sensorsLayer.js';
import { claimPointer, releasePointer } from '../.upstream/src/data/inputOwnership.js';
import CesiumEvent from '../.upstream/node_modules/@cesium/engine/Source/Core/Event.js';
import * as NativeCesium from '../.upstream/node_modules/cesium/Source/Cesium.js';

const reading = (n = 1, values = {}) => ({ id: `event-${n}`, sensor_id: `sensor-${n}`, sensor_type: 'rainfall', observed_at: '2026-10-03T12:00:00Z', lat: 4.65, lon: -74.1,
  department: 'Bogotá', municipality: 'Bogotá', value: 12.3, unit: 'mm', status: 'normal', mode: 'Synthetic', is_simulated: true, ...values });
const publication = (version = 'v1', sensors = [reading()]) => ({ version, sensors });
function harness(request) {
  let click; const requests = [], sources = [];
  const Cesium = {
    ...NativeCesium,
    ScreenSpaceEventHandler: class { setInputAction(handler) { click = handler; } destroy() { click = null; } },
  };
  const viewer = { scene: { canvas: { clientHeight: 1000 }, preUpdate: new CesiumEvent(), postRender: new CesiumEvent(),
    globe: { getHeight: () => 2500, show: true, tilesLoaded: true, tileLoadProgressEvent: new CesiumEvent() },
    sampleHeightSupported: true, sampleHeight: () => viewer.scene.globe.getHeight(),
    requestRender() { this.renderCount = (this.renderCount || 0) + 1; }, pick: () => ({ id: { id: 'sensors:sensor-1' } }) },
    camera: { positionWC: Cesium.Cartesian3.fromDegrees(-74.1, 4.65, 12500), frustum: { fov: Math.PI / 3 }, moveEnd: new CesiumEvent(), flyTo(value) { this.destination = value.destination; } },
    dataSources: { add(source) { sources.push(source); }, remove(source) { sources.splice(sources.indexOf(source), 1); } } };
  const lifetime = new AbortController();
  const layer = createSensorsLayer({ Cesium, request: request ?? ((path, options) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject }))), signal: lifetime.signal });
  return { layer, viewer, requests, sources, lifetime, click: () => click?.({ position: {} }) };
}

class Element extends EventTarget {
  constructor(tag = 'div') {
    super(); this.tagName = tag.toUpperCase(); this.nodes = new Map(); this.children = []; this.dataset = {}; this.style = {}; this.value = ''; this.className = '';
    this.classList = { contains: name => this.className.split(' ').includes(name), add: name => { this.className += ` ${name}`; } };
    this.elements = new Proxy({}, { get: (_, name) => this.querySelector(`[name="${name}"]`) });
  }
  querySelectorAll(selector) {
    const named = /^\[name="([^"]+)"\]$/.exec(selector);
    return [...this.children, ...this.nodes.values()].flatMap(node => {
      const matches = named ? node.name === named[1] : selector.startsWith('.') ? node.classList.contains(selector.slice(1)) : node.tagName === selector.toUpperCase();
      return [...(matches ? [node] : []), ...node.querySelectorAll(selector)];
    });
  }
  querySelector(selector) { if (this.nodes.has(selector)) return this.nodes.get(selector); const found = this.querySelectorAll(selector)[0]; if (found) return found; this.nodes.set(selector, new Element()); return this.nodes.get(selector); }
  setAttribute(name, value) { this[name] = value; }
  append(...items) { for (const item of items) { this.children.push(item); item.parentNode = this; } }
  replaceChildren(...items) { if (this.contains(document.activeElement)) document.activeElement = document.body; this.children = []; this.append(...items); }
  contains(element) { return element === this || this.children.some(child => child.contains(element)); }
  after(element) { this.afterElement = element; }
  focus() { if (!this.disabled) document.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() { this.removed = true; if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); }
}

function installDocument(t) {
  const prior = globalThis.document, host = new Element();
  globalThis.document = { body: new Element(), createElement: tag => new Element(tag), getElementById: () => host };
  t.after(() => { if (prior === undefined) delete globalThis.document; else globalThis.document = prior; });
}

async function panelHarness(t, sensors = [reading(), reading(2)]) {
  installDocument(t);
  const data = { ...publication('v1', sensors), can_review: true }, mutations = [], h = harness(async () => structuredClone(data));
  const { panel } = mountSensorsPanel({ layer: h.layer, request: (path, options) => new Promise((resolve, reject) => mutations.push({ path, ...options, resolve, reject })), signal: h.lifetime.signal,
    setPanelCollapsed: (_id, value) => { panel.className = value ? 'collapsed' : ''; } });
  h.layer.init(h.viewer); h.layer.enable(); await h.layer.update(); h.layer.select(sensors[0].id);
  t.after(() => { h.lifetime.abort(); h.layer.destroy(); });
  const form = panel.querySelector('.sensors-detail').querySelector('form'), dialog = () => document.body.querySelectorAll('dialog')[0];
  return { ...h, panel, data, mutations, form, dialog,
    coordinate: name => form.querySelector(`[name="${name}"]`), save: () => form.querySelector('button'), feedback: () => form.querySelector('.tc-review-status'),
    submit: () => form.dispatchEvent(new Event('submit', { cancelable: true })),
    edit(lat, lon) { this.coordinate('lat').value = String(lat); this.coordinate('lon').value = String(lon); form.dispatchEvent(new Event('input')); },
    dialogButton: text => dialog()?.querySelectorAll('button').find(button => button.textContent === text), settle: () => new Promise(setImmediate),
  };
}

test('sensor boundary accepts latest Synthetic readings and rejects malformed or duplicate measurements', () => {
  assert.equal(validateSensors(publication()).length, 1);
  assert.deepEqual(validateSensors({ version: 'legacy' }), []);
  for (const values of [{ value: Infinity }, { lat: 91 }, { lon: '4' }, { observed_at: 'bad' }, { mode: 'real' }, { is_simulated: false }, { sensor_type: 'unknown' }, { sensor_type: '__proto__' }, { status: 'unknown' }, { status: 'constructor' }, { id: '' }]) assert.throws(() => validateSensors(publication('bad', [reading(1, values)])));
  assert.throws(() => validateSensors(publication('duplicate', [reading(), reading(2, { sensor_id: 'sensor-1' })])));
  assert.equal(validateSensors(publication('maximum', Array.from({ length: 5000 }, (_, i) => reading(i)))).length, 5000);
  assert.throws(() => validateSensors(publication('too-large', Array.from({ length: 5001 }, (_, i) => reading(i)))));
});

test('native sensor layer handles mount-before-init, selection, filtering, newer readings and lifecycle without mutating data', async () => {
  const h = harness(); const seen = []; const unsubscribe = h.layer.subscribe(state => seen.push(state));
  h.layer.enable(); h.layer.init(h.viewer); assert.equal(h.sources[0].show, true);
  const first = h.layer.update(); h.requests[0].resolve(publication('v1', [reading(), reading(2, { sensor_type: 'temperature', municipality: 'Cali', department: 'Valle del Cauca', value: 25, unit: '°C' })])); await first;
  assert.equal(h.requests[0].path, '/api/prisma/snapshot'); assert.equal(h.sources[0].entities.values.length, 2);
  const lease = claimPointer('other-layer'); h.click(); assert.equal(h.layer.state().selectedId, undefined); releasePointer(lease);
  h.click(); assert.equal(h.layer.state().selectedId, 'event-1'); assert.equal(h.sources[0].entities.getById('sensors:sensor-1').point.pixelSize.getValue(), 15);
  const renderCount = h.viewer.scene.renderCount; h.viewer.scene.postRender.raiseEvent(); assert.equal(h.viewer.scene.renderCount, renderCount + 1); assert.equal(h.viewer.scene.postRender.numberOfListeners, 0);
  h.layer.select('event-2'); assert.ok(Math.abs(NativeCesium.Cartographic.fromCartesian(h.viewer.camera.destination).height - 12000) < 0.001);
  h.layer.setFilters({ q: 'cali', sensor_type: 'temperature', department: 'Valle del Cauca' }); assert.equal(h.layer.state().items.length, 1);
  const next = h.layer.update(); h.requests[1].resolve(publication('v2', [reading(2, { id: 'new-event', sensor_type: 'temperature', municipality: 'Cali', department: 'Valle del Cauca', value: 26, unit: '°C' })])); await next;
  assert.equal(h.layer.state().selectedId, 'new-event'); assert.equal(h.layer.state().items[0].value, 26);
  assert.equal(h.layer.select('missing'), false);
  h.layer.disable(); assert.equal(h.sources[0].show, false); assert.equal(await h.layer.update(), false); assert.equal(h.requests.length, 2);
  h.layer.destroy(); unsubscribe(); assert.equal(h.sources.length, 0); assert.equal(seen.at(-1).enabled, false);
  assert.ok(h.requests.every(request => !request.options.method), 'Layer reads publications only');
});

test('sensor stems follow Data Centers scaling, terrain, colors, visibility and lifecycle', async () => {
  const C = NativeCesium, h = harness(async () => publication('v1', ['normal', 'warning', 'critical'].map((status, i) => reading(i + 1, { status }))));
  h.layer.init(h.viewer); h.layer.enable(); await h.layer.update();
  const entities = h.sources[0].entities.values, entity = entities[0];
  const positions = () => entity.polyline.positions.getValue();
  const height = position => C.Cartographic.fromCartesian(position).height;
  for (const distance of [500, 10000, 3400000]) {
    h.viewer.camera.positionWC = C.Cartesian3.fromDegrees(-74.1, 4.65, 2500 + distance);
    h.viewer.camera.moveEnd.raiseEvent(); h.viewer.scene.preUpdate.raiseEvent();
    const [base, tip] = positions();
    assert.ok(Math.abs(height(base) - 2500) < 0.001, 'Stem begins on the terrain');
    assert.ok(Math.abs(C.Cartesian3.distance(base, tip) - distance * 2 * Math.tan(Math.PI / 6) * 65 / 1000) < 0.001, 'Same 65px scaling at street and aerial distances');
    assert.ok(C.Cartesian3.equalsEpsilon(entity.position.getValue(), tip, 1e-10), 'Point remains at the line tip');
    assert.notEqual(entity.point.heightReference?.getValue(), C.HeightReference.CLAMP_TO_GROUND);
    assert.equal(entity.polyline.width.getValue(), 3.5);
  }
  for (const [i, color] of ['#4ade80', '#fb923c', '#f87171'].entries()) {
    assert.ok(C.Color.equals(entities[i].point.color.getValue(), C.Color.fromCssColorString(color)));
    assert.ok(C.Color.equals(entities[i].polyline.material.color.getValue(), C.Color.fromCssColorString(color)));
  }
  h.viewer.camera.positionWC = C.Cartesian3.fromDegrees(-74.1, 4.65, 12500);
  h.viewer.scene.globe.getHeight = () => 2800;
  h.viewer.scene.globe.tileLoadProgressEvent.raiseEvent(0); h.viewer.scene.preUpdate.raiseEvent();
  assert.ok(Math.abs(height(positions()[0]) - 2800) < 0.001, 'A parked camera follows newly loaded terrain');
  h.layer.disable(); const tipBefore = C.Cartesian3.clone(entity.position.getValue());
  h.viewer.camera.positionWC = C.Cartesian3.fromDegrees(-74.1, 4.65, 4000);
  h.viewer.camera.moveEnd.raiseEvent(); h.viewer.scene.preUpdate.raiseEvent();
  assert.ok(C.Cartesian3.equals(entity.position.getValue(), tipBefore), 'Hidden layer does no geometry work');
  h.layer.enable(); assert.ok(!C.Cartesian3.equals(entity.position.getValue(), tipBefore), 'Re-enable catches up with the camera');
  h.viewer.camera.positionWC = C.Cartesian3.fromDegrees(105.9, -4.65, 10000);
  h.viewer.camera.moveEnd.raiseEvent(); h.viewer.scene.preUpdate.raiseEvent();
  assert.equal(entity.show, false, 'Far-side sensors do not show through Earth');
  h.layer.destroy();
  for (const event of [h.viewer.scene.preUpdate, h.viewer.scene.postRender, h.viewer.camera.moveEnd, h.viewer.scene.globe.tileLoadProgressEvent]) assert.equal(event.numberOfListeners, 0);
});

test('sensor stems ground after tile loading without camera motion, including scenes without depth sampling', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: 10000 });
  for (const supported of [true, false]) {
    const h = harness(async () => publication());
    h.viewer.scene.sampleHeightSupported = supported; h.viewer.scene.globe.tilesLoaded = false; h.viewer.scene.globe.getHeight = () => undefined;
    h.layer.init(h.viewer); h.layer.enable(); await h.layer.update();
    const entity = h.sources[0].entities.values[0];
    let terrainReads = 0;
    h.viewer.scene.globe.getHeight = () => { terrainReads++; return 2800; }; h.viewer.scene.globe.tilesLoaded = true;
    h.viewer.scene.sampleHeight = () => 2850;
    t.mock.timers.tick(100); h.viewer.scene.globe.tileLoadProgressEvent.raiseEvent(0); h.viewer.scene.preUpdate.raiseEvent();
    const base = entity.polyline.positions.getValue()[0];
    assert.ok(Math.abs(NativeCesium.Cartographic.fromCartesian(base).height - (supported ? 2850 : 2800)) < 0.001, 'Ground immediately on the final tile event, before the two-second sample cooldown');
    const settledReads = terrainReads;
    for (let i = 0; i < 5; i++) { t.mock.timers.tick(2100); h.viewer.scene.preUpdate.raiseEvent(); }
    assert.equal(terrainReads, settledReads, 'Stationary sensors do not schedule unavailable depth sampling or repeat completed samples');
    h.layer.destroy();
  }
});

test('4000 sensors bound visible geometry and terrain work across zoom, selection, tile bursts and camera rotation', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: 10000 });
  const C = NativeCesium, readings = Array.from({ length: 4000 }, (_, index) => reading(index));
  const h = harness(async () => publication('dense', readings)); t.after(() => h.layer.destroy());
  const camera = h.viewer.camera, scene = h.viewer.scene;
  camera.frustum = new C.PerspectiveFrustum({ fov: Math.PI / 3, aspectRatio: 1.5, near: 1, far: 20000000 });
  const surface = C.Cartesian3.fromDegrees(-74.1, 4.65, 2500);
  const frame = C.Transforms.eastNorthUpToFixedFrame(surface);
  camera.upWC = C.Matrix4.multiplyByPointAsVector(frame, C.Cartesian3.UNIT_Y, new C.Cartesian3());
  const setHeight = height => {
    camera.positionWC = C.Cartesian3.fromDegrees(-74.1, 4.65, height);
    camera.directionWC = C.Cartesian3.normalize(C.Cartesian3.subtract(surface, camera.positionWC, new C.Cartesian3()), new C.Cartesian3());
  };
  let samples = 0, terrainReads = 0, grounded = false, changes = 0;
  scene.globe.getHeight = () => { terrainReads++; return 2500; };
  scene.sampleHeight = () => { samples++; return grounded ? 2850 : undefined; };
  setHeight(4000000); h.layer.init(h.viewer); h.layer.enable(); await h.layer.update();
  const entities = h.sources[0].entities, visible = () => entities.values.filter(entity => entity.show);
  assert.equal(h.layer.state().items.length, 4000, 'LOD never removes readings from filtering and selection');
  assert.equal(visible().length, 80); assert.equal(samples, 0, 'A globe view never performs nearby depth reads');
  for (const entity of entities.values) entity.polyline.positions.definitionChanged.addEventListener(() => { changes++; });
  const budgets = [visible().length];
  for (const [height, budget] of [[500000, 200], [12500, 420]]) {
    const before = changes;
    setHeight(height); t.mock.timers.tick(500); scene.preUpdate.raiseEvent();
    budgets.push(visible().length);
    assert.equal(visible().length, budget);
    assert.ok(changes - before > 0 && changes - before <= budget, 'Camera motion rebuilds only the bounded active geometry');
  }
  // Before LOD, the same dense dataset made 4000 depth calls at once and another 4000 on a +100ms tile pulse.
  assert.equal(samples, 420, 'The regional view spends the native 420-stem budget, not 4000 samples');
  const unshown = readings.find(item => !entities.getById(`sensors:${item.sensor_id}`).show);
  assert.ok(unshown); assert.equal(h.layer.select(unshown.id, false), true);
  const selected = entities.getById(`sensors:${unshown.sensor_id}`);
  assert.equal(selected.show, true, 'A selected sensor outside the prior budget is immediately admitted');
  assert.equal(selected.point.pixelSize.getValue(), 15); assert.equal(visible().length, 420);
  assert.equal(selected.polyline.arcType.getValue(), C.ArcType.NONE);
  const beforeBurst = samples;
  for (const remaining of [400, 300, 200, 50, 1]) {
    t.mock.timers.tick(20); scene.globe.tileLoadProgressEvent.raiseEvent(remaining); scene.preUpdate.raiseEvent();
  }
  assert.equal(samples, beforeBurst, 'Intermediate tile events do not reset the sampling cooldown');
  grounded = true; scene.globe.tileLoadProgressEvent.raiseEvent(0); scene.preUpdate.raiseEvent();
  assert.equal(samples - beforeBurst, 420, 'Final tile load retries only the active budget');
  assert.ok(Math.abs(C.Cartographic.fromCartesian(selected.polyline.positions.getValue()[0]).height - 2850) < .001);
  const completed = { samples, terrainReads, changes };
  for (let i = 0; i < 5; i++) { t.mock.timers.tick(20); scene.globe.tileLoadProgressEvent.raiseEvent(0); scene.preUpdate.raiseEvent(); }
  assert.deepEqual({ samples, terrainReads, changes }, completed, 'Duplicate completion events cannot rearm terrain or geometry work');
  const position = C.Cartesian3.clone(camera.positionWC), direction = C.Cartesian3.clone(camera.directionWC);
  camera.directionWC = C.Cartesian3.negate(direction, new C.Cartesian3());
  t.mock.timers.tick(500); scene.preUpdate.raiseEvent();
  assert.ok(C.Cartesian3.equals(camera.positionWC, position)); assert.equal(visible().length, 0, 'Rotating in place culls sensors behind the view');
  camera.directionWC = direction; t.mock.timers.tick(500); scene.preUpdate.raiseEvent();
  assert.equal(visible().length, 420); assert.equal(selected.show, true);
  const parked = { samples, changes };
  for (let i = 0; i < 10; i++) { t.mock.timers.tick(500); scene.preUpdate.raiseEvent(); }
  assert.deepEqual({ samples, changes }, parked, 'Grounded parked sensors spend no further sampling or geometry work');
  t.diagnostic(`4000-record regression: active budgets=${budgets.join('/')}; initial regional samples=420; intermediate tile burst samples=0; final tile samples=${samples - beforeBurst}`);
  const regional = visible();
  setHeight(4000000); t.mock.timers.tick(500); scene.preUpdate.raiseEvent();
  const filtered = regional.find(entity => !entity.show);
  assert.ok(filtered, 'Zooming out hides a previously grounded and rendered sensor');
  h.layer.setFilters({ q: `${filtered.id.slice('sensors:'.length)} Bogotá` });
  assert.equal(h.layer.state().items.length, 1); assert.equal(entities.values.length, 1);
  assert.equal(visible()[0], filtered, 'Filtering immediately readmits the retained sensor without moving the camera');
  const [base, tip] = filtered.polyline.positions.getValue();
  const expectedLength = C.Cartesian3.distance(camera.positionWC, base) * 2 * Math.tan(camera.frustum.fov / 2) * 65 / scene.canvas.clientHeight;
  assert.ok(Math.abs(C.Cartesian3.distance(base, tip) - expectedLength) < .001, 'Readmitted geometry uses the current 65px scale, not its stale close-up length');
  assert.ok(C.Cartesian3.equals(filtered.position.getValue(), tip));
  h.layer.setFilters({});
  assert.equal(h.layer.state().items.length, 4000); assert.equal(entities.values.length, 4000);
  assert.equal(visible().length, 80, 'Clearing the filter restores the full dataset within the globe budget');
  assert.equal(entities.getById(filtered.id), filtered, 'The retained station keeps its entity identity');
});

test('sensor frustum visibility follows width-only resizing without camera motion', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: 10000 });
  const C = NativeCesium, h = harness(async () => publication('resize', [reading(), reading(2, { lon: -74.07 })]));
  t.after(() => h.layer.destroy());
  const camera = h.viewer.camera, scene = h.viewer.scene, surface = C.Cartesian3.fromDegrees(-74.1, 4.65, 2500);
  camera.frustum = new C.PerspectiveFrustum({ fov: Math.PI / 3, aspectRatio: .3, near: 1, far: 20000000 });
  camera.directionWC = C.Cartesian3.normalize(C.Cartesian3.subtract(surface, camera.positionWC, new C.Cartesian3()), new C.Cartesian3());
  camera.upWC = C.Matrix4.multiplyByPointAsVector(C.Transforms.eastNorthUpToFixedFrame(surface), C.Cartesian3.UNIT_Y, new C.Cartesian3());
  scene.canvas.clientWidth = 300;
  h.layer.init(h.viewer); h.layer.enable(); await h.layer.update();
  const center = h.sources[0].entities.getById('sensors:sensor-1'), edge = h.sources[0].entities.getById('sensors:sensor-2');
  assert.equal(center.show, true); assert.equal(edge.show, false);
  const position = C.Cartesian3.clone(camera.positionWC), direction = C.Cartesian3.clone(camera.directionWC), up = C.Cartesian3.clone(camera.upWC);
  scene.canvas.clientWidth = 1500; camera.frustum.aspectRatio = 1.5;
  t.mock.timers.tick(500); scene.preUpdate.raiseEvent();
  assert.equal(edge.show, true, 'The wider viewport admits the sensor without a moveEnd or height change');
  scene.canvas.clientWidth = 300; camera.frustum.aspectRatio = .3;
  t.mock.timers.tick(500); scene.preUpdate.raiseEvent();
  assert.equal(edge.show, false); assert.equal(center.show, true);
  assert.equal(scene.canvas.clientHeight, 1000); assert.equal(camera.frustum.fov, Math.PI / 3);
  assert.ok(C.Cartesian3.equals(camera.positionWC, position) && C.Cartesian3.equals(camera.directionWC, direction) && C.Cartesian3.equals(camera.upWC, up));
});

test('sensor refresh retains all 5000 entities for unchanged publications and updates readings by station identity', async t => {
  let data = publication('first', Array.from({ length: 5000 }, (_, index) => reading(index)));
  const h = harness(async () => structuredClone(data)); t.after(() => h.layer.destroy());
  h.layer.init(h.viewer); h.layer.enable(); await h.layer.update();
  assert.equal(h.layer.updateInterval, 30000);
  h.layer.select('event-1', false); h.viewer.scene.postRender.raiseEvent();
  const entities = h.sources[0].entities, original = [...entities.values], rows = h.layer.state().items;
  let added = 0, removed = 0, geometryChanges = 0;
  const geometryChangedIds = new Set();
  entities.collectionChanged.addEventListener((_source, additions, removals) => { added += additions.length; removed += removals.length; });
  for (const entity of original) entity.polyline.positions.definitionChanged.addEventListener(() => { geometryChanges++; geometryChangedIds.add(entity.id); });
  const renders = h.viewer.scene.renderCount;
  data.version = 'social-only'; data.can_review = true; await h.layer.update();
  assert.equal(h.layer.state().snapshot.version, 'social-only', 'Chat receives the current global publication version');
  assert.equal(h.layer.state().snapshot.can_review, true); assert.equal(h.layer.state().items, rows);
  assert.equal(h.viewer.scene.renderCount, renders, 'Unchanged sensors do not enqueue a new scene render');
  assert.equal(h.viewer.scene.postRender.numberOfListeners, 0);
  data.sensors = data.sensors.map(item => ({ ...item, id: `new-${item.id}`, observed_at: '2026-10-03T12:05:00Z', value: item.value + 1 }));
  data.sensors[1].status = 'critical'; await h.layer.update();
  assert.equal(h.layer.state().selectedId, 'new-event-1'); assert.equal(original[1].point.pixelSize.getValue(), 15);
  assert.equal(original[1].point.color.getValue().toCssHexString(), '#f87171');
  assert.equal(original[1].polyline.material.color.getValue().toCssHexString(), '#f87171');
  assert.equal(geometryChanges, 0, 'A new measurement does not rebuild terrain or stem positions');
  assert.equal(added, 0); assert.equal(removed, 0);
  for (const entity of original) assert.equal(entities.getById(entity.id), entity);
  h.layer.select('new-event-2', false); h.click(); assert.equal(h.layer.state().selectedId, 'new-event-1', 'Picking a retained entity selects its current reading');
  const previousVisible = new Set(entities.values.filter(entity => entity.show).map(entity => entity.id));
  geometryChanges = 0; geometryChangedIds.clear();
  data.sensors[1].lat = 4.66; data.sensors[1].lon = -74.12; data.sensors.splice(3, 1); data.sensors.push(reading(5000));
  await h.layer.update();
  assert.equal(added, 1); assert.equal(removed, 1); assert.ok(geometryChangedIds.has(original[1].id));
  assert.equal(geometryChanges, geometryChangedIds.size, 'Each affected retained geometry updates once');
  for (const id of geometryChangedIds) assert.ok(id === original[1].id || (!previousVisible.has(id) && entities.getById(id)?.show), 'Only corrected or newly admitted LOD stems rebuild');
  assert.equal(entities.getById(original[1].id), original[1], 'A location correction moves the same station entity');
  assert.equal(entities.getById(original[2].id), original[2]); assert.equal(entities.getById(original[3].id), undefined);
  const corrected = NativeCesium.Cartographic.fromCartesian(original[1].polyline.positions.getValue()[0]);
  assert.ok(Math.abs(NativeCesium.Math.toDegrees(corrected.latitude) - 4.66) < 1e-6);
  h.layer.setFilters({ q: 'sensor-4999' }); assert.equal(entities.values.length, 1); assert.equal(entities.values[0], original[4999]);
});

test('parked sensors skip terrain work while motion and late depth samples still update stems', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: 10000 });
  const h = harness(async () => publication()); t.after(() => h.layer.destroy());
  let heights = 0; h.viewer.scene.globe.getHeight = () => { heights++; return 2500; };
  h.layer.init(h.viewer); h.layer.enable(); await h.layer.update();
  const entity = h.sources[0].entities.values[0], firstHeights = heights, initialTip = NativeCesium.Cartesian3.clone(entity.position.getValue());
  for (let i = 0; i < 20; i++) { t.mock.timers.tick(500); h.viewer.scene.preUpdate.raiseEvent(); }
  assert.equal(heights, firstHeights, 'Grounded sensors do no stationary terrain resampling');
  h.viewer.camera.positionWC = NativeCesium.Cartesian3.fromDegrees(-74.1, 4.65, 15000);
  t.mock.timers.tick(500); h.viewer.scene.preUpdate.raiseEvent();
  assert.ok(!NativeCesium.Cartesian3.equals(entity.position.getValue(), initialTip), 'Motion without moveEnd updates geometry');
  const late = harness(async () => publication()); t.after(() => late.layer.destroy());
  late.viewer.scene.sampleHeight = () => undefined; late.layer.init(late.viewer); late.layer.enable(); await late.layer.update();
  late.viewer.scene.sampleHeight = () => 2850;
  t.mock.timers.tick(2100); late.viewer.scene.preUpdate.raiseEvent();
  assert.ok(Math.abs(NativeCesium.Cartographic.fromCartesian(late.sources[0].entities.values[0].polyline.positions.getValue()[0]).height - 2850) < 0.001, 'A failed depth sample retries even without tile or camera events');
});

test('stationary failed depth samples give up until camera, tiles or coordinates change', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: 10000 });
  const data = publication(), h = harness(async () => structuredClone(data)); t.after(() => h.layer.destroy());
  let samples = 0; h.viewer.scene.sampleHeight = () => { samples++; return undefined; };
  h.layer.init(h.viewer); h.layer.enable(); await h.layer.update();
  for (let i = 0; i < 40; i++) { t.mock.timers.tick(2100); h.viewer.scene.preUpdate.raiseEvent(); }
  const exhausted = samples;
  assert.ok(exhausted > 1 && exhausted <= 31, 'Initial sample plus at most the native 30 stationary retries');
  data.version = 'next'; data.sensors[0].value++; await h.layer.update();
  for (let i = 0; i < 10; i++) { t.mock.timers.tick(2100); h.viewer.scene.preUpdate.raiseEvent(); }
  assert.equal(samples, exhausted, 'Reading refresh does not rearm an exhausted terrain sampler');
  h.viewer.camera.positionWC = NativeCesium.Cartesian3.fromDegrees(-74.1, 4.65, 15000);
  t.mock.timers.tick(500); h.viewer.scene.preUpdate.raiseEvent(); assert.equal(samples, exhausted + 1);
  t.mock.timers.tick(100); h.viewer.scene.globe.tileLoadProgressEvent.raiseEvent(0); h.viewer.scene.preUpdate.raiseEvent();
  assert.equal(samples, exhausted + 2, 'New tile progress permits an immediate retry');
  data.sensors[0].lat = 4.66; await h.layer.update(); assert.equal(samples, exhausted + 3, 'Corrected coordinates get a fresh sampling budget');
});

test('late sensor replies cannot replace newer data or resurrect a disabled layer; errors retain the last publication', async () => {
  const h = harness(); h.layer.init(h.viewer); h.layer.enable();
  const old = h.layer.update(), latest = h.layer.update(); assert.equal(h.requests[0].options.signal.aborted, true);
  h.requests[1].resolve(publication('new')); await latest; h.requests[0].resolve(publication('old')); await old;
  assert.equal(h.layer.state().snapshot.version, 'new');
  const failed = h.layer.update(); h.requests[2].reject(new Error('Snapshot unavailable')); await failed;
  assert.equal(h.layer.state().snapshot.version, 'new'); assert.equal(h.layer.getStats().lastError, 'Snapshot unavailable');
  const disabled = h.layer.update(); h.layer.disable(); h.requests[3].resolve(publication('ignored')); await disabled;
  assert.equal(h.layer.state().snapshot.version, 'new'); assert.equal(h.sources[0].show, false);
  h.layer.enable(); const aborted = h.layer.update(); h.lifetime.abort(); h.requests[4].resolve(publication('ignored2')); await aborted;
  assert.equal(h.layer.state().snapshot.version, 'new'); h.layer.destroy();
});

test('sensor panel keeps keyboard focus and collapsed state across readings, and shows an active unavailable department', async t => {
  installDocument(t);
  const h = harness(); let collapsed = true;
  const { panel } = mountSensorsPanel({ layer: h.layer, signal: h.lifetime.signal, setPanelCollapsed: (_id, value) => { collapsed = value; panel.className = value ? 'collapsed' : ''; } });
  h.layer.init(h.viewer); h.layer.enable();
  const first = h.layer.update(); h.requests[0].resolve(publication('v1', [reading(), reading(2, { department: 'Valle del Cauca' })])); await first;
  h.layer.select('event-2'); const list = panel.querySelector('.sensors-list'); list.children[1].focus();
  panel.querySelector('.panel-collapse-btn').dispatchEvent(new Event('click')); assert.equal(collapsed, true);
  const previousRow = list.children[1]; list.scrollTop = 123;
  const unchanged = h.layer.update(); assert.equal(list.children[1], previousRow, 'Starting a background read keeps rows mounted');
  assert.equal(panel.querySelector('[data-status]').textContent, '', 'Background loading does not move the controls');
  h.requests[1].resolve(publication('social-change', [reading(), reading(2, { department: 'Valle del Cauca' })])); await unchanged;
  assert.equal(list.children[1], previousRow); assert.equal(document.activeElement, previousRow); assert.equal(list.scrollTop, 123);
  const next = h.layer.update(); h.requests[2].resolve(publication('v2', [reading(), reading(2, { id: 'latest-2', department: 'Valle del Cauca' })])); await next;
  assert.equal(collapsed, true, 'A new reading of the same station must not reopen the panel');
  assert.equal(document.activeElement, list.children[1], 'Focus follows the station, not its superseded reading id');
  assert.equal(h.layer.state().selectedId, 'latest-2');
  h.layer.setFilters({ department: 'Valle del Cauca' });
  assert.equal(list.scrollTop, 123, 'New readings preserve the list scroll position');
  const disappeared = h.layer.update(); h.requests[3].resolve(publication('v3')); await disappeared;
  const department = panel.querySelector('form').elements.department;
  assert.equal(h.layer.state().items.length, 0); assert.equal(department.value, 'Valle del Cauca');
  assert.ok(department.children.some(option => option.value === 'Valle del Cauca'), 'Keep the active empty filter visible');
  assert.equal(collapsed, true); h.lifetime.abort(); assert.equal(panel.removed, true); h.layer.destroy();
});

test('sensor Filter submits drafts explicitly, numbers continue across pages, and statuses have matching map colors', async t => {
  const view = await panelHarness(t, Array.from({ length: 55 }, (_, index) => reading(index, { status: ['normal', 'warning', 'critical'][index % 3] })));
  assert.equal(view.layer.source, 'OCI AIDP');
  assert.doesNotMatch(view.panel.innerHTML, /Synthetic|sensors-provenance|data-count/);
  const list = view.panel.querySelector('.sensors-list'), filters = view.panel.querySelector('form');
  assert.equal(list.children.length, 50); assert.match(list.children[0].querySelector('.scene-shot-label').textContent, /^#0001 sensor-0/);
  assert.match(list.children[49].querySelector('.scene-shot-label').textContent, /^#0050 sensor-49/);
  assert.deepEqual(view.sources[0].entities.values.slice(0, 3).map(row => row.point.color.getValue().toCssHexString()), ['#4ade80', '#fb923c', '#f87171']);
  assert.deepEqual(list.children.slice(0, 3).map(row => row.querySelector('.sensor-status').dataset.status), ['normal', 'warning', 'critical']);
  const metadata = list.children[0].querySelector('.scene-shot-meta');
  assert.equal(metadata.textContent, 'Rainfall · '); assert.equal(metadata.children[0].textContent, 'normal'); assert.equal(metadata.children[1].textContent, ' · 12.3 mm');
  view.panel.querySelector('[data-next]').dispatchEvent(new Event('click')); assert.match(list.children[0].querySelector('.scene-shot-label').textContent, /^#0051 sensor-50/);
  filters.elements.q.value = 'sensor-54'; filters.elements.q.dispatchEvent(new Event('input')); filters.dispatchEvent(new Event('change'));
  assert.equal(view.layer.state().items.length, 55, 'Typing does not repaint the map before Filter');
  filters.dispatchEvent(new Event('submit', { cancelable: true }));
  assert.equal(view.layer.state().items.length, 1); assert.equal(view.panel.querySelector('[data-page]').textContent, '1 / 1');
  assert.match(list.children[0].querySelector('.scene-shot-label').textContent, /^#0001 sensor-54/);
});

test('sensor location Save validates input and opens one confirmation; Cancel and Escape send no mutation', async t => {
  const view = await panelHarness(t);
  view.edit('', -74.1); view.submit(); assert.equal(view.dialog(), undefined); assert.match(view.feedback().textContent, /within Colombia/);
  view.edit(91, -74.1); view.submit(); assert.equal(view.dialog(), undefined); assert.equal(view.mutations.length, 0);
  view.edit(4.66, -74.12); view.submit(); view.submit();
  assert.equal(document.body.querySelectorAll('dialog').length, 1); assert.equal(view.dialog().open, true);
  assert.equal(view.dialog()['aria-label'], 'Save sensor coordinates?'); assert.equal(document.activeElement, view.dialogButton('Cancel'));
  assert.equal(view.coordinate('lat').disabled, true); assert.equal(view.mutations.length, 0);
  assert.equal(view.form['aria-busy'], 'true');
  view.dialogButton('Cancel').dispatchEvent(new Event('click')); await view.settle();
  assert.equal(view.dialog(), undefined); assert.equal(view.coordinate('lat').disabled, false); assert.equal(view.coordinate('lat').value, '4.66'); assert.equal(document.activeElement, view.save());
  assert.equal(view.form['aria-busy'], 'false');
  view.submit(); const escape = new Event('cancel', { cancelable: true }); view.dialog().dispatchEvent(escape); await view.settle();
  assert.equal(escape.defaultPrevented, true); assert.equal(view.dialog(), undefined); assert.equal(view.mutations.length, 0);
  view.data.can_review = false; await view.layer.update(); assert.equal(view.save().hidden, true); assert.equal(view.coordinate('lat').readOnly, true);
  view.submit(); assert.equal(view.dialog(), undefined); assert.equal(view.mutations.length, 0);
});

test('unsubmitted family, department and search drafts survive polling and refreshed department options', async t => {
  const view = await panelHarness(t), filters = view.panel.querySelector('form');
  filters.elements.sensor_type.value = 'temperature'; filters.elements.department.value = 'Valle del Cauca'; filters.elements.q.value = 'Cali';
  view.data.version = 'v2'; view.data.sensors[1].department = 'Antioquia'; await view.layer.update();
  assert.equal(filters.elements.sensor_type.value, 'temperature'); assert.equal(filters.elements.department.value, 'Valle del Cauca'); assert.equal(filters.elements.q.value, 'Cali');
  assert.ok(filters.elements.department.children.some(option => option.value === 'Valle del Cauca')); assert.deepEqual(view.layer.state().filters, {});
  view.layer.setFilters({ sensor_type: 'rainfall', department: 'Bogotá', q: 'sensor-1' });
  assert.equal(filters.elements.sensor_type.value, 'rainfall'); assert.equal(filters.elements.department.value, 'Bogotá'); assert.equal(filters.elements.q.value, 'sensor-1');
});

test('sensor Save sends frozen coordinates once, preserves polling drafts and waits for the persisted publication', async t => {
  const view = await panelHarness(t);
  view.edit(4.66, -74.12); view.data.version = 'v2'; view.data.sensors[0].id = 'new-reading'; view.data.sensors[0].value = 25; await view.layer.update();
  assert.equal(view.layer.state().selectedId, 'new-reading'); assert.equal(view.coordinate('lat').value, '4.66'); assert.equal(view.coordinate('lon').value, '-74.12');
  view.submit(); const confirm = view.dialogButton('Confirm save'); confirm.dispatchEvent(new Event('click')); confirm.dispatchEvent(new Event('click')); await view.settle();
  const request = view.mutations[0]; assert.equal(view.mutations.length, 1); assert.equal(request.path, '/api/prisma/sensors/sensor-1/location'); assert.equal(request.method, 'POST');
  assert.deepEqual(JSON.parse(request.body), { lat: 4.66, lon: -74.12, expected_lat: 4.65, expected_lon: -74.1 });
  await view.layer.update(); assert.equal(request.signal.aborted, false); assert.equal(view.coordinate('lat').value, '4.66');
  view.submit(); assert.equal(view.dialog(), undefined); assert.equal(view.mutations.length, 1);
  request.resolve({ location_saved: true, sensor_id: 'sensor-1', lat: 4.66, lon: -74.12, location_pending_publication: true }); await view.settle();
  assert.match(view.feedback().textContent, /Waiting for.*publication/); assert.equal(view.save().disabled, false);
  await view.layer.update(); assert.match(view.feedback().textContent, /Waiting for/, 'Previous published coordinates do not acknowledge a draft');
  view.data.version = 'v3'; Object.assign(view.data.sensors[0], { lat: 4.66, lon: -74.12 }); await view.layer.update();
  assert.equal(view.feedback().textContent, 'Coordinates saved.'); assert.equal(view.save().disabled, false); assert.equal(view.coordinate('lat').value, '4.66');
});

test('failed sensor saves preserve edits; conflicts refresh expected coordinates and require a new confirmation', async t => {
  const view = await panelHarness(t); view.edit(4.66, -74.12); view.submit(); view.dialogButton('Confirm save').dispatchEvent(new Event('click')); await view.settle();
  view.mutations[0].reject(new Error('Unavailable')); await view.settle();
  assert.match(view.feedback().textContent, /could not be confirmed: Unavailable/); assert.equal(view.feedback().role, 'alert'); assert.equal(view.save().disabled, false);
  await view.layer.update(); assert.equal(view.coordinate('lat').value, '4.66');
  view.submit(); view.dialogButton('Confirm save').dispatchEvent(new Event('click')); await view.settle();
  view.data.version = 'v2'; Object.assign(view.data.sensors[0], { lat: 4.67, lon: -74.13 });
  view.mutations[1].reject(Object.assign(new Error('Conflict'), { status: 409 })); await view.settle();
  assert.match(view.feedback().textContent, /Published coordinates: 4.67, -74.13/); assert.equal(view.coordinate('lat').value, '4.66'); assert.equal(view.mutations.length, 2);
  view.submit(); view.dialogButton('Confirm save').dispatchEvent(new Event('click')); await view.settle();
  assert.deepEqual(JSON.parse(view.mutations[2].body), { lat: 4.66, lon: -74.12, expected_lat: 4.67, expected_lon: -74.13 });
  view.mutations[2].resolve({ location_saved: true, sensor_id: 'wrong-sensor', lat: 4.66, lon: -74.12 }); await view.settle();
  assert.match(view.feedback().textContent, /server did not confirm/); assert.equal(view.feedback().role, 'alert'); assert.equal(view.coordinate('lat').value, '4.66');
});

test('an acknowledged immediate Save keeps accepted coordinates while the follow-up publication is unavailable', async t => {
  const view = await panelHarness(t); view.edit(4.66, -74.12); view.submit(); view.dialogButton('Confirm save').dispatchEvent(new Event('click')); await view.settle();
  view.data.sensors[0].value = NaN;
  view.mutations[0].resolve({ location_saved: true, sensor_id: 'sensor-1', lat: 4.66, lon: -74.12, location_pending_publication: false }); await view.settle();
  assert.equal(view.layer.state().snapshot.sensors[0].lat, 4.65, 'The failed refresh retains the last publication');
  assert.equal(view.coordinate('lat').value, '4.66'); assert.equal(view.coordinate('lon').value, '-74.12');
  assert.equal(view.save().disabled, false, 'A confirmed durable save can be retried if publication fails');
  view.data.version = 'v2'; Object.assign(view.data.sensors[0], { value: 12.3, lat: 4.66, lon: -74.12 }); await view.layer.update();
  assert.equal(view.save().disabled, false); assert.equal(view.feedback().textContent, 'Coordinates saved.');
});

test('publication failure allows an explicitly confirmed retry using the durable saved coordinates', async t => {
  const view = await panelHarness(t); view.edit(4.66, -74.12); view.submit(); view.dialogButton('Confirm save').dispatchEvent(new Event('click')); await view.settle();
  view.mutations[0].resolve({ location_saved: true, sensor_id: 'sensor-1', lat: 4.66, lon: -74.12, location_pending_publication: true, publication_error: 'Publication failed. Try again.' }); await view.settle();
  assert.match(view.feedback().textContent, /Publication failed/); assert.equal(view.save().disabled, false); assert.equal(view.coordinate('lat').value, '4.66');
  view.submit(); assert.equal(view.mutations.length, 1, 'Retry still requires confirmation'); view.dialogButton('Confirm save').dispatchEvent(new Event('click')); await view.settle();
  assert.deepEqual(JSON.parse(view.mutations[1].body), { lat: 4.66, lon: -74.12, expected_lat: 4.66, expected_lon: -74.12 });
  view.data.version = 'v2'; Object.assign(view.data.sensors[0], { lat: 4.66, lon: -74.12 });
  view.mutations[1].resolve({ location_saved: true, sensor_id: 'sensor-1', lat: 4.66, lon: -74.12, location_pending_publication: false }); await view.settle();
  assert.equal(view.feedback().textContent, 'Coordinates saved.'); assert.equal(view.coordinate('lat').value, '4.66');
});

test('a conflict after an accepted pending Save cannot turn the rejected correction into success', async t => {
  const view = await panelHarness(t); view.edit(4.66, -74.12); view.submit(); view.dialogButton('Confirm save').dispatchEvent(new Event('click')); await view.settle();
  view.mutations[0].resolve({ location_saved: true, sensor_id: 'sensor-1', lat: 4.66, lon: -74.12, location_pending_publication: true }); await view.settle();
  assert.match(view.feedback().textContent, /Waiting for/);
  view.edit(4.68, -74.14); view.submit(); view.dialogButton('Confirm save').dispatchEvent(new Event('click')); await view.settle();
  assert.deepEqual(JSON.parse(view.mutations[1].body), { lat: 4.68, lon: -74.14, expected_lat: 4.66, expected_lon: -74.12 });
  view.mutations[1].reject(Object.assign(new Error('Conflict'), { status: 409 })); await view.settle();
  assert.match(view.feedback().textContent, /Location changed since editing.*Published coordinates: 4.65, -74.1/);
  assert.equal(view.feedback().role, 'alert'); assert.equal(view.coordinate('lat').value, '4.68'); assert.equal(view.coordinate('lon').value, '-74.14');
  await view.layer.update(); assert.equal(view.feedback().role, 'alert'); assert.doesNotMatch(view.feedback().textContent, /Coordinates saved/);
  assert.equal(view.mutations.length, 2, 'A rejected correction is not retried automatically');
});

test('changing selection expires sensor confirmation and ignores a late acknowledgment after shutdown', async t => {
  const view = await panelHarness(t); view.edit(4.66, -74.12); view.submit();
  view.layer.select('event-2'); await view.settle(); assert.equal(view.dialog(), undefined); assert.equal(view.mutations.length, 0);
  view.layer.select('event-1'); assert.equal(view.coordinate('lat').value, '4.66'); view.submit();
  view.dialogButton('Confirm save').dispatchEvent(new Event('click')); await view.settle();
  const request = view.mutations[0]; view.lifetime.abort(); assert.equal(request.signal.aborted, true);
  request.resolve({ location_saved: true, sensor_id: 'sensor-1', lat: 4.66, lon: -74.12 }); await view.settle();
  assert.doesNotMatch(view.feedback().textContent, /^Coordinates saved/); assert.equal(view.layer.state().snapshot.sensors[0].lat, 4.65);
});

test('disabling Sensors or losing review access cancels an open confirmation without a mutation', async t => {
  const view = await panelHarness(t); view.edit(4.66, -74.12); view.submit(); view.layer.disable(); await view.settle();
  assert.equal(view.dialog(), undefined); assert.equal(view.mutations.length, 0);
  view.layer.enable(); view.submit(); view.data.can_review = false; await view.layer.update(); await view.settle();
  assert.equal(view.dialog(), undefined); assert.equal(view.mutations.length, 0); assert.equal(view.save().hidden, true); assert.equal(view.coordinate('lat').readOnly, true);
});
