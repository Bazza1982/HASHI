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
