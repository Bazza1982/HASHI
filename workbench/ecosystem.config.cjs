const { resolveBridgeEndpoint } = require('./endpoint_config.cjs');

const bridgeEndpoint = resolveBridgeEndpoint({ root: require('node:path').join(__dirname, '..') });
const INSTANCE_SUFFIX = String(bridgeEndpoint.port);
const bridgeApi = bridgeEndpoint.baseUrl;

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
