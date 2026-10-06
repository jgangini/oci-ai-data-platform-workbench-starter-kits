import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';

const compile = file => ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const compiled = compile('../src/GodsEyeViewSensorReadings.tsx'), state = {};
new Function('exports', compile('../src/godsEyeViewAdminState.ts'))(state);
const text = node => typeof node === 'object' && node ? [node.props?.children].flat(Infinity).map(text).join('') : String(node ?? '');
const readings = Array.from({ length: 45 }, (_, index) => ({ id: `event-${String(index).padStart(2, '0')}`, sensor_id: `station-${String(index).padStart(2, '0')}`,
  sensor_type: index % 2 ? 'rainfall' : 'river_level', observed_at: `2026-10-04T12:${String(index).padStart(2, '0')}:00Z`, department: 'Bogotá D.C.', municipality: 'Bogotá', locality: 'Kennedy', value: index + 0.25, unit: 'mm', status: index % 3 ? 'normal' : 'critical' }));

function harness(t, overrides = {}) {
  let cursor = 0, dirty = true, effects = [], tree, now = 0, nextTimer = 0;
  const slots = [], requests = [], timers = new Map(), timeouts = new Map(), listeners = new Map(), previous = globalThis.window, previousDocument = globalThis.document;
  globalThis.document = { hidden: false, addEventListener(name, callback) { listeners.set(name, callback); }, removeEventListener(name) { listeners.delete(name); } };
  globalThis.window = {
    setInterval(callback, delay) { assert.equal(delay, 5000); timers.set(++nextTimer, callback); return nextTimer; }, clearInterval(id) { timers.delete(id); },
    setTimeout(callback, delay) { timeouts.set(++nextTimer, { callback, at: now + delay }); return nextTimer; }, clearTimeout(id) { timeouts.delete(id); },
  };
  const hooks = {
    useState(initial) { const id = cursor++; if (!(id in slots)) slots[id] = initial; return [slots[id], value => { const next = typeof value === 'function' ? value(slots[id]) : value; dirty ||= !Object.is(slots[id], next); slots[id] = next; }]; },
    useEffect(callback, deps) { const id = cursor++, old = slots[id]; if (!old || deps.some((value, index) => value !== old.deps[index])) effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; }); },
  };
  const component = {}; new Function('exports', 'require', compiled)(component, name => name === 'react' ? hooks : name === 'react/jsx-runtime' ? jsxRuntime : name === './LoadingIndicator' ? { LoadingIndicator() {} } : state);
  const props = { api: (path, options) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })), timeZone: 'America/Bogota', family: '', families: { river_level: 'River level', rainfall: 'Rainfall' }, searchIcon: null, refreshIcon: null, ...overrides };
  function render() { for (let n = 0; dirty && n < 20; n++) { cursor = 0; dirty = false; effects = []; tree = component.GodsEyeViewSensorReadings(props); effects.forEach(effect => effect()); } assert.equal(dirty, false); }
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const find = (type, match = () => true) => { const value = nodes(tree).find(node => node?.type === type && match(node.props)); assert.ok(value, `Missing ${type}`); return value; };
  const act = callback => { callback(); render(); };
  const cleanup = () => slots.forEach(slot => slot?.cleanup?.());
  t.after(() => { cleanup(); if (previous === undefined) delete globalThis.window; else globalThis.window = previous; if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument; });
  render();
  return { requests, find, act, timers, cleanup, rows: () => find('tbody').props.children,
    count: () => text(find('span', p => p['aria-live'] === 'polite')),
    change: values => act(() => { Object.assign(props, values); dirty = true; }),
    hidden: value => act(() => { document.hidden = value; listeners.get('visibilitychange')?.(); }),
    poll: () => act(() => [...timers.values()].forEach(callback => callback())),
    advance: ms => act(() => { now += ms; for (const [id, timer] of timeouts) if (timer.at <= now) { timeouts.delete(id); timer.callback(); } }),
    search: query => act(() => find('input', p => p.type === 'search').props.onChange({ target: { value: query } })),
    submit: () => act(() => find('form').props.onSubmit({ preventDefault() {} })),
    settle: async () => { await new Promise(setImmediate); render(); },
    click: name => act(() => { const button = find('button', p => p['aria-label'] === name || p.children === name); assert.ok(!button.props.disabled); button.props.onClick(); }),
    status: value => act(() => find('select', p => typeof p.value === 'string').props.onChange({ target: { value } })),
    size: value => act(() => find('select', p => typeof p.value === 'number').props.onChange({ target: { value: String(value) } })),
  };
}

const page = (items, total = items.length, number = 1, version = 'gold-test') => ({ items, total, page: number, version });
const params = request => {
  const url = new URL(request.path, 'https://example.test');
  assert.equal(url.pathname, '/api/admin/gods-eye-view/sensors/readings');
  return Object.fromEntries(url.searchParams);
};
const firstPage = [...readings].reverse().slice(0, 20), secondPage = [...readings].reverse().slice(20, 40);
const defaults = { page: '1', limit: '20', order: 'desc' };

test('readings request only a server page and reset paging when filters, order or page size change', async t => {
  const h = harness(t);
  assert.deepEqual(params(h.requests[0]), defaults);
  assert.ok(h.find('div', p => p.className === 'table-wrap gods-eye-view-post-table').props.children.some(node => node?.props?.label === 'Loading sensor readings…'));
  h.requests[0].resolve(page(firstPage, 45)); await h.settle();
  assert.equal(h.rows().length, 20); assert.equal(h.rows()[0].key, 'station-44'); assert.equal(h.count(), '1–20 of 45');
  assert.equal(text(h.find('time')), state.timestamp(readings[44].observed_at, 'America/Bogota'));
  h.click('Next'); assert.equal(h.rows().length, 0); assert.deepEqual(params(h.requests[1]), { ...defaults, page: '2' });
  h.requests[1].resolve(page(secondPage, 45, 2)); await h.settle(); assert.equal(h.count(), '21–40 of 45');
  h.change({ family: 'river_level' }); assert.deepEqual(params(h.requests[2]), { ...defaults, family: 'river_level' });
  h.requests[2].resolve(page([readings[0], readings[2]], 2)); await h.settle();
  h.search(' BOGOTA '); h.advance(249); assert.equal(h.requests.length, 3); h.advance(1);
  assert.deepEqual(params(h.requests[3]), { ...defaults, family: 'river_level', q: 'BOGOTA' });
  h.requests[3].resolve(page([readings[0]], 1)); await h.settle();
  h.status('critical'); assert.deepEqual(params(h.requests[4]), { ...defaults, family: 'river_level', q: 'BOGOTA', status: 'critical' });
  h.requests[4].resolve(page([readings[0]], 1)); await h.settle();
  h.click('Sort by observation date, oldest first'); assert.equal(params(h.requests[5]).order, 'asc');
  h.requests[5].resolve(page([readings[0]], 1)); await h.settle();
  h.size(10); assert.deepEqual(params(h.requests[6]), { ...defaults, limit: '10', order: 'asc', family: 'river_level', q: 'BOGOTA', status: 'critical' });
  h.requests[6].resolve(page([readings[0]], 1)); await h.settle();
  assert.equal(h.rows().length, 1); assert.equal(h.count(), '1–1 of 1');
});

test('polls preserve filters and pages, retain data on error and ignore superseded responses', async t => {
  const h = harness(t); h.requests[0].resolve(page(firstPage, 45)); await h.settle();
  h.search('kennedy'); h.advance(250); h.requests[1].resolve(page(firstPage, 45)); await h.settle();
  h.click('Next'); h.requests[2].resolve(page(secondPage, 45, 2)); await h.settle();
  h.poll(); h.poll(); assert.equal(h.requests.length, 4);
  assert.deepEqual(params(h.requests[3]), { ...defaults, page: '2', q: 'kennedy' });
  h.requests[3].resolve(page(secondPage, 45, 2)); await h.settle(); assert.equal(h.count(), '21–40 of 45');
  h.poll(); h.requests[4].reject(new Error('Readings unavailable')); await h.settle();
  assert.equal(h.count(), '21–40 of 45'); assert.equal(text(h.find('p', p => p.role === 'alert')), 'Readings unavailable');
  h.poll(); h.click('Refresh sensor readings'); assert.equal(h.requests[5].options.signal.aborted, true);
  h.requests[6].resolve(page(readings.slice(0, 5), 5)); await h.settle(); assert.equal(h.count(), '1–5 of 5');
  h.requests[5].resolve(page(secondPage, 45, 2)); await h.settle(); assert.equal(h.count(), '1–5 of 5');
  h.poll(); assert.equal(params(h.requests[7]).page, '1');
  h.requests[7].resolve(page([], 0)); await h.settle(); assert.equal(h.count(), '0–0 of 0');
  assert.equal(text(h.find('p', p => p.className === 'empty')), 'No readings match these filters.');
  assert.equal(h.find('input').props.value, 'kennedy');
});

test('hidden modules abort requests, hidden browser tabs stop polls and returning preserves the visible page', async t => {
  const h = harness(t, { active: false }); assert.equal(h.requests.length, 0); assert.equal(h.timers.size, 0);
  h.change({ active: true }); h.requests[0].resolve(page(firstPage, 45)); await h.settle();
  h.click('Next'); h.requests[1].resolve(page(secondPage, 45, 2)); await h.settle();
  h.poll(); h.change({ active: false }); assert.equal(h.requests[2].options.signal.aborted, true); assert.equal(h.timers.size, 0);
  h.requests[2].resolve(page([], 0)); await h.settle(); assert.equal(h.count(), '21–40 of 45');
  h.change({ active: true }); assert.equal(params(h.requests[3]).page, '2');
  h.requests[3].resolve(page(secondPage, 45, 2)); await h.settle(); assert.equal(h.count(), '21–40 of 45');
  h.hidden(true); h.poll(); assert.equal(h.requests.length, 4);
  h.hidden(false); assert.equal(h.requests.length, 5); h.poll(); assert.equal(h.requests.length, 5);
  h.cleanup(); assert.equal(h.requests[4].options.signal.aborted, true); assert.equal(h.timers.size, 0);
});

test('backend-clamped pages are used by future polls and remain navigable when the dataset grows again', async t => {
  const h = harness(t); h.requests[0].resolve(page(firstPage, 45)); await h.settle();
  h.click('Next'); h.requests[1].resolve(page(secondPage, 45, 2)); await h.settle();
  h.poll(); h.requests[2].resolve(page(readings.slice(0, 5), 5)); await h.settle();
  assert.equal(h.count(), '1–5 of 5'); assert.equal(h.requests.length, 3, 'Clamping does not duplicate the request');
  h.poll(); assert.equal(params(h.requests[3]).page, '1');
  h.requests[3].resolve(page(firstPage, 45)); await h.settle();
  h.click('Next'); assert.equal(params(h.requests[4]).page, '2');
  h.requests[4].resolve(page(secondPage, 45, 2)); await h.settle(); assert.equal(h.count(), '21–40 of 45');
});

test('changing a filter clears stale rows and a late reply cannot cross the new filter', async t => {
  const h = harness(t, { family: 'river_level' }); h.requests[0].resolve(page([readings[0]], 1)); await h.settle();
  h.poll(); h.change({ family: 'rainfall' });
  assert.equal(h.requests[1].options.signal.aborted, true); assert.equal(h.rows().length, 0);
  assert.deepEqual(params(h.requests[2]), { ...defaults, family: 'rainfall' });
  h.requests[1].resolve(page([readings[0]], 1)); await h.settle(); assert.equal(h.rows().length, 0);
  h.requests[2].resolve(page([readings[1]], 1)); await h.settle(); assert.equal(h.rows()[0].key, 'station-01');
  h.search('  Bogotá  '); h.submit(); h.advance(300); assert.equal(h.requests.length, 4);
  assert.equal(params(h.requests[3]).q, 'Bogotá');
  h.requests[3].reject(new Error('Search unavailable')); await h.settle();
  assert.equal(h.rows().length, 0); assert.equal(text(h.find('p', p => p.role === 'alert')), 'Search unavailable');
  assert.equal(h.find('div', p => p.className === 'table-wrap gods-eye-view-post-table').props['aria-busy'], false);
  h.click('Refresh sensor readings'); h.requests[4].resolve(page([], 0)); await h.settle();
  assert.equal(text(h.find('p', p => p.className === 'empty')), 'No readings match these filters.');
});
