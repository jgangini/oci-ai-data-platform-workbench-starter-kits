import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

test('both browser entry points use canonical routes without retrying mutations', async () => {
  for (const entry of ['native/main.js', 'src/main.js']) {
    const source = await readFile(new URL('../' + entry, import.meta.url), 'utf8');
    const calls = [];
    const request = runInNewContext('(' + source.match(/^async function request\([\s\S]*?^}/m)[0] + ')', {
      AbortSignal,
      fetch: async (path, options) => {
        calls.push({ path, options });
        const ok = /^\/api\/gods-eye-view\//.test(path) && !path.endsWith('/missing');
        return { ok, status: ok ? 200 : 404, headers: new Headers(), json: async () => ({ detail: ok ? 'ok' : 'missing' }) };
      },
    });
    for (const [suffix, options] of [
      ['snapshot?locality=Kennedy', {}],
      ['chat', { method: 'POST', body: '{"question":"test"}' }],
      ['oci-voice/turn', { method: 'POST', body: '{"audio_base64":"fixture"}' }],
      ['incidents/flood/review', { method: 'POST', body: '{"status":"validated"}' }],
    ]) {
      const count = calls.length;
      assert.equal((await request('/api/gods-eye-view/' + suffix, options)).detail, 'ok');
      assert.equal(calls.length, count + 1);
      assert.equal(calls.at(-1).path, '/api/gods-eye-view/' + suffix);
      assert.equal(calls.at(-1).options.body, options.body);
      assert.equal(calls.at(-1).options.credentials, 'same-origin');
    }
    await assert.rejects(request('/api/gods-eye-view/missing', { method: 'POST', body: '{}' }), { message: 'missing' });
    assert.equal(calls.length, 5);
    for (const retired of ['prisma', 'territorial']) {
      await assert.rejects(request('/api/' + retired + '/snapshot'), { message: 'missing' });
    }
    assert.equal(calls.length, 7);
  }
  for (const [entry, assignment] of [['native/godsEyeViewLayer.js', 'image.src = media.url'], ['src/main.js', 'image.src = photo.url']]) {
    const source = await readFile(new URL('../' + entry, import.meta.url), 'utf8');
    assert.ok(source.includes(assignment));
    assert.ok(!source.includes('viewerApiPath'));
  }
});
