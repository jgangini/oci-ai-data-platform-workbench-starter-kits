import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';

const source = readFileSync(new URL('../src/territorialAdminState.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const exports = {};
new Function('exports', compiled)(exports);
const { captureState, postStatus, refreshedEditor, refreshedPosts, safeMediaUrl, scheduleLocalTime, schedulePayload, sourceDefaults, sourceDirty, sourcePayload, timestamp } = exports;
const saved = sourceDefaults({ platform: 'x', enabled: true, mode: 'simulation', query: '#bogota', interval_minutes: 5,
  secret_ref: 'TerritorialSource_x', credential_configured: false, status: 'paused', capture_running: false, config_version: 7 });

test('polling preserves unsaved searches, credentials and the optimistic revision', () => {
  for (const edit of [{ draft: { ...saved, query: '#kennedy' } }, { draft: { ...saved, mode: 'real' }, token: 'new-secret' },
    { draft: { ...saved, report_thresholds: { low: 7, medium: 12, high: 25 } } }]) {
    const editor = { saved, draft: saved, token: '', ...edit };
    const fresh = { ...saved, config_version: 8, query: '#other', capture_running: true };
    assert.equal(sourceDirty(editor), true);
    assert.equal(refreshedEditor(editor, fresh), editor);
    assert.equal(editor.saved.config_version, 7);
  }
});

test('Synthetic saves normalize legacy mode, omit hidden tokens and preserve revision checks', () => {
  const editor = { saved, draft: saved, token: 'hidden-token' };
  assert.equal(saved.mode, 'Synthetic');
  assert.equal(sourcePayload({ ...editor, draft: { ...saved, mode: 'simulation' } }).mode, 'Synthetic');
  assert.equal(sourceDirty(editor), false);
  assert.equal('bearer_token' in sourcePayload(editor), false);
  assert.equal(sourcePayload(editor).synthetic_batch_max, 3);
  assert.equal('interval_minutes' in sourcePayload(editor), false);
  assert.equal(sourceDirty({ ...editor, draft: { ...saved, interval_minutes: 10 } }), false);
  assert.equal(sourceDirty({ ...editor, draft: { ...saved, synthetic_batch_max: 4 } }), true);
  const real = sourcePayload({ ...editor, draft: { ...saved, mode: 'real' } });
  assert.equal(real.bearer_token, 'hidden-token'); assert.equal(real.expected_revision, 7);
  assert.equal('synthetic_batch_max' in real, false);
  for (const value of [0, 1.5, 101, NaN]) assert.throws(() => sourcePayload({ ...editor, draft: { ...saved, synthetic_batch_max: value } }), /whole number/);
  assert.throws(() => sourcePayload({ ...editor, draft: { ...saved, report_thresholds: { low: 8, medium: 7, high: 20 } } }), /must be positive and increase/);
});

test('shared schedule converts browser wall time to UTC and rejects missing, invalid and skipped local times', t => {
  const previous = process.env.TZ; process.env.TZ = 'America/New_York';
  t.after(() => { if (previous === undefined) delete process.env.TZ; else process.env.TZ = previous; });
  assert.equal(scheduleLocalTime(null), '');
  assert.equal(scheduleLocalTime('2026-10-05T15:30:12Z'), '2026-10-05T11:30:12');
  assert.deepEqual(schedulePayload('2026-10-05T11:30:12', 7, 2), { start_at: '2026-10-05T15:30:12.000Z', interval_minutes: 7, expected_revision: 2 });
  assert.equal(schedulePayload('2026-01-05T11:30', 1, 1).start_at, '2026-01-05T16:30:00.000Z');
  for (const value of ['', 'invalid', '2026-02-30T10:00', '2026-03-08T02:30']) assert.throws(() => schedulePayload(value, 5, 1), /valid scheduled/);
  for (const value of [0, 1.5, 1441, NaN]) assert.throws(() => schedulePayload('2026-10-05T11:30', value, 1), /whole number/);
});

function sourceFormHarness(t, name, initial) {
  let cursor = 0, dirty = true, effects = [], tree; const slots = [], requests = [], updated = [];
  const hooks = {
    useState(initial) { const id = cursor++; if (!(id in slots)) slots[id] = typeof initial === 'function' ? initial() : initial;
      return [slots[id], value => { const next = typeof value === 'function' ? value(slots[id]) : value; dirty ||= !Object.is(slots[id], next); slots[id] = next; }]; },
    useRef(initial) { const id = cursor++; return slots[id] ||= { current: initial }; },
    useEffect(callback, deps) { const id = cursor++, old = slots[id]; if (!old || deps.some((value, index) => value !== old.deps[index])) effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; }); },
  };
  const filename = name === 'SourceCard' ? 'TerritorialAdmin' : name;
  const source = readFileSync(new URL(`../src/${filename}.tsx`, import.meta.url), 'utf8') + (name === 'SourceCard' ? '\nexport { SourceCard };' : '');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const component = {}; new Function('exports', 'require', compiled)(component, dependency => dependency === 'react' ? hooks : dependency === 'react/jsx-runtime' ? jsxRuntime : dependency === './territorialAdminState' ? exports : {});
  const props = { ...initial, api: (path, options) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })), onUpdate: value => updated.push(value), disabled: false };
  function render() { for (let n = 0; dirty && n < 20; n++) { cursor = 0; dirty = false; effects = []; tree = component[name](props); effects.forEach(effect => effect()); } assert.equal(dirty, false); }
  const nodes = value => [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])];
  const find = (type, match = () => true) => { const value = nodes(tree).find(node => node?.type === type && match(node.props)); assert.ok(value, `Missing ${type}`); return value; };
  const act = callback => { callback(); render(); };
  const cleanup = () => slots.forEach(slot => slot?.cleanup?.()); t.after(cleanup); render();
  return { requests, updated, find, act, cleanup, nodes: () => nodes(tree),
    receive: value => act(() => { Object.assign(props, value); dirty = true; }),
    change: (type, value) => act(() => find('input', p => p.type === type).props.onChange({ target: { value } })),
    submit: () => act(() => find('form').props.onSubmit({ preventDefault() {} })),
    settle: async () => { await new Promise(setImmediate); render(); } };
}

test('viewer identity loads independently, saves once, resets empty fields and preserves failed drafts', async t => {
  const h = sourceFormHarness(t, 'ViewerIdentitySettings', {});
  assert.equal(h.requests.length, 1); assert.equal(h.requests[0].path, '/api/admin/territorial/identity');
  h.submit(); assert.equal(h.requests.length, 1, 'Cannot save defaults before loading');
  h.requests[0].resolve({ name: "God's Eye View", description: 'NO PLACE LEFT BEHIND' }); await h.settle();
  assert.equal(h.find('input', p => p.maxLength === 80).props.value, "God's Eye View");
  h.act(() => h.find('input', p => p.maxLength === 80).props.onChange({ target: { value: 'Community <map>' } }));
  h.act(() => h.find('input', p => p.maxLength === 200).props.onChange({ target: { value: '' } }));
  h.submit(); h.submit(); assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].options.method, 'PUT');
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { name: 'Community <map>', description: '' });
  h.requests[1].reject(new Error('Saving unavailable')); await h.settle();
  assert.equal(h.find('input', p => p.maxLength === 80).props.value, 'Community <map>');
  assert.match(h.find('p', p => p.role === 'alert').props.children, /Saving unavailable/);
  h.submit(); h.requests[2].resolve({ name: 'Community <map>', description: 'NO PLACE LEFT BEHIND' }); await h.settle();
  assert.equal(h.find('input', p => p.maxLength === 200).props.value, 'NO PLACE LEFT BEHIND');
  assert.match(h.find('p', p => p.role === 'status').props.children, /Reload the viewer/);
  h.submit(); h.cleanup(); assert.equal(h.requests[3].options.signal.aborted, true);
  h.requests[3].resolve({ name: 'Ignored after unmount', description: '' }); await h.settle();
  assert.equal(h.find('input', p => p.maxLength === 80).props.value, 'Community <map>');
});

test('viewer identity load errors permit retry without enabling a destructive save', async t => {
  const h = sourceFormHarness(t, 'ViewerIdentitySettings', {});
  h.requests[0].reject(new Error('Configuration unavailable')); await h.settle();
  assert.equal(h.find('fieldset').props.disabled, true); h.submit(); assert.equal(h.requests.length, 1);
  h.act(() => h.find('button', p => p.children === 'Retry').props.onClick());
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve({ name: 'Saved', description: 'Persisted' }); await h.settle();
  assert.equal(h.find('fieldset').props.disabled, false);
  assert.equal(h.find('input', p => p.maxLength === 80).props.value, 'Saved');
});

test('shared schedule saves once without starting capture and preserves dirty drafts and revisions across polling conflicts', async t => {
  const initial = { start_at: null, interval_minutes: 5, config_version: 1 };
  const h = sourceFormHarness(t, 'CaptureScheduleForm', { schedule: initial, kind: 'social' });
  assert.equal(h.requests.length, 0); assert.equal(h.find('input', p => p.type === 'datetime-local').props.value, '');
  h.submit(); await h.settle(); assert.equal(h.requests.length, 0); assert.match(h.find('p', p => p.role === 'alert').props.children, /valid scheduled/);
  h.change('datetime-local', '2026-10-06T11:30:00'); h.change('number', '7');
  const fresh = { start_at: '2026-10-07T16:00:00Z', interval_minutes: 9, config_version: 2 };
  h.receive({ schedule: fresh }); assert.equal(h.find('input', p => p.type === 'number').props.value, 7);
  h.submit(); h.submit(); assert.equal(h.requests.length, 1); assert.equal(h.requests[0].path, '/api/admin/territorial/social-schedule');
  assert.equal(h.requests[0].options.method, 'PUT');
  assert.deepEqual(JSON.parse(h.requests[0].options.body), schedulePayload('2026-10-06T11:30:00', 7, 1));
  h.requests[0].reject(Object.assign(new Error('Schedule conflict'), { status: 409 })); await h.settle();
  assert.equal(h.find('input', p => p.type === 'number').props.value, 7); assert.equal(h.updated.length, 0);
  h.act(() => h.find('button', p => p.children === 'Discard schedule changes').props.onClick());
  assert.equal(h.find('input', p => p.type === 'datetime-local').props.value, scheduleLocalTime(fresh.start_at));
  h.change('number', '10'); h.submit(); assert.equal(JSON.parse(h.requests[1].options.body).expected_revision, 2);
  const saved = { ...fresh, interval_minutes: 10, config_version: 3 }; h.requests[1].resolve(saved); await h.settle();
  assert.deepEqual(h.updated, [saved]); h.receive({ schedule: initial }); assert.equal(h.find('input', p => p.type === 'number').props.value, 10);
  h.receive({ kind: 'sensor', schedule: saved }); h.change('number', '11'); h.submit();
  assert.equal(h.requests[2].path, '/api/admin/territorial/sensor-schedule');
  h.cleanup(); assert.equal(h.requests[2].options.signal.aborted, true); h.requests[2].resolve({ ...saved, config_version: 4 }); await h.settle();
  assert.equal(h.updated.length, 1, 'Unmounted saves cannot update parent state');
});

test('both shared schedules place their submit beside the interval with controls disabled together', t => {
  for (const kind of ['social', 'sensor']) {
    const h = sourceFormHarness(t, 'CaptureScheduleForm', { schedule: { start_at: null, interval_minutes: 5, config_version: 1 }, kind });
    const row = h.find('div', p => p.className === 'territorial-fields territorial-schedule-fields').props.children;
    assert.deepEqual(row.map(node => node.type), ['label', 'label', 'button']);
    assert.equal(row[0].props.children.at(-1).props.type, 'datetime-local');
    assert.equal(row[1].props.children.at(-1).props.type, 'number');
    assert.equal(row[2].props.type, 'submit'); assert.equal(row[2].props.children, 'Save schedule');
    h.receive({ disabled: true }); assert.equal(h.find('fieldset').props.disabled, true);
    h.submit(); assert.equal(h.requests.length, 0);
  }
});

test('individual source limits apply only to Synthetic mode and never write the shared interval', async t => {
  const h = sourceFormHarness(t, 'SourceCard', { source: saved, timeZone: 'UTC' });
  assert.equal(h.find('input', p => p.max === '100').props.value, 3);
  assert.equal(h.nodes().some(node => node?.type === 'input' && node.props.max === '1440'), true, 'Correlation window remains per source');
  assert.equal(h.nodes().filter(node => node?.type === 'input' && node.props.max === '1440').length, 1);
  h.act(() => h.find('input', p => p.max === '100').props.onChange({ target: { value: '8' } })); h.submit();
  assert.equal(JSON.parse(h.requests[0].options.body).synthetic_batch_max, 8);
  assert.equal('interval_minutes' in JSON.parse(h.requests[0].options.body), false);
  h.requests[0].resolve({ ...saved, synthetic_batch_max: 8, config_version: 8 }); await h.settle();
  h.act(() => h.find('select').props.onChange({ target: { value: 'real' } }));
  assert.equal(h.nodes().some(node => node?.type === 'input' && node.props.max === '100'), false);
  h.submit(); assert.equal('synthetic_batch_max' in JSON.parse(h.requests[1].options.body), false);
  h.requests[1].resolve({ ...saved, mode: 'real', config_version: 9 }); await h.settle();
});

test('searches accept 1000 characters and reject excess without truncating saved drafts', () => {
  const query = 'a'.repeat(500) + '\n' + 'b'.repeat(499);
  const editor = { saved, draft: { ...saved, query }, token: '' };
  assert.equal(sourcePayload(editor).query, query);
  for (const excess of [query + 'x', '🌧'.repeat(501)]) {
    const draft = { ...editor, draft: { ...saved, query: excess } };
    assert.throws(() => sourcePayload(draft), /at most 1000 characters in total/);
    assert.equal(draft.draft.query, excess);
  }
});

test('saving configuration never restores a stale capture switch', () => {
  const disabled = { ...saved, enabled: false };
  const editor = { saved: disabled, draft: disabled, token: '' };
  const running = { ...saved, capture_running: true };
  assert.equal(sourceDirty({ ...editor, draft: { ...disabled, enabled: true } }), false);
  assert.deepEqual(refreshedEditor(editor, running).draft, running);
  for (const draft of [disabled, running]) {
    const payload = sourcePayload({ ...editor, draft });
    assert.equal('enabled' in payload, false);
    assert.equal('capture_running' in payload, false);
  }
});

test('publication display requires confirmed processed state and respects the selected time zone', () => {
  for (const status of ['captured', 'landing_written', 'ingested', 'analyzed', 'published', 'error', '']) assert.equal(postStatus(status), 'New');
  assert.equal(postStatus('processed'), 'Processed');
  assert.equal(timestamp('2026-10-04T00:00:00Z'), '03/10/2026, 19:00:00');
  assert.equal(timestamp('2026-10-04T00:00:00Z', 'UTC'), '04/10/2026, 00:00:00');
  assert.equal(timestamp('2026-03-29T00:30:00Z', 'Europe/Madrid'), '29/03/2026, 01:30:00');
  assert.equal(timestamp('2026-03-29T01:30:00Z', 'Europe/Madrid'), '29/03/2026, 03:30:00');
  assert.equal(timestamp(undefined, 'UTC'), 'Not available');
});

test('clean editors refresh, while late responses cannot undo a completed save', () => {
  const editor = { saved, draft: saved, token: '' };
  const fresh = { ...saved, config_version: 8, capture_running: true };
  assert.deepEqual(refreshedEditor(editor, fresh), { saved: fresh, draft: fresh, token: '' });
  assert.equal(refreshedEditor(editor, { ...saved, config_version: 6 }), editor);
  assert.equal(sourceDirty({ ...editor, draft: { ...saved, report_thresholds: { high: 20, low: 5, medium: 10 } } }), false);
  assert.equal(saved.correlation_window_minutes, 30);
});

test('capture indication has only Running and Paused while errors remain separate', () => {
  assert.equal(captureState(saved), 'Paused');
  assert.equal(captureState({ ...saved, capture_running: true }), 'Running');
  assert.equal(captureState({ ...saved, capture_running: true, capture_state: 'capturing' }), 'Running');
  assert.equal(captureState({ ...saved, capture_running: true, capture_state: 'scheduled' }), 'Running');
  assert.equal(captureState({ ...saved, capture_running: true, capture_state: 'running', next_due: '2099-01-01T00:00:00Z' }), 'Running');
  assert.equal(captureState({ ...saved, capture_running: true, capture_state: 'error', last_error: 'rate_limited' }), 'Running');
  assert.equal(captureState({ ...saved, capture_state: 'error', last_error: 'credential_required' }), 'Paused');
  assert.equal(captureState({ ...saved, enabled: false, capture_running: true }), 'Paused');
});

test('network tabs toggle configuration, retain mounted editors and follow capture state while collapsed', async t => {
  const slots = [], requests = [], timers = new Map(), listeners = new Map();
  let cursor = 0, dirty = true, effects = [], tree;
  const previousWindow = globalThis.window;
  globalThis.window = { location: { hash: '' }, addEventListener(name, callback) { listeners.set(name, callback); }, removeEventListener(name) { listeners.delete(name); }, setInterval(callback) { timers.set(callback, callback); return callback; }, clearInterval(id) { timers.delete(id); } };
  const hooks = {
    useState(initial) {
      const id = cursor++; if (!(id in slots)) slots[id] = typeof initial === 'function' ? initial() : initial;
      return [slots[id], value => { const next = typeof value === 'function' ? value(slots[id]) : value; dirty ||= !Object.is(next, slots[id]); slots[id] = next; }];
    },
    useRef(initial) { const id = cursor++; return slots[id] ||= { current: initial }; },
    useEffect(callback, deps) {
      const id = cursor++, old = slots[id];
      if (!old || deps.some((value, index) => !Object.is(value, old.deps[index]))) effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; });
    },
  };
  const compiled = ts.transpileModule(readFileSync(new URL('../src/TerritorialAdmin.tsx', import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const component = {};
  new Function('exports', 'require', compiled)(component, name => {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return jsxRuntime;
    if (name === './LoadingIndicator') return { LoadingIndicator() {} };
    if (name === './CaptureScheduleForm') return { CaptureScheduleForm() {} };
    if (name === './territorialAdminState') return exports;
    if (name === './territorial.css') return {};
    return { TerritorialPosts: () => null, SyntheticDataReset: () => null, SyntheticDataResetStatus: () => null };
  });
  const props = { api: () => new Promise(resolve => requests.push(resolve)), viewerUrlControl: null, searchIcon: null, refreshIcon: null };
  function render() {
    for (let turns = 0; dirty && turns < 20; turns++) {
      dirty = false; cursor = 0; effects = []; tree = component.TerritorialAdmin(props);
      for (const effect of effects) effect();
    }
    assert.equal(dirty, false);
  }
  function nodes(value) { return [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])]; }
  const find = match => { const node = nodes(tree).find(node => node?.props && match(node.props)); assert.ok(node); return node; };
  const dot = platform => find(props => props.id === `territorial-tab-${platform}`).props.children.at(-1).props;
  const sources = ['x', 'facebook', 'instagram', 'tiktok'].map((platform, index) => ({ ...saved, platform, capture_running: index < 3, capture_state: ['running', 'scheduled', 'capturing', 'paused'][index] }));
  t.after(() => { for (const slot of slots) slot?.cleanup?.(); if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow; });
  render();
  assert.ok(!nodes(tree).some(node => node?.type?.name === 'LoadingIndicator'));
  const posts = nodes(tree).find(node => node?.type?.name === 'TerritorialPosts');
  assert.ok(posts, 'Publications load independently while source configuration is pending');
  requests[0]({ sources, runtime: 'local_fixture', social_schedule: { start_at: null, interval_minutes: 5, config_version: 1 } }); await new Promise(setImmediate); render();
  assert.equal(nodes(tree).filter(node => node?.type?.name === 'CaptureScheduleForm').length, 1);
  assert.equal(find(props => props.kind === 'social').props.schedule.start_at, null);
  assert.ok(!nodes(tree).some(node => node?.type?.name === 'LoadingIndicator'));
  assert.equal(nodes(tree).find(node => node?.type?.name === 'TerritorialPosts').key, posts.key, 'Loading configuration does not remount publications');
  for (const [index, platform] of ['x', 'facebook', 'instagram', 'tiktok'].entries()) {
    assert.equal(dot(platform)['aria-label'], index < 3 ? 'Running' : 'Paused');
    assert.equal(dot(platform).className, `territorial-capture-dot ${index < 3 ? 'running' : 'paused'}`);
  }
  const tab = platform => find(props => props.id === `territorial-tab-${platform}`);
  const formHidden = () => find(props => props.id === 'territorial-source-forms').props.hidden;
  assert.equal(tab('x').props['aria-expanded'], true);
  tab('x').props.onClick(); render();
  assert.equal(formHidden(), true);
  assert.ok(nodes(tree).some(node => node?.type?.name === 'CaptureScheduleForm'), 'Shared schedule remains outside collapsed individual forms');
  assert.equal(tab('x').props['aria-selected'], true);
  assert.equal(tab('x').props['aria-expanded'], false);
  assert.equal(tab('x').props.tabIndex, 0);
  assert.equal(nodes(tree).filter(node => node?.props?.role === 'tabpanel').length, 4, 'Editors stay mounted when collapsed');
  tab('x').props.onClick(); render(); assert.equal(formHidden(), false);
  tab('x').props.onClick(); render();
  tab('facebook').props.onClick(); render();
  assert.equal(formHidden(), false); assert.equal(tab('facebook').props['aria-expanded'], true);
  find(props => props['aria-controls'] === 'territorial-source-forms').props.onClick(); render();
  assert.equal(tab('facebook').props['aria-expanded'], false);
  let prevented = false;
  tab('facebook').props.onKeyDown({ key: 'Home', preventDefault() { prevented = true; } }); render();
  assert.equal(prevented, true); assert.equal(tab('x').props['aria-expanded'], true);
  tab('x').props.onKeyDown({ key: 'Home', preventDefault() {} }); render();
  assert.equal(formHidden(), false, 'Keyboard navigation opens rather than toggling the active tab');
  find(props => props['aria-controls'] === 'territorial-source-forms').props.onClick(); render();
  assert.equal(find(props => props.id === 'territorial-source-forms').props.hidden, true);
  globalThis.window.location.hash = '#parameters'; listeners.get('hashchange')(); render();
  assert.equal(find(props => props.children === 'Parameters').props['aria-pressed'], true);
  assert.equal(timers.size, 1); [...timers.values()][0]();
  requests[1]({ sources: sources.map(source => ({ ...source, capture_running: false, capture_state: 'paused' })), runtime: 'local_fixture' });
  await new Promise(setImmediate); render();
  assert.equal(dot('x').className, 'territorial-capture-dot paused');
  assert.equal(find(props => props.id === 'territorial-source-forms').props.hidden, true);
});

test('incremental polling updates processing without shifting rows or pagination', () => {
  const page = { items: [{ id: 'b', processing_status: 'captured' }, { id: 'a', processing_status: 'captured' }],
    next_cursor: 'stable-next', total: 3, version: 'posts-v1-3' };
  const response = { items: [{ id: 'new', processing_status: 'captured' }, { id: 'b', processing_status: 'analyzed' }],
    next_cursor: 'new-next', total: 4, version: 'posts-v1-4' };
  const refreshed = refreshedPosts({ page, changed: false }, response, false);
  assert.deepEqual(refreshed.page.items.map(item => item.id), ['b', 'a']);
  assert.equal(refreshed.page.items[0].processing_status, 'analyzed');
  assert.equal(refreshed.page.next_cursor, 'stable-next');
  assert.equal(refreshed.changed, true);
  assert.deepEqual(refreshedPosts(refreshed, response, true), { page: response, changed: false });
});

test('preview permits authenticated fixture media and HTTPS without executable or ambiguous URLs', () => {
  assert.equal(safeMediaUrl('/api/admin/territorial/media/post-0001/image-01.svg'), '/api/admin/territorial/media/post-0001/image-01.svg');
  assert.equal(safeMediaUrl('https://pbs.twimg.com/media/example.jpg'), 'https://pbs.twimg.com/media/example.jpg');
  for (const value of ['javascript:alert(1)', 'data:image/svg+xml,test', 'http://example.com/video.mp4', '//example.com/photo.png',
    'https://user:password@example.com/a.png', '/api/admin/territorial/media/../secret.svg', '/api/admin/territorial/media/%2e%2e/photo.svg', '/api/admin/users']) {
    assert.equal(safeMediaUrl(value), null, value);
  }
});

test('network panels and preview retain keyboard, provenance and reduced-motion contracts', () => {
  const admin = readFileSync(new URL('../src/TerritorialAdmin.tsx', import.meta.url), 'utf8');
  const posts = readFileSync(new URL('../src/TerritorialPosts.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/territorial.css', import.meta.url), 'utf8');
  assert.match(admin, /role="tablist" aria-label="Social networks"/);
  assert.match(admin, /role="tabpanel"[\s\S]*hidden=\{selected !== platform\}/);
  assert.match(admin, /JSON\.stringify\(sourcePayload\(editor\)\)/);
  assert.match(admin, /action\('pause'\)/);
  assert.match(posts, /showModal\(\)/);
  assert.match(posts, /origin\.focus\(\)/);
  assert.match(posts, /post\.mode === 'real'/);
  assert.match(posts, /controls preload="metadata"/);
  assert.doesNotMatch(posts, /autoPlay|dangerouslySetInnerHTML/);
  assert.match(styles, /prefers-reduced-motion: reduce/);
  assert.equal((admin.match(/<TerritorialPosts\b/g) || []).length, 1);
  assert.match(admin, /aria-controls="territorial-source-forms"/);
  assert.match(admin, /draft\.mode === 'real' && <fieldset className="territorial-credentials">[\s\S]*?<label>Credential reference[\s\S]*?<label>Update token[\s\S]*?<\/fieldset>/);
  assert.match(posts, /new URLSearchParams\(\{ limit: String\(pageSize\)/);
  assert.doesNotMatch(posts, /Publications from|\['Attachments', 'Username', 'Display name'/);
  assert.match(posts, /timestamp\(post\.captured_at, timeZone\)/);
  assert.match(posts, /\[10, 20, 50, 100\]/);
});

// Run the component's real handlers/effects; child previews are outside this pagination check.
const postsCompiled = ts.transpileModule(readFileSync(new URL('../src/TerritorialPosts.tsx', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
}).outputText;

function postsHarness(t) {
  const slots = [], requests = [], timers = new Map(), timeouts = new Map();
  let cursor = 0, dirty = true, effects = [], tree, timerId = 0, now = 0;
  const previousWindow = globalThis.window;
  globalThis.window = {
    setInterval(callback) { timers.set(++timerId, callback); return timerId; }, clearInterval(id) { timers.delete(id); },
    setTimeout(callback, delay) { timeouts.set(++timerId, { callback, due: now + delay }); return timerId; }, clearTimeout(id) { timeouts.delete(id); },
  };
  const hooks = {
    useState(initial) {
      const id = cursor++; if (!(id in slots)) slots[id] = initial;
      return [slots[id], value => { const next = typeof value === 'function' ? value(slots[id]) : value; dirty ||= !Object.is(next, slots[id]); slots[id] = next; }];
    },
    useEffect(callback, deps) {
      const id = cursor++, old = slots[id];
      if (!old || deps.some((value, index) => !Object.is(value, old.deps[index]))) effects.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: callback() }; });
    },
  };
  const component = {};
  new Function('exports', 'require', postsCompiled)(component, name => {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return jsxRuntime;
    if (name === './LoadingIndicator') return { LoadingIndicator() {} };
    assert.equal(name, './territorialAdminState'); return exports;
  });
  const props = { refreshKey: 0, searchIcon: null, refreshIcon: null,
    api: (url, options) => new Promise((resolve, reject) => requests.push({ url, signal: options.signal, resolve, reject })) };
  function render() {
    for (let turns = 0; dirty && turns < 20; turns++) {
      dirty = false; cursor = 0; effects = []; tree = component.TerritorialPosts(props);
      for (const effect of effects) effect();
    }
    assert.equal(dirty, false, 'Component state did not settle');
  }
  function nodes(value) { return [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])]; }
  const find = (type, match = () => true) => { const node = nodes(tree).find(node => node?.type === type && match(node.props)); assert.ok(node, `Missing ${type}`); return node; };
  const act = callback => { callback(); render(); };
  t.after(() => { for (const slot of slots) slot?.cleanup?.(); if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow; });
  render();
  return { requests, find, all: type => nodes(tree).filter(node => node?.type === type), rows: () => (find('tbody').props.children || []).map(row => row.key),
    input: value => act(() => find('input').props.onChange({ target: { value } })),
    submit: () => act(() => find('form').props.onSubmit({ preventDefault() {} })),
    click: name => act(() => { const button = find('button', props => (props['aria-label'] || props.children) === name); assert.ok(!button.props.disabled, `${name} is disabled`); button.props.onClick(); }),
    poll: () => act(() => { assert.equal(timers.size, 1); [...timers.values()][0](); }),
    advance: ms => act(() => { now += ms; for (const [id, timeout] of timeouts) if (timeout.due <= now) { timeouts.delete(id); timeout.callback(); } }),
    network: value => act(() => find('select', props => typeof props.value === 'string').props.onChange({ target: { value } })),
    pageSize: value => act(() => find('select', props => typeof props.value === 'number').props.onChange({ target: { value } })),
    refreshParent: () => act(() => { props.refreshKey++; dirty = true; }),
    settle: async () => { await new Promise(setImmediate); render(); },
  };
}

const postPage = (id, next_cursor = null) => ({ items: [{ id, platform: 'x', username: id, country: 'Colombia', text: id, attachments: [], processing_status: 'captured', published_at: '2026-10-03T12:00:00Z', captured_at: '2026-10-03T15:00:00Z' }],
  next_cursor, total: 3, version: `posts-${id}` });
const postParams = request => Object.fromEntries(new URL(request.url, 'https://example.test').searchParams);
const defaultPostParams = { limit: '20', sort: 'published_at', order: 'desc' };

test('publication loading belongs to its table container and clears on response or error', async t => {
  const view = postsHarness(t);
  const loading = () => view.find('div', props => props.className === 'table-wrap territorial-post-table').props.children.find(node => node?.props?.label === 'Loading publications…');
  assert.ok(loading());
  view.requests[0].reject(new Error('Publications unavailable')); await view.settle();
  assert.equal(view.find('tbody').props.children, undefined);
  assert.equal(loading(), undefined);
  assert.equal(view.find('p', props => props.role === 'alert').props.children[0], 'Publications unavailable');
  view.click('Refresh posts'); assert.ok(loading());
  view.requests[1].resolve(postPage('loaded')); await view.settle();
  assert.deepEqual(view.rows(), ['loaded']);
  assert.equal(loading(), undefined);
});

test('publication search debounces typing, resets paging and retains its query on polling and refresh', async (t) => {
  const view = postsHarness(t);
  assert.equal(view.all('h3').length, 0);
  assert.equal(view.find('section').props['aria-label'], 'Captured publications');
  assert.deepEqual(postParams(view.requests[0]), defaultPostParams);
  view.requests[0].resolve(postPage('initial', 'initial-next')); await view.settle();
  view.click('Next'); assert.deepEqual(view.rows(), []);
  assert.deepEqual(postParams(view.requests[1]), { ...defaultPostParams, cursor: 'initial-next' });
  view.requests[1].resolve(postPage('second')); await view.settle();
  view.input(' Ken'); view.advance(200); view.input('  Kennedy  '); view.advance(249);
  assert.equal(view.requests.length, 2); assert.equal(view.find('button', props => props.children === 'Previous').props.disabled, true);
  view.advance(1); assert.deepEqual(view.rows(), []);
  assert.deepEqual(postParams(view.requests[2]), { ...defaultPostParams, q: 'Kennedy' });
  view.requests[2].resolve(postPage('matched', 'matched-next')); await view.settle();
  view.click('Next');
  assert.deepEqual(postParams(view.requests[3]), { ...defaultPostParams, cursor: 'matched-next', q: 'Kennedy' });
  view.requests[3].resolve(postPage('matched-second')); await view.settle();
  view.poll();
  assert.deepEqual(postParams(view.requests[4]), postParams(view.requests[3]));
  view.requests[4].resolve(postPage('matched-second')); await view.settle();
  view.click('Refresh posts');
  assert.deepEqual(postParams(view.requests[5]), postParams(view.requests[3]));
  view.requests[5].resolve(postPage('matched-second')); await view.settle();
  view.refreshParent(); assert.deepEqual(postParams(view.requests[6]), postParams(view.requests[3]));
  view.requests[6].resolve(postPage('matched-second')); await view.settle();
  view.input('   '); view.advance(250);
  assert.deepEqual(postParams(view.requests[7]), defaultPostParams);
  view.requests[7].resolve(postPage('unfiltered')); await view.settle(); assert.deepEqual(view.rows(), ['unfiltered']);
});

test('changed publication searches discard late replies, and failed Next never displays the previous page rows', async (t) => {
  const view = postsHarness(t);
  view.input('rain'); view.submit(); assert.equal(view.requests[0].signal.aborted, true);
  view.advance(500); assert.equal(view.requests.length, 2, 'Enter must cancel the pending debounce');
  view.requests[1].resolve(postPage('rain', 'rain-next')); await view.settle();
  view.requests[0].resolve(postPage('stale')); await view.settle(); assert.deepEqual(view.rows(), ['rain']);
  view.click('Next'); assert.deepEqual(view.rows(), []);
  view.requests[2].reject(new Error('Capture service unavailable')); await view.settle();
  assert.deepEqual(view.rows(), []);
  assert.equal(view.find('p', props => props.role === 'alert').props.children[0], 'Capture service unavailable');
  view.click('Previous'); assert.deepEqual(view.rows(), []);
  assert.deepEqual(postParams(view.requests[3]), { ...defaultPostParams, q: 'rain' });
  view.requests[3].resolve(postPage('restored', 'rain-next')); await view.settle(); assert.deepEqual(view.rows(), ['restored']);
});

test('network and creation date sorting reset cursors, cancel old replies and persist across pagination', async (t) => {
  const view = postsHarness(t);
  view.requests[0].resolve(postPage('initial', 'next')); await view.settle(); view.click('Next');
  view.input('Flood'); view.network('instagram');
  assert.equal(view.requests[1].signal.aborted, true);
  assert.deepEqual(postParams(view.requests[2]), { ...defaultPostParams, platform: 'instagram', q: 'Flood' });
  view.advance(250); assert.equal(view.requests.length, 3);
  view.click('Sort by creation date, oldest first');
  assert.equal(view.requests[2].signal.aborted, true);
  const filters = { ...defaultPostParams, order: 'asc', platform: 'instagram', q: 'Flood' };
  assert.deepEqual(postParams(view.requests[3]), filters);
  assert.equal(view.find('th', props => props['aria-sort']).props['aria-sort'], 'ascending');
  view.requests[3].resolve(postPage('sorted', 'sorted-next')); await view.settle();
  view.requests[2].resolve(postPage('old-filter')); view.requests[1].resolve(postPage('old-page')); await view.settle();
  assert.deepEqual(view.rows(), ['sorted']);
  assert.equal(view.find('time').props.dateTime, '2026-10-03T12:00:00Z');
  assert.equal(view.find('time').props.children, '03/10/2026, 07:00:00');
  view.click('Next'); assert.deepEqual(postParams(view.requests[4]), { ...filters, cursor: 'sorted-next' });
  view.requests[4].resolve(postPage('sorted-second')); await view.settle();
  view.poll(); assert.deepEqual(postParams(view.requests[5]), postParams(view.requests[4]));
  view.requests[5].resolve(postPage('sorted-second')); await view.settle();
  view.pageSize(10); assert.deepEqual(postParams(view.requests[6]), { ...filters, limit: '10' });
  view.requests[6].resolve(postPage('ten')); await view.settle();
  view.click('Sort by creation date, newest first');
  assert.deepEqual(postParams(view.requests[7]), { ...filters, limit: '10', order: 'desc' });
  view.network(''); assert.deepEqual(postParams(view.requests[8]), { ...defaultPostParams, limit: '10', q: 'Flood' });
});

test('search highlights literal case-insensitive matches safely, including punctuation and markup text', async (t) => {
  const view = postsHarness(t);
  for (const [query, text, expected] of [['x', 'prefix X suffix', ['x', 'X', 'x']], ['.*[', 'name .*[ literal', ['.*[']], ['<script>', '<script>alert(1)</script>', ['<script>']], ['BOGOTÁ', 'Bogotá', ['Bogotá']]]) {
    view.input(query); view.advance(250);
    view.requests.at(-1).resolve(postPage(text)); await view.settle();
    const username = view.all('td')[2];
    assert.deepEqual(username.props.children.filter(value => value?.type === 'mark').map(value => value.props.children), expected);
    assert.equal(view.all('script').length, 0);
    assert.equal(view.find('tbody').props.children[0].props.className, 'territorial-search-match');
  }
  view.input(''); view.advance(250); view.requests.at(-1).resolve(postPage('unfiltered')); await view.settle();
  assert.equal(view.all('mark').length, 0);
  assert.equal(view.find('tbody').props.children[0].props.className, undefined);
});
