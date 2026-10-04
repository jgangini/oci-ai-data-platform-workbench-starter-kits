import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import * as jsxRuntime from 'react/jsx-runtime';

const source = readFileSync(new URL('../src/prismaAdminState.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const exports = {};
new Function('exports', compiled)(exports);
const { captureState, postStatus, refreshedEditor, refreshedPosts, safeMediaUrl, sourceDefaults, sourceDirty, sourcePayload, timestamp } = exports;
const saved = sourceDefaults({ platform: 'x', enabled: true, mode: 'simulation', query: '#bogota', interval_minutes: 5,
  secret_ref: 'PrismaSource_x', credential_configured: false, status: 'paused', capture_running: false, config_version: 7 });

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
  const real = sourcePayload({ ...editor, draft: { ...saved, mode: 'real' } });
  assert.equal(real.bearer_token, 'hidden-token'); assert.equal(real.expected_revision, 7);
  assert.throws(() => sourcePayload({ ...editor, draft: { ...saved, report_thresholds: { low: 8, medium: 7, high: 20 } } }), /must be positive and increase/);
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
  const compiled = ts.transpileModule(readFileSync(new URL('../src/PrismaAdmin.tsx', import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const component = {};
  new Function('exports', 'require', compiled)(component, name => {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return jsxRuntime;
    if (name === './prismaAdminState') return exports;
    if (name === './prisma.css') return {};
    return { PrismaPosts: () => null, PrismaSyntheticReset: () => null, PrismaSyntheticResetStatus: () => null };
  });
  const props = { api: () => new Promise(resolve => requests.push(resolve)), viewerUrlControl: null, searchIcon: null, refreshIcon: null };
  function render() {
    for (let turns = 0; dirty && turns < 20; turns++) {
      dirty = false; cursor = 0; effects = []; tree = component.PrismaAdmin(props);
      for (const effect of effects) effect();
    }
    assert.equal(dirty, false);
  }
  function nodes(value) { return [value, ...[value?.props?.children].flat(Infinity).filter(Boolean).flatMap(child => typeof child === 'object' ? nodes(child) : [])]; }
  const find = match => { const node = nodes(tree).find(node => node?.props && match(node.props)); assert.ok(node); return node; };
  const dot = platform => find(props => props.id === `prisma-tab-${platform}`).props.children.at(-1).props;
  const sources = ['x', 'facebook', 'instagram', 'tiktok'].map((platform, index) => ({ ...saved, platform, capture_running: index < 3, capture_state: ['running', 'scheduled', 'capturing', 'paused'][index] }));
  t.after(() => { for (const slot of slots) slot?.cleanup?.(); if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow; });
  render(); requests[0]({ sources, runtime: 'local_fixture' }); await new Promise(setImmediate); render();
  for (const [index, platform] of ['x', 'facebook', 'instagram', 'tiktok'].entries()) {
    assert.equal(dot(platform)['aria-label'], index < 3 ? 'Running' : 'Paused');
    assert.equal(dot(platform).className, `prisma-capture-dot ${index < 3 ? 'running' : 'paused'}`);
  }
  const tab = platform => find(props => props.id === `prisma-tab-${platform}`);
  const formHidden = () => find(props => props.id === 'prisma-source-forms').props.hidden;
  assert.equal(tab('x').props['aria-expanded'], true);
  tab('x').props.onClick(); render();
  assert.equal(formHidden(), true);
  assert.equal(tab('x').props['aria-selected'], true);
  assert.equal(tab('x').props['aria-expanded'], false);
  assert.equal(tab('x').props.tabIndex, 0);
  assert.equal(nodes(tree).filter(node => node?.props?.role === 'tabpanel').length, 4, 'Editors stay mounted when collapsed');
  tab('x').props.onClick(); render(); assert.equal(formHidden(), false);
  tab('x').props.onClick(); render();
  tab('facebook').props.onClick(); render();
  assert.equal(formHidden(), false); assert.equal(tab('facebook').props['aria-expanded'], true);
  find(props => props['aria-controls'] === 'prisma-source-forms').props.onClick(); render();
  assert.equal(tab('facebook').props['aria-expanded'], false);
  let prevented = false;
  tab('facebook').props.onKeyDown({ key: 'Home', preventDefault() { prevented = true; } }); render();
  assert.equal(prevented, true); assert.equal(tab('x').props['aria-expanded'], true);
  tab('x').props.onKeyDown({ key: 'Home', preventDefault() {} }); render();
  assert.equal(formHidden(), false, 'Keyboard navigation opens rather than toggling the active tab');
  find(props => props['aria-controls'] === 'prisma-source-forms').props.onClick(); render();
  assert.equal(find(props => props.id === 'prisma-source-forms').props.hidden, true);
  globalThis.window.location.hash = '#parameters'; listeners.get('hashchange')(); render();
  assert.equal(find(props => props.children === 'Parameters').props['aria-pressed'], true);
  assert.equal(timers.size, 1); [...timers.values()][0]();
  requests[1]({ sources: sources.map(source => ({ ...source, capture_running: false, capture_state: 'paused' })), runtime: 'local_fixture' });
  await new Promise(setImmediate); render();
  assert.equal(dot('x').className, 'prisma-capture-dot paused');
  assert.equal(find(props => props.id === 'prisma-source-forms').props.hidden, true);
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
  assert.equal(safeMediaUrl('/api/admin/prisma/media/post-0001/image-01.svg'), '/api/admin/prisma/media/post-0001/image-01.svg');
  assert.equal(safeMediaUrl('https://pbs.twimg.com/media/example.jpg'), 'https://pbs.twimg.com/media/example.jpg');
  for (const value of ['javascript:alert(1)', 'data:image/svg+xml,test', 'http://example.com/video.mp4', '//example.com/photo.png',
    'https://user:password@example.com/a.png', '/api/admin/prisma/media/../secret.svg', '/api/admin/prisma/media/%2e%2e/photo.svg', '/api/admin/users']) {
    assert.equal(safeMediaUrl(value), null, value);
  }
});

test('network panels and preview retain keyboard, provenance and reduced-motion contracts', () => {
  const admin = readFileSync(new URL('../src/PrismaAdmin.tsx', import.meta.url), 'utf8');
  const posts = readFileSync(new URL('../src/PrismaPosts.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/prisma.css', import.meta.url), 'utf8');
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
  assert.equal((admin.match(/<PrismaPosts\b/g) || []).length, 1);
  assert.match(admin, /aria-controls="prisma-source-forms"/);
  assert.match(admin, /draft\.mode === 'real' && <fieldset className="prisma-credentials">[\s\S]*?<label>Credential reference[\s\S]*?<label>Update token[\s\S]*?<\/fieldset>/);
  assert.match(posts, /new URLSearchParams\(\{ limit: String\(pageSize\)/);
  assert.doesNotMatch(posts, /Publications from|\['Attachments', 'Username', 'Display name'/);
  assert.match(posts, /timestamp\(post\.captured_at, timeZone\)/);
  assert.match(posts, /\[10, 20, 50, 100\]/);
});

// Run the component's real handlers/effects; child previews are outside this pagination check.
const postsCompiled = ts.transpileModule(readFileSync(new URL('../src/PrismaPosts.tsx', import.meta.url), 'utf8'), {
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
    assert.equal(name, './prismaAdminState'); return exports;
  });
  const props = { refreshKey: 0, searchIcon: null, refreshIcon: null,
    api: (url, options) => new Promise((resolve, reject) => requests.push({ url, signal: options.signal, resolve, reject })) };
  function render() {
    for (let turns = 0; dirty && turns < 20; turns++) {
      dirty = false; cursor = 0; effects = []; tree = component.PrismaPosts(props);
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
    assert.equal(view.find('tbody').props.children[0].props.className, 'prisma-search-match');
  }
  view.input(''); view.advance(250); view.requests.at(-1).resolve(postPage('unfiltered')); await view.settle();
  assert.equal(view.all('mark').length, 0);
  assert.equal(view.find('tbody').props.children[0].props.className, undefined);
});
