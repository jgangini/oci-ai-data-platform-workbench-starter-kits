export const FILTER_KEYS = ['locality', 'platform', 'category', 'severity', 'mode', 'date_from', 'date_to', 'bbox'];
export const DATE_KEYS = ['date_from', 'date_to'];

export function bogotaToUtc(value) {
  if (!value) return '';
  const normalized = value.length === 16 ? `${value}:00` : value;
  const date = new Date(`${normalized}-05:00`);
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$/.test(normalized) || !Number.isFinite(date.getTime()) || utcToBogota(date.toISOString()) !== normalized) throw new Error('Enter a valid period in Bogotá time.');
  return date.toISOString();
}

export function utcToBogota(value) {
  if (!value) return '';
  return new Date(Date.parse(value) - 5 * 60 * 60 * 1000).toISOString().slice(0, 19);
}

export function validPeriod(filters) {
  for (const key of DATE_KEYS) {
    const value = filters[key];
    if (!value) continue;
    if (typeof value !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?(Z|[+-]\d\d:\d\d)$/.test(value) || !Number.isFinite(Date.parse(value))) return false;
    if (Number(value.slice(11, 13)) > 23 || Number(value.slice(14, 16)) > 59 || Number(value.slice(17, 19)) > 59) return false;
    const day = value.slice(0, 10);
    if (new Date(`${day}T00:00:00Z`).toISOString().slice(0, 10) !== day) return false;
  }
  return !filters.date_from || !filters.date_to || Date.parse(filters.date_from) <= Date.parse(filters.date_to);
}

const canonicalMode = (mode) => mode === 'simulation' ? 'Synthetic' : mode;

export function modeLabel(mode) {
  return mode === 'real' ? 'REAL' : canonicalMode(mode) === 'Synthetic' ? 'Synthetic' : 'UNCLASSIFIED';
}

export const displayLocality = (value) => value === 'Sin localizar' ? 'Location unresolved' : value;
export const displaySeverity = (value) => ({ low: 'Low', medium: 'Medium', high: 'High' }[value] || value);

export function evidenceFor(snapshot, incident) {
  const ids = new Set(incident?.evidence_ids ?? []);
  return [...new Map(snapshot.evidence.filter((item) => ids.has(item.id)).map((item) => [item.id, item])).values()];
}

export function reportActivity(incident, selectedEvidence = []) {
  const labels = { below_threshold: 'Below threshold', low: 'Low', medium: 'Medium', high: 'High' };
  const counts = incident.report_counts && typeof incident.report_counts === 'object' && !Array.isArray(incident.report_counts) ? incident.report_counts : {};
  const linked = [...new Map(selectedEvidence.filter((item) => incident.evidence_ids?.includes(item.id)).map((item) => [item.id, item])).values()];
  const platforms = new Set([...Object.keys(counts).filter((key) => Number.isSafeInteger(counts[key]) && counts[key] >= 0), ...linked.map((item) => item.platform)]);
  return {
    level: labels[incident.report_activity] || 'Unavailable',
    networks: [...platforms].map((platform) => {
      const posts = linked.filter((item) => item.platform === platform);
      const dates = posts.map((item) => item.created_at).filter((value) => typeof value === 'string' && Number.isFinite(Date.parse(value))).sort((a, b) => Date.parse(a) - Date.parse(b));
      const window = incident.correlation_windows_minutes?.[platform];
      return { platform, count: Number.isSafeInteger(counts[platform]) && counts[platform] >= 0 ? counts[platform] : null,
        total: posts.length, windowMinutes: Number.isSafeInteger(window) && window > 0 ? window : null, latestAt: dates.at(-1) || null,
        level: labels[incident.report_activity_by_platform?.[platform]] || 'Unavailable' };
    }),
  };
}

export function parseBbox(value) {
  if (value == null || value === '') return null;
  if (typeof value !== 'string') throw new Error('Invalid geographic area.');
  const parts = value.split(',');
  if (parts.length !== 4 || !parts.every((part) => /^[+-]?\d+(?:\.\d+)?$/.test(part.trim()))) throw new Error('Invalid geographic area.');
  const [west, south, east, north] = parts.map(Number);
  const inRange = [west, south, east, north].every((point, index) => Number.isFinite(point) && Math.abs(point) <= (index % 2 ? 90 : 180));
  if (!inRange || west > east || south > north) throw new Error('Area is out of range or crosses the antimeridian.');
  return [west, south, east, north];
}

export function withinBbox(incident, bounds) {
  if (!bounds) return true;
  const [west, south, east, north] = bounds;
  return Number.isFinite(incident.lat) && Number.isFinite(incident.lon) && west <= incident.lon && incident.lon <= east && south <= incident.lat && incident.lat <= north;
}

function validArea(value) {
  try { parseBbox(value); return true; }
  catch { return false; }
}

export function filteredIncidents(snapshot, filters) {
  if (!validPeriod(filters) || !validArea(filters.bbox)) return [];
  const bounds = parseBbox(filters.bbox);
  return snapshot.incidents.filter((incident) => {
    if (filters.date_from || filters.date_to || filters.platform) {
      const publications = evidenceFor(snapshot, incident);
      const matches = (publications.length ? publications : [incident]).some((item) => {
        const created = Date.parse(item.created_at || incident.created_at);
        return (!filters.platform || item.platform === filters.platform) && (!filters.date_from || created >= Date.parse(filters.date_from)) && (!filters.date_to || created <= Date.parse(filters.date_to));
      });
      if (!matches) return false;
    }
    return FILTER_KEYS.every((key) => {
      if (!filters[key] || DATE_KEYS.includes(key) || key === 'platform') return true;
      if (key === 'bbox') return withinBbox(incident, bounds);
      if (key === 'mode') return canonicalMode(incident.mode) === canonicalMode(filters.mode);
      return String(incident[key]) === String(filters[key]);
    });
  });
}

export function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function photosFor(evidence, incident) {
  if (!incident?.evidence_ids?.includes(evidence?.id)) return [];
  const originals = evidence.platform === 'x' && Array.isArray(evidence.media) ? evidence.media.filter((media) => {
    const safe = safeSourceUrl(media?.url);
    if (media?.type !== 'photo' || !safe) return false;
    const url = new URL(safe);
    return url.hostname === 'pbs.twimg.com' && !url.port && !url.hash && url.pathname.startsWith('/media/');
  }) : [];
  const bundled = canonicalMode(evidence.mode) === 'Synthetic' && Array.isArray(evidence.attachments) ? evidence.attachments.flatMap((item) => {
    if (item?.type !== 'image' || !/^[a-f0-9]{64}$/.test(item.sha256 || '')) return [];
    const legacy = /^posts\/(post-\d{4})\/media\/(image-\d{2}\.svg)$/.exec(item.dataset_path || '');
    if (legacy && item.mime_type === 'image/svg+xml') return [{ ...item, url: `/api/gods-eye-view/media/${legacy[1]}/${legacy[2]}` }];
    const asset = /^media\/[a-z0-9_-]+\.(png|jpg|jpeg|webp)$/.exec(item.dataset_path || '');
    const identifier = /^(post-\d{4})-(image-\d{2})$/.exec(item.id || '');
    const mimeTypes = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp' };
    if (!asset || !identifier || item.origin !== 'ai_generated' || item.mime_type !== mimeTypes[asset[1]]) return [];
    return [{ ...item, url: `/api/gods-eye-view/media/${identifier[1]}/${identifier[2]}.${asset[1]}` }];
  }) : [];
  return [...new Map([...originals, ...bundled].map((item) => [item.url, item])).values()].slice(0, 4);
}

export function allowedActions(actions, snapshot) {
  if (!Array.isArray(actions)) return [];
  return actions.filter((action) => {
    if (action?.type === 'focus_incident') return snapshot.incidents.some((item) => item.id === action.incident_id);
    if (action?.type !== 'filter_incidents' || !action.filters || typeof action.filters !== 'object') return false;
    return !Array.isArray(action.filters) && validPeriod(action.filters) && validArea(action.filters.bbox) && Object.entries(action.filters).every(([key, value]) => {
      if (key === 'mode' || !FILTER_KEYS.includes(key) || typeof value !== 'string' || value.length > 100) return false;
      if (!value || DATE_KEYS.includes(key) || key === 'bbox') return true;
      const records = key === 'platform' ? snapshot.evidence : snapshot.incidents;
      return records.some((item) => String(item[key]) === value);
    });
  });
}

export function validateSnapshot(data) {
  if (!data || typeof data.version !== 'string' || !Array.isArray(data.incidents) || !Array.isArray(data.evidence)) {
    throw new Error('The publication format is invalid.');
  }
  for (const item of data.incidents) {
    const unresolved = item.lat == null && item.lon == null;
    if (typeof item.id !== 'string' || !Array.isArray(item.evidence_ids) || (!unresolved && (!Number.isFinite(item.lat) || !Number.isFinite(item.lon) || Math.abs(item.lat) > 90 || Math.abs(item.lon) > 180))) {
      throw new Error('The publication contains an invalid location or reference.');
    }
  }
  return data;
}
