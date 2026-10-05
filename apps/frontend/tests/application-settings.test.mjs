import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';

const compiled = ts.transpileModule(readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const poll = {};
new Function('exports', ts.transpileModule(readFileSync(new URL('../src/registrationPoll.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText)(poll);
const settings = { aidp_service_endpoint: 'https://example.test/api', aidp_url: 'https://example.test/workbench', aidp_platform_id: 'platform', deployment_mode: 'laboratory', registration_code_configured: true, time_zone: 'America/Bogota', time_zones: ['America/Bogota', 'UTC'] };
const release = { current_release: 'v2.3.9', current_commit_sha: 'abc123', latest_release: 'v2.4.0', update_available: true, updater_available: true, operation: null, packages: [] };
async function harness(t, currentRelease = release, route = '/admin/settings', initial = {}) {
  let cursor = 0, dirty = true, effects = [], tree; const slots = [], requests = [], navigations = [], previousWindow = globalThis.window;
  const location = new URL(route, 'https://example.test');
  globalThis.window = { location: { pathname: location.pathname, search: location.search, hash: '#application', origin: location.origin, assign(path) { navigations.push(path); }, reload() { assert.fail('Unexpected reload'); } } };
  t.mock.method(globalThis, 'fetch', (path, options) => new Promise((resolve, reject) => requests.push({ path, options, reject, resolve: body => resolve({ ok: true, status: 200, json: async () => body }) })));
  const hooks = {
    useState(initial) { const id = cursor++; if (!(id in slots)) slots[id] = typeof initial === 'function' ? initial() : initial; return [slots[id], value => { const next = typeof value === 'function' ? value(slots[id]) : value; dirty ||= !Object.is(slots[id], next); slots[id] = next; }]; },
    useRef(initial) { const id = cursor++; return slots[id] ||= { current: initial }; },
    useEffect(callback, deps) { const id = cursor++, old = slots[id]; if (!old || deps.some((value, index) => value !== old.deps[index])) effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; }); },
  };
  const module = {};
  new Function('exports', 'require', compiled)(module, name => name === 'react' ? hooks : name === 'react/jsx-runtime' ? jsxRuntime : name === './LoadingIndicator' ? { LoadingIndicator() {} } : name === './registrationPoll' ? { ...poll, pollRegistration: ({ request, signal }) => request(signal) } : {});
  const component = module.App().type;
  function render() { for (let n = 0; dirty && n < 20; n++) { cursor = 0; dirty = false; effects = []; tree = component(); effects.forEach(effect => effect()); } assert.equal(dirty, false); }
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const find = (type, match = () => true) => { const value = nodes(tree).find(node => (node?.type === type || node?.type?.name === type) && match(node.props)); assert.ok(value, `Missing ${type}`); return value; };
  const act = callback => { callback(); render(); };
  const settle = async () => { await new Promise(setImmediate); render(); };
  t.after(() => { slots.forEach(slot => slot?.cleanup?.()); if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow; });
  render();
  for (const request of requests) if (initial[request.path] !== null) request.resolve(initial[request.path] ?? (request.path === '/api/admin/settings' ? settings : request.path === '/api/admin/application' ? currentRelease : { username: 'admin' }));
  await settle();
  return { requests, navigations, find, act, settle, nodes: () => nodes(tree), mutations: () => requests.filter(request => ['PUT', 'POST'].includes(request.options.method)),
    dialog: () => find('ConfirmModal', props => props.title === 'Update application?'),
    update: () => act(() => { const button = find('button', props => props.className === 'settings-save application-update'); assert.ok(!button.props.disabled); button.props.onClick(); }) };
}

for (const failed of [false, true]) test(`Workbench configuration loader ends on ${failed ? 'failure' : 'empty success'}`, async t => {
  const h = await harness(t, release, '/admin/settings', { '/api/admin/settings': null });
  assert.equal(h.find('LoadingIndicator', props => props.label === 'Loading configuration…').props.inline, true);
  const request = h.requests.find(item => item.path === '/api/admin/settings');
  if (failed) request.reject(new Error('Settings unavailable'));
  else request.resolve({ ...settings, aidp_url: '' });
  await h.settle();
  assert.ok(!h.nodes().some(node => node?.props?.label === 'Loading configuration…'));
  assert.equal(h.find('input', props => props['aria-label'] === 'AI Data Platform Workbench URL').props.placeholder, 'Not configured');
});

test('application updates require confirmation, preserve the warning and cancel without dispatching', async t => {
  const h = await harness(t);
  assert.equal(h.dialog().props.open, false); h.update();
  assert.equal(h.dialog().props.open, true);
  assert.match(h.dialog().props.description, /latest published release/);
  assert.ok(h.dialog().props.description.includes('Existing participant installations remain unchanged until their Update or Redeploy action is used.'));
  assert.equal(h.mutations().length, 0);
  h.act(() => h.dialog().props.onClose());
  assert.equal(h.dialog().props.open, false); assert.equal(h.mutations().length, 0);
  h.update(); const confirm = h.dialog().props.onConfirm;
  h.act(() => { confirm(); confirm(); });
  assert.equal(h.dialog().props.open, false); assert.equal(h.mutations().length, 1);
  const request = h.mutations()[0];
  assert.equal(request.path, '/api/admin/application/update'); assert.equal(request.options.method, 'POST');
  assert.match(JSON.parse(request.options.body).operation_id, /^[\da-f-]{36}$/);
  assert.equal(request.options.signal.aborted, false);
  assert.equal(h.find('button', props => props.className === 'settings-save application-update').props.disabled, true);
});

test('Settings Governance icon explains navigation and Cancel never opens the manager or deploys', async t => {
  const h = await harness(t, { ...release, packages: [{ package_id: 'ai_data_governance_vsc_extension', display_name: 'AI Data Governance', bundled_version: '3.0.0', scope: 'global' }] });
  const card = h.find('ApplicationReleaseSettings');
  const tree = card.type(card.props);
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const button = nodes(tree).find(node => node?.props?.className === 'module-configure module-deploy');
  assert.equal(button.type, 'button'); assert.equal(button.props.type, 'button');
  assert.equal(button.props.children.type.name, 'RefreshIcon');
  assert.equal(button.props['aria-label'], 'Deploy or redeploy AI Data Governance');
  assert.equal(button.props['aria-haspopup'], 'dialog');
  const confirmation = () => h.find('ConfirmModal', props => props.title === 'AI Data Governance');
  h.act(() => button.props.onClick());
  assert.equal(confirmation().props.open, true);
  assert.match(confirmation().props.description, /Open the shared module manager/);
  assert.match(confirmation().props.description, /Select an existing AI Data Platform administrator/);
  assert.equal(confirmation().props.confirmLabel, 'Accept');
  assert.equal(h.navigations.length, 0); assert.equal(h.mutations().length, 0);
  h.act(() => confirmation().props.onClose());
  assert.equal(confirmation().props.open, false);
  assert.equal(h.navigations.length, 0); assert.equal(h.mutations().length, 0);
  h.act(() => button.props.onClick());
  h.act(() => confirmation().props.onConfirm());
  assert.deepEqual(h.navigations, ['/admin/users?module=ai_data_governance_vsc_extension']);
  assert.equal(h.mutations().length, 0);
});

for (const installed of [false, true]) test(`Governance ${installed ? 'redeploy' : 'deploy'} requires an AIDP administrator and surfaces errors`, async t => {
  const module = { module_id: 'ai_data_governance_vsc_extension', display_name: 'AI Data Governance', installed, status: installed ? 'active' : 'not_installed', enabled: installed };
  const users = [{ id: 'participant', email: 'student@example.test', name: 'Student', is_aidp_admin: false, active: true, labs: [] }, { id: 'operator', email: 'operator@example.test', name: 'Operator', is_aidp_admin: true, active: true, managed: false, labs: [] }];
  const h = await harness(t, release, '/admin/users?module=ai_data_governance_vsc_extension', {
    '/api/config': { deployment_mode: 'laboratory', labs: [] },
    '/api/admin/users': { users }, '/api/admin/modules': { modules: [module] },
  });
  const entry = () => h.find('button', props => props.children === 'Deploy / Redeploy');
  const selector = () => h.find('select');
  assert.equal(entry().props.disabled, true);
  assert.ok(!JSON.stringify(selector().props.children).includes('student@example.test'));
  h.act(() => selector().props.onChange({ target: { value: 'participant' } }));
  h.act(() => entry().props.onClick());
  assert.equal(h.mutations().length, 0);
  assert.equal(h.find('GovernanceModuleModal').props.open, false);
  h.act(() => selector().props.onChange({ target: { value: 'operator' } }));
  assert.equal(entry().props.disabled, false);
  h.act(() => entry().props.onClick());
  h.requests.at(-1).resolve({ modules: [module] }); await h.settle();
  assert.equal(h.find('GovernanceModuleModal').props.open, true);
  assert.equal(h.find('GovernanceModuleModal').props.user.id, 'operator');
  h.act(() => h.find('GovernanceModuleModal').props.onSelectedChange(true));
  h.act(() => h.find('GovernanceModuleModal').props[installed ? 'onRedeploy' : 'onInstall']());
  const deployment = () => h.find('ConfirmModal', props => props.title === `${installed ? 'Redeploy' : 'Deploy'} AI Data Governance?`);
  assert.equal(deployment().props.open, true);
  assert.equal(deployment().props.confirmLabel, 'Accept');
  assert.match(deployment().props.description, /global module used by all AIDP developers/);
  assert.match(deployment().props.description, /operator@example.test/);
  assert.equal(h.mutations().length, 0);
  h.act(() => deployment().props.onClose());
  assert.equal(h.mutations().length, 0);
  assert.equal(h.find('GovernanceModuleModal').props.open, true);
  h.act(() => h.find('GovernanceModuleModal').props[installed ? 'onRedeploy' : 'onInstall']());
  const accept = deployment().props.onConfirm;
  h.act(() => { accept(); accept(); });
  assert.equal(h.mutations().length, 1);
  const request = h.mutations()[0];
  assert.equal(request.path, `/api/admin/users/operator/modules/ai_data_governance_vsc_extension${installed ? '/redeploy' : ''}`);
  assert.equal(request.options.method, 'POST');
  assert.match(JSON.parse(request.options.body).operation_id, /^[\da-f-]{36}$/);
  request.reject(new Error('Deployment prerequisites unavailable'));
  await h.settle(); h.requests.at(-1).resolve({ modules: [module] }); await h.settle();
  assert.equal(h.find('GovernanceModuleModal').props.error, 'Deployment prerequisites unavailable');
  assert.equal(deployment().props.error, 'Deployment prerequisites unavailable');
  assert.equal(h.mutations().length, 1);
  h.act(() => deployment().props.onClose());
  h.act(() => h.find('GovernanceModuleModal').props.onDelete());
  const confirmation = h.find('ConfirmModal', props => props.title === 'Delete global governance module?');
  assert.match(confirmation.props.description, /shared OCI credentials are retained/);
  assert.equal(confirmation.props.open, true);
  h.act(() => confirmation.props.onClose());
  assert.equal(h.mutations().length, 1);
});

test('the reused confirmation labels its dialog, focuses Cancel and traps keyboard focus without acting on Escape', t => {
  const previousDocument = globalThis.document, previousElement = globalThis.HTMLElement;
  let cursor = 0, effects = [], open = true, tree, accepted = 0;
  const slots = [], listeners = new Map(), appRoot = { inert: false };
  class Element {
    focus() { globalThis.document.activeElement = this; }
    querySelectorAll() { return buttons; }
  }
  const origin = new Element(); let buttons = [];
  globalThis.HTMLElement = Element;
  globalThis.document = { activeElement: origin, body: { style: { overflow: '' } }, getElementById: () => appRoot,
    addEventListener: (type, listener) => listeners.set(type, listener), removeEventListener: type => listeners.delete(type) };
  const hooks = {
    useId() { return `dialog-${cursor++}`; },
    useRef(initial) { const id = cursor++; return slots[id] ||= { current: initial }; },
    useEffect(callback, deps) { const id = cursor++, old = slots[id]; if (!old || deps.some((value, index) => value !== old.deps[index])) effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; }); },
  };
  const module = {};
  new Function('exports', 'require', compiled + '\nexports.ConfirmModal = ConfirmModal;')(module, name => name === 'react' ? hooks : name === 'react/jsx-runtime' ? jsxRuntime : name === 'react-dom' ? { createPortal: node => node } : {});
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  function close() { open = false; render(); }
  function render() {
    cursor = 0; effects = [];
    tree = module.ConfirmModal({ open, kind: 'question', title: 'Deploy AI Data Governance?', description: 'One shared module for all AIDP developers.', confirmLabel: 'Accept', onClose: close, onConfirm: () => { accepted++; } });
    buttons = nodes(tree).filter(node => node?.type === 'button').map(node => Object.assign(new Element(), { props: node.props }));
    for (const node of nodes(tree)) if (node?.props?.ref) node.props.ref.current = node.type === 'button' ? buttons.find(button => button.props === node.props) : new Element();
    effects.forEach(effect => effect());
  }
  t.after(() => { slots.forEach(slot => slot?.cleanup?.()); if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument; if (previousElement === undefined) delete globalThis.HTMLElement; else globalThis.HTMLElement = previousElement; });
  render();
  const dialog = nodes(tree).find(node => node?.props?.role === 'dialog');
  assert.equal(dialog.props['aria-modal'], 'true');
  assert.equal(nodes(tree).find(node => node?.props?.id === dialog.props['aria-labelledby']).props.children, 'Deploy AI Data Governance?');
  assert.match(nodes(tree).find(node => node?.props?.id === dialog.props['aria-describedby']).props.children, /shared module/);
  assert.equal(document.activeElement.props.children, 'Cancel'); assert.equal(appRoot.inert, true);
  buttons[1].focus(); listeners.get('keydown')({ key: 'Tab', preventDefault() {} });
  assert.equal(document.activeElement, buttons[0]);
  listeners.get('keydown')({ key: 'Tab', shiftKey: true, preventDefault() {} });
  assert.equal(document.activeElement, buttons[1]);
  listeners.get('keydown')({ key: 'Escape', preventDefault() {} });
  assert.equal(open, false); assert.equal(accepted, 0); assert.equal(document.activeElement, origin); assert.equal(appRoot.inert, false);
  open = true; render(); buttons[0].props.onClick();
  assert.equal(open, false); assert.equal(accepted, 0); assert.equal(document.activeElement, origin);
});

test('resuming a running application operation still asks and reuses its operation ID', async t => {
  const h = await harness(t, { ...release, update_available: false, operation: { operation_id: 'existing-operation', status: 'building' } });
  h.update(); assert.equal(h.dialog().props.open, true); assert.equal(h.mutations().length, 0);
  h.act(() => h.dialog().props.onConfirm());
  assert.equal(h.mutations().length, 1); assert.equal(h.dialog().props.open, false);
  assert.deepEqual(JSON.parse(h.mutations()[0].options.body), { operation_id: 'existing-operation' });
});

test('display time zone follows release details and keeps its independent save action', async t => {
  const h = await harness(t);
  const panel = h.find('section', props => props.id === 'settings-panel-application');
  const siblings = children => [children].flat(Infinity).filter(Boolean).flatMap(child => child.type === jsxRuntime.Fragment ? siblings(child.props.children) : [child]);
  const children = siblings(panel.props.children), releaseIndex = children.findIndex(child => child.type?.name === 'ApplicationReleaseSettings');
  assert.ok(releaseIndex >= 0); assert.equal(children[releaseIndex + 1].props.className, 'settings-time-zone-controls');
  const releaseCard = children[releaseIndex].type(children[releaseIndex].props);
  assert.ok(!JSON.stringify(releaseCard).includes('Existing participant installations remain unchanged'));
  assert.ok(!h.nodes().some(node => node.props?.id === 'time-zone-title' || node.props?.children === 'Regional settings'));
  h.act(() => h.find('select').props.onChange({ target: { value: 'UTC' } }));
  h.act(() => h.find('button', props => props.children === 'Save time zone').props.onClick());
  assert.equal(h.dialog().props.open, false); assert.equal(h.mutations().length, 1);
  assert.equal(h.mutations()[0].path, '/api/admin/settings'); assert.equal(h.mutations()[0].options.method, 'PUT');
  assert.deepEqual(JSON.parse(h.mutations()[0].options.body), { time_zone: 'UTC' });
  h.mutations()[0].resolve({ ...settings, time_zone: 'UTC' }); await h.settle();
  assert.equal(h.find('select').props.value, 'UTC');
  assert.equal(h.find('button', props => props.children === 'Save time zone').props.disabled, true);
});
