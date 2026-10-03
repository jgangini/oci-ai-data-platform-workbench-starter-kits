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
const page = await context.newPage();
page.setDefaultTimeout(20000);
const errors = [];
const requests = [];
const consoleErrors = [];
const checks = [];
page.on('pageerror', (error) => errors.push(error.message));
page.on('console', (message) => {
  if (message.type() !== 'error') return;
  const location = message.location().url;
  consoleErrors.push({ origin: location ? new URL(location).origin : '', path: location ? new URL(location).pathname : '', text: message.text() });
});
page.on('requestfailed', (request) => {
  const url = new URL(request.url());
  requests.push({ origin: url.origin, path: url.pathname, error: request.failure()?.errorText });
});
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
  checks.push('proxy authentication');

  await page.goto(`${base.origin}/admin/prisma`, { waitUntil: 'domcontentloaded' });
  await page.locator('.prisma-source').last().waitFor();
  assert.deepEqual(await page.locator('.prisma-source-heading h2').allTextContents(), ['X', 'Facebook', 'Instagram', 'TikTok']);
  assert.equal(await page.getByRole('link', { name: 'Territorial Control', exact: true }).getAttribute('aria-current'), 'page');
  assert.equal(await page.getByRole('button', { name: 'Save', exact: true }).count(), 4);
  assert.equal(await page.getByRole('button', { name: 'Test', exact: true }).count(), 4);
  assert.equal(await page.getByRole('button', { name: 'Run now', exact: true }).count(), 4);
  checks.push('four source controls and admin navigation');

  await page.getByRole('button', { name: 'Restart simulation', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.prisma-runtime')?.textContent.includes('idle'));
  await page.getByRole('button', { name: 'Start scenario', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.prisma-runtime')?.textContent.includes('running'));
  await page.getByRole('button', { name: 'Pause', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.prisma-runtime')?.textContent.includes('paused'));
  assert.ok(await page.getByRole('button', { name: 'Resume', exact: true }).isEnabled());
  checks.push('reset, start and pause through DOM');

  await page.getByRole('link', { name: 'Open God’s Eye View' }).click();
  await page.waitForURL('**/prisma/');
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
  await page.locator('#incidents .incident').first().click();
  await page.locator('#detail .evidence').first().waitFor();
  assert.match(await page.locator('#detail').innerText(), /Kennedy/);
  assert.equal(await page.locator('#detail .badge').first().innerText(), 'SIMULATED');
  assert.ok(await page.getByRole('button', { name: 'Validate', exact: true }).isEnabled());
  checks.push('GodEye canvas, selection and linked simulated evidence');

  await page.getByRole('combobox', { name: /^Origin/ }).selectOption('real');
  assert.equal(await page.locator('#incidents .incident').count(), 0);
  await page.getByRole('combobox', { name: /^Origin/ }).selectOption('simulation');
  assert.ok(await page.locator('#incidents .incident').count() > 0);
  checks.push('REAL and SIMULADO filters');

  const snapshot = await (await context.request.get(`${base.origin}/api/prisma/snapshot`)).json();
  const incidentTime = Date.parse(snapshot.incidents.find((item) => item.locality === 'Kennedy').created_at);
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
  await page.locator('#incidents .incident').first().click();
  checks.push('inclusive Bogotá period, invalid range and selection reset');

  const question = '¿Qué evidencia hay de inundación en Kennedy?';
  await page.getByLabel('Your question', { exact: true }).fill(question);
  const firstReply = page.waitForResponse((response) => response.url().endsWith('/api/prisma/chat') && response.status() === 200);
  await page.getByRole('button', { name: 'Ask agent ↗', exact: true }).click();
  const first = await (await firstReply).json();
  await page.locator('.reply').first().waitFor();
  assert.match(await page.locator('.reply').first().innerText(), /Kennedy/);
  assert.match(await page.locator('.reply').first().innerText(), /SIMULADO/);
  await page.locator('.reply summary').first().click();
  await page.locator('.reply .evidence').first().waitFor({ state: 'visible' });
  assert.match(await page.locator('.reply .evidence').first().innerText(), /x:lluvia-1/);
  await page.getByRole('button', { name: 'Focus event on map', exact: true }).first().click();
  assert.equal(await page.locator('#incidents .incident[aria-pressed="true"]').count(), 1);
  checks.push('Kennedy question, cited evidence and explicit map action');

  const followupText = '¿Qué evidencia lo respalda?';
  await page.getByLabel('Your question', { exact: true }).fill(followupText);
  const followupReply = page.waitForResponse((response) => response.url().endsWith('/api/prisma/chat') && response.status() === 200);
  await page.getByRole('button', { name: 'Ask agent ↗', exact: true }).click();
  const followup = await (await followupReply).json();
  assert.equal(followup.session_id, first.session_id);
  assert.equal(followup.version, first.version);
  assert.deepEqual(followup.evidence_ids, first.evidence_ids);
  await page.locator('.reply').nth(1).waitFor();
  checks.push('follow-up keeps session, version and evidence context');
  await page.getByLabel('Your question', { exact: true }).fill(question);

  await page.route('**/api/prisma/chat', async (route) => {
    const data = route.request().postDataJSON();
    await route.continue({ postData: JSON.stringify({ ...data, version: 'deliberately-stale' }) });
  }, { times: 1 });
  await page.getByRole('button', { name: 'Ask agent ↗', exact: true }).click();
  await page.locator('#conversation [role="alert"]').last().waitFor();
  assert.match(await page.locator('#conversation [role="alert"]').last().innerText(), /publication changed/);
  assert.equal(await page.getByLabel('Your question', { exact: true }).inputValue(), question);
  checks.push('real backend 409 retains the question');

  await page.route('**/api/prisma/chat', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.continue().catch(() => {});
  }, { times: 1 });
  await page.getByRole('button', { name: 'Ask agent ↗', exact: true }).click();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(await page.locator('#chat-status').innerText(), 'Request cancelled.');
  assert.ok(await page.getByRole('button', { name: 'Ask agent ↗', exact: true }).isEnabled());
  assert.equal(await page.locator('.reply').count(), 2);
  checks.push('cancel pending real request without an invented reply');

  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#question').scrollIntoViewIfNeeded();
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'Mobile layout overflows horizontally');
  checks.push('mobile DOM fit');
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
  await context.close();
  await browser.close();
}
