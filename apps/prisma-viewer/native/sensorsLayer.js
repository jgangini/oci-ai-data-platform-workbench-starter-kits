import { bindPanelDisclosure, collapsePanelOnEscape } from '../.upstream/src/ui/panelDisclosure.js';
import { clearOverlaySource, setOverlayEntries, setOverlaySourceVisible } from '../.upstream/src/overlays/worldOverlay.js';
import { isPointerFree } from '../.upstream/src/data/inputOwnership.js';
import { GROUND_SAMPLE_MAX_ARMED_RETRIES, refreshLocalTerrainFloor, setLocalGroundHeight, updateLocalStemGeometry } from '../.upstream/src/data/localGeojsonCore.js';
import { selectInfraLod, applyInfraEvictionGrace } from '../.upstream/src/data/localGeojsonLod.js';
import { utcToBogota } from '../src/model.js';

export const SENSOR_LAYER_ID = 'sensors';
export const sensorFamilies = { river_level: 'River level', rainfall: 'Rainfall', temperature: 'Temperature', soil_moisture: 'Soil moisture', wind_speed: 'Wind speed' };
const statusColors = { normal: '#4ade80', warning: '#fb923c', critical: '#f87171' };
const label = (tag, value, className) => { const element = document.createElement(tag); element.textContent = value ?? ''; if (className) element.className = className; return element; };
const sensorTitle = (item) => `${item.sensor_id} · ${item.municipality || item.locality || 'Colombia'}`;
const sensorTime = (item) => utcToBogota(item.observed_at).replace('T', ' ');
const sensorValue = (item) => `${item.value.toLocaleString('en-US', { maximumFractionDigits: 2 })} ${item.unit}`;

export function validateSensors(snapshot) {
  if (!snapshot || typeof snapshot.version !== 'string' || !Array.isArray(snapshot.sensors ?? [])) throw new Error('Invalid sensor publication.');
  const rows = snapshot.sensors || [], ids = new Set(), sensors = new Set();
  if (rows.length > 5000) throw new Error('Sensor publication exceeds 5000 sensors.');
  for (const item of rows) {
    if (!item || typeof item.id !== 'string' || !item.id || typeof item.sensor_id !== 'string' || !item.sensor_id || ids.has(item.id) || sensors.has(item.sensor_id)) throw new Error('Sensor publication contains invalid or repeated identities.');
    if (!Object.hasOwn(sensorFamilies, item.sensor_type) || !Object.hasOwn(statusColors, item.status) || item.mode !== 'Synthetic' || item.is_simulated !== true) throw new Error('Sensor publication must contain supported Synthetic readings.');
    if (![item.value, item.lat, item.lon].every(Number.isFinite) || Math.abs(item.lat) > 90 || Math.abs(item.lon) > 180 || typeof item.unit !== 'string' || !item.unit || !Number.isFinite(Date.parse(item.observed_at))) throw new Error('Sensor publication contains an invalid measurement, location or time.');
    ids.add(item.id); sensors.add(item.sensor_id);
  }
  return rows;
}

/** Native catalog ownership keeps Sensors independent of social capture and map providers. */
export function createSensorsLayer({ Cesium, request, signal }) {
  let viewer, source, picking, pending, settleRender, enabled = false, selectedId, filters = {}, rows = [], snapshot = { version: '', sensors: [] }, lastError = null, lastUpdate = null;
  const listeners = new Set(), stems = new Map(), removeGeometryListeners = [];
  let lastGeometryUpdate = 0, occluder, cameraPosition, cameraDirection, cameraUp, cameraFov, cameraAspectRatio, canvasHeight, geometryDirty = true, nextGroundSample = 0, groundRetries = 0, sensorsKey = '[]';
  let activeIds = new Set(), lodGrace = new Map(), lastTileLoadCount;
  const bounds = new Cesium.BoundingSphere();
  const state = () => ({ snapshot, items: rows, filters: { ...filters }, selectedId, enabled, lastError, lastUpdate, isRefreshing: !!pending });
  const notify = () => { for (const listener of listeners) listener(state()); };
  function selection() {
    const item = rows.find((row) => row.id === selectedId);
    setOverlayEntries(SENSOR_LAYER_ID, item ? [{ id: item.id, position: () => { const record = stems.get(item.sensor_id); return record?.entity.show ? record.tip : undefined; },
      variant: 'selected', selected: true, title: sensorTitle(item), details: [sensorFamilies[item.sensor_type], `${sensorValue(item)} · ${item.status}`, sensorTime(item)], accent: statusColors[item.status],
      interactive: true, accessibilityLabel: `${sensorTitle(item)}, ${sensorValue(item)}, ${item.status}`, activate: () => layer.select(item.id, false) }] : [], { visible: enabled, cohortLimit: 1, collisionCapacity: 0 });
    // Native points need a settling frame after their canvas texture is enqueued.
    settleRender ||= viewer.scene.postRender.addEventListener(() => { settleRender(); settleRender = undefined; viewer.scene.requestRender(); });
  }
  function updateStems(force = false) {
    const now = Date.now();
    if (!enabled || !viewer || (!force && now - lastGeometryUpdate < 450)) return;
    const moved = geometryDirty || !Cesium.Cartesian3.equals(cameraPosition, viewer.camera.positionWC)
      || !Cesium.Cartesian3.equals(cameraDirection, viewer.camera.directionWC) || !Cesium.Cartesian3.equals(cameraUp, viewer.camera.upWC)
      || cameraFov !== viewer.camera.frustum.fov || cameraAspectRatio !== viewer.camera.frustum.aspectRatio || canvasHeight !== viewer.scene.canvas.clientHeight;
    if (!force && !moved && (now < nextGroundSample || groundRetries >= GROUND_SAMPLE_MAX_ARMED_RETRIES)) return;
    groundRetries = force || moved ? 0 : groundRetries + 1;
    lastGeometryUpdate = now; occluder.cameraPosition = viewer.camera.positionWC;
    cameraPosition = Cesium.Cartesian3.clone(viewer.camera.positionWC, cameraPosition); cameraFov = viewer.camera.frustum.fov; canvasHeight = viewer.scene.canvas.clientHeight;
    cameraAspectRatio = viewer.camera.frustum.aspectRatio;
    cameraDirection = Cesium.Cartesian3.clone(viewer.camera.directionWC, cameraDirection); cameraUp = Cesium.Cartesian3.clone(viewer.camera.upWC, cameraUp);
    geometryDirty = false; nextGroundSample = Infinity;
    if (moved || force) {
      const frustum = viewer.camera.frustum.computeCullingVolume?.(cameraPosition, cameraDirection, cameraUp);
      const candidates = [...stems].map(([id, record]) => {
        const distanceM = Cesium.Cartesian3.distance(cameraPosition, record.base);
        bounds.center = record.base; bounds.radius = distanceM * 2 * Math.tan((cameraFov || Math.PI / 3) / 2) * 65 / (canvasHeight || 1080);
        record.inView = occluder.isPointVisible(record.base) && (!frustum || frustum.computeVisibility(bounds) !== Cesium.Intersect.OUTSIDE);
        return { id, inView: record.inView, distanceM, priority: record.item.id === selectedId ? 10000 : 0 };
      });
      const selection = selectInfraLod(candidates, { cameraHeightM: Cesium.Cartographic.fromCartesian(cameraPosition).height, incumbentIds: activeIds });
      const grace = applyInfraEvictionGrace({ selectedIds: selection.activeIds, builtIds: [...activeIds], graceState: lodGrace, nowMs: now, activeLimit: selection.budget.activeLimit });
      activeIds = new Set(grace.keepIds); lodGrace = grace.graceState;
    }
    // ponytail: reuse Data Centers' zoom budget (80/200/420); every reading remains available in the panel.
    for (const record of stems.values()) {
      const changed = moved || record.dirty || !record.entity.show;
      record.entity.show = record.inView && activeIds.has(record.item.sensor_id);
      if (!record.entity.show) continue;
      // Match the native terrain sampler's distance and retry limits without rebuilding parked geometry.
      const needsGround = !record.groundSampled && viewer.scene.sampleHeightSupported && Cesium.Cartesian3.distance(cameraPosition, record.base) < 75000;
      if (needsGround) nextGroundSample = now + 2000;
      if (!changed && (!needsGround || now - record.lastGroundSampleMs < 2000)) continue;
      const ground = !record.groundSampled && viewer.scene.globe.show && viewer.scene.globe.tilesLoaded !== false ? viewer.scene.globe.getHeight(record.carto) : undefined;
      if (Number.isFinite(ground) && Math.abs(ground) <= 9000 && ground !== record.groundHeight) setLocalGroundHeight(record, ground);
      refreshLocalTerrainFloor(viewer, record);
      updateLocalStemGeometry(viewer, record, now); record.dirty = false;
      if (needsGround && record.groundSampled) groundRetries = 0;
    }
  }
  function refreshStems() {
    if (!enabled) return;
    lastGeometryUpdate = 0; geometryDirty = true;
    for (const record of stems.values()) if (!record.groundSampled) record.lastGroundSampleMs = 0;
    viewer.scene.requestRender();
  }
  function paint() {
    const query = (filters.q || '').toLocaleLowerCase();
    rows = snapshot.sensors.filter((item) => (!filters.sensor_type || item.sensor_type === filters.sensor_type) && (!filters.department || item.department === filters.department)
      && (!query || `${item.sensor_id} ${item.municipality || ''} ${item.department || ''}`.toLocaleLowerCase().includes(query)));
    if (!rows.some((item) => item.id === selectedId)) selectedId = undefined;
    if (!source) return;
    const retained = new Set(rows.map((item) => item.sensor_id)); let newGeometry = false;
    source.entities.suspendEvents();
    for (const [id, record] of stems) if (!retained.has(id)) { source.entities.remove(record.entity); stems.delete(id); newGeometry = true; }
    for (const item of rows) {
      let record = stems.get(item.sensor_id);
      if (record) {
        if (record.item.lat !== item.lat || record.item.lon !== item.lon) {
          record.carto = Cesium.Cartographic.fromDegrees(item.lon, item.lat);
          setLocalGroundHeight(record, viewer.scene.globe.getHeight(record.carto) || 0);
          record.groundSampled = false; record.lastGroundSampleMs = 0; record.dirty = true; newGeometry = true;
        }
        if (record.item.status !== item.status) {
          const color = Cesium.Color.fromCssColorString(statusColors[item.status]); record.entity.point.color = color; record.entity.polyline.material.color = color;
        }
        record.entity.name = sensorTitle(item);
        record.item = item;
        continue;
      }
      const carto = Cesium.Cartographic.fromDegrees(item.lon, item.lat), groundHeight = viewer.scene.globe.getHeight(carto) || 0;
      const base = Cesium.Cartesian3.fromDegrees(item.lon, item.lat, groundHeight), tip = Cesium.Cartesian3.fromDegrees(item.lon, item.lat, groundHeight + 2000);
      const color = Cesium.Color.fromCssColorString(statusColors[item.status]);
      const entity = source.entities.add({ id: `sensors:${item.sensor_id}`, name: sensorTitle(item), position: tip,
        polyline: { positions: [base, tip], width: 3.5, material: color, arcType: Cesium.ArcType.NONE },
        point: { pixelSize: item.id === selectedId ? 15 : 7, color, outlineColor: Cesium.Color.WHITE,
          outlineWidth: item.id === selectedId ? 2 : 0, disableDepthTestDistance: Infinity } });
      stems.set(item.sensor_id, { entity, item, dirty: true, carto, base, tip, nextTip: Cesium.Cartesian3.clone(tip), groundHeight, groundSampled: false, lastGroundSampleMs: 0,
        stemPositionBuffers: [[base, tip], [base, tip]], stemPositionBufferIndex: 0 });
      newGeometry = true;
    }
    source.entities.resumeEvents(); if (newGeometry) updateStems(true); selection(); viewer.scene.requestRender();
  }
  const layer = {
    id: SENSOR_LAYER_ID, name: 'Sensors', icon: '◈', source: 'OCI AIDP', updateInterval: 30000,
    init(target) {
      viewer = target; source = new Cesium.CustomDataSource(SENSOR_LAYER_ID); source.show = enabled; viewer.dataSources.add(source);
      occluder = new Cesium.EllipsoidalOccluder(Cesium.Ellipsoid.WGS84, viewer.camera.positionWC);
      removeGeometryListeners.push(viewer.scene.preUpdate.addEventListener(() => updateStems()), viewer.camera.moveEnd.addEventListener(refreshStems),
        viewer.scene.globe.tileLoadProgressEvent.addEventListener((remaining) => {
          if (remaining === 0 && lastTileLoadCount !== 0) refreshStems();
          lastTileLoadCount = remaining;
        }));
      picking = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
      picking.setInputAction(({ position }) => { if (!enabled || !isPointerFree()) return; const id = viewer.scene.pick(position)?.id?.id; if (typeof id === 'string' && id.startsWith('sensors:')) layer.select(stems.get(id.slice(8))?.item.id, false); }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
      paint();
    },
    enable() { enabled = true; if (source) source.show = true; geometryDirty = true; updateStems(true); setOverlaySourceVisible(SENSOR_LAYER_ID, true); notify(); },
    disable() { enabled = false; pending?.abort(); if (source) source.show = false; setOverlaySourceVisible(SENSOR_LAYER_ID, false); viewer?.scene.requestRender(); notify(); },
    async update(_viewer, { signal: operationSignal } = {}) {
      if (!enabled) return false;
      pending?.abort(); const turn = new AbortController(); pending = turn;
      const requestSignal = AbortSignal.any([turn.signal, ...[signal, operationSignal].filter(Boolean)]); notify();
      try {
        const next = await request('/api/prisma/snapshot', { signal: requestSignal });
        const readings = validateSensors(next);
        if (requestSignal.aborted || pending !== turn || !enabled) return false;
        const selectedSensor = snapshot.sensors.find((item) => item.id === selectedId)?.sensor_id;
        const nextSensorsKey = JSON.stringify(readings), changed = sensorsKey !== nextSensorsKey;
        sensorsKey = nextSensorsKey;
        snapshot = { ...next, sensors: readings }; lastUpdate = Date.now(); lastError = null;
        selectedId = readings.find((item) => item.sensor_id === selectedSensor)?.id;
        if (changed) paint();
        return true;
      } catch (error) { if (!requestSignal.aborted) lastError = error.message || 'Sensor publication unavailable.'; return false; }
      finally { if (pending === turn) { pending = undefined; notify(); } }
    },
    select(id, focus = true) {
      const item = rows.find((row) => row.id === id); if (!enabled || !item) return false;
      const previous = stems.get(rows.find((row) => row.id === selectedId)?.sensor_id)?.entity; if (previous) { previous.point.pixelSize = 7; previous.point.outlineWidth = 0; }
      selectedId = item.id;
      geometryDirty = true; updateStems(true);
      const current = stems.get(item.sensor_id)?.entity; if (current) { current.point.pixelSize = 15; current.point.outlineWidth = 2; }
      selection(); notify(); viewer?.scene.requestRender();
      if (focus && viewer) viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(item.lon, item.lat, 12000), duration: 1 });
      return true;
    },
    setFilters(next) { filters = { ...next }; paint(); notify(); },
    state,
    subscribe(listener) { listeners.add(listener); listener(state()); return () => listeners.delete(listener); },
    getStats: () => ({ count: rows.length, available: !!snapshot.version, lastUpdate, lastError }),
    destroy() { enabled = false; pending?.abort(); settleRender?.(); picking?.destroy(); for (const remove of removeGeometryListeners) remove(); stems.clear(); clearOverlaySource(SENSOR_LAYER_ID); if (source) viewer.dataSources.remove(source, true); listeners.clear(); },
  };
  return layer;
}

export function mountSensorsPanel({ layer, signal, setPanelCollapsed }) {
  const panel = label('section', '', 'tc-social-panel panel-collapsible collapsed'); panel.id = 'sensors-panel'; panel.dataset.panelId = panel.id; panel.hidden = true;
  panel.innerHTML = `<div class="panel-glow"></div><div class="global-context-panel-inner"><div class="panel-header"><span class="panel-title">SENSORS</span><span class="panel-divider"></span><button class="panel-collapse-btn" data-dock-toggle-target="sensors-panel" type="button" aria-expanded="false" aria-label="Expand Sensors" title="Expand Sensors"><span aria-hidden="true">▶</span></button></div>
    <div class="tc-panel-body data-toggle-list" data-rail-scroller><p data-status role="status"></p><form class="tc-filters" aria-label="Sensor filters"><label>Family<select name="sensor_type"><option value="">All</option></select></label><label>Department<select name="department"><option value="">All</option></select></label><div class="sensors-search"><label for="sensors-query">Search sensors</label><div class="sensors-search-controls"><input id="sensors-query" name="q" type="search" maxlength="200" placeholder="Sensor or municipality"><button type="submit">Filter</button></div></div></form><div class="sensors-list scene-shot-list" aria-label="Sensors"></div><div class="sensors-pages"><button type="button" data-previous aria-label="Previous sensors">Previous</button><span data-page></span><button type="button" data-next aria-label="Next sensors">Next</button></div><section class="sensors-detail" aria-label="Sensor coordinates"></section></div></div>`;
  document.getElementById('territorial-panel').after(panel);
  const onChange = (collapsed) => setPanelCollapsed(panel.id, collapsed, { explicit: true, persist: false, syncShare: false });
  const disclosure = bindPanelDisclosure({ panel, buttons: [panel.querySelector('.panel-collapse-btn')], onChange, onEscape: (event) => collapsePanelOnEscape(event, { panel, onChange }) });
  const form = panel.querySelector('form'), list = panel.querySelector('.sensors-list'), detail = panel.querySelector('.sensors-detail');
  const coordinates = label('dl', '', 'sensors-coordinates'), coordinateValues = {};
  for (const [name, title] of [['lat', 'Latitude'], ['lon', 'Longitude']]) {
    const field = label('div'), value = label('dd');
    field.append(label('dt', title), value); coordinates.append(field); coordinateValues[name] = value;
  }
  detail.append(coordinates);
  for (const [value, name] of Object.entries(sensorFamilies)) { const option = label('option', name); option.value = value; form.elements.sensor_type.append(option); }
  let page = 0, previousEnabled = false, previousSelection, departments = '', appliedFilters, listKey;
  const render = (state) => {
    const selected = state.items.find((row) => row.id === state.selectedId);
    panel.hidden = !state.enabled;
    if (state.enabled && (!previousEnabled || (selected && selected.sensor_id !== previousSelection))) onChange(false);
    if (previousEnabled && !state.enabled) onChange(true);
    previousEnabled = state.enabled; previousSelection = selected?.sensor_id;
    panel.querySelector('[data-status]').textContent = state.lastError ? `${state.lastError}${state.snapshot.version ? ' Showing the last successful publication.' : ''}` : state.snapshot.version ? '' : state.isRefreshing ? 'Loading readings…' : 'Waiting for sensor publication…';
    list.setAttribute('aria-busy', String(state.isRefreshing));
    const filterKey = JSON.stringify(state.filters);
    if (filterKey !== appliedFilters) {
      appliedFilters = filterKey;
      for (const name of ['sensor_type', 'department', 'q']) form.elements[name].value = state.filters[name] || '';
    }
    const draftDepartment = form.elements.department.value;
    const values = [...new Set([...state.snapshot.sensors.map((item) => item.department), state.filters.department, draftDepartment].filter(Boolean))].sort();
    if (JSON.stringify(values) !== departments) {
      departments = JSON.stringify(values); const all = label('option', 'All'); all.value = ''; form.elements.department.replaceChildren(all);
      for (const value of values) { const option = label('option', value); option.value = value; form.elements.department.append(option); }
      form.elements.department.value = draftDepartment;
    }
    const pages = Math.max(1, Math.ceil(state.items.length / 50)); page = Math.min(page, pages - 1);
    const visible = state.items.slice(page * 50, page * 50 + 50), nextListKey = JSON.stringify([page, state.selectedId, visible]);
    if (nextListKey !== listKey) {
      listKey = nextListKey;
      const focusedId = list.contains(document.activeElement) ? document.activeElement.dataset.sensorId : null, scrollTop = list.scrollTop;
      list.replaceChildren();
      for (const [index, item] of visible.entries()) {
        const button = label('button', '', 'scene-shot-row'); button.type = 'button'; button.dataset.sensorId = item.sensor_id; button.setAttribute('aria-pressed', String(item.id === state.selectedId));
        const status = label('span', item.status, 'sensor-status'); status.dataset.status = item.status;
        const metadata = label('span', `${sensorFamilies[item.sensor_type]} · `, 'scene-shot-meta'); metadata.append(status, label('span', ` · ${sensorValue(item)}`));
        button.append(label('strong', `#${String(page * 50 + index + 1).padStart(4, '0')} ${sensorTitle(item)}`, 'scene-shot-label'), metadata);
        button.addEventListener('click', () => layer.select(item.id)); list.append(button); if (item.sensor_id === focusedId) button.focus({ preventScroll: true });
      }
      list.scrollTop = scrollTop;
    }
    panel.querySelector('[data-page]').textContent = `${page + 1} / ${pages}`;
    panel.querySelector('[data-previous]').disabled = page === 0; panel.querySelector('[data-next]').disabled = page + 1 >= pages;
    detail.hidden = !selected;
    for (const name of ['lat', 'lon']) coordinateValues[name].textContent = selected ? String(selected[name]) : '';
  };
  const apply = () => { page = 0; list.scrollTop = 0; layer.setFilters({ sensor_type: form.elements.sensor_type.value, department: form.elements.department.value, q: form.elements.q.value.trim() }); };
  form.addEventListener('submit', (event) => { event.preventDefault(); apply(); }, { signal });
  panel.querySelector('[data-previous]').addEventListener('click', () => { page = Math.max(0, page - 1); list.scrollTop = 0; render(layer.state()); }, { signal });
  panel.querySelector('[data-next]').addEventListener('click', () => { page++; list.scrollTop = 0; render(layer.state()); }, { signal });
  const unsubscribe = layer.subscribe(render);
  signal.addEventListener('abort', () => { unsubscribe(); disclosure.destroy(); panel.remove(); }, { once: true });
  return { panel };
}
