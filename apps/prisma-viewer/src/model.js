export const FILTER_KEYS = ['locality', 'platform', 'category', 'severity', 'mode', 'date_from', 'date_to'];
export const DATE_KEYS = ['date_from', 'date_to'];

export function bogotaToUtc(value) {
  if (!value) return '';
  const normalized = value.length === 16 ? `${value}:00` : value;
  const date = new Date(`${normalized}-05:00`);
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$/.test(normalized) || !Number.isFinite(date.getTime()) || utcToBogota(date.toISOString()) !== normalized) throw new Error('El período debe contener fechas válidas en hora Bogotá.');
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
  return mode === 'real' ? 'REAL' : mode === 'simulation' ? 'SIMULADO' : 'SIN CLASIFICAR';
}

export function evidenceFor(snapshot, incident) {
  const ids = new Set(incident?.evidence_ids ?? []);
  return snapshot.evidence.filter((item) => ids.has(item.id));
}

export function filteredIncidents(snapshot, filters) {
  if (!validPeriod(filters)) return [];
  return snapshot.incidents.filter((incident) => FILTER_KEYS.every((key) => {
    if (!filters[key]) return true;
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

export function allowedActions(actions, snapshot) {
  if (!Array.isArray(actions)) return [];
  return actions.filter((action) => {
    if (action?.type === 'focus_incident') return snapshot.incidents.some((item) => item.id === action.incident_id);
    if (action?.type !== 'filter_incidents' || !action.filters || typeof action.filters !== 'object') return false;
    return !Array.isArray(action.filters) && validPeriod(action.filters) && Object.entries(action.filters).every(([key, value]) => {
      if (!FILTER_KEYS.includes(key) || typeof value !== 'string' || value.length > 100) return false;
      if (!value || DATE_KEYS.includes(key)) return true;
      if (key === 'mode') return ['real', 'simulation'].includes(value);
      const records = key === 'platform' ? snapshot.evidence : snapshot.incidents;
      return records.some((item) => String(item[key]) === value);
    });
  });
}

export function validateSnapshot(data) {
  if (!data || typeof data.version !== 'string' || !Array.isArray(data.incidents) || !Array.isArray(data.evidence)) {
    throw new Error('La publicación recibida no tiene un formato válido.');
  }
  for (const item of data.incidents) {
    const unresolved = item.lat == null && item.lon == null;
    if (typeof item.id !== 'string' || !Array.isArray(item.evidence_ids) || (!unresolved && (!Number.isFinite(item.lat) || !Number.isFinite(item.lon) || Math.abs(item.lat) > 90 || Math.abs(item.lon) > 180))) {
      throw new Error('La publicación contiene una ubicación o referencia inválida.');
    }
  }
  return data;
}
