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
  status: 'available', runtime: 'aidp', message: 'Enable the existing module.' };
const pending = { ...available, status: 'activating', operation_id: 'existing-operation', message: 'Waiting for the native run.' };
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
  new Function('exports', 'require', compiled)(component, name => {
    if (name === 'react') return hooks;
    if (name === 'react-dom') return { createPortal: content => content };
    if (name === 'react/jsx-runtime') return jsxRuntime;
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
    loading: () => nodes(tree).filter(node => node?.type === LoadingIndicator).map(node => node.props.label),
    buttons: () => nodes(tree).filter(node => node?.type === 'button').map(node => node.props.children),
    click: label => act(() => { const button = find('button', props => props.children === label); assert.ok(!button.props.disabled); button.props.onClick(); }),
    doubleClick: label => act(() => { const click = find('button', props => props.children === label).props.onClick; click(); click(); }),
    escape: () => act(() => find('dialog').props.onCancel({ preventDefault() {} })),
    poll: () => act(() => { assert.equal(timers.size, 1); [...timers.values()][0](); }),
    settle: async () => { await new Promise(setImmediate); render(); } };
}

test('global module uses one native modal, loads its own status and deploys only on explicit action', async t => {
  const view = harness(t);
  assert.equal(view.modal(), true); assert.equal(view.focus(), 'close');
  assert.deepEqual(view.loading(), ['Loading module settings…']);
  assert.equal(view.requests[0].path, '/api/admin/gods-eye-view/module'); assert.equal(view.requests[0].method, undefined);
  assert.deepEqual(view.buttons(), ['Close']);
  view.requests[0].resolve(available); await view.settle();
  assert.doesNotMatch(view.text(), /Select an administrator|Redeploy/);
  view.doubleClick('Deploy'); assert.equal(view.requests.length, 2);
  assert.equal(view.requests[1].method, 'POST'); assert.equal(view.requests[1].path, '/api/admin/gods-eye-view/module/deploy');
  assert.equal(view.requests[1].body, undefined); view.escape(); assert.equal(view.closed(), 0);
  view.requests[1].resolve(ready); await view.settle();
  assert.equal(view.changed(), 1); assert.deepEqual(view.buttons(), ['Close', 'Verify installation']);
  assert.match(view.text(), /Publication and workflows verified/);
  view.escape(); assert.equal(view.closed(), 1); view.cleanup(); assert.equal(view.modal(), false); assert.equal(view.focus(), 'origin');
});

test('reopening an activation polls GET only and explicit Resume reuses the server operation', async t => {
  const view = harness(t); view.requests[0].resolve(pending); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Resume activation']); view.poll(); view.poll(); assert.equal(view.requests.length, 2);
  assert.equal(view.requests[1].method, undefined); view.requests[1].resolve(pending); await view.settle();
  view.click('Resume activation'); assert.equal(view.requests[2].method, 'POST'); assert.equal(view.requests[2].body, undefined);
  view.requests[2].resolve(pending); await view.settle(); view.poll(); view.requests[3].resolve(ready); await view.settle();
  assert.equal(view.changed(), 1); assert.equal(view.timers.size, 0);
});

test('a lost deployment response is reconciled by GET before another POST is allowed', async t => {
  const view = harness(t); view.requests[0].resolve(available); await view.settle(); view.click('Deploy');
  view.requests[1].reject(new Error('Response lost')); await view.settle();
  assert.equal(view.requests[2].path, '/api/admin/gods-eye-view/module'); assert.equal(view.requests[2].method, undefined);
  view.requests[2].reject(new Error('Still unavailable')); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Retry settings']); assert.equal(view.changed(), 0);
  assert.match(view.text(), /Check the current status before retrying deployment/);
  view.click('Retry settings'); view.requests[3].resolve(ready); await view.settle();
  assert.deepEqual(view.buttons(), ['Close', 'Verify installation']); assert.doesNotMatch(view.text(), /Response lost/);
  assert.equal(view.requests.filter(request => request.method === 'POST').length, 1);
});

test('successful reconciliation after a lost POST reports the verified installation', async t => {
  const view = harness(t); view.requests[0].resolve(available); await view.settle(); view.click('Deploy');
  view.requests[1].reject(new Error('Response lost')); await view.settle(); view.requests[2].resolve(ready); await view.settle();
  assert.equal(view.changed(), 1); assert.deepEqual(view.buttons(), ['Close', 'Verify installation']); assert.doesNotMatch(view.text(), /Response lost/);
});

test('missing infrastructure and failed status stay actionable without false success or an infinite loader', async t => {
  const view = harness(t); view.requests[0].reject(new Error('Unavailable')); await view.settle();
  assert.deepEqual(view.loading(), []); assert.equal(view.find('p', props => props.role === 'alert').props.children, 'Unavailable');
  view.click('Retry settings'); view.requests[1].resolve({ ...available, status: 'deployment_required', message: 'Use Deploy Studio first.' }); await view.settle();
  assert.deepEqual(view.buttons(), ['Close']); assert.match(view.text(), /Use Deploy Studio first/); assert.equal(view.changed(), 0);
});

test('failed activation can retry and unmount aborts requests without late callbacks', async t => {
  const view = harness(t); view.requests[0].resolve({ ...pending, status: 'failed', message: 'Native task failed.' }); await view.settle();
  assert.match(view.text(), /Native task failed/); view.click('Retry deployment');
  assert.equal(view.requests[1].method, 'POST'); view.cleanup(); assert.equal(view.requests[1].signal.aborted, true);
  view.requests[1].resolve(ready); await view.settle(); assert.equal(view.changed(), 0);
});
