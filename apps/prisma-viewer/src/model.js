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

export function modeLabel(mode) {
  return mode === 'real' ? 'REAL' : mode === 'simulation' ? 'SIMULATED' : 'UNCLASSIFIED';
}

export function evidenceFor(snapshot, incident) {
  const ids = new Set(incident?.evidence_ids ?? []);
  return snapshot.evidence.filter((item) => ids.has(item.id));
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
  return snapshot.incidents.filter((incident) => FILTER_KEYS.every((key) => {
    if (!filters[key]) return true;
    if (key === 'bbox') return withinBbox(incident, bounds);
    if (key === 'date_from') return Date.parse(incident.created_at) >= Date.parse(filters.date_from);
    if (key === 'date_to') return Date.parse(incident.created_at) <= Date.parse(filters.date_to);
    if (key === 'platform') return evidenceFor(snapshot, incident).some((item) => item.platform === filters.platform);
    return String(incident[key]) === String(filters[key]);
  }));
}

export function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function photosFor(evidence, incident) {
  if (evidence.mode !== 'real' || incident?.review_status !== 'validated' || !incident.evidence_ids?.includes(evidence.id) || evidence.platform !== 'x' || !Array.isArray(evidence.media)) return [];
  return evidence.media.filter((media) => {
    const safe = safeSourceUrl(media?.url);
    if (media?.type !== 'photo' || !safe) return false;
    const url = new URL(safe);
    return url.hostname === 'pbs.twimg.com' && !url.port && url.pathname.startsWith('/media/');
  }).slice(0, 4);
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
