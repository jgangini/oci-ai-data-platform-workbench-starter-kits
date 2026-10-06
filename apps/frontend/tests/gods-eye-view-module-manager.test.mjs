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
  status: 'available', runtime: 'aidp', message: 'Install the private viewer and its shared AIDP resources.' };
const pending = { ...available, status: 'activating', operation_id: 'existing-operation', stage: 'infrastructure', message: 'Creating the private viewer.' };
const ready = { ...pending, status: 'ready', enabled: true, installed: true, message: 'Publication and workflows verified.' };

function harness(t) {
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
  const SettingsConfirmation = () => null;
  new Function('exports', 'require', compiled)(component, name => {
    if (name === 'react') return hooks;
    if (name === 'react-dom') return { createPortal: content => content };
    if (name === 'react/jsx-runtime') return jsxRuntime;
    if (name === './SettingsConfirmation') return { SettingsConfirmation };
    assert.equal(name, './LoadingIndicator'); return { LoadingIndicator };
  });
  const props = { api: (path, options) => new Promise((resolve, reject) => requests.push({ path, ...options, resolve, reject })),
    onClose: () => closed++, onChanged: () => changed++ };
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const text = value => typeof value === 'string' ? value : [value?.props?.children].flat(Infinity).filter(Boolean).map(text).join(' ');
  function render() {
    for (let turns = 0; dirty && turns < 20; turns++) {
      dirty = false; cursor = 0; effects = []; tree = component.GodsEyeViewModuleManager(props);
      for (const node of nodes(tree)) if (node?.props?.ref) node.props.ref.current = node.type === 'dialog'
        ? { showModal() { modal = true; }, close() { modal = false; } } : { focus() { focus = 'close'; } };
      effects.forEach(effect => effect());
    }
    assert.equal(dirty, false);
  }
  const find = (type, match = () => true) => { const node = nodes(tree).find(node => node?.type === type && match(node.props)); assert.ok(node, `Missing ${String(type)}`); return node; };
  const act = callback => { callback(); render(); };
  const cleanup = () => slots.forEach(slot => slot?.cleanup?.());
  t.after(() => { cleanup(); for (const [name, value] of Object.entries(globals)) if (value === undefined) delete globalThis[name]; else globalThis[name] = value; });
  render();
  return { requests, timers, find, cleanup, changed: () => changed, closed: () => closed, modal: () => modal, focus: () => focus, text: () => text(tree),
    confirmation: () => find(SettingsConfirmation).props,
    confirm: () => act(() => find(SettingsConfirmation).props.onConfirm()),
    cancel: () => act(() => find(SettingsConfirmation).props.onCancel()),
    loading: () => nodes(tree).filter(node => node?.type === LoadingIndicator).map(node => node.props.label),
    buttons: () => nodes(tree).filter(node => node?.type === 'button').map(node => node.props.children),
    click: label => act(() => { const button = find('button', props => props.children === label); assert.ok(!button.props.disabled); button.props.onClick(); }),
    doubleClick: label => act(() => { const click = find('button', props => props.children === label).props.onClick; click(); click(); }),
    escape: () => act(() => find('dialog').props.onCancel({ preventDefault() {} })),
    poll: () => act(() => { assert.equal(timers.size, 1); [...timers.values()][0](); }),
    settle: async () => { await new Promise(setImmediate); render(); } };
}

test('installation explains resources and costs, requires confirmation and prevents duplicate POSTs', async t => {
  const view = harness(t);
  assert.equal(view.modal(), true); assert.equal(view.focus(), 'close');
  assert.deepEqual(view.loading(), ['Loading module settings…']);
  assert.equal(view.requests[0].path, '/api/admin/gods-eye-view/module'); assert.equal(view.requests[0].method, undefined);
  assert.deepEqual(view.buttons(), ['Close']);
  view.requests[0].resolve(available); await view.settle();
  assert.doesNotMatch(view.text(), /Select an administrator|Redeploy/);
  view.doubleClick('Install module'); assert.equal(view.requests.length, 1);
  const confirmation = view.confirmation();
  assert.equal(confirmation.confirmLabel, 'Install module');
  assert.match(confirmation.changes.join(' '), /private viewer VM.*existing network and shared credentials/);
  assert.match(confirmation.changes.join(' '), /social network and sensor streams.*separate AIDP compute.*Gold query compute.*dedicated agent AI Compute/);
  assert.match(confirmation.changes.join(' '), /incur costs/);
  assert.match(confirmation.changes.join(' '), /Closing.*does not cancel/);
  view.confirm(); confirmation.onConfirm(); assert.equal(view.requests.length, 2);
  assert.equal(view.requests[1].method, 'POST'); assert.equal(view.requests[1].path, '/api/admin/gods-eye-view/module/deploy');
  assert.equal(view.requests[1].body, undefined);
  assert.equal(view.find('button', props => props.children === 'Close').props.disabled, undefined);
  view.requests[1].resolve(ready); await view.settle();
  assert.equal(view.changed(), 1); assert.deepEqual(view.buttons(), ['Close', 'Verify installation']);
  assert.match(view.text(), /Publication and workflows verified/);
  view.escape(); assert.equal(view.closed(), 1); view.cleanup(); assert.equal(view.modal(), false); assert.equal(view.focus(), 'origin');
});

test('reopening a live installation polls GET without overlap and displays the actual phase with standard progress', async t => {
  const view = harness(t); view.requests[0].resolve(pending); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Installing module…']);
  assert.equal(view.find('button', props => props.children === 'Installing module…').props.disabled, true);
  assert.match(view.text(), /Provisioning the private viewer/); assert.match(view.text(), /Creating the private viewer/);
  assert.match(view.text(), /Closing this popup does not cancel/);
  assert.equal(view.find('div', props => props.className === 'registration-result module-install-progress').props.role, 'status');
  view.find('span', props => props.className === 'progress-orbit');
  assert.doesNotMatch(view.text(), /\d+%/);
  view.poll(); view.poll(); assert.equal(view.requests.length, 2);
  assert.equal(view.requests[1].method, undefined);
  view.requests[1].resolve({ ...pending, stage: 'aidp', message: 'Preparing the shared workflows.' }); await view.settle();
  assert.match(view.text(), /Preparing AIDP resources/); assert.match(view.text(), /Preparing the shared workflows/);
  view.poll(); view.requests[2].resolve(ready); await view.settle();
  assert.equal(view.changed(), 1); assert.equal(view.timers.size, 0);
  view.click('Verify installation'); assert.equal(view.requests[3].method, 'POST');
  view.requests[3].resolve(ready); await view.settle(); assert.equal(view.changed(), 2);
});

test('cancelled or stale approval cannot install, while a stopped worker can resume the same operation', async t => {
  const view = harness(t); view.requests[0].resolve(available); await view.settle(); view.click('Install module');
  const cancelled = view.confirmation(); view.cancel(); cancelled.onConfirm(); assert.equal(view.requests.length, 1);
  view.click('Install module'); view.confirm(); view.requests[1].resolve({ ...pending, resumable: true }); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Resume installation']);
  view.click('Resume installation'); assert.match(view.confirmation().changes.join(' '), /existing installation.*without creating a duplicate viewer VM/);
  const stale = view.confirmation(); view.poll(); view.requests[2].resolve(pending); await view.settle();
  stale.onConfirm(); assert.equal(view.requests.length, 3);
  view.poll(); view.requests[3].resolve({ ...pending, resumable: true }); await view.settle();
  view.click('Resume installation'); view.confirm(); assert.equal(view.requests[4].method, 'POST');
  view.requests[4].resolve(pending); await view.settle(); assert.equal(view.requests[4].body, undefined);
});

test('a lost deployment response is reconciled by GET before another POST is allowed', async t => {
  const view = harness(t); view.requests[0].resolve(available); await view.settle(); view.click('Install module'); view.confirm();
  view.requests[1].reject(new Error('Response lost')); await view.settle();
  assert.equal(view.requests[2].path, '/api/admin/gods-eye-view/module'); assert.equal(view.requests[2].method, undefined);
  view.requests[2].reject(new Error('Still unavailable')); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Retry settings']); assert.equal(view.changed(), 0);
  assert.match(view.text(), /Check the current status before retrying installation/);
  view.click('Retry settings'); view.requests[3].resolve(ready); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Verify installation']); assert.doesNotMatch(view.text(), /Response lost/);
  assert.equal(view.requests.filter(request => request.method === 'POST').length, 1);
});

test('a failed status poll invalidates an open resume approval before another POST', async t => {
  const view = harness(t); view.requests[0].resolve({ ...pending, resumable: true }); await view.settle();
  view.click('Resume installation'); const approval = view.confirmation();
  view.poll(); view.requests[1].reject(new Error('Status unavailable')); await view.settle();
  approval.onConfirm(); assert.equal(view.requests.length, 2);
  assert.deepEqual(view.buttons(), ['Close', 'Retry settings']);
});

test('successful reconciliation after a lost POST reports the verified installation', async t => {
  const view = harness(t); view.requests[0].resolve(available); await view.settle(); view.click('Install module'); view.confirm();
  view.requests[1].reject(new Error('Response lost')); await view.settle(); view.requests[2].resolve(ready); await view.settle();
  assert.equal(view.changed(), 1); assert.deepEqual(view.buttons(), ['Close', 'Verify installation']); assert.doesNotMatch(view.text(), /Response lost/);
});

test('missing infrastructure and failed status stay actionable without false success or an infinite loader', async t => {
  const view = harness(t); view.requests[0].reject(new Error('Unavailable')); await view.settle();
  assert.deepEqual(view.loading(), []); assert.equal(view.find('p', props => props.role === 'alert').props.children, 'Unavailable');
  view.click('Retry settings'); view.requests[1].resolve({ ...available, status: 'deployment_required', message: 'Use Deploy Studio first.' }); await view.settle();
  assert.deepEqual(view.buttons(), ['Close']); assert.match(view.text(), /Use Deploy Studio first/); assert.equal(view.changed(), 0);
});

test('failed installation is dismissible and a confirmed retry can be closed without a cancellation request', async t => {
  const view = harness(t); view.requests[0].resolve({ ...pending, status: 'failed', message: 'Native task failed.' }); await view.settle();
  assert.match(view.text(), /Native task failed/); view.click('Retry installation');
  assert.equal(view.requests.length, 1); assert.match(view.confirmation().title, /Retry/); view.confirm();
  assert.equal(view.requests[1].method, 'POST'); view.escape(); assert.equal(view.closed(), 1);
  view.cleanup(); assert.equal(view.requests[1].signal.aborted, true); assert.equal(view.requests.length, 2);
  view.requests[1].resolve(ready); await view.settle(); assert.equal(view.changed(), 0);
});

test('local simulation confirmation never claims to provision or charge for cloud resources', async t => {
  const view = harness(t); view.requests[0].resolve({ ...available, runtime: 'local_fixture' }); await view.settle();
  view.click('Install module'); const confirmation = view.confirmation();
  assert.match(confirmation.description, /local simulation only.*No OCI or AIDP resources/);
  assert.doesNotMatch(confirmation.changes.join(' '), /Provision|incur costs/);
});
