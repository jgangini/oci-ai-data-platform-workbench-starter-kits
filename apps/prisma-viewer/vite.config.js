import { defineConfig } from 'vite';

export default defineConfig({
  base: '/gods-eye-view/',
  define: { CESIUM_BASE_URL: JSON.stringify('/gods-eye-view/cesium/') },
  server: { proxy: { '/api': 'http://localhost:8081' } },
});
