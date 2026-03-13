# Hashi Linux Migration Plan

**Created**: 2026-03-12  
**Target**: Full Linux/WSL2 compatibility  
**Status**: Planning Phase

---

## Executive Summary

This document outlines a comprehensive plan to migrate the Hashi project from Windows to Linux. The migration involves:

1. **Script Migration**: Converting 10 `.bat` files and 1 `.ps1` file to `.sh` equivalents
2. **Python Code Fixes**: Replacing Windows-specific APIs with cross-platform alternatives
3. **Dependency Changes**: Replacing Windows-only components with Linux equivalents
4. **Configuration Updates**: Updating default paths and behaviors

---

## Phase 1: Shell Script Migration

### 1.1 Batch Files to Convert

| Windows File | Linux Equivalent | Priority | Complexity |
|-------------|------------------|----------|------------|
| `bridge-u.bat` | `bridge-u.sh` | 🔴 Critical | High |
| `start-agent.bat` | `start-agent.sh` | 🟡 Medium | Low |
| `stop-agent.bat` | `stop-agent.sh` | 🟡 Medium | Low |
| `restart_bridge_u_f.bat` | `restart-bridge.sh` | 🔴 Critical | Medium |
| `kill_bridge_u_f_sessions.bat` | `kill-sessions.sh` | 🔴 Critical | High |
| `workbench.bat` | `workbench.sh` | 🟢 Low | Medium |
| `stop_workbench.bat` | `stop-workbench.sh` | 🟢 Low | Low |
| `restart_workbench.bat` | `restart-workbench.sh` | 🟢 Low | Low |
| `workbench_ctl.ps1` | `workbench-ctl.sh` | 🟢 Low | High |
| `workbench/start-workbench.bat` | `workbench/start.sh` | 🟢 Low | Low |
| `workbench/pm2-startup.bat` | `workbench/pm2-startup.sh` | 🟢 Low | Low |

### 1.2 Script Conversion Details

#### `bridge-u.sh` (Main Entry Point)
```bash
#!/usr/bin/env bash
# Bridge-U-F Launcher for Linux

set -euo pipefail

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BRIDGE_CODE_ROOT="$SCRIPT_DIR"
BRIDGE_HOME="${BRIDGE_HOME:-$BRIDGE_CODE_ROOT}"

STATE_FILE="$BRIDGE_HOME/.bridge_u_last_agents.txt"
AGENTS_FILE="/tmp/bridge_u_active_agents.txt"
INACTIVE_FILE="/tmp/bridge_u_inactive_agents.txt"
WORKBENCH_LAUNCH=0
API_GATEWAY_LAUNCH=0
AUTO_RESUME_LAST=0
DRY_RUN=0

# ANSI Colors
C_RESET="\033[0m"
C_ACCENT="\033[38;5;111m"
C_OK="\033[38;5;114m"
C_WARN="\033[38;5;222m"
C_LABEL="\033[38;5;109m"
C_TEXT="\033[97m"
C_MUTED="\033[90m"
C_RAIL="\033[38;5;61m"
C_TITLE="\033[1;38;5;153m"

# Parse arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --resume-last) AUTO_RESUME_LAST=1 ;;
            --workbench) WORKBENCH_LAUNCH=1 ;;
            --api-gateway) API_GATEWAY_LAUNCH=1 ;;
            --dry-run) DRY_RUN=1 ;;
            *) ;;
        esac
        shift
    done
}

# Ensure Python environment
ensure_env() {
    if [[ ! -d .venv ]]; then
        echo -e "${C_MUTED}Creating virtual environment...${C_RESET}"
        python3 -m venv .venv || return 1
    fi
    
    source .venv/bin/activate || return 1
    
    if ! python3 -c "import telegram, httpx, aiohttp, PIL" 2>/dev/null; then
        echo -e "${C_MUTED}Installing Python dependencies...${C_RESET}"
        pip install python-telegram-bot httpx aiohttp pillow || return 1
    fi
}

# Load agents from config
load_agents() {
    local cfg_path="$BRIDGE_HOME/agents.json"
    [[ ! -f "$cfg_path" ]] && cfg_path="$BRIDGE_CODE_ROOT/agents.json"
    
    # Extract active agents using Python (more reliable than jq for complex JSON)
    python3 -c "
import json
with open('$cfg_path') as f:
    cfg = json.load(f)
for agent in cfg.get('agents', []):
    if agent.get('is_active', True):
        engine = agent.get('active_backend') or agent.get('engine') or agent.get('type', 'unknown')
        print(f\"{agent['name']}|{engine}\")
" > "$AGENTS_FILE"
    
    python3 -c "
import json
with open('$cfg_path') as f:
    cfg = json.load(f)
for agent in cfg.get('agents', []):
    if not agent.get('is_active', True):
        engine = agent.get('active_backend') or agent.get('engine') or agent.get('type', 'unknown')
        print(f\"{agent['name']}|{engine}\")
" > "$INACTIVE_FILE"
    
    AGENT_COUNT=$(wc -l < "$AGENTS_FILE" | tr -d ' ')
    INACTIVE_COUNT=$(wc -l < "$INACTIVE_FILE" | tr -d ' ')
}

# Check if bridge is already running
preflight_check() {
    local pid_file="$SCRIPT_DIR/.bridge_u_f.pid"
    if [[ -f "$pid_file" ]]; then
        local existing_pid
        existing_pid=$(cat "$pid_file" 2>/dev/null)
        if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
            # Verify it's actually our process
            if grep -q "main.py" /proc/"$existing_pid"/cmdline 2>/dev/null; then
                echo -e "\n  ${C_WARN}Bridge-u-f is already running (PID $existing_pid).${C_RESET}"
                return 1
            fi
        fi
        rm -f "$pid_file"
    fi
    return 0
}

# Launch the orchestrator
launch() {
    local py_args="$1"
    local start_label="$2"
    
    clear
    echo -e "\n${C_RAIL}│${C_RESET} ${C_TITLE}BRIDGE-U-F BOOT${C_RESET}  ${C_MUTED}Multi-backend orchestrator launch${C_RESET}"
    echo -e "${C_RAIL}│${C_RESET}"
    echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}Agents           ${C_RESET} ${C_TEXT}$start_label${C_RESET}"
    
    if [[ "$WORKBENCH_LAUNCH" == "1" ]]; then
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}Workbench       ${C_RESET} ${C_OK}starting in background${C_RESET}"
        "$SCRIPT_DIR/workbench-ctl.sh" start &
    else
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}Workbench       ${C_RESET} ${C_MUTED}disabled${C_RESET}"
    fi
    
    local gw_arg=""
    if [[ "$API_GATEWAY_LAUNCH" == "1" ]]; then
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}API Gateway     ${C_RESET} ${C_OK}enabled (port 18801)${C_RESET}"
        gw_arg="--api-gateway"
    else
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}API Gateway     ${C_RESET} ${C_MUTED}disabled${C_RESET}"
    fi
    
    if [[ "$DRY_RUN" == "1" ]]; then
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}Launch mode     ${C_RESET} ${C_WARN}dry run only${C_RESET}"
        echo -e "${C_RAIL}│${C_RESET}"
        return 0
    fi
    
    preflight_check || return 1
    
    echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}Bridge launch    ${C_RESET} ${C_OK}proceeding${C_RESET}"
    echo -e "${C_RAIL}│${C_RESET}"
    
    # shellcheck disable=SC2086
    python3 main.py --bridge-home "$BRIDGE_HOME" $py_args $gw_arg
}

# Main
parse_args "$@"
ensure_env || exit 1
load_agents || exit 1

if [[ "$AUTO_RESUME_LAST" == "1" ]] && [[ -f "$STATE_FILE" ]]; then
    # Resume last session
    IFS='|' read -r last_mode last_agents < "$STATE_FILE"
    if [[ "$last_mode" == "all" ]]; then
        launch "" "all active agents (resumed)"
    else
        launch "--agents $last_agents" "$last_agents (resumed)"
    fi
else
    # Start all active agents by default
    launch "" "all active agents"
fi
```

#### `start-agent.sh` / `stop-agent.sh` (Simple API calls)
```bash
#!/usr/bin/env bash
# start-agent.sh / stop-agent.sh

if [[ -z "$1" ]]; then
    echo "Usage: $0 agent-name"
    exit 1
fi

ACTION="${0##*/}"
ACTION="${ACTION%.sh}"
ACTION="${ACTION/start-agent/start-agent}"
ACTION="${ACTION/stop-agent/stop-agent}"

curl -s -X POST "http://127.0.0.1:18800/api/admin/${ACTION}" \
    -H "Content-Type: application/json" \
    -d "{\"agent\":\"$1\"}"
```

#### `kill-sessions.sh` (Process Cleanup)
```bash
#!/usr/bin/env bash
# Kill all bridge-u-f sessions

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

log() { [[ "$QUIET" == "0" ]] && echo "$@"; }

log "================================================================"
log "           KILL BRIDGE-U-F REMAINING SESSIONS"
log "================================================================"
log ""

# Stop workbench if running
if [[ -x "$SCRIPT_DIR/workbench-ctl.sh" ]]; then
    "$SCRIPT_DIR/workbench-ctl.sh" stop 2>/dev/null || true
fi

# Find and kill bridge-u-f processes
FOUND_ANY=0
while IFS= read -r pid; do
    if [[ -n "$pid" ]]; then
        FOUND_ANY=1
        log "Stopping PID $pid ..."
        kill -TERM "$pid" 2>/dev/null || true
    fi
done < <(pgrep -f "python.*main\.py.*$SCRIPT_DIR" 2>/dev/null || true)

# Also check for processes on our ports
for port in 18800 18801; do
    pid=$(lsof -ti :"$port" 2>/dev/null || true)
    if [[ -n "$pid" ]]; then
        FOUND_ANY=1
        log "Stopping process on port $port (PID $pid) ..."
        kill -TERM "$pid" 2>/dev/null || true
    fi
done

if [[ "$FOUND_ANY" == "0" ]]; then
    log "No bridge-u-f processes found."
else
    sleep 2
    # Force kill any remaining
    pgrep -f "python.*main\.py.*$SCRIPT_DIR" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
    log "Cleanup commands issued."
fi

# Check if anything is still running
if ! pgrep -f "python.*main\.py.*$SCRIPT_DIR" >/dev/null 2>&1; then
    rm -f "$SCRIPT_DIR/.bridge_u_f.lock" "$SCRIPT_DIR/.bridge_u_f.pid"
    log "Removed stale lock/pid files"
    log ""
    log "Cleanup complete."
    exit 0
else
    log ""
    log "Cleanup incomplete. Some processes may still be alive."
    exit 1
fi
```

#### `workbench-ctl.sh` (Service Manager)
```bash
#!/usr/bin/env bash
# Workbench control script for Linux

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKBENCH_DIR="$SCRIPT_DIR/workbench"
STATE_DIR="$SCRIPT_DIR/state/workbench"
LOG_DIR="$STATE_DIR/logs"

ACTION="${1:-start}"
OPEN_BROWSER="${2:-}"

mkdir -p "$STATE_DIR" "$LOG_DIR"

SERVER_PID_FILE="$STATE_DIR/server.pid"
CLIENT_PID_FILE="$STATE_DIR/client.pid"
SERVER_LOG="$LOG_DIR/server.log"
CLIENT_LOG="$LOG_DIR/client.log"
SERVER_PORT=3001
CLIENT_PORT=5173

get_pid() {
    local pid_file="$1"
    [[ -f "$pid_file" ]] && cat "$pid_file" 2>/dev/null || echo ""
}

is_alive() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

get_port_owner() {
    local port="$1"
    lsof -ti :"$port" 2>/dev/null | head -1 || true
}

test_health() {
    local url="$1"
    curl -sf --max-time 3 "$url" >/dev/null 2>&1
}

stop_service() {
    local name="$1"
    local pid_file="$2"
    local port="$3"
    
    local managed_pid
    managed_pid=$(get_pid "$pid_file")
    
    if is_alive "$managed_pid"; then
        echo "Stopping $name (PID $managed_pid)..."
        kill -TERM "$managed_pid" 2>/dev/null || true
        sleep 1
    fi
    
    local port_owner
    port_owner=$(get_port_owner "$port")
    if [[ -n "$port_owner" ]] && [[ "$port_owner" != "$managed_pid" ]]; then
        echo "Stopping $name listener on port $port (PID $port_owner)..."
        kill -TERM "$port_owner" 2>/dev/null || true
        sleep 1
    fi
    
    rm -f "$pid_file"
}

start_service() {
    local name="$1"
    local pid_file="$2"
    local port="$3"
    local health_url="$4"
    local command="$5"
    local log_file="$6"
    
    local managed_pid
    managed_pid=$(get_pid "$pid_file")
    
    if is_alive "$managed_pid" && test_health "$health_url"; then
        echo "$name already healthy (PID $managed_pid)."
        return 0
    fi
    
    # Clean up stale state
    [[ -n "$managed_pid" ]] && rm -f "$pid_file"
    
    local port_owner
    port_owner=$(get_port_owner "$port")
    if [[ -n "$port_owner" ]]; then
        if test_health "$health_url"; then
            echo "$port_owner" > "$pid_file"
            echo "$name recovered from existing listener PID $port_owner."
            return 0
        fi
        echo "Killing orphaned $name process (PID $port_owner)..."
        kill -TERM "$port_owner" 2>/dev/null || true
        sleep 1
    fi
    
    echo "Starting $name..."
    cd "$WORKBENCH_DIR"
    nohup bash -c "$command" > "$log_file" 2>&1 &
    local start_pid=$!
    echo "$start_pid" > "$pid_file"
    
    # Wait for health
    for _ in {1..45}; do
        sleep 1
        if test_health "$health_url"; then
            local listener_pid
            listener_pid=$(get_port_owner "$port")
            [[ -n "$listener_pid" ]] && echo "$listener_pid" > "$pid_file"
            echo "$name is healthy."
            return 0
        fi
        is_alive "$start_pid" || break
    done
    
    echo "ERROR: $name failed health check after startup."
    stop_service "$name" "$pid_file" "$port"
    return 1
}

ensure_deps() {
    if [[ ! -f "$WORKBENCH_DIR/package.json" ]]; then
        echo "ERROR: Workbench directory missing: $WORKBENCH_DIR"
        exit 1
    fi
    
    if [[ ! -d "$WORKBENCH_DIR/node_modules" ]]; then
        echo "Installing workbench dependencies..."
        cd "$WORKBENCH_DIR" && npm install
    fi
}

case "$ACTION" in
    start)
        ensure_deps
        start_service "Workbench API" "$SERVER_PID_FILE" "$SERVER_PORT" \
            "http://localhost:$SERVER_PORT/api/config" "npm run dev:server" "$SERVER_LOG"
        start_service "Workbench UI" "$CLIENT_PID_FILE" "$CLIENT_PORT" \
            "http://localhost:$CLIENT_PORT/" "npm run dev:client" "$CLIENT_LOG"
        [[ "$OPEN_BROWSER" == "--open" ]] && xdg-open "http://localhost:$CLIENT_PORT/" 2>/dev/null &
        ;;
    stop)
        stop_service "Workbench UI" "$CLIENT_PID_FILE" "$CLIENT_PORT"
        stop_service "Workbench API" "$SERVER_PID_FILE" "$SERVER_PORT"
        ;;
    restart)
        "$0" stop
        "$0" start "$OPEN_BROWSER"
        ;;
    status)
        for svc in server client; do
            pid_file="$STATE_DIR/$svc.pid"
            port=$([[ "$svc" == "server" ]] && echo "$SERVER_PORT" || echo "$CLIENT_PORT")
            health_url="http://localhost:$port/$([[ "$svc" == "server" ]] && echo "api/config" || echo "")"
            
            managed_pid=$(get_pid "$pid_file")
            alive=$(is_alive "$managed_pid" && echo "true" || echo "false")
            health=$(test_health "$health_url" && echo "true" || echo "false")
            port_owner=$(get_port_owner "$port")
            
            echo "Workbench $svc: pid=${managed_pid:-none} alive=$alive health=$health portOwner=${port_owner:-none}"
        done
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status} [--open]"
        exit 1
        ;;
esac
```

---

## Phase 2: Python Code Modifications

### 2.1 Critical Changes

#### `main.py` - Instance Lock (Lines 156-248)

**Current Windows Code:**
```python
import msvcrt
msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
```

**Cross-Platform Replacement:**
```python
import fcntl  # Unix
import sys

class InstanceLock:
    """Cross-platform single-instance guard using file locking."""
    
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self):
        fh = None
        try:
            try:
                fh = open(str(self.path), "r+b")
            except FileNotFoundError:
                fh = open(str(self.path), "w+b")

            fh.seek(0)
            
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            our_pid = str(os.getpid())
            fh.seek(0)
            fh.truncate(0)
            fh.write(our_pid.encode("utf-8"))
            fh.flush()
            self._fh = fh
            self._write_pid_file(our_pid)

        except (OSError, IOError) as exc:
            if fh:
                fh.close()
            pid = self._read_pid_file()
            hint = f"Run: kill {pid}" if pid.isdigit() else "Check running processes"
            raise RuntimeError(
                f"bridge-u-f is already running (PID {pid}). "
                f"Shut down the existing instance first. Hint: {hint}"
            ) from exc

    def release(self):
        if self._fh is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        # ... rest of cleanup
```

#### `main.py` - Console Encoding (Lines 46-56)

**Current:**
```python
if os.name == "nt":
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleCP(65001)
    kernel32.SetConsoleOutputCP(65001)
```

**Fix:** Already conditional, no changes needed.

#### `orchestrator/banner.py` - Console Detection (Line 68-72)

**Current:**
```python
if os.name == "nt":
    if ctypes.windll.kernel32.GetConsoleOutputCP() != 65001:
        return box_ascii_only(...)
```

**Fix:**
```python
def _supports_unicode() -> bool:
    """Check if the terminal supports Unicode output."""
    if os.name == "nt":
        try:
            import ctypes
            return ctypes.windll.kernel32.GetConsoleOutputCP() == 65001
        except Exception:
            return False
    else:
        # Unix: check LANG/LC_* environment
        import locale
        try:
            encoding = locale.getpreferredencoding(False).lower()
            return 'utf' in encoding
        except Exception:
            return os.environ.get('LANG', '').lower().endswith('utf-8')
```

### 2.2 Adapter Changes

#### `adapters/base.py` - Process Killing (Lines 106-134)

**Current:**
```python
if os.name == "nt" and pid:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], ...)
```

**Fix:**
```python
async def force_kill_process_tree(self, proc, logger=None, reason: str = "") -> bool:
    if not proc:
        return False

    pid = getattr(proc, "pid", None)
    returncode = getattr(proc, "returncode", None)
    if returncode is not None:
        return False

    try:
        if sys.platform == "win32" and pid:
            def _taskkill():
                return subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=10,
                )
            completed = await asyncio.to_thread(_taskkill)
            # ... logging
        else:
            # Unix: kill process group
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                proc.kill()
            if logger:
                logger.warning(f"Forced kill for pid={pid} reason={reason!r}")
    except Exception as exc:
        if logger:
            logger.warning(f"Failed to terminate pid={pid} reason={reason!r}: {exc}")
        return False
    # ... rest
```

#### `adapters/codex_cli.py` - Command Extension (Lines 37-45)

**Current:**
```python
if os.name == "nt" and Path(self.cmd_base).suffix.lower() not in {".cmd", ".exe", ".bat", ".ps1"}:
    self.cmd_base = f"{self.cmd_base}.cmd"
```

**Fix:**
```python
def __init__(self, agent_config, global_config, api_key: str = None):
    super().__init__(agent_config, global_config, api_key)
    self.cmd_base = self.global_config.codex_cmd
    
    if sys.platform == "win32":
        # Windows: ensure .cmd extension for npm-installed CLIs
        if Path(self.cmd_base).suffix.lower() not in {".cmd", ".exe", ".bat", ".ps1"}:
            self.cmd_base = f"{self.cmd_base}.cmd"
    # else: Unix - command names don't need extension modification
```

### 2.3 TTS Provider Changes

#### `orchestrator/tts_providers/windows.py`

**Status:** Windows-only, needs runtime check

**Fix:**
```python
# Add at module level
import sys
if sys.platform != "win32":
    raise ImportError("Windows TTS provider is only available on Windows")
```

**Better Fix:** Modify `orchestrator/tts_providers/__init__.py`:
```python
def build_provider(name: str, config: dict):
    """Factory function that filters out incompatible providers."""
    if name == "windows" and sys.platform != "win32":
        raise ValueError("Windows TTS provider is not available on Linux. Use 'piper', 'kokoro', or 'edge' instead.")
    # ... rest of factory
```

---

## Phase 3: Dependency Changes

### 3.1 System Dependencies

| Windows | Linux (Ubuntu/Debian) | Purpose |
|---------|----------------------|---------|
| (built-in) | `lsof` | Port checking |
| (built-in) | `procps` (pgrep) | Process finding |
| (built-in) | `curl` | API calls |
| PowerShell | `jq` (optional) | JSON parsing |
| (built-in) | `xdg-utils` | Browser opening |

**Install Command:**
```bash
sudo apt-get update && sudo apt-get install -y lsof procps curl jq xdg-utils
```

### 3.2 Python Dependencies

**No changes required** - all current dependencies are cross-platform:
- `python-telegram-bot`
- `httpx`
- `aiohttp`
- `pillow`
- `neonize` (WhatsApp - has Linux builds)

### 3.3 TTS Provider Alternatives

| Windows Provider | Linux Alternative | Notes |
|-----------------|------------------|-------|
| `windows` (SAPI) | `piper` | Offline, fast, good quality |
| `windows` (SAPI) | `edge` | Online, Microsoft Edge TTS |
| `windows` (SAPI) | `kokoro` | Offline, neural voices |
| `windows` (SAPI) | `coqui` | Offline, open-source |

**Configuration Update (`agents.json`):**
```json
{
  "global": {
    "tts_provider": "piper",  // Change from "windows"
    "piper_model": "en_US-lessac-medium"
  }
}
```

---

## Phase 4: Configuration Path Updates

### 4.1 Default Path Changes

| Setting | Windows Default | Linux Default |
|---------|----------------|---------------|
| `workspace_dir` | `C:\Users\...\workspaces\agent` | `~/projects/hashi/workspaces/agent` |
| `bridge_home` | `C:\Users\...\bridge-u-f` | `~/projects/hashi` |
| `project_root` | `C:\Users\...\projects` | `~/projects` |

### 4.2 agents.json Template Update

Create a new `agents.json.linux.example`:
```json
{
  "global": {
    "project_root": "~/projects",
    "gemini_cmd": "gemini",
    "claude_cmd": "claude",
    "codex_cmd": "codex",
    "workbench_port": 18800,
    "api_gateway_port": 18801,
    "tts_provider": "piper",
    "authorized_id": 123456789
  },
  "agents": [
    {
      "name": "sakura",
      "type": "flex",
      "display_name": "Sakura",
      "workspace_dir": "~/projects/hashi/workspaces/sakura",
      "is_active": true
    }
  ]
}
```

---

## Phase 5: Implementation Checklist

### 5.1 High Priority (Do First)

- [ ] Create `bridge-u.sh` (main entry point)
- [ ] Create `kill-sessions.sh` (cleanup script)
- [ ] Fix `main.py` InstanceLock class (cross-platform)
- [ ] Fix `adapters/base.py` process killing
- [ ] Create `requirements.txt` file

### 5.2 Medium Priority

- [ ] Create `start-agent.sh` and `stop-agent.sh`
- [ ] Create `restart-bridge.sh`
- [ ] Create `workbench-ctl.sh`
- [ ] Fix `orchestrator/banner.py` Unicode detection
- [ ] Update TTS provider factory

### 5.3 Low Priority

- [ ] Create workbench shell scripts
- [ ] Create `agents.json.linux.example`
- [ ] Update documentation (README.md)
- [ ] Create systemd service file (optional)

---

## Phase 6: Testing Plan

### 6.1 Unit Tests

```bash
# Test instance lock
python3 -c "
from main import InstanceLock
from pathlib import Path
lock = InstanceLock(Path('/tmp/test.lock'))
lock.acquire()
print('Lock acquired')
lock.release()
print('Lock released')
"

# Test process killing
python3 -c "
from adapters.base import BaseBackend
import asyncio
# ... test force_kill_process_tree
"
```

### 6.2 Integration Tests

```bash
# Start with all agents
./bridge-u.sh

# Start specific agent
./start-agent.sh sakura

# Stop agent
./stop-agent.sh sakura

# Kill all sessions
./kill-sessions.sh

# Workbench
./workbench-ctl.sh start
./workbench-ctl.sh status
./workbench-ctl.sh stop
```

### 6.3 Smoke Test

```bash
# Full cycle test
./bridge-u.sh --dry-run
./bridge-u.sh &
sleep 10
curl http://localhost:18800/api/health
./kill-sessions.sh
```

---

## Phase 7: Optional Enhancements

### 7.1 Systemd Service (for production)

Create `/etc/systemd/system/hashi.service`:
```ini
[Unit]
Description=Hashi Bridge-U-F Orchestrator
After=network.target

[Service]
Type=simple
User=lily
WorkingDirectory=/home/lily/projects/hashi
Environment=BRIDGE_HOME=/home/lily/projects/hashi
ExecStart=/home/lily/projects/hashi/.venv/bin/python main.py
ExecStop=/home/lily/projects/hashi/kill-sessions.sh --quiet
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 7.2 Docker Support (future)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install python-telegram-bot httpx aiohttp pillow
CMD ["python", "main.py"]
```

---

## Summary

**Total Files to Create:** 8 shell scripts  
**Total Python Files to Modify:** 4 files  
**Estimated Effort:** 4-6 hours  
**Risk Level:** Low (mostly straightforward port)

The migration is largely mechanical - replacing Windows-specific APIs with their Unix equivalents. The project architecture is already mostly cross-platform, with Windows-specific code properly isolated behind `os.name` checks.

---

*Document created by 小蕾 for 爸爸's hashi project Linux migration* 🌸
