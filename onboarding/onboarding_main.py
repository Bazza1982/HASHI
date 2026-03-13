import textwrap
import os
import sys
import json
import time
import subprocess
import shutil
import traceback
from pathlib import Path
from datetime import datetime

# ── Crash log setup ──────────────────────────────────────────────────────────
_LOG_PATH = Path(__file__).parent.parent / "onboarding_crash.log"

def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

def _clear_log():
    try:
        _LOG_PATH.write_text(f"=== Onboarding run: {datetime.now().isoformat()} ===\n", encoding="utf-8")
    except Exception:
        pass
# ─────────────────────────────────────────────────────────────────────────────

# Colors mimicking bridge-u.sh
C_RESET = "\033[0m"
_BOLD = "\033[1m"
_BLINK = "\033[5m"
_BRIGHT_WHITE = "\033[97m"
C_ACCENT = "\033[38;5;111m"
C_OK = "\033[38;5;114m"
C_WARN = "\033[38;5;180m"
C_ERR = "\033[38;5;203m"
C_MUTED = "\033[90m"
C_TITLE = "\033[1;38;5;153m"
C_LABEL = "\033[38;5;109m"
C_TEXT = "\033[97m"
C_RAIL = "\033[38;5;61m"

def load_languages():
    langs = []
    lang_dir = Path(__file__).parent / "languages"
    order = ["english.json", "japanese.json", "chinese_sim.json", "chinese_trad.json", "korean.json", "german.json", "french.json", "russian.json", "arabic.json"]
    found_files = list(lang_dir.glob("*.json"))
    found_files.sort(key=lambda x: order.index(x.name) if x.name in order else 99)
    for f in found_files:
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
                data['_file'] = f.name
                langs.append(data)
        except:
            pass
    return langs

def audit_environment():
    checks = [
        ("Claude Code", "claude-cli", ["claude", "-v"]),
        ("Gemini CLI", "gemini-cli", ["gemini", "--version"]),
        ("Codex CLI", "codex-cli", ["codex", "--version"]),
    ]
    for name, engine, cmd in checks:
        try:
            if shutil.which(cmd[0]):
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    return name, engine
        except:
            pass
    return None, None

def verify_openrouter(key):
    import http.client
    try:
        conn = http.client.HTTPSConnection("openrouter.ai")
        headers = {"Authorization": f"Bearer {key}"}
        conn.request("GET", "/api/v1/models", headers=headers)
        res = conn.getresponse()
        return res.status == 200
    except:
        return False

def print_rich_banner():
    os.system("")
    print(f"\n{C_RAIL}│{C_RESET} {C_TITLE}{_BOLD}HASHI ハシ 橋{C_RESET}")
    print(f"{C_RAIL}│{C_RESET}")
    print(f"{C_RAIL}│{C_RESET}  {C_ACCENT}「橋」は「知」を繋ぎ、「知」は未来を拓く。{C_RESET}")
    print(f"{C_RAIL}│{C_RESET}  {C_MUTED}The Bridge connects Intellect; Intellect opens the future.{C_RESET}")
    print(f"{C_RAIL}│{C_RESET}")
    print(f"{C_RAIL}│{C_RESET}  HASHI is a Universal Flexible Safe AI Agents powered by CLI backends.")
    print(f"{C_RAIL}│{C_RESET}  This is the onboarding program.")
    print(f"{C_RAIL}│{C_RESET}")
    print(f"{C_RAIL}│{C_RESET}  Please select your preferred language to continue:")
    print(f"{C_RAIL}│{C_RESET}  継続するには、使用する言語を選択してください：")
    print(f"{C_RAIL}│{C_RESET}")

def run_onboarding():
    _clear_log()
    _log(f"Python: {sys.executable} {sys.version}")
    _log(f"CWD: {os.getcwd()}")

    langs = load_languages()
    _log(f"Languages loaded: {len(langs)}")
    if not langs:
        print("Error: No language files found in onboarding/languages/")
        return

    # Phase 1: Language Selection
    lang = None
    while True:
        print_rich_banner()
        for i, l in enumerate(langs, 1):
            print(f"  {C_ACCENT}[{i}]{C_RESET} {C_TEXT}{l['displayName']}{C_RESET}")
        choice = input(f"\n{C_MUTED}> {C_RESET}").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(langs):
                lang = langs[idx]
                _log(f"Language selected: {lang.get('displayName')} (file: {lang.get('_file')})")
                break
            else:
                print(f"\n{C_WARN}Invalid choice. Please select a number between 1 and {len(langs)}.{C_RESET}")
                time.sleep(1.5)
        except ValueError:
            print(f"\n{C_WARN}Invalid input. Please enter a number.{C_RESET}")
            time.sleep(1.5)

    # Language mapping
    lang_file = lang.get('_file', '').lower()
    l_code = 'en'
    if 'chinese_sim' in lang_file: l_code = 'zh'
    elif 'chinese_trad' in lang_file: l_code = 'zh-tw'
    elif 'japanese' in lang_file: l_code = 'ja'
    elif 'korean' in lang_file: l_code = 'ko'
    elif 'german' in lang_file: l_code = 'de'
    elif 'french' in lang_file: l_code = 'fr'
    elif 'russian' in lang_file: l_code = 'ru'
    elif 'arabic' in lang_file: l_code = 'ar'

    _log(f"Language code: {l_code}")

    # Phase 1.2: Legal Disclaimer (Robust implementation)
    disclaimer_dir = Path(__file__).parent / "languages"
    disclaimer_file = disclaimer_dir / f"disclaimer_{l_code}.md"
    if not disclaimer_file.exists():
        disclaimer_file = disclaimer_dir / "disclaimer_en.md"
    if disclaimer_file.exists():
        content_md = disclaimer_file.read_text(encoding="utf-8")
        print(f"\n{C_WARN}{'='*72}{C_RESET}")
        print(f"{C_TITLE}{_BOLD}  LEGAL NOTICE & RISK ACKNOWLEDGEMENT  {C_RESET}")
        print(f"{C_WARN}{'='*72}{C_RESET}\n")
        for line in content_md.splitlines():
            print(f"  {C_TEXT}{line}{C_RESET}")
        print(f"\n{C_WARN}{'='*72}{C_RESET}")
        while True:
            print(f"\n{C_WARN}{_BOLD}Type \"I AGREE\" in all caps to confirm and proceed:{C_RESET}")
            consent = input(f"{C_MUTED}> {C_RESET}").strip()
            if consent == "I AGREE":
                _log("User consented to legal terms.")
                break
            elif consent.lower() in ["q", "quit", "exit", "n", "no"]:
                print(f"\n{C_ERR}Consent not provided. Exiting...{C_RESET}\n")
                sys.exit(0)
            else:
                print(f"{C_WARN}Invalid input. You must type \"I AGREE\" (exactly as shown) to continue.{C_RESET}")
    # Immediate Hatchery Initialization
    project_root = Path(__file__).parent.parent
    _log(f"project_root: {project_root}")
    workspace_dir = project_root / "workspaces" / "onboarding_agent"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    _log(f"workspace_dir created: {workspace_dir}")
    shutil.copy(project_root / "docs" / "initial.md", workspace_dir / "initial.md")
    shutil.copy(project_root / "docs" / "AGENT_FYI.md", workspace_dir / "AGENT_FYI.md")
    _log(f"initial.md and AGENT_FYI.md copied to {workspace_dir}")

    # 1. Prime the conversation log
    log_path = workspace_dir / "conversation_log.jsonl"
    welcome_prompt = lang.get('welcomePrompt', "Hello! I am ready to assist you.")

    if log_path.exists():
        log_path.unlink()

    first_entry = {
        "timestamp": datetime.now().isoformat(),
        "role": "user",
        "source": "system",
        "text": welcome_prompt
    }
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(first_entry, ensure_ascii=True) + "\n")

    # 2. Write the one-time wakeup trigger
    wakeup_path = workspace_dir / "WAKEUP.prompt"
    with open(wakeup_path, 'w', encoding='utf-8') as f:
        f.write(welcome_prompt)

    # Phase 1.5: Completion Detection
    _log("Phase 1.5: Completion detection")
    agents_path = project_root / "agents.json"
    secrets_path = project_root / "secrets.json"
    is_completed = False
    if agents_path.exists():
        try:
            with open(agents_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                if any(a.get('name') == 'hashiko' for a in cfg.get('agents', [])):
                    is_completed = True
        except:
            pass

    if is_completed:
        print(f"\n{C_WARN}{lang['alreadyCompleted']}{C_RESET}")
        confirm = input(f"{C_LABEL}{lang['resetConfirm']}{C_RESET}").strip().lower()
        if confirm != 'y':
            print(f"\n{C_OK}{lang['enjoyMessage']}{C_RESET}")
            sys.exit(0)

    # Phase 1.8: Pre-Hatch Warm Prompt
    print(f"\n{C_OK}{lang['continueToHatch'].format(style=_BOLD+_BLINK+_BRIGHT_WHITE, reset=C_RESET+C_OK)}{C_RESET}")
    input()

    # Phase 2: System Audit
    _log("Phase 2: System audit starting")
    print(f"\n{C_LABEL}{lang['auditStart']}{C_RESET}")
    cli_name, engine_name = audit_environment()
    _log(f"Audit result: cli={cli_name}, engine={engine_name}")

    or_key = None
    if cli_name:
        print(f"{C_OK}{lang['auditResultCli'].format(cli=cli_name)}{C_RESET}")
    else:
        print(f"{C_WARN}{lang['auditNoSignal']}{C_RESET}")
        while True:
            or_key = input(f"{C_LABEL}{lang['openRouterPrompt']}{C_RESET}").strip()
            if not or_key: continue
            print(f"{C_MUTED}{lang['openRouterVerifying']}{C_RESET}")
            if verify_openrouter(or_key):
                print(f"{C_OK}{lang['openRouterSuccess']}{C_RESET}")
                engine_name = "openrouter-api"
                break
            else:
                print(f"{C_ERR}{lang['openRouterFail']}{C_RESET}")

    # Phase 4: Determine agent display name by language (no user-specific content)
    _display_names = {
        "zh":    "小乔",
        "zh-tw": "小喬",
        "ja":    "ハシコ",
        "ko":    "하시코",
        "ru":    "Хашико",
        "ar":    "هاشيكو",
    }
    agent_name_local = _display_names.get(l_code, "Hashiko")

    # Config Generation
    current_engine = engine_name if engine_name else "gemini-cli"
    default_models = {
        "gemini-cli": "gemini-3.1-pro-preview",
        "claude-cli": "claude-sonnet-4-6",
        "codex-cli": "gpt-5.4",
        "openrouter-api": "anthropic/claude-sonnet-4.6",
    }

    new_agent = {
        "name": "hashiko",
        "display_name": agent_name_local,
        "emoji": "🐣",
        "type": "flex",
        "engine": current_engine,
        "system_md": "docs/initial.md",
        "workspace_dir": "workspaces/onboarding_agent",
        "is_active": True,
        "model": default_models.get(current_engine, "default"),
        "allowed_backends": [
            {"engine": "gemini-cli", "model": default_models["gemini-cli"]},
            {"engine": "claude-cli", "model": default_models["claude-cli"]},
            {"engine": "codex-cli", "model": default_models["codex-cli"]},
            {"engine": "openrouter-api", "model": default_models["openrouter-api"]},
        ],
        "active_backend": current_engine,
    }

    agents_cfg = {"global": {"authorized_id": 0}, "agents": [new_agent]}
    if agents_path.exists():
        try:
            with open(agents_path, 'r', encoding='utf-8') as f:
                old_cfg = json.load(f)
                agents_cfg['global'] = old_cfg.get('global', agents_cfg['global'])
                old_agents = [a for a in old_cfg.get('agents', []) if a.get('name') != 'hashiko']
                agents_cfg['agents'] = [new_agent] + old_agents
        except:
            pass
    with open(agents_path, 'w', encoding='utf-8') as f:
        json.dump(agents_cfg, f, indent=2, ensure_ascii=False)
    _log(f"agents.json written: {agents_path}")

    secrets = {}
    if secrets_path.exists():
        try:
            with open(secrets_path, 'r', encoding='utf-8') as f:
                secrets = json.load(f)
        except:
            pass
    if 'hashiko' not in secrets:
        secrets['hashiko'] = "WORKBENCH_ONLY_NO_TOKEN"
    if or_key:
        secrets['openrouter-api_key'] = or_key
    with open(secrets_path, 'w', encoding='utf-8') as f:
        json.dump(secrets, f, indent=2, ensure_ascii=False)

    last_agents_path = project_root / ".bridge_u_last_agents.txt"
    with open(last_agents_path, 'w', encoding='utf-8') as f:
        f.write("selected|hashiko\n")

    print(f"\n{C_OK}{lang['hatcheryComplete']}{C_RESET}")
    print(f"{C_TEXT}{lang['launching']}{C_RESET}")

    main_sh = project_root / "bin" / "bridge-u.sh"
    _log(f"bridge-u.sh path: {main_sh} | exists: {main_sh.exists()}")
    if main_sh.exists():
        os.chmod(main_sh, 0o755)
        (project_root / ".bridge_u_lang.txt").write_text(l_code, encoding="utf-8")

        # Kill any stale HASHI orchestrator process so bridge-u.sh won't prompt
        _log("Checking for stale HASHI processes...")
        my_pid = os.getpid()
        try:
            # Only match the bridge orchestrator main.py (has --bridge-home flag)
            result = subprocess.run(
                ["pgrep", "-f", "main.py.*--bridge-home"],
                capture_output=True, text=True
            )
            pids = [p for p in result.stdout.strip().split() if p and int(p) != my_pid]
            for pid in pids:
                try:
                    os.kill(int(pid), 15)  # SIGTERM
                    _log(f"Killed stale HASHI process PID {pid}")
                except Exception:
                    pass
            if pids:
                time.sleep(1)  # Give it a moment to die
        except Exception as e:
            _log(f"Preflight kill attempt: {e}")

        # Clean up stale lock/pid files
        for f in [project_root / ".bridge_u_f.pid", project_root / ".bridge_u_f.lock"]:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

        args = ["/usr/bin/bash", str(main_sh), "--workbench", "--resume-last", "--force"]
        _log(f"Launching bridge-u.sh via subprocess: {args}")

        # Use subprocess.run so we can capture the exit code for logging
        # stdout/stderr are inherited (user sees output on screen)
        result = subprocess.run(args, cwd=str(project_root))
        exit_code = result.returncode
        _log(f"bridge-u.sh exited with code: {exit_code}")
        if exit_code != 0:
            import signal as _sig
            # Decode signal number (exit code 128+N or raw N)
            sig_num = exit_code - 128 if exit_code > 128 else exit_code
            try:
                sig_name = _sig.Signals(sig_num).name
            except ValueError:
                sig_name = "unknown"
            _log(f"Exit code {exit_code} → signal {sig_num} ({sig_name})")
        sys.exit(exit_code)
    else:
        _log("ERROR: bridge-u.sh not found!")
        print(f"\n{C_ERR}Error: bridge-u.sh not found.{C_RESET}")

if __name__ == "__main__":
    try:
        run_onboarding()
    except KeyboardInterrupt:
        _log("Aborted by user (KeyboardInterrupt)")
        print(f"\n{C_MUTED}Aborted.{C_RESET}")
        sys.exit(0)
    except Exception as e:
        tb = traceback.format_exc()
        _log(f"CRITICAL CRASH:\n{tb}")
        print(f"\n{C_ERR}Critical Error: {e}{C_RESET}")
        print(f"{C_MUTED}Full log: {_LOG_PATH}{C_RESET}")
        sys.exit(1)
