module.exports = {
  apps: [
    {
      name: 'workbench-backend',
      script: 'server/index.js',
      cwd: __dirname,
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      env: {
        NODE_ENV: 'production'
      }
    },
    {
      name: 'workbench-frontend',
      script: 'node_modules/vite/bin/vite.js',
      args: '--host',
      cwd: __dirname,
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      env: {
        NODE_ENV: 'production'
      }
    }
  ]
};
