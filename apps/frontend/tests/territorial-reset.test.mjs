import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';
import { renderToStaticMarkup } from 'react-dom/server';

const source = readFileSync(new URL('../src/SyntheticDataReset.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;

function harness(t, status, runtime = 'local_fixture', sensor) {
  const slots = [], requests = [], changes = [], timers = new Map(), portals = [];
  let cursor = 0, dirty = true, effects = [], tree, timerId = 0, completed = 0, focused = '', modal = false;
  const globals = { window: globalThis.window, document: globalThis.document, HTMLElement: globalThis.HTMLElement };
  globalThis.window = { setInterval(callback) { timers.set(++timerId, callback); return timerId; }, clearInterval(id) { timers.delete(id); } };
  globalThis.HTMLElement = class { isConnected = true; focus() { focused = 'origin'; } };
  globalThis.document = { activeElement: new globalThis.HTMLElement(), body: {} };
  const hooks = {
    useState(initial) {
      const id = cursor++; if (!(id in slots)) slots[id] = initial;
      return [slots[id], value => { const next = typeof value === 'function' ? value(slots[id]) : value; dirty ||= !Object.is(next, slots[id]); slots[id] = next; }];
    },
    useRef(initial) { const id = cursor++; return slots[id] ||= { current: initial }; },
    useEffect(callback, deps) {
      const id = cursor++, old = slots[id];
      if (!old || deps.some((value, index) => !Object.is(value, old.deps[index]))) effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; });
    },
  };
  const component = {};
  new Function('exports', 'require', compiled)(component, name => {
    if (name === 'react') return hooks;
    if (name === 'react-dom') return { createPortal: (content, target) => { portals.push(target); return content; } };
    if (name === 'react/jsx-runtime') return jsxRuntime;
    assert.equal(name, './territorialAdminState');
    return { territorialEndpoint: '/api/admin/territorial', territorialError: reason => reason.message };
  });
  const props = { status, runtime, sensor, api: (url, options) => new Promise((resolve, reject) => requests.push({ url, ...options, resolve, reject })),
    onChange: (state, error) => changes.push({ state, error }), onComplete: () => completed++ };
  function nodes(value) { return [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])]; }
  function render() {
    for (let turns = 0; dirty && turns < 20; turns++) {
      dirty = false; cursor = 0; effects = []; tree = component.SyntheticDataReset(props);
      for (const node of nodes(tree)) if (node?.props?.ref) node.props.ref.current = node.type === 'dialog'
        ? { showModal() { modal = true; }, close() { modal = false; }, focus() { focused = 'dialog'; } }
        : { focus() { focused = 'Cancel'; } };
      for (const effect of effects) effect();
    }
    assert.equal(dirty, false, 'Component state did not settle');
  }
  const find = (type, match = () => true) => { const node = nodes(tree).find(node => node?.type === type && match(node.props)); assert.ok(node, `Missing ${type}`); return node; };
  const act = callback => { callback(); render(); };
  t.after(() => {
    for (const slot of slots) slot?.cleanup?.();
    for (const [name, value] of Object.entries(globals)) if (value === undefined) delete globalThis[name]; else globalThis[name] = value;
  });
  render();
  return { requests, changes, portals, find, timers, state: () => changes.at(-1)?.state, completed: () => completed, focused: () => focused, modal: () => modal,
    markup: () => renderToStaticMarkup(tree),
    progress: () => renderToStaticMarkup(component.SyntheticDataResetStatus({ runtime, sensorLabel: sensor?.label, ...(changes.at(-1) || { state: {}, error: '' }) })),
    click: label => act(() => { const button = find('button', props => (props['aria-label'] || props.children) === label); assert.ok(!button.props.disabled); button.props.onClick(); }),
    confirm: () => act(() => find('button', props => props.className === 'confirm-primary').props.onClick()),
    escape: () => act(() => find('dialog').props.onCancel({ preventDefault() {} })),
    status: next => act(() => { props.status = next; dirty = true; }),
    disabled: value => act(() => { props.disabled = value; dirty = true; }),
    poll: () => act(() => { assert.equal(timers.size, 1); [...timers.values()][0](); }),
    settle: async () => { await new Promise(setImmediate); render(); },
  };
}

test('sensor deletion confirms its selected scope, preserves locations and retries only its sensor endpoint', async t => {
  const sensor = { type: 'river_level', label: 'River Level' }, view = harness(t, undefined, 'aidp', sensor);
  view.click('Delete River Level data');
  assert.match(view.markup(), /Delete River Level data\?/);
  assert.match(view.markup(), /Other sensor types, real readings, social network data, saved sensor locations and sensor configuration are kept/);
  assert.doesNotMatch(view.markup(), /across all networks|Delete all Synthetic/);
  view.click('Cancel'); assert.equal(view.requests.length, 0);
  view.click('Delete River Level data'); view.confirm();
  const body = JSON.parse(view.requests[0].body);
  assert.equal(view.requests[0].url, '/api/admin/territorial/sensors/river_level/reset'); assert.equal(body.confirm, true);
  assert.equal(view.state().sensor_type, 'river_level'); assert.equal(view.completed(), 0);
  view.requests[0].reject(new Error('Response lost')); await view.settle();
  view.requests[1].resolve({ ...body, sensor_type: 'river_level', status: 'pending', stage: 'delta' }); await view.settle();
  assert.match(view.progress(), /Deleting River Level readings from Delta tables/);
  assert.match(view.progress(), /River Level reset progress/);
  view.poll(); view.requests[2].resolve({ ...body, sensor_type: 'river_level', status: 'error', error: 'Cleanup interrupted' }); await view.settle();
  assert.match(view.progress(), /Cleanup interrupted/); assert.equal(view.completed(), 0);
  view.click('Retry River Level reset'); view.confirm();
  assert.deepEqual(JSON.parse(view.requests[3].body), body);
  view.requests[3].resolve({ ...body, sensor_type: 'river_level', status: 'completed' }); await view.settle();
  assert.equal(view.completed(), 1); assert.equal(view.progress(), '');
  assert.ok(view.requests.every(request => request.url === '/api/admin/territorial/sensors/river_level/reset'));
});

test('a sensor reset never adopts another sensor type or social operation after a conflict', async t => {
  const view = harness(t, undefined, 'local_fixture', { type: 'rainfall', label: 'Rainfall' });
  view.click('Delete Rainfall data'); view.confirm(); const body = JSON.parse(view.requests[0].body);
  view.requests[0].reject(Object.assign(new Error('Another reset is active'), { status: 409 })); await view.settle();
  view.requests[1].resolve({ operation_id: 'other-type', sensor_type: 'temperature', status: 'pending' }); await view.settle();
  assert.equal(view.state().operation_id, body.operation_id); assert.equal(view.state().sensor_type, 'rainfall');
  assert.match(view.progress(), /Another data cleanup is active/);
  view.poll(); view.requests[2].resolve({ operation_id: body.operation_id, status: 'completed' }); await view.settle();
  assert.equal(view.completed(), 0, 'Even a matching ID cannot confirm a response for the wrong scope');
  view.click('Retry Rainfall reset'); view.confirm();
  assert.deepEqual(JSON.parse(view.requests[3].body), body);
  assert.ok(view.requests.every(request => request.url === '/api/admin/territorial/sensors/rainfall/reset'));
});

test('social reset ignores sensor checkpoints and a disabled control cannot confirm a reset', t => {
  const view = harness(t, { operation_id: 'sensor-operation', sensor_type: 'rainfall', status: 'pending' });
  assert.deepEqual(view.state(), {}); assert.match(view.progress(), /Another data cleanup is active/);
  view.click('Delete Synthetic data'); view.disabled(true); view.confirm();
  assert.equal(view.requests.length, 0); assert.equal(view.find('button').props.disabled, true);
});

test('the destructive dialog focuses Cancel and neither Cancel nor Escape sends a reset', t => {
  const view = harness(t);
  view.click('Delete Synthetic data');
  assert.equal(view.modal(), true); assert.equal(view.focused(), 'Cancel');
  assert.equal(view.find('dialog').props['aria-describedby'], 'territorial-reset-description');
  view.click('Cancel'); assert.equal(view.modal(), false); assert.equal(view.focused(), 'origin');
  view.click('Delete Synthetic data'); view.escape();
  assert.equal(view.modal(), false); assert.equal(view.requests.length, 0);
});

test('confirmed resets block repeated submission and refresh once only after completion', async t => {
  const view = harness(t);
  view.click('Delete Synthetic data'); view.confirm();
  assert.equal(view.modal(), true); assert.equal(view.focused(), 'dialog'); assert.equal(view.completed(), 0);
  assert.equal(view.find('button').props.disabled, true);
  const body = JSON.parse(view.requests[0].body);
  assert.equal(body.confirm, true); assert.match(body.operation_id, /^[\da-f-]{36}$/);
  assert.equal(view.requests[0].method, 'POST');
  view.poll(); assert.equal(view.requests.length, 1, 'No overlapping status request while POST is in flight');
  view.requests[0].resolve({ operation_id: body.operation_id, status: 'pending' }); await view.settle();
  view.poll(); assert.equal(view.requests[1].method, undefined);
  view.requests[1].resolve({ operation_id: body.operation_id, status: 'completed' }); await view.settle();
  assert.equal(view.completed(), 1); assert.equal(view.find('button').props.disabled, false);
  view.status({ operation_id: body.operation_id, status: 'pending' });
  assert.equal(view.state().status, 'completed'); assert.equal(view.completed(), 1);
});

test('a lost POST response recovers its persisted status, and failure retries the same operation', async t => {
  const view = harness(t);
  view.click('Delete Synthetic data'); view.confirm();
  const body = JSON.parse(view.requests[0].body);
  view.requests[0].reject(new Error('Network disconnected')); await view.settle();
  assert.equal(view.requests[1].method, undefined);
  view.requests[1].resolve({ operation_id: body.operation_id, status: 'pending' }); await view.settle();
  assert.equal(view.state().status, 'pending'); assert.equal(view.completed(), 0);
  view.poll(); view.requests[2].resolve({ operation_id: body.operation_id, status: 'error', error: 'Cleanup interrupted' }); await view.settle();
  view.click('Retry Synthetic reset'); view.confirm();
  assert.deepEqual(JSON.parse(view.requests[3].body), body);
  view.requests[3].resolve({ operation_id: body.operation_id, status: 'completed' }); await view.settle();
  assert.equal(view.completed(), 1);
});

test('empty or older status after a lost response cannot discard the in-flight operation ID', async t => {
  const view = harness(t);
  view.click('Delete Synthetic data'); view.confirm();
  const body = JSON.parse(view.requests[0].body);
  view.status({ operation_id: 'older', status: 'error' });
  view.requests[0].reject(new Error('Lost response')); await view.settle();
  view.requests[1].resolve({}); await view.settle();
  assert.equal(view.state().operation_id, body.operation_id); assert.equal(view.state().status, 'error');
  view.poll(); view.requests[2].resolve({ operation_id: 'older', status: 'completed' }); await view.settle();
  assert.equal(view.state().operation_id, body.operation_id); assert.equal(view.completed(), 0);
  view.click('Retry Synthetic reset'); view.confirm();
  assert.deepEqual(JSON.parse(view.requests[3].body), body);
  view.requests[3].resolve({ operation_id: body.operation_id, status: 'completed' }); await view.settle();
  view.status({ operation_id: 'older', status: 'error' });
  view.requests[4].resolve({ operation_id: body.operation_id, status: 'completed' }); await view.settle();
  assert.equal(view.state().status, 'completed'); assert.equal(view.completed(), 1);
});

test('a new remote reset seen during a local reset can be verified and followed after local completion', async t => {
  const view = harness(t, { operation_id: 'local-reset', status: 'pending' });
  view.status({ operation_id: 'other-reset', status: 'pending' });
  assert.equal(view.requests.length, 0);
  view.poll(); view.requests[0].resolve({ operation_id: 'local-reset', status: 'completed' }); await view.settle();
  assert.equal(view.progress(), '');
  view.status({ operation_id: 'other-reset', status: 'pending' });
  view.requests[1].reject(new Error('Network unavailable')); await view.settle();
  assert.match(view.progress(), /role="alert"/);
  assert.match(view.progress(), /Network unavailable/);
  assert.doesNotMatch(view.progress(), /data deleted|cleanup completed|Choose Run now|<progress/);
  view.status({ operation_id: 'other-reset', status: 'pending' });
  view.requests[2].resolve({ operation_id: 'other-reset', status: 'pending' }); await view.settle();
  assert.equal(view.state().operation_id, 'other-reset'); assert.equal(view.state().status, 'pending');
  view.poll(); view.requests[3].resolve({ operation_id: 'other-reset', status: 'completed' }); await view.settle();
  assert.equal(view.progress(), '');
  assert.equal(view.completed(), 2);
});

test('double network failure retains the operation and checks status without submitting another reset automatically', async t => {
  const view = harness(t);
  view.click('Delete Synthetic data'); view.confirm();
  const body = JSON.parse(view.requests[0].body);
  view.requests[0].reject(new Error('Disconnected')); await view.settle();
  view.requests[1].reject(new Error('Still disconnected')); await view.settle();
  assert.equal(view.state().status, 'pending'); assert.equal(view.find('button').props.disabled, false);
  view.poll(); view.requests[2].resolve({ operation_id: body.operation_id, status: 'completed' }); await view.settle();
  assert.equal(view.requests.filter(request => request.method === 'POST').length, 1); assert.equal(view.completed(), 1);
});

test('healthy pending resets only poll and failures require a fresh same-ID confirmation', async t => {
  const view = harness(t, { operation_id: 'interrupted-native-job', status: 'pending' });
  assert.doesNotMatch(view.markup(), /Retry/);
  assert.equal(view.find('button').props.disabled, true);
  view.poll(); view.requests[0].resolve({ operation_id: 'interrupted-native-job', status: 'error', error: 'Job interrupted' }); await view.settle();
  view.click('Retry Synthetic reset');
  assert.equal(view.requests.length, 1, 'Reviewing a retry never posts');
  view.click('Cancel'); assert.equal(view.requests.length, 1);
  view.click('Retry Synthetic reset'); view.escape(); assert.equal(view.requests.length, 1);
  view.click('Retry Synthetic reset');
  const confirm = view.find('button', props => props.className === 'confirm-primary').props.onClick;
  confirm(); confirm(); await view.settle();
  assert.equal(view.requests.length, 2); assert.equal(view.find('button').props.disabled, true);
  assert.deepEqual(JSON.parse(view.requests[1].body), { operation_id: 'interrupted-native-job', confirm: true });
  view.requests[1].resolve({ operation_id: 'interrupted-native-job', status: 'pending' }); await view.settle();
  assert.doesNotMatch(view.markup(), /Retry/); assert.equal(view.completed(), 0);
  view.poll(); view.requests[2].resolve({ operation_id: 'interrupted-native-job', status: 'completed' }); await view.settle();
  assert.equal(view.completed(), 1);
});

test('a recovered healthy poll cancels an unconfirmed retry without posting again', async t => {
  const view = harness(t, { operation_id: 'recovering', status: 'error', revision: 2 });
  view.click('Retry Synthetic reset');
  const confirm = view.find('button', props => props.className === 'confirm-primary').props.onClick;
  view.poll(); view.requests[0].resolve({ operation_id: 'recovering', status: 'pending', revision: 3 }); await view.settle();
  assert.doesNotMatch(view.markup(), /Retry|confirm-primary/);
  assert.equal(view.modal(), true); assert.equal(view.focused(), 'dialog');
  confirm(); await view.settle();
  assert.equal(view.requests.length, 1, 'The stale confirmation has no authority after recovery');
  view.poll(); view.requests[1].resolve({ operation_id: 'recovering', status: 'completed', revision: 4 }); await view.settle();
  assert.equal(view.completed(), 1); assert.equal(view.focused(), 'origin');
});

test('definite pre-operation rejection releases controls, while generic 503 retains the operation', async t => {
  const view = harness(t);
  for (const status of [501, 422]) {
    view.click('Delete Synthetic data'); view.confirm();
    const count = view.requests.length;
    view.requests.at(-1).reject(Object.assign(new Error('Update the AIDP workflow before resetting Synthetic data'), { status })); await view.settle();
    assert.deepEqual(view.state(), {}); assert.equal(view.find('button').props.disabled, false);
    assert.equal(view.requests.length, count, 'Definite rejection needs no recovery read');
    assert.match(view.changes.at(-1).error, /Update the AIDP workflow/);
    assert.match(view.progress(), /Synthetic reset could not be started/);
    assert.doesNotMatch(view.progress(), /Resetting Synthetic data|<progress/);
  }
  view.click('Delete Synthetic data'); view.confirm();
  const body = JSON.parse(view.requests.at(-1).body);
  view.requests.at(-1).reject(Object.assign(new Error('Database unavailable'), { status: 503 })); await view.settle();
  view.requests.at(-1).resolve({}); await view.settle();
  assert.equal(view.state().operation_id, body.operation_id); assert.equal(view.state().status, 'error');
});

test('a conflict follows the active server operation instead of starting another cleanup', async t => {
  const view = harness(t);
  view.click('Delete Synthetic data'); view.confirm();
  view.requests[0].reject(Object.assign(new Error('Another reset is active'), { status: 409 })); await view.settle();
  view.requests[1].resolve({ operation_id: 'other-admin-reset', status: 'pending' }); await view.settle();
  assert.equal(view.state().operation_id, 'other-admin-reset'); assert.equal(view.state().status, 'pending');
  view.poll(); view.requests[2].resolve({ operation_id: 'other-admin-reset', status: 'completed' }); await view.settle();
  assert.equal(view.completed(), 1); assert.equal(view.requests.filter(request => request.method === 'POST').length, 1);
});

test('a newly observed remote reset closes an unconfirmed dialog without sending a second request', t => {
  const view = harness(t);
  view.click('Delete Synthetic data'); assert.equal(view.modal(), true);
  view.status({ operation_id: 'another-admin', status: 'pending' });
  assert.equal(view.modal(), true); assert.equal(view.requests.length, 0);
  assert.equal(view.find('button').props['aria-label'], 'Deleting Synthetic data');
});

test('persisted failures support same-ID retry and an aborted status read cannot undo completion', async t => {
  const view = harness(t, { operation_id: 'persisted-reset', status: 'error' });
  view.poll(); const old = view.requests[0];
  view.click('Retry Synthetic reset'); view.confirm();
  assert.equal(old.signal.aborted, true);
  assert.equal(JSON.parse(view.requests[1].body).operation_id, 'persisted-reset');
  view.requests[1].resolve({ operation_id: 'persisted-reset', status: 'completed' }); await view.settle();
  old.resolve({ operation_id: 'persisted-reset', status: 'pending' }); await view.settle();
  assert.equal(view.state().status, 'completed'); assert.equal(view.completed(), 1);
});

test('reset integration disables source actions and replaces stale publication pages without remounting source drafts', () => {
  const admin = readFileSync(new URL('../src/TerritorialAdmin.tsx', import.meta.url), 'utf8');
  assert.match(admin, /if \([^\n]+disabled \|\| controller\.current\) return/);
  assert.match(admin, /<fieldset disabled=\{[^\n]+disabled \|\| !!busy\}/);
  assert.match(admin, /<SourceCard[^>]+disabled=\{resetBlocked\}/);
  assert.doesNotMatch(admin, /<SourceCard[^>]+key=\{resetRevision\}/);
  assert.match(admin, /<TerritorialPosts key=\{resetRevision\}/);
  assert.match(admin, /<SyntheticDataReset[^>]+runtime=\{config\?\.runtime \|\| ''\}/);
  assert.doesNotMatch(admin, /SyntheticDataResetStatus/);
});

test('a fast local reset hides its progress after completion and explicitly excludes AIDP and Delta while pending', async t => {
  const view = harness(t);
  view.click('Delete Synthetic data');
  assert.match(view.markup(), /local publications, events and generated files/);
  assert.match(view.markup(), /No AIDP job or Delta tables are involved/);
  view.confirm();
  assert.match(view.progress(), /Preparing the local Synthetic cleanup/);
  assert.match(view.progress(), /class="progress-orbit" aria-hidden="true"/);
  assert.match(view.progress(), /class="sr-only">Synthetic reset progress/);
  assert.doesNotMatch(view.progress(), /<progress[^>]*value=/);
  const { operation_id } = JSON.parse(view.requests[0].body);
  view.requests[0].resolve({ operation_id, status: 'completed', stage: 'completed' }); await view.settle();
  assert.equal(view.progress(), '');
  assert.equal(view.completed(), 1);
});

test('AIDP progress follows reported stages and history remains pending until confirmed completion', async t => {
  const view = harness(t, undefined, 'aidp');
  view.click('Delete Synthetic data');
  assert.match(view.markup(), /including Delta tables, Autonomous Database and Object Storage/);
  view.confirm();
  const { operation_id } = JSON.parse(view.requests[0].body);
  view.requests[0].resolve({ operation_id, status: 'pending', stage: 'waiting_for_aidp' }); await view.settle();
  assert.match(view.progress(), /Waiting for the AIDP cleanup job/);
  for (const [stage, description] of [
    ['draining', 'Waiting for active captures'], ['landing', 'Deleting Synthetic Landing files'],
    ['delta', 'Deleting Synthetic rows from Delta tables'], ['database', 'Deleting Synthetic records from Autonomous Database'],
    ['publishing', 'Publishing the cleaned events'], ['history', 'Cleaning Synthetic publication history'],
  ]) {
    view.poll(); view.requests.at(-1).resolve({ operation_id, status: 'pending', stage }); await view.settle();
    assert.ok(view.progress().includes(description));
    assert.doesNotMatch(view.progress(), /<progress[^>]*value=/);
    assert.equal(view.completed(), 0);
  }
  for (const count of [159, 166]) {
    view.poll(); view.requests.at(-1).resolve({ operation_id, status: 'pending', stage: 'history', replacements: Object.fromEntries(Array.from({ length: count }, (_, index) => [`old-${index}`, `new-${index}`])) }); await view.settle();
    assert.ok(view.markup().includes(`Historical publications rebuilt: ${count}.`));
    assert.equal(view.modal(), true); assert.equal(view.completed(), 0);
    assert.doesNotMatch(view.markup(), /old-\d|new-\d|\d+%/);
  }
  view.poll(); view.requests.at(-1).resolve({ operation_id, status: 'completed', stage: 'completed' }); await view.settle();
  assert.equal(view.progress(), '');
  assert.equal(view.completed(), 1);
});

test('failed cleanup never displays a completed bar and resumes the same operation', async t => {
  const view = harness(t, { operation_id: 'failed-delta', status: 'pending', stage: 'delta' }, 'aidp');
  view.poll(); view.requests[0].resolve({ operation_id: 'failed-delta', status: 'error', stage: 'delta', error: 'Delta cleanup interrupted' }); await view.settle();
  assert.match(view.progress(), /role="alert"/);
  assert.match(view.progress(), /Synthetic reset is incomplete/);
  assert.match(view.progress(), /Delta cleanup interrupted/);
  assert.doesNotMatch(view.progress(), /<progress/);
  assert.equal(view.completed(), 0);
  view.click('Retry Synthetic reset'); view.confirm();
  assert.equal(JSON.parse(view.requests.at(-1).body).operation_id, 'failed-delta');
  assert.doesNotMatch(view.progress(), /<progress[^>]*value=/);
});

test('unknown runtime and stages use neutral wording and old completed operations stay hidden', async t => {
  const view = harness(t, { operation_id: 'old', status: 'completed', stage: 'completed' }, 'future-runtime');
  assert.equal(view.progress(), ''); assert.equal(view.completed(), 0);
  view.click('Delete Synthetic data');
  assert.doesNotMatch(view.markup(), /AIDP|Delta|Autonomous|Object Storage|local publications/);
  view.confirm();
  const { operation_id } = JSON.parse(view.requests[0].body);
  view.requests[0].resolve({ operation_id, status: 'pending', stage: 'unknown' }); await view.settle();
  assert.match(view.progress(), /Waiting for cleanup to finish/);
  assert.doesNotMatch(view.progress(), /AIDP|Delta|Autonomous|Object Storage/);
});

test('reset progress is a focused modal outside hidden tabs, keeps pending Escape blocked and retries its original request', async t => {
  const view = harness(t, { operation_id: 'current', status: 'pending', stage: 'history' }, 'aidp');
  assert.equal(view.modal(), true); assert.equal(view.focused(), 'dialog');
  assert.ok(view.portals.every(target => target === document.body));
  assert.equal(view.find('dialog').props['aria-label'], 'Synthetic reset');
  assert.match(view.markup(), /registration-result territorial-reset-progress/);
  assert.match(view.markup(), /Cleaning Synthetic publication history/);
  view.escape(); assert.equal(view.modal(), true); assert.equal(view.requests.length, 0);
  view.poll(); view.requests[0].reject(new Error('Temporary status failure')); await view.settle();
  assert.match(view.markup(), /role="alert"/); assert.match(view.markup(), /Temporary status failure/);
  const retry = view.find('button', props => !props['aria-label'] && props.children?.join?.('') === 'Retry Synthetic reset').props.onClick;
  retry(); retry(); await view.settle();
  assert.equal(view.requests.length, 1, 'Retry opens the destructive confirmation first');
  assert.equal(view.focused(), 'Cancel'); view.click('Cancel');
  assert.equal(view.requests.length, 1);
  retry(); await view.settle(); view.confirm();
  assert.equal(view.requests.length, 2);
  assert.deepEqual(JSON.parse(view.requests[1].body), { operation_id: 'current', confirm: true });
  view.requests[1].resolve({ operation_id: 'current', status: 'completed' }); await view.settle();
  assert.equal(view.modal(), false); assert.equal(view.focused(), 'origin'); assert.equal(view.timers.size, 0);
});

for (const sensor of [undefined, { type: 'river_level', label: 'River Level' }]) {
  test(`completed configuration resolves a stalled ${sensor?.type || 'social'} poll without accepting foreign completion`, async t => {
    const pending = { operation_id: 'own-reset', ...(sensor ? { sensor_type: sensor.type } : {}), status: 'pending', stage: 'history' };
    const view = harness(t, pending, 'aidp', sensor);
    view.poll(); const stale = view.requests[0];
    view.status({ ...pending, operation_id: 'older-reset', status: 'completed' });
    assert.equal(view.completed(), 0); assert.equal(stale.signal.aborted, false);
    view.status({ ...pending, sensor_type: sensor ? 'rainfall' : 'river_level', status: 'completed' });
    assert.equal(view.completed(), 0); assert.equal(stale.signal.aborted, false);
    view.status({ ...pending, status: 'completed' });
    assert.equal(view.completed(), 1); assert.equal(view.modal(), false); assert.equal(stale.signal.aborted, true);
    stale.resolve(pending); await view.settle();
    view.status(pending); view.status({ ...pending, status: 'completed' });
    assert.equal(view.completed(), 1); assert.equal(view.state().status, 'completed'); assert.equal(view.timers.size, 0);
    assert.equal(view.requests.length, 1, 'Configuration completion does not trigger another mutation');
  });
}

test('verified configuration can finish a stalled POST without letting its late response unlock a new reset', async t => {
  const view = harness(t);
  view.click('Delete Synthetic data'); view.confirm(); const old = view.requests[0], { operation_id } = JSON.parse(old.body);
  view.status({ operation_id, status: 'completed' });
  assert.equal(view.modal(), false); assert.equal(view.completed(), 1); assert.equal(old.signal.aborted, true);
  view.click('Delete Synthetic data'); view.confirm();
  old.resolve({ operation_id, status: 'pending' }); await view.settle();
  assert.equal(view.find('button').props.disabled, true);
  assert.notEqual(view.state().operation_id, operation_id);
  view.requests[1].resolve({ operation_id: JSON.parse(view.requests[1].body).operation_id, status: 'completed' }); await view.settle();
  assert.equal(view.completed(), 2); assert.equal(view.modal(), false);
});

const allSensors = { type: 'all', label: 'Synthetic sensor', labels: { river_level: 'River Level', rainfall: 'Rainfall', temperature: 'Temperature', soil_moisture: 'Soil Moisture', wind_speed: 'Wind Speed' } };
test('global sensor deletion confirms all five types, preserves other data and polls real progress to completion', async t => {
  const view = harness(t, undefined, 'aidp', allSensors);
  view.click('Delete Synthetic sensor data');
  assert.match(view.markup(), /all five sensor types/);
  assert.match(view.markup(), /Real readings, social network data, saved sensor locations and sensor configuration are kept/);
  view.escape(); assert.equal(view.requests.length, 0);
  view.click('Delete Synthetic sensor data');
  const confirm = view.find('button', p => p.className === 'confirm-primary').props.onClick;
  confirm(); confirm(); await view.settle();
  assert.equal(view.requests.length, 1);
  assert.equal(view.requests[0].url, '/api/admin/territorial/sensors/reset');
  const { operation_id } = JSON.parse(view.requests[0].body);
  const pending = { operation_id, sensor_type: 'all', status: 'pending', stage: 'history', revision: 2, replacements: { old: 'new' } };
  view.requests[0].resolve(pending); await view.settle();
  assert.match(view.markup(), /Historical publications rebuilt: 1/);
  assert.match(view.markup(), /registration-progress-track territorial-reset-track/);
  assert.doesNotMatch(view.markup(), /aria-valuenow|\d+%|Retry/);
  view.poll(); view.requests[1].resolve({ ...pending, revision: 1, replacements: {} }); await view.settle();
  assert.equal(view.state().revision, 2); assert.match(view.markup(), /Historical publications rebuilt: 1/);
  view.poll(); view.requests[2].resolve({ ...pending, revision: 3, replacements: {}, counts: { history_rewritten: 2 } }); await view.settle();
  assert.match(view.markup(), /Historical publications rebuilt: 2/);
  view.poll(); view.requests[3].resolve({ ...pending, revision: 4, stage: 'waiting_for_sensor_stream' }); await view.settle();
  assert.match(view.markup(), /Waiting for active sensor captures to finish/);
  view.poll(); view.requests[4].resolve({ ...pending, status: 'completed', revision: 5, completed_at: '2026-10-06T10:00:00Z' }); await view.settle();
  assert.equal(view.completed(), 1); assert.equal(view.state().completed_at, '2026-10-06T10:00:00Z');
  assert.equal(view.modal(), false);
});

test('a global control resumes a legacy family only on its original endpoint after explicit retry confirmation', async t => {
  const pending = { operation_id: 'legacy-family-reset', sensor_type: 'river_level', status: 'pending', stage: 'delta' };
  const view = harness(t, pending, 'aidp', allSensors);
  assert.equal(view.find('dialog').props['aria-label'], 'River Level reset');
  assert.doesNotMatch(view.markup(), /all five|Retry/);
  view.poll(); assert.equal(view.requests[0].url, '/api/admin/territorial/sensors/river_level/reset');
  view.requests[0].resolve({ ...pending, status: 'error', error: 'Interrupted' }); await view.settle();
  view.click('Retry River Level reset');
  assert.match(view.markup(), /Other sensor types, real readings, social network data/);
  assert.doesNotMatch(view.markup(), /all five/); view.click('Cancel'); assert.equal(view.requests.length, 1);
  view.click('Retry River Level reset'); view.confirm();
  assert.equal(view.requests[1].url, '/api/admin/territorial/sensors/river_level/reset');
  assert.deepEqual(JSON.parse(view.requests[1].body), { operation_id: pending.operation_id, confirm: true });
  view.requests[1].resolve({ ...pending, status: 'completed' }); await view.settle();
  view.click('Delete Synthetic sensor data'); assert.match(view.markup(), /all five sensor types/); view.confirm();
  assert.equal(view.requests[2].url, '/api/admin/territorial/sensors/reset');
  assert.notEqual(JSON.parse(view.requests[2].body).operation_id, pending.operation_id);
});

test('global sensor state rejects social scope and never accepts widening a matching legacy operation ID', async t => {
  const pending = { operation_id: 'legacy', sensor_type: 'rainfall', status: 'pending' };
  const view = harness(t, pending, 'aidp', allSensors);
  view.poll(); view.requests[0].resolve({ ...pending, sensor_type: 'all', status: 'completed' }); await view.settle();
  assert.equal(view.completed(), 0); assert.equal(view.state().sensor_type, 'rainfall');
  view.poll(); view.requests[1].resolve({ operation_id: pending.operation_id, status: 'completed' }); await view.settle();
  assert.equal(view.completed(), 0); assert.equal(view.state().sensor_type, 'rainfall');
});

test('an error can close without releasing controls or reopening from the same operation poll', async t => {
  const failed = { operation_id: 'incomplete', status: 'error', stage: 'history', revision: 8, error: 'Cleanup interrupted' };
  const view = harness(t, failed, 'aidp');
  view.poll(); const inflight = view.requests[0];
  view.click('Close'); assert.equal(view.modal(), false); assert.equal(view.focused(), 'origin');
  assert.equal(view.state().status, 'error', 'Closing a popup must not release the backend or parent guard');
  inflight.resolve({ ...failed, revision: 9 }); await view.settle();
  view.status({ ...failed, revision: 8 }); assert.equal(view.modal(), false);
  assert.equal(view.requests.filter(request => request.method === 'POST').length, 0);
  view.click('Retry Synthetic reset'); assert.equal(view.modal(), true);
  view.click('Cancel'); view.escape(); assert.equal(view.modal(), false);
  assert.equal(view.state().operation_id, 'incomplete'); assert.equal(view.state().status, 'error');
  view.poll(); view.requests[1].reject(new Error('Temporary status failure')); await view.settle();
  assert.equal(view.modal(), false, 'A failed status check must not reopen a dismissed error');
});

test('confirmed cancellation closes legacy progress, rejects stale state and starts a fresh global reset only after confirmation', async t => {
  const failed = { operation_id: 'cancelled-legacy', sensor_type: 'river_level', status: 'error', stage: 'history', revision: 8 };
  const view = harness(t, failed, 'aidp', allSensors);
  view.click('Retry River Level reset');
  const staleConfirm = view.find('button', p => p.className === 'confirm-primary').props.onClick;
  view.poll(); const inflight = view.requests[0];
  const cancelled = { ...failed, status: 'cancelled', revision: 9, cancelled_at: '2026-10-05T18:49:41Z', replacements: { old: 'new' } };
  view.status(cancelled);
  assert.equal(view.modal(), false); assert.equal(view.progress(), ''); assert.equal(view.timers.size, 0);
  assert.equal(view.completed(), 0, 'Cancellation does not claim completed deletion');
  assert.equal(inflight.signal.aborted, true); assert.equal(view.state().cancelled_at, cancelled.cancelled_at);
  staleConfirm(); await view.settle(); assert.equal(view.requests.length, 1);
  inflight.resolve({ ...failed, revision: 8 }); await view.settle();
  view.status(failed); assert.equal(view.modal(), false); assert.equal(view.state().status, 'cancelled');
  assert.doesNotMatch(view.markup(), /Retry|progress-orbit|progressbar/);
  view.click('Delete Synthetic sensor data'); assert.match(view.markup(), /all five sensor types/);
  view.click('Cancel'); assert.equal(view.requests.length, 1);
  view.click('Delete Synthetic sensor data'); view.confirm();
  assert.equal(view.requests[1].url, '/api/admin/territorial/sensors/reset');
  const body = JSON.parse(view.requests[1].body);
  assert.notEqual(body.operation_id, cancelled.operation_id); assert.equal(body.confirm, true);
});

test('cancelled status is terminal on initial load and cancellation metadata alone does not release an error', t => {
  const cancelled = { operation_id: 'cancelled', sensor_type: 'rainfall', status: 'cancelled', revision: 9, cancelled_at: '2026-10-05T18:49:41Z' };
  const view = harness(t, cancelled, 'aidp', allSensors);
  assert.equal(view.modal(), false); assert.equal(view.progress(), ''); assert.equal(view.timers.size, 0);
  assert.equal(view.state().status, 'cancelled'); assert.equal(view.completed(), 0);
  view.status({ ...cancelled, status: 'error', revision: 8 }); assert.equal(view.state().status, 'cancelled');
});

test('only a verified terminal cancellation of the same scope and current revision can stop a pending request', async t => {
  const pending = { operation_id: 'own', sensor_type: 'river_level', status: 'pending', revision: 10 };
  const view = harness(t, pending, 'aidp', allSensors);
  view.poll(); const inflight = view.requests[0];
  for (const next of [
    { ...pending, status: 'cancelled', revision: 9 },
    { ...pending, status: 'cancelled', revision: 11, operation_id: 'other' },
    { ...pending, status: 'cancelled', revision: 11, sensor_type: 'all' },
  ]) {
    view.status(next); assert.equal(view.modal(), true); assert.equal(inflight.signal.aborted, false);
    assert.equal(view.state().status, 'pending');
  }
  inflight.resolve({ ...pending, status: 'error', revision: 11, cancelled_at: '2026-10-05T18:49:41Z' }); await view.settle();
  assert.equal(view.modal(), true); assert.equal(view.state().status, 'error');
  view.escape(); assert.equal(view.modal(), false); assert.equal(view.state().status, 'error');
});

test('a cancellation received directly from status polling never offers retry of the cancelled operation', async t => {
  const pending = { operation_id: 'own', status: 'pending', revision: 10 };
  const view = harness(t, pending, 'aidp');
  view.poll(); view.requests[0].resolve({ ...pending, status: 'cancelled', revision: 11 }); await view.settle();
  assert.equal(view.modal(), false); assert.equal(view.timers.size, 0); assert.equal(view.state().status, 'cancelled');
  view.click('Delete Synthetic data'); view.confirm();
  assert.notEqual(JSON.parse(view.requests[1].body).operation_id, pending.operation_id);
});
