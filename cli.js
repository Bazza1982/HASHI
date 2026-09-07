#!/usr/bin/env node
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const HASHI_ROOT = __dirname;
const MAIN_PY = path.join(HASHI_ROOT, 'main.py');
const RUNTIME_CHECK = path.join(HASHI_ROOT, 'scripts', 'check_runtime_contract.py');

// Check if Python is available (synchronous)
function checkPython() {
  const candidates = [
    { command: 'python3.12', prefix: [] },
    { command: 'py', prefix: ['-3.12'] },
    { command: 'python3', prefix: [] },
    { command: 'python', prefix: [] },
  ];
  for (const candidate of candidates) {
    try {
      const result = spawnSync(
        candidate.command,
        [...candidate.prefix, RUNTIME_CHECK, '--code-root', HASHI_ROOT],
        { stdio: 'pipe' }
      );
      if (result.status === 0) {
        return candidate;
      }
    } catch (e) {
      continue;
    }
  }
  console.error('❌ The approved CPython 3.12.13 runtime and dependency lock are required.');
  console.error('Please install Python from https://www.python.org/downloads/');
  process.exit(1);
}

// Launch HASHI
const python = checkPython();
const args = process.argv.slice(2);

console.log('🌸 Launching HASHI...');

const child = spawn(python.command, [...python.prefix, MAIN_PY, ...args], {
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
