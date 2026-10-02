'use strict';

const fs = require('fs');
const path = require('path');

function preparedPrivacyPython(versionRoot, programVersion) {
  try {
    const pointer = path.join(versionRoot, 'privacy-active.json');
    const payload = JSON.parse(fs.readFileSync(pointer, 'utf8'));
    if (
      payload.schema_version !== 1 ||
      payload.program_version !== programVersion ||
      typeof payload.python !== 'string'
    ) return '';
    const candidate = path.resolve(payload.python);
    const relative = path.relative(path.resolve(versionRoot), candidate);
    if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return '';
    const pieces = relative.split(path.sep);
    if (
      pieces.length !== 3 || !pieces[0].startsWith('privacy-') ||
      pieces[1] !== (process.platform === 'win32' ? 'Scripts' : 'bin') ||
      pieces[2] !== (process.platform === 'win32' ? 'python.exe' : 'python')
    ) return '';
    const runtimeDir = path.join(versionRoot, pieces[0]);
    if (fs.realpathSync(runtimeDir) !== path.resolve(runtimeDir)) return '';
    if (process.platform !== 'win32' && candidate.toLowerCase().endsWith('.exe')) return '';
    if (process.platform === 'win32' && /^\\\\(?:wsl\$|wsl\.localhost)\\/i.test(candidate)) return '';
    return fs.existsSync(candidate) ? candidate : '';
  } catch (_) {
    return '';
  }
}

module.exports = { preparedPrivacyPython };
