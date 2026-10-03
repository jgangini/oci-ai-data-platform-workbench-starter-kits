import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, readdir } from 'node:fs/promises';
import test from 'node:test';
import { allowedActions, bogotaToUtc, evidenceFor, filteredIncidents, modeLabel, safeSourceUrl, utcToBogota, validPeriod, validateSnapshot } from '../src/model.js';
import { createPrismaSession } from '../src/chat.js';

const snapshot = { version: 'v1', incidents: [
  { id: 'sim-1', mode: 'simulation', locality: 'Suba', severity: 'high', category: 'flood', lat: 4.7, lon: -74.1, created_at: '2026-10-05T14:00:00Z', evidence_ids: ['e1'] },
  { id: 'real-1', mode: 'real', locality: 'Bosa', severity: 'medium', category: 'rain', lat: 4.6, lon: -74.1, created_at: '2026-10-05T14:05:00Z', evidence_ids: ['e2'] },
], evidence: [{ id: 'e1', platform: 'x', mode: 'simulation' }, { id: 'e2', platform: 'meteo', mode: 'real' }] };

test('filters keep mode, platform and evidence ownership intact', () => {
  assert.deepEqual(filteredIncidents(snapshot, { mode: 'simulation', platform: 'x' }).map((item) => item.id), ['sim-1']);
  assert.equal(filteredIncidents(snapshot, { mode: 'real', platform: 'x' }).length, 0);
  assert.deepEqual(evidenceFor(snapshot, snapshot.incidents[1]), [snapshot.evidence[1]]);
  assert.equal(modeLabel('simulation'), 'SIMULADO');
  assert.equal(modeLabel('real'), 'REAL');
  assert.equal(modeLabel(null), 'SIN CLASIFICAR');
});

test('Bogotá period is timezone-independent, inclusive and rejects reversed bounds', () => {
  assert.equal(bogotaToUtc('2026-10-05T09:00'), '2026-10-05T14:00:00.000Z');
  assert.equal(utcToBogota('2026-10-05T14:00:00Z'), '2026-10-05T09:00:00');
  assert.throws(() => bogotaToUtc('2026-02-30T09:00'));
  const exact = { date_from: '2026-10-05T14:00:00Z', date_to: '2026-10-05T14:00:00Z' };
  assert.deepEqual(filteredIncidents(snapshot, exact).map((item) => item.id), ['sim-1']);
  assert.deepEqual(filteredIncidents(snapshot, { date_from: '2026-10-05T14:05:00Z' }).map((item) => item.id), ['real-1']);
  assert.equal(validPeriod({ date_from: '2026-10-05T14:05:00Z', date_to: exact.date_to }), false);
  assert.equal(validPeriod({ date_from: '2026-02-30T14:00:00Z' }), false);
  assert.equal(allowedActions([{ type: 'filter_incidents', filters: { date_from: 'not-a-date' } }], snapshot).length, 0);
});

test('evidence links reject executable, relative, credential-bearing and unencrypted URLs', () => {
  for (const value of ['javascript:alert(1)', '/admin', 'data:text/html,hello', 'https://user:token@example.com/', 'http://example.com/']) assert.equal(safeSourceUrl(value), null);
  assert.equal(safeSourceUrl('https://example.com/evidence'), 'https://example.com/evidence');
});

test('agent actions are restricted to known incidents and filter names', () => {
  const accepted = [{ type: 'focus_incident', incident_id: 'real-1' }, { type: 'filter_incidents', filters: { locality: 'Suba', mode: 'simulation' } }];
  assert.deepEqual(allowedActions([...accepted, { type: 'focus_incident', incident_id: 'missing' }, { type: 'run_script', code: 'alert(1)' }, { type: 'filter_incidents', filters: { url: 'https://example.com' } }], snapshot), accepted);
  assert.deepEqual(allowedActions([{ type: 'filter_incidents', filters: { locality: 'Unknown option' } }], snapshot), []);
});

test('malformed snapshots cannot replace a valid publication', () => {
  assert.equal(validateSnapshot(snapshot), snapshot);
  assert.throws(() => validateSnapshot({ version: 'v1', incidents: null }));
  assert.throws(() => validateSnapshot({ ...snapshot, incidents: [{ ...snapshot.incidents[0], lat: 100 }] }));
  assert.doesNotThrow(() => validateSnapshot({ ...snapshot, incidents: [{ ...snapshot.incidents[0], lat: null, lon: null }] }));
  assert.throws(() => validateSnapshot({ ...snapshot, incidents: [{ ...snapshot.incidents[0], lat: null }] }));
});

test('GodEye text adapter sends context and ignores canceled late responses', async () => {
  let resolve;
  let captured;
  const replies = [];
  const errors = [];
  const session = createPrismaSession({
    context: () => ({ version: 'v1', incident_id: 'sim-1', filters: { mode: 'simulation' } }),
    request: (path, options) => { captured = { path, ...options }; return new Promise((done) => { resolve = done; }); },
    onReply: (reply) => replies.push(reply), onError: (error) => errors.push(error), onBusy: () => {},
  });
  await session.start();
  const turn = session.sendText('¿Qué ocurrió?');
  assert.equal(captured.path, '/api/prisma/chat');
  assert.equal(JSON.parse(captured.body).incident_id, 'sim-1');
  session.stop();
  assert.equal(captured.signal.aborted, true);
  resolve({ answer: 'Late answer', version: 'v1' });
  await turn;
  assert.deepEqual(replies, []);
  assert.deepEqual(errors, []);
  session.destroy();
});

test('invalid or stale agent replies are errors, never generated answers', async () => {
  for (const reply of [{ answer: '', version: 'v1' }, { answer: 'Something', version: 'v0' }]) {
    const errors = [];
    const session = createPrismaSession({ context: () => ({ version: 'v1' }), request: async () => reply, onReply: () => assert.fail('Unexpected answer'), onError: (error) => errors.push(error), onBusy: () => {} });
    await session.start(); await session.sendText('Consulta');
    assert.equal(errors.length, 1); session.destroy();
  }
});

test('follow-up questions preserve the session and publication context with period filters', async () => {
  const sent = [];
  const context = { version: 'v1', incident_id: 'sim-1', filters: { date_from: '2026-10-05T14:00:00Z' } };
  const session = createPrismaSession({ context: () => context,
    request: async (_path, options) => { sent.push(JSON.parse(options.body)); return { answer: 'SIMULADO · e1', version: 'v1', session_id: '2b4e409b-38da-42cf-b040-75e146d2c0c0' }; },
    onReply: () => {}, onError: (error) => assert.fail(error.message), onBusy: () => {},
  });
  await session.start(); await session.sendText('¿Qué ocurrió?'); await session.sendText('¿Qué evidencia lo respalda?');
  assert.equal(sent[1].session_id, '2b4e409b-38da-42cf-b040-75e146d2c0c0');
  assert.equal(sent[0].version, sent[1].version);
  assert.deepEqual(sent[1].filters, context.filters);
  assert.equal(sent[1].incident_id, 'sim-1');
  session.destroy();
});

test('vendored GodEye matches the pinned allowlist; no third-party datasets are bundled', async () => {
  const root = new URL('../vendor/gods-eye-view/', import.meta.url);
  const provenance = JSON.parse(await readFile(new URL('PROVENANCE.json', root), 'utf8'));
  assert.match(provenance.commit, /^[0-9a-f]{40}$/);
  const found = (await readdir(root, { recursive: true, withFileTypes: true })).filter((entry) => entry.isFile());
  assert.equal(found.length, Object.keys(provenance.files).length + 1);
  for (const [path, hash] of Object.entries(provenance.files)) {
    assert.match(path, /^(LICENSE|src\/(app|maps|voice)\/[a-zA-Z]+\.js)$/);
    assert.equal(createHash('sha256').update(await readFile(new URL(path, root))).digest('hex'), hash, path);
  }
});
