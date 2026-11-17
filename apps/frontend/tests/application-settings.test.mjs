import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';

const compiled = ts.transpileModule(readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const poll = {};
new Function('exports', ts.transpileModule(readFileSync(new URL('../src/registrationPoll.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText)(poll);
const assignments = {};
new Function('exports', ts.transpileModule(readFileSync(new URL('../src/labAssignments.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText)(assignments);
const settings = { aidp_service_endpoint: 'https://example.test/api', aidp_url: 'https://example.test/workbench', aidp_platform_id: 'platform', registration_code_configured: true, time_zone: 'America/Bogota', time_zones: ['America/Bogota', 'UTC'] };
const release = { current_release: 'v2.3.9', current_commit_sha: 'abc123', latest_release: 'v2.4.0', update_available: true, updater_available: true, operation: null, packages: [] };
async function harness(t, currentRelease = release, route = '/admin/settings', initial = {}, sessionState = new Map(), loginProps = null) {
  let cursor = 0, dirty = true, effects = [], tree;
  const slots = [], requests = [], navigations = [], scrolls = [], previous = { window: globalThis.window, document: globalThis.document, HTMLElement: globalThis.HTMLElement };
  const listeners = new Map(), storage = new Map(), timers = new Map(), intervals = new Map(), appRoot = { inert: false };
  let nextTimer = 0;
  let focusable = [];
  class Element { focus() { document.activeElement = this; } querySelectorAll() { return focusable; } scrollIntoView() {} }
  const origin = new Element();
  globalThis.HTMLElement = Element;
  globalThis.document = { activeElement: origin, body: { style: { overflow: '' } }, getElementById: () => appRoot,
    addEventListener: (type, callback) => listeners.set(type, callback), removeEventListener: type => listeners.delete(type) };
  const location = new URL(route, 'https://example.test');
  globalThis.window = { location: { pathname: location.pathname, hostname: location.hostname, search: location.search, hash: '#application', origin: location.origin, assign(path) { navigations.push(path); }, reload() { assert.fail('Unexpected reload'); } },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    sessionStorage: { getItem: key => sessionState.get(key) ?? null, setItem: (key, value) => sessionState.set(key, value), removeItem: key => sessionState.delete(key) },
    scrollY: 0, scrollTo: (x, y) => scrolls.push([x, y]),
    addEventListener: (type, callback) => listeners.set(`window:${type}`, callback), removeEventListener: type => listeners.delete(`window:${type}`),
    setInterval: callback => { const id = intervals.size + 1; intervals.set(id, callback); return id; }, clearInterval: id => intervals.delete(id),
    setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; }, clearTimeout: id => timers.delete(id) };
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
  new Function('exports', 'require', compiled + '\nexports.AdminLoginCard = AdminLoginCard;')(module, name => name === 'react' ? hooks : name === 'react/jsx-runtime' ? jsxRuntime : name === 'react-dom' ? { createPortal: node => node } : name === './LoadingIndicator' ? { LoadingIndicator() {} } : name === './SearchableCombobox' ? { SearchableCombobox() {} } : name === './labAssignments' ? assignments : name === './registrationPoll' ? { ...poll, pollRegistration: options => options.request(options.signal) } : {});
  const routeElement = module.App();
  const component = loginProps ? () => module.AdminLoginCard(loginProps) : () => routeElement.type(routeElement.props);
  function render() { for (let n = 0; dirty && n < 20; n++) {
    cursor = 0; dirty = false; effects = []; tree = component();
    const elements = nodes(tree).filter(node => node?.props).map(node => Object.assign(node.props.ref?.current instanceof Element ? node.props.ref.current : new Element(), { node, props: node.props }));
    focusable = elements.filter(({ node, props }) => ['button', 'select'].includes(node.type) && !props.disabled);
    for (const element of elements) if (element.props.ref) element.props.ref.current = element;
    effects.forEach(effect => effect());
  } assert.equal(dirty, false); }
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const find = (type, match = () => true) => { const value = nodes(tree).find(node => (node?.type === type || node?.type?.name === type) && match(node.props)); assert.ok(value, `Missing ${type}`); return value; };
  const act = callback => { callback(); dirty = true; render(); };
  const settle = async () => { await new Promise(setImmediate); render(); };
  t.after(() => { slots.forEach(slot => slot?.cleanup?.()); for (const [name, value] of Object.entries(previous)) { if (value === undefined) delete globalThis[name]; else globalThis[name] = value; } });
  render();
  for (const request of requests) if (initial[request.path] !== null) request.resolve(initial[request.path] ?? (request.path === '/api/admin/settings' ? settings : request.path === '/api/admin/application' ? currentRelease : { username: 'admin' }));
  await settle();
  return { requests, navigations, find, act, settle, nodes: () => nodes(tree), mutations: () => requests.filter(request => ['PUT', 'POST', 'DELETE'].includes(request.options.method)),
    listeners, appRoot, origin, intervals, timers, sessionState, scrolls, cleanup: () => slots.forEach(slot => slot?.cleanup?.()), focusable: () => focusable,
    dialog: () => find('ConfirmModal', props => props.title === 'Update application?'),
    update: () => act(() => { const button = find('button', props => props.className === 'settings-save application-update'); assert.ok(!button.props.disabled); button.props.onClick(); }) };
}

const adminLoginHarness = (t, route, props = {}) => harness(t, release, route, {}, new Map(), props);

for (const next of ['/gods-eye-view/', 'https://outside.test/', '//outside.test/', '/gods-eye-view/../api/', '', '/admin/settings']) {
  test(`login ignores return URLs and opens participant administration: ${next || '(none)'}`, async t => {
    const h = await adminLoginHarness(t, '/admin/login?next=' + encodeURIComponent(next));
    const view = '#v=2&lat=4.6&lon=-74.1&style=normal';
    window.location.hash = view;
    h.act(() => h.find('input', props => props.name === 'aidp-admin-username').props.onChange({ target: { value: 'admin' } }));
    h.act(() => h.find('input', props => props.name === 'aidp-admin-password').props.onChange({ target: { value: 'test-login' } }));
    const pending = h.find('form').props.onSubmit({ preventDefault() {} });
    assert.equal(h.navigations.length, 0);
    const request = h.mutations()[0];
    assert.equal(request.path, '/api/admin/login');
    assert.equal(request.options.credentials, 'include');
    request.resolve({}, 204); await pending; await h.settle();
    assert.deepEqual(h.navigations, ['/admin/users']);
    assert.equal(h.find('input', props => props.name === 'aidp-admin-password').props.value, '');
  });
}

test('failed administrator login stays on the form for retry', async t => {
  const h = await adminLoginHarness(t, '/admin/login?next=/gods-eye-view/');
  window.location.hash = '#v=2&lat=4.6';
  const pending = h.find('form').props.onSubmit({ preventDefault() {} });
  h.mutations()[0].resolve({ detail: 'Invalid administrator credentials' }, 401);
  await pending; await h.settle();
  assert.deepEqual(h.navigations, []);
  assert.equal(window.location.hash, '#v=2&lat=4.6');
  assert.equal(h.find('p', props => props.role === 'alert').props.children, 'Invalid administrator credentials');
});

test('general administrator login keeps its original presentation and registration shell', async t => {
  const h = await harness(t, release, '/admin/login?next=https://outside.test');
  assert.deepEqual(h.find('AdminLoginCard').props, {});
  assert.ok(!h.nodes().some(node => node?.props?.className === 'centered viewer-login'));
});

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

for (const configured of [false, true]) test(`registration code ${configured ? 'replacement' : 'activation'} requires confirmation and saves one captured draft`, async t => {
  const h = await harness(t, release, '/admin/settings', { '/api/admin/settings': { ...settings, registration_code_configured: configured } });
  const registration = () => h.find('RegistrationAccessSettings');
  const field = () => registration().type(registration().props);
  const dialog = () => h.find('ConfirmModal', props => props.title === 'Save registration code?');
  assert.equal(field().props.action.props.children, 'Save Code');
  assert.equal(field().props.action.props['aria-haspopup'], 'dialog');
  assert.equal(field().props.action.props.disabled, true);
  h.act(() => registration().props.onRegistrationCodeChange('DEMO-4321'));
  assert.equal(field().props.action.props.disabled, false);
  h.act(() => field().props.action.props.onClick());
  assert.equal(dialog().props.open, true);
  assert.match(dialog().props.description, configured ? /previous code will stop working.*Existing participants keep their access/ : /enable participant registration/);
  assert.ok(!dialog().props.description.includes('DEMO-4321'));
  assert.equal(h.mutations().length, 0);
  const cancelledConfirm = dialog().props.onConfirm;
  h.act(() => dialog().props.onClose());
  h.act(cancelledConfirm);
  assert.equal(h.mutations().length, 0);
  assert.equal(registration().props.registrationCode, 'DEMO-4321');
  h.act(() => field().props.action.props.onClick());
  const confirm = dialog().props.onConfirm;
  // A later state change must not replace the value approved by this dialog.
  h.act(() => registration().props.onRegistrationCodeChange('NEXT-5678'));
  h.act(() => { confirm(); confirm(); });
  assert.equal(dialog().props.open, false);
  assert.equal(h.mutations().length, 1);
  const request = h.mutations()[0];
  assert.equal(request.path, '/api/admin/settings');
  assert.equal(request.options.method, 'PUT');
  assert.deepEqual(JSON.parse(request.options.body), { registration_code: 'DEMO-4321' });
  assert.equal(registration().props.busy, true);
  assert.equal(field().props.action.props.disabled, true);
  h.act(() => registration().props.onSave());
  assert.equal(dialog().props.open, false);
  request.resolve({ ...settings, registration_code_configured: true }); await h.settle();
  assert.equal(registration().props.busy, false);
  assert.equal(registration().props.registrationCode, 'NEXT-5678');
  h.act(confirm);
  assert.equal(h.mutations().length, 1);
});

test('registration code validates incomplete drafts and preserves a rejected draft for a confirmed retry', async t => {
  const h = await harness(t);
  const registration = () => h.find('RegistrationAccessSettings');
  const dialog = () => h.find('ConfirmModal', props => props.title === 'Save registration code?');
  h.act(() => registration().props.onRegistrationCodeChange('DEMO-43'));
  h.act(() => registration().props.onSave());
  assert.equal(dialog().props.open, false); assert.equal(h.mutations().length, 0);
  h.act(() => registration().props.onRegistrationCodeChange('DEMO-4321'));
  h.act(() => registration().props.onSave());
  h.act(() => dialog().props.onConfirm());
  h.mutations()[0].resolve({ detail: 'Settings unavailable' }, 503); await h.settle();
  assert.equal(registration().props.busy, false);
  assert.equal(registration().props.registrationCode, 'DEMO-4321');
  assert.match(h.find('p', props => props.role === 'alert').props.children, /Settings unavailable/);
  h.act(() => registration().props.onSave());
  assert.equal(h.mutations().length, 1);
  h.act(() => dialog().props.onConfirm());
  h.mutations()[1].resolve({ ...settings, registration_code_configured: true }); await h.settle();
  assert.equal(registration().props.registrationCode, '');
  assert.equal(registration().props.busy, false);
});

test('registration settings always expose code configuration', async t => {
  const h = await harness(t, release, '/admin/settings');
  const registration = h.find('RegistrationAccessSettings');
  assert.equal(registration.type(registration.props).type.name, 'SettingsRegistrationCodeField');
  h.act(() => { registration.props.onRegistrationCodeChange('DEMO-4321'); registration.props.onSave(); });
  assert.equal(h.mutations().length, 0);
  h.act(() => h.find('RegistrationAccessSettings').props.onSave());
  assert.equal(h.find('ConfirmModal', props => props.title === 'Save registration code?').props.open, true);
});

test('application updates require confirmation, preserve the warning and cancel without dispatching', async t => {
  const h = await harness(t);
  assert.equal(text(h.find('button', props => props.className === 'settings-save application-update')), 'Update');
  assert.equal(h.dialog().props.open, false); h.update();
  assert.equal(h.dialog().props.open, true);
  assert.equal(h.dialog().props.confirmLabel, 'Accept');
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

const administrators = [
  { id: 'participant', email: 'student@example.test', name: 'Student', is_aidp_admin: false, active: true, labs: [] },
  { id: 'operator', email: 'operator@example.test', name: 'Operator', is_aidp_admin: true, active: true, managed: false, labs: [] },
];
const text = value => Array.isArray(value) ? value.map(text).join('') : value == null || typeof value === 'boolean' ? '' : typeof value === 'object' ? text(value.props?.children) : String(value);
const action = (h, label) => h.find('button', props => text(props.children) === label);
test('Users keeps participant actions and OCI administrator restrictions without module requests', async t => {
  const h = await harness(t, release, '/admin/users', { '/api/admin/users': { users: administrators }, '/api/config': { labs: [] } });
  assert.equal(h.nodes().filter(node => node?.props?.className === 'table-action table-module').length, 0);
  assert.ok(h.find('button', props => props['aria-label'] === 'Manage starter kits for student@example.test'));
  assert.ok(h.find('button', props => props['aria-label'] === 'Manage starter kits for operator@example.test'));
  assert.match(text(h.nodes()[0]), /AIDP administrator/);
  assert.ok(!h.nodes().some(node => node?.props?.['aria-label'] === 'Delete operator@example.test'));
  assert.ok(!h.nodes().some(node => node?.props?.className === 'table-action table-access' || node?.props?.children === 'Shared modules'));
  assert.ok(!h.requests.some(request => request.path === '/api/admin/modules'));
  assert.equal(h.mutations().length, 0); assert.deepEqual(h.navigations, []);
  assert.ok(!h.nodes().some(node => node?.type?.name === 'GovernanceModuleManager'));
});

for (const outcome of ['populated', 'empty', 'error']) test(`Users spinner ends on ${outcome} and does not show an empty table while waiting`, async t => {
  const h = await harness(t, release, '/admin/users', { '/api/admin/users': null, '/api/config': { labs: [] } });
  assert.ok(h.find('LoadingIndicator', props => props.label === 'Loading users…'));
  assert.equal(h.find('table').props['aria-busy'], true);
  assert.ok(!text(h.nodes()[0]).includes('No matching lab users.'));
  const request = h.requests.find(request => request.path === '/api/admin/users');
  request.resolve(outcome === 'error' ? { detail: 'Users unavailable' } : { users: outcome === 'empty' ? [] : administrators }, outcome === 'error' ? 503 : 200);
  await h.settle();
  assert.ok(!h.nodes().some(node => node?.props?.label === 'Loading users…'));
  assert.equal(h.find('table').props['aria-busy'], false);
  assert.equal(text(h.nodes()[0]).includes('No matching lab users.'), outcome === 'empty');
  if (outcome === 'populated') assert.ok(text(h.find('tbody')).includes('operator@example.test'));
  if (outcome === 'error') {
    assert.match(text(h.find('td', props => props.role === 'alert')), /Users unavailable/);
    h.act(() => h.find('button', props => props['aria-label'] === 'Refresh users').props.onClick());
    assert.ok(h.find('LoadingIndicator', props => props.label === 'Loading users…'));
    h.requests.at(-1).resolve({ users: administrators }); await h.settle();
    assert.equal(h.find('table').props['aria-busy'], false);
    assert.ok(!h.nodes().some(node => node?.type === 'td' && node.props.role === 'alert'));
  }
});

for (const oldFirst of [true, false]) test(`Users ignores an older request resolved ${oldFirst ? 'before' : 'after'} its replacement`, async t => {
  const h = await harness(t, release, '/admin/users', { '/api/admin/users': null, '/api/config': { labs: [] } });
  const previous = h.requests.find(request => request.path === '/api/admin/users');
  h.act(() => h.find('button', props => props['aria-label'] === 'Refresh users').props.onClick());
  if (oldFirst) {
    previous.resolve({ users: [] }); await h.settle();
    assert.equal(h.find('table').props['aria-busy'], true);
  }
  h.requests.at(-1).resolve({ users: administrators }); await h.settle();
  if (!oldFirst) { previous.resolve({ detail: 'Old failure' }, 500); await h.settle(); }
  assert.equal(h.find('table').props['aria-busy'], false);
  assert.ok(text(h.find('tbody')).includes('operator@example.test'));
  assert.ok(!text(h.nodes()[0]).includes('Old failure'));
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
    tree = module.ConfirmModal({ open, kind: 'question', title: 'Update application?', description: 'Update the installed application release.', confirmLabel: 'Accept', onClose: close, onConfirm: () => { accepted++; } });
    buttons = nodes(tree).filter(node => node?.type === 'button').map(node => Object.assign(new Element(), { props: node.props }));
    for (const node of nodes(tree)) if (node?.props?.ref) node.props.ref.current = node.type === 'button' ? buttons.find(button => button.props === node.props) : new Element();
    effects.forEach(effect => effect());
  }
  t.after(() => { slots.forEach(slot => slot?.cleanup?.()); if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument; if (previousElement === undefined) delete globalThis.HTMLElement; else globalThis.HTMLElement = previousElement; });
  render();
  const dialog = nodes(tree).find(node => node?.props?.role === 'dialog');
  assert.equal(dialog.props['aria-modal'], 'true');
  assert.equal(nodes(tree).find(node => node?.props?.id === dialog.props['aria-labelledby']).props.children, 'Update application?');
  assert.match(nodes(tree).find(node => node?.props?.id === dialog.props['aria-describedby']).props.children, /application release/);
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

for (const status of ['succeeded', 'up_to_date']) test(`release ${status} notice expires without changing operation state or focus`, async t => {
  const operation = { operation_id: 'completed-update', status, message: 'Application updated to v2.4.0.' };
  const h = await harness(t, { ...release, operation });
  const card = () => h.find('ApplicationReleaseSettings');
  assert.ok(text(card().type(card().props)).includes(operation.message));
  const [id, timer] = [...h.timers].find(([, timer]) => timer.delay === 8000);
  // Unrelated re-renders must not restart the dismissal countdown.
  h.act(() => h.find('SearchableCombobox', props => props.label === 'Display time zone').props.onChange('UTC'));
  assert.equal(h.timers.get(id), timer);
  h.act(() => { h.timers.delete(id); timer.callback(); });
  assert.ok(!text(card().type(card().props)).includes(operation.message));
  assert.deepEqual(card().props.release.operation, operation);
  assert.equal(document.activeElement, h.origin);
  assert.equal(h.mutations().length, 0);
  h.cleanup(); assert.equal(h.timers.size, 0);
});

for (const status of ['failed', 'building']) test(`release ${status} notice remains visible`, async t => {
  const operation = { operation_id: 'current-update', status, message: 'Update status requires attention.' };
  const h = await harness(t, { ...release, operation });
  assert.ok(![...h.timers.values()].some(timer => timer.delay === 8000));
  const card = h.find('ApplicationReleaseSettings');
  assert.ok(text(card.type(card.props)).includes(operation.message));
  assert.equal(h.mutations().length, 0);
});

test('package columns present scope, version and verified date in the saved display time zone', async t => {
  const updatedAt = '2026-10-31T01:00:00+00:00';
  const h = await harness(t, { ...release, packages: [
    { package_id: 'retail', display_name: 'Retail', bundled_version: '2.0.0', scope: 'participant', updated_at: updatedAt },
    { package_id: 'banking', display_name: 'Banking', bundled_version: '2.0.0', scope: 'participant', updated_at: null },
  ] });
  const card = h.find('ApplicationReleaseSettings');
  const tree = card.type(card.props);
  const flatten = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? flatten(child) : [])];
  const nodes = flatten(tree);
  assert.deepEqual(nodes.filter(node => node?.type === 'th').map(text), ['Starter kit', 'Scope', 'Version', 'Updated']);
  const region = nodes.find(node => node?.props?.className === 'release-packages-wrap');
  assert.equal(region.props.role, 'region'); assert.equal(region.props.tabIndex, 0);
  assert.equal(region.props['aria-label'], 'Starter kit versions');
  assert.match(text(nodes.find(node => node?.props?.id === region.props['aria-describedby'])), /Scroll horizontally/);
  const date = nodes.find(node => node?.type === 'time');
  assert.equal(date.props.dateTime, updatedAt);
  assert.equal(text(date), '2026-10-30');
  assert.match(date.props.title, /America\/Bogota/);
  assert.ok(text(tree).includes('Not available'));
  const changed = card.type({ ...card.props, displayTimeZone: 'UTC' });
  assert.equal(text(flatten(changed).find(node => node?.type === 'time')), '2026-10-31');
  const invalid = card.type({ ...card.props, release: { ...release, packages: [{ package_id: 'retail', display_name: 'Retail', bundled_version: '2.0.0', updated_at: 'invalid' }] } });
  assert.equal(text(flatten(invalid).find(node => node?.type === 'time')), 'Not available');
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
  const picker = () => h.find('SearchableCombobox', props => props.label === 'Display time zone');
  const dialog = () => h.find('ConfirmModal', props => props.title === 'Save time zone?');
  h.act(() => picker().props.onChange('UTC'));
  h.act(() => h.find('button', props => props.children === 'Save time zone').props.onClick());
  assert.equal(dialog().props.open, true); assert.match(dialog().props.description, /UTC \(UTC\+00:00\)/);
  assert.equal(h.mutations().length, 0);
  const cancelledConfirm = dialog().props.onConfirm;
  h.act(() => dialog().props.onClose()); h.act(cancelledConfirm);
  assert.equal(h.mutations().length, 0); assert.equal(picker().props.value, 'UTC');
  h.act(() => h.find('button', props => props.children === 'Save time zone').props.onClick());
  const confirm = dialog().props.onConfirm;
  h.act(() => picker().props.onChange('America/Bogota'));
  h.act(() => { confirm(); confirm(); });
  assert.equal(dialog().props.open, false); assert.equal(h.mutations().length, 1);
  assert.equal(h.mutations()[0].path, '/api/admin/settings'); assert.equal(h.mutations()[0].options.method, 'PUT');
  assert.deepEqual(JSON.parse(h.mutations()[0].options.body), { time_zone: 'UTC' });
  assert.equal(picker().props.disabled, true);
  h.mutations()[0].resolve({ ...settings, time_zone: 'UTC' }); await h.settle();
  assert.equal(picker().props.value, 'UTC');
  assert.equal(h.find('button', props => props.children === 'Save time zone').props.disabled, true);
  h.act(confirm); assert.equal(h.mutations().length, 1);
});

test('package rows sort newest first with stable ties and unknown dates last without mutating the API result', async t => {
  const packages = [
    { package_id: 'old', display_name: 'Old', updated_at: '2026-08-18T12:00:00Z' },
    { package_id: 'unknown', display_name: 'Unknown', updated_at: null },
    { package_id: 'z', display_name: 'Zebra', updated_at: '2026-10-31T12:00:00Z' },
    { package_id: 'invalid', display_name: 'Invalid', updated_at: 'invalid' },
    { package_id: 'a', display_name: 'Alpha', updated_at: '2026-10-31T12:00:00Z' },
  ].map(item => ({ ...item, bundled_version: '1.0.0', scope: 'participant' }));
  const original = JSON.stringify(packages);
  const h = await harness(t, { ...release, packages });
  const card = h.find('ApplicationReleaseSettings');
  const flatten = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? flatten(child) : [])];
  const body = flatten(card.type(card.props)).find(node => node?.type === 'tbody');
  assert.deepEqual(body.props.children.map(row => row.key), ['a', 'z', 'old', 'invalid', 'unknown']);
  assert.equal(JSON.stringify(packages), original);
});

for (const [state, expected] of [
  [{ latest_release: 'v2.8.2', current_release: 'v2.8.2', update_available: false }, null],
  [{ latest_release: '', update_available: false }, 'Check unavailable'],
  [{ update_available: true }, 'Update available'],
  [{ operation: { status: 'failed' } }, 'Update failed'],
  [{ operation: { status: 'building' } }, 'Updating'],
]) test(`release state has no redundant badge: ${expected || 'none'}`, async t => {
  const h = await harness(t, { ...release, ...state });
  const card = h.find('ApplicationReleaseSettings');
  const flatten = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? flatten(child) : [])];
  const badge = flatten(card.type(card.props)).find(node => node?.props?.className?.includes('release-state'));
  assert.equal(badge, undefined);
});

for (const scenario of [
  { name: 'the same failure from polling and the saved operation', error: 'Download failed; previous release restored.',
    operation: { status: 'failed', message: 'Download failed; previous release restored.' }, expected: 'Download failed; previous release restored.' },
  { name: 'a current request failure alongside an older failure', error: 'Unable to read the current update status.',
    operation: { status: 'failed', message: 'Earlier update failed.' }, expected: 'Unable to read the current update status.' },
  { name: 'a saved failure after reload', error: '',
    operation: { status: 'failed', message: 'Download failed; previous release restored.' }, expected: 'Download failed; previous release restored.' },
  { name: 'a failed operation with no detail', error: '', operation: { status: 'failed' }, expected: 'The application update failed.' },
]) test(`one update error is rendered for ${scenario.name}`, async t => {
  const h = await harness(t);
  const card = h.find('ApplicationReleaseSettings');
  const nodes = flattenElements(card.type({ ...card.props, error: scenario.error,
    release: { ...release, operation: scenario.operation, update_check_error: 'Registry check unavailable.' } }));
  const alerts = nodes.filter(node => node?.props?.role === 'alert');
  assert.equal(alerts.length, 1);
  assert.equal(text(alerts[0]), scenario.expected);
  assert.equal(nodes.filter(node => ['release-operation error', 'release-warning'].includes(node?.props?.className)).length, 1);
});

test('application update keeps the tab and reading position when the new version reloads', async t => {
  const h = await harness(t);
  const reloaded = [];
  window.location.hash = '';
  window.scrollY = 540;
  window.location.reload = () => reloaded.push(window.location.hash);
  h.update(); h.act(() => h.dialog().props.onConfirm());
  h.mutations()[0].resolve({ status: 'active', phase: 'complete' }); await h.settle();
  h.requests.filter(request => request.path === '/api/admin/application').at(-1).resolve({ ...release, current_release: 'v2.4.0' });
  await h.settle();
  assert.deepEqual(reloaded, ['application']);
  assert.deepEqual(JSON.parse(h.sessionState.get('aidp-settings-update-position')), { release: 'v2.4.0', y: 540 });
});

test('after an update, Application restores the position once after settings and release data load', async t => {
  const state = new Map([['aidp-settings-update-position', JSON.stringify({ release: 'v2.4.0', y: 540 })]]);
  const h = await harness(t, { ...release, current_release: 'v2.4.0' }, '/admin/settings', {}, state);
  const [id, timer] = [...h.timers].find(([, timer]) => timer.delay === 0);
  h.act(() => { h.timers.delete(id); timer.callback(); });
  assert.deepEqual(h.scrolls, [[0, 540]]);
  assert.ok(!state.has('aidp-settings-update-position'));
  assert.equal(h.find('section', props => props.id === 'settings-panel-application').props.hidden, false);
  h.act(() => h.find('button', props => props.id === 'settings-tab-workbench').props.onClick());
  assert.equal(window.location.hash, 'workbench');
  h.act(() => h.find('button', props => props.id === 'settings-tab-application').props.onClick());
  assert.equal(window.location.hash, 'application');
  assert.deepEqual(h.scrolls, [[0, 540]]);
});

test('Settings follows hash navigation for browser Back and Forward without moving focus', async t => {
  const h = await harness(t);
  const origin = document.activeElement;
  for (const tab of ['workbench', 'application']) {
    window.location.hash = '#' + tab;
    h.act(() => h.listeners.get('window:hashchange')());
    assert.equal(h.find('button', props => props.id === 'settings-tab-' + tab).props['aria-selected'], true);
    assert.equal(h.find('section', props => props.id === 'settings-panel-' + tab).props.hidden, false);
    assert.equal(document.activeElement, origin);
  }
});

test('a cancelled position timer can resume when Application becomes visible again', async t => {
  const state = new Map([['aidp-settings-update-position', JSON.stringify({ release: release.current_release, y: 320 })]]);
  const h = await harness(t, release, '/admin/settings', {}, state);
  const initialTimer = [...h.timers].find(([, timer]) => timer.delay === 0)[0];
  h.act(() => h.find('button', props => props.id === 'settings-tab-workbench').props.onClick());
  assert.equal(h.timers.has(initialTimer), false);
  assert.ok(state.has('aidp-settings-update-position'));
  h.act(() => h.find('button', props => props.id === 'settings-tab-application').props.onClick());
  const [id, timer] = [...h.timers].find(([, timer]) => timer.delay === 0);
  h.act(() => { h.timers.delete(id); timer.callback(); });
  assert.deepEqual(h.scrolls, [[0, 320]]);
  assert.equal(state.has('aidp-settings-update-position'), false);
});

test('failed time-zone save preserves the selection and requires confirmation for retry', async t => {
  const h = await harness(t);
  const picker = () => h.find('SearchableCombobox', props => props.label === 'Display time zone');
  const save = () => h.find('button', props => props.children === 'Save time zone');
  const dialog = () => h.find('ConfirmModal', props => props.title === 'Save time zone?');
  h.act(() => picker().props.onChange('UTC'));
  h.act(() => save().props.onClick()); h.act(() => dialog().props.onConfirm());
  h.act(() => save().props.onClick()); assert.equal(dialog().props.open, false);
  h.mutations()[0].resolve({ detail: 'Settings unavailable' }, 503); await h.settle();
  assert.equal(picker().props.value, 'UTC'); assert.equal(save().props.disabled, false);
  assert.match(text(h.find('p', props => props.role === 'alert')), /Settings unavailable/);
  h.act(() => save().props.onClick()); assert.equal(h.mutations().length, 1);
  h.act(() => dialog().props.onConfirm()); assert.equal(h.mutations().length, 2);
  assert.deepEqual(JSON.parse(h.mutations()[1].options.body), { time_zone: 'UTC' });
  h.mutations()[1].resolve({ ...settings, time_zone: 'UTC' }); await h.settle();
  assert.equal(save().props.disabled, true);
});



const flattenElements = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? flattenElements(child) : [])];

const kitCatalog = [
  ['banking', 'Banking'], ['telecommunications', 'Telecommunications'],
  ['telco_lineage', 'Telco Customer 360 Lineage'], ['retail', 'Retail'], ['healthcare', 'Healthcare'],
].map(([lab_id, display_name]) => ({ lab_id, display_name, pack_version: '2.0.0', available: true, status: 'available' }));
const participant = { id: 'participant', name: 'Student', email: 'student@example.test', active: true, status: 'active', managed: true,
  is_aidp_admin: false, labs: [{ lab_id: 'banking', pack_version: '2.0.0', phase: 'permissions' }] };
const kitInitial = (user = participant) => ({ '/api/config': { labs: kitCatalog }, '/api/admin/users': { users: [user] } });
const create = h => h.find('CreateUserModal');
const manager = h => h.find('LabManagerModal');
const modalControl = (h, component, type, match) => {
  const element = h.find(component);
  const result = flattenElements(element.type(element.props)).find(node => node?.type === type && match(node.props));
  assert.ok(result, `Missing ${component} ${type}`); return result;
};
const openCreate = h => h.act(() => h.find('button', props => props.className === 'create-user').props.onClick());
const openManager = h => h.act(() => h.find('button', props => props['aria-label'] === 'Manage starter kits for student@example.test').props.onClick());

test('Settings loads only settings, session and kit releases without module managers or endpoints', async t => {
  const packages = kitCatalog.map(kit => ({ package_id: kit.lab_id, display_name: kit.display_name, scope: 'participant', bundled_version: kit.pack_version }));
  const h = await harness(t, { ...release, packages });
  assert.deepEqual(h.requests.map(request => request.path).sort(), ['/api/admin/application', '/api/admin/session', '/api/admin/settings']);
  assert.ok(!h.nodes().some(node => /ModuleManager|GodsEye/.test(node?.type?.name || '')));
  const card = h.find('ApplicationReleaseSettings');
  const nodes = flattenElements(card.type(card.props));
  assert.deepEqual(nodes.find(node => node?.type === 'tbody').props.children.map(row => row.key).sort(), kitCatalog.map(kit => kit.lab_id).sort());
  assert.ok(!nodes.some(node => node?.type === 'button' || /module|agent|viewer/i.test(node?.props?.href || '')));
});

test('Add user presents five kits, requires an assignment, and sends only the participant contract once', async t => {
  const h = await harness(t, release, '/admin/users', kitInitial());
  openCreate(h);
  const controls = flattenElements(create(h).type(create(h).props)).filter(node => node?.type === 'input' && node.props.type === 'checkbox');
  assert.equal(controls.length, 5);
  assert.ok(controls.every(node => node.props.checked === false));
  assert.equal(modalControl(h, 'CreateUserModal', 'button', props => props.type === 'submit').props.disabled, true);
  assert.doesNotMatch(text(create(h).type(create(h).props)), /Local mode:|welcome file|no email|shared module|God’s Eye/i);
  h.act(() => create(h).props.onSubmit({ preventDefault() {} }));
  assert.equal(h.mutations().length, 0);
  assert.equal(create(h).props.error, 'Select at least one starter kit.');
  h.act(() => create(h).props.onDraftChange({ name: 'New User', email: 'new@example.test', lab_ids: ['banking', 'retail'] }));
  assert.equal(modalControl(h, 'CreateUserModal', 'button', props => props.type === 'submit').props.disabled, false);
  const submit = create(h).props.onSubmit;
  h.act(() => { submit({ preventDefault() {} }); submit({ preventDefault() {} }); });
  assert.equal(h.mutations().length, 1);
  assert.equal(h.mutations()[0].path, '/api/admin/users');
  assert.deepEqual(JSON.parse(h.mutations()[0].options.body), { name: 'New User', email: 'new@example.test', lab_ids: ['banking', 'retail'] });
  h.mutations()[0].resolve({ status: 'active', message: 'Created' }); await h.settle();
  h.requests.filter(request => request.path === '/api/admin/users' && request.options.method !== 'POST').at(-1).resolve({ users: [participant] }); await h.settle();
  assert.deepEqual(create(h).props.draft, { name: '', email: '', lab_ids: [] });
  assert.ok(!h.requests.some(request => /modules|gods-eye|viewer/.test(request.path)));
});

test('Manage keeps at least one participant kit and confirms removal before its API call', async t => {
  const h = await harness(t, release, '/admin/users', kitInitial());
  openManager(h);
  h.act(() => manager(h).props.onSelectionChange([]));
  assert.equal(modalControl(h, 'LabManagerModal', 'button', props => props.children === 'Save').props.disabled, true);
  h.act(() => manager(h).props.onSave());
  assert.equal(manager(h).props.error, 'A participant must keep at least one starter kit.');
  assert.equal(h.mutations().length, 0);
  h.act(() => manager(h).props.onSelectionChange(['retail']));
  h.act(() => manager(h).props.onSave());
  assert.equal(manager(h).props.confirmingRemoval, true);
  assert.equal(h.mutations().length, 0);
  h.act(() => manager(h).props.onClose());
  assert.equal(manager(h).props.confirmingRemoval, false);
  assert.equal(h.mutations().length, 0);
});

test('Manage exposes kit inventory but blocks all mutations for unmanaged OCI users', async t => {
  const h = await harness(t, release, '/admin/users', kitInitial({ ...participant, managed: false }));
  openManager(h);
  const nodes = flattenElements(manager(h).type(manager(h).props));
  assert.equal(nodes.filter(node => node?.type === 'input' && node.props.type === 'checkbox').length, 5);
  assert.ok(nodes.filter(node => node?.type === 'input' && node.props.type === 'checkbox').every(node => node.props.disabled));
  assert.ok(!nodes.some(node => node?.props?.className === 'table-action table-reset'));
  assert.ok(!h.nodes().some(node => node?.props?.['aria-label'] === 'Delete student@example.test'));
  assert.equal(h.mutations().length, 0);
});

test('a failed kit addition preserves its selection and retries only the uncompleted assignment', async t => {
  const h = await harness(t, release, '/admin/users', kitInitial());
  openManager(h);
  h.act(() => manager(h).props.onSelectionChange(['banking', 'retail']));
  h.act(() => manager(h).props.onSave());
  assert.equal(h.mutations().length, 1);
  assert.equal(h.mutations()[0].path, '/api/admin/users/participant/labs');
  assert.deepEqual(JSON.parse(h.mutations()[0].options.body), { lab_id: 'retail' });
  h.mutations()[0].resolve({ detail: 'Workspace unavailable' }, 500); await h.settle();
  h.requests.filter(request => request.path === '/api/admin/users').at(-1).resolve({ users: [participant] }); await h.settle();
  assert.equal(manager(h).props.error, 'Workspace unavailable');
  assert.deepEqual(manager(h).props.selectedLabIds, ['banking', 'retail']);
  h.act(() => manager(h).props.onSave());
  assert.equal(h.mutations().length, 2);
  assert.deepEqual(JSON.parse(h.mutations()[1].options.body), { lab_id: 'retail' });
});
