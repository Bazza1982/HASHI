# HASHI Multi-Instance Port Isolation Debug Session -- 2026-03-14

## Overview

This document records the debugging session on 2026-03-14 that identified and fixed port isolation failures when running HASHI1 and HASHI2 as concurrent instances on the same machine.

---

## Problems Identified

1. HASHI2 frontend (port 5175) was wiring to HASHI1 backend (port 3001) instead of its own backend (3003)
2. HASHI2 bridge launched on port 18800 instead of 18802 -- conflicting with HASHI1
3. PM2 processes named with wrong port suffix (*-18800 instead of *-18802)
4. Browser opened to localhost:5173 (HASHI1 port) instead of localhost:5175 (HASHI2 port)
5. Onboarding completing without preserving HASHI_BRIDGE_PORT env var

---

## Root Cause

Two-step port drop in the launch chain:

- Hashi2_Start.bat correctly sets HASHI_BRIDGE_PORT=18802
- bridge-u.sh spawns onboarding_main.py WITHOUT passing HASHI_BRIDGE_PORT
- Onboarding completes and re-invokes bridge-u.sh WITHOUT HASHI_BRIDGE_PORT
- Result: defaults to port 18800, PM2 names become *-18800, browser opens to 5173

The env var was set at the top of the launch chain but not propagated through the two subprocess handoffs (bat -> sh -> python -> sh again).

---

## Fixes Applied (branch: fix/v1-multi-instance-port-isolation)

### 1. bin/bridge-u.sh
- Exports HASHI_BRIDGE_PORT before spawning onboarding
- Passes it back on re-entry after onboarding completes
- Ensures the env var survives the full launch chain

### 2. Hashi2_Stop.bat
- Upgraded to kill ALL HASHI2 processes regardless of which port they accidentally started on
- Port sweep covers: 18800-18803, 3003, 5175
- Uses SIGTERM -> SIGKILL escalation to handle stubborn processes
- Ensures a clean slate for relaunch even after a mis-started session

### 3. .gitignore
- Added workspaces/ to prevent local agent workspace data from being pushed
- Added *.bak to prevent backup files from being committed

---

## Instance Port Map (for reference)

| Instance              | Bridge Port | Backend Port | Frontend Port |
|-----------------------|-------------|--------------|---------------|
| HASHI0 (Windows/Claude) | --        | --           | --            |
| HASHI1                | 18800       | 3001         | 5173          |
| HASHI2                | 18802       | 3003         | 5175          |

---

## Files Changed on This Branch

- .gitignore
- bin/bridge-u.sh
- bin/workbench-ctl.sh
- main.py
- onboarding/onboarding_main.py
- workbench/ecosystem.config.cjs
- workbench/package.json
- workbench/package-lock.json
- workbench/vite.config.js

---

## Verification

After fixes applied:

1. Nuclear reset of HASHI2 processes via Hashi2_Stop.bat
2. Relaunch via Hashi2_Start.bat
3. Onboarding flows with port 18802 confirmed in logs
4. PM2 process names show *-18802 suffix
5. Browser opens to localhost:5175
6. HASHI2 workbench shows the correct Chinese conversation (HASHI2-specific content, not HASHI1)

HASHI1 remained unaffected throughout -- no cross-instance interference after fix.

---

## Merge Readiness Assessment (2026-03-14)

- Branch fix/v1-multi-instance-port-isolation is 3 commits ahead of main
- main has no diverging commits -- fast-forward or clean merge is possible
- Dry-run merge result: no conflicts
- Branch is safe to merge to main
