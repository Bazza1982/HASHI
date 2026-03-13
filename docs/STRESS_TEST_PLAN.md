# Bridge-U-F Automated Stress Test Plan v2

## Overview

This document defines a **3-hour automated stress testing workflow** that simulates real user behavior across all agents and functions in `bridge-u-f`.

**Philosophy**: Never stop. Fix what we can. Document what we can't. Report everything at the end.

### Test Philosophy

- **Mimic human behavior**: Natural pacing with breaks, not bombardment
- **Cover all functions**: Every command, every agent type, every backend
- **Self-healing**: Detect issues → Fix → Restart → Continue
- **Full observability**: Log analysis after each test phase

## Test Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    2-Hour Test Loop                             │
├─────────────────────────────────────────────────────────────────┤
│  Phase 1: Health Check & Baseline (5 min)                       │
│  ├─ Verify all agents online                                    │
│  ├─ Run smoke test on all agents                                │
│  └─ Capture baseline logs                                       │
├─────────────────────────────────────────────────────────────────┤
│  Phase 2: Fixed Agent Testing (25 min)                          │
│  ├─ Gemini agents: lily, coder, agent-dev                           │
│  ├─ Claude agent: claude-coder                                  │
│  ├─ Codex agent: codex-coder                                    │
│  └─ OpenRouter agent: temp                                      │
├─────────────────────────────────────────────────────────────────┤
│  Phase 3: Flex Agent Testing (15 min)                           │
│  ├─ Backend switching (gemini → claude → codex → gemini)        │
│  ├─ Handoff generation                                          │
│  └─ Memory operations                                           │
├─────────────────────────────────────────────────────────────────┤
│  Phase 4: Log Analysis & Issue Detection (5 min)                │
│  ├─ Scan errors.log for all agents                              │
│  ├─ Classify issues by DEBUGGING.md patterns                    │
│  └─ Decide: Fix / Skip / Escalate                               │
├─────────────────────────────────────────────────────────────────┤
│  Phase 5: Auto-Fix & Restart (5 min if needed)                  │
│  ├─ Apply low-risk fixes                                        │
│  ├─ Restart affected agent(s)                                   │
│  └─ Wait for stabilization                                      │
├─────────────────────────────────────────────────────────────────┤
│  Phase 6: Cooldown & Loop (5 min)                               │
│  ├─ Wait 60 seconds                                             │
│  └─ Loop back to Phase 1                                        │
└─────────────────────────────────────────────────────────────────┘
                    ↓ Repeat ~2 times per hour ↓
```

## Agent Matrix

| Agent | Type | Engine | Commands to Test |
|-------|------|--------|------------------|
| sakura | flex | gemini/claude/codex | /help, /status, /backend, /handoff, /memory, /model, /effort, /new, /clear, /stop, /retry, /think |
| lily | fixed | gemini-cli | /help, /status, /model, /new, /clear, /stop, /retry, /think |
| coder | fixed | gemini-cli | /help, /status, /model, /new, /clear, /stop, /retry, /think |
| agent-dev | fixed | gemini-cli | /help, /status, /model, /new, /clear, /stop, /retry, /think |
| claude-coder | fixed | claude-cli | /help, /status, /model, /effort, /new, /clear, /stop, /retry, /think |
| codex-coder | fixed | codex-cli | /help, /status, /model, /effort, /new, /clear, /stop, /retry, /think |
| temp | fixed | openrouter-api | /help, /status, /model, /credit, /new, /clear, /stop, /retry |

## Test Scenarios

### Scenario 1: Basic Chat (Per Agent)

```python
prompts = [
    "Hello! How are you today?",
    "What's 2 + 2?",
    "Tell me a short joke.",
    "Summarize what we discussed.",
]
```

### Scenario 2: Multi-Turn Conversation

```python
conversation = [
    "Let's play a game. I'm thinking of a number between 1 and 10.",
    "Higher",
    "Lower", 
    "Correct! Now your turn to think of a number.",
]
```

### Scenario 3: Code Tasks (Coder Agents Only)

```python
coding_prompts = [
    "Write a Python function to calculate fibonacci numbers.",
    "Add error handling to the previous function.",
    "Write unit tests for the fibonacci function.",
    "Refactor to use memoization.",
]
```

### Scenario 4: Long-Running Task (Background Mode Test)

```python
long_task = """
Analyze this complex scenario step by step:
1. Research the history of quantum computing
2. List 10 key milestones
3. Predict future developments
4. Write a 500-word summary
Take your time and be thorough.
"""
```

### Scenario 5: Flex Backend Switching

```python
flex_sequence = [
    ("/status", "Check current backend"),
    ("/backend", "View available backends"),
    # Switch to claude-cli
    ("Ask claude to analyze this text...", "Test claude"),
    ("/status", "Verify switch"),
    # Switch to codex-cli with context
    ("/backend", "Switch with handoff"),
    ("Continue the analysis...", "Test codex"),
    # Switch back to gemini-cli
    ("/backend", "Return to gemini"),
]
```

### Scenario 6: Error Recovery

```python
error_triggers = [
    "/stop",  # Interrupt current task
    "/retry", # Retry last message
    "/new",   # Start fresh session
    "/clear", # Clear context
]
```

### Scenario 7: Dynamic Agent Lifecycle

```python
lifecycle_test = [
    ("POST /api/admin/stop-agent", {"agent": "temp"}),
    ("GET /api/health", "Verify temp is offline"),
    ("POST /api/admin/start-agent", {"agent": "temp"}),
    ("GET /api/health", "Verify temp is back online"),
    ("POST /api/chat", {"agent": "temp", "text": "Are you back?"}),
]
```

## API Test Matrix

| Endpoint | Method | Test |
|----------|--------|------|
| /api/agents | GET | List all agents, verify count |
| /api/transcript/{name} | GET | Fetch recent messages |
| /api/transcript/{name}/poll | GET | Incremental fetch |
| /api/chat | POST | Send text message |
| /api/admin/commands/{name} | GET | List available commands |
| /api/admin/command | POST | Execute each command |
| /api/admin/smoke | POST | Run smoke test |
| /api/admin/start-agent | POST | Start stopped agent |
| /api/admin/stop-agent | POST | Stop running agent |
| /api/health | GET | Verify running agents |

## Log Analysis Rules

Based on `docs/DEBUGGING.md`, classify errors as:

### Auto-Fixable (Low Risk)

- **Telegram polling conflict**: Kill stale process, restart
- **OpenRouter closed client**: Restart agent
- **Empty inlineData**: Clear resume, restart with `/new`

### Manual Review Required

- **Claude nested session**: Environment variable issue
- **Codex chunk limit**: Prompt budget issue
- **PTY/ConPTY failures**: Windows compatibility issue

### Ignore (Noise)

- Transient network errors that auto-recover
- Telegram `httpx.ReadError` with subsequent success

## Implementation

### Entry Point: `stress_test.py`

```python
#!/usr/bin/env python3
"""
Bridge-U-F Automated Stress Test Runner
Run: python stress_test.py --duration 7200
"""

import asyncio
import argparse
import httpx
import json
import time
from pathlib import Path
from datetime import datetime

BASE_URL = "http://127.0.0.1:18800"
LOGS_DIR = Path("./logs")

class StressTestRunner:
    def __init__(self, duration_seconds: int = 7200):
        self.duration = duration_seconds
        self.start_time = None
        self.client = httpx.AsyncClient(timeout=120.0)
        self.results = []
        self.errors_found = []
        
    async def run(self):
        self.start_time = time.time()
        cycle = 0
        
        while self.elapsed() < self.duration:
            cycle += 1
            print(f"\n{'='*60}")
            print(f"CYCLE {cycle} | Elapsed: {self.elapsed():.0f}s / {self.duration}s")
            print(f"{'='*60}")
            
            await self.phase_health_check()
            await self.phase_fixed_agents()
            await self.phase_flex_agent()
            await self.phase_log_analysis()
            await self.phase_auto_fix()
            await self.phase_cooldown()
        
        await self.generate_report()
        await self.client.aclose()
    
    def elapsed(self) -> float:
        return time.time() - self.start_time if self.start_time else 0
    
    async def phase_health_check(self):
        """Phase 1: Health Check & Baseline (5 min)"""
        print("\n[Phase 1] Health Check & Baseline")
        
        # Check all agents online
        resp = await self.client.get(f"{BASE_URL}/api/health")
        health = resp.json()
        print(f"  Online agents: {health.get('agents', [])}")
        
        # Run smoke test
        resp = await self.client.post(
            f"{BASE_URL}/api/admin/smoke",
            json={"include_chat": False, "include_commands": True}
        )
        smoke = resp.json()
        print(f"  Smoke test: {'PASS' if smoke.get('ok') else 'FAIL'}")
        
        await asyncio.sleep(5)
    
    async def phase_fixed_agents(self):
        """Phase 2: Fixed Agent Testing (25 min)"""
        print("\n[Phase 2] Fixed Agent Testing")
        
        fixed_agents = [
            ("lily", "gemini-cli"),
            ("coder", "gemini-cli"),
            ("agent-dev", "gemini-cli"),
            ("claude-coder", "claude-cli"),
            ("codex-coder", "codex-cli"),
            ("temp", "openrouter-api"),
        ]
        
        for agent, engine in fixed_agents:
            if self.elapsed() > self.duration:
                break
            await self.test_fixed_agent(agent, engine)
            await asyncio.sleep(30)  # 30s between agents
    
    async def test_fixed_agent(self, agent: str, engine: str):
        """Test a single fixed agent"""
        print(f"\n  Testing {agent} ({engine})")
        
        # Get available commands
        resp = await self.client.get(f"{BASE_URL}/api/admin/commands/{agent}")
        commands = resp.json().get("commands", [])
        
        # Test each command
        for cmd in commands[:5]:  # Limit to 5 commands per agent
            resp = await self.client.post(
                f"{BASE_URL}/api/admin/command",
                json={"agent": agent, "command": f"/{cmd}"}
            )
            result = resp.json()
            status = "✓" if result.get("ok") else "✗"
            print(f"    /{cmd}: {status}")
            await asyncio.sleep(2)
        
        # Send a chat message
        resp = await self.client.post(
            f"{BASE_URL}/api/chat",
            json={"agent": agent, "text": f"Hi {agent}! Quick test: what is 1+1?"}
        )
        print(f"    Chat: {'sent' if resp.status_code == 200 else 'failed'}")
        
        # Wait for response
        await asyncio.sleep(15)
    
    async def phase_flex_agent(self):
        """Phase 3: Flex Agent Testing (15 min)"""
        print("\n[Phase 3] Flex Agent (sakura) Testing")
        
        # Test backend switching sequence
        backends = ["gemini-cli", "claude-cli", "codex-cli"]
        
        for backend in backends:
            if self.elapsed() > self.duration:
                break
            
            print(f"\n  Switching to {backend}")
            
            # Check status
            await self.client.post(
                f"{BASE_URL}/api/admin/command",
                json={"agent": "sakura", "command": "/status"}
            )
            
            # Send test message
            await self.client.post(
                f"{BASE_URL}/api/chat",
                json={"agent": "sakura", "text": f"Testing {backend}: what's your name?"}
            )
            
            await asyncio.sleep(30)
        
        # Test handoff
        print("\n  Testing /handoff")
        await self.client.post(
            f"{BASE_URL}/api/admin/command",
            json={"agent": "sakura", "command": "/handoff"}
        )
        
        # Test memory
        print("  Testing /memory")
        await self.client.post(
            f"{BASE_URL}/api/admin/command",
            json={"agent": "sakura", "command": "/memory"}
        )
        
        await asyncio.sleep(10)
    
    async def phase_log_analysis(self):
        """Phase 4: Log Analysis & Issue Detection (5 min)"""
        print("\n[Phase 4] Log Analysis")
        
        agents = ["sakura", "lily", "coder", "claude-coder", "codex-coder", "temp", "agent-dev"]
        
        for agent in agents:
            agent_logs = LOGS_DIR / agent
            if not agent_logs.exists():
                continue
            
            # Find latest session
            sessions = sorted(agent_logs.iterdir(), reverse=True)
            if not sessions:
                continue
            
            latest = sessions[0]
            errors_log = latest / "errors.log"
            
            if errors_log.exists():
                content = errors_log.read_text(encoding="utf-8", errors="ignore")
                lines = content.strip().split("\n")
                recent_errors = [l for l in lines[-20:] if l.strip()]
                
                if recent_errors:
                    print(f"  {agent}: {len(recent_errors)} recent error lines")
                    self.errors_found.extend([
                        {"agent": agent, "error": e} for e in recent_errors
                    ])
                else:
                    print(f"  {agent}: clean")
        
        await asyncio.sleep(5)
    
    async def phase_auto_fix(self):
        """Phase 5: Auto-Fix & Restart (5 min if needed)"""
        if not self.errors_found:
            print("\n[Phase 5] No fixes needed")
            return
        
        print(f"\n[Phase 5] Auto-Fix ({len(self.errors_found)} issues)")
        
        # Classify errors
        fixable = []
        for error in self.errors_found:
            if "Conflict: terminated by other getUpdates" in error.get("error", ""):
                fixable.append((error["agent"], "telegram_conflict"))
            elif "Cannot send a request, as the client has been closed" in error.get("error", ""):
                fixable.append((error["agent"], "closed_client"))
        
        # Apply fixes
        agents_to_restart = set()
        for agent, fix_type in fixable:
            print(f"  Fixing {agent}: {fix_type}")
            agents_to_restart.add(agent)
        
        # Restart affected agents
        for agent in agents_to_restart:
            print(f"  Restarting {agent}...")
            await self.client.post(
                f"{BASE_URL}/api/admin/stop-agent",
                json={"agent": agent}
            )
            await asyncio.sleep(2)
            await self.client.post(
                f"{BASE_URL}/api/admin/start-agent",
                json={"agent": agent}
            )
            await asyncio.sleep(5)
        
        self.errors_found = []
        await asyncio.sleep(10)
    
    async def phase_cooldown(self):
        """Phase 6: Cooldown & Loop (5 min)"""
        print("\n[Phase 6] Cooldown (60s)")
        await asyncio.sleep(60)
    
    async def generate_report(self):
        """Generate final test report"""
        print("\n" + "="*60)
        print("STRESS TEST COMPLETE")
        print("="*60)
        print(f"Duration: {self.elapsed():.0f} seconds")
        print(f"Errors found: {len(self.errors_found)}")
        
        # Save report
        report = {
            "timestamp": datetime.now().isoformat(),
            "duration_seconds": self.elapsed(),
            "errors": self.errors_found,
            "results": self.results,
        }
        
        report_path = LOGS_DIR / f"stress_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        report_path.write_text(json.dumps(report, indent=2))
        print(f"Report saved: {report_path}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=7200, help="Test duration in seconds")
    args = parser.parse_args()
    
    runner = StressTestRunner(duration_seconds=args.duration)
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
```

## Running the Test

### Targeted New-Feature Run (1 hour)

Use this after core regression coverage is already done. This run tests only newly added surfaces:
- new commands: `/active`, `/voice`, `/think`, `/fyi`, `/debug`, `/skill`, `/terminate`
- delegation skills: `/skill codex`, `/skill claude`, `/skill gemini`
- bridge APIs: `/api/bridge/message`, `/api/bridge/reply`, `/api/bridge/message/{id}`, `/api/bridge/thread/{id}`, `/api/bridge/capabilities/{agent}`, `/api/bridge/spawn` (expected reserved/not-implemented path)
- optional WhatsApp coverage (skip cleanly when disabled)

```bash
cd /path/to/bridge-u-f
python stress_test.py --duration 3600
```

### Quick Start (30 minutes)

```bash
cd /path/to/bridge-u-f
python stress_test.py --duration 1800
```

### Full Test (2 hours)

```bash
cd /path/to/bridge-u-f
python stress_test.py --duration 7200
```

### Monitor Live

In a separate terminal:
```bash
# Watch bridge logs
Get-Content logs\sakura\*\events.log -Tail 50 -Wait

# Watch errors across all agents
Get-ChildItem logs\*\*\errors.log | Get-Content -Tail 10
```

## Success Criteria

| Metric | Target |
|--------|--------|
| All agents stay online | 100% uptime |
| Commands succeed | >95% |
| Chat responses received | >90% |
| Auto-fixes applied | <5 per 2 hours |
| Manual escalations | 0 |

## Known Limitations

1. **Gemini CLI PTY issue**: May cause `AttachConsole failed` in background mode
2. **Claude nested session**: May fail if called from another Claude session
3. **Network transients**: Telegram polling may have brief interruptions

## Future Enhancements

- [ ] Add media upload testing
- [ ] Add concurrent user simulation
- [ ] Add response quality scoring
- [ ] Add performance metrics (latency, throughput)
- [ ] Add Telegram-side testing (via MTProto)
