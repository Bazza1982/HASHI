#!/usr/bin/env node
const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const HASHI_ROOT = __dirname;
const REQUIREMENTS = path.join(HASHI_ROOT, 'requirements.txt');

console.log('\n🌸 HASHI Post-Install Check\n');

// Check Python
let pythonCmd = null;
for (const cmd of ['python3', 'python']) {
  try {
    const version = execSync(`${cmd} --version`, { encoding: 'utf8' });
    const match = version.match(/Python (\d+)\.(\d+)/);
    if (match) {
      const major = parseInt(match[1]);
      const minor = parseInt(match[2]);
      if (major === 3 && minor >= 10) {
        pythonCmd = cmd;
        console.log(`✅ Found ${version.trim()}`);
        break;
      }
    }
  } catch (e) {
    continue;
  }
}

if (!pythonCmd) {
  console.error('❌ Python 3.10+ is required but not found.');
  console.error('   Install from: https://www.python.org/downloads/\n');
  process.exit(1);
}

// Check pip
try {
  execSync(`${pythonCmd} -m pip --version`, { stdio: 'pipe' });
  console.log('✅ pip is available');
} catch (e) {
  console.error('❌ pip is not installed.');
  console.error('   Install pip: https://pip.pypa.io/en/stable/installation/\n');
  process.exit(1);
}

// Prompt to install Python dependencies
console.log('\n📦 Python dependencies required.');
console.log('   Run the following command to install:');
console.log(`   ${pythonCmd} -m pip install -r ${REQUIREMENTS}\n`);

console.log('🚀 HASHI is ready!');
console.log('   Run `hashi-onboard` to set up your first agent.\n');
