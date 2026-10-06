import { bogotaToUtc, displayLocality, displaySeverity, evidenceFor, filteredIncidents, modeLabel, parseBbox, photosFor, safeSourceUrl, utcToBogota, validPeriod, validateSnapshot } from '../src/model.js';
import { bindPanelDisclosure, collapsePanelOnEscape } from '../.upstream/src/ui/panelDisclosure.js';
import { createSceneDialog } from '../.upstream/src/ui/sceneSharing.js';
import { isOverlayPointVisible } from '../.upstream/src/overlays/worldOverlay.js';
import { placementVariants } from '../.upstream/src/overlays/worldOverlayDraw.js';
import { WORLD_OVERLAY_STYLE } from '../.upstream/src/overlays/worldOverlayTokens.js';
import { pickWorldFromScreen } from '../.upstream/src/annotations/annotationResolver.js';
import { claimPointer, isPointerFree, releasePointer } from '../.upstream/src/data/inputOwnership.js';
import { viewerApiPath } from '../src/api.js';

export const GODS_EYE_VIEW_LAYER_ID = 'gods-eye-view-events';
export function seedBogotaView(location, history) {
  if (location.hash) return false;
  const url = new URL(location.href);
  url.hash = 'lat=4.66&lon=-74.085&alt=38000&heading=0&pitch=-90&roll=0';
  history.replaceState(history.state, '', url.href);
  return true;
}

export const categoryName = (value) => ({ inundacion: 'Flooding', incendio: 'Fire', movimiento_masa: 'Landslide', infraestructura: 'Infrastructure', lluvia: 'Rainfall' }[value] || value);
const SOCIAL_NETWORKS = { x: 'X', facebook: 'Facebook', instagram: 'Instagram', tiktok: 'TikTok' };
export const platformName = (value) => SOCIAL_NETWORKS[value] || value;
export const text = (tag, value, className) => {
  const node = document.createElement(tag);
  node.textContent = value ?? '';
  if (className) node.className = className;
  return node;
};

function drawGodsEyeViewPoints(viewer, dataSource, rows, selectedId, Cesium) {
  dataSource.entities.removeAll();
  for (const item of rows) {
    if (!Number.isFinite(item.lat) || !Number.isFinite(item.lon)) continue;
    dataSource.entities.add({
        id: `gods-eye-view:${item.id}`, name: item.title || categoryName(item.category),
        position: Cesium.Cartesian3.fromDegrees(item.lon, item.lat),
        point: { pixelSize: item.id === selectedId ? 17 : 11,
          color: Cesium.Color.fromCssColorString(item.severity === 'high' ? '#ff796f' : '#ffc86b'),
          outlineColor: Cesium.Color.WHITE, outlineWidth: item.id === selectedId ? 3 : 1,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Infinity },
    });
  }
  viewer.scene.requestRender();
}

const recentPeriod = () => {
  const end = Math.floor(Date.now() / 1000) * 1000;
  return { date_from: new Date(end - 24 * 60 * 60 * 1000).toISOString(), date_to: new Date(end).toISOString() };
};

async function refreshPublication(feed, request, lifetime, operationSignal, filters) {
  if (!feed.enabled) return false;
  feed.pending?.abort();
  const turn = new AbortController(); feed.pending = turn;
  const signal = AbortSignal.any([turn.signal, ...[lifetime, operationSignal].filter(Boolean)]);
  try {
    const query = new URLSearchParams(Object.entries(filters).filter(([key, value]) => key.startsWith('date_') && value));
    const next = validateSnapshot(await request(`/api/gods-eye-view/snapshot?${query}`, { signal }));
    if (signal.aborted || feed.pending !== turn || !feed.enabled) return false;
    feed.snapshot = next; feed.lastError = null; feed.lastUpdate = Date.now();
    return true;
  } catch (error) {
    if (!signal.aborted) feed.lastError = error.message;
    return false;
  } finally { if (feed.pending === turn) feed.pending = undefined; }
}

function anchorEventPopup(viewer, Cesium, popup, state) {
  const scene = viewer.scene;
  const occluder = new Cesium.EllipsoidalOccluder(scene.globe.ellipsoid, viewer.camera.positionWC);
  const panels = ['left-panel-stack', 'right-context-rail', 'top-center-actions', 'command-dock'].map((id) => document.getElementById(id));
  viewer.container.append(popup);
  const update = () => {
    const current = state(), incident = current.incident;
    if (!current.enabled || !Number.isFinite(incident?.lat) || !Number.isFinite(incident?.lon) || popup.dataset.closedId === incident.id) { popup.hidden = true; return; }
    const location = Cesium.Cartographic.fromDegrees(incident.lon, incident.lat);
    const position = Cesium.Cartesian3.fromDegrees(incident.lon, incident.lat, scene.globe.getHeight(location) || 0);
    const screen = scene.cartesianToCanvasCoordinates(position);
    occluder.cameraPosition = viewer.camera.positionWC;
    if (!isOverlayPointVisible({ horizonCull: scene.mode === Cesium.SceneMode.SCENE3D }, position, screen,
      { width: scene.canvas.clientWidth, height: scene.canvas.clientHeight }, occluder)) { popup.hidden = true; return; }
    popup.hidden = false;
    const canvasRect = scene.canvas.getBoundingClientRect(), containerRect = viewer.container.getBoundingClientRect();
    const x = screen.x + canvasRect.left - containerRect.left, y = screen.y + canvasRect.top - containerRect.top;
    const width = viewer.container.clientWidth, height = viewer.container.clientHeight;
    const [leftPanel, rightPanel, topPanel, dock] = panels.map((panel) => panel?.getBoundingClientRect());
    let left = leftPanel?.height > 0 ? Math.max(0, leftPanel.right - containerRect.left) : 0;
    let right = rightPanel?.height > 0 ? Math.min(width, rightPanel.left - containerRect.left) : width;
    const top = topPanel?.height > 0 ? Math.max(0, topPanel.bottom - containerRect.top) : 0;
    const bottom = dock?.height > 0 ? Math.min(height, dock.top - containerRect.top) : height;
    const compact = right - left < 220;
    // ponytail: when both rails fill a narrow screen, keep controls usable above them instead of shrinking the card to unreadable text.
    if (compact) { left = 0; right = width; }
    popup.style.zIndex = compact ? '120' : '';
    popup.style.width = `${Math.max(0, Math.min(360, right - left - 16))}px`;
    popup.style.maxHeight = `${Math.max(0, Math.min(480, bottom - top - 16, Math.max(y - top, bottom - y) - 26))}px`;
    const [placement] = placementVariants({ anchorX: x - left, anchorY: y - top, width: popup.offsetWidth, height: popup.offsetHeight,
      viewportWidth: right - left, viewportHeight: bottom - top, gap: 18, preferred: y - top >= bottom - y ? 'above' : 'below', verticalOnly: true, viewportMargin: 8 });
    popup.style.left = `${left + placement.rect.x}px`; popup.style.top = `${top + placement.rect.y}px`; popup.dataset.placement = placement.corner;
    popup.style.setProperty('--gev-anchor-x', `${Math.max(12, Math.min(x - left - placement.rect.x, popup.offsetWidth - 12))}px`);
  };
  const removeRender = scene.postRender.addEventListener(update);
  const resize = new ResizeObserver(update); resize.observe(popup); resize.observe(viewer.container);
  for (const panel of panels.filter(Boolean)) resize.observe(panel);
  return { update, destroy() { removeRender(); resize.disconnect(); popup.remove(); } };
}

/** A normal native catalog layer. Only this layer's data source is ever cleared. */
export function createGodsEyeViewLayer({ Cesium, request, signal }) {
  let viewer, dataSource, picking, settleRender, popup, popupAnchor, drag;
  let filters = recentPeriod(), selectedId, rollingPeriod = true;
  const locationDrafts = new Map(), reviewLocks = new Set(), lifetime = new AbortController();
  const feed = { snapshot: { version: '', incidents: [], evidence: [] }, enabled: false, lastError: null, lastUpdate: null, pending: undefined };
  const listeners = new Set();
  const items = () => filteredIncidents(feed.snapshot, filters).map((item) => ({ ...item, ...locationDrafts.get(item.id) }));
  const canMove = (id) => {
    const incident = feed.snapshot.incidents.find((item) => item.id === id);
    return !!incident && feed.enabled && !!feed.snapshot.can_review && !reviewLocks.has(id) && incident.review_status !== 'validated';
  };
  const state = () => ({ snapshot: feed.snapshot, filters: { ...filters }, selectedId, enabled: feed.enabled, isRefreshing: !!feed.pending, lastError: feed.lastError, lastUpdate: feed.lastUpdate, items: items() });
  const notify = () => { for (const listener of listeners) listener(state()); popupAnchor?.update(); };
  function paint() {
    if (!dataSource) return;
    const rows = items();
    if (selectedId && !rows.some((item) => item.id === selectedId)) selectedId = undefined;
    if (drag && drag.id !== selectedId) endDrag();
    drawGodsEyeViewPoints(viewer, dataSource, rows, selectedId, Cesium);
    // Clamped points enqueue their canvas texture after the first frame; idle Cesium needs one more.
    settleRender ||= viewer.scene.postRender.addEventListener(() => {
      settleRender(); settleRender = undefined; viewer.scene.requestRender();
    });
  }

  function endDrag(cancel = false) {
    if (!drag) return;
    const previous = drag; drag = undefined;
    viewer.scene.screenSpaceCameraController.enableInputs = previous.cameraInputs; releasePointer(previous.lease);
    if (cancel && canMove(previous.id)) layer.setLocation(previous.id, previous.lat, previous.lon);
  }

  function select(id, focus = true) {
    const item = items().find((row) => row.id === id);
    if (!item) return false;
    if (popup) popup.dataset.closedId = '';
    selectedId = id;
    paint(); notify();
    if (popup && !popup.hidden) popup.focus({ preventScroll: true });
    if (focus && Number.isFinite(item.lat) && Number.isFinite(item.lon)) {
      viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(item.lon, item.lat, 7000), duration: 1 });
    }
    return true;
  }

  const layer = {
    id: GODS_EYE_VIEW_LAYER_ID, name: 'Social networks', icon: '◉', source: 'OCI AIDP · Workflow', updateInterval: 0,
    init(target) {
      viewer = target;
      dataSource = new Cesium.CustomDataSource(GODS_EYE_VIEW_LAYER_ID);
      dataSource.show = false;
      viewer.dataSources.add(dataSource);
      picking = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
      picking.setInputAction(({ position }) => {
        if (!isPointerFree()) return;
        const entity = viewer.scene.pick(position)?.id;
        if (typeof entity?.id === 'string' && entity.id.startsWith('gods-eye-view:')) select(entity.id.slice('gods-eye-view:'.length), false);
      }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
      picking.setInputAction(({ position }) => {
        const entity = viewer.scene.pick(position)?.id;
        if (entity?.id !== `gods-eye-view:${selectedId}` || !canMove(selectedId)) return;
        const lease = claimPointer(GODS_EYE_VIEW_LAYER_ID); if (!lease) return;
        const item = items().find((entry) => entry.id === selectedId);
        drag = { id: item.id, lat: item.lat, lon: item.lon, lease, cameraInputs: viewer.scene.screenSpaceCameraController.enableInputs };
        viewer.scene.screenSpaceCameraController.enableInputs = false;
      }, Cesium.ScreenSpaceEventType.LEFT_DOWN);
      picking.setInputAction(({ endPosition }) => {
        if (!drag) return;
        if (!canMove(drag.id)) { endDrag(); return; }
        const point = pickWorldFromScreen(viewer, endPosition.x / viewer.scene.canvas.clientWidth, endPosition.y / viewer.scene.canvas.clientHeight);
        if (point) layer.setLocation(drag.id, Number(point.lat.toFixed(6)), Number(point.lon.toFixed(6)));
      }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
      picking.setInputAction(() => endDrag(), Cesium.ScreenSpaceEventType.LEFT_UP);
      globalThis.document?.addEventListener?.('keydown', (event) => { if (drag && event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); endDrag(true); } }, { signal: lifetime.signal, capture: true });
      for (const type of ['pointerup', 'pointercancel']) globalThis.document?.addEventListener?.(type, () => endDrag(), { signal: lifetime.signal, capture: true });
      globalThis.window?.addEventListener?.('blur', () => endDrag(), { signal: lifetime.signal });
      if (popup) layer.attachPopup(popup);
    },
    enable() { feed.enabled = true; dataSource.show = true; notify(); },
    disable() { endDrag(); feed.enabled = false; feed.pending?.abort(); dataSource.show = false; viewer.scene.requestRender(); notify(); },
    async update(_viewer, { signal: operationSignal } = {}) {
      if (rollingPeriod) filters = { ...filters, ...recentPeriod() };
      const refresh = refreshPublication(feed, request, signal, operationSignal, filters); notify();
      const updated = await refresh;
      if (updated) {
        for (const [id, point] of locationDrafts) {
          const saved = feed.snapshot.incidents.find((item) => item.id === id);
          if (saved?.review_status === 'validated' || (saved?.lat === point.lat && saved?.lon === point.lon)) locationDrafts.delete(id);
        }
        if (drag && !canMove(drag.id)) endDrag();
        paint();
      }
      notify(); return updated;
    },
    destroy() { feed.enabled = false; notify(); endDrag(); lifetime.abort(); feed.pending?.abort(); settleRender?.(); popupAnchor?.destroy(); picking?.destroy(); if (dataSource) viewer.dataSources.remove(dataSource, true); listeners.clear(); },
    attachPopup(element) { popupAnchor?.destroy(); popup = element; if (viewer) popupAnchor = anchorEventPopup(viewer, Cesium, popup, () => ({ enabled: feed.enabled, incident: { ...feed.snapshot.incidents.find((item) => item.id === selectedId), ...locationDrafts.get(selectedId) } })); return () => { popupAnchor?.destroy(); popupAnchor = undefined; popup = undefined; }; },
    getStats: () => ({ count: items().length, lastUpdate: feed.lastUpdate, lastError: feed.lastError, available: Boolean(feed.snapshot.version) }),
    state,
    subscribe(listener) { listeners.add(listener); listener(state()); return () => listeners.delete(listener); },
    setFilters(next) {
      if (!validPeriod(next)) throw new Error('The period must be valid and From must precede To.');
      parseBbox(next.bbox);
      rollingPeriod = (!next.date_from && !next.date_to) || (rollingPeriod && next.date_from === filters.date_from && next.date_to === filters.date_to);
      const period = rollingPeriod ? recentPeriod() : { date_from: next.date_from, date_to: next.date_to };
      const changed = period.date_from !== filters.date_from || period.date_to !== filters.date_to;
      filters = { ...next, ...period };
      if (changed) void layer.update();
      else { paint(); notify(); }
    },
    canMove,
    lockReview(id, locked) { if (locked) reviewLocks.add(id); else reviewLocks.delete(id); },
    setLocation(id, lat, lon) {
      if (!canMove(id)) throw new Error('Save the event as unvalidated before changing its location.');
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) throw new Error('Enter valid latitude and longitude.');
      locationDrafts.set(id, { lat, lon }); paint(); notify();
    },
    select,
    visibleArea() {
      const rectangle = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid);
      if (!rectangle) throw new Error('Point the camera toward the globe before selecting an area.');
      const bounds = [rectangle.west, rectangle.south, rectangle.east, rectangle.north].map((value) => Cesium.Math.toDegrees(value).toFixed(6)).join(',');
      parseBbox(bounds); return bounds;
    },
  };
  signal?.addEventListener('abort', () => endDrag(), { once: true });
  return layer;
}

function appendEvidence(parent, evidence, incident) {
  const card = text('article', '', 'gev-evidence'); card.id = `gev-evidence-${evidence.id}`;
  const author = evidence.username ? ` · @${evidence.username.replace(/^@/, '')}` : '';
  card.append(text('strong', `${platformName(evidence.platform)}${author}`));
  if (modeLabel(evidence.mode) === 'Synthetic') card.append(text('small', 'Synthetic'));
  const content = text('div', '', 'gev-evidence-content');
  content.append(text('p', evidence.text, 'gev-evidence-text'));
  const media = photosFor(evidence, incident)[0];
  if (media) {
    const image = document.createElement('img');
    image.src = viewerApiPath(media.url); image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
    image.title = media.origin === 'ai_generated' ? 'AI-generated Synthetic image' : 'Publication attachment'; image.alt = media.alt_text || image.title;
    image.addEventListener('error', () => image.remove(), { once: true }); content.append(image);
  }
  const createdAt = evidence.created_at || evidence.observed_at;
  card.append(content, text('small', Number.isFinite(Date.parse(createdAt)) ? utcToBogota(createdAt).replace('T', ' ') : 'Time unavailable'));
  const url = evidence.mode === 'real' ? safeSourceUrl(evidence.source_uri) : null;
  if (url) { const link = text('a', 'Open original source ↗'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; card.append(link); }
  parent.append(card);
  return card;
}

function confirmReview(incident, action, values, signal) {
  return new Promise((resolve) => {
    let dialog;
    const finish = (confirmed = false) => {
      if (!dialog) return;
      dialog.dispose(); dialog = null; signal.removeEventListener('abort', cancel);
      resolve(confirmed);
    };
    const cancel = () => finish();
    dialog = createSceneDialog(action === 'Pending' ? 'Mark event as pending?' : `${action} event?`, cancel);
    dialog.element.classList.add('gev-review-dialog');
    const description = dialog.text(`Save this review for ${incident.title || `${categoryName(incident.category)} · ${displayLocality(incident.locality)}`}?`);
    description.id = 'gev-review-confirm-description'; dialog.element.setAttribute('aria-describedby', description.id);
    dialog.text(values.note || 'No review note.', dialog.body).className = 'gev-review-confirm-note';
    if (values.lat != null) dialog.text(`Latitude: ${values.lat} · Longitude: ${values.lon}`);
    dialog.button(`Confirm ${action.toLowerCase()}`, () => finish(true));
    dialog.listen(dialog.element, 'keydown', (event) => { if (event.key === 'Escape') event.stopPropagation(); });
    dialog.footer.querySelector('button').focus();
    signal.addEventListener('abort', cancel, { once: true });
    if (signal.aborted) cancel();
  });
}

function reviewMessage(review) {
  const status = { validated: 'Validated', rejected: 'Rejected', pending: 'Pending' }[review.status];
  return `Review saved: ${status}.${review.pending ? ' Waiting for the updated AIDP publication.' : ''}${review.publicationError ? ` ${review.publicationError} You can retry this review.` : ''}`;
}

function reconcileReview(incident, view, layer) {
  const review = view.reviews.get(incident?.id);
  if (!review || review.busy) return review;
  const matches = incident.review_status === review.status && (incident.review_note ?? '') === review.note &&
    JSON.stringify([...(incident.reviewed_evidence_ids || [])].sort()) === JSON.stringify([...(review.evidenceIds || review.expected_evidence_ids)].sort()) &&
    (review.lat == null || (incident.lat === review.lat && incident.lon === review.lon));
  if (review.published && !matches) { view.reviews.delete(incident.id); return; }
  if (!matches || incident.review_pending_publication) return review;
  review.published = true; review.publicationError = undefined;
  if (review.error) { review.error = false; review.message = 'The published review matches this decision.'; }
  else if (review.pending) review.message = reviewMessage({ ...review, pending: false });
  review.pending = false;
  layer.lockReview(incident.id, false);
  return review;
}

async function saveReview(incident, values, { request, layer, signal, view, redraw }) {
  const review = { ...values, busy: true, message: 'Saving review…' };
  view.reviews.set(incident.id, review); redraw();
  try {
    const result = await request(`/api/gods-eye-view/incidents/${encodeURIComponent(incident.id)}/review`, { method: 'POST', body: JSON.stringify(values), signal });
    if (signal.aborted) return;
    if (result.review_status !== values.status) throw new Error('The server did not confirm the requested review. Refresh before trying again.');
    review.busy = false; review.note = result.review_note ?? values.note; review.evidenceIds = result.reviewed_evidence_ids || values.expected_evidence_ids;
    if (values.lat != null) { review.lat = result.lat ?? values.lat; review.lon = result.lon ?? values.lon; }
    review.pending = !!result.review_pending_publication; review.publicationError = result.publication_error; review.message = reviewMessage(review); redraw();
    await layer.update();
  } catch (error) {
    if (signal.aborted) return;
    review.busy = false; review.error = true; review.message = error.status === 409 ? 'Event evidence or its saved location changed. Refresh the event and review it again. Your note is preserved.' : `Review could not be confirmed: ${error.message || 'The service is unavailable. Refresh before trying again.'}`; redraw();
    if (error.status === 409) await layer.update();
  }
}

function appendReview(parent, { incident, request, layer, signal, formSignal, draft, view, redraw }) {
  const form = document.createElement('form'); form.className = 'gev-review';
  const coordinates = text('div', '', 'gev-review-coordinates'), inputs = {};
  for (const [name, title, limit] of [['lat', 'Latitude', 90], ['lon', 'Longitude', 180]]) {
    const label = text('label', title), input = document.createElement('input'); input.name = name; input.type = 'number'; input.step = 'any'; input.min = -limit; input.max = limit; input.value = draft[name]; label.append(input); coordinates.append(label); inputs[name] = input;
  }
  form.append(coordinates);
  const label = text('label', 'Review note'); const note = document.createElement('textarea'); note.maxLength = 1000; note.rows = 2; note.value = draft.note; label.append(note); form.append(label);
  const count = text('small', '', 'gev-review-count'); count.id = 'gev-review-count'; count.setAttribute('aria-live', 'polite'); note.setAttribute('aria-describedby', count.id);
  const updateCount = () => { count.textContent = `${note.value.length} / ${note.maxLength}`; };
  note.addEventListener('input', updateCount, { signal: formSignal }); updateCount(); form.append(count);
  const review = view.reviews.get(incident.id);
  const status = text('p', review?.message, 'gev-review-status'); status.setAttribute('role', review?.error || review?.publicationError ? 'alert' : 'status');
  for (const [name, title] of [['validated', 'Validated'], ['rejected', 'Rejected']]) {
    const label = text('label', title, 'gev-review-choice'), input = document.createElement('input'); input.name = name; input.type = 'checkbox'; input.checked = draft.status === name; inputs[name] = input; label.append(input); form.append(label);
    input.addEventListener('change', () => { if (input.checked) inputs[name === 'validated' ? 'rejected' : 'validated'].checked = false; }, { signal: formSignal });
  }
  const save = text('button', 'Save'); save.type = 'submit'; form.append(save, status);
  const readValues = () => ({ note: note.value, status: inputs.validated.checked ? 'validated' : inputs.rejected.checked ? 'rejected' : 'pending', lat: inputs.lat.value, lon: inputs.lon.value });
  view.readReview = readValues;
  const setBusy = (busy) => { save.disabled = busy; note.disabled = busy; inputs.validated.disabled = busy; inputs.rejected.disabled = busy; inputs.lat.disabled = busy || !layer.canMove(incident.id); inputs.lon.disabled = inputs.lat.disabled; form.setAttribute('aria-busy', String(busy)); };
  view.syncReviewControls = () => setBusy(!!view.reviews.get(incident.id)?.busy);
  for (const input of [inputs.lat, inputs.lon]) input.addEventListener('change', () => {
    const lat = Number(inputs.lat.value), lon = Number(inputs.lon.value);
    if (!inputs.lat.value || !inputs.lon.value || !Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) return;
    layer.setLocation(incident.id, lat, lon);
  }, { signal: formSignal });
  setBusy(!!review?.busy); let confirming = false;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (confirming || view.reviews.get(incident.id)?.busy) return;
    const current = readValues(), value = { status: current.status, note: current.note, expected_evidence_ids: [...incident.evidence_ids] };
    if (current.lat || current.lon) {
      value.lat = Number(current.lat); value.lon = Number(current.lon);
      if (!current.lat || !current.lon || !Number.isFinite(value.lat) || !Number.isFinite(value.lon) || Math.abs(value.lat) > 90 || Math.abs(value.lon) > 180) { status.textContent = 'Enter valid latitude and longitude together.'; status.setAttribute('role', 'alert'); return; }
    }
    const action = { validated: 'Validate', rejected: 'Reject', pending: 'Pending' }[value.status];
    confirming = true; layer.lockReview(incident.id, true); setBusy(true);
    try {
      if (await confirmReview(incident, action, value, formSignal)) await saveReview(incident, value, { request, layer, signal, view, redraw });
    } finally {
      layer.lockReview(incident.id, !!view.reviews.get(incident.id)?.pending);
      confirming = false; setBusy(!!view.reviews.get(incident.id)?.busy);
      view.syncReviewControls?.();
      if (!formSignal.aborted && event.submitter?.isConnected) event.submitter.focus();
    }
  }, { signal: formSignal });
  parent.append(form);
}

function severityLabel(value) {
  const label = text('span', displaySeverity(value), 'gev-severity'); label.dataset.severity = value; return label;
}

function appendIncidentOverview(detail, incident) {
  const metadata = text('p', `${displayLocality(incident.locality)} · Severity: `);
  metadata.append(severityLabel(incident.severity), text('span', ` · Review: ${incident.review_status}`));
  detail.append(text('h3', incident.title || categoryName(incident.category), 'panel-title'), text('p', incident.summary), metadata);
  if (Number.isFinite(incident.corroboration_score)) detail.append(text('p', `Corroboration index: ${incident.corroboration_score}/100 · ${incident.independent_source_count ?? 'Unknown'} independent sources. A heuristic, not a probability or confirmation.`));
  if (incident.lat == null) detail.append(text('p', 'Location unresolved: no map position available.'));
}

function appendEvidencePage(detail, evidence, incident, view) {
  const slides = evidence;
  const section = text('section', '', 'gev-evidence-page'); section.tabIndex = 0; section.setAttribute('aria-label', 'Event publications'); section.setAttribute('aria-roledescription', 'carousel');
  const heading = text('h4', '', 'panel-title'), cards = text('div', ''); heading.setAttribute('aria-live', 'polite');
  const previous = text('button', '', 'gev-carousel-previous'), next = text('button', '', 'gev-carousel-next');
  const render = () => {
    const retained = slides.findIndex((item) => item.id === view.evidenceId);
    view.evidencePage = Math.max(0, Math.min(retained < 0 ? view.evidencePage : retained, slides.length - 1));
    const item = slides[view.evidencePage]; cards.replaceChildren();
    if (item) {
      view.evidenceId = item.id;
      const card = appendEvidence(cards, item, incident); card.setAttribute('role', 'group'); card.setAttribute('aria-roledescription', 'slide'); card.setAttribute('aria-label', `Publication ${view.evidencePage + 1} of ${slides.length}`);
      if (slides.length > 1) card.append(previous, next);
    } else cards.append(text('p', 'No publications available.'));
    previous.disabled = view.evidencePage === 0; next.disabled = view.evidencePage + 1 >= slides.length;
    heading.textContent = `Publications (${slides.length ? view.evidencePage + 1 : 0}/${slides.length})`;
  };
  const move = (index) => {
    const focused = document.activeElement;
    view.evidencePage = Math.max(0, Math.min(index, slides.length - 1)); view.evidenceId = slides[view.evidencePage]?.id; render();
    if (focused === previous || focused === next) (focused.disabled ? section : focused).focus({ preventScroll: true });
  };
  for (const [button, delta] of [[previous, -1], [next, 1]]) {
    button.setAttribute('aria-label', delta < 0 ? 'Previous publication' : 'Next publication');
    button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${delta < 0 ? 'm15 6-6 6 6 6' : 'm9 6 6 6-6 6'}"/></svg>`;
    button.type = 'button'; button.addEventListener('click', () => move(view.evidencePage + delta));
  }
  section.addEventListener('keydown', (event) => {
    const index = { ArrowLeft: view.evidencePage - 1, ArrowRight: view.evidencePage + 1, Home: 0, End: slides.length - 1 }[event.key];
    if (index === undefined || event.altKey || event.ctrlKey || event.metaKey) return;
    event.preventDefault(); event.stopPropagation(); move(index);
  });
  section.append(heading, cards); detail.append(section); render();
}

function renderEventDetail(detail, state, view, { request, layer, signal }) {
  if (state.isRefreshing) return;
  const incident = state.items.find((item) => item.id === state.selectedId);
  const evidence = incident ? evidenceFor(state.snapshot, incident) : [];
  const published = state.snapshot.incidents.find((item) => item.id === state.selectedId);
  const review = reconcileReview(published, view, layer);
  if (detail.dataset.incidentId !== incident?.id) { view.evidencePage = 0; view.evidenceId = undefined; }
  const nextKey = JSON.stringify([incident, evidence, state.snapshot.can_review, review]);
  if (nextKey === view.detailKey) return;
  const previous = detail.dataset.incidentId === incident?.id ? view.readReview?.() : undefined;
  const baseline = { note: incident?.review_note ?? '', status: incident?.review_status ?? 'pending', lat: incident?.lat == null ? '' : String(incident.lat), lon: incident?.lon == null ? '' : String(incident.lon) };
  const draft = { ...baseline };
  for (const key of Object.keys(baseline)) if (previous && previous[key] !== view.reviewBaseline[key]) draft[key] = previous[key];
  if (baseline.lat !== view.reviewBaseline?.lat || baseline.lon !== view.reviewBaseline?.lon) { draft.lat = baseline.lat; draft.lon = baseline.lon; }
  view.reviewBaseline = baseline;
  view.detailKey = nextKey; view.reviewLifetime?.abort(); view.reviewLifetime = new AbortController(); detail.replaceChildren(); view.popupContent.replaceChildren(); detail.dataset.incidentId = incident?.id || '';
  if (!incident) return;
  const display = Number.isFinite(incident.lat) && Number.isFinite(incident.lon) ? view.popupContent : detail;
  appendIncidentOverview(display, incident);
  appendEvidencePage(display, evidence, incident, view);
  if (state.snapshot.can_review) {
    appendReview(detail, { incident, request, layer, signal, formSignal: AbortSignal.any([signal, view.reviewLifetime.signal]), draft, view,
      redraw: () => renderEventDetail(detail, layer.state(), view, { request, layer, signal }) });
  }
}

function renderEventFilters(form, state) {
  for (const name of ['locality', 'platform', 'category', 'severity']) {
    const select = form.elements.namedItem(name);
    const values = (name === 'platform' ? Object.keys(SOCIAL_NETWORKS)
      : [...new Set([...state.snapshot.incidents.map((row) => row[name]), state.filters[name]])].filter(Boolean)).sort();
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
  for (const [index, incident] of state.items.entries()) {
    const button = text('button', '', 'gev-event scene-shot-row'); button.type = 'button'; button.setAttribute('aria-pressed', String(incident.id === state.selectedId)); button.classList.toggle('active', incident.id === state.selectedId);
    const metadata = text('small', `${displayLocality(incident.locality)} · `, 'scene-shot-meta');
    metadata.append(severityLabel(incident.severity), text('span', ` · ${evidenceFor(state.snapshot, incident).length} evidence items`));
    button.append(text('strong', `#${String(index + 1).padStart(3, '0')} ${incident.title || categoryName(incident.category)}`, 'scene-shot-label'), metadata);
    button.addEventListener('click', () => layer.select(incident.id)); list.append(button);
  }
}

function labelCustomLayers(signal) {
  const container = document.getElementById('data-toggles');
  if (!container) return;
  const update = () => {
    for (const heading of container.querySelectorAll('.data-layer-group-heading')) {
      if (heading.textContent === 'Other layers') heading.textContent = 'Custom layers';
    }
  };
  const observer = new MutationObserver(update);
  observer.observe(container, { childList: true }); update();
  signal.addEventListener('abort', () => observer.disconnect(), { once: true });
}

export function mountGodsEyeViewPanel({ layer, request, signal, setPanelCollapsed }) {
  const panel = document.createElement('section'); panel.id = 'gods-eye-view-panel'; panel.className = 'gev-social-panel panel-collapsible collapsed'; panel.dataset.panelId = panel.id; panel.hidden = true;
  panel.innerHTML = `<div class="panel-glow"></div><div class="global-context-panel-inner"><div class="panel-header">
    <span class="panel-title">SOCIAL NETWORKS</span><span class="panel-divider"></span>
    <button class="panel-collapse-btn" data-dock-toggle-target="gods-eye-view-panel" type="button" aria-expanded="false" aria-label="Expand Social networks" title="Expand Social networks"><span aria-hidden="true">▶</span></button>
    </div><div class="gev-panel-body data-toggle-list" data-rail-scroller>
    <p data-status role="status"></p><form class="gev-filters" aria-label="Social network event filters">
    <label>Locality<select name="locality"><option value="">All</option></select></label><label>Network<select name="platform"><option value="">All</option></select></label>
    <label>Event<select name="category"><option value="">All</option></select></label><label>Severity<select name="severity"><option value="">All</option></select></label>
    <label title="Bogotá time (UTC−05:00)">From<input name="date_from" aria-label="From · Bogotá time" type="datetime-local" step="1"></label><label title="Bogotá time (UTC−05:00)">To<input name="date_to" aria-label="To · Bogotá time" type="datetime-local" step="1"></label>
    <input name="bbox" type="hidden"><div class="gev-actions"><button type="button" data-area>Filter</button></div></form>
    <p data-area-status></p><p data-filter-error role="alert"></p><div class="gev-event-list scene-shot-list" aria-label="Social network events"></div><section class="gev-event-detail" aria-label="Selected event"></section></div></div>`;
  document.getElementById('global-context-panel').after(panel);
  const popup = text('section', '', 'gev-event-popup gev-social-panel'); popup.id = 'gev-event-popup'; popup.hidden = true; popup.tabIndex = -1;
  popup.setAttribute('role', 'dialog'); popup.setAttribute('aria-label', 'Selected social event');
  for (const [name, value] of Object.entries({ background: WORLD_OVERLAY_STYLE.selectedBackground, border: WORLD_OVERLAY_STYLE.selectedBorder, title: WORLD_OVERLAY_STYLE.title, detail: WORLD_OVERLAY_STYLE.detail })) popup.style.setProperty(`--gev-popup-${name}`, value);
  popup.style.font = WORLD_OVERLAY_STYLE.fontDetail;
  const popupContent = text('div', '', 'gev-popup-content gev-panel-body data-toggle-list');
  const close = text('button', '×', 'gev-popup-close'); close.type = 'button'; close.setAttribute('aria-label', 'Close selected event');
  popup.append(close, popupContent);
  const detachPopup = layer.attachPopup(popup);
  const closePopup = () => { popup.dataset.closedId = layer.state().selectedId; popup.hidden = true; panel.querySelector('.gev-event[aria-pressed="true"]')?.focus(); };
  close.addEventListener('click', closePopup, { signal });
  popup.addEventListener('keydown', (event) => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closePopup(); } }, { signal });
  for (const type of ['pointerdown', 'wheel']) popup.addEventListener(type, (event) => event.stopPropagation(), { signal });
  labelCustomLayers(signal);
  const onChange = (collapsed) => setPanelCollapsed(panel.id, collapsed, { explicit: true, persist: false, syncShare: false });
  const disclosure = bindPanelDisclosure({ panel, buttons: [panel.querySelector('.panel-collapse-btn')], onChange, onEscape: (event) => collapsePanelOnEscape(event, { panel, onChange }) });
  const form = panel.querySelector('form'); const detail = panel.querySelector('.gev-event-detail');
  const view = { detailKey: '', evidencePage: 0, popupContent, reviewLifetime: undefined, reviews: new Map() }; let previousSelection, previousEnabled = false;
  const showError = (error) => { panel.querySelector('[data-filter-error]').textContent = error?.message || ''; };
  const apply = () => {
    try { const filters = Object.fromEntries([...new FormData(form)].filter(([, value]) => value).map(([key, value]) => [key, key.startsWith('date_') ? bogotaToUtc(value) : value])); layer.setFilters(filters); showError(); }
    catch (error) { showError(error); }
  };
  form.addEventListener('submit', (event) => event.preventDefault(), { signal });
  form.addEventListener('change', apply, { signal });
  panel.querySelector('[data-area]').addEventListener('click', () => { try { form.elements.bbox.value = form.elements.bbox.value ? '' : layer.visibleArea(); apply(); } catch (error) { showError(error); } }, { signal });

  const unsubscribe = layer.subscribe((state) => {
    panel.hidden = !state.enabled;
    if (state.enabled && (!previousEnabled || (state.selectedId && state.selectedId !== previousSelection))) onChange(false);
    if (previousEnabled && !state.enabled) onChange(true);
    previousSelection = state.selectedId; previousEnabled = state.enabled;
    const status = panel.querySelector('[data-status]');
    status.textContent = state.isRefreshing && !state.snapshot.version ? 'Loading publications…' : state.lastError ? `Publication unavailable: ${state.lastError}${state.snapshot.version ? ' Showing the last successful publication.' : ''}` : state.snapshot.version ? '' : 'Waiting for a publication…';
    status.hidden = !status.textContent;
    renderEventFilters(form, state);
    panel.querySelector('[data-area]').textContent = state.filters.bbox ? 'Clear map area' : 'Filter';
    panel.querySelector('[data-area-status]').textContent = state.filters.bbox ? 'Fixed map area selected. Reports without coordinates are excluded.' : '';
    renderEventList(panel.querySelector('.gev-event-list'), state, layer);
    renderEventDetail(detail, state, view, { request, layer, signal });
  });
  const showEvidence = (id) => {
    const state = layer.state(); const incident = state.items.find((item) => item.evidence_ids.includes(id)); if (!state.enabled || !incident) return;
    onChange(false); layer.select(incident.id, false);
    view.evidenceId = id; view.detailKey = '';
    renderEventDetail(detail, layer.state(), view, { request, layer, signal });
    document.getElementById(`gev-evidence-${view.evidenceId}`)?.scrollIntoView({ block: 'nearest' });
  };
  signal.addEventListener('abort', () => { unsubscribe(); disclosure.destroy(); detachPopup(); view.reviewLifetime?.abort(); panel.remove(); }, { once: true });
  return { panel, popup, showEvidence };
}
