import * as Cesium from 'cesium';
import 'cesium/Build/Cesium/Widgets/widgets.css';
import './style.css';
import { createApplicationViewer, installTrackpadPinchZoom } from '../vendor/gods-eye-view/src/app/viewer.js';
import { createKeylessTerrain } from '../vendor/gods-eye-view/src/maps/terrain.js';
import { allowedActions, bogotaToUtc, DATE_KEYS, displayLocality, displaySeverity, evidenceFor, filteredIncidents, FILTER_KEYS, modeLabel, parseBbox, photosFor, reportActivity, safeSourceUrl, utcToBogota, validPeriod, validateSnapshot } from './model.js';
import { createTerritorialSession } from './chat.js';
import { loadContext, nasaDate, nasaUrl, renderNews } from './context.js';
import { observeImagery } from './imagery.js';

const $ = (id) => document.getElementById(id);
const filtersForm = $('filters');
let snapshot = { version: '', incidents: [], evidence: [] };
let selectedId;
let viewer;
let refreshing = false;
let turnSnapshot;
let terrainRequest = 0;
let satelliteLayer;
function filters() {
  return Object.fromEntries([...new FormData(filtersForm)].filter(([, value]) => value).map(([key, value]) => [key, DATE_KEYS.includes(key) ? bogotaToUtc(value) : value]));
}
const text = (tag, value, className) => {
  const element = document.createElement(tag);
  element.textContent = value ?? '';
  if (className) element.className = className;
  return element;
};
const modeBadge = (mode) => text('span', modeLabel(mode), `badge ${mode === 'real' ? 'real' : 'simulation'}`);
const date = (value) => value ? new Date(value).toLocaleString('es-CO', { timeZone: 'America/Bogota' }) : 'No date';
const severityColor = (value) => /critical|critica|crítica|alta|high|^4$|^5$/i.test(String(value)) ? '#fb786e' : '#efbe64';
const platformNames = { x: 'X', facebook: 'Facebook', instagram: 'Instagram', tiktok: 'TikTok' };
const categoryName = (value) => ({ inundacion: 'Flooding', incendio: 'Fire', movimiento_masa: 'Landslide', infraestructura: 'Infrastructure', lluvia: 'Rainfall' }[value] || value);
const reviewName = (value) => ({ pending: 'Pending review', validated: 'Validated', rejected: 'Rejected' }[value] || value);

function platformBadge(platform) {
  const badge = text('span', '', 'platform');
  if (platformNames[platform]) {
    const logo = document.createElement('img');
    logo.src = `${import.meta.env.BASE_URL}brand-icons/${platform}.svg`;
    logo.alt = ''; logo.width = 16; logo.height = 16;
    badge.append(logo);
  }
  badge.append(text('span', platformNames[platform] || platform));
  return badge;
}
function clearFilters() {
  filtersForm.reset();
  filtersForm.elements.namedItem('bbox').value = '';
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...options.headers },
    signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(110000)]) : AbortSignal.timeout(15000),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(response.status === 401 ? 'Your session expired. Sign in through administration.' : typeof data.detail === 'string' ? data.detail : `Service unavailable (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function home() {
  viewer?.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(-74.085, 4.66, 38000), duration: 1.2 });
}

function selectIncident(id, focus = true) {
  selectedId = id;
  renderIncidents();
  renderDetail();
  const incident = snapshot.incidents.find((item) => item.id === id);
  if (focus && incident && viewer && Number.isFinite(incident.lat) && Number.isFinite(incident.lon)) viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(incident.lon, incident.lat, 7000), duration: 1 });
}

function renderIncidents() {
  let activeFilters;
  try { activeFilters = filters(); }
  catch (error) { $('period-status').textContent = error.message; return; }
  $('period-status').textContent = validPeriod(activeFilters) ? 'Inclusive period by incident creation time.' : 'From cannot be later than To.';
  $('area-status').textContent = activeFilters.bbox ? 'Fixed map area selected. Move the map and filter again to change it. Reports without coordinates are excluded.' : '';
  $('clear-area').hidden = !activeFilters.bbox;
  const items = filteredIncidents(snapshot, activeFilters);
  const evidenceIds = new Set(items.flatMap((item) => item.evidence_ids));
  renderNews(snapshot.evidence.filter((item) => evidenceIds.has(item.id)));
  if (selectedId && !items.some((item) => item.id === selectedId)) { selectedId = undefined; renderDetail(); }
  $('count').textContent = String(items.length);
  $('incidents').replaceChildren();
  if (!items.length) $('incidents').append(text('p', 'No events match these filters.', 'empty'));
  for (const item of items) {
    const row = text('button', '', `incident ${item.id === selectedId ? 'selected' : ''}`);
    row.type = 'button';
    row.setAttribute('aria-pressed', String(item.id === selectedId));
    const header = text('span', '', 'incident-header');
    header.append(text('span', `${displaySeverity(item.severity)} · ${displayLocality(item.locality)}`));
    row.append(text('strong', `${categoryName(item.category)} · ${displayLocality(item.locality)}`), header, text('small', `${item.evidence_ids.length} evidence items · ${reviewName(item.review_status)}`));
    const sources = text('span', '', 'platform-list');
    for (const platform of new Set(evidenceFor(snapshot, item).map((record) => record.platform))) sources.append(platformBadge(platform));
    row.append(sources);
    row.addEventListener('click', () => selectIncident(item.id));
    $('incidents').append(row);
  }
  if (!viewer) return;
  viewer.entities.removeAll();
  for (const item of items) {
    if (!Number.isFinite(item.lat) || !Number.isFinite(item.lon)) continue;
    viewer.entities.add({ id: item.id, position: Cesium.Cartesian3.fromDegrees(item.lon, item.lat),
      point: { pixelSize: item.id === selectedId ? 17 : 11, color: Cesium.Color.fromCssColorString(severityColor(item.severity)), outlineColor: Cesium.Color.WHITE, outlineWidth: item.id === selectedId ? 3 : 1, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY },
      label: { text: `${categoryName(item.category)} · ${displayLocality(item.locality)}`, font: '12px sans-serif', fillColor: Cesium.Color.WHITE, showBackground: true, backgroundColor: Cesium.Color.fromCssColorString('#10252de6'), pixelOffset: new Cesium.Cartesian2(0, -25), heightReference: Cesium.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY, distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 70000) },
    });
  }
}

function appendPhotos(card, evidence, incident) {
  for (const photo of photosFor(evidence, incident)) {
    const figure = text('figure', '', 'evidence-photo');
    const image = document.createElement('img');
    image.src = photo.url;
    image.alt = typeof photo.alt_text === 'string' && photo.alt_text ? photo.alt_text : photo.origin === 'ai_generated' ? 'AI-generated Synthetic image' : 'Photo attached to the original publication';
    image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
    const caption = text('figcaption', photo.origin === 'ai_generated' ? 'AI-generated Synthetic image' : 'Publication attachment · image claim not independently verified');
    image.addEventListener('error', () => { image.remove(); caption.textContent = 'Image unavailable. Open the original source.'; }, { once: true });
    figure.append(image, caption); card.append(figure);
  }
}

function evidenceCard(item, incident) {
  const card = text('article', '', 'evidence');
  const header = text('div', '', 'evidence-header');
  header.append(modeBadge(item.mode), platformBadge(item.platform), text('small', item.id));
  card.append(header, text('p', item.text), text('small', `${date(item.observed_at || item.created_at)} · ${item.location_method || 'Location method unavailable'}`));
  appendPhotos(card, item, incident);
  const url = safeSourceUrl(item.source_uri);
  if (url) {
    const link = text('a', 'Open source ↗');
    link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; card.append(link);
  }
  return card;
}

function renderReportActivity(detail, incident) {
  const activity = reportActivity(incident);
  const section = text('section', '', 'evidence');
  section.setAttribute('aria-label', 'Report activity by network');
  section.append(text('h4', `Report activity: ${activity.level}`));
  for (const network of activity.networks) {
    const row = text('p', '');
    row.append(platformBadge(network.platform), text('span', ` · ${network.count} distinct reports · ${network.level}`));
    section.append(row);
  }
  if (!activity.networks.length) section.append(text('p', 'Network counts are not available for this publication.'));
  section.append(text('p', 'Counts use each network’s configured time window and activity thresholds. They do not measure severity, independent corroboration or human confirmation.', 'metadata'));
  detail.append(section);
}

function renderDetail() {
  const detail = $('detail');
  const item = snapshot.incidents.find((incident) => incident.id === selectedId);
  const previousNote = detail.dataset.incidentId === selectedId ? detail.querySelector('textarea')?.value : undefined;
  detail.dataset.incidentId = selectedId || '';
  detail.replaceChildren();
  if (!item) { detail.append(text('p', 'Select an event on the map or list to inspect its evidence.')); return; }
  detail.append(modeBadge(item.mode), text('h3', item.title || categoryName(item.category)), text('p', item.summary), text('p', `${displayLocality(item.locality)} · Severity: ${displaySeverity(item.severity)} · Classification confidence: ${item.confidence} · ${reviewName(item.review_status)}`, 'metadata'));
  if (Number.isFinite(item.corroboration_score) && item.corroboration_score >= 0 && item.corroboration_score <= 100) detail.append(text('p', `Corroboration index: ${Math.round(item.corroboration_score)}/100 · ${item.independent_source_count ?? 'Unknown number of'} independent sources. This is a source agreement rule, not a probability or confirmation.`, 'metadata'));
  renderReportActivity(detail, item);
  if (!Number.isFinite(item.lat) || !Number.isFinite(item.lon)) detail.append(text('p', 'Unresolved location: this event remains in the list without an invented map position.', 'metadata'));
  else if (['text_locality_centroid', 'text_locality_anchor'].includes(item.location_method)) detail.append(text('p', 'Approximate location within the locality, inferred from text. It is not an exact address.', 'metadata'));
  const evidence = evidenceFor(snapshot, item);
  detail.append(text('h4', `Linked evidence (${evidence.length})`));
  for (const record of evidence) detail.append(evidenceCard(record, item));
  if (!evidence.length) detail.append(text('p', 'No linked evidence. Check the source before validating.'));
  if (snapshot.can_review === false) { detail.append(text('p', 'Read-only access · an administrator can validate this event.', 'metadata')); return; }
  const form = document.createElement('form');
  form.className = 'review';
  const noteLabel = text('label', 'Review note');
  const note = document.createElement('textarea');
  note.maxLength = 1000; note.rows = 2; note.value = previousNote ?? item.review_note ?? ''; noteLabel.append(note);
  const status = text('p', '', 'metadata'); status.setAttribute('role', 'status');
  const actions = text('div', '', 'review-actions');
  for (const [value, title] of [['validated', 'Validate'], ['rejected', 'Reject'], ['pending', 'Pending']]) {
    const button = text('button', title); button.type = 'button';
    button.addEventListener('click', async () => {
      actions.querySelectorAll('button').forEach((control) => { control.disabled = true; });
      try {
        await request(`/api/territorial/incidents/${encodeURIComponent(item.id)}/review`, { method: 'POST', body: JSON.stringify({ status: value, note: note.value, expected_evidence_ids: item.evidence_ids }) });
        status.textContent = 'Review saved.';
        await refresh();
      } catch (error) { status.textContent = error.message; }
      finally { actions.querySelectorAll('button').forEach((control) => { control.disabled = false; }); }
    });
    actions.append(button);
  }
  form.append(noteLabel, actions, status); detail.append(form);
}

function updateFilterOptions() {
  for (const key of FILTER_KEYS.filter((value) => !['mode', 'bbox', ...DATE_KEYS].includes(value))) {
    const select = filtersForm.elements.namedItem(key);
    const selected = select.value;
    const source = key === 'platform' ? snapshot.evidence : snapshot.incidents;
    const values = [...new Set(source.map((item) => String(item[key] ?? '')).filter(Boolean))].sort();
    const display = { category: categoryName, locality: displayLocality, severity: displaySeverity }[key] || ((value) => value);
    select.replaceChildren(new Option('All', ''), ...values.map((value) => new Option(display(value), value)));
    if (values.includes(selected)) select.value = selected;
  }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const next = validateSnapshot(await request('/api/territorial/snapshot'));
    const changed = next.version !== snapshot.version;
    snapshot = next;
    const adminLink = document.querySelector('.admin-link');
    const portal = snapshot.can_admin === false ? '/local/gods-eye-view/workspace' : '/admin/gods-eye-view';
    adminLink.href = portal;
    adminLink.textContent = snapshot.can_admin === false ? 'Participant workspace ↗' : 'Manage sources ↗';
    document.querySelector('.brand').href = portal;
    $('runtime').textContent = 'Active publication';
    $('published').textContent = `Published ${date(snapshot.published_at)}`;
    $('connection').textContent = 'Refresh every 10 s · Bogotá time';
    $('connection').classList.remove('error');
    if (changed) { updateFilterOptions(); renderIncidents(); renderDetail(); }
  } catch (error) {
    $('connection').textContent = `${error.message} ${snapshot.version ? 'The last publication remains visible and may be outdated.' : ''}`;
    $('connection').classList.add('error');
  } finally { refreshing = false; }
}

function renderReply(reply, question) {
  const basis = turnSnapshot;
  const card = text('article', '', 'reply');
  const cited = basis.evidence.filter((item) => reply.evidence_ids?.includes(item.id));
  card.append(text('strong', question));
  if (!cited.length) card.append(text('span', 'NO EVIDENCE', 'badge'));
  card.append(text('small', `Publication · ${date(reply.published_at)}`), text('p', reply.answer));
  if (cited.length) {
    const disclosure = document.createElement('details');
    disclosure.append(text('summary', `Inspect ${cited.length} evidence items`));
    for (const item of cited) disclosure.append(evidenceCard(item));
    card.append(disclosure);
  }
  for (const action of allowedActions(reply.actions, basis)) {
    const button = text('button', action.type === 'focus_incident' ? 'Focus event on map' : 'Apply suggested filters');
    button.type = 'button';
    button.addEventListener('click', () => {
      if (snapshot.version !== reply.version) { $('chat-status').textContent = 'The publication changed. Ask again before applying this action.'; return; }
      if (action.type === 'focus_incident') {
        if (!filteredIncidents(snapshot, filters()).some((item) => item.id === action.incident_id)) clearFilters();
        renderIncidents(); selectIncident(action.incident_id);
      } else {
        for (const [key, value] of Object.entries(action.filters)) filtersForm.elements.namedItem(key).value = DATE_KEYS.includes(key) ? utcToBogota(value) : value;
        renderIncidents();
      }
    });
    card.append(button);
  }
  $('conversation').append(card);
  card.scrollIntoView({ block: 'nearest' });
}

const session = createTerritorialSession({ request,
  context: () => ({ version: turnSnapshot.version, incident_id: selectedId, filters: filters() }),
  onReply: renderReply,
  onSubmitted: (count) => { $('question-count').textContent = `${count} ${count === 1 ? 'question' : 'questions'} submitted`; },
  onBusy: (busy) => {
    $('send').disabled = busy;
    $('send').setAttribute('aria-busy', String(busy));
    $('send').setAttribute('aria-label', busy ? 'Sending question' : 'Send question');
    $('cancel').hidden = !busy;
    $('chat-status').textContent = busy ? 'Asking agent…' : '';
  },
  onError: (error) => {
    const message = error.status === 409 ? 'The publication changed. Refresh the context and ask again; your question was preserved.' : error.message;
    const card = text('p', message, 'error'); card.setAttribute('role', 'alert'); $('conversation').append(card);
    if (error.status === 409) void refresh();
  },
});
$('chat').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!snapshot.version) { $('chat-status').textContent = 'Wait for a valid publication before asking.'; return; }
  const question = $('question').value.trim();
  if (!question) return;
  try {
    if (!filtersForm.reportValidity() || !validPeriod(filters())) { $('chat-status').textContent = 'Correct the period before asking.'; return; }
  } catch (error) { $('chat-status').textContent = error.message; return; }
  turnSnapshot = snapshot;
  await session.start();
  void session.sendText(question);
});
$('cancel').addEventListener('click', () => { session.stop(); $('chat-status').textContent = 'Request cancelled.'; });
document.querySelectorAll('.examples button').forEach((button) => button.addEventListener('click', () => { $('question').value = button.textContent; $('question').focus(); }));
filtersForm.addEventListener('change', renderIncidents);
$('clear-filters').addEventListener('click', () => { clearFilters(); renderIncidents(); });
$('home').addEventListener('click', home);
$('clear-area').addEventListener('click', () => { filtersForm.elements.namedItem('bbox').value = ''; renderIncidents(); });
$('satellite-date').value = nasaDate();
$('satellite-date').max = new Date().toISOString().slice(0, 10);
function updateSatellite() {
  if (!viewer) return;
  if (satelliteLayer) { viewer.imageryLayers.remove(satelliteLayer); satelliteLayer = undefined; }
  if (!$('satellite').checked) { $('satellite-status').textContent = 'Daily imagery; not a live camera. Clouds can obscure the ground.'; return; }
  try {
    const day = $('satellite-date').value;
    const provider = new Cesium.UrlTemplateImageryProvider({ url: nasaUrl(day), maximumLevel: 9, credit: 'NASA GIBS · MODIS Terra' });
    provider.errorEvent.addEventListener(() => { $('satellite-status').textContent = `NASA imagery unavailable for ${day}. Choose another date.`; });
    satelliteLayer = viewer.imageryLayers.addImageryProvider(provider);
    satelliteLayer.alpha = 0.7;
    $('satellite-status').textContent = `NASA MODIS · ${day} · daily observation, not live · clouds may obscure the ground`;
  } catch (error) { $('satellite-status').textContent = error.message; }
}
$('satellite').addEventListener('change', updateSatellite);
$('satellite-date').addEventListener('change', updateSatellite);
$('filter-area').addEventListener('click', () => {
  if (!viewer) return;
  const view = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid);
  if (!view) { $('area-status').textContent = 'Zoom towards Bogotá before selecting an area.'; return; }
  const degrees = [view.west, view.south, view.east, view.north].map(Cesium.Math.toDegrees);
  const bbox = degrees.map((value, index) => (index < 2 ? Math.floor(value * 1e6) : Math.ceil(value * 1e6)) / 1e6).join(',');
  try { parseBbox(bbox); }
  catch (error) { $('area-status').textContent = error.message; return; }
  filtersForm.elements.namedItem('bbox').value = bbox;
  renderIncidents();
});

try {
  Cesium.Ion.defaultAccessToken = '';
  viewer = createApplicationViewer({ container: $('map'), creditContainer: $('map-credits') });
  viewer.scene.globe.show = true;
  viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString('#183d47');
  const imagery = new Cesium.OpenStreetMapImageryProvider({ url: 'https://tile.openstreetmap.org/' });
  observeImagery(imagery, ({ loaded, failed }) => {
    $('map').dataset.tilesLoaded = String(loaded);
    $('map').dataset.tilesFailed = String(failed);
    $('map').dataset.cartography = loaded ? 'loaded' : 'unavailable';
    $('map-status').textContent = loaded ? (failed ? 'Map available; some tiles failed to load.' : '') : 'Map tiles are unavailable. The event list and assistant remain available.';
  });
  viewer.imageryLayers.addImageryProvider(imagery);
  const removePinch = installTrackpadPinchZoom(viewer);
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  handler.setInputAction((click) => { const picked = viewer.scene.pick(click.position); if (picked?.id?.id) selectIncident(picked.id.id, false); }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
  viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(-74.085, 4.66, 38000) });
  const reportCamera = () => {
    const position = viewer.camera.positionCartographic;
    $('map').dataset.longitude = String(Cesium.Math.toDegrees(position.longitude));
    $('map').dataset.latitude = String(Cesium.Math.toDegrees(position.latitude));
  };
  reportCamera(); viewer.camera.moveEnd.addEventListener(reportCamera);
  window.addEventListener('pagehide', () => { handler.destroy(); removePinch(); viewer.destroy(); }, { once: true });
} catch (error) {
  viewer = undefined;
  $('map-status').textContent = `This browser cannot display the 3D map. Use the event list and evidence. ${error.message}`;
  $('terrain').disabled = true;
  $('filter-area').disabled = true;
  $('satellite').disabled = true;
}
$('terrain').addEventListener('change', async () => {
  if (!viewer) return;
  const generation = ++terrainRequest;
  if (!$('terrain').checked) { viewer.terrainProvider = new Cesium.EllipsoidTerrainProvider(); $('map-status').textContent = ''; return; }
  $('map-status').textContent = 'Loading Re:Earth / Mapterhorn terrain…';
  const { provider } = await createKeylessTerrain();
  if (generation !== terrainRequest || viewer.isDestroyed()) return;
  viewer.terrainProvider = provider;
  $('map-status').textContent = provider instanceof Cesium.EllipsoidTerrainProvider ? 'Terrain no disponible; se mantiene el mapa plano.' : 'Terrain Re:Earth / Mapterhorn · CC BY 4.0';
});
void refresh();
void loadContext(request);
const polling = setInterval(() => { if (!document.hidden) void refresh(); }, 60000);
window.addEventListener('pagehide', () => { clearInterval(polling); session.destroy(); }, { once: true });
