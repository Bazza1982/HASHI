const os = require('node:os');

const INSTANCE_SUFFIX = process.env.HASHI_BRIDGE_API_PORT || '18800';
const STABLE_WSL_HOST = '10.255.255.254';
const interfaceAddresses = Object.values(os.networkInterfaces()).flat().filter(Boolean);
const bridgeHost = process.env.HASHI_BRIDGE_API_HOST
  || (interfaceAddresses.some((entry) => entry.address === STABLE_WSL_HOST)
    ? STABLE_WSL_HOST
    : '127.0.0.1');
const bridgeApi = process.env.BRIDGE_U_API
  || `http://${bridgeHost}:${process.env.HASHI_BRIDGE_API_PORT || '18800'}`;

module.exports = {
  apps: [
    {
      name: `workbench-backend-${INSTANCE_SUFFIX}`,
      script: 'server/index.js',
      cwd: __dirname,
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      env: {
        NODE_ENV: 'production',
        PORT: process.env.HASHI_SERVER_PORT || '3001',
        BRIDGE_U_API: bridgeApi
      }
    },
    {
      name: `workbench-frontend-${INSTANCE_SUFFIX}`,
      script: 'node_modules/vite/bin/vite.js',
      args: '--host',
      cwd: __dirname,
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      env: {
        NODE_ENV: 'production',
        HASHI_CLIENT_PORT: process.env.HASHI_CLIENT_PORT || '5173',
        HASHI_SERVER_PORT: process.env.HASHI_SERVER_PORT || '3001'
      }
    }
  ]
};
