import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';

const compiled = ts.transpileModule(readFileSync(new URL('../src/TerritorialSensors.tsx', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const families = ['river_level', 'rainfall', 'temperature', 'soil_moisture', 'wind_speed'];
const saved = { sensor_type: 'river_level', config_version: 1, mode: 'Synthetic', is_simulated: true, interval_minutes: 5, sensor_count: 800, capture_running: false };
const configuration = (overrides = {}) => ({ configs: families.map(sensor_type => ({ ...saved, sensor_type, ...overrides[sensor_type] })), sensor_schedule: { start_at: null, interval_minutes: 5, config_version: 1 }, runtime: 'local_fixture' });
function harness(t) {
  let cursor = 0, dirty = true, effects = [], tree; const slots = [], requests = [], timers = new Set(); const previous = globalThis.window;
  globalThis.window = { setInterval(callback) { timers.add(callback); return callback; }, clearInterval(callback) { timers.delete(callback); } };
  const hooks = {
    useState(initial) { const id = cursor++; if (!(id in slots)) slots[id] = initial; return [slots[id], value => { const next = typeof value === 'function' ? value(slots[id]) : value; dirty ||= !Object.is(slots[id], next); slots[id] = next; }]; },
    useRef(initial) { const id = cursor++; return slots[id] ||= { current: initial }; },
    useEffect(callback, deps) { const id = cursor++, old = slots[id]; if (!old || deps.some((value, index) => value !== old.deps[index])) effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; }); },
  };
  const component = {}; new Function('exports', 'require', compiled)(component, name => name === 'react' ? hooks : name === 'react/jsx-runtime' ? jsxRuntime : name === './CaptureScheduleForm' ? { CaptureScheduleForm() {} } : name === './LoadingIndicator' ? { LoadingIndicator() {} } : name === './SyntheticDataReset' ? { SyntheticDataReset() {}, SyntheticDataResetStatus() {} } : name === './TerritorialSensorReadings' ? { TerritorialSensorReadings() {} } : { territorialEndpoint: '/api/admin/territorial', territorialError: error => error.message, timestamp: value => value || 'Not available' });
  const props = { api: (path, options) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })), timeZone: 'America/Bogota' };
  function render() { for (let n = 0; dirty && n < 20; n++) { cursor = 0; dirty = false; effects = []; tree = component.TerritorialSensors(props); effects.forEach(effect => effect()); } assert.equal(dirty, false); }
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const find = (type, match = () => true) => { const value = nodes(tree).find(node => (node?.type === type || node?.type?.name === type) && match(node.props)); assert.ok(value, `Missing ${type}`); return value; };
  const act = callback => { callback(); render(); };
  t.after(() => { slots.forEach(slot => slot?.cleanup?.()); if (previous === undefined) delete globalThis.window; else globalThis.window = previous; });
  render();
  return { requests, find, act, nodes: () => nodes(tree), poll: () => act(() => [...timers][0]()), settle: async () => { await new Promise(setImmediate); render(); },
    select: family => act(() => find('button', p => p.id === `territorial-sensor-tab-${family}`).props.onClick()),
    status: name => find('div', p => p.children?.[0]?.type === 'dt' && p.children[0].props.children === name).props.children[1].props.children,
    click: name => act(() => { const button = find('button', props => props.children === name); assert.ok(!button.props.disabled); button.props.onClick(); }),
    submit: () => act(() => find('form').props.onSubmit({ preventDefault() {} })) };
}

test('sensor administration preserves drafts and revision, disables duplicate actions and reflects only server capture state', async t => {
  const h = harness(t); h.requests[0].resolve(configuration()); await h.settle();
  h.act(() => h.find('input', p => p.max === '5000').props.onChange({ target: { value: '700' } }));
  assert.equal(h.find('button', p => p.children === 'Run now').props.disabled, true);
  h.poll(); h.requests[1].resolve(configuration({ river_level: { config_version: 2, sensor_count: 600, capture_running: true } })); await h.settle();
  assert.equal(h.find('input', p => p.max === '5000').props.value, 700);
  h.submit(); h.submit(); assert.equal(h.requests.length, 3);
  assert.equal(h.requests[2].path, '/api/admin/territorial/sensors/river_level');
  assert.deepEqual(JSON.parse(h.requests[2].options.body), { expected_revision: 1, sensor_count: 700 });
  h.requests[2].reject(new Error('Configuration changed; refresh')); await h.settle(); assert.equal(h.find('p', p => p.role === 'alert').props.children, 'Configuration changed; refresh');
  h.click('Discard changes'); assert.equal(h.find('input', p => p.max === '5000').props.value, 600);
  h.click('Pause'); assert.equal(h.requests[3].path, '/api/admin/territorial/sensors/river_level/pause'); h.requests[3].resolve({ config: { ...saved, config_version: 2, sensor_count: 600 }, message: 'Paused' }); await h.settle();
  h.click('Run now'); assert.equal(h.requests[4].path, '/api/admin/territorial/sensors/river_level/run'); assert.equal(h.requests[4].options.method, 'POST'); h.requests[4].resolve({ config: { ...saved, config_version: 2, capture_running: true }, message: 'Started' }); await h.settle();
  assert.equal(h.find('p', p => p.role === 'status' && p.className === 'territorial-success').props.children, 'Started');
});

test('individual sensor configurations reject invalid quantities before a mutation and do not expose family checkboxes', async t => {
  const h = harness(t); h.requests[0].resolve(configuration()); await h.settle();
  assert.ok(!h.nodes().some(node => node.type === 'input' && node.props.type === 'checkbox'));
  assert.equal(h.find('input', p => p.max === '5000').props.min, '1');
  assert.equal(h.find('CaptureScheduleForm').props.kind, 'sensor');
  assert.equal(h.find('select').props.value, 'Synthetic');
  assert.equal(h.find('option', p => p.value === 'real').props.disabled, true);
  assert.ok(!h.nodes().some(node => node.type === 'input' && node.props.max === '60'));
  for (const [maximum, valid, invalid] of [['5000', '800', ['0', '1.5', '5001']]]) {
    for (const value of invalid) {
      h.act(() => h.find('input', p => p.max === maximum).props.onChange({ target: { value } })); h.submit(); await h.settle();
      assert.equal(h.requests.length, 1); assert.match(h.find('p', p => p.role === 'alert').props.children, /whole numbers/);
    }
    h.act(() => h.find('input', p => p.max === maximum).props.onChange({ target: { value: valid } }));
  }
});

test('sensor type tabs filter their panel and preserve configuration drafts while collapsed', async t => {
  const h = harness(t); h.requests[0].resolve(configuration()); await h.settle();
  const selected = () => h.find('button', p => p.role === 'tab' && p['aria-selected']);
  const hidden = () => h.find('div', p => p.id === 'territorial-sensor-configuration').props.hidden;
  let focused;
  for (const family of families) h.find('button', p => p.id === `territorial-sensor-tab-${family}`).props.ref({ focus() { focused = family; } });
  const press = key => { let prevented = false; h.act(() => selected().props.onKeyDown({ key, preventDefault() { prevented = true; } })); assert.equal(prevented, true); };
  h.act(() => h.find('input', p => p.max === '5000').props.onChange({ target: { value: '700' } }));
  assert.equal(selected().props.id, 'territorial-sensor-tab-river_level'); assert.equal(selected().props['aria-expanded'], true);
  h.select('river_level'); assert.equal(hidden(), true);
  assert.equal(selected().props.id, 'territorial-sensor-tab-river_level'); assert.equal(selected().props.tabIndex, 0); assert.equal(selected().props['aria-expanded'], false);
  assert.equal(selected().props['aria-controls'], 'territorial-sensor-readings-panel');
  assert.ok(!h.find('section', p => p.role === 'tabpanel').props.hidden); assert.equal(h.find('TerritorialSensorReadings').props.family, 'river_level');
  assert.equal(h.find('button', p => p['aria-label'] === 'Expand sensor configuration').props['aria-expanded'], false);
  h.poll(); h.requests[1].resolve(configuration()); await h.settle(); assert.equal(hidden(), true);
  assert.equal(h.find('input', p => p.max === '5000').props.value, 700);
  h.select('river_level'); assert.equal(hidden(), false); assert.equal(selected().props['aria-expanded'], true);
  h.act(() => h.find('button', p => p['aria-label'] === 'Collapse sensor configuration').props.onClick());
  assert.equal(hidden(), true); assert.equal(selected().props['aria-expanded'], false);
  h.select('rainfall'); assert.equal(hidden(), false); assert.equal(selected().props['aria-expanded'], true);
  assert.equal(h.find('button', p => p.id === 'territorial-sensor-tab-river_level').props['aria-expanded'], false);
  assert.equal(h.find('section', p => p.role === 'tabpanel').props['aria-labelledby'], 'territorial-sensor-tab-rainfall');
  assert.equal(h.find('TerritorialSensorReadings').props.family, 'rainfall');
  assert.equal(h.find('input', p => p.max === '5000').props.value, 800);
  h.act(() => h.find('input', p => p.max === '5000').props.onChange({ target: { value: '600' } }));
  assert.equal(h.find('h3').props.children[1], 'Rainfall');
  assert.equal(h.find('h3').props.children[0].props.children.props.d, h.find('button', p => p.id === 'territorial-sensor-tab-rainfall').props.children[0].props.children.props.d);
  h.select('rainfall'); assert.equal(hidden(), true);
  press('End'); assert.equal(focused, 'wind_speed'); assert.equal(hidden(), false); assert.equal(selected().props['aria-expanded'], true);
  assert.equal(selected().props.id, 'territorial-sensor-tab-wind_speed');
  h.act(() => h.find('button', p => p['aria-label'] === 'Collapse sensor configuration').props.onClick());
  assert.equal(hidden(), true); assert.equal(selected().props['aria-expanded'], false);
  h.act(() => h.find('button', p => p['aria-label'] === 'Expand sensor configuration').props.onClick());
  assert.equal(hidden(), false); assert.equal(selected().props['aria-expanded'], true);
  assert.equal(h.find('input', p => p.max === '5000').props.value, 800);
  h.select('wind_speed'); press('Home'); assert.equal(focused, 'river_level'); assert.equal(hidden(), false);
  h.select('river_level'); press('Home'); assert.equal(hidden(), false, 'Home opens the already selected first tab');
  assert.equal(h.find('input', p => p.max === '5000').props.value, 700);
  for (const [key, family, count] of [['ArrowRight', 'rainfall', 600], ['ArrowLeft', 'river_level', 700], ['ArrowDown', 'rainfall', 600], ['ArrowUp', 'river_level', 700]]) {
    h.select(focused); assert.equal(hidden(), true); press(key);
    assert.equal(focused, family); assert.equal(selected().props.id, `territorial-sensor-tab-${family}`); assert.equal(selected().props['aria-expanded'], true); assert.equal(hidden(), false);
    assert.equal(h.find('input', p => p.max === '5000').props.value, count);
  }
  assert.equal(h.requests.length, 2, 'Tab and disclosure changes cause no additional API calls');
});

test('a poll that started before Run cannot restore its old Paused state after the action', async t => {
  const h = harness(t); h.requests[0].resolve(configuration()); await h.settle();
  h.poll(); h.click('Run now'); h.requests[2].resolve({ config: { ...saved, capture_running: true }, message: 'Started' }); await h.settle();
  h.requests[1].resolve(configuration({ rainfall: { capture_running: true } })); await h.settle();
  assert.equal(h.find('button', props => props.children === 'Pause').props.disabled, false);
  assert.equal(h.find('button', p => p.id === 'territorial-sensor-tab-rainfall').props.children.at(-1).props['aria-label'], 'Running');
});

test('a successful status poll preserves a failed capture action until the next action', async t => {
  const h = harness(t); h.requests[0].resolve(configuration()); await h.settle();
  h.click('Run now'); h.requests[1].reject(new Error('Install the Sensors workflow before starting capture')); await h.settle();
  h.poll(); h.requests[2].resolve(configuration()); await h.settle();
  assert.equal(h.find('p', props => props.role === 'alert').props.children, 'Install the Sensors workflow before starting capture');
  h.click('Run now'); h.requests[3].resolve({ config: { ...saved, capture_running: true }, message: 'Started' }); await h.settle();
  assert.equal(h.find('button', props => props.children === 'Pause').props.disabled, false);
});

test('own Pause preserves the dirty tab draft and advances its Save revision without touching a sibling', async t => {
  const h = harness(t); h.requests[0].resolve(configuration({ river_level: { capture_running: true } })); await h.settle();
  h.act(() => h.find('input', p => p.max === '5000').props.onChange({ target: { value: '700' } }));
  h.click('Pause'); h.select('rainfall');
  h.act(() => h.find('input', p => p.max === '5000').props.onChange({ target: { value: '600' } }));
  h.requests[1].resolve({ config: { ...saved, config_version: 2 }, message: 'Paused' }); await h.settle();
  assert.equal(h.find('input', p => p.max === '5000').props.value, 600);
  h.select('river_level'); assert.equal(h.find('input', p => p.max === '5000').props.value, 700);
  assert.equal(h.find('button', p => p.children === 'Pause').props.disabled, true);
  h.submit(); assert.deepEqual(JSON.parse(h.requests[2].options.body), { expected_revision: 2, sensor_count: 700 });
  h.requests[2].resolve({ config: { ...saved, config_version: 3, sensor_count: 700 } }); await h.settle();
  assert.equal(h.find('button', p => p.children === 'Run now').props.disabled, false);
});

test('Pause cannot rebase a draft across a concurrent configuration change', async t => {
  const h = harness(t); h.requests[0].resolve(configuration({ river_level: { capture_running: true } })); await h.settle();
  h.act(() => h.find('input', p => p.max === '5000').props.onChange({ target: { value: '700' } })); h.click('Pause');
  h.requests[1].resolve({ config: { ...saved, config_version: 3, sensor_count: 600 }, message: 'Paused' }); await h.settle();
  assert.equal(h.find('input', p => p.max === '5000').props.value, 700);
  assert.ok(h.nodes().some(node => node.type === 'p' && /Configuration changed elsewhere/.test(node.props.children)));
  h.submit(); assert.equal(JSON.parse(h.requests[2].options.body).expected_revision, 1);
  h.requests[2].reject(new Error('Configuration changed; refresh')); await h.settle();
  assert.equal(h.find('p', p => p.role === 'alert').props.children, 'Configuration changed; refresh');
});

test('parallel capture results and errors belong to their original sensor tabs', async t => {
  const h = harness(t); h.requests[0].resolve(configuration()); await h.settle();
  h.click('Run now'); h.select('rainfall'); h.click('Run now');
  assert.equal(h.requests[1].path, '/api/admin/territorial/sensors/river_level/run'); assert.equal(h.requests[2].path, '/api/admin/territorial/sensors/rainfall/run');
  h.requests[1].reject(new Error('River workflow unavailable')); await h.settle();
  assert.ok(!h.nodes().some(node => node.props?.role === 'alert'));
  h.requests[2].resolve({ config: { ...saved, sensor_type: 'rainfall', capture_running: true }, message: 'Rainfall started' }); await h.settle();
  assert.equal(h.find('button', p => p.children === 'Pause').props.disabled, false);
  assert.equal(h.find('p', p => p.className === 'territorial-success').props.children, 'Rainfall started');
  h.select('river_level'); assert.equal(h.find('button', p => p.children === 'Pause').props.disabled, true);
  assert.equal(h.find('p', p => p.role === 'alert').props.children, 'River workflow unavailable');
});

test('a Save resolving after a tab switch cannot replace a sibling draft or capture state', async t => {
  const h = harness(t); h.requests[0].resolve(configuration()); await h.settle();
  h.act(() => h.find('input', p => p.max === '5000').props.onChange({ target: { value: '700' } })); h.submit();
  h.select('rainfall'); h.act(() => h.find('input', p => p.max === '5000').props.onChange({ target: { value: '600' } }));
  h.requests[1].resolve({ config: { ...saved, config_version: 2, sensor_count: 700 } }); await h.settle();
  assert.equal(h.find('input', p => p.max === '5000').props.value, 600);
  assert.ok(!h.nodes().some(node => node.props?.className === 'territorial-success'));
  h.select('river_level'); assert.equal(h.find('input', p => p.max === '5000').props.value, 700); assert.equal(h.find('button', p => p.children === 'Run now').props.disabled, false);
  h.click('Run now'); h.requests[2].resolve({ config: { ...saved, sensor_type: 'rainfall', capture_running: true } }); await h.settle();
  assert.match(h.find('p', p => p.role === 'alert').props.children, /different sensor type/);
  assert.equal(h.find('button', p => p.children === 'Pause').props.disabled, true);
});

test('each sensor tab uses the Social Networks capture labels and independent timing and counts', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: Date.parse('2026-10-04T12:00:07Z') });
  const h = harness(t); h.requests[0].resolve(configuration({ river_level: { capture_running: true, last_run_at: '2026-10-04T11:55:00Z', next_due: '2026-10-04T12:00:00Z', last_received_count: 750 }, rainfall: { last_received_count: 650 } })); await h.settle();
  assert.equal(h.status('Last capture'), '2026-10-04T11:55:00Z'); assert.equal(h.status('Next capture'), '2026-10-04T12:00:00Z');
  assert.equal(h.status('Records received · last successful capture'), 750); assert.equal(h.status('Capture delay'), '7 s');
  assert.equal(h.find('span', p => p.role === 'status').props.className, 'territorial-mode territorial-capture-state running');
  h.select('rainfall'); assert.equal(h.status('Last capture'), 'Not available'); assert.equal(h.status('Next capture'), 'Not scheduled');
  assert.equal(h.status('Records received · last successful capture'), 650); assert.equal(h.status('Capture delay'), 'Stopped');
  assert.equal(h.find('span', p => p.role === 'status').props.className, 'territorial-mode territorial-capture-state paused');
});

test('a delayed sensor cleanup keeps its original tab and preserves sibling drafts and controls', async t => {
  const h = harness(t); h.requests[0].resolve(configuration({ river_level: { capture_running: true } })); await h.settle();
  const reset = h.find('SyntheticDataReset', p => p.sensor.type === 'river_level');
  const wrappers = () => h.nodes().filter(node => node.type === 'span' && node.props.children?.type?.name === 'SyntheticDataReset');
  assert.equal(wrappers().length, 5); assert.deepEqual(wrappers().filter(node => !node.props.hidden).map(node => node.key), ['river_level']);
  h.act(() => reset.props.onChange({ operation_id: 'river-reset', sensor_type: 'river_level', status: 'pending' }, ''));
  assert.equal(h.find('CaptureScheduleForm').props.disabled, true);
  assert.equal(h.find('fieldset').props.disabled, true); h.submit(); assert.equal(h.requests.length, 1);
  h.select('rainfall'); assert.equal(h.find('fieldset').props.disabled, false);
  assert.equal(h.find('CaptureScheduleForm').props.disabled, true, 'Shared schedule cannot change while another family reset is pending');
  assert.deepEqual(wrappers().filter(node => !node.props.hidden).map(node => node.key), ['rainfall']);
  assert.ok(wrappers().some(node => node.key === 'river_level' && node.props.hidden), 'The pending type remains mounted while hidden');
  h.act(() => h.find('input', p => p.max === '5000').props.onChange({ target: { value: '600' } }));
  h.act(() => reset.props.onChange({ operation_id: 'river-reset', sensor_type: 'river_level', status: 'error' }, 'River cleanup failed'));
  assert.equal(h.find('fieldset').props.disabled, false, 'A sibling cleanup error cannot disable this draft');
  h.select('river_level'); assert.equal(h.find('fieldset').props.disabled, true);
  h.select('rainfall');
  h.act(() => { reset.props.onChange({ operation_id: 'river-reset', sensor_type: 'river_level', status: 'completed' }, ''); reset.props.onComplete(); });
  assert.equal(h.requests[1].path, '/api/admin/territorial/sensors');
  h.requests[1].resolve(configuration({ river_level: { config_version: 2 } })); await h.settle();
  assert.equal(h.find('CaptureScheduleForm').props.disabled, false);
  assert.equal(h.find('input', p => p.max === '5000').props.value, 600);
  assert.equal(h.find('TerritorialSensorReadings').key, '1');
  h.select('river_level'); assert.equal(h.find('fieldset').props.disabled, false);
  assert.equal(h.find('button', p => p.children === 'Pause').props.disabled, true);
});
