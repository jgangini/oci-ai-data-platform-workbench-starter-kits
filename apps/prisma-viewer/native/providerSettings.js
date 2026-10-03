import { text } from './territorialLayer.js';

export function selectableModels(payload) {
  return Array.isArray(payload?.items) ? payload.items.filter((item) => typeof item.id === 'string' && item.selectable === true) : [];
}

export const managedProviderLabel = (configured) => configured ? 'Configured · server managed' : 'Not configured · server managed';

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

function nativeProviderPresentation(dialog, signal) {
  const chip = document.getElementById('key-setup-chip');
  const description = dialog.querySelector('#key-setup-description');
  if (description) description.textContent = 'Native provider keys are managed in the server environment. Availability below reflects configured credentials, not a feed or quota test. Configure the OCI text model in its separate section.';
  const apply = () => {
    if (chip?.hidden) chip.hidden = false;
    const save = dialog.querySelector('[data-key-setup-apply]');
    if (save) { save.disabled = true; save.hidden = true; }
    for (const row of dialog.querySelectorAll('.key-setup-row')) {
      const badge = row.querySelector('.key-setup-external');
      const label = managedProviderLabel(row.dataset.set === 'true');
      if (badge && badge.textContent !== label) { badge.textContent = label; badge.title = 'Configure native provider credentials in the server environment.'; }
    }
    const note = dialog.querySelector('[data-key-setup-status]');
    const explanation = 'Native key editing is disabled here. An administrator manages server credentials; OCI model selection and testing are available below.';
    if (note && note.textContent !== explanation) note.textContent = explanation;
  };
  // Native setup fetches its registry asynchronously and owns subsequent renders.
  const observer = new MutationObserver(apply);
  observer.observe(dialog, { childList: true, subtree: true });
  if (chip) observer.observe(chip, { attributes: true, attributeFilter: ['hidden'] });
  signal.addEventListener('abort', () => observer.disconnect(), { once: true });
  apply();
}

async function loadModelPage(request, signal, cursor, section) {
  const select = section.querySelector('select');
  const payload = await request(`/api/admin/prisma/oci-provider/models${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`, { signal });
  for (const item of selectableModels(payload)) {
    const existing = [...select.options].find((option) => option.value === item.id);
    const option = existing || new Option(`${item.name || item.id}${item.vendor ? ` · ${item.vendor}` : ''}`, item.id);
    option.dataset.selectable = 'true'; if (!existing) select.add(option);
  }
  const next = payload.next_cursor || null; section.querySelector('[data-models]').textContent = next ? 'More models' : 'Refresh models';
  section.querySelector('[data-oci-status]').textContent = select.options.length ? 'Choose an available model, then save. Region and credentials remain server-managed.' : 'No supported active chat models were returned.';
  return next;
}

/** Adds OCI configuration without changing native voice or the native key writer. */
export async function mountProviderSettings({ request, signal }) {
  const dialog = document.getElementById('key-setup');
  if (!dialog) return;
  nativeProviderPresentation(dialog, signal);
  const section = text('section', '', 'tc-oci-settings'); section.setAttribute('aria-label', 'OCI Generative AI settings');
  section.innerHTML = `<h3>OCI Generative AI</h3><p data-oci-status role="status">Loading provider status…</p><p>Server-managed OCI credentials. Text assistance is separate from the native voice provider.</p><div data-oci-admin hidden><label>Model<select aria-label="OCI Generative AI model"></select></label><div class="tc-actions"><button type="button" data-models>Load models</button><button type="button" data-save disabled>Save model</button><button type="button" data-test>Test saved model</button></div></div>`;
  dialog.querySelector('.key-setup-footer')?.before(section);
  const status = section.querySelector('[data-oci-status]'); const admin = section.querySelector('[data-oci-admin]'); const select = section.querySelector('select');
  let cursor = null, configuredModel = '';
  function render(value) {
    configuredModel = value.model_id || ''; admin.hidden = value.can_configure !== true;
    status.textContent = `${value.region || 'Region unavailable'} · ${configuredModel || 'No model selected'} · ${value.message || value.status || (value.configured ? 'Configured' : 'Not configured')}`;
    if (configuredModel && ![...select.options].some((option) => option.value === configuredModel)) select.add(new Option(`${configuredModel} (saved)`, configuredModel));
    select.value = configuredModel;
  }
  async function action(work) {
    const buttons = [...section.querySelectorAll('button')]; buttons.forEach((button) => { button.disabled = true; });
    try { await work(); }
    catch (error) { if (!signal.aborted) status.textContent = error.message; }
    finally { buttons.forEach((button) => { button.disabled = false; }); section.querySelector('[data-save]').disabled = !select.selectedOptions[0]?.dataset.selectable; }
  }
  select.addEventListener('change', () => { section.querySelector('[data-save]').disabled = !select.selectedOptions[0]?.dataset.selectable; }, { signal });
  section.querySelector('[data-models]').addEventListener('click', () => void action(async () => {
    cursor = await loadModelPage(request, signal, cursor, section);
  }), { signal });
  section.querySelector('[data-save]').addEventListener('click', () => void action(async () => { render(await request('/api/admin/prisma/oci-provider', { method: 'PUT', body: JSON.stringify({ model_id: select.value }), signal })); status.textContent += ' · Model saved.'; }), { signal });
  section.querySelector('[data-test]').addEventListener('click', () => void action(async () => { const value = await request('/api/admin/prisma/oci-provider/test', { method: 'POST', body: '{}', signal }); render(value); status.textContent += value.test?.status === 'success' ? ' · Inference test passed.' : ' · Test did not confirm success.'; }), { signal });
  signal.addEventListener('abort', () => section.remove(), { once: true });
  try { render(await request('/api/prisma/oci-provider', { signal })); }
  catch (error) { if (!signal.aborted) status.textContent = error.message; }
}
