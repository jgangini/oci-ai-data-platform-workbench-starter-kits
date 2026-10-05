import test from 'node:test';
import assert from 'node:assert/strict';
import { createApplicationCatalog } from '../.upstream/src/app/constructCatalog.js';
import { createSurfaceServices } from '../.upstream/src/app/surfaceServices.js';
import { createStandaloneLayerSources } from '../.upstream/src/standalone/layerSources.js';
import { createLayerCatalog } from '../.upstream/src/app/catalog.js';

test('sanitized native catalog constructs and accepts the AIDP layer without unmatched metadata', () => {
  const lifetime = new AbortController();
  try {
    const catalog = createApplicationCatalog({
      sources: createStandaloneLayerSources(), signal: lifetime.signal,
      surface: createSurfaceServices({ terrainSource: { getHeights: async () => [] },
        signal: lifetime.signal, eventTarget: null }),
    });
    assert.equal(catalog.layers.length, catalog.metadata.length);
    for (const id of ['bhote-koshi-2026', 'bhote-koshi-locator']) assert.equal(catalog.get(id), undefined);
    const aidp = { id: 'territorial-events' };
    const extended = createLayerCatalog([...catalog.layers, aidp],
      [...catalog.metadata, { id: aidp.id, disposition: 'local-only' }]);
    assert.equal(extended.get(aidp.id), aidp);
    for (const layer of catalog.layers) assert.equal(extended.get(layer.id), layer);
  } finally {
    lifetime.abort();
  }
});
