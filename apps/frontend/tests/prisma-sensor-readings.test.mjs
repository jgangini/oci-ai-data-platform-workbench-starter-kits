import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';

const compile = file => ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const compiled = compile('../src/PrismaSensorReadings.tsx'), state = {};
new Function('exports', compile('../src/prismaAdminState.ts'))(state);
const text = node => typeof node === 'object' && node ? [node.props?.children].flat(Infinity).map(text).join('') : String(node ?? '');
const readings = Array.from({ length: 45 }, (_, index) => ({ id: `event-${String(index).padStart(2, '0')}`, sensor_id: `station-${String(index).padStart(2, '0')}`,
  sensor_type: index % 2 ? 'rainfall' : 'river_level', observed_at: `2026-10-04T12:${String(index).padStart(2, '0')}:00Z`, department: 'Bogotá D.C.', municipality: 'Bogotá', locality: 'Kennedy', value: index + 0.25, unit: 'mm', status: index % 3 ? 'normal' : 'critical' }));

function harness(t, overrides = {}) {
  let cursor = 0, dirty = true, effects = [], tree, now = 0, nextTimer = 0;
  const slots = [], requests = [], timers = new Map(), timeouts = new Map(), previous = globalThis.window;
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
  function render() { for (let n = 0; dirty && n < 20; n++) { cursor = 0; dirty = false; effects = []; tree = component.PrismaSensorReadings(props); effects.forEach(effect => effect()); } assert.equal(dirty, false); }
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const find = (type, match = () => true) => { const value = nodes(tree).find(node => node?.type === type && match(node.props)); assert.ok(value, `Missing ${type}`); return value; };
  const act = callback => { callback(); render(); };
  const cleanup = () => slots.forEach(slot => slot?.cleanup?.());
  t.after(() => { cleanup(); if (previous === undefined) delete globalThis.window; else globalThis.window = previous; });
  render();
  return { requests, find, act, timers, cleanup, rows: () => find('tbody').props.children,
    count: () => text(find('span', p => p['aria-live'] === 'polite')),
    change: values => act(() => { Object.assign(props, values); dirty = true; }),
    poll: () => act(() => [...timers.values()].forEach(callback => callback())),
    advance: ms => act(() => { now += ms; for (const [id, timer] of timeouts) if (timer.at <= now) { timeouts.delete(id); timer.callback(); } }),
    search: query => act(() => find('input', p => p.type === 'search').props.onChange({ target: { value: query } })),
    settle: async () => { await new Promise(setImmediate); render(); },
    click: name => act(() => { const button = find('button', p => p['aria-label'] === name || p.children === name); assert.ok(!button.props.disabled); button.props.onClick(); }),
    status: value => act(() => find('select', p => typeof p.value === 'string').props.onChange({ target: { value } })),
    size: value => act(() => find('select', p => typeof p.value === 'number').props.onChange({ target: { value: String(value) } })),
  };
}

test('readings deduplicate stations, filter accents and family, sort dates, and reset local pagination', async t => {
  const h = harness(t);
  assert.equal(h.requests[0].path, '/api/prisma/snapshot');
  h.requests[0].resolve({ sensors: [...readings, { ...readings[44], id: 'older', observed_at: '2026-10-01T00:00:00Z', value: -1 }, readings[44]] }); await h.settle();
  assert.equal(h.rows().length, 20); assert.equal(h.rows()[0].key, 'station-44'); assert.equal(h.count(), '1–20 of 45');
  assert.equal(text(h.find('time')), state.timestamp(readings[44].observed_at, 'America/Bogota'));
  h.click('Next'); assert.equal(h.count(), '21–40 of 45');
  h.change({ family: 'river_level' }); assert.equal(h.count(), '1–20 of 23');
  h.click('Next'); assert.equal(h.count(), '21–23 of 23');
  h.search('BOGOTA'); h.advance(249); assert.equal(h.count(), '21–23 of 23'); h.advance(1); assert.equal(h.count(), '1–20 of 23');
  h.status('critical'); assert.equal(h.count(), '1–8 of 8'); assert.ok(h.rows().every(row => Number(row.key.slice(-2)) % 3 === 0));
  h.status(''); h.search('RIVER LEVEL'); h.advance(250); assert.equal(h.count(), '1–20 of 23');
  h.click('Sort by observation date, oldest first'); assert.equal(h.rows()[0].key, 'station-00');
  h.size(10); h.click('Next'); assert.equal(h.count(), '11–20 of 23'); h.size(50); assert.equal(h.count(), '1–23 of 23');
  h.change({ family: 'rainfall' }); assert.equal(h.count(), '0–0 of 0'); h.search(''); h.advance(250); assert.equal(h.count(), '1–22 of 22');
  h.change({ family: '' }); h.search('station-03'); h.advance(250); assert.equal(h.rows().length, 1); assert.equal(h.rows()[0].key, 'station-03');
  h.search('critical'); h.advance(250); assert.equal(h.rows().length, 15);
});

test('polls preserve filters and pages, clamp reduced results, retain data on error and ignore stale responses', async t => {
  const h = harness(t); h.requests[0].resolve({ sensors: readings }); await h.settle();
  h.search('kennedy'); h.advance(250); h.click('Next'); h.poll(); h.poll(); assert.equal(h.requests.length, 2);
  h.requests[1].resolve({ sensors: readings }); await h.settle(); assert.equal(h.count(), '21–40 of 45'); assert.equal(h.find('input').props.value, 'kennedy');
  h.poll(); h.requests[2].reject(new Error('Snapshot unavailable')); await h.settle();
  assert.equal(h.count(), '21–40 of 45'); assert.equal(text(h.find('p', p => p.role === 'alert')), 'Snapshot unavailable');
  h.poll(); h.click('Refresh sensor readings'); assert.equal(h.requests[3].options.signal.aborted, true);
  h.requests[4].resolve({ sensors: readings.slice(0, 5) }); await h.settle(); assert.equal(h.count(), '1–5 of 5');
  h.requests[3].resolve({ sensors: readings }); await h.settle(); assert.equal(h.count(), '1–5 of 5');
  h.poll(); h.requests[5].resolve({}); await h.settle(); assert.equal(h.count(), '0–0 of 0');
  assert.equal(text(h.find('p', p => p.className === 'empty')), 'No readings match these filters.');
});

test('hidden readings do not fetch and abort in-flight requests while preserving table state', async t => {
  const h = harness(t, { active: false }); assert.equal(h.requests.length, 0); assert.equal(h.timers.size, 0);
  h.change({ active: true }); h.requests[0].resolve({ sensors: readings }); await h.settle(); h.click('Next'); h.search('Bogotá'); h.advance(250); h.click('Next');
  h.poll(); h.change({ active: false }); assert.equal(h.requests[1].options.signal.aborted, true); assert.equal(h.timers.size, 0);
  h.requests[1].resolve({ sensors: [] }); await h.settle(); assert.equal(h.count(), '21–40 of 45');
  h.change({ active: true }); assert.equal(h.requests.length, 3); h.requests[2].resolve({ sensors: readings }); await h.settle(); assert.equal(h.count(), '21–40 of 45');
  h.poll(); h.cleanup(); assert.equal(h.requests[3].options.signal.aborted, true); assert.equal(h.timers.size, 0);
});

test('equal observation timestamps keep a stable event ID order in both directions', async t => {
  const h = harness(t); h.requests[0].resolve({ sensors: [{ ...readings[1], observed_at: readings[0].observed_at }, readings[0]] }); await h.settle();
  assert.deepEqual(h.rows().map(row => row.key), ['station-00', 'station-01']);
  h.click('Sort by observation date, oldest first'); assert.deepEqual(h.rows().map(row => row.key), ['station-00', 'station-01']);
});
