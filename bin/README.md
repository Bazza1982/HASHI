# HASHI Launcher Scripts

This directory contains platform-specific launcher scripts for HASHI.

**Platform Support:** Scripts are tested on Windows and Linux. macOS is untested but may work (standard bash).

---

## Main Launchers

### Launch HASHI

**Linux:**
```bash
./bin/bridge-u.sh
```

**Windows:**
```cmd
bin\bridge-u.bat
```

**PowerShell (Windows):**
```powershell
.\bin\bridge_ctl.ps1
```

---

## Agent Management

### Start Agent
```bash
./bin/start-agent.sh [agent_name]    # Linux
bin\start-agent.bat [agent_name]     # Windows
```

### Stop Agent
```bash
./bin/stop-agent.sh [agent_name]     # Linux
bin\stop-agent.bat [agent_name]      # Windows
```

---

## Workbench

### Start Workbench
```bash
./bin/workbench-ctl.sh start         # Linux
bin\workbench.bat                    # Windows
.\bin\workbench_ctl.ps1 start        # PowerShell
```

### Stop Workbench
```bash
./bin/workbench-ctl.sh stop          # Linux
bin\stop_workbench.bat               # Windows
.\bin\workbench_ctl.ps1 stop         # PowerShell
```

### Restart Workbench
```bash
./bin/workbench-ctl.sh restart       # Linux
bin\restart_workbench.bat            # Windows
.\bin\workbench_ctl.ps1 restart      # PowerShell
```

---

## System Management

### Restart HASHI
```bash
./bin/restart-bridge.sh              # Linux
bin\restart_bridge_u_f.bat           # Windows
```

### Kill All Sessions
```bash
./bin/kill-sessions.sh               # Linux
bin\kill_bridge_u_f_sessions.bat     # Windows
```

---

## Onboarding

### Run Onboarding
```cmd
bin\onboard.bat                      # Windows
```

Or use the npm CLI:
```bash
hashi-onboard                        # If installed via npm
```

Or directly:
```bash
python onboarding/onboarding_main.py
```

---

## Chrome/Browser Helpers

### Start Linux Chrome (WSL)
```bash
./bin/start-linux-chrome.sh
```

---

## Note

Most of these scripts are wrappers around `main.py` or other Python entry points. You can also run HASHI directly with:

```bash
python main.py [--agents agent1 agent2] [--api-gateway]
```

See `python main.py --help` for all options.
