#!/usr/bin/env node
'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { preparedPrivacyPython } = require('./privacy-runtime');
const { preparedTranscriptionPython, transcriptionLockDigest } = require('./transcription-runtime');
const { preparedTTSPython, ttsLockDigest } = require('./tts-runtime');

const HASHI_ROOT = __dirname;
const PACKAGE = require(path.join(HASHI_ROOT, 'package.json'));
const LOCK = path.join(HASHI_ROOT, 'constraints', 'standard-py312.lock');
const RUNTIME_CHECK = path.join(HASHI_ROOT, 'scripts', 'check_runtime_contract.py');
const PRIVACY_SETUP = path.join(HASHI_ROOT, 'scripts', 'provision_privacy_runtime.py');
const TRANSCRIPTION_SETUP = path.join(HASHI_ROOT, 'scripts', 'provision_transcription_runtime.py');
const TTS_SETUP = path.join(HASHI_ROOT, 'scripts', 'provision_tts_runtime.py');

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

function preparePrivacy(base, versionRoot) {
  if (process.env.HASHI_POSTINSTALL_NO_PRIVACY === '1') {
    process.stdout.write('Level 2 privacy setup was skipped by request.\n');
    return;
  }
  const pointer = path.join(versionRoot, 'privacy-active.json');
  const active = preparedPrivacyPython(versionRoot, PACKAGE.version);
  const ready = (python) => {
    const runtimeDir = path.dirname(path.dirname(python));
    const result = spawnSync(base.command, [
      ...base.prefix, PRIVACY_SETUP, '--runtime-dir', runtimeDir, '--check',
    ], { stdio: 'ignore', windowsHide: true, timeout: 120_000 });
    return result.status === 0;
  };
  if (active && ready(active)) {
    process.stdout.write('HASHI Level 2 privacy detector is ready.\n');
    return;
  }
  const buildRoot = path.join(versionRoot, `privacy-${Date.now()}-${crypto.randomUUID()}`);
  const install = spawnSync(base.command, [
    ...base.prefix, PRIVACY_SETUP, '--runtime-dir', buildRoot,
  ], { stdio: 'inherit', windowsHide: true, timeout: 960_000 });
  const python = preparedPython(buildRoot);
  if (install.status !== 0 || !ready(python)) {
    safeRemoveBuild(versionRoot, buildRoot);
    process.stderr.write('Level 2 privacy detector setup is incomplete; Level 2 remains unavailable.\n');
    return;
  }
  atomicJson(pointer, {
    schema_version: 1,
    program_version: PACKAGE.version,
    python,
    prepared_at: new Date().toISOString(),
  });
  process.stdout.write('HASHI Level 2 privacy detector is ready.\n');
}

function tryPreparePrivacy(base, versionRoot) {
  try {
    preparePrivacy(base, versionRoot);
  } catch (_) {
    process.stderr.write('Level 2 privacy detector setup is incomplete; Level 2 remains unavailable.\n');
  }
}


function prepareTranscription(base, versionRoot) {
  return prepareSpeechHelper(base, versionRoot, {kind: 'transcription', setup: TRANSCRIPTION_SETUP,
    selected: preparedTranscriptionPython, lockDigest: transcriptionLockDigest, extra: ['--prepare-model']});
}

function prepareTTS(base, versionRoot) {
  return prepareSpeechHelper(base, versionRoot, {kind: 'tts', setup: TTS_SETUP,
    selected: preparedTTSPython, lockDigest: ttsLockDigest, extra: []});
}

function prepareSpeechHelper(base, versionRoot, {kind, setup, selected, lockDigest, extra}) {
  if (process.env[`HASHI_POSTINSTALL_NO_${kind.toUpperCase()}`] === '1') {
    process.stdout.write(`${kind} setup was explicitly skipped; this is a partial media installation.\n`);
    return false;
  }
  const ready = (python) => {
    const runtimeDir = path.dirname(path.dirname(python));
    const result = spawnSync(base.command, [
      ...base.prefix, setup,
      '--bridge-home', runtimeDir, '--runtime-dir', runtimeDir, '--check', ...extra,
    ], { stdio: 'ignore', windowsHide: true, timeout: 120_000 });
    return result.status === 0;
  };
  let buildRoot = '';
  try {
    const active = selected(versionRoot, PACKAGE.version);
    if (active && ready(active)) {
      process.stdout.write(`HASHI ${kind} runtime is ready.\n`);
      return true;
    }
    buildRoot = path.join(versionRoot, `${kind}-${Date.now()}-${crypto.randomUUID()}`);
    fs.mkdirSync(buildRoot, { recursive: true, mode: 0o700 });
    // The preparation home is this disposable Function artifact, never an
    // instance or the approved Core virtual environment. The provisioner owns
    // locked pip installation and native import/version validation.
    const install = spawnSync(base.command, [
      ...base.prefix, setup,
      '--bridge-home', buildRoot, '--runtime-dir', buildRoot, ...extra,
    ], { stdio: 'inherit', windowsHide: true, timeout: 1_560_000 });
    const python = preparedPython(buildRoot);
    if (install.status !== 0 || !ready(python)) throw new Error('transcription probe failed');
    const receipt = JSON.parse(fs.readFileSync(
      path.join(buildRoot, 'state', 'platform', `${kind}.json`), 'utf8'
    ));
    if (receipt.schema_version !== 1 || receipt.python !== python ||
        receipt.runtime_dir !== buildRoot || receipt.lock_sha256 !== lockDigest()) {
      throw new Error('transcription receipt did not match this generation');
    }
    atomicJson(path.join(versionRoot, `${kind}-active.json`), {
      ...receipt, program_version: PACKAGE.version, prepared_at: new Date().toISOString(),
    });
    process.stdout.write(`HASHI ${kind} runtime is ready.\n`);
    return true;
  } catch (_) {
    if (buildRoot) safeRemoveBuild(versionRoot, buildRoot);
    process.stderr.write(`HASHI ${kind} setup is incomplete. Reinstall after fixing runtime prerequisites.\n`);
    return false;
  }
}

function prepareMedia(base, versionRoot) {
  const stt = prepareTranscription(base, versionRoot);
  const tts = prepareTTS(base, versionRoot);
  if (!stt || !tts) {
    incomplete('Full media setup did not pass. Recording, speech and conversion must all be ready.');
    return 1;
  }
  process.stdout.write('Recording, speech synthesis and media conversion are ready.\n');
  return 0;
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
    return 1;
  }

  const selectedDataRoot = dataRoot();
  if (isInsideOrEqual(HASHI_ROOT, selectedDataRoot)) {
    incomplete('HASHI_DATA_ROOT must be outside the installed program directory.');
    return 1;
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
        tryPreparePrivacy(base, versionRoot);
        const mediaStatus = prepareMedia(base, versionRoot);
        process.stdout.write('No instance data was changed. Run `hashi` to create or select an instance.\n\n');
        return mediaStatus;
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
    return 1;
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
    return 1;
  }
  atomicJson(pointer, {
    schema_version: 1,
    program_version: PACKAGE.version,
    python,
    prepared_at: new Date().toISOString(),
  });
  process.stdout.write(`✓ HASHI ${PACKAGE.version} isolated runtime is ready.\n`);
  tryPreparePrivacy(base, versionRoot);
  const mediaStatus = prepareMedia(base, versionRoot);
  process.stdout.write('No instance data was changed. Run `hashi` to create or select an instance.\n\n');
  return mediaStatus;
}

module.exports = {
  main,
  prepareTranscription,
  prepareTTS,
  prepareMedia,
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
    process.exitCode = 1;
  }
}
