#!/usr/bin/env node
const { spawnSync } = require('child_process');
const path = require('path');

const HASHI_ROOT = __dirname;
const LOCK = path.join(HASHI_ROOT, 'constraints', 'standard-py312.lock');
const RUNTIME_CHECK = path.join(HASHI_ROOT, 'scripts', 'check_runtime_contract.py');

console.log('\n🌸 HASHI Post-Install Check\n');

// Check Python
let python = null;
for (const candidate of [
  { command: 'python3.12', prefix: [] },
  { command: 'py', prefix: ['-3.12'] },
  { command: 'python3', prefix: [] },
  { command: 'python', prefix: [] },
]) {
  const checked = spawnSync(
    candidate.command,
    [...candidate.prefix, RUNTIME_CHECK, '--code-root', HASHI_ROOT, '--runtime-only'],
    { encoding: 'utf8' }
  );
  if (checked.status === 0) {
    python = candidate;
    const version = spawnSync(
      candidate.command,
      [...candidate.prefix, '--version'],
      { encoding: 'utf8' }
    );
    console.log(`✅ Found ${(version.stdout || version.stderr || '').trim()}`);
    break;
  }
}

if (!python) {
  console.error('❌ The approved CPython 3.12.13 runtime is required.');
  console.error('   Install from: https://www.python.org/downloads/\n');
  process.exit(1);
}

const pip = spawnSync(
  python.command,
  [...python.prefix, '-m', 'pip', '--version'],
  { stdio: 'pipe' }
);
if (pip.status === 0) {
  console.log('✅ pip is available');
} else {
  console.error('❌ pip is not installed.');
  console.error('   Install pip: https://pip.pypa.io/en/stable/installation/\n');
  process.exit(1);
}

// Prompt to install Python dependencies
console.log('\n📦 Python dependencies required.');
console.log('   Run the following command to install:');
console.log(
  `   ${python.command} ${python.prefix.join(' ')} -m pip install -r "${LOCK}"\n`
);

console.log('🚀 HASHI is ready!');
console.log('   Run `hashi-onboard` to set up your first agent.\n');
