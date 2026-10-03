// Real Chromium DOM checks against the isolated local fixture; no screenshots, video or traces.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { parseArgs } from 'node:util';

const { values } = parseArgs({ options: {
  url: { type: 'string', default: 'http://localhost:18081' },
  'access-file': { type: 'string', default: '.tmp/prisma-local-access.json' },
  'playwright-module': { type: 'string', default: 'playwright' },
  channel: { type: 'string', default: 'chrome' },
} });
const base = new URL(values.url);
assert.ok(['127.0.0.1', 'localhost'].includes(base.hostname) && ['http:', 'https:'].includes(base.protocol), 'Only the loopback fixture may be tested');
const { chromium } = createRequire(import.meta.url)(values['playwright-module']);
const browser = await chromium.launch({ headless: true, channel: values.channel });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
let page = await context.newPage();
const errors = [];
const requests = [];
const consoleErrors = [];
const checks = [];
let fixtureVerified = false;
function observePage(target) {
  target.setDefaultTimeout(20000);
  target.on('pageerror', (error) => errors.push(error.message));
  target.on('console', (message) => {
    if (message.type() !== 'error') return;
    const location = message.location().url;
    consoleErrors.push({ origin: location ? new URL(location).origin : '', path: location ? new URL(location).pathname : '', text: message.text() });
  });
  target.on('requestfailed', (request) => {
    const url = new URL(request.url());
    requests.push({ origin: url.origin, path: url.pathname, error: request.failure()?.errorText });
  });
}
observePage(page);
context.on('page', observePage);
try {
  const unauthorized = await context.request.get(`${base.origin}/api/prisma/snapshot`);
  assert.equal(unauthorized.status(), 401);
  const access = JSON.parse(await readFile(values['access-file'], 'utf8'));
  const login = await context.request.post(`${base.origin}/api/admin/login`, { data: access });
  assert.equal(login.status(), 204, 'Fixture login failed');
  const sourceResponse = await context.request.get(`${base.origin}/api/admin/prisma/sources`);
  assert.ok(sourceResponse.ok());
  const state = await sourceResponse.json();
  assert.equal(state.runtime, 'local_fixture', 'Mutating smoke requires the explicit local fixture runtime');
  assert.ok(state.sources.every((source) => source.mode === 'simulation'), 'All tested sources must be simulated');
  fixtureVerified = true;
  if (state.sources.find(source => source.platform === 'x').capture_running) {
    assert.ok((await context.request.post(`${base.origin}/api/admin/prisma/sources/x/pause`)).ok());
  }
  checks.push('proxy authentication');

  await page.goto(`${base.origin}/admin/gods-eye-view`, { waitUntil: 'domcontentloaded' });
  const networkTabs = page.getByRole('tablist', { name: 'Social networks', exact: true });
  await networkTabs.waitFor();
  assert.deepEqual(await networkTabs.getByRole('tab').allTextContents(), ['X', 'Facebook', 'Instagram', 'TikTok']);
  await networkTabs.getByRole('tab', { name: 'X', exact: true }).click();
  await page.keyboard.press('ArrowRight');
  assert.equal(await networkTabs.getByRole('tab', { name: 'Facebook', exact: true }).getAttribute('aria-selected'), 'true');
  await page.keyboard.press('ArrowLeft');
  assert.equal(await page.getByRole('button', { name: 'Save', exact: true }).count(), 1);
  assert.equal(await page.getByRole('button', { name: 'Test', exact: true }).count(), 1);
  assert.equal(await page.getByRole('button', { name: 'Run now', exact: true }).count(), 1);
  checks.push('four accessible network tabs and one active configuration panel');

  assert.equal(await page.getByRole('button', { name: 'Start scenario', exact: true }).count(), 0);
  const sourceCard = page.locator('#prisma-panel-x .prisma-source');
  await sourceCard.getByRole('checkbox', { name: 'Source enabled', exact: true }).check();
  await sourceCard.getByLabel('Searches · one per line', { exact: true }).fill('#bogota #inundacion');
  const refreshed = page.waitForResponse(response => response.url().endsWith('/api/admin/prisma/sources') && response.request().method() === 'GET');
  await page.getByRole('button', { name: 'Refresh status', exact: true }).click();
  await refreshed;
  assert.equal(await sourceCard.getByLabel('Searches · one per line', { exact: true }).inputValue(), '#bogota #inundacion');
  await networkTabs.getByRole('tab', { name: 'Facebook', exact: true }).click();
  await networkTabs.getByRole('tab', { name: 'X', exact: true }).click();
  assert.equal(await sourceCard.getByLabel('Searches · one per line', { exact: true }).inputValue(), '#bogota #inundacion');
  const savedResponse = page.waitForResponse((response) => response.url().endsWith('/api/admin/prisma/sources/x') && response.request().method() === 'PUT');
  await sourceCard.getByRole('button', { name: 'Save', exact: true }).click();
  const saved = await savedResponse;
  assert.equal(saved.status(), 200);
  assert.equal((await saved.json()).capture_running, false, 'Save must not start capture');
  const runResponse = page.waitForResponse((response) => response.url().endsWith('/api/admin/prisma/sources/x/run'));
  await sourceCard.getByRole('button', { name: 'Run now', exact: true }).click();
  const started = await (await runResponse).json();
  assert.equal(started.source.capture_running, true);
  assert.ok(started.source.last_received_count > 0, 'Fixture run must capture matching records immediately');
  const stoppedResponse = page.waitForResponse(response => response.url().endsWith('/api/admin/prisma/sources/x/pause'));
  await sourceCard.getByRole('button', { name: 'Pause', exact: true }).click();
  assert.equal((await (await stoppedResponse).json()).source.capture_running, false);
  await sourceCard.locator('.prisma-capture-state').filter({ hasText: 'Paused' }).waitFor();
  checks.push('draft survives refresh and tabs; save does not start; run captures; explicit pause');

  const posts = page.locator('#prisma-panel-x .prisma-posts');
  await posts.locator('tbody tr').first().waitFor();
  const previewButton = posts.getByRole('button', { name: /^Preview publication by / }).first();
  await previewButton.click();
  const preview = page.getByRole('dialog', { name: 'Publication preview', exact: true });
  await preview.waitFor();
  assert.match(await preview.innerText(), /Synthetic fixture/);
  assert.equal(await preview.getByRole('link', { name: 'Open original publication ↗' }).count(), 0);
  assert.equal(await preview.locator('video[autoplay]').count(), 0);
  await page.keyboard.press('Escape');
  await preview.waitFor({ state: 'hidden' });
  assert.ok(await previewButton.evaluate(button => button === document.activeElement));
  await networkTabs.getByRole('tab', { name: 'Facebook', exact: true }).click();
  await page.reload({ waitUntil: 'domcontentloaded' });
  assert.equal(await page.getByRole('tab', { name: 'Facebook', exact: true }).getAttribute('aria-selected'), 'true');
  checks.push('publication table, accessible preview, honest synthetic provenance and selected-tab persistence');

  const popup = page.waitForEvent('popup');
  await page.getByRole('link', { name: 'Open God’s Eye View ↗', exact: true }).click();
  page = await popup;
  await page.waitForURL('**/gods-eye-view/');
  await page.locator('#incidents .incident').first().waitFor();
  await page.locator('#map canvas').waitFor({ state: 'visible' });
  assert.doesNotMatch(await page.locator('#map-status').innerText(), /cannot display the 3D map/);
  assert.ok((await page.locator('#map canvas').boundingBox()).width > 300);
  await page.waitForFunction(() => Number(document.getElementById('map').dataset.tilesLoaded) > 0);
  assert.ok(Math.abs(Number(await page.locator('#map').getAttribute('data-latitude')) - 4.66) < 0.01);
  assert.ok(Math.abs(Number(await page.locator('#map').getAttribute('data-longitude')) + 74.085) < 0.01);
  await page.getByRole('button', { name: 'Filter map area', exact: true }).click();
  assert.equal((await page.locator('input[name="bbox"]').inputValue()).split(',').length, 4);
  await page.getByRole('button', { name: 'Clear area', exact: true }).click();
  assert.equal(await page.locator('input[name="bbox"]').inputValue(), '');
  checks.push('Bogotá camera, real map tiles and map-area filter');
  await page.locator('#incidents .incident').filter({ hasText: 'Kennedy' }).first().click();
  await page.locator('#detail .evidence').first().waitFor();
  assert.match(await page.locator('#detail').innerText(), /Kennedy/);
  assert.equal(await page.locator('#detail .badge').first().innerText(), 'SIMULATED');
  assert.ok(await page.getByRole('button', { name: 'Validate', exact: true }).isEnabled());
  checks.push('GodEye canvas, selection and linked simulated evidence');

  assert.equal(await page.getByRole('combobox', { name: /^Origin/ }).count(), 0);
  assert.equal(await page.locator('#mode-status').count(), 0);
  assert.doesNotMatch(await page.locator('#incidents').innerText(), /SIMULATED|REAL/);
  checks.push('ingestion-neutral operating view retains provenance in evidence details');

  const snapshot = await (await context.request.get(`${base.origin}/api/prisma/snapshot`)).json();
  const selectedIncidentId = await page.locator('#detail').getAttribute('data-incident-id');
  const selectedIncident = snapshot.incidents.find((item) => item.id === selectedIncidentId);
  const incidentTime = Date.parse(selectedIncident.created_at);
  const minute = Math.floor(incidentTime / 60000) * 60000;
  const inBogota = (time) => new Date(time - 5 * 60 * 60 * 1000).toISOString().slice(0, 16);
  await page.getByLabel('From · Bogotá time', { exact: true }).fill(inBogota(minute));
  await page.getByLabel('To · Bogotá time', { exact: true }).fill(inBogota(minute + 60000));
  await page.getByLabel('To · Bogotá time', { exact: true }).blur();
  assert.ok(await page.locator('#incidents .incident').count() > 0);
  await page.getByLabel('From · Bogotá time', { exact: true }).fill(inBogota(minute + 120000));
  await page.getByLabel('From · Bogotá time', { exact: true }).blur();
  assert.equal(await page.locator('#incidents .incident').count(), 0);
  assert.match(await page.locator('#period-status').innerText(), /cannot be later/);
  await page.getByLabel('From · Bogotá time', { exact: true }).fill(inBogota(minute));
  await page.getByLabel('From · Bogotá time', { exact: true }).blur();
  await page.locator('#incidents .incident').filter({ hasText: 'Kennedy' }).first().click();
  checks.push('inclusive Bogotá period, invalid range and selection reset');

  const question = '¿Qué evidencia hay de inundación en Kennedy?';
  await page.getByLabel('Your question', { exact: true }).fill(question);
  assert.equal(await page.getByLabel('Questions submitted in this conversation', { exact: true }).innerText(), '0 questions submitted');
  const firstReply = page.waitForResponse((response) => response.url().endsWith('/api/prisma/chat') && response.status() === 200);
  await page.getByRole('button', { name: 'Send question', exact: true }).click();
  const first = await (await firstReply).json();
  await page.locator('.reply').first().waitFor();
  assert.equal(await page.getByLabel('Questions submitted in this conversation', { exact: true }).innerText(), '1 question submitted');
  assert.match(await page.locator('.reply').first().innerText(), /Kennedy/);
  assert.match(await page.locator('.reply').first().innerText(), /DEMOSTRACIÓN LOCAL/);
  await page.locator('.reply summary').first().click();
  await page.locator('.reply .evidence').first().waitFor({ state: 'visible' });
  assert.ok(first.evidence_ids.length > 0);
  for (const id of first.evidence_ids) {
    assert.ok(selectedIncident.evidence_ids.includes(id), 'Agent citation must belong to the selected incident');
    assert.ok((await page.locator('.reply .evidence').allTextContents()).some((value) => value.includes(id)), 'Cited evidence must be inspectable');
  }
  await page.getByRole('button', { name: 'Focus event on map', exact: true }).first().click();
  assert.equal(await page.locator('#incidents .incident[aria-pressed="true"]').count(), 1);
  checks.push('Kennedy question, cited evidence and explicit map action');

  const followupText = '¿Qué evidencia lo respalda?';
  await page.getByLabel('Your question', { exact: true }).fill(followupText);
  const followupReply = page.waitForResponse((response) => response.url().endsWith('/api/prisma/chat') && response.status() === 200);
  await page.getByRole('button', { name: 'Send question', exact: true }).click();
  const followup = await (await followupReply).json();
  assert.equal(followup.session_id, first.session_id);
  assert.equal(followup.version, first.version);
  assert.deepEqual(followup.evidence_ids, first.evidence_ids);
  await page.locator('.reply').nth(1).waitFor();
  assert.equal(await page.getByLabel('Questions submitted in this conversation', { exact: true }).innerText(), '2 questions submitted');
  checks.push('follow-up keeps session, version and evidence context');
  await page.getByLabel('Your question', { exact: true }).fill(question);

  await page.route('**/api/prisma/chat', async (route) => {
    const data = route.request().postDataJSON();
    await route.continue({ postData: JSON.stringify({ ...data, version: 'deliberately-stale' }) });
  }, { times: 1 });
  await page.getByRole('button', { name: 'Send question', exact: true }).click();
  await page.locator('#conversation [role="alert"]').last().waitFor();
  assert.match(await page.locator('#conversation [role="alert"]').last().innerText(), /publication changed/);
  assert.equal(await page.getByLabel('Your question', { exact: true }).inputValue(), question);
  assert.equal(await page.getByLabel('Questions submitted in this conversation', { exact: true }).innerText(), '3 questions submitted');
  checks.push('real backend 409 retains the question');

  await page.route('**/api/prisma/chat', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.continue().catch(() => {});
  }, { times: 1 });
  await page.getByRole('button', { name: 'Send question', exact: true }).click();
  assert.equal(await page.getByRole('button', { name: 'Sending question', exact: true }).getAttribute('aria-busy'), 'true');
  assert.equal(await page.getByRole('button', { name: 'Sending question', exact: true }).isDisabled(), true);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(await page.locator('#chat-status').innerText(), 'Request cancelled.');
  assert.ok(await page.getByRole('button', { name: 'Send question', exact: true }).isEnabled());
  assert.equal(await page.getByLabel('Questions submitted in this conversation', { exact: true }).innerText(), '4 questions submitted');
  assert.equal(await page.locator('.reply').count(), 2);
  checks.push('cancel pending real request without an invented reply');

  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#question').scrollIntoViewIfNeeded();
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'Mobile layout overflows horizontally');
  assert.ok(await page.evaluate(() => {
    const input = document.getElementById('question');
    const box = input.getBoundingClientRect();
    const send = document.getElementById('send').getBoundingClientRect();
    const style = getComputedStyle(input);
    return send.left >= box.left && send.right <= box.right && send.top >= box.top && send.bottom <= box.bottom
      && Number.parseFloat(style.paddingBottom) >= box.bottom - send.top;
  }), 'Send button must stay inside the composer with reserved text space');
  checks.push('mobile DOM fit, circular composer control and question count');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('#question-count').waitFor();
  assert.equal(await page.getByLabel('Questions submitted in this conversation', { exact: true }).innerText(), '0 questions submitted');
  assert.equal(await page.locator('.reply').count(), 0);
  checks.push('new page conversation starts empty with a zero question count');
  const localFailures = requests.filter((item) => item.origin === base.origin && !/ERR_ABORTED/.test(item.error || ''));
  assert.deepEqual(localFailures, [], 'Local requests failed');
  assert.deepEqual(errors, [], 'Browser execution errors');
  const localConsole = consoleErrors.filter((item) => item.origin === base.origin && !(item.path === '/api/prisma/chat' && item.text.includes('409')));
  assert.deepEqual(localConsole, [], 'Unexpected local console error');
  console.log(JSON.stringify({ status: 'passed', checks, browser: 'Chromium DOM only', pageErrors: errors.length,
    localFailures: localFailures.length, unexpectedLocalConsoleErrors: localConsole.length,
    externalConsoleErrors: consoleErrors.filter((item) => item.origin !== base.origin).length,
    externalFailures: requests.filter((item) => item.origin !== base.origin), screenshots: 0 }));
} finally {
  if (fixtureVerified) await context.request.post(`${base.origin}/api/admin/prisma/sources/x/pause`).catch(() => {});
  await context.close();
  await browser.close();
}
