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
async function harness(t, currentRelease = release, route = '/admin/settings', initial = {}, managerProps = null) {
  let cursor = 0, dirty = true, effects = [], tree, closed = 0, changed = 0;
  const slots = [], requests = [], navigations = [], previous = { window: globalThis.window, document: globalThis.document, HTMLElement: globalThis.HTMLElement };
  const listeners = new Map(), storage = new Map(), timers = new Map(), appRoot = { inert: false };
  let focusable = [];
  class Element { focus() { document.activeElement = this; } querySelectorAll() { return focusable; } }
  const origin = new Element();
  globalThis.HTMLElement = Element;
  globalThis.document = { activeElement: origin, body: { style: { overflow: '' } }, getElementById: () => appRoot,
    addEventListener: (type, callback) => listeners.set(type, callback), removeEventListener: type => listeners.delete(type) };
  const location = new URL(route, 'https://example.test');
  globalThis.window = { location: { pathname: location.pathname, search: location.search, hash: '#application', origin: location.origin, assign(path) { navigations.push(path); }, reload() { assert.fail('Unexpected reload'); } },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    setTimeout: callback => { const id = timers.size + 1; timers.set(id, callback); return id; }, clearTimeout: id => timers.delete(id) };
  t.mock.method(globalThis, 'fetch', (path, options) => new Promise((resolve, reject) => {
    options.signal?.addEventListener('abort', () => reject(options.signal.reason), { once: true });
    requests.push({ path, options, reject, resolve: (body, status = 200) => resolve({ ok: status < 400, status, headers: new Headers(), json: async () => body }) });
  }));
  const hooks = {
    useId() { return `dialog-${cursor++}`; },
    useState(initial) { const id = cursor++; if (!(id in slots)) slots[id] = typeof initial === 'function' ? initial() : initial; return [slots[id], value => { const next = typeof value === 'function' ? value(slots[id]) : value; dirty ||= !Object.is(slots[id], next); slots[id] = next; }]; },
    useRef(initial) { const id = cursor++; return slots[id] ||= { current: initial }; },
    useEffect(callback, deps) { const id = cursor++, old = slots[id]; if (!old || deps.some((value, index) => value !== old.deps[index])) effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; }); },
  };
  const module = {};
  new Function('exports', 'require', compiled + '\nexports.GovernanceModuleManager = GovernanceModuleManager;')(module, name => name === 'react' ? hooks : name === 'react/jsx-runtime' ? jsxRuntime : name === 'react-dom' ? { createPortal: node => node } : name === './LoadingIndicator' ? { LoadingIndicator() {} } : name === './registrationPoll' ? { ...poll, pollRegistration: options => managerProps ? poll.pollRegistration({ ...options, sleep: async () => {} }) : options.request(options.signal) } : {});
  const component = managerProps ? () => module.GovernanceModuleManager({ ...managerProps, onClose: () => { closed++; }, onChanged: () => { changed++; } }) : module.App().type;
  function render() { for (let n = 0; dirty && n < 20; n++) {
    cursor = 0; dirty = false; effects = []; tree = component();
    const elements = nodes(tree).filter(node => node?.props).map(node => Object.assign(node.props.ref?.current instanceof Element ? node.props.ref.current : new Element(), { node, props: node.props }));
    focusable = elements.filter(({ node, props }) => ['button', 'select'].includes(node.type) && !props.disabled);
    for (const element of elements) if (element.props.ref) element.props.ref.current = element;
    effects.forEach(effect => effect());
  } assert.equal(dirty, false); }
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const find = (type, match = () => true) => { const value = nodes(tree).find(node => (node?.type === type || node?.type?.name === type) && match(node.props)); assert.ok(value, `Missing ${type}`); return value; };
  const act = callback => { callback(); render(); };
  const settle = async () => { await new Promise(setImmediate); render(); };
  t.after(() => { slots.forEach(slot => slot?.cleanup?.()); for (const [name, value] of Object.entries(previous)) { if (value === undefined) delete globalThis[name]; else globalThis[name] = value; } });
  render();
  for (const request of requests) if (initial[request.path] !== null) request.resolve(initial[request.path] ?? (request.path === '/api/admin/settings' ? settings : request.path === '/api/admin/application' ? currentRelease : { username: 'admin' }));
  await settle();
  return { requests, navigations, find, act, settle, nodes: () => nodes(tree), mutations: () => requests.filter(request => ['PUT', 'POST', 'DELETE'].includes(request.options.method)),
    closed: () => closed, changed: () => changed, listeners, appRoot, origin, focusable: () => focusable,
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

const governance = { module_id: 'ai_data_governance_vsc_extension', display_name: 'AI Data Governance', installed: false, status: 'not_installed', enabled: false, bundled_version: '3.0.0' };
const administrators = [
  { id: 'participant', email: 'student@example.test', name: 'Student', is_aidp_admin: false, active: true, labs: [] },
  { id: 'operator', email: 'operator@example.test', name: 'Operator', is_aidp_admin: true, active: true, managed: false, labs: [] },
];
const moduleInitial = module => ({ '/api/admin/users': { users: administrators }, '/api/admin/modules': { modules: [module] } });
const text = value => Array.isArray(value) ? value.map(text).join('') : value == null || typeof value === 'boolean' ? '' : typeof value === 'object' ? text(value.props?.children) : String(value);
const action = (h, label) => h.find('button', props => text(props.children) === label);
const managerHarness = (t, module = governance, props = {}, initial = {}) => harness(t, release, '/admin/settings', { ...moduleInitial(module), ...initial }, props);

test('Settings Governance icon opens one shared manager without navigation or deployment', async t => {
  const h = await harness(t, { ...release, packages: [{ package_id: governance.module_id, display_name: governance.display_name, bundled_version: '3.0.0', scope: 'global' }] });
  const card = h.find('ApplicationReleaseSettings');
  const tree = card.type(card.props);
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const button = nodes(tree).find(node => node?.props?.className === 'module-configure module-deploy');
  assert.equal(button.type, 'button'); assert.equal(button.props.type, 'button');
  assert.equal(button.props.children.type.name, 'RefreshIcon');
  assert.equal(button.props['aria-label'], 'Deploy or redeploy AI Data Governance');
  assert.equal(button.props['aria-haspopup'], 'dialog');
  assert.ok(!h.nodes().some(node => node?.type?.name === 'GovernanceModuleManager'));
  h.act(() => button.props.onClick());
  assert.equal(h.nodes().filter(node => node?.type?.name === 'GovernanceModuleManager').length, 1);
  assert.ok(!h.nodes().some(node => node?.type?.name === 'ConfirmModal' && node.props.open));
  assert.equal(h.navigations.length, 0); assert.equal(h.mutations().length, 0);
  h.act(() => h.find('GovernanceModuleManager').props.onClose());
  assert.ok(!h.nodes().some(node => node?.type?.name === 'GovernanceModuleManager'));
  assert.equal(h.navigations.length, 0); assert.equal(h.mutations().length, 0);
});

test('Users opens the same manager with its administrator preselected', async t => {
  const h = await harness(t, release, '/admin/users', { ...moduleInitial(governance), '/api/config': { deployment_mode: 'laboratory', labs: [] } });
  assert.equal(h.nodes().filter(node => node?.props?.className === 'table-action table-module').length, 1);
  h.act(() => h.find('button', props => props['aria-label'] === 'Manage AI Data Governance as operator@example.test').props.onClick());
  assert.equal(h.find('GovernanceModuleManager').props.initialUserId, 'operator');
  assert.equal(h.mutations().length, 0); assert.deepEqual(h.navigations, []);
  h.act(() => h.find('GovernanceModuleManager').props.onClose());
  assert.ok(!h.nodes().some(node => node?.type?.name === 'GovernanceModuleManager'));
});

for (const installed of [false, true]) test(`Governance ${installed ? 'redeploy' : 'deploy'} is direct, administrator-only and idempotent on double click`, async t => {
  const module = { ...governance, installed, enabled: installed, status: installed ? 'active' : 'not_installed', installed_version: installed ? '2.0.0' : null };
  const h = await managerHarness(t, module);
  const label = installed ? 'Redeploy' : 'Deploy';
  assert.equal(h.nodes().filter(node => node?.props?.role === 'dialog').length, 1);
  assert.ok(!h.nodes().some(node => node?.props?.type === 'checkbox' || node?.type?.name === 'ConfirmModal'));
  assert.match(text(h.nodes()[0]), /shared OCI credentials are retained/);
  assert.match(text(h.nodes()[0]), /Bundled 3.0.0/);
  if (installed) assert.match(text(h.nodes()[0]), /Installed 2.0.0/);
  assert.equal(action(h, label).props.disabled, true);
  assert.ok(!text(h.find('select')).includes('student@example.test'));
  h.act(() => h.find('select').props.onChange({ target: { value: 'participant' } }));
  h.act(() => action(h, label).props.onClick());
  assert.equal(h.mutations().length, 0);
  h.act(() => h.find('select').props.onChange({ target: { value: 'operator' } }));
  assert.equal(action(h, label).props.disabled, false);
  assert.equal(h.mutations().length, 0);
  const deploy = action(h, label).props.onClick;
  h.act(() => { deploy(); deploy(); });
  assert.equal(h.mutations().length, 1);
  assert.equal(h.find('section', props => props.role === 'dialog').props['aria-busy'], true);
  assert.equal(action(h, 'Cancel').props.disabled, true);
  const request = h.mutations()[0];
  assert.equal(request.path, `/api/admin/users/operator/modules/${governance.module_id}${installed ? '/redeploy' : ''}`);
  assert.equal(request.options.method, 'POST');
  assert.match(JSON.parse(request.options.body).operation_id, /^[\da-f-]{36}$/);
  request.reject(new Error('Deployment prerequisites unavailable'));
  await h.settle(); h.requests.at(-1).resolve({ modules: [module] }); await h.settle();
  assert.ok(h.nodes().some(node => node?.props?.role === 'alert' && text(node) === 'Deployment prerequisites unavailable'));
  assert.equal(h.nodes().filter(node => node?.props?.role === 'dialog').length, 1);
  assert.equal(h.find('section', props => props.role === 'dialog').props['aria-busy'], false);
  h.act(() => action(h, 'Cancel').props.onClick());
  assert.equal(h.closed(), 1); assert.equal(h.mutations().length, 1);
});

for (const path of ['/api/admin/users', '/api/admin/modules']) test(`Governance load failure at ${path} ends loading, blocks writes and can retry`, async t => {
  const h = await managerHarness(t, governance, { initialUserId: 'operator' }, { [path]: null });
  assert.ok(h.find('LoadingIndicator', props => props.label === 'Loading module settings…'));
  h.requests.find(request => request.path === path).reject(new Error('Service unavailable'));
  await h.settle();
  assert.ok(!h.nodes().some(node => node?.type?.name === 'LoadingIndicator'));
  assert.ok(h.find('p', props => props.role === 'alert' && props.children === 'Service unavailable'));
  assert.equal(action(h, 'Deploy').props.disabled, true);
  h.act(() => action(h, 'Deploy').props.onClick());
  assert.equal(h.mutations().length, 0);
  const count = h.requests.length;
  h.act(() => action(h, 'Retry settings').props.onClick());
  const reloaded = h.requests.slice(count);
  reloaded.find(request => request.path === '/api/admin/modules').resolve(moduleInitial(governance)['/api/admin/modules']);
  await h.settle();
  assert.equal(action(h, 'Deploy').props.disabled, true);
  h.act(() => action(h, 'Deploy').props.onClick());
  assert.equal(h.mutations().length, 0);
  reloaded.find(request => request.path === '/api/admin/users').resolve(moduleInitial(governance)['/api/admin/users']);
  await h.settle();
  assert.equal(action(h, 'Deploy').props.disabled, false);
  assert.ok(!h.nodes().some(node => node?.props?.role === 'alert'));
});

test('an empty global module response is unavailable rather than a permanent loader', async t => {
  const h = await managerHarness(t, governance, { initialUserId: 'operator' }, { '/api/admin/modules': { modules: [] } });
  assert.ok(!h.nodes().some(node => node?.type?.name === 'LoadingIndicator'));
  assert.ok(h.find('p', props => props.role === 'alert' && props.children === 'AI Data Governance is unavailable.'));
  assert.equal(action(h, 'Deploy').props.disabled, true);
  assert.equal(h.mutations().length, 0);
});

for (const [kind, status, label] of [['install', 'installing', 'installation'], ['redeploy', 'redeploying', 'redeployment'], ['delete', 'deleting', 'deletion']]) test(`Governance resumes ${kind} with the server's operation ID`, async t => {
  const operationId = 'server-owned-operation';
  const h = await managerHarness(t, { ...governance, status, operation_type: kind, operation_id: operationId }, { initialUserId: 'operator' });
  assert.equal(h.mutations().length, 0);
  const resume = action(h, `Resume ${label}`).props.onClick;
  h.act(() => { resume(); resume(); });
  assert.equal(h.mutations().length, 1);
  const request = h.mutations()[0];
  assert.equal(request.options.method, kind === 'delete' ? 'DELETE' : 'POST');
  if (kind === 'delete') assert.equal(new URL(request.path, 'https://example.test').searchParams.get('operation_id'), operationId);
  else assert.deepEqual(JSON.parse(request.options.body), { operation_id: operationId });
});

test('Governance preserves a replacement operation ID and shows progress inside the same dialog', async t => {
  const h = await managerHarness(t, governance, { initialUserId: 'operator' });
  h.act(() => action(h, 'Deploy').props.onClick());
  h.mutations()[0].resolve({ status: 'installing', operation_id: 'resumed-by-server', phase: 'content', message: 'Installing shared tools' });
  await h.settle();
  assert.equal(h.mutations().length, 2);
  assert.deepEqual(JSON.parse(h.mutations()[1].options.body), { operation_id: 'resumed-by-server' });
  assert.match(text(h.find('div', props => props.className === 'governance-module-progress')), /Installing shared tools/);
  assert.equal(h.nodes().filter(node => node?.props?.role === 'dialog').length, 1);
  h.mutations()[1].resolve({ status: 'active', message: 'Shared module ready' });
  await h.settle(); h.requests.at(-1).resolve({ modules: [{ ...governance, installed: true, status: 'active' }] }); await h.settle();
  assert.equal(h.changed(), 1);
  assert.equal(action(h, 'Close').props.disabled, false);
  assert.ok(!h.nodes().some(node => node?.props?.className === 'governance-module-progress'));
});

test('Governance deletion uses an inline warning, Back sends no request, and confirmation sends one DELETE', async t => {
  const h = await managerHarness(t, { ...governance, installed: true, status: 'active' }, { initialUserId: 'operator' });
  h.act(() => action(h, 'Delete').props.onClick());
  assert.equal(text(document.activeElement.props.children), 'Back');
  assert.equal(h.nodes().filter(node => node?.props?.role === 'dialog').length, 1);
  assert.ok(!h.nodes().some(node => node?.type?.name === 'ConfirmModal'));
  assert.match(text(h.find('p', props => props.className === 'lab-manager-warning')), /shared OCI credentials are retained/);
  assert.equal(h.mutations().length, 0);
  h.act(() => action(h, 'Back').props.onClick());
  assert.equal(h.closed(), 0); assert.equal(h.mutations().length, 0);
  assert.equal(action(h, 'Redeploy').props.disabled, false);
  h.act(() => action(h, 'Delete').props.onClick());
  const confirm = action(h, 'Delete module').props.onClick;
  h.act(() => { confirm(); confirm(); });
  assert.equal(h.mutations().length, 1); assert.equal(h.mutations()[0].options.method, 'DELETE');
  assert.match(new URL(h.mutations()[0].path, 'https://example.test').searchParams.get('operation_id'), /^[\da-f-]{36}$/);
  h.mutations()[0].resolve({ status: 'not_installed' });
  await h.settle(); h.requests.at(-1).resolve({ modules: [governance] }); await h.settle();
  assert.equal(h.changed(), 1);
  assert.ok(!h.nodes().some(node => node?.props?.className === 'lab-manager-warning'));
  assert.equal(action(h, 'Deploy').props.disabled, false);
});

test('the shared Governance dialog labels its scope, focuses Cancel and traps keyboard focus', async t => {
  const h = await managerHarness(t, governance);
  const dialog = h.find('section', props => props.role === 'dialog');
  assert.equal(dialog.props['aria-modal'], 'true');
  assert.equal(h.find('h2', props => props.id === dialog.props['aria-labelledby']).props.children, 'AI Data Governance');
  assert.match(text(h.find('p', props => props.id === dialog.props['aria-describedby'])), /shared module for all AIDP developers/);
  assert.equal(text(document.activeElement.props.children), 'Cancel');
  assert.equal(h.appRoot.inert, true);
  const first = h.focusable()[0], last = h.focusable().at(-1);
  last.focus(); h.listeners.get('keydown')({ key: 'Tab', preventDefault() {} }); assert.equal(document.activeElement, first);
  first.focus(); h.listeners.get('keydown')({ key: 'Tab', shiftKey: true, preventDefault() {} }); assert.equal(document.activeElement, last);
  h.listeners.get('keydown')({ key: 'Escape', preventDefault() {} });
  assert.equal(h.closed(), 1); assert.equal(h.mutations().length, 0);
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
