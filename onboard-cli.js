#!/usr/bin/env node
const { spawn } = require('child_process');
const path = require('path');

const HASHI_ROOT = __dirname;
const ONBOARD_PY = path.join(HASHI_ROOT, 'onboarding', 'onboarding_main.py');

// Check if Python is available
function checkPython() {
  const pythonCommands = ['python3', 'python'];
  for (const cmd of pythonCommands) {
    try {
      const result = spawn(cmd, ['--version'], { stdio: 'pipe' });
      result.on('exit', (code) => {
        if (code === 0) return cmd;
      });
    } catch (e) {
      continue;
    }
  }
  console.error('❌ Python 3.10+ is required but not found.');
  console.error('Please install Python from https://www.python.org/downloads/');
  process.exit(1);
}

// Launch HASHI Onboarding
const python = checkPython();

console.log('🌸 Launching HASHI Onboarding...');

const child = spawn(python, [ONBOARD_PY], {
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
