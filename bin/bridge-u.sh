#!/usr/bin/env bash
#
# HASHI Launcher for Linux (macOS untested)
# Interactive menu with rich system info
#

set -euo pipefail

# Colors
C_RESET="\033[0m"
C_ACCENT="\033[38;5;111m"
C_OK="\033[38;5;114m"
C_WARN="\033[38;5;222m"
C_ERR="\033[38;5;203m"
C_MUTED="\033[90m"
C_TITLE="\033[1;38;5;153m"
C_LABEL="\033[38;5;109m"
C_TEXT="\033[97m"
C_RAIL="\033[38;5;61m"

# Paths
# Navigate to project root (parent of bin/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

export BRIDGE_CODE_ROOT="$SCRIPT_DIR"
export BRIDGE_HOME="${BRIDGE_HOME:-$SCRIPT_DIR}"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# Options
WORKBENCH_LAUNCH=0
API_GATEWAY_LAUNCH=0
SELECTED_AGENTS=""
AUTO_RESUME_LAST=0
FORCE_LAUNCH=0
DRY_RUN=0

# State file for resuming
STATE_FILE="$BRIDGE_HOME/.bridge_u_last_agents.txt"

# Agent arrays
declare -a ACTIVE_AGENTS=()
declare -a ACTIVE_BACKENDS=()
declare -a ACTIVE_TYPES=()
declare -a ACTIVE_TOKEN_STATUS=()
declare -a INACTIVE_AGENTS=()
declare -a INACTIVE_BACKENDS=()
LAST_MODE=""
LAST_AGENTS=""

# System info
WHATSAPP_ENABLED="no"
WHATSAPP_DEFAULT_AGENT=""
WORKBENCH_PORT=""
CLI_GEMINI="missing"
CLI_CLAUDE="missing"
CLI_CODEX="missing"
PYTHON_VERSION=""
SECRETS_EXIST="no"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --resume-last) AUTO_RESUME_LAST=1; shift ;;
        --force) FORCE_LAUNCH=1; shift ;;
        --workbench|-w) WORKBENCH_LAUNCH=1; shift ;;
        --api-gateway|-a) API_GATEWAY_LAUNCH=1; shift ;;
        --agents) SELECTED_AGENTS="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo "Options:"
            echo "  --resume-last       Automatically resume last selected agents"
            echo "  --workbench, -w     Start workbench UI"
            echo "  --api-gateway, -a   Enable API gateway (port 18801)"
            echo "  --agents NAME       Start specific agent(s)"
            echo "  --dry-run           Show what would be done"
            echo "  --help, -h          Show this help"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

print_banner() {
    local title="$1"
    local subtitle="${2:-}"
    echo ""
    echo -e "${C_RAIL}│${C_RESET} ${C_TITLE}${title}${C_RESET}  ${C_MUTED}${subtitle}${C_RESET}"
    echo -e "${C_RAIL}│${C_RESET}"
}

check_system_info() {
    # Python version
    PYTHON_VERSION=$(python3 --version 2>/dev/null | cut -d' ' -f2 || echo "unknown")
    
    # CLI tools
    command -v gemini &>/dev/null && CLI_GEMINI="ok"
    command -v claude &>/dev/null && CLI_CLAUDE="ok"
    command -v codex &>/dev/null && CLI_CODEX="ok"
    
    # Secrets file
    local secrets_file="$BRIDGE_HOME/secrets.json"
    [[ ! -f "$secrets_file" ]] && secrets_file="$BRIDGE_CODE_ROOT/secrets.json"
    [[ -f "$secrets_file" ]] && SECRETS_EXIST="yes"
}

load_agents() {
    local helper_script="$SCRIPT_DIR/scripts/launcher_helper.py"
    
    if [[ ! -f "$helper_script" ]]; then
        echo -e "${C_WARN}launcher_helper.py not found!${C_RESET}"
        exit 1
    fi
    
    # Parse all info using Python helper script
    local output
    output=$(python3 "$helper_script" 2>&1)
    if [[ $? -ne 0 ]]; then
        echo -e "${C_WARN}Failed to parse agents.json!${C_RESET}"
        echo "$output"
        exit 1
    fi
    
    eval "$output"
    
    if [[ ${#ACTIVE_AGENTS[@]} -eq 0 ]]; then
        echo -e "${C_WARN}No active agents found in agents.json.${C_RESET}"
        exit 1
    fi
}

load_last_state() {
    if [[ -f "$STATE_FILE" ]]; then
        IFS='|' read -r LAST_MODE LAST_AGENTS < "$STATE_FILE" || true
    fi
}

is_in_last() {
    local name="$1"
    if [[ "$LAST_MODE" == "all" ]]; then
        echo 1
        return
    fi
    if [[ "$LAST_MODE" == "selected" && -n "$LAST_AGENTS" ]]; then
        for agent in $LAST_AGENTS; do
            if [[ "$agent" == "$name" ]]; then
                echo 1
                return
            fi
        done
    fi
    echo 0
}

render_menu() {
    clear
    
    local wb_label="OFF"
    [[ "$WORKBENCH_LAUNCH" == "1" ]] && wb_label="ON"
    local api_label="OFF"
    [[ "$API_GATEWAY_LAUNCH" == "1" ]] && api_label="ON"
    
    print_banner "HASHI LAUNCHER" "Multi-agent orchestrator"
    
    # ── System Info ──
    echo -e "${C_RAIL}│${C_RESET} ${C_ACCENT}System${C_RESET}"
    echo -e "${C_RAIL}│${C_RESET}   Python          ${C_TEXT}${PYTHON_VERSION}${C_RESET}"
    
    # CLI tools on one line
    local cli_line=""
    if [[ "$CLI_GEMINI" == "ok" ]]; then
        cli_line="${C_OK}gemini${C_RESET}"
    else
        cli_line="${C_ERR}gemini${C_RESET}"
    fi
    if [[ "$CLI_CLAUDE" == "ok" ]]; then
        cli_line="$cli_line  ${C_OK}claude${C_RESET}"
    else
        cli_line="$cli_line  ${C_ERR}claude${C_RESET}"
    fi
    if [[ "$CLI_CODEX" == "ok" ]]; then
        cli_line="$cli_line  ${C_OK}codex${C_RESET}"
    else
        cli_line="$cli_line  ${C_ERR}codex${C_RESET}"
    fi
    echo -e "${C_RAIL}│${C_RESET}   CLI tools       $cli_line"
    
    if [[ "$SECRETS_EXIST" == "yes" ]]; then
        echo -e "${C_RAIL}│${C_RESET}   secrets.json    ${C_OK}found${C_RESET}"
    else
        echo -e "${C_RAIL}│${C_RESET}   secrets.json    ${C_ERR}missing${C_RESET}"
    fi
    
    echo -e "${C_RAIL}│${C_RESET}"
    
    # ── Services ──
    echo -e "${C_RAIL}│${C_RESET} ${C_ACCENT}Services${C_RESET}"
    
    if [[ "$wb_label" == "ON" ]]; then
        echo -e "${C_RAIL}│${C_RESET}   Workbench       ${C_OK}ON${C_RESET} (:${WORKBENCH_PORT})"
    else
        echo -e "${C_RAIL}│${C_RESET}   Workbench       ${C_MUTED}OFF${C_RESET} (:${WORKBENCH_PORT})"
    fi
    
    if [[ "$api_label" == "ON" ]]; then
        echo -e "${C_RAIL}│${C_RESET}   API Gateway     ${C_OK}ON${C_RESET} (:18801)"
    else
        echo -e "${C_RAIL}│${C_RESET}   API Gateway     ${C_MUTED}OFF${C_RESET} (:18801)"
    fi
    
    if [[ "$WHATSAPP_ENABLED" == "yes" ]]; then
        local wa_info="configured"
        [[ -n "$WHATSAPP_DEFAULT_AGENT" ]] && wa_info="default: $WHATSAPP_DEFAULT_AGENT"
        echo -e "${C_RAIL}│${C_RESET}   WhatsApp        ${C_OK}enabled${C_RESET} ($wa_info)"
    else
        echo -e "${C_RAIL}│${C_RESET}   WhatsApp        ${C_MUTED}disabled${C_RESET}"
    fi
    
    echo -e "${C_RAIL}│${C_RESET}"
    
    # ── Agents Summary ──
    local token_ok=0
    local token_missing=0
    for status in "${ACTIVE_TOKEN_STATUS[@]}"; do
        if [[ "$status" == "ok" ]]; then
            ((token_ok++)) || true
        else
            ((token_missing++)) || true
        fi
    done
    
    echo -e "${C_RAIL}│${C_RESET} ${C_ACCENT}Agents${C_RESET}"
    echo -e "${C_RAIL}│${C_RESET}   Active          ${C_TEXT}${#ACTIVE_AGENTS[@]}${C_RESET}"
    echo -e "${C_RAIL}│${C_RESET}   Inactive        ${C_TEXT}${#INACTIVE_AGENTS[@]}${C_RESET}"
    if [[ $token_missing -gt 0 ]]; then
        echo -e "${C_RAIL}│${C_RESET}   Tokens          ${C_OK}${token_ok} ok${C_RESET}  ${C_WARN}${token_missing} missing${C_RESET}"
    else
        echo -e "${C_RAIL}│${C_RESET}   Tokens          ${C_OK}${token_ok} ok${C_RESET}"
    fi
    
    echo -e "${C_RAIL}│${C_RESET}"
    echo ""
    
    # ── Active Roster ──
    echo -e "${C_ACCENT}Active Roster${C_RESET}"
    for i in "${!ACTIVE_AGENTS[@]}"; do
        local idx=$((i + 1))
        local name="${ACTIVE_AGENTS[$i]}"
        local backend="${ACTIVE_BACKENDS[$i]}"
        local atype="${ACTIVE_TYPES[$i]}"
        local token="${ACTIVE_TOKEN_STATUS[$i]}"
        
        # Build status indicators
        local indicators=""
        [[ "$atype" == "flex" ]] && indicators="${C_ACCENT}flex${C_RESET} "
        
        if [[ "$token" == "ok" ]]; then
            indicators="${indicators}${C_OK}token${C_RESET}"
        else
            indicators="${indicators}${C_WARN}no-token${C_RESET}"
        fi
        
        local last_mark=""
        [[ "$(is_in_last "$name")" == "1" ]] && last_mark=" ${C_OK}[last]${C_RESET}"
        
        echo -e "  ${C_ACCENT}[$idx]${C_RESET} $(printf '%-12s' "$name") ${C_MUTED}[${backend}]${C_RESET} ${indicators}${last_mark}"
    done
    echo ""
    
    # ── Inactive Roster ──
    echo -e "${C_ACCENT}Inactive Roster${C_RESET}"
    if [[ ${#INACTIVE_AGENTS[@]} -gt 0 ]]; then
        for i in "${!INACTIVE_AGENTS[@]}"; do
            local name="${INACTIVE_AGENTS[$i]}"
            local backend="${INACTIVE_BACKENDS[$i]}"
            echo -e "  ${C_MUTED}- ${name}  [${backend}]${C_RESET}"
        done
    else
        echo -e "  ${C_MUTED}- none${C_RESET}"
    fi
    echo ""
    
    # ── Actions ──
    echo -e "${C_ACCENT}Actions${C_RESET}"
    echo -e "  ${C_ACCENT}[1]${C_RESET} Start all active agents"
    echo -e "  ${C_ACCENT}[2]${C_RESET} Start same as last time"
    echo -e "  ${C_ACCENT}[3]${C_RESET} Choose agents now"
    echo -e "  ${C_ACCENT}[W]${C_RESET} Toggle workbench"
    echo -e "  ${C_ACCENT}[A]${C_RESET} Toggle API gateway"
    echo -e "  ${C_ACCENT}[Q]${C_RESET} Quit"
    echo ""
}

choose_agents() {
    clear
    print_banner "AGENT SELECTION" "Choose one or more active agents"
    
    for i in "${!ACTIVE_AGENTS[@]}"; do
        local idx=$((i + 1))
        local name="${ACTIVE_AGENTS[$i]}"
        local backend="${ACTIVE_BACKENDS[$i]}"
        local token="${ACTIVE_TOKEN_STATUS[$i]}"
        
        local token_mark=""
        if [[ "$token" == "ok" ]]; then
            token_mark="${C_OK}token${C_RESET}"
        else
            token_mark="${C_WARN}no-token${C_RESET}"
        fi
        
        local last_mark=""
        [[ "$(is_in_last "$name")" == "1" ]] && last_mark=" ${C_OK}[last]${C_RESET}"
        
        echo -e "  ${C_ACCENT}[$idx]${C_RESET} $(printf '%-12s' "$name") ${C_MUTED}[${backend}]${C_RESET} ${token_mark}${last_mark}"
    done
    echo ""
    
    echo -ne "${C_MUTED}Enter one or more numbers separated by spaces: ${C_RESET}"
    read -r choice_list
    
    [[ -z "$choice_list" ]] && return 1
    
    SELECTED_AGENTS=""
    for num in $choice_list; do
        # Validate number
        if [[ "$num" =~ ^[0-9]+$ ]] && [[ "$num" -ge 1 ]] && [[ "$num" -le "${#ACTIVE_AGENTS[@]}" ]]; then
            local idx=$((num - 1))
            local agent_name="${ACTIVE_AGENTS[$idx]}"
            # Check if already selected
            if [[ -z "$SELECTED_AGENTS" ]]; then
                SELECTED_AGENTS="$agent_name"
            elif [[ ! " $SELECTED_AGENTS " =~ " $agent_name " ]]; then
                SELECTED_AGENTS="$SELECTED_AGENTS $agent_name"
            fi
        fi
    done
    
    if [[ -z "$SELECTED_AGENTS" ]]; then
        echo -e "${C_WARN}No valid agents selected.${C_RESET}"
        sleep 2
        return 1
    fi
    
    return 0
}

ensure_env() {
    if [[ ! -d .venv ]]; then
        echo -e "${C_MUTED}Creating virtual environment...${C_RESET}"
        python3 -m venv .venv
    fi
    
    source .venv/bin/activate
    
    # Check dependencies
    if ! python3 -c "import telegram, httpx, aiohttp, PIL" 2>/dev/null; then
        echo -e "${C_WARN}Installing Python dependencies...${C_RESET}"
        pip install --quiet python-telegram-bot httpx aiohttp pillow
    fi
}

preflight_check() {
    # Check if bridge-u-f is already running using multiple methods
    local pid_file="$SCRIPT_DIR/.bridge_u_f.pid"
    local lock_file="$SCRIPT_DIR/.bridge_u_f.lock"
    local existing_pid=""
    
    # Method 1: Check PID file
    if [[ -f "$pid_file" ]]; then
        existing_pid=$(cat "$pid_file" 2>/dev/null)
        if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
            local cmdline
            cmdline=$(ps -p "$existing_pid" -o args= 2>/dev/null || true)
            if [[ ! "$cmdline" =~ main\.py ]]; then
                # Not our process, reset
                existing_pid=""
            fi
        else
            existing_pid=""
        fi
    fi
    
    # Method 2: Search for running main.py process
    if [[ -z "$existing_pid" ]]; then
        # Find python processes running main.py with our bridge-home
        existing_pid=$(pgrep -f "main.py --bridge-home $SCRIPT_DIR" 2>/dev/null | head -1 || true)
        
        # Fallback: search all python main.py processes
        if [[ -z "$existing_pid" ]]; then
            for pid in $(pgrep -f "python.*main.py" 2>/dev/null || true); do
                local cmdline
                cmdline=$(ps -p "$pid" -o args= 2>/dev/null || true)
                if [[ "$cmdline" =~ "$SCRIPT_DIR" ]]; then
                    existing_pid="$pid"
                    break
                fi
            done
        fi
    fi
    
    # No running instance found
    if [[ -z "$existing_pid" ]]; then
        # Clean up stale files if any
        rm -f "$pid_file" "$lock_file" 2>/dev/null
        return 0
    fi
    
    # Verify the PID is valid
    if ! kill -0 "$existing_pid" 2>/dev/null; then
        rm -f "$pid_file" "$lock_file" 2>/dev/null
        return 0
    fi
    
    # Process is running - ask user (or auto-kill if --force)
    echo ""
    echo -e "  ${C_WARN}HASHI is already running (PID $existing_pid)${C_RESET}"
    echo ""

    echo "[$(date +%T)] preflight: found existing PID=$existing_pid FORCE=$FORCE_LAUNCH" >> "$BRIDGE_LOG"
    if [[ "$FORCE_LAUNCH" == "1" ]]; then
        echo -e "  ${C_MUTED}Force mode: stopping existing instance automatically...${C_RESET}"
    else
        echo -ne "  Kill existing instance and continue? [y/N] "
        read -r -n 1 confirm
        echo ""
        if [[ "${confirm,,}" != "y" ]]; then
            echo -e "  ${C_MUTED}Aborted.${C_RESET}"
            return 1
        fi
    fi
    
    echo -e "  ${C_MUTED}Stopping existing instance...${C_RESET}"
    
    # Try graceful kill first
    kill "$existing_pid" 2>/dev/null || true
    
    # Wait up to 5 seconds
    local waited=0
    while kill -0 "$existing_pid" 2>/dev/null && [[ $waited -lt 5 ]]; do
        sleep 1
        ((waited++)) || true
    done
    
    # Force kill if still running
    if kill -0 "$existing_pid" 2>/dev/null; then
        echo -e "  ${C_WARN}Force killing...${C_RESET}"
        kill -9 "$existing_pid" 2>/dev/null || true
        sleep 1
    fi
    
    # Clean up files
    rm -f "$pid_file" "$lock_file" 2>/dev/null
    
    echo -e "  ${C_OK}Previous instance stopped.${C_RESET}"
    sleep 1
    return 0
}

launch() {
    local start_label="$1"
    local py_args="$2"

    # In force mode, skip preflight (caller already cleaned up stale processes)
    if [[ "$FORCE_LAUNCH" != "1" ]]; then
        if ! preflight_check; then
            exit 1
        fi
    fi
    
    clear
    print_banner "HASHI BOOT" "Multi-agent orchestrator launch"
    
    echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}Agents           ${C_RESET} ${C_TEXT}${start_label}${C_RESET}"
    
    if [[ "$WORKBENCH_LAUNCH" == "1" ]]; then
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}Workbench        ${C_RESET} ${C_OK}starting${C_RESET} (:${WORKBENCH_PORT})"
        if [[ -x "$SCRIPT_DIR/bin/workbench-ctl.sh" ]]; then
            "$SCRIPT_DIR/bin/workbench-ctl.sh" start --open &
        else
            echo -e "${C_WARN}workbench-ctl.sh not found, skipping workbench${C_RESET}"
        fi
    else
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}Workbench        ${C_RESET} ${C_MUTED}disabled${C_RESET}"
    fi
    
    if [[ "$API_GATEWAY_LAUNCH" == "1" ]]; then
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}API Gateway      ${C_RESET} ${C_OK}enabled${C_RESET} (:18801)"
        py_args="$py_args --api-gateway"
    else
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}API Gateway      ${C_RESET} ${C_MUTED}disabled${C_RESET}"
    fi
    
    if [[ "$WHATSAPP_ENABLED" == "yes" ]]; then
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}WhatsApp         ${C_RESET} ${C_OK}enabled${C_RESET}"
    else
        echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}WhatsApp         ${C_RESET} ${C_MUTED}disabled${C_RESET}"
    fi
    
    echo -e "${C_RAIL}│${C_RESET}"
    
    if [[ "$DRY_RUN" == "1" ]]; then
        echo -e "${C_WARN}DRY RUN - would execute:${C_RESET}"
        echo "  python3 main.py --bridge-home $BRIDGE_HOME $py_args"
        exit 0
    fi
    
    echo -e "${C_RAIL}│${C_RESET} ${C_LABEL}Status           ${C_RESET} ${C_OK}launching...${C_RESET}"
    echo -e "${C_RAIL}│${C_RESET}"
    echo ""
    
    # ── Wakeup prompt injector ──────────────────────────────────────────────
    # If onboarding wrote a WAKEUP.prompt, send it to Hashiko as soon as
    # the orchestrator API is up — so the response is ready when the
    # browser window opens.
    local wakeup_file="$BRIDGE_HOME/workspaces/onboarding_agent/WAKEUP.prompt"
    if [[ -f "$wakeup_file" ]]; then
        local hashi_api="http://localhost:${WORKBENCH_PORT:-18800}"
        (
            echo "[$(date +%T)] wakeup-injector: waiting for HASHI API..." >> "$BRIDGE_LOG"
            for i in $(seq 1 40); do
                sleep 1
                if curl -sf --max-time 2 "$hashi_api/api/health" >/dev/null 2>&1; then
                    sleep 1  # reduced grace: let agent fully register
                    echo "[$(date +%T)] wakeup-injector: sending prompt..." >> "$BRIDGE_LOG"
                    json_body=$(python3 -c "
import json, sys
prompt = open(sys.argv[1], encoding='utf-8').read()
print(json.dumps({'agent': 'hashiko', 'text': prompt}))
" "$wakeup_file")
                    curl -s -X POST "$hashi_api/api/chat" \
                        -H "Content-Type: application/json" \
                        -d "$json_body" \
                        >/dev/null 2>&1 && \
                    rm -f "$wakeup_file" && \
                    echo "[$(date +%T)] wakeup-injector: done, prompt sent and file removed" >> "$BRIDGE_LOG"
                    break
                fi
            done
        ) &
    fi
    # ────────────────────────────────────────────────────────────────────────

    echo "[$(date +%T)] launching: python3 main.py --bridge-home $BRIDGE_HOME $py_args" >> "$BRIDGE_LOG"
    python3 main.py --bridge-home "$BRIDGE_HOME" $py_args
    local py_exit=$?
    echo "[$(date +%T)] main.py exited: code=$py_exit" >> "$BRIDGE_LOG"

    echo ""
    echo -e "${C_MUTED}HASHI stopped.${C_RESET}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

# ── Debug trap ────────────────────────────────────────────────────────────────
BRIDGE_LOG="$SCRIPT_DIR/bridge_launch.log"
echo "=== bridge-u.sh start: $(date) args=$* ===" >> "$BRIDGE_LOG"
_bridge_exit_trap() {
    local code=$?
    echo "[$(date +%T)] bridge-u.sh EXIT code=$code" >> "$BRIDGE_LOG"
}
trap '_bridge_exit_trap' EXIT
# ─────────────────────────────────────────────────────────────────────────────

# Initial setup
ensure_env
echo "[$(date +%T)] ensure_env done" >> "$BRIDGE_LOG"
check_system_info
load_agents
load_last_state

# Auto-resume mode (for restart scripts)
if [[ "$AUTO_RESUME_LAST" == "1" ]]; then
    if [[ "$LAST_MODE" == "all" ]]; then
        launch "all active agents (resumed)" ""
    elif [[ "$LAST_MODE" == "selected" && -n "$LAST_AGENTS" ]]; then
        launch "$LAST_AGENTS (resumed)" "--agents $LAST_AGENTS"
    else
        launch "all active agents" ""
    fi
    exit 0
fi

# Pre-selected agents via command line
if [[ -n "$SELECTED_AGENTS" ]]; then
    launch "$SELECTED_AGENTS" "--agents $SELECTED_AGENTS"
    exit 0
fi

# Interactive menu loop
while true; do
    render_menu
    
    echo -ne "${C_MUTED}Select option: ${C_RESET}"
    read -r -n 1 choice
    echo ""
    
    case "${choice,,}" in
        1)
            # Start all active agents
            echo "all|" > "$STATE_FILE"
            launch "all active agents" ""
            exit 0
            ;;
        2)
            # Start same as last time
            if [[ "$LAST_MODE" == "all" ]]; then
                launch "all active agents (same as last time)" ""
            elif [[ "$LAST_MODE" == "selected" && -n "$LAST_AGENTS" ]]; then
                launch "$LAST_AGENTS (same as last time)" "--agents $LAST_AGENTS"
            else
                echo -e "${C_WARN}No saved previous selection yet. Falling back to all active agents.${C_RESET}"
                sleep 2
                echo "all|" > "$STATE_FILE"
                launch "all active agents" ""
            fi
            exit 0
            ;;
        3)
            # Choose agents
            if choose_agents; then
                echo "selected|$SELECTED_AGENTS" > "$STATE_FILE"
                launch "$SELECTED_AGENTS" "--agents $SELECTED_AGENTS"
                exit 0
            fi
            ;;
        w)
            # Toggle workbench
            if [[ "$WORKBENCH_LAUNCH" == "1" ]]; then
                WORKBENCH_LAUNCH=0
            else
                WORKBENCH_LAUNCH=1
            fi
            ;;
        a)
            # Toggle API gateway
            if [[ "$API_GATEWAY_LAUNCH" == "1" ]]; then
                API_GATEWAY_LAUNCH=0
            else
                API_GATEWAY_LAUNCH=1
            fi
            ;;
        q)
            echo -e "${C_MUTED}Goodbye!${C_RESET}"
            exit 0
            ;;
        *)
            # Invalid option, just refresh
            ;;
    esac
done
