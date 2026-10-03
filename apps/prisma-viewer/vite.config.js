import { defineConfig } from 'vite';

export default defineConfig({
  base: '/prisma/',
  define: { CESIUM_BASE_URL: JSON.stringify('/prisma/cesium/') },
  server: { proxy: { '/api': 'http://localhost:8081' } },
});
