import { text } from './godsEyeViewLayer.js';
export const managedProviderLabel = (configured) => configured ? 'Configured · server managed' : 'Not configured · server managed';

export function ociModelLabel(name, vendor) {
  const friendly = typeof name === 'string' && name.trim() && !name.trim().startsWith('ocid1.') ? name.trim() : 'OCI conversational model';
  return typeof vendor === 'string' && vendor.trim() ? `${friendly} · ${vendor.trim()}` : friendly;
}

export function ociProviderPresentation(value) {
  const configured = value.configured === true;
  return {
    set: String(configured), label: managedProviderLabel(configured),
    model: value.model_id ? ociModelLabel(value.model_name, value.model_vendor) : 'No model selected',
  };
}

export function browserProviderConfig(payload) {
  const keys = ['googleApiKey', 'cesiumToken'];
  if (!payload || typeof payload !== 'object' || Array.isArray(payload) || Object.keys(payload).some((key) => !keys.includes(key))) throw new Error('The browser provider configuration is invalid.');
  const options = {};
  for (const key of keys) {
    if (typeof payload[key] !== 'string') throw new Error('The browser provider configuration is incomplete.');
    options[key] = payload[key] || undefined;
  }
  return options;
}

function nativeProviderPresentation(dialog, signal, ociRows) {
  const chip = document.getElementById('key-setup-chip');
  const description = dialog.querySelector('#key-setup-description');
  if (description) description.textContent = 'Administrators manage provider keys and OCI models in Parameters. Availability below reflects configured credentials, not a feed or quota test.';
  const apply = () => {
    const rowsHost = dialog.querySelector('[data-key-setup-rows]');
    for (const row of ociRows) if (rowsHost && row.parentNode !== rowsHost) rowsHost.append(row);
    if (chip?.hidden) chip.hidden = false;
    const save = dialog.querySelector('[data-key-setup-apply]');
    if (save) { save.disabled = true; save.hidden = true; }
    for (const row of dialog.querySelectorAll('.key-setup-row')) {
      if (ociRows.includes(row)) continue;
      const badge = row.querySelector('.key-setup-external');
      const label = managedProviderLabel(row.dataset.set === 'true');
      if (badge && badge.textContent !== label) { badge.textContent = label; badge.title = 'Configure provider credentials in Administration → God’s Eye View → Parameters.'; }
    }
    const note = dialog.querySelector('[data-key-setup-status]');
    const explanation = 'Provider configuration is managed on the server.';
    if (note && note.textContent !== explanation) note.textContent = explanation;
  };
  // Native setup fetches its registry asynchronously and owns subsequent renders.
  const observer = new MutationObserver(apply);
  observer.observe(dialog, { childList: true, subtree: true });
  if (chip) observer.observe(chip, { attributes: true, attributeFilter: ['hidden'] });
  signal.addEventListener('abort', () => observer.disconnect(), { once: true });
  apply();
}

async function loadOciStatus(section, request, signal, path, voice) {
  try {
    const value = await request(path, { signal });
    if (signal.aborted) return;
    const presentation = ociProviderPresentation(value);
    section.dataset.set = presentation.set;
    section.querySelector('[data-oci-configured]').textContent = presentation.label;
    section.querySelector('[data-oci-model]').textContent = `${value.region || 'Region unavailable'} · ${presentation.model}${voice && value.tts_model ? ` · xAI Grok TTS · ${value.voice || 'Voice unavailable'}` : ''}`;
    return value;
  } catch (error) {
    if (!signal.aborted) {
      section.querySelector('[data-oci-configured]').textContent = 'Configuration unavailable';
      section.querySelector('[data-oci-model]').textContent = error.message;
    }
  }
}

function ociStatusRow(title, id, signal) {
  const section = text('section', '', 'key-setup-row gev-oci-settings');
  section.dataset.keyId = id; section.dataset.managed = 'external'; section.dataset.set = 'false';
  section.setAttribute('aria-label', title);
  section.innerHTML = `<div class="key-setup-row-head"><span class="key-setup-led" aria-hidden="true"></span><strong></strong><span class="key-setup-external" data-oci-configured>Loading configuration…</span></div><p data-oci-model class="key-setup-unlocks" role="status">Loading provider configuration…</p>`;
  section.querySelector('strong').textContent = title;
  signal.addEventListener('abort', () => section.remove(), { once: true });
  return section;
}

/** Append read-only OCI status and an administration link for authorized operators. */
export async function mountProviderSettings({ request, signal }) {
  const dialog = document.getElementById('key-setup');
  if (!dialog) return;
  const rows = [ociStatusRow('OCI Generative AI · text', 'oci', signal), ociStatusRow('OCI Generative AI · voice', 'oci-voice', signal)];
  nativeProviderPresentation(dialog, signal, rows);
  const statuses = await Promise.all([
    loadOciStatus(rows[0], request, signal, '/api/gods-eye-view/oci-provider', false),
    loadOciStatus(rows[1], request, signal, '/api/gods-eye-view/oci-voice', true),
  ]);
  if (!signal.aborted && statuses.some((value) => value?.can_configure === true)) {
    const link = text('a', 'Manage parameters ↗', 'key-setup-get');
    link.href = '/admin/gods-eye-view#parameters';
    link.target = '_blank'; link.rel = 'noopener';
    (dialog.querySelector('.key-setup-footer') || dialog).append(link);
    signal.addEventListener('abort', () => link.remove(), { once: true });
  }
}
