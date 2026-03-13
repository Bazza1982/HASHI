# Post-Release TODO

Issues identified during v1.0.0 pre-release audit. These are non-blocking but should be addressed in future updates.

---

## 🟡 Recommended Fixes

### 1. Workbench Command Authentication Bypass
**File:** `orchestrator/workbench_api.py`

The `handle_agent_command` endpoint (line ~453) does not check `_check_admin_auth()`, while `handle_admin_command` does. This allows unauthenticated command execution from any local caller that can reach `127.0.0.1:18800`.

**Impact:** Low (localhost-bound, not remote RCE)
**Fix:** Add `_check_admin_auth()` check to `handle_agent_command`.

---

### 2. Test Runner Path Error
**File:** `tests/run_tests.sh`

Lines 24-25 set:
```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$SCRIPT_DIR/tests"
```

Since the script is already in `tests/`, this creates `tests/tests/...` paths.

**Fix:** Change `TEST_DIR="$SCRIPT_DIR/tests"` to `TEST_DIR="$SCRIPT_DIR"`.

---

### 3. INSTALL.md Path and Secret Name Errors
**File:** `INSTALL.md`

- Line ~148: `chmod +x bridge-u.sh` should be `chmod +x bin/bridge-u.sh`
- Same for `cli.js` and `onboard-cli.js` - missing `bin/` prefix

**Secret name mismatch:**
- Documentation uses `openrouter_api_key` (underscore)
- Actual code uses `openrouter-api_key` (hyphen) in `onboarding/onboarding_main.py` and `orchestrator/flexible_backend_registry.py`

---

## 🟠 Optional Fixes

### 4. docs/README.md Stale File References
**File:** `docs/README.md`

References to:
- `2026-03-11_delivery_routing_fix_plan.md` → actual: `Delivery_routing_fix_plan.md`
- `BRIDGE_AGENT_HANDOFF_2026-03-10.md` → actual: `BRIDGE_AGENT_HANDOFF.md`

---

### 5. PowerShell Scripts Hardcoded Paths
**Files:**
- `scripts/install_elevated_autostart.ps1`
- `scripts/check_stress_test.ps1`
- `scripts/start_stress_test.ps1`

These scripts hardcode `C:\Users\thene\projects\bridge-u-f`.

**Options:**
- Replace with relative paths or environment variables
- Move to `.personal/` and git-ignore
- Delete if not needed for public release

---

## Audit Date
2026-03-14

## Auditor
小夏 (Codex Agent) - verified by 小蕾 (Claude)
