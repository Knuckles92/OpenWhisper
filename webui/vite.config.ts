import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The built app is served by the Python meeting server:
//   GET /m/{token}  -> dist/index.html
//   GET /assets/*   -> dist/assets/*
// base is './' per the packaging contract; index.html carries <base href="/">
// so relative asset URLs resolve to /assets/* regardless of the /m/{token} path.
export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
    emptyOutDir: true,
  },
  server: {
    // Dev only: point at a locally running meeting server (port is ephemeral
    // in production; fix it with the meeting_server_port setting when
    // developing against a live backend).
    proxy: {
      '/api': 'http://localhost:8765',
      '/ws': { target: 'ws://localhost:8765', ws: true },
    },
  },
});
