import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/prismaAdminState.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const exports = {};
new Function('exports', compiled)(exports);
const { captureState, refreshedEditor, refreshedPosts, safeMediaUrl, sourceDefaults, sourceDirty } = exports;
const saved = sourceDefaults({ platform: 'x', enabled: true, mode: 'simulation', query: '#bogota', interval_minutes: 5,
  secret_ref: 'PrismaSource_x', credential_configured: false, status: 'paused', capture_running: false, config_version: 7 });

test('polling preserves unsaved searches, credentials and the optimistic revision', () => {
  for (const edit of [{ draft: { ...saved, query: '#kennedy' } }, { token: 'new-secret' },
    { draft: { ...saved, report_thresholds: { low: 7, medium: 12, high: 25 } } }]) {
    const editor = { saved, draft: saved, token: '', ...edit };
    const fresh = { ...saved, config_version: 8, query: '#other', capture_running: true };
    assert.equal(sourceDirty(editor), true);
    assert.equal(refreshedEditor(editor, fresh), editor);
    assert.equal(editor.saved.config_version, 7);
  }
});

test('clean editors refresh, while late responses cannot undo a completed save', () => {
  const editor = { saved, draft: saved, token: '' };
  const fresh = { ...saved, config_version: 8, capture_running: true };
  assert.deepEqual(refreshedEditor(editor, fresh), { saved: fresh, draft: fresh, token: '' });
  assert.equal(refreshedEditor(editor, { ...saved, config_version: 6 }), editor);
  assert.equal(sourceDirty({ ...editor, draft: { ...saved, report_thresholds: { high: 20, low: 5, medium: 10 } } }), false);
  assert.equal(saved.correlation_window_minutes, 30);
});

test('capture indication follows persisted state and exposes failures before animation', () => {
  assert.equal(captureState(saved), 'Paused');
  assert.equal(captureState({ ...saved, capture_running: true }), 'Running');
  assert.equal(captureState({ ...saved, capture_running: true, capture_state: 'capturing' }), 'Capturing');
  assert.equal(captureState({ ...saved, capture_running: true, capture_state: 'scheduled' }), 'Scheduled');
  assert.equal(captureState({ ...saved, capture_running: true, capture_state: 'running', next_due: '2099-01-01T00:00:00Z' }), 'Running');
  assert.equal(captureState({ ...saved, capture_running: true, capture_state: 'capturing', last_error: 'rate_limited' }), 'Error');
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
  assert.match(admin, /expected_revision: saved.config_version/);
  assert.match(admin, /action\('pause'\)/);
  assert.match(posts, /showModal\(\)/);
  assert.match(posts, /origin\.focus\(\)/);
  assert.match(posts, /post\.mode === 'real'/);
  assert.match(posts, /controls preload="metadata"/);
  assert.doesNotMatch(posts, /autoPlay|dangerouslySetInnerHTML/);
  assert.match(styles, /prefers-reduced-motion: reduce/);
});
