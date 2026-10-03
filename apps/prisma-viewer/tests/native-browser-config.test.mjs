import test from 'node:test';
import assert from 'node:assert/strict';
import { browserConfiguration } from '../native/vite.config.mjs';

test('runtime browser configuration exposes only explicitly browser-safe provider keys', () => {
  assert.deepEqual(browserConfiguration({}), { googleApiKey: '', cesiumToken: '' });
  const environment = {
    GOOGLE_MAPS_API_KEY: 'browser-google', CESIUM_ION_TOKEN: 'browser-cesium',
    GOOGLE_MAPS_SERVER_API_KEY: 'private-google', OPENAI_API_KEY: 'private-openai',
    OCI_PRIVATE_KEY: 'private-oci', PRISMA_ADMIN_URL: 'private-bridge',
  };
  assert.deepEqual(browserConfiguration(environment), {
    googleApiKey: 'browser-google', cesiumToken: 'browser-cesium',
  });
});
