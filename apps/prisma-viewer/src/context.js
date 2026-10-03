import { safeSourceUrl, modeLabel, displayLocality } from './model.js';

const node = (tag, value) => { const element = document.createElement(tag); element.textContent = value ?? ''; return element; };

export function nasaDate(now = new Date()) {
  return new Date(now.getTime() - 2 * 86400000).toISOString().slice(0, 10);
}

export function nasaUrl(day) {
  if (!/^\d{4}-\d\d-\d\d$/.test(day) || new Date(`${day}T00:00:00Z`).toISOString().slice(0, 10) !== day) throw new Error('Invalid satellite date.');
  return `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/MODIS_Terra_CorrectedReflectance_TrueColor/default/${day}/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg`;
}

function referenceLink(label, url) {
  const target = safeSourceUrl(url);
  if (!target) return node('span', label);
  const link = node('a', label); link.href = target; link.target = '_blank'; link.rel = 'noopener noreferrer';
  return link;
}

function renderWeather(weather) {
  const root = document.getElementById('weather-context'); root.replaceChildren();
  root.append(node('p', `Weather · ${weather.status || 'unavailable'} · model estimate, not incident evidence`));
  for (const point of weather.points || []) {
    const row = node('p', `${displayLocality(point.name || point.locality)}: ${point.temperature_c ?? '—'} °C · rain ${point.precipitation_mm ?? '—'} mm · wind ${point.wind_kmh ?? '—'} km/h`);
    row.append(node('small', `Valid ${point.valid_at || 'time unavailable'}`)); root.append(row);
  }
  root.append(referenceLink(weather.source || 'Open-Meteo', weather.source_url));
  if (weather.license_url) root.append(referenceLink('Weather data · CC BY 4.0', weather.license_url));
}

export async function loadContext(request) {
  try {
    const data = await request('/api/prisma/context');
    renderWeather(data.weather || {});
    const cameras = document.getElementById('camera-context'); cameras.replaceChildren();
    cameras.append(node('p', 'Public camera references · no authorized live feeds configured'));
    for (const camera of data.cameras?.items || []) cameras.append(referenceLink(camera.name, camera.url));
  } catch {
    document.getElementById('weather-context').textContent = 'Weather context unavailable. No weather values have been inferred.';
    document.getElementById('camera-context').textContent = 'Camera references unavailable. No live feed is configured.';
  }
}

export function renderNews(evidence) {
  const root = document.getElementById('news-context'); root.replaceChildren();
  const reports = evidence.filter((item) => ['news', 'institutional', 'institucional', 'noticias', 'idiger', 'sire', '123'].includes(item.platform)).slice(-5).reverse();
  if (!reports.length) root.append(node('p', 'No news or institutional reports in the selected publication area.'));
  for (const report of reports) {
    const article = node('article', `${modeLabel(report.mode)} · ${report.platform}`);
    article.append(node('p', report.text), referenceLink('Original source ↗', report.source_uri)); root.append(article);
  }
}
