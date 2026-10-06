import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, readdir } from 'node:fs/promises';
import test from 'node:test';
import { allowedActions, bogotaToUtc, displayLocality, displaySeverity, evidenceFor, filteredIncidents, modeLabel, parseBbox, photosFor, reportActivity, safeSourceUrl, utcToBogota, validPeriod, validateSnapshot, withinBbox } from '../src/model.js';
import { createGodsEyeViewSession } from '../src/chat.js';
import { nasaDate, nasaUrl } from '../src/context.js';
import { observeImagery } from '../src/imagery.js';

const snapshot = { version: 'v1', incidents: [
  { id: 'sim-1', mode: 'Synthetic', locality: 'Suba', severity: 'high', category: 'flood', lat: 4.7, lon: -74.1, created_at: '2026-10-05T14:00:00Z', evidence_ids: ['e1'] },
  { id: 'real-1', mode: 'real', locality: 'Bosa', severity: 'medium', category: 'rain', lat: 4.6, lon: -74.1, created_at: '2026-10-05T14:05:00Z', evidence_ids: ['e2'] },
], evidence: [{ id: 'e1', platform: 'x', mode: 'Synthetic' }, { id: 'e2', platform: 'meteo', mode: 'real' }] };

test('NASA imagery uses a named daily layer and explicit valid date', () => {
  assert.equal(nasaDate(new Date('2026-10-03T12:00:00Z')), '2026-10-01');
  assert.match(nasaUrl('2026-10-01'), /gibs\.earthdata\.nasa\.gov.*\/2026-10-01\/GoogleMapsCompatible_Level9\/\{z\}\/\{y\}\/\{x\}\.jpg$/);
  assert.throws(() => nasaUrl('2026-02-30'));
  assert.throws(() => nasaUrl('../other-layer'));
});

test('imagery recovery reports actual tiles and retains provider failures and throttling', async () => {
  const states = [];
  let result;
  const provider = { requestImage: () => result };
  observeImagery(provider, (state) => states.push(state));
  assert.equal(provider.requestImage(), undefined);
  result = Promise.reject(new Error('tile unavailable'));
  await assert.rejects(provider.requestImage(), /tile unavailable/);
  assert.deepEqual(states.at(-1), { loaded: 0, failed: 1 });
  result = Promise.resolve('tile pixels');
  assert.equal(await provider.requestImage(), 'tile pixels');
  assert.deepEqual(states.at(-1), { loaded: 1, failed: 0 });
});

test('filters keep mode, platform and evidence ownership intact', () => {
  for (const mode of ['Synthetic', 'simulation']) {
    const published = { ...snapshot, incidents: [{ ...snapshot.incidents[0], mode }, snapshot.incidents[1]] };
    for (const filter of ['Synthetic', 'simulation']) assert.deepEqual(filteredIncidents(published, { mode: filter, platform: 'x' }).map((item) => item.id), ['sim-1']);
    assert.equal(modeLabel(mode), 'Synthetic');
    assert.equal(published.incidents[0].mode, mode, 'Reading a publication does not mutate its provenance');
  }
  assert.equal(filteredIncidents(snapshot, { mode: 'real', platform: 'x' }).length, 0);
  assert.deepEqual(evidenceFor(snapshot, snapshot.incidents[1]), [snapshot.evidence[1]]);
  assert.equal(modeLabel('real'), 'REAL');
  assert.equal(modeLabel(null), 'UNCLASSIFIED');
});

test('each captured publication remains navigable while duplicate IDs appear once', () => {
  const original = { id: 'first', platform: 'x', mode: 'Synthetic', text: 'Lluvia en Bogotá', created_at: '2026-10-03T10:00:00Z' };
  const latest = { ...original, id: 'last', mode: 'simulation', text: 'Lluvia  en Bogota\u0301\n', created_at: '2026-10-03T10:10:00Z', username: 'latest_author' };
  const records = [latest, original, { ...original, id: 'other-network', platform: 'facebook' },
    { ...original, id: 'real-post', mode: 'real' }, { ...original, id: 'different-text', text: 'Ya no llueve en Suba' },
    { ...original, id: original.text, text: '' }, { ...original, id: 'empty-two', text: '' }];
  const before = structuredClone(records), incident = { evidence_ids: records.map((item) => item.id) };
  const evidence = evidenceFor({ evidence: [...records, original, { ...original, id: 'not-linked' }] }, incident);
  assert.equal(evidence.length, 7, 'A repeated row ID is returned once and unrelated evidence is excluded');
  assert.equal(evidence[0].id, 'last');
  assert.equal(evidence[1].id, 'first', 'Matching wording does not hide a distinct captured publication');
  assert.deepEqual(records, before, 'Display must not rewrite source evidence');
  assert.deepEqual(incident.evidence_ids, records.map((item) => item.id));
});

test('English system labels preserve original Bogotá localities and filter values', async () => {
  assert.equal(displayLocality('Sin localizar'), 'Location unresolved');
  for (const locality of ['Kennedy', 'Ciudad Bolívar', 'Usaquén']) assert.equal(displayLocality(locality), locality);
  assert.deepEqual(['low', 'medium', 'high'].map(displaySeverity), ['Low', 'Medium', 'High']);
  const unresolved = { ...snapshot.incidents[0], locality: 'Sin localizar' };
  assert.deepEqual(filteredIncidents({ ...snapshot, incidents: [unresolved] }, { locality: 'Sin localizar' }), [unresolved]);
  assert.equal(unresolved.locality, 'Sin localizar');
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  assert.match(html, /Which alerts have the highest severity\?/);
  assert.match(html, /What evidence supports the selected alert\?/);
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
  const ongoing = { ...snapshot, incidents: [{ ...snapshot.incidents[0], created_at: '2026-09-01T00:00:00Z', evidence_ids: ['old', 'recent', 'future'] }],
    evidence: [{ id: 'old', created_at: '2026-09-01T00:00:00Z' }, { id: 'recent', created_at: exact.date_from }, { id: 'future', created_at: '2026-10-06T00:00:00Z' }] };
  assert.equal(filteredIncidents(ongoing, exact).length, 1, 'An old incident with a publication in the requested range remains visible');
  assert.equal(filteredIncidents({ ...ongoing, evidence: [ongoing.evidence[0], ongoing.evidence[2]] }, exact).length, 0, 'Both period bounds apply to the same publication');
});

test('evidence links reject executable, relative, credential-bearing and unencrypted URLs', () => {
  for (const value of ['javascript:alert(1)', '/admin', 'data:text/html,hello', 'https://user:token@example.com/', 'http://example.com/']) assert.equal(safeSourceUrl(value), null);
  assert.equal(safeSourceUrl('https://example.com/evidence'), 'https://example.com/evidence');
});

test('agent actions are restricted to known incidents and filter names', () => {
  const accepted = [{ type: 'focus_incident', incident_id: 'real-1' }, { type: 'filter_incidents', filters: { locality: 'Suba' } }];
  assert.deepEqual(allowedActions([...accepted, { type: 'focus_incident', incident_id: 'missing' }, { type: 'run_script', code: 'alert(1)' }, { type: 'filter_incidents', filters: { url: 'https://example.com' } }], snapshot), accepted);
  assert.deepEqual(allowedActions([{ type: 'filter_incidents', filters: { locality: 'Unknown option' } }], snapshot), []);
  for (const mode of ['', 'real', 'Synthetic', 'simulation']) assert.deepEqual(allowedActions([{ type: 'filter_incidents', filters: { mode } }], snapshot), [], 'The agent cannot apply an invisible origin filter');
});

test('area is inclusive, rejects invalid bounds and keeps the same incident evidence scope', () => {
  const bbox = '-74.1,4.7,-74.0,4.8';
  const selected = filteredIncidents(snapshot, { bbox });
  assert.deepEqual(selected.map((item) => item.id), ['sim-1']);
  assert.deepEqual(evidenceFor(snapshot, selected[0]).map((item) => item.id), ['e1']);
  assert.equal(withinBbox({ lat: null, lon: null }, parseBbox(bbox)), false);
  for (const value of ['1,2,3', '1,,3,4', 'nan,2,3,4', '-181,0,180,1', '0,-91,1,1', '3,2,1,4', '1,4,3,2', '170,-10,-170,10', '0x10,0,20,10', '1_0,0,20,10']) {
    assert.throws(() => parseBbox(value));
    assert.deepEqual(filteredIncidents(snapshot, { bbox: value }), []);
    assert.deepEqual(allowedActions([{ type: 'filter_incidents', filters: { bbox: value } }], snapshot), []);
  }
  assert.equal(allowedActions([{ type: 'filter_incidents', filters: { bbox } }], snapshot).length, 1);
});

test('attached X photos remain available during review and require incident membership and the original media host', () => {
  const photo = { type: 'photo', url: 'https://pbs.twimg.com/media/example.jpg', alt_text: 'Inundación' };
  const evidence = { id: 'e1', platform: 'x', mode: 'real', media: [photo] };
  const incident = { review_status: 'validated', evidence_ids: ['e1'], reviewed_evidence_ids: ['e1'] };
  assert.deepEqual(photosFor(evidence, incident), [photo]);
  for (const status of ['pending', 'rejected', undefined]) assert.deepEqual(photosFor(evidence, { ...incident, review_status: status }), [photo]);
  assert.deepEqual(photosFor(evidence, { ...incident, evidence_ids: ['other'] }), []);
  assert.deepEqual(photosFor(evidence, { ...incident, reviewed_evidence_ids: undefined }), [photo]);
  assert.deepEqual(photosFor({ ...evidence, id: 'e2' }, { ...incident, evidence_ids: ['e1', 'e2'] }), [photo]);
  assert.deepEqual(photosFor({ ...evidence, id: 'e2' }, { ...incident, evidence_ids: ['e1', 'e2'], reviewed_evidence_ids: ['e1', 'e2'] }), [photo]);
  assert.deepEqual(photosFor(evidence), []);
  for (const url of ['javascript:alert(1)', 'http://pbs.twimg.com/media/x.jpg', 'https://pbs.twimg.com.evil.test/media/x.jpg', 'https://pbs.twimg.com:8443/media/x.jpg', 'https://user:secret@pbs.twimg.com/media/x.jpg', 'https://pbs.twimg.com/profile_images/x.jpg', 'https://pbs.twimg.com/media/x.jpg#fragment']) {
    assert.deepEqual(photosFor({ ...evidence, media: [{ ...photo, url }] }, incident), []);
  }
  assert.deepEqual(photosFor({ ...evidence, media: [{ ...photo, type: 'video' }] }, incident), []);
});

test('bundled image attachments use the authenticated allowlisted endpoint without changing provenance', () => {
  const attachment = { type: 'image', mime_type: 'image/svg+xml', dataset_path: 'posts/post-0001/media/image-01.svg',
    sha256: 'a'.repeat(64), alt_text: 'Synthetic illustration, not a photograph', is_simulated: true };
  const evidence = { id: 'e1', platform: 'facebook', mode: 'Synthetic', attachments: [attachment, attachment] };
  const incident = { evidence_ids: ['e1'], review_status: 'pending' };
  for (const mode of ['Synthetic', 'simulation']) assert.deepEqual(photosFor({ ...evidence, mode }, incident), [{ ...attachment, url: '/api/gods-eye-view/media/post-0001/image-01.svg' }]);
  assert.equal(evidence.mode, 'Synthetic'); assert.equal(attachment.is_simulated, true); assert.equal('url' in attachment, false);
  assert.deepEqual(photosFor({ ...evidence, mode: 'real' }, incident), []);
  for (const change of [{ dataset_path: '../secret.pem' }, { dataset_path: 'posts/post-0001/media/../../secret.svg' },
    { dataset_path: '//evil.test/image-01.svg' }, { sha256: 'bad' }, { type: 'video' }, { mime_type: 'text/html' }]) {
    assert.deepEqual(photosFor({ ...evidence, attachments: [{ ...attachment, ...change }] }, incident), []);
  }
  assert.deepEqual(photosFor(evidence, { evidence_ids: ['other'] }), []);
});

test('AI-generated raster attachments retain provenance and use only authenticated fixture image URLs', () => {
  const attachment = { id: 'post-0001-image-01', type: 'image', mime_type: 'image/png', dataset_path: 'media/kennedy-flood.png',
    origin: 'ai_generated', sha256: 'b'.repeat(64), alt_text: 'AI-generated Synthetic scene of flooding in Bogotá', is_simulated: true };
  const evidence = { id: 'captured-1', platform: 'instagram', mode: 'Synthetic', attachments: [attachment, attachment] };
  const incident = { evidence_ids: [evidence.id] };
  for (const [extension, mime_type] of Object.entries({ png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp' })) {
    const image = { ...attachment, dataset_path: `media/kennedy-flood.${extension}`, mime_type };
    for (const mode of ['Synthetic', 'simulation']) assert.deepEqual(photosFor({ ...evidence, mode, attachments: [image, image] }, incident),
      [{ ...image, url: `/api/gods-eye-view/media/post-0001/image-01.${extension}` }]);
  }
  for (const changes of [{ id: '../post-0001-image-01' }, { id: 'post-0001' }, { origin: 'photograph' }, { origin: undefined },
    { mime_type: 'image/jpeg' }, { dataset_path: 'media/../../secret.png' }, { dataset_path: 'media/scene.svg' },
    { dataset_path: '//evil.test/scene.png' }, { dataset_path: 'media/scene.png?file=secret' }, { sha256: 'bad' }, { type: 'video' }]) {
    assert.deepEqual(photosFor({ ...evidence, attachments: [{ ...attachment, ...changes }] }, incident), []);
  }
  assert.deepEqual(photosFor({ ...evidence, mode: 'real' }, incident), []);
  assert.deepEqual(photosFor(evidence, { evidence_ids: ['another-post'] }), []);
  assert.equal(attachment.origin, 'ai_generated'); assert.equal(attachment.is_simulated, true); assert.equal('url' in attachment, false);
});

test('captured v2 images retain their dataset version instead of resolving the same fixture in v1', () => {
  const attachment = { id: 'post-0001-image-01', type: 'image', mime_type: 'image/png', dataset_path: 'media/flood-doorway.png',
    origin: 'ai_generated', sha256: 'c'.repeat(64), is_simulated: true };
  const evidence = { id: 'captured-v2', platform: 'x', mode: 'Synthetic', attachments: [attachment],
    raw_metadata: { dataset_version: 'bogota-v2' } };
  const incident = { evidence_ids: [evidence.id] };
  const before = JSON.stringify(evidence);
  assert.deepEqual(photosFor(evidence, incident), [{ ...attachment,
    url: '/api/gods-eye-view/media/post-0001/image-01.png?dataset_version=bogota-v2' }]);
  for (const version of [undefined, 'bogota-v1']) assert.equal(
    photosFor({ ...evidence, raw_metadata: { dataset_version: version } }, incident)[0].url,
    '/api/gods-eye-view/media/post-0001/image-01.png');
  for (const version of ['bogota-v3', '../v1', 'bogota-v2&other=1', null, {}, []]) assert.deepEqual(
    photosFor({ ...evidence, raw_metadata: { dataset_version: version } }, incident), []);
  assert.equal(JSON.stringify(evidence), before);
});

test('report activity displays published network counts without deriving severity or confirmation', () => {
  const incident = { severity: 'high', review_status: 'validated', report_activity: 'medium',
    report_counts: { x: 10, facebook: 0, instagram: -1, tiktok: '20' },
    report_activity_by_platform: { x: 'medium', facebook: 'below_threshold' } };
  assert.deepEqual(reportActivity(incident), { level: 'Medium', networks: [
    { platform: 'x', count: 10, level: 'Medium', total: 0, windowMinutes: null, latestAt: null },
    { platform: 'facebook', count: 0, level: 'Below threshold', total: 0, windowMinutes: null, latestAt: null },
  ] });
  assert.deepEqual(reportActivity({ severity: 'high', review_status: 'validated' }), { level: 'Unavailable', networks: [] });
  assert.deepEqual(reportActivity({ report_activity: 'confirmed', report_counts: [20] }), { level: 'Unavailable', networks: [] });
});

test('historical evidence counts do not replace zero recent distinct reports or invented window values', () => {
  const evidence = Array.from({ length: 30 }, (_, index) => ({ id: `e${index}`, platform: index < 20 ? 'x' : 'facebook',
    created_at: '2026-10-03T10:00:00Z', content_hash: 'same-contents' }));
  const incident = { evidence_ids: evidence.map((item) => item.id), report_counts: { x: 0, facebook: 0 },
    report_activity: 'below_threshold', report_activity_by_platform: { x: 'below_threshold', facebook: 'below_threshold' },
    correlation_windows_minutes: { x: 30 } };
  const result = reportActivity(incident, [...evidence, evidence[0], { id: 'not-linked', platform: 'x' }]);
  assert.equal(result.level, 'Below threshold');
  assert.deepEqual(result.networks, [
    { platform: 'x', count: 0, total: 20, windowMinutes: 30, latestAt: '2026-10-03T10:00:00Z', level: 'Below threshold' },
    { platform: 'facebook', count: 0, total: 10, windowMinutes: null, latestAt: '2026-10-03T10:00:00Z', level: 'Below threshold' },
  ]);
  assert.equal(reportActivity({ evidence_ids: ['e0'] }, evidence).networks[0].count, null);
});

test('platform logos retain the exact pinned Simple Icons CC0 bytes', async () => {
  const root = new URL('../public/brand-icons/', import.meta.url);
  const provenance = JSON.parse(await readFile(new URL('PROVENANCE.json', root), 'utf8'));
  assert.equal(provenance.commit, 'd9ea58066506bc80da65d5516813636b22b58a06');
  assert.equal(provenance.license, 'CC0-1.0');
  for (const [path, hash] of Object.entries(provenance.files)) {
    const name = path.split('/').at(-1);
    assert.equal(createHash('sha256').update(await readFile(new URL(name, root))).digest('hex'), hash, name);
  }
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
  const session = createGodsEyeViewSession({
    context: () => ({ version: 'v1', incident_id: 'sim-1', filters: { mode: 'Synthetic' } }),
    request: (path, options) => { captured = { path, ...options }; return new Promise((done) => { resolve = done; }); },
    onReply: (reply) => replies.push(reply), onError: (error) => errors.push(error), onBusy: () => {},
  });
  await session.start();
  const turn = session.sendText('¿Qué ocurrió?');
  assert.equal(captured.path, '/api/gods-eye-view/chat');
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
    const session = createGodsEyeViewSession({ context: () => ({ version: 'v1' }), request: async () => reply, onReply: () => assert.fail('Unexpected answer'), onError: (error) => errors.push(error), onBusy: () => {} });
    await session.start(); await session.sendText('Consulta');
    assert.equal(errors.length, 1); session.destroy();
  }
});

test('follow-up questions preserve the session and publication context with period filters', async () => {
  const sent = [];
  const context = { version: 'v1', incident_id: 'sim-1', filters: { date_from: '2026-10-05T14:00:00Z' } };
  const session = createGodsEyeViewSession({ context: () => context,
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

test('question count tracks submitted turns, survives cancellation and resets with an isolated session', async () => {
  const counts = [];
  const bodies = [];
  const replies = [];
  const errors = [];
  let finishCancelled;
  const options = {
    context: () => ({ version: 'v1' }),
    request: async (_path, request) => {
      const body = JSON.parse(request.body);
      bodies.push(body);
      if (body.question === 'Fail') throw new Error('Service unavailable');
      if (body.question === 'Cancel') return new Promise((resolve) => { finishCancelled = resolve; });
      return { answer: 'Evidence e1', version: 'v1', session_id: 'first-session' };
    },
    onReply: (reply) => replies.push(reply), onError: (error) => errors.push(error),
    onBusy: () => {}, onSubmitted: (count) => counts.push(count),
  };
  const first = createGodsEyeViewSession(options);
  await first.start();
  await first.sendText('   ');
  assert.deepEqual(counts, [0]);
  assert.equal(bodies.length, 0);
  await first.sendText('One question, regardless of length');
  await first.sendText('Fail');
  const cancelled = first.sendText('Cancel');
  first.stop();
  finishCancelled({ answer: 'Late response', version: 'v1', session_id: 'late-session' });
  await cancelled;
  await first.start();
  await first.sendText('Follow up');
  assert.deepEqual(counts, [0, 1, 2, 3, 4]);
  assert.equal(replies.length, 2);
  assert.equal(errors.length, 1);
  assert.equal(bodies.at(-1).session_id, 'first-session');
  first.destroy();
  const nextCounts = [];
  const second = createGodsEyeViewSession({ ...options, onSubmitted: (count) => nextCounts.push(count) });
  await second.start();
  await second.sendText('New conversation');
  assert.deepEqual(nextCounts, [0, 1]);
  assert.equal(bodies.at(-1).session_id, undefined);
  assert.deepEqual(counts, [0, 1, 2, 3, 4]);
  second.destroy();
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
