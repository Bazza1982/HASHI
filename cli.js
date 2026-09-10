#!/usr/bin/env node
'use strict';

const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const HASHI_ROOT = __dirname;
const PACKAGE = require(path.join(HASHI_ROOT, 'package.json'));
const RUNTIME_CHECK = path.join(HASHI_ROOT, 'scripts', 'check_runtime_contract.py');
const INSTANCE_CLI = path.join(HASHI_ROOT, 'scripts', 'hashi_instance_cli.py');

const HELP_PAGES = require('./scripts/terminal-help.json');
const HELP = HELP_PAGES[''];

function dataRoot() {
  if (process.env.HASHI_DATA_ROOT) return path.resolve(process.env.HASHI_DATA_ROOT);
  if (process.platform === 'win32') {
    return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'HASHI');
  }
  return path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'), 'hashi');
}

function preparedRuntimeCandidate() {
  const versionRoot = path.join(dataRoot(), 'runtimes', PACKAGE.version);
  const pointer = path.join(versionRoot, 'active.json');
  try {
    const payload = JSON.parse(fs.readFileSync(pointer, 'utf8'));
    if (
      payload.schema_version !== 1 ||
      payload.program_version !== PACKAGE.version ||
      typeof payload.python !== 'string'
    ) return '';
    const root = path.resolve(versionRoot);
    const candidate = path.resolve(payload.python);
    const relative = path.relative(root, candidate);
    if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return '';
    return fs.existsSync(candidate) ? candidate : '';
  } catch (_) {
    return '';
  }
}

function pythonCandidates() {
  const candidates = [];
  if (process.env.HASHI_PYTHON) candidates.push({ command: process.env.HASHI_PYTHON, prefix: [] });
  const prepared = preparedRuntimeCandidate();
  if (prepared) candidates.push({ command: prepared, prefix: [] });
  candidates.push({ command: 'python3.12', prefix: [] });
  if (process.platform === 'win32') candidates.push({ command: 'py', prefix: ['-3.12'] });
  candidates.push(
    { command: 'python3', prefix: [] },
    { command: 'python', prefix: [] },
  );
  const seen = new Set();
  return candidates.filter((candidate) => {
    const key = JSON.stringify([candidate.command, candidate.prefix]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function sameEnvironmentCommand(command) {
  const value = String(command || '');
  if (process.platform !== 'win32' && value.toLowerCase().endsWith('.exe')) return false;
  if (process.platform === 'win32') {
    const lowered = value.toLowerCase();
    if (lowered.startsWith('\\\\wsl$\\') || lowered.startsWith('\\\\wsl.localhost\\')) return false;
  }
  return true;
}

function selectManagementPython() {
  for (const candidate of pythonCandidates()) {
    if (!sameEnvironmentCommand(candidate.command)) continue;
    try {
      const result = spawnSync(
        candidate.command,
        [...candidate.prefix, RUNTIME_CHECK, '--code-root', HASHI_ROOT, '--runtime-only'],
        { stdio: 'ignore', windowsHide: true },
      );
      if (result.status === 0) return candidate;
    } catch (_) {
      // Continue through the bounded candidate list.
    }
  }
  return null;
}

function normalizeGlobalArguments(argv) {
  const globals = [];
  const rest = [];
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (['--instance', '-i', '--lang'].includes(value)) {
      if (index + 1 >= argv.length) return argv;
      globals.push(value === '-i' ? '--instance' : value, argv[index + 1]);
      index += 1;
    } else if (value.startsWith('--instance=')) {
      globals.push('--instance', value.slice('--instance='.length));
    } else if (['--json', '--non-interactive', '--no-color', '--verbose'].includes(value)) {
      globals.push(value);
    } else {
      rest.push(value);
    }
  }
  return [...globals, ...rest];
}

function run(argv = process.argv.slice(2)) {
  const normalized = normalizeGlobalArguments(argv);
  const versionOnly = argv.length === 1 && ['version', '--version'].includes(argv[0]);
  if (versionOnly) {
    process.stdout.write(`HASHI ${PACKAGE.version}\n`);
    return 0;
  }
  let language = Intl.DateTimeFormat().resolvedOptions().locale.startsWith('zh') ? 'zh' : 'en';
  const helpArgv = [];
  for (let i=0; i<argv.length; i++) {
    if (argv[i] === '--lang') { language = argv[++i]; }
    else if (argv[i].startsWith('--lang=')) { language = argv[i].split('=')[1]; }
    else { helpArgv.push(argv[i]); }
  }
  const helpIndex = helpArgv.findIndex(x => ['help', '--help', '-h'].includes(x));
  if (helpIndex >= 0 && !argv.includes('--json')) {
    let topic = helpArgv[helpIndex] === 'help' ? helpArgv.slice(helpIndex + 1) : helpArgv.slice(0, helpIndex);
    topic = topic.filter(x => x !== '--all');
    const key = (language === 'zh' ? 'zh:' : '') + topic.join(' ');
    if (Object.hasOwn(HELP_PAGES, key)) {
      process.stdout.write(HELP_PAGES[key]);
      return 0;
    }
  }
  const python = selectManagementPython();
  if (!python) {
    const error = { code: 'RUNTIME_INCOMPLETE', message: 'Approved CPython 3.12.13 is unavailable.', next_step: 'Install the required runtime and prepare HASHI dependencies.' };
    if (argv.includes('--json')) {
      process.stdout.write(JSON.stringify({schema_version:1,ok:false,command:argv.find(x => !x.startsWith('-')) || 'tui',instance:null,exit_code:78,data:null,warnings:[],error,effects:{steps:[],unknown:[]}}) + '\n');
    } else {
      process.stderr.write(`${error.code}: ${error.message}\n${error.next_step}\n`);
      if (argv.includes('doctor')) process.stdout.write(JSON.stringify({entry:__filename,platform:process.platform,node:process.version,path:process.env.PATH}) + '\n');
    }
    return 78;
  }
  const child = spawn(
    python.command,
    [...python.prefix, INSTANCE_CLI, ...normalizeGlobalArguments(argv)],
    {
      cwd: process.cwd(),
      stdio: 'inherit',
      windowsHide: true,
      env: {
        ...process.env,
        HASHI_PROGRAM_ROOT: HASHI_ROOT,
        HASHI_PROGRAM_VERSION: PACKAGE.version,
        HASHI_INVOCATION_CWD: process.cwd(),
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1',
      },
    },
  );
  child.on('exit', (code, signalName) => {
    if (signalName) {
      process.kill(process.pid, signalName);
      return;
    }
    process.exit(code === null ? 1 : code);
  });
  child.on('error', (error) => {
    process.stderr.write(`HASHI command failed to start: ${error.message}\n`);
    process.exit(78);
  });
  for (const signalName of ['SIGINT', 'SIGTERM']) {
    process.on(signalName, () => child.kill(signalName));
  }
  return null;
}

if (require.main === module) {
  const result = run();
  if (Number.isInteger(result)) process.exit(result);
}

module.exports = { HELP, dataRoot, normalizeGlobalArguments, run, sameEnvironmentCommand };
