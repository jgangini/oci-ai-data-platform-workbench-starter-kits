import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';

const compiled = Object.fromEntries(['PrismaParameters', 'PrismaOciParameters'].map(name => [name, ts.transpileModule(readFileSync(new URL(`../src/${name}.tsx`, import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText]));
const provider = { id: 'google-maps', label: 'Google Maps', configured: true, fields: [{ id: 'GOOGLE_MAPS_API_KEY', label: 'API key', configured: true, secret: true, client_exposed: true }, { id: 'SECOND_KEY', label: 'Other key', configured: true, secret: true }] };
const providerLinks = {
  'google-maps': 'https://developers.google.com/maps/documentation/tile/get-api-key', openai: 'https://platform.openai.com/api-keys',
  aisstream: 'https://aisstream.io/account', firms: 'https://firms.modaps.eosdis.nasa.gov/api/map_key/',
  tomtom: 'https://docs.tomtom.com/platform/documentation/my-tomtom/how-to-get-a-tomtom-api-key', 'cesium-ion': 'https://ion.cesium.com/tokens',
  opensky: 'https://openskynetwork.github.io/opensky-api/rest.html#authentication', 'launch-library': 'https://lldev.thespacedevs.com/docs',
};
const providers = Object.keys(providerLinks).map((id, index) => ({ ...provider, id, label: id, configured: index % 2 === 0 }));
const oci = { model_id: 'current', model_name: 'Current model', configured: true, available: true, region: 'us-chicago-1', voice: 'ara', voices: ['ara', 'eve'] };
function harness(t, componentName, props, tabList = null) {
  let cursor = 0, dirty = true, effects = [], tree, focused = ''; const slots = [], setters = [], requests = [], modules = {};
  const hooks = {
    useState(initial) { const id = cursor++; if (!(id in slots)) slots[id] = typeof initial === 'function' ? initial() : initial; setters[id] ||= value => { const next = typeof value === 'function' ? value(slots[id]) : value; dirty ||= !Object.is(slots[id], next); slots[id] = next; }; return [slots[id], setters[id]]; },
    useRef(initial) { const id = cursor++; return slots[id] ||= { current: initial }; },
    useEffect(callback, deps) { const id = cursor++, old = slots[id]; if (!old || deps.some((value, index) => value !== old.deps[index])) effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; }); },
  };
  function require(name) {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return jsxRuntime;
    if (name === './prismaAdminState') return { prismaEndpoint: '/api/admin/prisma', prismaError: reason => reason.message };
    return load(name.slice(2));
  }
  function load(name) { if (!modules[name]) { modules[name] = {}; new Function('exports', 'require', compiled[name])(modules[name], require); } return modules[name]; }
  const component = Object.assign({}, load('PrismaParameters'), load('PrismaOciParameters'))[componentName];
  props = { active: true, api: (path, options) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })), ...props };
  function render() { for (let n = 0; dirty && n < 20; n++) { cursor = 0; dirty = false; effects = []; tree = component(props); for (const node of nodes(tree)) if (node?.props?.role === 'tab') node.props.ref?.({ focus() { focused = node.props.id; } }); if (tabList) find('div', p => p.role === 'tablist').props.ref.current = tabList; effects.forEach(effect => effect()); } assert.equal(dirty, false); }
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const find = (type, match = () => true) => { const value = nodes(tree).find(node => (typeof type === 'string' ? node?.type === type : typeof node?.type === 'function' && node.type.name === type.name) && match(node.props)); assert.ok(value, `Missing ${typeof type === 'string' ? type : type.name}`); return value; };
  const act = callback => { callback(); render(); };
  t.after(() => slots.forEach(slot => slot?.cleanup?.())); render();
  return { requests, find, act, nodes: () => nodes(tree), focused: () => focused, advance: milliseconds => act(() => t.mock.timers.tick(milliseconds)), props: patch => act(() => { props = { ...props, ...patch }; dirty = true; }), settle: async () => { await new Promise(setImmediate); render(); },
    click: name => act(() => { const button = find('button', props => props.children === name); assert.ok(!button.props.disabled); button.props.onClick(); }),
    submit: () => act(() => find('form').props.onSubmit({ preventDefault() {} })),
    confirm: () => act(() => find(function ParameterConfirmation() {}).props.onConfirm()),
    cancel: () => act(() => find(function ParameterConfirmation() {}).props.onCancel()) };
}

test('provider replacement requires confirmation, omits blanks and never discloses the key in the confirmation', async t => {
  const saved = [], h = harness(t, 'PrismaProviderParameters', { provider, revision: 'environment', onSaved: next => saved.push(next) });
  assert.equal(h.find('input').props.value, ''); assert.equal(h.find('input').props.type, 'password'); assert.equal(h.find('input').props.maxLength, 512);
  h.act(() => h.find('input').props.onChange({ target: { value: 'secret-new-key' } })); h.submit();
  assert.equal(h.requests.length, 0);
  const confirm = h.find(function ParameterConfirmation() {});
  assert.deepEqual(confirm.props.changes, ['API key: replace value']); assert.ok(!JSON.stringify(confirm.props).includes('secret-new-key'));
  h.cancel(); assert.equal(h.find('input').props.value, 'secret-new-key'); h.submit(); h.act(() => { confirm.props.onConfirm(); confirm.props.onConfirm(); });
  assert.equal(h.requests.length, 1);
  assert.deepEqual(JSON.parse(h.requests[0].options.body), { expected_revision: 'environment', values: { GOOGLE_MAPS_API_KEY: 'secret-new-key' } });
  h.requests[0].resolve({ revision: 'updated', providers: [provider], message: 'Saved and reloaded.' }); await h.settle();
  assert.equal(h.find('input').props.value, ''); assert.equal(saved[0].revision, 'updated');
});

test('provider Test checks draft credentials without saving and reports quota separately', async t => {
  const h = harness(t, 'PrismaProviderParameters', { provider, revision: 'revision', onSaved: () => assert.fail('Test must not save') });
  h.act(() => h.find('input').props.onChange({ target: { value: 'replacement' } })); h.click('Test');
  assert.equal(h.requests[0].path, '/api/admin/prisma/parameters/google-maps/test'); assert.equal(h.requests[0].options.method, 'POST');
  h.requests[0].resolve({ ok: false, status: 'rate_limited', message: 'Provider quota exceeded. Try again later.' }); await h.settle();
  assert.equal(h.find('input').props.value, 'replacement'); assert.match(h.find('p', p => p.role === 'alert').props.children, /quota/);
  h.click('Test'); h.props({ active: false }); assert.equal(h.requests[1].options.signal.aborted, true);
  h.requests[1].resolve({ ok: true, message: 'Late response' }); await h.settle();
  assert.ok(!h.nodes().some(node => node.props?.children === 'Late response')); assert.equal(h.find('input').props.value, 'replacement');
});

test('Parameters refreshes after Save, polls pending application through read failures and stops when inactive', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = harness(t, 'PrismaParameters', { active: false }); assert.equal(h.requests.length, 0);
  h.props({ active: true }); h.requests[0].resolve({ revision: 'old', providers: [provider] }); await h.settle();
  h.act(() => h.find(function PrismaProviderParameters() {}).props.onRefresh());
  h.act(() => h.find(function PrismaProviderParameters() {}).props.onSaved({ revision: 'new', providers: [provider], runtime_status: 'pending' }));
  assert.equal(h.requests.length, 3); assert.equal(h.requests[1].options.signal.aborted, true);
  h.requests[1].resolve({ revision: 'old', providers: [provider] }); await h.settle();
  assert.equal(h.find(function PrismaProviderParameters() {}).props.revision, 'new');
  h.requests[2].resolve({ revision: 'new', providers: [provider], runtime_status: 'pending' }); await h.settle();
  assert.ok(h.nodes().some(node => node.props?.className === 'prisma-reset-status'));
  h.advance(2499); assert.equal(h.requests.length, 3); h.advance(1); assert.equal(h.requests.length, 4);
  h.requests[3].reject(new Error('Globe status unavailable')); await h.settle();
  assert.equal(h.find('p', p => p.role === 'alert').props.children, 'Globe status unavailable');
  h.advance(2500); assert.equal(h.requests.length, 5);
  h.props({ active: false }); assert.equal(h.requests[4].options.signal.aborted, true);
  h.requests[4].resolve({ revision: 'old', providers: [provider], runtime_status: 'applied' }); await h.settle();
  h.advance(10000); assert.equal(h.requests.length, 5); assert.equal(h.find(function PrismaProviderParameters() {}).props.revision, 'new');
  h.props({ active: true }); h.requests[5].resolve({ revision: 'new', providers: [provider], runtime_status: 'applied' }); await h.settle();
  h.advance(10000); assert.equal(h.requests.length, 6);
  assert.ok(!h.nodes().some(node => node.props?.className === 'prisma-reset-status' || node.props?.role === 'alert'));
  assert.ok(h.requests.every(request => request.path === '/api/admin/prisma/parameters' && request.options.method === undefined));
});

test('a failed provider read exposes Retry and keeps OCI tabs reachable through recovery', async t => {
  const h = harness(t, 'PrismaParameters', {});
  const selected = () => h.find('button', p => p.role === 'tab' && p['aria-selected']);
  assert.equal(selected().props.id, 'prisma-parameter-tab-oci-text'); assert.equal(selected().props.tabIndex, 0);
  assert.equal(h.find('section', p => p.role === 'tabpanel' && !p.hidden).props.id, 'prisma-parameter-panel-oci-text');
  assert.ok(!h.nodes().some(node => node.type === 'button' && /Refresh|Retry/.test(node.props.children)));
  h.requests[0].reject(new Error('Settings unavailable')); await h.settle();
  h.act(() => selected().props.onKeyDown({ key: 'ArrowRight', preventDefault() {} }));
  assert.equal(selected().props.id, 'prisma-parameter-tab-oci-voice'); assert.equal(selected().props.tabIndex, 0);
  assert.equal(h.focused(), 'prisma-parameter-tab-oci-voice'); assert.equal(h.requests.length, 1);
  h.click('Retry settings'); h.requests[1].resolve({ revision: 'loaded', providers }); await h.settle();
  assert.ok(!h.nodes().some(node => node.props?.role === 'alert' || node.props?.children === 'Retry settings'));
  assert.equal(h.find(function PrismaProviderParameters() {}).props.revision, 'loaded');
  assert.equal(selected().props.id, 'prisma-parameter-tab-oci-voice');
  assert.equal(h.find('section', p => p.role === 'tabpanel' && !p.hidden).props.id, 'prisma-parameter-panel-oci-voice');
});

test('provider tabs collapse on repeated clicks, preserve mounted drafts and always open through keyboard navigation', async t => {
  const h = harness(t, 'PrismaParameters', {}); h.requests[0].resolve({ revision: 'loaded', providers }); await h.settle();
  const panels = () => h.nodes().filter(node => node.props?.role === 'tabpanel');
  const identities = panels().map(node => [node.key, node.props.id, node.props.children.type]);
  const visible = () => panels().filter(node => !node.props.hidden).map(node => node.props.id);
  const selected = () => h.find('button', p => p.role === 'tab' && p['aria-selected']);
  const dot = id => h.find('button', p => p.id === `prisma-parameter-tab-${id}`).props.children[1].props;
  const editor = harness(t, 'PrismaProviderParameters', h.find(function PrismaProviderParameters() {}).props);
  editor.act(() => editor.find('input').props.onChange({ target: { value: 'unsaved-replacement' } }));
  assert.equal(panels().length, 10); assert.deepEqual(visible(), ['prisma-parameter-panel-google-maps']);
  h.act(() => selected().props.onClick());
  assert.deepEqual(visible(), []); assert.equal(selected().props.id, 'prisma-parameter-tab-google-maps');
  assert.equal(selected().props.tabIndex, 0); assert.equal(selected().props['aria-expanded'], false);
  assert.equal(panels().length, 10); assert.ok(panels().every(node => node.props.children.props.active));
  h.act(() => selected().props.onClick());
  assert.deepEqual(visible(), ['prisma-parameter-panel-google-maps']); assert.equal(selected().props['aria-expanded'], true);
  h.act(() => selected().props.onClick());
  h.act(() => h.find('button', p => p.id === 'prisma-parameter-tab-openai').props.onClick());
  assert.deepEqual(visible(), ['prisma-parameter-panel-openai']); assert.equal(selected().props['aria-expanded'], true);
  for (const item of providers) assert.equal(dot(item.id)['aria-label'], item.configured ? 'Configured' : 'Not configured');
  assert.equal(dot('oci-text')['aria-label'], 'Loading settings'); assert.equal(dot('oci-voice')['aria-label'], 'Loading settings');
  const text = h.find(function PrismaOciParameters() {}, p => !p.voice), voice = h.find(function PrismaOciParameters() {}, p => p.voice);
  h.act(() => { text.props.onConfigured(true); voice.props.onConfigured(false); });
  assert.equal(dot('oci-text')['aria-label'], 'Configured'); assert.equal(dot('oci-voice')['aria-label'], 'Not configured');
  for (const [key, id] of [['End', 'oci-voice'], ['End', 'oci-voice'], ['ArrowRight', 'google-maps'], ['ArrowDown', 'openai'], ['Home', 'google-maps'], ['Home', 'google-maps'], ['ArrowLeft', 'oci-voice']]) {
    h.act(() => selected().props.onClick()); assert.deepEqual(visible(), []);
    let prevented = false; h.act(() => selected().props.onKeyDown({ key, preventDefault() { prevented = true; } }));
    assert.equal(prevented, true); assert.equal(h.focused(), `prisma-parameter-tab-${id}`); assert.deepEqual(visible(), [`prisma-parameter-panel-${id}`]);
    assert.equal(selected().props['aria-expanded'], true); assert.equal(selected().props.tabIndex, 0);
    assert.ok(h.nodes().filter(node => node.props?.role === 'tab' && !node.props['aria-selected']).every(node => node.props['aria-expanded'] === false));
    if (key === 'Home' || key === 'End') { h.act(() => selected().props.onKeyDown({ key, preventDefault() {} })); assert.deepEqual(visible(), [`prisma-parameter-panel-${id}`]); }
  }
  assert.deepEqual(panels().map(node => [node.key, node.props.id, node.props.children.type]), identities);
  editor.props(h.find(function PrismaProviderParameters() {}).props); assert.equal(editor.find('input').props.value, 'unsaved-replacement');
  assert.ok(panels().every(node => node.props.children.props.active === true)); assert.equal(h.requests.length, 1);
  assert.equal(h.find(function PrismaOciParameters() {}, p => !p.voice).props.onConfigured, text.props.onConfigured);
  h.props({ active: false }); assert.ok(panels().every(node => node.props.children.props.active === false));
});

test('provider scroll controls follow overflow, resize and activation without selecting or reloading providers', async t => {
  const observers = [], original = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe(element) { this.element = element; }
    disconnect() { this.disconnected = true; }
  };
  t.after(() => { if (original) globalThis.ResizeObserver = original; else delete globalThis.ResizeObserver; });
  const list = { scrollLeft: 0, clientWidth: 300, scrollWidth: 300, scrollBy({ left }) { this.scrollLeft = Math.max(0, Math.min(this.scrollWidth - this.clientWidth, this.scrollLeft + left)); } };
  const h = harness(t, 'PrismaParameters', {}, list);
  const arrow = direction => h.find('button', p => p['aria-label'] === `Scroll providers ${direction}`).props;
  const scroll = direction => h.act(() => { assert.equal(arrow(direction).disabled, false); arrow(direction).onClick(); h.find('div', p => p.role === 'tablist').props.onScroll(); });
  assert.equal(arrow('left').disabled, true); assert.equal(arrow('right').disabled, true);
  list.scrollWidth = 900;
  h.requests[0].resolve({ revision: 'loaded', providers }); await h.settle();
  assert.equal(observers[0].disconnected, true); assert.equal(arrow('right').disabled, false);
  scroll('right'); assert.equal(list.scrollLeft, 240); assert.equal(arrow('left').disabled, false);
  scroll('right'); scroll('right'); assert.equal(list.scrollLeft, 600); assert.equal(arrow('right').disabled, true);
  scroll('left'); assert.equal(list.scrollLeft, 360); assert.equal(arrow('right').disabled, false);
  assert.equal(h.find('button', p => p.role === 'tab' && p['aria-selected']).props.id, 'prisma-parameter-tab-google-maps');
  assert.equal(h.requests.length, 1);
  list.clientWidth = 900; list.scrollLeft = 0;
  h.act(() => observers.at(-1).callback()); assert.equal(arrow('left').disabled, true); assert.equal(arrow('right').disabled, true);
  h.props({ active: false }); assert.equal(observers.at(-1).disconnected, true);
  list.clientWidth = 300; list.scrollLeft = 599.5;
  h.props({ active: true }); assert.equal(arrow('left').disabled, false); assert.equal(arrow('right').disabled, true);
  assert.equal(observers.at(-1).element, list); assert.ok(!observers.at(-1).disconnected);
});

test('provider Save and Test conflicts refresh metadata without discarding entered credentials', async t => {
  for (const kind of ['save', 'test']) {
    let refreshed = 0;
    const h = harness(t, 'PrismaProviderParameters', { provider, revision: 'old', onSaved: () => assert.fail('Conflict must not save'), onRefresh: () => refreshed++ });
    h.act(() => h.find('input').props.onChange({ target: { value: 'replacement' } }));
    if (kind === 'save') { h.submit(); h.confirm(); } else h.click('Test');
    h.requests[0].reject(Object.assign(new Error('Configuration changed'), { status: 409 })); await h.settle();
    assert.equal(refreshed, 1); assert.equal(h.find('input').props.value, 'replacement');
    h.props({ revision: 'new', provider: { ...provider, configured: false } });
    assert.equal(h.find('input').props.value, 'replacement'); assert.equal(h.find('p', p => p.role === 'alert').props.children, 'Configuration changed');
    if (kind === 'save') { h.submit(); h.confirm(); } else h.click('Test');
    assert.deepEqual(JSON.parse(h.requests[1].options.body), { expected_revision: 'new', values: { GOOGLE_MAPS_API_KEY: 'replacement' } });
  }
});

test('all provider editors include their official help links and OCI reports only authoritative configured state', async t => {
  for (const item of providers) {
    const h = harness(t, 'PrismaProviderParameters', { provider: item, revision: 'loaded', onSaved() {} });
    const link = h.find('a', p => p.className === 'prisma-key-help').props;
    assert.equal(link.href, providerLinks[item.id]); assert.equal(link.target, '_blank'); assert.equal(link.rel, 'noopener noreferrer');
    assert.equal(link['aria-label'], `Get key information for ${item.label}`); assert.equal(h.requests.length, 0);
  }
  for (const voice of [false, true]) {
    const reports = [], h = harness(t, 'PrismaOciParameters', { voice, onConfigured: value => reports.push(value) });
    const link = h.find('a', p => p.className === 'prisma-key-help').props;
    assert.equal(link.href, 'https://docs.oracle.com/en-us/iaas/Content/generative-ai/getting-started.htm');
    assert.equal(link.target, '_blank'); assert.equal(link.rel, 'noopener noreferrer'); assert.equal(link['aria-label'], `Get key information for OCI Generative AI · ${voice ? 'voice' : 'text'}`);
    assert.deepEqual(reports, []); h.requests[0].resolve({ ...oci, configured: false }); await h.settle(); assert.deepEqual(reports, [false]);
    h.act(() => h.find('select', p => p.value === 'current').props.onChange({ target: { value: 'next' } }));
    assert.deepEqual(reports, [false]); h.submit(); h.confirm();
    h.requests[1].resolve({ ...oci, model_id: 'next' }); await h.settle(); assert.deepEqual(reports, [false, true]);
    h.props({ active: false }); h.props({ active: true }); h.requests[2].resolve({ ...oci, model_id: 'next', configured: false }); await h.settle();
    assert.deepEqual(reports, [false, true, false]);
  }
});

test('OCI model catalogs paginate, preserve drafts and require a confirmed Save before Test', async t => {
  const h = harness(t, 'PrismaOciParameters', {}); assert.equal(h.requests.length, 1); h.requests[0].resolve(oci); await h.settle();
  h.click('Load models'); h.requests[1].resolve({ items: [{ id: 'next', name: 'New model', selectable: true }, { id: 'retired', name: 'Old', selectable: false, reason: 'Retired' }], next_cursor: 'page/2' }); await h.settle();
  assert.equal(h.find('option', p => p.value === 'retired').props.disabled, true);
  h.act(() => h.find('select').props.onChange({ target: { value: 'next' } })); assert.equal(h.find('button', p => p.children === 'Test').props.disabled, true);
  h.props({ active: false }); h.props({ active: true }); h.requests[2].resolve(oci); await h.settle(); assert.equal(h.find('select').props.value, 'next');
  h.click('More models'); assert.match(h.requests[3].path, /cursor=page%2F2/); h.requests[3].resolve({ items: [], next_cursor: null }); await h.settle();
  h.submit(); assert.equal(h.requests.length, 4); h.confirm();
  assert.deepEqual(JSON.parse(h.requests[4].options.body), { model_id: 'next' });
  h.requests[4].resolve({ ...oci, model_id: 'next', model_name: 'New model' }); await h.settle();
  h.click('Test'); assert.equal(h.requests[5].path, '/api/admin/prisma/oci-provider/test'); assert.equal(h.requests[5].options.method, 'POST');
  h.requests[5].resolve({ test: { status: 'success', model_id: 'next' } }); await h.settle(); h.requests[6].resolve({ ...oci, model_id: 'next' }); await h.settle();
  assert.equal(h.find('p', p => p.className === 'prisma-success').props.children, 'Test passed.');
  h.act(() => h.find('select').props.onChange({ target: { value: 'current' } })); assert.ok(!h.nodes().some(node => node.props?.children === 'Test passed.'));
});

test('OCI voice Test is explicit, aborts on hiding and cannot report an interrupted inference as successful', async t => {
  const h = harness(t, 'PrismaOciParameters', { voice: true, active: false }); assert.equal(h.requests.length, 0);
  h.props({ active: true }); h.requests[0].resolve(oci); await h.settle();
  h.click('Test'); assert.equal(h.requests[1].path, '/api/admin/prisma/oci-voice/test'); assert.equal(h.requests[1].options.body, undefined);
  h.props({ active: false }); assert.equal(h.requests[1].options.signal.aborted, true);
  h.requests[1].resolve({ test: { status: 'success' } }); await h.settle(); assert.ok(!h.nodes().some(node => node.props?.children === 'Test passed.'));
  h.props({ active: true }); h.requests[2].resolve(oci); await h.settle(); h.click('Test'); h.requests[3].reject(new Error('OCI model is rate-limited. Please retry shortly.')); await h.settle();
  assert.match(h.find('p', p => p.role === 'alert').props.children, /rate-limited/);
});

test('OCI settings can retry a failed initial read without starting an inference', async t => {
  const h = harness(t, 'PrismaOciParameters', {}); h.requests[0].reject(new Error('Settings temporarily unavailable')); await h.settle();
  h.click('Retry settings'); assert.equal(h.requests[1].path, '/api/admin/prisma/oci-provider'); assert.equal(h.requests[1].options.method, undefined);
  h.requests[1].resolve(oci); await h.settle(); assert.equal(h.find('select').props.value, 'current'); assert.equal(h.requests.length, 2);
});

test('returning after an interrupted OCI Save reconciles the persisted selection and refresh preserves a different draft', async t => {
  const h = harness(t, 'PrismaOciParameters', {}); h.requests[0].resolve(oci); await h.settle();
  h.act(() => h.find('select').props.onChange({ target: { value: 'persisted' } })); h.submit(); h.confirm();
  h.props({ active: false }); assert.equal(h.requests[1].options.signal.aborted, true);
  h.requests[1].resolve({ ...oci, model_id: 'persisted' }); await h.settle();
  h.props({ active: true }); h.requests[2].resolve({ ...oci, model_id: 'persisted', model_name: 'Persisted model' }); await h.settle();
  assert.equal(h.find('select').props.value, 'persisted'); assert.equal(h.find('button', p => p.children === 'Test').props.disabled, false);
  h.act(() => h.find('select').props.onChange({ target: { value: 'draft' } })); h.props({ active: false }); h.props({ active: true });
  h.requests[3].resolve({ ...oci, model_id: 'externally-updated' }); await h.settle();
  assert.equal(h.find('select').props.value, 'draft'); assert.equal(h.find('button', p => p.children === 'Test').props.disabled, true);
  assert.equal(h.find('option', p => p.value === 'draft').props.children, 'draft');
});

test('OCI Test cannot label a concurrent model or voice change as a passing test for the displayed selection', async t => {
  const h = harness(t, 'PrismaOciParameters', { voice: true }); h.requests[0].resolve(oci); await h.settle(); h.click('Test');
  h.requests[1].resolve({ test: { status: 'success', model_id: 'current', voice: 'ara' } }); await h.settle();
  h.requests[2].resolve({ ...oci, voice: 'eve' }); await h.settle();
  assert.match(h.find('p', p => p.role === 'alert').props.children, /selection changed/); assert.ok(!h.nodes().some(node => node.props?.children === 'Test passed.'));
  assert.equal(h.find('select', p => p.value === 'eve').props.value, 'eve');
  h.click('Test'); h.requests[3].resolve({ test: { status: 'success', model_id: 'new', voice: 'eve' } }); await h.settle();
  h.requests[4].resolve({ ...oci, model_id: 'new', voice: 'eve' }); await h.settle();
  assert.equal(h.find('select', p => p.value === 'new').props.value, 'new'); assert.match(h.find('p', p => p.role === 'alert').props.children, /selection changed/);
});
