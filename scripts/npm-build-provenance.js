#!/usr/bin/env node
'use strict';

// npm runs this before packing.  The generated allow-listed file is included
// in the tarball so a Git-less install can identify the artifact it actually
// adopted instead of consulting a checkout at request time.

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const packageJson = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));

// The packaging audit runs a nested `npm pack --dry-run` to learn the real file
// list.  That child is started with --ignore-scripts so it cannot re-enter this
// hook; if it ever does, fail loudly instead of recursing.
if (process.env.HASHI_NPM_PACK_LIST_CHILD === '1') {
  throw new Error('Refusing to run the packaging audit inside its own npm pack child process.');
}

function git(...args) {
  const result = spawnSync('git', ['-C', root, ...args], {
    encoding: 'utf8',
    windowsHide: true,
    timeout: 5000,
  });
  return result.status === 0 ? String(result.stdout || '').trim() : '';
}

function productVersion() {
  try {
    const source = fs.readFileSync(path.join(root, 'pyproject.toml'), 'utf8');
    const project = source.match(/\[project\][\s\S]*?\nversion\s*=\s*["']([^"']+)["']/);
    if (project && project[1]) return project[1];
  } catch (_) {
    // package.json remains the formal npm fallback.
  }
  return packageJson.version;
}

const statusRows = git('status', '--porcelain=v1', '--untracked-files=normal')
  .split(/\r?\n/)
  .filter(Boolean);
const provenance = {
  schema_version: 1,
  product_version: productVersion(),
  artifact_kind: 'npm-package',
  release_channel: 'npm',
  commit: git('rev-parse', '--verify', 'HEAD') || packageJson.gitHead || null,
  branch: git('symbolic-ref', '--quiet', '--short', 'HEAD') || null,
  tag: git('describe', '--tags', '--exact-match', 'HEAD') || null,
  commit_time: git('show', '-s', '--format=%cI', 'HEAD') || null,
  build_time: new Date().toISOString(),
  dirty_at_build: statusRows.length > 0,
  change_count_at_build: statusRows.length,
  generation_id: null,
};
provenance.verifiable = Boolean(provenance.commit);
provenance.build_id = `sha256:${crypto
  .createHash('sha256')
  .update(JSON.stringify(provenance, Object.keys(provenance).sort()))
  .digest('hex')}`;

// The full file publication is created by the canonical Python/Core source
// owner. A dirty checkout cannot be turned into a trusted Git-less package.
const pythonChoices = [
  ...(process.env.HASHI_PYTHON ? [[process.env.HASHI_PYTHON]] : []),
  [path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')],
  ['python3.12'], ...(process.platform === 'win32' ? [['py', '-3.12']] : []),
  ['python3'], ['python'],
];
let interpreter = null;
for (const [command, ...prefix] of pythonChoices) {
  const result = spawnSync(command, [...prefix, '-B',
    path.join(root, 'scripts', 'export_function_source.py'),
    '--output', path.join(root, 'FUNCTION_SOURCE.json')],
  { encoding: 'utf8', windowsHide: true, timeout: 120_000 });
  if (result.status === 0) { interpreter = [command, ...prefix]; break; }
}
if (!interpreter) throw new Error('Cannot export committed HASHI Function source; package was not qualified.');

// The contract above is captured from every tracked file, while the tarball only
// carries what npm actually selects.  A package that advertises a module it does
// not ship installs cleanly and then fails Function generation on every instance,
// so refuse to build one.  The comparison uses npm's own file list (a nested
// `npm pack --dry-run --json --ignore-scripts`), which is the only authority for
// allow-list entries, nested .npmignore files and negative patterns alike.
const packAudit = spawnSync(interpreter[0], [...interpreter.slice(1), '-B',
  path.join(root, 'scripts', 'check_npm_package_files.py'), '--root', root, '--timeout', '240'],
{ encoding: 'utf8', windowsHide: true, timeout: 300_000 });
if (packAudit.status !== 0) {
  const detail = `${packAudit.stdout || ''}${packAudit.stderr || ''}`.trim();
  throw new Error(`npm package is missing required Function source; package was not qualified.\n${detail}`);
}

fs.writeFileSync(
  path.join(root, 'BUILD_INFO.json'),
  `${JSON.stringify({
    schema_version: 1,
    product: 'HASHI npm package',
    built_at_utc: provenance.build_time,
    product_version: provenance.product_version,
    provenance,
  }, null, 2)}\n`,
  { encoding: 'utf8', mode: 0o644 },
);
