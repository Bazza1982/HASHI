import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const clientPort = parseInt(process.env.HASHI_CLIENT_PORT || '5173');
const serverPort = parseInt(process.env.HASHI_SERVER_PORT || '3001');

export default defineConfig({
  plugins: [react()],
  server: {
    port: clientPort,
    proxy: {
      '/api': {
        target: `http://localhost:${serverPort}`,
        changeOrigin: true,
      },
    },
  },
});
