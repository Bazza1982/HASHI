// Qualified speech helper selection; no Core dependency or instance mutation.
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { preparedHelperPython } = require('./transcription-runtime');
function ttsLockDigest() {
  return crypto.createHash('sha256').update(fs.readFileSync(
    path.join(__dirname, 'constraints', 'tts-py312.lock'))).digest('hex');
}
function preparedTTSPython(root, version) {
  return preparedHelperPython(root, version, 'tts', ttsLockDigest());
}
function preparedFFmpeg(root, version) {
  const python = preparedTTSPython(root, version);
  if (!python) return '';
  try {
    const payload = JSON.parse(fs.readFileSync(path.join(root, 'tts-active.json'), 'utf8'));
    const file = path.resolve(payload.ffmpeg);
    const relative = path.relative(path.dirname(path.dirname(python)), file);
    return relative && !relative.startsWith('..') && !path.isAbsolute(relative) && fs.statSync(file).isFile() ? file : '';
  } catch { return ''; }
}
module.exports = { ttsLockDigest, preparedTTSPython, preparedFFmpeg };
