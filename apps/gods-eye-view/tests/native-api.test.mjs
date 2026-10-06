import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { viewerApiPath } from '../src/api.js';

test('both browser entry points work through the installed old proxy without retrying mutations', async () => {
  for (const entry of ['native/main.js', 'src/main.js']) {
    const source = await readFile(new URL('../' + entry, import.meta.url), 'utf8');
    const calls = [];
    const request = runInNewContext('(' + source.match(/^async function request\([\s\S]*?^}/m)[0] + ')', {
      viewerApiPath, AbortSignal,
      fetch: async (path, options) => {
        calls.push({ path, options });
        // Frozen old nginx forwards only these API prefixes to the viewer.
        const ok = /^\/api\/(territorial|prisma)\//.test(path) && !path.endsWith('/missing');
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
      assert.equal(calls.at(-1).path, '/api/prisma/' + suffix);
      assert.equal(calls.at(-1).options.body, options.body);
      assert.equal(calls.at(-1).options.credentials, 'same-origin');
    }
    await assert.rejects(request('/api/gods-eye-view/missing', { method: 'POST', body: '{}' }), { message: 'missing' });
    assert.equal(calls.length, 5);
  }
  assert.equal(viewerApiPath('/api/gods-eye-view/media/post-1/image-1.png?v=1'), '/api/prisma/media/post-1/image-1.png?v=1');
  for (const path of ['/api/setup/browser', '/api/territorial/snapshot', '/api/prisma/snapshot', '/api/gods-eye-viewer/snapshot', 'https://example.com/api/gods-eye-view/image.png']) {
    assert.equal(viewerApiPath(path), path);
  }
});
