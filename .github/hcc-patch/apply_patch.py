"""Apply only the SHA-256-verified HCC patch tested in the isolated sandbox."""
from pathlib import Path
import base64
import gzip
import hashlib
import json
import os
import subprocess

EXPECTED = 'ddf77b391aae825726bcbd7cc2f81726e0dcd0a77690fb738ad90b108c8dda63'
root = Path(__file__).resolve().parent
parts = [root / f'part{i:02d}.b64' for i in range(16)]
encoded = ''.join(path.read_text(encoding='ascii').strip() for path in parts)
patch = gzip.decompress(base64.b64decode(encoded, validate=True))
actual = hashlib.sha256(patch).hexdigest()
if actual != EXPECTED:
    raise SystemExit(f'Patch digest mismatch: {actual}')
# Reverse ONLY the reviewed, HCC-only preview before applying its completed form.
# No branch reset or unrelated-file restoration is performed.
base = '79a204a8d5df30bb80511e96ed84df323796acaf'
preview = '23aebe28e8dc82d502a4af3a6218abed70c705a6'
reviewed = {
    'adapters/her_v2.py', 'orchestrator/bridge_memory.py',
    'orchestrator/command_specs.py', 'orchestrator/config_json.py',
    'orchestrator/flexible_agent_runtime.py', 'orchestrator/hcc.py',
    'orchestrator/pcm.py', 'orchestrator/runtime_hcc.py',
    'skills/hcc-refresh/SKILL.md', 'skills/hcc-refresh/scripts/hcc_update.py',
    'tests/test_hcc.py', 'tests/test_hcc_integration.py',
}
changed = set(subprocess.check_output(['git', 'diff', '--name-only', base, preview], text=True).splitlines())
if changed != reviewed:
    raise SystemExit('Preview reconciliation contains unreviewed files')
preview_patch = Path(os.environ['RUNNER_TEMP']) / 'hcc-reviewed-preview.patch'
preview_patch.write_bytes(subprocess.check_output(['git', 'diff', '--binary', base, preview, '--', *sorted(reviewed)]))
subprocess.run(['git', 'apply', '--reverse', '--check', str(preview_patch)], check=True)
subprocess.run(['git', 'apply', '--reverse', str(preview_patch)], check=True)
path = Path(os.environ['RUNNER_TEMP']) / 'hcc-implementation.patch'
path.write_bytes(patch)
subprocess.run(['git', 'apply', '--check', '--whitespace=error', str(path)], check=True)
subprocess.run(['git', 'apply', '--whitespace=error', str(path)], check=True)
supplement = root / 'reconciliation.patch'
subprocess.run(['git', 'apply', '--check', '--whitespace=error', str(supplement)], check=True)
subprocess.run(['git', 'apply', '--whitespace=error', str(supplement)], check=True)
expected_files = json.loads((root / 'expected-files.json').read_text(encoding='utf-8'))
for name, digest in expected_files.items():
    if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
        raise SystemExit(f'Final file differs from sandbox-tested bytes: {name}')
print(f'Applied verified HCC patch: {actual} ({len(patch)} bytes), plus reviewed preview compatibility')
