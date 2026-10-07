import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';

const source = readFileSync(new URL('../src/GodsEyeViewModuleManager.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const available = { module_id: 'gods_eye_view', enabled: false, installed: false,
  status: 'available', runtime: 'aidp', bundled_version: '1.0.1', message: 'Install the private viewer and its shared AIDP resources.' };
const pending = { ...available, status: 'activating', operation_id: 'existing-operation', stage: 'infrastructure', message: 'Creating the private viewer.' };
const ready = { ...pending, status: 'ready', enabled: true, installed: true, message: 'Publication and workflows verified.' };
const users = [
  { id: 'participant', email: 'student@example.test', is_aidp_admin: false },
  { id: 'operator', email: 'operator@example.test', is_aidp_admin: true },
  { id: 'other', email: 'other@example.test', is_aidp_admin: true },
];

function harness(t, onAdministrators = request => request.resolve({ users }), options = {}) {
  const slots = [], requests = [], timers = new Map();
  let cursor = 0, dirty = true, effects = [], tree, timerId = 0, changed = 0, closed = 0, modal = false, focus = '';
  const globals = { window: globalThis.window, document: globalThis.document, HTMLElement: globalThis.HTMLElement };
  globalThis.window = { setInterval(callback) { timers.set(++timerId, callback); return timerId; }, clearInterval(id) { timers.delete(id); } };
  globalThis.HTMLElement = class { isConnected = true; focus() { focus = 'origin'; } };
  globalThis.document = { activeElement: new globalThis.HTMLElement(), body: {} };
  const hooks = {
    useId() { return `module-id-${cursor++}`; },
    useState(initial) { const id = cursor++; if (!(id in slots)) slots[id] = initial;
      return [slots[id], value => { const next = typeof value === 'function' ? value(slots[id]) : value; dirty ||= !Object.is(slots[id], next); slots[id] = next; }]; },
    useRef(initial) { const id = cursor++; return slots[id] ||= { current: initial }; },
    useEffect(callback, deps) { const id = cursor++, old = slots[id]; if (!old || deps.some((value, index) => !Object.is(value, old.deps[index])))
      effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; }); },
  };
  const component = {};
  const LoadingIndicator = () => null;
  const dependencies = {
    react: hooks,
    'react-dom': { createPortal: content => content },
    'react/jsx-runtime': jsxRuntime,
    './SearchableCombobox': { SearchableCombobox() {} },
    './LoadingIndicator': { LoadingIndicator },
  };
  new Function('exports', 'require', compiled)(component, name => {
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected import ${name}`);
    return dependencies[name];
  });
  const props = { api: (path, options) => new Promise((resolve, reject) => requests.push({ path, ...options, resolve, reject })),
    onClose: () => closed++, onChanged: () => changed++, ...options };
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const text = value => typeof value === 'string' ? value : [value?.props?.children].flat(Infinity).filter(Boolean).map(text).join(' ');
  function render() {
    for (let turns = 0; dirty && turns < 20; turns++) {
      dirty = false; cursor = 0; effects = []; tree = component.GodsEyeViewModuleManager(props);
      for (const node of nodes(tree)) if (node?.props?.ref) node.props.ref.current = node.type === 'dialog'
        ? { showModal() { modal = true; }, close() { modal = false; } } : { focus() { focus = 'close'; } };
      effects.forEach(effect => effect());
      requests.filter(request => request.path === '/api/admin/users').forEach(onAdministrators);
    }
    assert.equal(dirty, false);
  }
  const find = (type, match = () => true) => { const node = nodes(tree).find(node => (node?.type === type || node?.type?.name === type) && match(node.props)); assert.ok(node, `Missing ${String(type)}`); return node; };
  const act = callback => { callback(); render(); };
  const cleanup = () => slots.forEach(slot => slot?.cleanup?.());
  t.after(() => { cleanup(); for (const [name, value] of Object.entries(globals)) if (value === undefined) delete globalThis[name]; else globalThis[name] = value; });
  render();
  return { get requests() { return requests.filter(request => request.path !== '/api/admin/users'); },
    get administratorRequests() { return requests.filter(request => request.path === '/api/admin/users'); },
    timers, find, cleanup, changed: () => changed, closed: () => closed, modal: () => modal, focus: () => focus, text: () => text(tree),
    selectAdministrator: (value = 'operator') => act(() => find('SearchableCombobox').props.onChange(value)),
    nodes: () => nodes(tree),
    loading: () => nodes(tree).filter(node => node?.type === LoadingIndicator).map(node => node.props.label),
    buttons: () => nodes(tree).filter(node => node?.type === 'button').map(node => node.props.children),
    click: label => act(() => { const button = find('button', props => props.children === label); assert.ok(!button.props.disabled); button.props.onClick(); }),
    doubleClick: label => act(() => { const click = find('button', props => props.children === label).props.onClick; click(); click(); }),
    escape: () => act(() => find('dialog').props.onCancel({ preventDefault() {} })),
    poll: () => act(() => { assert.equal(timers.size, 1); [...timers.values()][0](); }),
    settle: async () => { await new Promise(setImmediate); render(); } };
}

test('the single installation dialog shows services and directly deploys once', async t => {
  const view = harness(t);
  assert.equal(view.modal(), true); assert.equal(view.focus(), 'close');
  assert.deepEqual(view.loading(), ['Loading module settings…']);
  assert.equal(view.requests[0].path, '/api/admin/gods-eye-view/module'); assert.equal(view.requests[0].method, undefined);
  assert.deepEqual(view.buttons(), ['Close']);
  view.requests[0].resolve(available); await view.settle();
  assert.equal(view.find('SearchableCombobox').props.placeholder, 'Select an administrator');
  assert.deepEqual(view.find('SearchableCombobox').props.options.map(option => option.value), ['operator', 'other']);
  assert.doesNotMatch(view.text(), /student@example.test/);
  assert.equal(view.find('button', props => props.children === 'Deploy').props.disabled, true);
  assert.match(view.text(), /Not installed/);
  assert.ok(!view.nodes().some(node => node.props?.className === 'governance-module-note kit-version-copy'));
  assert.equal(view.find('ul', props => props['aria-label'] === 'Services').props.children.length, 6);
  assert.match(view.text(), /OCI Compute.*Object Storage.*AIDP Workbench.*Master Catalog.*Spark Compute.*AI Compute/);
  view.selectAdministrator('participant'); view.doubleClick('Deploy'); assert.equal(view.requests.length, 1);
  assert.ok(!view.nodes().some(node => node?.props?.['aria-label'] === 'Deployment resources'));
  assert.equal(view.nodes().filter(node => node?.type === 'dialog').length, 1);
  view.selectAdministrator(); view.doubleClick('Deploy'); assert.equal(view.requests.length, 2);
  assert.equal(view.requests[1].method, 'POST'); assert.equal(view.requests[1].path, '/api/admin/users/operator/modules/gods_eye_view');
  assert.equal(view.requests[1].body, undefined);
  assert.equal(view.find('button', props => props.children === 'Close').props.disabled, undefined);
  view.requests[1].resolve(ready); await view.settle();
  assert.equal(view.changed(), 1); assert.deepEqual(view.buttons(), ['Close', 'Verify']);
  assert.match(view.text(), /Publication and workflows verified/);
  const oldVerify = view.find('button', props => props.children === 'Verify').props.onClick;
  view.escape(); assert.equal(view.closed(), 1); view.cleanup(); assert.equal(view.modal(), false); assert.equal(view.focus(), 'origin');
  oldVerify(); assert.equal(view.requests.length, 2);
});

test('reopening a live installation polls GET without overlap and displays the actual phase with standard progress', async t => {
  const view = harness(t); view.requests[0].resolve(pending); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Deploying…']);
  assert.equal(view.find('button', props => props.children === 'Deploying…').props.disabled, true);
  assert.equal(view.nodes().filter(node => node?.type?.name === 'SearchableCombobox').length, 0);
  assert.equal(view.find('p', props => props.className === 'registration-progress-phase').props.children, 'OCI Compute');
  assert.match(view.text(), /Creating the private viewer/);
  assert.match(view.text(), /Installation continues in the background/);
  assert.equal(view.find('div', props => props.className === 'registration-result module-install-progress').props.role, 'status');
  view.find('span', props => props.className === 'progress-orbit');
  const track = view.find('div', props => props.role === 'progressbar');
  assert.equal(track.props.className, 'registration-progress-track gods-eye-view-reset-track');
  assert.equal(track.props['aria-valuenow'], undefined);
  assert.doesNotMatch(view.text(), /\d+%/);
  view.poll(); view.poll(); assert.equal(view.requests.length, 2);
  assert.equal(view.requests[1].method, undefined);
  view.requests[1].resolve({ ...pending, stage: 'aidp', message: 'Preparing the shared workflows.' }); await view.settle();
  assert.equal(view.find('p', props => props.className === 'registration-progress-phase').props.children, 'AIDP Workbench');
  assert.match(view.text(), /Preparing the shared workflows/);
  for (const [stage, service] of [['controls', 'Object Storage'], ['volumes', 'Master Catalog'], ['computes', 'Spark Compute · AI Compute'],
    ['workflows', 'AIDP Workbench'], ['agent', 'AI Compute'], ['streaming', 'Spark Compute'], ['publication', 'Object Storage'], ['verification', 'Verifying services…']]) {
    view.poll(); view.requests.at(-1).resolve({ ...pending, stage, message: `Native ${stage} phase.` }); await view.settle();
    assert.equal(view.find('p', props => props.className === 'registration-progress-phase').props.children, service);
    assert.match(view.text(), new RegExp(`Native ${stage} phase`));
    assert.equal(view.find('div', props => props.role === 'progressbar').props['aria-valuenow'], undefined);
  }
  view.poll(); view.requests.at(-1).resolve(ready); await view.settle();
  assert.equal(view.changed(), 1); assert.equal(view.timers.size, 0);
  view.selectAdministrator(); view.click('Verify'); assert.equal(view.requests.at(-1).method, 'POST');
  view.requests.at(-1).resolve(ready); await view.settle(); assert.equal(view.changed(), 2);
});

test('Close dismisses live progress without sending an installation cancellation', async t => {
  const view = harness(t); view.requests[0].resolve(pending); await view.settle(); view.poll();
  assert.deepEqual(view.buttons(), ['Close', 'Deploying…']);
  view.click('Close'); assert.equal(view.closed(), 1); view.cleanup();
  assert.equal(view.requests.length, 2); assert.equal(view.requests[1].signal.aborted, true);
  assert.ok(view.requests.every(request => request.method !== 'POST'));
  assert.equal(view.changed(), 0); assert.equal(view.focus(), 'origin');
});

test('a stopped worker resumes directly, and a stale action cannot resume a running worker', async t => {
  const view = harness(t); view.requests[0].resolve({ ...pending, resumable: true }); await view.settle(); view.selectAdministrator();
  assert.deepEqual(view.buttons(), ['Close', 'Resume deployment']);
  assert.equal(view.find('SearchableCombobox').props.disabled, false);
  const stale = view.find('button', props => props.children === 'Resume deployment').props.onClick;
  view.poll(); view.requests[1].resolve({ ...pending, resumable: 'true' }); await view.settle();
  stale(); assert.equal(view.requests.length, 2);
  view.poll(); view.requests[2].resolve({ ...pending, resumable: true }); await view.settle();
  view.doubleClick('Resume deployment'); assert.equal(view.requests[3].method, 'POST');
  view.requests[3].resolve(pending); await view.settle(); assert.equal(view.requests[3].body, undefined);
});

test('a lost deployment response is reconciled by GET before another POST is allowed', async t => {
  const view = harness(t); view.requests[0].resolve(available); await view.settle(); view.selectAdministrator(); view.click('Deploy');
  view.requests[1].reject(new Error('Response lost')); await view.settle();
  assert.equal(view.requests[2].path, '/api/admin/gods-eye-view/module'); assert.equal(view.requests[2].method, undefined);
  view.requests[2].reject(new Error('Still unavailable')); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Retry settings']); assert.equal(view.changed(), 0);
  assert.match(view.text(), /Check the current status before retrying installation/);
  view.click('Retry settings'); view.requests[3].resolve(ready); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Verify']); assert.doesNotMatch(view.text(), /Response lost/);
  assert.equal(view.requests.filter(request => request.method === 'POST').length, 1);
});

test('a failed status poll invalidates an old resume action before another POST', async t => {
  const view = harness(t); view.requests[0].resolve({ ...pending, resumable: true }); await view.settle();
  view.selectAdministrator(); const action = view.find('button', props => props.children === 'Resume deployment').props.onClick;
  view.poll(); view.requests[1].reject(new Error('Status unavailable')); await view.settle();
  action(); assert.equal(view.requests.length, 2);
  assert.deepEqual(view.buttons(), ['Close', 'Retry settings']);
});

test('successful reconciliation after a lost POST reports the verified installation', async t => {
  const view = harness(t); view.requests[0].resolve(available); await view.settle(); view.selectAdministrator(); view.click('Deploy');
  view.requests[1].reject(new Error('Response lost')); await view.settle(); view.requests[2].resolve(ready); await view.settle();
  assert.equal(view.changed(), 1); assert.deepEqual(view.buttons(), ['Close', 'Verify']); assert.doesNotMatch(view.text(), /Response lost/);
});

test('missing infrastructure and failed status stay actionable without false success or an infinite loader', async t => {
  const view = harness(t); view.requests[0].reject(new Error('Unavailable')); await view.settle();
  assert.deepEqual(view.loading(), []); assert.equal(view.find('p', props => props.role === 'alert').props.children, 'Unavailable');
  view.click('Retry settings'); view.requests[1].resolve({ ...available, status: 'deployment_required', message: 'Use Deploy Studio first.' }); await view.settle();
  assert.deepEqual(view.buttons(), ['Close']); assert.match(view.text(), /Use Deploy Studio first/); assert.equal(view.changed(), 0);
});

test('failed installation retries directly and can close without cancelling the operation', async t => {
  const view = harness(t); view.requests[0].resolve({ ...pending, status: 'failed', message: 'Native task failed.' }); await view.settle();
  assert.match(view.text(), /Native task failed/); view.selectAdministrator(); view.doubleClick('Retry deployment');
  assert.equal(view.requests.length, 2);
  assert.equal(view.requests[1].method, 'POST'); view.escape(); assert.equal(view.closed(), 1);
  view.cleanup(); assert.equal(view.requests[1].signal.aborted, true); assert.equal(view.requests.length, 2);
  view.requests[1].resolve(ready); await view.settle(); assert.equal(view.changed(), 0);
});

test('a backend local fixture clearly excludes OCI provisioning and charges in the inline review', async t => {
  const view = harness(t); view.requests[0].resolve({ ...available, runtime: 'local_fixture' }); await view.settle();
  assert.match(view.text(), /Local simulation only.*does not deploy OCI or AIDP resources/);
  assert.doesNotMatch(view.text(), /incur costs/);
  view.selectAdministrator(); view.click('Deploy'); assert.equal(view.requests[1].method, 'POST');
});

test('direct deployment validates the current selected administrator rather than an earlier callback', async t => {
  const view = harness(t); view.requests[0].resolve(available); await view.settle();
  view.selectAdministrator(); const action = view.find('button', props => props.children === 'Deploy').props.onClick;
  view.selectAdministrator('other'); action();
  assert.equal(view.requests[1].path, '/api/admin/users/other/modules/gods_eye_view');
});

test('administrator load failure blocks deployment, ends loading and retries without a POST', async t => {
  const view = harness(t, () => {}); view.requests[0].resolve(available);
  view.administratorRequests[0].reject(new Error('Administrators unavailable')); await view.settle();
  assert.deepEqual(view.loading(), []); assert.match(view.text(), /Administrators unavailable/);
  assert.deepEqual(view.buttons(), ['Close', 'Retry settings']);
  view.click('Retry settings'); view.requests[1].resolve(available); await view.settle();
  assert.deepEqual(view.loading(), ['Loading module settings…']);
  view.administratorRequests[1].resolve({ users }); await view.settle();
  assert.equal(view.find('button', props => props.children === 'Deploy').props.disabled, true);
  view.selectAdministrator(); assert.equal(view.find('button', props => props.children === 'Deploy').props.disabled, false);
  assert.ok(view.requests.every(request => request.method !== 'POST'));
  view.cleanup(); assert.equal(view.administratorRequests[1].signal.aborted, true);
});

test('absence of native administrators blocks deployment and leaves Close available', async t => {
  const view = harness(t, request => request.resolve({ users: [users[0]] })); view.requests[0].resolve(available); await view.settle();
  assert.match(view.text(), /No AI Data Platform administrator is available/);
  assert.equal(view.find('button', props => props.children === 'Deploy').props.disabled, true);
  view.click('Close'); assert.equal(view.closed(), 1);
});

test('a failed administrator refresh blocks a previously captured deployment action', async t => {
  const view = harness(t, () => {}); view.requests[0].resolve(available); view.administratorRequests[0].resolve({ users }); await view.settle();
  view.selectAdministrator(); const action = view.find('button', props => props.children === 'Deploy').props.onClick;
  view.click('Deploy'); view.requests[1].reject(new Error('Response lost')); await view.settle();
  view.requests[2].reject(new Error('Status unavailable')); await view.settle();
  view.click('Retry settings'); view.requests[3].resolve(available); view.administratorRequests[1].reject(new Error('Administrators unavailable')); await view.settle();
  view.selectAdministrator(); action();
  assert.equal(view.requests.filter(request => request.method === 'POST').length, 1);
});

for (const [label, response] of [
  ['wrong module', { ...ready, module_id: 'other' }],
  ['nonboolean installed', { ...ready, installed: 'false' }],
  ['nonboolean enabled', { ...ready, enabled: 1 }],
  ['unverified ready', { ...ready, installed: false }],
  ['unknown status', { ...available, status: 'unknown' }],
  ['nonstring message', { ...available, message: {} }],
  ['unknown runtime', { ...available, runtime: 'unknown' }],
  ['empty response', null],
]) test(`malformed module state fails closed: ${label}`, async t => {
  const view = harness(t); view.requests[0].resolve(response); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Retry settings']);
  assert.match(view.text(), /Unexpected module response/);
  assert.equal(view.changed(), 0); assert.ok(view.requests.every(request => request.method !== 'POST'));
});

test('only an explicit native administrator boolean enables deployment', async t => {
  const view = harness(t, request => request.resolve({ users: [{ ...users[1], is_aidp_admin: 'false' }] }));
  view.requests[0].resolve(available); await view.settle(); view.selectAdministrator(); view.doubleClick('Deploy');
  assert.doesNotMatch(view.text(), /operator@example.test/);
  assert.equal(view.requests.length, 1);
});
