import { bogotaToUtc, displayLocality, displaySeverity, evidenceFor, filteredIncidents, modeLabel, parseBbox, photosFor, reportActivity, safeSourceUrl, utcToBogota, validPeriod, validateSnapshot } from '../src/model.js';

export const TERRITORIAL_LAYER_ID = 'territorial-events';
export function seedBogotaView(location, history) {
  if (location.hash) return false;
  const url = new URL(location.href);
  url.hash = 'lat=4.66&lon=-74.085&alt=38000&heading=0&pitch=-90&roll=0';
  history.replaceState(history.state, '', url.href);
  return true;
}

export const categoryName = (value) => ({ inundacion: 'Flooding', incendio: 'Fire', movimiento_masa: 'Landslide', infraestructura: 'Infrastructure', lluvia: 'Rainfall' }[value] || value);
export const platformName = (value) => ({ x: 'X', facebook: 'Facebook', instagram: 'Instagram', tiktok: 'TikTok' }[value] || value);
export const text = (tag, value, className) => {
  const node = document.createElement(tag);
  node.textContent = value ?? '';
  if (className) node.className = className;
  return node;
};

function drawTerritorialPoints(viewer, dataSource, rows, selectedId, Cesium) {
  dataSource.entities.removeAll();
  for (const item of rows) {
    if (!Number.isFinite(item.lat) || !Number.isFinite(item.lon)) continue;
    dataSource.entities.add({
        id: `territorial:${item.id}`, name: item.title || categoryName(item.category),
        position: Cesium.Cartesian3.fromDegrees(item.lon, item.lat),
        point: { pixelSize: item.id === selectedId ? 17 : 11,
          color: Cesium.Color.fromCssColorString(item.severity === 'high' ? '#ff796f' : '#ffc86b'),
          outlineColor: Cesium.Color.WHITE, outlineWidth: item.id === selectedId ? 3 : 1,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Infinity },
    });
  }
  viewer.scene.requestRender();
}

async function refreshPublication(feed, request, lifetime, operationSignal) {
  if (!feed.enabled) return false;
  feed.pending?.abort();
  const turn = new AbortController(); feed.pending = turn;
  const signal = AbortSignal.any([turn.signal, ...[lifetime, operationSignal].filter(Boolean)]);
  try {
    const next = validateSnapshot(await request('/api/prisma/snapshot', { signal }));
    if (signal.aborted || feed.pending !== turn || !feed.enabled) return false;
    feed.snapshot = next; feed.lastError = null; feed.lastUpdate = Date.now();
    return true;
  } catch (error) {
    if (!signal.aborted) feed.lastError = error.message;
    return false;
  } finally { if (feed.pending === turn) feed.pending = undefined; }
}

/** A normal native catalog layer. Only this layer's data source is ever cleared. */
export function createTerritorialLayer({ Cesium, request, signal }) {
  let viewer, dataSource, picking;
  let filters = {}, selectedId;
  const feed = { snapshot: { version: '', incidents: [], evidence: [] }, enabled: false, lastError: null, lastUpdate: null, pending: undefined };
  const listeners = new Set();
  const items = () => filteredIncidents(feed.snapshot, filters);
  const state = () => ({ snapshot: feed.snapshot, filters: { ...filters }, selectedId, enabled: feed.enabled, lastError: feed.lastError, lastUpdate: feed.lastUpdate, items: items() });
  const notify = () => { for (const listener of listeners) listener(state()); };
  function paint() {
    if (!dataSource) return;
    const rows = items();
    if (selectedId && !rows.some((item) => item.id === selectedId)) selectedId = undefined;
    drawTerritorialPoints(viewer, dataSource, rows, selectedId, Cesium);
  }

  function select(id, focus = true) {
    const item = items().find((row) => row.id === id);
    if (!item) return false;
    selectedId = id;
    paint(); notify();
    if (focus && Number.isFinite(item.lat) && Number.isFinite(item.lon)) {
      viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(item.lon, item.lat, 7000), duration: 1 });
    }
    return true;
  }

  const layer = {
    id: TERRITORIAL_LAYER_ID, name: 'Territorial Control', icon: '◉', source: 'AIDP · Bogotá', updateInterval: 10000,
    init(target) {
      viewer = target;
      dataSource = new Cesium.CustomDataSource(TERRITORIAL_LAYER_ID);
      dataSource.show = false;
      viewer.dataSources.add(dataSource);
      picking = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
      picking.setInputAction(({ position }) => {
        const entity = viewer.scene.pick(position)?.id;
        if (typeof entity?.id === 'string' && entity.id.startsWith('territorial:')) select(entity.id.slice(12), false);
      }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
    },
    enable() { feed.enabled = true; dataSource.show = true; notify(); },
    disable() { feed.enabled = false; feed.pending?.abort(); dataSource.show = false; viewer.scene.requestRender(); notify(); },
    async update(_viewer, { signal: operationSignal } = {}) {
      const updated = await refreshPublication(feed, request, signal, operationSignal);
      if (updated) paint();
      notify(); return updated;
    },
    destroy() { feed.pending?.abort(); picking?.destroy(); if (dataSource) viewer.dataSources.remove(dataSource, true); listeners.clear(); },
    getStats: () => ({ count: items().length, lastUpdate: feed.lastUpdate, lastError: feed.lastError, available: Boolean(feed.snapshot.version) }),
    state,
    subscribe(listener) { listeners.add(listener); listener(state()); return () => listeners.delete(listener); },
    setFilters(next) {
      if (!validPeriod(next)) throw new Error('The period must be valid and From must precede To.');
      parseBbox(next.bbox);
      filters = { ...next }; paint(); notify();
    },
    select,
    visibleArea() {
      const rectangle = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid);
      if (!rectangle) throw new Error('Point the camera toward the globe before selecting an area.');
      const bounds = [rectangle.west, rectangle.south, rectangle.east, rectangle.north].map((value) => Cesium.Math.toDegrees(value).toFixed(6)).join(',');
      parseBbox(bounds); return bounds;
    },
  };
  return layer;
}

function appendEvidence(parent, evidence, incident) {
  const card = text('article', '', 'tc-evidence'); card.id = `tc-evidence-${evidence.id}`;
  card.append(text('strong', `${platformName(evidence.platform)} · ${modeLabel(evidence.mode)}`), text('p', evidence.text), text('small', `${evidence.id} · ${evidence.observed_at || evidence.created_at || 'Time unavailable'} · ${evidence.location_method || 'Location method unavailable'}`));
  for (const media of photosFor(evidence, incident)) {
    const figure = document.createElement('figure');
    const image = document.createElement('img');
    image.src = media.url; image.alt = media.alt_text || 'Photo attached to the original publication'; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
    const caption = text('figcaption', 'Source attachment · event reviewed; image claim not independently verified.');
    image.addEventListener('error', () => { image.remove(); caption.textContent = 'Image unavailable. Open the original source.'; }, { once: true });
    figure.append(image, caption); card.append(figure);
  }
  const url = evidence.mode === 'real' ? safeSourceUrl(evidence.source_uri) : null;
  if (url) { const link = text('a', 'Open original source ↗'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; card.append(link); }
  parent.append(card);
}

function appendActivity(parent, incident) {
  const activity = reportActivity(incident);
  const block = text('section', '', 'tc-evidence'); block.setAttribute('aria-label', 'Report activity by network');
  block.append(text('h4', `Report activity: ${activity.level}`));
  for (const row of activity.networks) block.append(text('p', `${platformName(row.platform)} · ${row.count} distinct reports · ${row.level}`));
  block.append(text('small', 'Network windows and thresholds measure report activity, not severity, independent corroboration or human confirmation.'));
  parent.append(block);
}

function appendReview(parent, { incident, request, layer, signal }) {
  const form = document.createElement('form'); form.className = 'tc-review';
  const label = text('label', 'Review note'); const note = document.createElement('textarea'); note.maxLength = 1000; note.rows = 2; note.value = incident.review_note || ''; label.append(note); form.append(label);
  const status = text('p', ''); status.setAttribute('role', 'status');
  const values = [['validated', 'Validate'], ['rejected', 'Reject'], ['pending', 'Pending']];
  for (const [value, title] of values) { const button = text('button', title); button.type = 'submit'; button.value = value; form.append(button); }
  form.append(status);
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); const buttons = [...form.querySelectorAll('button')]; buttons.forEach((button) => { button.disabled = true; });
    try { await request(`/api/prisma/incidents/${encodeURIComponent(incident.id)}/review`, { method: 'POST', body: JSON.stringify({ status: event.submitter.value, note: note.value }), signal }); await layer.update(); }
    catch (error) { status.textContent = error.message; }
    finally { buttons.forEach((button) => { button.disabled = false; }); }
  }, { signal });
  parent.append(form);
}

function appendIncidentOverview(detail, incident) {
  detail.append(text('h3', incident.title || categoryName(incident.category)), text('p', incident.summary), text('p', `${displayLocality(incident.locality)} · Severity: ${displaySeverity(incident.severity)} · Review: ${incident.review_status}`), text('small', `Provenance: ${modeLabel(incident.mode)} · Classification confidence: ${incident.confidence} · Last observed: ${incident.last_observed_at || incident.created_at || 'Unavailable'}`));
  if (Number.isFinite(incident.corroboration_score)) detail.append(text('p', `Corroboration index: ${incident.corroboration_score}/100 · ${incident.independent_source_count ?? 'Unknown'} independent sources. A heuristic, not a probability or confirmation.`));
  appendActivity(detail, incident);
  detail.append(text('p', incident.lat == null ? 'Location unresolved: no invented map position.' : 'Location may be approximate; inspect each source’s location method.'));
}

function renderEventDetail(detail, state, view, { request, layer, signal }) {
  const incident = state.items.find((item) => item.id === state.selectedId);
  const nextKey = JSON.stringify([incident, incident && evidenceFor(state.snapshot, incident), state.snapshot.can_review]);
  if (nextKey === view.detailKey) return;
  const previousNote = detail.dataset.incidentId === incident?.id ? detail.querySelector('textarea')?.value : undefined;
  view.detailKey = nextKey; view.reviewLifetime?.abort(); view.reviewLifetime = new AbortController(); detail.replaceChildren(); detail.dataset.incidentId = incident?.id || '';
  if (!incident) { detail.append(text('p', 'Select an event to inspect its evidence.')); return; }
  appendIncidentOverview(detail, incident);
  for (const evidence of evidenceFor(state.snapshot, incident)) appendEvidence(detail, evidence, incident);
  if (state.snapshot.can_review) {
    appendReview(detail, { incident, request, layer, signal: AbortSignal.any([signal, view.reviewLifetime.signal]) });
    if (previousNote != null) detail.querySelector('textarea').value = previousNote;
  }
}

function renderEventFilters(form, state) {
  for (const name of ['locality', 'platform', 'category', 'severity']) {
    const select = form.elements.namedItem(name); const rows = name === 'platform' ? state.snapshot.evidence : state.snapshot.incidents;
    const values = [...new Set(rows.map((row) => row[name]).filter(Boolean))].sort();
    if (state.filters[name] && !values.includes(state.filters[name])) values.push(state.filters[name]);
    const labels = { locality: displayLocality, platform: platformName, category: categoryName, severity: displaySeverity };
    select.replaceChildren(new Option('All', ''), ...values.map((value) => new Option(labels[name](value), value)));
    select.value = state.filters[name] || '';
  }
  for (const name of ['date_from', 'date_to']) form.elements.namedItem(name).value = utcToBogota(state.filters[name]);
  form.elements.bbox.value = state.filters.bbox || '';
}

function renderEventList(list, state, layer) {
  list.replaceChildren();
  if (!state.items.length) list.append(text('p', 'No events match these filters.'));
  for (const incident of state.items) {
    const button = text('button', '', 'tc-event'); button.type = 'button'; button.setAttribute('aria-pressed', String(incident.id === state.selectedId));
    button.append(text('strong', incident.title || categoryName(incident.category)), text('small', `${displayLocality(incident.locality)} · ${displaySeverity(incident.severity)} · ${incident.evidence_ids.length} evidence items`));
    button.addEventListener('click', () => layer.select(incident.id)); list.append(button);
  }
}

export function mountTerritorialPanel({ layer, request, signal }) {
  const panel = document.createElement('details'); panel.id = 'territorial-panel'; panel.className = 'tc-panel tc-events';
  panel.innerHTML = `<summary>Territorial Control <span data-count></span></summary><div class="tc-panel-body">
    <p data-status role="status"></p><form class="tc-filters" aria-label="Territorial event filters">
    <label>Locality<select name="locality"><option value="">All</option></select></label><label>Network<select name="platform"><option value="">All</option></select></label>
    <label>Event<select name="category"><option value="">All</option></select></label><label>Severity<select name="severity"><option value="">All</option></select></label>
    <label>From · Bogotá time<input name="date_from" type="datetime-local" step="1"></label><label>To · Bogotá time<input name="date_to" type="datetime-local" step="1"></label>
    <input name="bbox" type="hidden"><div class="tc-actions"><button type="button" data-area>Filter map area</button><button type="reset">Clear filters</button></div></form>
    <p data-area-status></p><p data-filter-error role="alert"></p><div class="tc-event-list" aria-label="Territorial events"></div><section class="tc-event-detail" aria-label="Selected event"></section></div>`;
  document.body.append(panel);
  const form = panel.querySelector('form'); const detail = panel.querySelector('.tc-event-detail');
  const view = { detailKey: '', reviewLifetime: undefined }; let previousSelection;
  panel.addEventListener('keydown', (event) => { if (event.key === 'Escape' && panel.open) { event.preventDefault(); event.stopPropagation(); panel.open = false; panel.querySelector('summary').focus(); } }, { signal });
  const showError = (error) => { panel.querySelector('[data-filter-error]').textContent = error?.message || ''; };
  const apply = () => {
    try { const filters = Object.fromEntries([...new FormData(form)].filter(([, value]) => value).map(([key, value]) => [key, key.startsWith('date_') ? bogotaToUtc(value) : value])); layer.setFilters(filters); showError(); }
    catch (error) { showError(error); }
  };
  form.addEventListener('submit', (event) => event.preventDefault(), { signal });
  form.addEventListener('change', apply, { signal });
  form.addEventListener('reset', () => { queueMicrotask(() => { form.elements.bbox.value = ''; apply(); }); }, { signal });
  panel.querySelector('[data-area]').addEventListener('click', () => { try { form.elements.bbox.value = layer.visibleArea(); apply(); } catch (error) { showError(error); } }, { signal });

  const unsubscribe = layer.subscribe((state) => {
    if (state.selectedId && state.selectedId !== previousSelection) panel.open = true;
    previousSelection = state.selectedId;
    panel.querySelector('[data-count]').textContent = `· ${state.items.length}`;
    panel.querySelector('[data-status]').textContent = state.lastError ? `Publication unavailable: ${state.lastError}${state.snapshot.version ? ' Showing the last successful publication.' : ''}` : state.snapshot.version ? `${state.enabled ? 'Layer on' : 'Layer off'} · Published ${state.snapshot.published_at || state.snapshot.version}` : 'Waiting for a publication…';
    renderEventFilters(form, state);
    panel.querySelector('[data-area-status]').textContent = state.filters.bbox ? 'Fixed map area selected. Reports without coordinates are excluded.' : '';
    renderEventList(panel.querySelector('.tc-event-list'), state, layer);
    renderEventDetail(detail, state, view, { request, layer, signal });
  });
  const showEvidence = (id) => { const incident = layer.state().items.find((item) => item.evidence_ids.includes(id)); if (!incident) return; panel.open = true; layer.select(incident.id, false); document.getElementById(`tc-evidence-${id}`)?.scrollIntoView({ block: 'nearest' }); };
  signal.addEventListener('abort', () => { unsubscribe(); view.reviewLifetime?.abort(); panel.remove(); }, { once: true });
  return { panel, showEvidence };
}
