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
const settings = { aidp_service_endpoint: 'https://example.test/api', aidp_url: 'https://example.test/workbench', aidp_platform_id: 'platform', deployment_mode: 'laboratory', registration_code_configured: true, time_zone: 'America/Bogota', time_zones: ['America/Bogota', 'UTC'] };
const release = { current_release: 'v2.3.9', current_commit_sha: 'abc123', latest_release: 'v2.4.0', update_available: true, updater_available: true, operation: null, packages: [] };
async function harness(t, currentRelease = release, route = '/admin/settings', initial = {}, managerProps = null, sessionState = new Map()) {
  let cursor = 0, dirty = true, effects = [], tree, closed = 0, changed = 0;
  const slots = [], requests = [], navigations = [], previous = { window: globalThis.window, document: globalThis.document, HTMLElement: globalThis.HTMLElement };
  const listeners = new Map(), storage = new Map(), timers = new Map(), intervals = new Map(), appRoot = { inert: false };
  let focusable = [];
  class Element { focus() { document.activeElement = this; } querySelectorAll() { return focusable; } scrollIntoView() {} }
  const origin = new Element();
  globalThis.HTMLElement = Element;
  globalThis.document = { activeElement: origin, body: { style: { overflow: '' } }, getElementById: () => appRoot,
    addEventListener: (type, callback) => listeners.set(type, callback), removeEventListener: type => listeners.delete(type) };
  const location = new URL(route, 'https://example.test');
  globalThis.window = { location: { pathname: location.pathname, hostname: location.hostname, search: location.search, hash: '#application', origin: location.origin, assign(path) { navigations.push(path); }, reload() { assert.fail('Unexpected reload'); } },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    sessionStorage: { getItem: key => sessionState.get(key) ?? null, setItem: (key, value) => sessionState.set(key, value) },
    setInterval: callback => { const id = intervals.size + 1; intervals.set(id, callback); return id; }, clearInterval: id => intervals.delete(id),
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
  new Function('exports', 'require', compiled + '\nexports.GovernanceModuleManager = GovernanceModuleManager; exports.AdminLoginCard = AdminLoginCard;')(module, name => name === 'react' ? hooks : name === 'react/jsx-runtime' ? jsxRuntime : name === 'react-dom' ? { createPortal: node => node } : name === './LoadingIndicator' ? { LoadingIndicator() {} } : name === './SearchableCombobox' ? { SearchableCombobox() {} } : name === './GodsEyeViewModuleManager' ? { GodsEyeViewModuleManager() {}, godsEyeServices: Array(6) } : name === './labAssignments' ? assignments : name === './registrationPoll' ? { ...poll, pollRegistration: options => managerProps ? poll.pollRegistration({ ...options, sleep: async () => {} }) : options.request(options.signal) } : {});
  const component = managerProps ? () => module.GovernanceModuleManager({ ...managerProps, onClose: () => { closed++; }, onChanged: () => { changed++; } }) : location.pathname === '/admin/login' ? module.AdminLoginCard : module.App().type;
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
    closed: () => closed, changed: () => changed, listeners, appRoot, origin, intervals, sessionState, cleanup: () => slots.forEach(slot => slot?.cleanup?.()), focusable: () => focusable,
    pollModule: () => act(() => { assert.equal(intervals.size, 1); [...intervals.values()][0](); }),
    dialog: () => find('ConfirmModal', props => props.title === 'Update application?'),
    update: () => act(() => { const button = find('button', props => props.className === 'settings-save application-update'); assert.ok(!button.props.disabled); button.props.onClick(); }) };
}

for (const next of ['/gods-eye-view/', 'https://outside.test/', '//outside.test/', '/gods-eye-view/../api/', '', '/admin/settings']) {
  test(`login accepts only the fixed viewer return target: ${next || '(none)'}`, async t => {
    const h = await harness(t, release, '/admin/login?next=' + encodeURIComponent(next));
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
    assert.deepEqual(h.navigations, [next === '/gods-eye-view/' ? '/gods-eye-view/' + view : '/admin/users']);
    assert.equal(h.find('input', props => props.name === 'aidp-admin-password').props.value, '');
  });
}

test('failed viewer login stays on the form and preserves the map fragment for retry', async t => {
  const h = await harness(t, release, '/admin/login?next=/gods-eye-view/');
  window.location.hash = '#v=2&lat=4.6';
  const pending = h.find('form').props.onSubmit({ preventDefault() {} });
  h.mutations()[0].resolve({ detail: 'Invalid administrator credentials' }, 401);
  await pending; await h.settle();
  assert.deepEqual(h.navigations, []);
  assert.equal(window.location.hash, '#v=2&lat=4.6');
  assert.equal(h.find('p', props => props.role === 'alert').props.children, 'Invalid administrator credentials');
});

for (const error of ['', 'access_denied', 'sign_in_failed', '<script>untrusted</script>']) test(`viewer sign-in uses OCI redirect and a predefined error only: ${error || 'none'}`, async t => {
  const h = await harness(t, release, '/viewer/login?error=' + encodeURIComponent(error), { '/api/config': { viewer_signin_enabled: true } });
  assert.equal(h.find('a', props => props.children === 'Sign in with OCI').props.href, '/api/auth/oci/login');
  assert.equal(h.find('a', props => props.children === 'Administrator sign-in').props.href, '/admin/login?next=/gods-eye-view/');
  assert.ok(!h.nodes().some(node => node?.type === 'input' || node?.type === 'form'));
  assert.deepEqual(h.requests.map(request => request.path), ['/api/config']);
  const alert = h.nodes().find(node => node?.props?.role === 'alert');
  assert.equal(Boolean(alert), ['access_denied', 'sign_in_failed'].includes(error));
  if (alert) assert.match(text(alert), error === 'access_denied' ? /Contact your administrator/ : /Try again/);
  assert.ok(!text(h.nodes()[0]).includes('<script>'));
});

for (const enabled of [false, undefined, 'true']) test(`viewer sign-in stays disabled without an explicit enabled capability: ${enabled}`, async t => {
  const h = await harness(t, release, '/viewer/login', { '/api/config': { viewer_signin_enabled: enabled } });
  assert.equal(h.find('button', props => props.children === 'Sign in with OCI').props.disabled, true);
  assert.ok(!h.nodes().some(node => node?.props?.href === '/api/auth/oci/login'));
  assert.match(text(h.nodes()[0]), /OCI sign-in is unavailable/);
  assert.equal(h.mutations().length, 0);
});

test('viewer sign-in stays disabled while capability loading fails', async t => {
  const h = await harness(t, release, '/viewer/login', { '/api/config': null });
  assert.equal(h.find('button', props => props.children === 'Sign in with OCI').props.disabled, true);
  h.requests[0].reject(new Error('Config unavailable')); await h.settle();
  assert.ok(!h.nodes().some(node => node?.props?.href === '/api/auth/oci/login'));
  assert.match(text(h.nodes()[0]), /OCI sign-in is unavailable/);
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

test('production registration settings expose no code save action', async t => {
  const h = await harness(t, release, '/admin/settings', { '/api/admin/settings': { ...settings, deployment_mode: 'production' } });
  const registration = h.find('RegistrationAccessSettings');
  assert.equal(registration.type(registration.props), null);
  h.act(() => { registration.props.onRegistrationCodeChange('DEMO-4321'); registration.props.onSave(); });
  assert.equal(h.mutations().length, 0);
  assert.equal(h.find('ConfirmModal', props => props.title === 'Save registration code?').props.open, false);
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

const governance = { module_id: 'ai_data_governance', display_name: 'AI Data Governance', installed: false, status: 'not_installed', enabled: false, bundled_version: '3.0.0' };
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
  assert.equal(button.props.children.type.name, 'InstallIcon');
  assert.equal(button.props['aria-label'], 'Deploy or redeploy AI Data Governance');
  assert.equal(button.props['aria-haspopup'], 'dialog');
  assert.equal(h.find('GovernanceModuleManager').props.visible, false);
  h.act(() => button.props.onClick());
  assert.equal(h.nodes().filter(node => node?.type?.name === 'GovernanceModuleManager').length, 1);
  assert.equal(h.find('GovernanceModuleManager').props.visible, true);
  assert.ok(!h.nodes().some(node => node?.type?.name === 'ConfirmModal' && node.props.open));
  assert.equal(h.navigations.length, 0); assert.equal(h.mutations().length, 0);
  h.act(() => h.find('GovernanceModuleManager').props.onClose());
  assert.equal(h.find('GovernanceModuleManager').props.visible, false);
  assert.equal(h.navigations.length, 0); assert.equal(h.mutations().length, 0);
});

test('Users keeps participant actions but global installation stays in Settings', async t => {
  const h = await harness(t, release, '/admin/users', { ...moduleInitial(governance), '/api/config': { deployment_mode: 'laboratory', labs: [] } });
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

const viewerReady = { module_id: 'gods_eye_view', status: 'ready', installed: true, enabled: true, bundled_version: '1.0.1' };
const viewerUser = { ...administrators[0], status: 'active', gods_eye_view_access: false };
const banking = { lab_id: 'banking', display_name: 'Banking', pack_version: '2.0.0', available: true };
const retail = { lab_id: 'retail', display_name: 'Retail', pack_version: '2.0.0', available: true };
const membershipInitial = (user = viewerUser, module = viewerReady) => ({ '/api/config': { labs: [banking, retail] },
  '/api/admin/users': { users: [user] }, '/api/admin/gods-eye-view/module': module });
const manager = h => h.find('LabManagerModal');
const create = h => h.find('CreateUserModal');
const subnodes = element => { const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])]; return nodes(element.type(element.props)); };
const modalControl = (h, type, nodeType, match) => { const node = subnodes(h.find(type)).find(node => node?.type === nodeType && match(node.props)); assert.ok(node, `Missing ${type} ${nodeType}`); return node; };
const godCheck = (h, type = 'LabManagerModal') => modalControl(h, type, 'input', props => props['aria-label']?.startsWith(type === 'CreateUserModal' ? 'Select God’s Eye' : 'God’s Eye'));
const openManager = h => h.act(() => h.find('button', props => props['aria-label'] === 'Manage starter kits for student@example.test').props.onClick());
const openCreate = h => h.act(() => h.find('button', props => props.className === 'create-user').props.onClick());
const finishUsers = async (h, user) => { h.requests.filter(request => request.path === '/api/admin/users' && request.options.method !== 'POST').at(-1).resolve({ users: [user] }); await h.settle(); };

test('A viewer-only user shows shared access without claiming participant kits are active', async t => {
  const h = await harness(t, release, '/admin/users', membershipInitial({ ...viewerUser, labs: [], gods_eye_view_access: true }));
  const summary = h.find('div', props => props.className === 'lab-summary');
  assert.match(text(summary), /0 starter kits/);
  assert.match(text(summary), /God’s Eye View · Custom layers.*Shared module access/);
  assert.doesNotMatch(text(summary), /All active|\d+ active/);
});

for (const mixed of [false, true]) test(`Add user starts unchecked and sends ${mixed ? 'mixed kit and shared access' : 'God-only access'} separately`, async t => {
  const h = await harness(t, release, '/admin/users', membershipInitial());
  openCreate(h);
  assert.deepEqual(create(h).props.draft.lab_ids, []);
  assert.equal(Boolean(create(h).props.draft.gods_eye_view), false);
  assert.ok(subnodes(create(h)).filter(node => node?.type === 'input' && node.props.type === 'checkbox').every(node => !node.props.checked));
  assert.equal(modalControl(h, 'CreateUserModal', 'button', props => props.type === 'submit').props.disabled, true);
  h.act(() => create(h).props.onDraftChange({ ...create(h).props.draft, name: 'New User', email: 'new@example.test' }));
  h.act(() => godCheck(h, 'CreateUserModal').props.onChange({ target: { checked: true } }));
  if (mixed) h.act(() => modalControl(h, 'CreateUserModal', 'input', props => props['aria-label'] === 'Select Banking starter kit').props.onChange({ target: { checked: true } }));
  assert.equal(modalControl(h, 'CreateUserModal', 'button', props => props.type === 'submit').props.disabled, false);
  assert.match(text(subnodes(create(h))), /1\.0\.1/);
  assert.match(text(modalControl(h, 'CreateUserModal', 'span', props => props.className === 'lab-selection-count')), mixed ? /2 selected/ : /1 selected/);
  const submit = create(h).props.onSubmit;
  h.act(() => { submit({ preventDefault() {} }); submit({ preventDefault() {} }); });
  assert.equal(h.mutations().length, 1);
  assert.deepEqual(JSON.parse(h.mutations()[0].options.body), { name: 'New User', email: 'new@example.test', lab_ids: mixed ? ['banking'] : [], gods_eye_view: true });
  h.mutations()[0].resolve({ status: 'active', message: 'Created' }); await h.settle();
  await finishUsers(h, viewerUser);
  assert.deepEqual(create(h).props.draft.lab_ids, []);
  assert.equal(Boolean(create(h).props.draft.gods_eye_view), false);
  openCreate(h);
  h.act(() => godCheck(h, 'CreateUserModal').props.onChange({ target: { checked: true } }));
  h.act(() => create(h).props.onClose());
  openCreate(h);
  assert.deepEqual(create(h).props.draft.lab_ids, []);
  assert.equal(Boolean(create(h).props.draft.gods_eye_view), false);
});

for (const [condition, module, userOverride = {}] of [
  ['module missing', { ...viewerReady, installed: false }],
  ['module activating', { ...viewerReady, status: 'activating' }],
  ['module disabled', { ...viewerReady, enabled: false }],
  ['module malformed installed', { ...viewerReady, installed: 'false' }],
  ['module malformed enabled', { ...viewerReady, enabled: null }],
  ['module loading', null],
  ['module error', null],
  ['identity inactive', viewerReady, { active: false }],
  ['user pending', viewerReady, { status: 'pending' }],
]) test(`God access selection blocks grants when ${condition}`, async t => {
  const user = { ...viewerUser, ...userOverride };
  const h = await harness(t, release, '/admin/users', membershipInitial(user, module));
  if (condition === 'module error') {
    h.requests.find(request => request.path === '/api/admin/gods-eye-view/module').reject(new Error('Module unavailable')); await h.settle();
  }
  openManager(h);
  assert.equal(godCheck(h).props.disabled, true);
  assert.equal(h.mutations().length, 0);
  if (!condition.startsWith('identity') && condition !== 'user pending') {
    h.act(() => manager(h).props.onClose()); openCreate(h);
    assert.equal(godCheck(h, 'CreateUserModal').props.disabled, true);
    const godRow = modalControl(h, 'CreateUserModal', 'tr', props => text(props.children).includes('God’s Eye View'));
    assert.match(text(godRow), /Unavailable/);
    assert.ok(!text(godRow).includes('Available'));
  }
});

for (const enabled of [false, true]) test(`Manage starter kits ${enabled ? 'grants' : 'revokes'} shared access through Save without kit provisioning`, async t => {
  const h = await harness(t, release, '/admin/users', membershipInitial({ ...viewerUser, gods_eye_view_access: !enabled }));
  openManager(h);
  h.act(() => godCheck(h).props.onChange({ target: { checked: enabled } }));
  assert.equal(h.mutations().length, 0);
  h.act(() => manager(h).props.onSave());
  if (!enabled) {
    assert.equal(h.mutations().length, 0);
    assert.equal(manager(h).props.confirmingRemoval, true);
    assert.match(text(subnodes(manager(h))), /including existing sessions/);
    h.act(() => manager(h).props.onClose());
    assert.equal(manager(h).props.confirmingRemoval, false);
    h.act(() => manager(h).props.onSave());
  }
  const save = manager(h).props.onSave;
  h.act(() => { save(); save(); });
  assert.equal(h.mutations().length, 1);
  const request = h.mutations()[0];
  assert.equal(request.path, '/api/admin/gods-eye-view/users/participant');
  assert.equal(request.options.method, 'PUT');
  assert.deepEqual(JSON.parse(request.options.body), { enabled });
  request.resolve({ enabled }); await h.settle();
  await finishUsers(h, { ...viewerUser, gods_eye_view_access: enabled });
  assert.equal(manager(h).props.open, false);
  assert.ok(!h.mutations().some(request => /modules|labs/.test(request.path)));
  assert.ok(!h.nodes().some(node => node?.props?.className === 'table-action table-access' || node?.props?.children === 'Shared modules'));
});

test('Manage allows revocation for inactive users while the shared module is unavailable', async t => {
  const h = await harness(t, release, '/admin/users', membershipInitial({ ...viewerUser, active: false, gods_eye_view_access: true }, { ...viewerReady, installed: false }));
  openManager(h);
  assert.equal(godCheck(h).props.disabled, false);
  h.act(() => godCheck(h).props.onChange({ target: { checked: false } }));
  h.act(() => manager(h).props.onSave());
  h.act(() => manager(h).props.onSave());
  assert.deepEqual(JSON.parse(h.mutations()[0].options.body), { enabled: false });
});

test('Manage supports unmanaged OCI accounts while preventing all kit mutations and user deletion', async t => {
  const h = await harness(t, release, '/admin/users', membershipInitial({ ...viewerUser, managed: false }));
  assert.match(text(h.nodes()[0]), /Existing OCI user/);
  assert.doesNotMatch(text(h.nodes()[0]), /AIDP administrator/);
  openManager(h);
  assert.ok(subnodes(manager(h)).filter(node => node?.type === 'input' && node.props['aria-label']?.includes('Banking')).every(node => node.props.disabled));
  assert.ok(!subnodes(manager(h)).some(node => node?.props?.className === 'table-action table-reset'));
  assert.ok(!h.nodes().some(node => node?.props?.['aria-label'] === 'Delete student@example.test'));
  h.act(() => manager(h).props.onSelectionChange(['banking']));
  h.act(() => godCheck(h).props.onChange({ target: { checked: true } }));
  h.act(() => manager(h).props.onSave());
  assert.equal(h.mutations().length, 1);
  assert.equal(h.mutations()[0].path, '/api/admin/gods-eye-view/users/participant');
});

test('Shared access never substitutes for the last assigned participant kit', async t => {
  const h = await harness(t, release, '/admin/users', membershipInitial({ ...viewerUser, labs: [{ lab_id: 'banking', pack_version: '2.0.0', phase: 'active' }] }));
  openManager(h);
  h.act(() => manager(h).props.onSelectionChange([]));
  h.act(() => godCheck(h).props.onChange({ target: { checked: true } }));
  assert.equal(modalControl(h, 'LabManagerModal', 'button', props => text(props.children) === 'Save').props.disabled, true);
  h.act(() => manager(h).props.onSave());
  assert.equal(h.mutations().length, 0);
  assert.match(manager(h).props.error, /at least one starter kit/);
});

for (const enabled of [false, true]) test(`Manage reconciles a lost ${enabled ? 'grant' : 'revoke'} response with an explicit membership read`, async t => {
  const h = await harness(t, release, '/admin/users', membershipInitial({ ...viewerUser, gods_eye_view_access: !enabled }));
  openManager(h);
  h.act(() => godCheck(h).props.onChange({ target: { checked: enabled } }));
  h.act(() => manager(h).props.onSave());
  if (!enabled) h.act(() => manager(h).props.onSave());
  h.mutations()[0].reject(new Error('Response lost')); await h.settle();
  await finishUsers(h, { ...viewerUser, gods_eye_view_access: enabled });
  await finishUsers(h, { ...viewerUser, gods_eye_view_access: enabled });
  assert.equal(manager(h).props.open, false);
  assert.equal(h.mutations().length, 1);
});

test('A failed membership write retains the selected grant for a deliberate Save retry', async t => {
  const h = await harness(t, release, '/admin/users', membershipInitial());
  openManager(h);
  h.act(() => godCheck(h).props.onChange({ target: { checked: true } }));
  h.act(() => manager(h).props.onSave());
  h.mutations()[0].reject(new Error('Access unavailable')); await h.settle();
  await finishUsers(h, viewerUser); await finishUsers(h, viewerUser);
  assert.equal(manager(h).props.open, true);
  assert.equal(manager(h).props.selectedViewerAccess, true);
  assert.equal(manager(h).props.error, 'Access unavailable');
  h.act(() => manager(h).props.onSave());
  assert.equal(h.mutations().length, 2);
  assert.deepEqual(JSON.parse(h.mutations()[1].options.body), { enabled: true });
});

for (const result of [{ enabled: false }, {}, undefined]) test(`An unverified PUT body preserves grant retry until the native membership agrees: ${JSON.stringify(result)}`, async t => {
  const h = await harness(t, release, '/admin/users', membershipInitial());
  openManager(h);
  h.act(() => godCheck(h).props.onChange({ target: { checked: true } }));
  h.act(() => manager(h).props.onSave());
  h.mutations()[0].resolve(result); await h.settle();
  await finishUsers(h, viewerUser); await finishUsers(h, viewerUser);
  assert.equal(manager(h).props.open, true);
  assert.equal(manager(h).props.selectedViewerAccess, true);
  assert.match(manager(h).props.error, /access was not confirmed/);
  assert.equal(h.mutations().length, 1);
});

test('A membership field omitted during lost revocation remains unverified and retryable', async t => {
  const h = await harness(t, release, '/admin/users', membershipInitial({ ...viewerUser, gods_eye_view_access: true }));
  openManager(h);
  h.act(() => godCheck(h).props.onChange({ target: { checked: false } }));
  h.act(() => manager(h).props.onSave()); h.act(() => manager(h).props.onSave());
  h.mutations()[0].reject(new Error('Response lost')); await h.settle();
  const { gods_eye_view_access, ...unknown } = viewerUser;
  await finishUsers(h, unknown); await finishUsers(h, unknown);
  assert.equal(manager(h).props.open, true);
  assert.equal(manager(h).props.error, 'Response lost');
  assert.equal(h.mutations().length, 1);
});

test('A mixed Save preserves unapplied kit selections after confirmed revocation and kit failure', async t => {
  const installed = [{ lab_id: 'banking', pack_version: '2.0.0', phase: 'active' }];
  const h = await harness(t, release, '/admin/users', membershipInitial({ ...viewerUser, labs: installed, gods_eye_view_access: true }));
  openManager(h);
  h.act(() => manager(h).props.onSelectionChange(['banking', 'retail']));
  h.act(() => godCheck(h).props.onChange({ target: { checked: false } }));
  h.act(() => manager(h).props.onSave()); h.act(() => manager(h).props.onSave());
  h.mutations()[0].resolve({ enabled: false }); await h.settle();
  assert.equal(h.mutations()[1].path, '/api/admin/users/participant/labs');
  h.mutations()[1].reject(new Error('Kit unavailable')); await h.settle();
  await finishUsers(h, { ...viewerUser, labs: installed });
  assert.deepEqual(manager(h).props.selectedLabIds, ['banking', 'retail']);
  assert.equal(manager(h).props.selectedViewerAccess, false);
  assert.equal(manager(h).props.error, 'Kit unavailable');
  h.act(() => manager(h).props.onSave());
  assert.equal(h.mutations()[2].path, '/api/admin/users/participant/labs');
  assert.equal(h.mutations().filter(request => request.options.method === 'PUT').length, 1);
});

for (const state of [null, { status: 'available', installed: false, enabled: false },
  { status: 'activating', installed: true, enabled: true }, { status: 'activating', installed: false, enabled: false, resumable: true },
  { status: 'failed', installed: false, enabled: false }, { status: 'ready', installed: 'false', enabled: true },
  { status: 'ready', installed: true, enabled: false }, { status: 'ready', installed: true, enabled: true }]) {
test(`Settings configures the viewer only after verified installation: ${state?.status || 'loading'} ${state?.enabled}`, async t => {
  const h = await harness(t, { ...release, packages: [{ package_id: 'gods_eye_view', display_name: 'God’s Eye View · Custom layers', scope: 'global', bundled_version: '1.0.0' }] }, '/admin/settings', {
    '/api/admin/gods-eye-view/module': state && { module_id: 'gods_eye_view', ...state },
  });
  const card = h.find('ApplicationReleaseSettings');
  const children = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? children(child) : [])];
  const nodes = children(card.type(card.props));
  const install = nodes.find(node => node?.type === 'button' && node.props.className === 'module-configure module-deploy');
  assert.equal(install.type, 'button');
  assert.equal(install.props.children.type.name, state?.status === 'activating' && state.resumable !== true ? 'LoadingIndicator' : 'InstallIcon');
  if (state?.status === 'activating' && state.resumable !== true) assert.equal(install.props.children.props.label, 'Deployment in progress');
  assert.equal(install.props['aria-haspopup'], 'dialog');
  const configure = nodes.find(node => node?.props?.['aria-label'] === 'Configure God’s Eye View · Custom layers');
  assert.equal(Boolean(configure), state?.installed === true && state.enabled === true && state.status === 'ready');
  if (configure) assert.equal(configure.props.href, '/admin/gods-eye-view');
  h.act(() => install.props.onClick());
  assert.equal(h.nodes().filter(node => node?.type?.name === 'GodsEyeViewModuleManager').length, 1);
  assert.equal(h.mutations().length, 0);
  h.act(() => h.find('GodsEyeViewModuleManager').props.onChanged());
  const refresh = h.requests.filter(request => request.path === '/api/admin/gods-eye-view/module').at(-1);
  refresh.resolve({ module_id: 'gods_eye_view', status: 'ready', installed: true, enabled: true }); await h.settle();
  assert.equal(h.find('ApplicationReleaseSettings').props.godsEyeReady, true);
  h.act(() => h.find('GodsEyeViewModuleManager').props.onClose());
  assert.ok(!h.nodes().some(node => node?.type?.name === 'GodsEyeViewModuleManager'));
});
}

for (const hostname of ['localhost', '127.0.0.1', '[::1]', 'example.test', 'localhost.example']) test(`Settings observes real module state and ignores saved simulation steps on ${hostname}`, async t => {
  const state = new Map([['aidp-module.preview.gods_eye_view.platform', '6'], ['aidp-module.preview.ai_data_governance.platform', '5']]);
  const h = await harness(t, release, `http://${hostname}/admin/settings`, { '/api/admin/gods-eye-view/module': { module_id: 'gods_eye_view', status: 'activating', installed: false, enabled: false } }, null, state);
  assert.equal(h.find('ApplicationReleaseSettings').props.godsEyeInstalling, true);
  assert.equal(h.find('ApplicationReleaseSettings').props.godsEyeReady, false);
  h.act(() => h.find('GovernanceModuleManager').props.onStatusChange({ module: { ...governance, status: 'installing', operation_id: 'saved-operation' }, busy: false, unverified: false }));
  assert.equal(h.find('ApplicationReleaseSettings').props.governanceInstalling, true);
  h.act(() => h.find('ApplicationReleaseSettings').props.onConfigureGodsEye());
  for (const name of ['GovernanceModuleManager', 'GodsEyeViewModuleManager']) {
    const props = h.find(name).props;
    assert.equal(props.preview, undefined); assert.equal(props.previewStep, undefined); assert.equal(props.onPreviewStep, undefined);
  }
  assert.equal(h.mutations().length, 0);
  assert.deepEqual([...h.sessionState], [...state]);
});

test('Settings restores server deployment progress and polls without overlapping or starting another installation', async t => {
  const h = await harness(t, release, '/admin/settings', { '/api/admin/gods-eye-view/module': {
    module_id: 'gods_eye_view', status: 'activating', installed: false, enabled: false, stage: 'volumes', operation_id: 'existing-operation',
  } });
  const card = () => h.find('ApplicationReleaseSettings').props;
  assert.equal(card().godsEyeInstalling, true); assert.equal(card().godsEyeAction, 'View deployment progress for');
  h.pollModule(); h.pollModule();
  assert.equal(h.requests.filter(request => request.path === '/api/admin/gods-eye-view/module').length, 2);
  const pending = h.requests.at(-1);
  pending.resolve({ module_id: 'gods_eye_view', status: 'activating', installed: false, enabled: false }); await h.settle();
  h.act(() => card().onConfigureGodsEye());
  assert.equal(window.location.hash, 'application'); assert.equal(h.intervals.size, 0);
  h.act(() => h.find('GodsEyeViewModuleManager').props.onClose());
  assert.equal(h.intervals.size, 1); assert.equal(card().godsEyeInstalling, true);
  h.requests.at(-1).resolve({ module_id: 'gods_eye_view', status: 'ready', installed: true, enabled: true }); await h.settle();
  assert.equal(card().godsEyeInstalling, false); assert.equal(card().godsEyeReady, true); assert.equal(h.intervals.size, 0);
  assert.equal(h.mutations().length, 0);
});

test('a temporarily unreadable deployment status remains recoverable and never appears verified', async t => {
  const h = await harness(t, release, '/admin/settings', { '/api/admin/gods-eye-view/module': {
    module_id: 'gods_eye_view', status: 'activating', installed: true, enabled: true,
  } });
  h.pollModule(); h.requests.at(-1).reject(new Error('Status unavailable')); await h.settle();
  assert.equal(h.find('ApplicationReleaseSettings').props.godsEyeInstalling, false);
  assert.equal(h.find('ApplicationReleaseSettings').props.godsEyeReady, false);
  assert.equal(h.find('ApplicationReleaseSettings').props.godsEyeAction, 'Check installation status for');
  h.pollModule(); h.requests.at(-1).resolve({ module_id: 'gods_eye_view', status: 'ready', installed: true, enabled: true }); await h.settle();
  assert.equal(h.find('ApplicationReleaseSettings').props.godsEyeReady, true); assert.equal(h.intervals.size, 0);
  assert.equal(h.mutations().length, 0);
});

test('leaving Settings releases its status poll and ignores late responses', async t => {
  const h = await harness(t, release, '/admin/settings', { '/api/admin/gods-eye-view/module': {
    module_id: 'gods_eye_view', status: 'activating', installed: false, enabled: false,
  } });
  h.pollModule(); const pending = h.requests.at(-1); h.cleanup();
  assert.equal(pending.options.signal.aborted, true); assert.equal(h.intervals.size, 0);
  pending.resolve({ module_id: 'gods_eye_view', status: 'ready', installed: true, enabled: true }); await h.settle();
  assert.equal(h.find('ApplicationReleaseSettings').props.godsEyeReady, false); assert.equal(h.mutations().length, 0);
});

test('Settings hides viewer configuration when installation status cannot be read', async t => {
  const h = await harness(t, release, '/admin/settings', { '/api/admin/gods-eye-view/module': null });
  h.requests.find(request => request.path === '/api/admin/gods-eye-view/module').reject(new Error('Unavailable'));
  await h.settle();
  assert.equal(h.find('ApplicationReleaseSettings').props.godsEyeReady, false);
  assert.equal(h.mutations().length, 0);
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

for (const installed of [false, true]) test(`Governance ${installed ? 'redeploy' : 'deploy'} is direct, administrator-only and idempotent on double click`, async t => {
  const module = { ...governance, installed, enabled: installed, status: installed ? 'active' : 'not_installed', installed_version: installed ? '2.0.0' : null };
  const h = await managerHarness(t, module);
  const label = installed ? 'Redeploy' : 'Deploy';
  assert.equal(h.nodes().filter(node => node?.props?.role === 'dialog').length, 1);
  assert.ok(!h.nodes().some(node => node?.props?.type === 'checkbox' || node?.type?.name === 'ConfirmModal'));
  assert.match(text(h.nodes()[0]), /shared OCI credentials are retained/);
  assert.ok(!h.nodes().some(node => node.props?.className === 'governance-module-note kit-version-copy'));
  assert.equal(h.find('SearchableCombobox').props.placeholder, 'Select an administrator');
  assert.match(text(h.find('ul', props => props['aria-label'] === 'Services')), /AIDP WorkbenchObject StorageSpark ComputeMaster CatalogAI Compute/);
  assert.equal(text(h.find('span', props => props.className.startsWith('lab-state'))), installed ? 'active' : 'not installed');
  assert.equal(action(h, label).props.disabled, true);
  assert.ok(!h.find('SearchableCombobox').props.options.some(option => option.value === 'participant'));
  h.act(() => h.find('SearchableCombobox').props.onChange('participant'));
  h.act(() => action(h, label).props.onClick());
  assert.equal(h.mutations().length, 0);
  h.act(() => h.find('SearchableCombobox').props.onChange('operator'));
  assert.equal(action(h, label).props.disabled, false);
  assert.equal(h.mutations().length, 0);
  const deploy = action(h, label).props.onClick;
  h.act(() => { deploy(); deploy(); });
  assert.equal(h.mutations().length, 1);
  assert.equal(h.find('section', props => props.role === 'dialog').props['aria-busy'], true);
  assert.equal(action(h, 'Close').props.disabled, undefined);
  assert.ok(!h.nodes().some(node => node?.type?.name === 'SearchableCombobox'));
  assert.ok(h.find('div', props => props.className === 'registration-result module-install-progress'));
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
  assert.match(text(h.find('div', props => props.className === 'registration-result module-install-progress')), /Installing shared tools/);
  assert.equal(h.find('div', props => props.role === 'progressbar').props['aria-valuenow'], undefined);
  assert.equal(h.nodes().filter(node => node?.props?.role === 'dialog').length, 1);
  h.mutations()[1].resolve({ status: 'active', message: 'Shared module ready' });
  await h.settle(); h.requests.at(-1).resolve({ modules: [{ ...governance, installed: true, status: 'active' }] }); await h.settle();
  assert.equal(h.changed(), 1);
  assert.notEqual(action(h, 'Close').props.disabled, true);
  assert.ok(!h.nodes().some(node => node?.props?.className === 'registration-result module-install-progress'));
});

test('closing Governance hides its monitor without aborting reconciliation and it can reopen while pending', async t => {
  const statuses = [];
  const props = { initialUserId: 'operator', onStatusChange: state => statuses.push(state) };
  const h = await managerHarness(t, governance, props);
  h.act(() => action(h, 'Deploy').props.onClick());
  const request = h.mutations()[0];
  h.act(() => { action(h, 'Close').props.onClick(); props.visible = false; });
  assert.equal(h.closed(), 1); assert.equal(h.appRoot.inert, false);
  assert.equal(h.nodes()[0], null); assert.equal(request.options.signal.aborted, false);
  request.resolve({ status: 'installing', phase: 'agent', message: 'Starting the agent', operation_id: 'original-operation' });
  await h.settle();
  assert.equal(h.mutations().length, 2); assert.equal(statuses.at(-1).busy, true);
  h.act(() => { props.visible = true; });
  assert.equal(h.appRoot.inert, true);
  assert.equal(text(h.find('p', props => props.className === 'registration-progress-phase')), 'AI Compute');
  assert.ok(!h.nodes().some(node => node?.type?.name === 'SearchableCombobox'));
  h.mutations()[1].resolve({ status: 'active', message: 'Module ready' });
  await h.settle(); h.requests.at(-1).resolve({ modules: [{ ...governance, status: 'active', installed: true }] }); await h.settle();
  assert.equal(h.changed(), 1); assert.equal(statuses.at(-1).busy, false);
});

test('a late Governance response after navigation cannot continue reconciliation or report completion', async t => {
  const h = await managerHarness(t, governance, { initialUserId: 'operator' });
  h.act(() => action(h, 'Deploy').props.onClick());
  const request = h.mutations()[0];
  h.cleanup();
  request.resolve({ status: 'active', operation_id: 'late-operation' });
  await h.settle();
  assert.equal(request.options.signal.aborted, true);
  assert.equal(h.changed(), 0); assert.equal(h.mutations().length, 1);
});

test('Governance Deploy on localhost submits a real operation once and displays server phases', async t => {
  const h = await harness(t, release, 'http://localhost:18083/admin/settings', moduleInitial(governance), { initialUserId: 'operator' });
  assert.doesNotMatch(text(h.nodes()[0]), /Simulation|Restart preview|Next step/);
  assert.equal(h.mutations().length, 0);
  const deploy = action(h, 'Deploy').props.onClick;
  h.act(() => { deploy(); deploy(); });
  assert.equal(h.mutations().length, 1);
  assert.equal(h.mutations()[0].path, '/api/admin/users/operator/modules/ai_data_governance');
  assert.equal(h.mutations()[0].options.method, 'POST');
  h.mutations()[0].resolve({ status: 'installing', phase: 'sync', operation_id: 'native-operation', message: 'Reading native catalog metadata' });
  await h.settle();
  assert.equal(text(h.find('p', props => props.className === 'registration-progress-phase')), 'Spark Compute · Master Catalog');
  assert.equal(h.find('div', props => props.role === 'progressbar').props['aria-valuenow'], undefined);
  assert.deepEqual(JSON.parse(h.mutations()[1].options.body), { operation_id: 'native-operation' });
  assert.match(text(h.nodes()[0]), /Reading native catalog metadata/);
});

test('Settings reads native Governance pending and terminal states without starting an operation', async t => {
  const h = await harness(t);
  for (const state of [
    { module: { ...governance, status: 'installing', operation_id: 'persisted' }, busy: false, unverified: false },
    { module: governance, busy: true, unverified: false },
    { module: governance, busy: false, unverified: false },
    { module: { ...governance, status: 'installing' }, busy: false, unverified: true },
  ]) {
    h.act(() => h.find('GovernanceModuleManager').props.onStatusChange(state));
    assert.equal(h.find('ApplicationReleaseSettings').props.governanceInstalling, !state.unverified && (state.busy || state.module.status === 'installing'));
  }
  assert.equal(h.mutations().length, 0);
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
