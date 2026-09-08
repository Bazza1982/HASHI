#!/usr/bin/env node
'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const HASHI_ROOT = __dirname;
const PACKAGE = require(path.join(HASHI_ROOT, 'package.json'));
const LOCK = path.join(HASHI_ROOT, 'constraints', 'standard-py312.lock');
const RUNTIME_CHECK = path.join(HASHI_ROOT, 'scripts', 'check_runtime_contract.py');

function dataRoot() {
  if (process.env.HASHI_DATA_ROOT) return path.resolve(process.env.HASHI_DATA_ROOT);
  if (process.platform === 'win32') {
    return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'HASHI');
  }
  return path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'), 'hashi');
}

function candidates() {
  const result = [];
  if (process.env.HASHI_PYTHON) result.push({ command: process.env.HASHI_PYTHON, prefix: [] });
  result.push({ command: 'python3.12', prefix: [] });
  if (process.platform === 'win32') result.push({ command: 'py', prefix: ['-3.12'] });
  result.push(
    { command: 'python3', prefix: [] },
    { command: 'python', prefix: [] },
  );
  return result;
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

function checkRuntime(candidate, full) {
  const args = [...candidate.prefix, RUNTIME_CHECK, '--code-root', HASHI_ROOT];
  if (!full) args.push('--runtime-only');
  const result = spawnSync(candidate.command, args, {
    stdio: 'ignore',
    windowsHide: true,
    timeout: 60_000,
  });
  return result.status === 0;
}

function preparedPython(buildRoot) {
  return process.platform === 'win32'
    ? path.join(buildRoot, 'Scripts', 'python.exe')
    : path.join(buildRoot, 'bin', 'python');
}

function atomicJson(target, payload) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const temporary = `${target}.${process.pid}.${crypto.randomUUID()}.tmp`;
  const previous = `${target}.${process.pid}.${crypto.randomUUID()}.previous`;
  let movedPrevious = false;
  let completed = false;
  try {
    fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, { mode: 0o600 });
    try {
      fs.renameSync(temporary, target);
    } catch (error) {
      if (!fs.existsSync(target)) throw error;
      fs.renameSync(target, previous);
      movedPrevious = true;
      fs.renameSync(temporary, target);
    }
    completed = true;
    if (movedPrevious) {
      try { fs.unlinkSync(previous); } catch (_) { /* harmless stale backup */ }
    }
  } catch (error) {
    if (movedPrevious && !fs.existsSync(target) && fs.existsSync(previous)) {
      fs.renameSync(previous, target);
    }
    throw error;
  } finally {
    try { fs.unlinkSync(temporary); } catch (_) { /* already renamed */ }
    if (completed) {
      try { fs.unlinkSync(previous); } catch (_) { /* absent or stale backup */ }
    }
  }
}

function safeRemoveBuild(versionRoot, buildRoot) {
  const relative = path.relative(path.resolve(versionRoot), path.resolve(buildRoot));
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return;
  fs.rmSync(buildRoot, { recursive: true, force: true });
}

function isInsideOrEqual(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return !relative || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

function preparedRuntimeCandidate(versionRoot, payload) {
  if (
    !payload ||
    payload.schema_version !== 1 ||
    payload.program_version !== PACKAGE.version ||
    typeof payload.python !== 'string'
  ) return '';
  try {
    const root = path.resolve(versionRoot);
    const candidate = path.resolve(payload.python);
    const relative = path.relative(root, candidate);
    if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return '';
    if (!sameEnvironmentCommand(candidate)) return '';
    return candidate;
  } catch (_) {
    return '';
  }
}

function incomplete(message) {
  process.stderr.write(`\n⚠ HASHI program installed, but runtime setup is incomplete.\n${message}\n`);
  process.stderr.write('Instance data was not created, changed, or removed. Fix the prerequisite and reinstall hashi-bridge.\n\n');
}

function main() {
  process.stdout.write('\nHASHI isolated runtime preparation\n');
  if (process.env.HASHI_POSTINSTALL_NO_PREPARE === '1') {
    incomplete('Automatic runtime preparation was explicitly disabled.');
    return 0;
  }
  const base = candidates().find((candidate) => {
    if (!sameEnvironmentCommand(candidate.command)) return false;
    try { return checkRuntime(candidate, false); } catch (_) { return false; }
  });
  if (!base) {
    incomplete('Approved CPython 3.12.13 was not found.');
    return 0;
  }

  const selectedDataRoot = dataRoot();
  if (isInsideOrEqual(HASHI_ROOT, selectedDataRoot)) {
    incomplete('HASHI_DATA_ROOT must be outside the installed program directory.');
    return 0;
  }
  const versionRoot = path.join(selectedDataRoot, 'runtimes', PACKAGE.version);
  const pointer = path.join(versionRoot, 'active.json');
  try {
    const active = JSON.parse(fs.readFileSync(pointer, 'utf8'));
    const activePython = preparedRuntimeCandidate(versionRoot, active);
    if (activePython) {
      const candidate = { command: activePython, prefix: [] };
      if (checkRuntime(candidate, true)) {
        process.stdout.write(`✓ HASHI ${PACKAGE.version} isolated runtime is ready.\n`);
        process.stdout.write('No instance data was changed. Run `hashi` to create or select an instance.\n\n');
        return 0;
      }
    }
  } catch (_) {
    // Build a new versioned runtime; a failed previous build is never adopted.
  }

  fs.mkdirSync(versionRoot, { recursive: true });
  const buildRoot = path.join(versionRoot, `build-${Date.now()}-${crypto.randomUUID()}`);
  const venv = spawnSync(
    base.command,
    [...base.prefix, '-m', 'venv', buildRoot],
    { stdio: 'inherit', windowsHide: true, timeout: 180_000 },
  );
  if (venv.status !== 0) {
    safeRemoveBuild(versionRoot, buildRoot);
    incomplete('Could not create the user-scoped Python virtual environment.');
    return 0;
  }
  const python = preparedPython(buildRoot);
  const install = spawnSync(
    python,
    ['-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '-r', LOCK],
    { stdio: 'inherit', windowsHide: true, timeout: 900_000 },
  );
  const candidate = { command: python, prefix: [] };
  if (install.status !== 0 || !checkRuntime(candidate, true)) {
    safeRemoveBuild(versionRoot, buildRoot);
    incomplete('The locked HASHI dependency generation could not be installed or verified.');
    return 0;
  }
  atomicJson(pointer, {
    schema_version: 1,
    program_version: PACKAGE.version,
    python,
    prepared_at: new Date().toISOString(),
  });
  process.stdout.write(`✓ HASHI ${PACKAGE.version} isolated runtime is ready.\n`);
  process.stdout.write('No instance data was changed. Run `hashi` to create or select an instance.\n\n');
  return 0;
}

module.exports = {
  atomicJson,
  dataRoot,
  isInsideOrEqual,
  preparedRuntimeCandidate,
  safeRemoveBuild,
  sameEnvironmentCommand,
};

if (require.main === module) {
  try {
    process.exitCode = main();
  } catch (error) {
    incomplete(`Unexpected setup error: ${error.message}`);
    process.exitCode = 0;
  }
}
