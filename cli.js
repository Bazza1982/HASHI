#!/usr/bin/env node
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const HASHI_ROOT = __dirname;
const MAIN_PY = path.join(HASHI_ROOT, 'main.py');

// Check if Python is available (synchronous)
function checkPython() {
  const pythonCommands = ['python3', 'python'];
  for (const cmd of pythonCommands) {
    try {
      const result = spawnSync(cmd, ['--version'], { stdio: 'pipe' });
      if (result.status === 0) {
        return cmd;
      }
    } catch (e) {
      continue;
    }
  }
  console.error('❌ Python 3.10+ is required but not found.');
  console.error('Please install Python from https://www.python.org/downloads/');
  process.exit(1);
}

// Launch HASHI
const python = checkPython();
const args = process.argv.slice(2);

console.log('🌸 Launching HASHI...');

const child = spawn(python, [MAIN_PY, ...args], {
  cwd: HASHI_ROOT,
  stdio: 'inherit',
  env: { ...process.env, PYTHONUNBUFFERED: '1' }
});

child.on('exit', (code) => {
  process.exit(code || 0);
});

process.on('SIGINT', () => {
  child.kill('SIGINT');
});

process.on('SIGTERM', () => {
  child.kill('SIGTERM');
});
