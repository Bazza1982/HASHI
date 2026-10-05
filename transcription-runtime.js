// Program-scoped Function helper. It conveys no Core or Tool authority.
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function transcriptionLockDigest() {
  return crypto.createHash('sha256').update(fs.readFileSync(
    path.join(__dirname, 'constraints', 'transcription-py312.lock')
  )).digest('hex');
}

function preparedTranscriptionPython(versionRoot, programVersion) {
  try {
    const payload = JSON.parse(fs.readFileSync(
      path.join(versionRoot, 'transcription-active.json'), 'utf8'
    ));
    if (!payload || payload.schema_version !== 1 ||
        payload.program_version !== programVersion ||
        typeof payload.python !== 'string' ||
        typeof payload.runtime_dir !== 'string' ||
        payload.lock_sha256 !== transcriptionLockDigest()) return '';
    const candidate = path.resolve(payload.python);
    const relative = path.relative(path.resolve(versionRoot), candidate);
    if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return '';
    const pieces = relative.split(path.sep);
    if (pieces.length !== 3 || !pieces[0].startsWith('transcription-') ||
        pieces[1] !== (process.platform === 'win32' ? 'Scripts' : 'bin') ||
        pieces[2] !== (process.platform === 'win32' ? 'python.exe' : 'python')) return '';
    const runtimeDir = path.join(path.resolve(versionRoot), pieces[0]);
    if (path.resolve(payload.runtime_dir) !== runtimeDir ||
        fs.realpathSync(runtimeDir) !== runtimeDir) return '';
    if (process.platform !== 'win32' && candidate.toLowerCase().endsWith('.exe')) return '';
    if (process.platform === 'win32' && /^\\\\(?:wsl\$|wsl\.localhost)\\/i.test(candidate)) return '';
    return fs.statSync(candidate).isFile() ? candidate : '';
  } catch (_) {
    return '';
  }
}

module.exports = { preparedTranscriptionPython, transcriptionLockDigest };
