import * as Cesium from 'cesium';
import 'cesium/Build/Cesium/Widgets/widgets.css';
import './style.css';
import { createApplicationViewer, installTrackpadPinchZoom } from '../vendor/gods-eye-view/src/app/viewer.js';
import { createKeylessTerrain } from '../vendor/gods-eye-view/src/maps/terrain.js';
import { allowedActions, bogotaToUtc, DATE_KEYS, evidenceFor, filteredIncidents, FILTER_KEYS, modeLabel, safeSourceUrl, utcToBogota, validPeriod, validateSnapshot } from './model.js';
import { createPrismaSession } from './chat.js';

const $ = (id) => document.getElementById(id);
const filtersForm = $('filters');
let snapshot = { version: '', incidents: [], evidence: [] };
let selectedId;
let viewer;
let refreshing = false;
let turnSnapshot;
let terrainRequest = 0;
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
const date = (value) => value ? new Date(value).toLocaleString('es-CO', { timeZone: 'America/Bogota' }) : 'Sin fecha';
const severityColor = (value) => /critical|critica|crítica|alta|high|^4$|^5$/i.test(String(value)) ? '#fb786e' : '#efbe64';

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...options.headers },
    signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(110000)]) : AbortSignal.timeout(15000),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(response.status === 401 ? 'La sesión expiró. Ingresa desde la administración.' : typeof data.detail === 'string' ? data.detail : `Servicio no disponible (${response.status}).`);
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
  $('period-status').textContent = validPeriod(activeFilters) ? 'Período inclusivo por fecha de creación de la alerta.' : 'La fecha Desde no puede ser posterior a Hasta.';
  const items = filteredIncidents(snapshot, activeFilters);
  if (selectedId && !items.some((item) => item.id === selectedId)) { selectedId = undefined; renderDetail(); }
  $('count').textContent = String(items.length);
  $('incidents').replaceChildren();
  if (!items.length) $('incidents').append(text('p', 'No hay alertas que coincidan con estos filtros.', 'empty'));
  for (const item of items) {
    const row = text('button', '', `incident ${item.id === selectedId ? 'selected' : ''}`);
    row.type = 'button';
    row.setAttribute('aria-pressed', String(item.id === selectedId));
    const header = text('span', '', 'incident-header');
    header.append(modeBadge(item.mode), text('span', `${item.severity} · ${item.locality}`));
    row.append(header, text('strong', item.title || item.category), text('small', `${item.category} · ${item.evidence_ids.length} evidencias · ${item.review_status}`));
    row.addEventListener('click', () => selectIncident(item.id));
    $('incidents').append(row);
  }
  if (!viewer) return;
  viewer.entities.removeAll();
  for (const item of items) {
    if (!Number.isFinite(item.lat) || !Number.isFinite(item.lon)) continue;
    viewer.entities.add({ id: item.id, position: Cesium.Cartesian3.fromDegrees(item.lon, item.lat),
      point: { pixelSize: item.id === selectedId ? 17 : 11, color: Cesium.Color.fromCssColorString(severityColor(item.severity)), outlineColor: Cesium.Color.WHITE, outlineWidth: item.id === selectedId ? 3 : 1, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY },
      label: { text: `${modeLabel(item.mode)} · ${item.locality}`, font: '12px sans-serif', fillColor: Cesium.Color.WHITE, showBackground: true, backgroundColor: Cesium.Color.fromCssColorString('#10252de6'), pixelOffset: new Cesium.Cartesian2(0, -25), heightReference: Cesium.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY, distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 70000) },
    });
  }
}

function evidenceCard(item) {
  const card = text('article', '', 'evidence');
  const header = text('div', '', 'evidence-header');
  header.append(modeBadge(item.mode), text('strong', item.platform), text('small', item.id));
  card.append(header, text('p', item.text), text('small', `${date(item.observed_at || item.created_at)} · ${item.location_method || 'Ubicación sin método informado'}`));
  const url = safeSourceUrl(item.source_uri);
  if (url) {
    const link = text('a', 'Abrir fuente ↗');
    link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; card.append(link);
  }
  return card;
}

function renderDetail() {
  const detail = $('detail');
  const item = snapshot.incidents.find((incident) => incident.id === selectedId);
  const previousNote = detail.dataset.incidentId === selectedId ? detail.querySelector('textarea')?.value : undefined;
  detail.dataset.incidentId = selectedId || '';
  detail.replaceChildren();
  if (!item) { detail.append(text('p', 'Selecciona una señal en el mapa o en la lista para revisar su evidencia.')); return; }
  detail.append(modeBadge(item.mode), text('h3', item.title || item.category), text('p', item.summary), text('p', `${item.locality} · Criticidad: ${item.severity} · Confianza: ${item.confidence} · ${item.review_status}`, 'metadata'));
  if (!Number.isFinite(item.lat) || !Number.isFinite(item.lon)) detail.append(text('p', 'Ubicación sin resolver: esta alerta permanece en la lista, sin un punto inventado en el mapa.', 'metadata'));
  else if (['text_locality_centroid', 'text_locality_anchor'].includes(item.location_method)) detail.append(text('p', 'Ubicación aproximada dentro de la localidad, inferida del texto. No representa una dirección exacta.', 'metadata'));
  const evidence = evidenceFor(snapshot, item);
  detail.append(text('h4', `Evidencia vinculada (${evidence.length})`));
  for (const record of evidence) detail.append(evidenceCard(record));
  if (!evidence.length) detail.append(text('p', 'Sin evidencia vinculada. No validar sin contrastar la fuente.'));
  const form = document.createElement('form');
  form.className = 'review';
  const noteLabel = text('label', 'Nota de revisión');
  const note = document.createElement('textarea');
  note.maxLength = 1000; note.rows = 2; note.value = previousNote ?? item.review_note ?? ''; noteLabel.append(note);
  const status = text('p', '', 'metadata'); status.setAttribute('role', 'status');
  const actions = text('div', '', 'review-actions');
  for (const [value, title] of [['validated', 'Validar'], ['rejected', 'Descartar'], ['pending', 'Pendiente']]) {
    const button = text('button', title); button.type = 'button';
    button.addEventListener('click', async () => {
      actions.querySelectorAll('button').forEach((control) => { control.disabled = true; });
      try {
        await request(`/api/prisma/incidents/${encodeURIComponent(item.id)}/review`, { method: 'POST', body: JSON.stringify({ status: value, note: note.value }) });
        status.textContent = 'Revisión guardada.';
        await refresh();
      } catch (error) { status.textContent = error.message; }
      finally { actions.querySelectorAll('button').forEach((control) => { control.disabled = false; }); }
    });
    actions.append(button);
  }
  form.append(noteLabel, actions, status); detail.append(form);
}

function updateFilterOptions() {
  for (const key of FILTER_KEYS.filter((value) => value !== 'mode' && !DATE_KEYS.includes(value))) {
    const select = filtersForm.elements.namedItem(key);
    const selected = select.value;
    const source = key === 'platform' ? snapshot.evidence : snapshot.incidents;
    const values = [...new Set(source.map((item) => String(item[key] ?? '')).filter(Boolean))].sort();
    select.replaceChildren(new Option('Todas', ''), ...values.map((value) => new Option(value, value)));
    if (values.includes(selected)) select.value = selected;
  }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const next = validateSnapshot(await request('/api/prisma/snapshot'));
    const changed = next.version !== snapshot.version;
    snapshot = next;
    $('runtime').textContent = snapshot.runtime === 'aidp' ? 'AIDP · Publicación activa' : 'DEMOSTRACIÓN · Motor local';
    $('published').textContent = `Publicado ${date(snapshot.published_at)}`;
    $('connection').textContent = `Actualización cada 10 s · ${snapshot.simulation?.status || 'Fuentes activas'} · Hora Bogotá`;
    $('connection').classList.remove('error');
    if (changed) { updateFilterOptions(); renderIncidents(); renderDetail(); }
  } catch (error) {
    $('connection').textContent = `${error.message} ${snapshot.version ? 'Se conserva la última publicación; puede estar desactualizada.' : ''}`;
    $('connection').classList.add('error');
  } finally { refreshing = false; }
}

function renderReply(reply, question) {
  const basis = turnSnapshot;
  const card = text('article', '', 'reply');
  const cited = basis.evidence.filter((item) => reply.evidence_ids?.includes(item.id));
  card.append(text('strong', question));
  const modes = [...new Set(cited.map((item) => item.mode))];
  for (const mode of modes) card.append(modeBadge(mode));
  if (!modes.length) card.append(text('span', 'SIN EVIDENCIA', 'badge'));
  card.append(text('small', `${reply.runtime === 'aidp' ? 'Agente AIDP' : 'Demostración local'} · ${date(reply.published_at)}`), text('p', reply.answer));
  if (cited.length) {
    const disclosure = document.createElement('details');
    disclosure.append(text('summary', `Consultar ${cited.length} evidencias`));
    for (const item of cited) disclosure.append(evidenceCard(item));
    card.append(disclosure);
  }
  for (const action of allowedActions(reply.actions, basis)) {
    const button = text('button', action.type === 'focus_incident' ? 'Ver alerta en el mapa' : 'Aplicar filtros sugeridos');
    button.type = 'button';
    button.addEventListener('click', () => {
      if (snapshot.version !== reply.version) { $('chat-status').textContent = 'La publicación cambió. Repite la consulta antes de aplicar esta acción.'; return; }
      if (action.type === 'focus_incident') {
        if (!filteredIncidents(snapshot, filters()).some((item) => item.id === action.incident_id)) filtersForm.reset();
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

const session = createPrismaSession({ request,
  context: () => ({ version: turnSnapshot.version, incident_id: selectedId, filters: filters() }),
  onReply: renderReply,
  onBusy: (busy) => { $('send').disabled = busy; $('cancel').hidden = !busy; $('chat-status').textContent = busy ? 'Consultando…' : ''; },
  onError: (error) => {
    const message = error.status === 409 ? 'La publicación cambió. Actualiza el contexto y vuelve a consultar; conservamos tu pregunta.' : error.message;
    const card = text('p', message, 'error'); card.setAttribute('role', 'alert'); $('conversation').append(card);
    if (error.status === 409) void refresh();
  },
});
$('chat').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!snapshot.version) { $('chat-status').textContent = 'Espera una publicación válida antes de consultar.'; return; }
  const question = $('question').value.trim();
  if (!question) return;
  try {
    if (!filtersForm.reportValidity() || !validPeriod(filters())) { $('chat-status').textContent = 'Corrige el período antes de consultar.'; return; }
  } catch (error) { $('chat-status').textContent = error.message; return; }
  turnSnapshot = snapshot;
  await session.start();
  void session.sendText(question);
});
$('cancel').addEventListener('click', () => { session.stop(); $('chat-status').textContent = 'Consulta cancelada.'; });
document.querySelectorAll('.examples button').forEach((button) => button.addEventListener('click', () => { $('question').value = button.textContent; $('question').focus(); }));
filtersForm.addEventListener('change', renderIncidents);
$('clear-filters').addEventListener('click', () => { filtersForm.reset(); renderIncidents(); });
$('home').addEventListener('click', home);

try {
  Cesium.Ion.defaultAccessToken = '';
  viewer = createApplicationViewer({ container: $('map'), creditContainer: $('map-credits') });
  viewer.scene.globe.show = true;
  viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString('#183d47');
  const imagery = new Cesium.OpenStreetMapImageryProvider({ url: 'https://tile.openstreetmap.org/' });
  imagery.errorEvent.addEventListener(() => { $('map-status').textContent = 'Cartografía temporalmente no disponible. La lista y las consultas siguen disponibles.'; });
  viewer.imageryLayers.addImageryProvider(imagery);
  const removePinch = installTrackpadPinchZoom(viewer);
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  handler.setInputAction((click) => { const picked = viewer.scene.pick(click.position); if (picked?.id?.id) selectIncident(picked.id.id, false); }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
  home();
  window.addEventListener('pagehide', () => { handler.destroy(); removePinch(); viewer.destroy(); }, { once: true });
} catch (error) {
  viewer = undefined;
  $('map-status').textContent = `El mapa 3D no está disponible en este navegador. Usa la lista y las evidencias. ${error.message}`;
  $('terrain').disabled = true;
}
$('terrain').addEventListener('change', async () => {
  if (!viewer) return;
  const generation = ++terrainRequest;
  if (!$('terrain').checked) { viewer.terrainProvider = new Cesium.EllipsoidTerrainProvider(); $('map-status').textContent = ''; return; }
  $('map-status').textContent = 'Cargando relieve Re:Earth / Mapterhorn…';
  const { provider } = await createKeylessTerrain();
  if (generation !== terrainRequest || viewer.isDestroyed()) return;
  viewer.terrainProvider = provider;
  $('map-status').textContent = provider instanceof Cesium.EllipsoidTerrainProvider ? 'Relieve no disponible; se mantiene el mapa plano.' : 'Relieve Re:Earth / Mapterhorn · CC BY 4.0';
});
void refresh();
const polling = setInterval(() => { if (!document.hidden) void refresh(); }, 10000);
window.addEventListener('pagehide', () => { clearInterval(polling); session.destroy(); }, { once: true });
