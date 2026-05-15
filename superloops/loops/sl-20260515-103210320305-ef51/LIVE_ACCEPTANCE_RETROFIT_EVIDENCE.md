# Live Acceptance Retrofit Evidence

Loop: `sl-20260515-103210320305-ef51`  
Recording: `slrec-20260515-103210314239-bdf6`  
Updated by: `zelda@HASHI1`  
Date: 2026-05-16

## Why This Retrofit Exists

The first `audit_vibe_coding` loop exited after implementation, automated tests, smoke evidence, and independent final review. The user correctly identified that this was not strong enough for products with real runtime behavior.

The loop now treats safe live acceptance as a mandatory gate when a product promises real-world behavior such as rescue, restart, runtime supervision, network visibility, persistence, or user-facing operation.

## New Required Gate

Safe live acceptance must include:

1. Live-test preflight:
   - protected systems that must not be touched
   - exact target services/processes
   - independent control channel
   - auth level and shared-token path
   - dynamic port and route expectations
   - rollback/restore plan

2. Real live test:
   - invoke the deployed product through the intended external/user path
   - avoid local shortcuts or mock-only validation
   - include controlled off/on or failure/recovery behavior when relevant

3. Visibility and health verification:
   - status endpoints
   - list views such as `/remote list`
   - LAN/WSL/Windows perspectives when applicable
   - process state
   - audit logs
   - post-action health checks

4. Failure loop:
   - restore service first
   - record evidence
   - debug root cause
   - patch
   - rerun focused automated tests
   - rerun live acceptance
   - repeat until green

## Evidence From Watchtower Retest

The missing live gate was exercised retroactively against the Watchtower product:

- HASHI9:
  - stopped HASHI9 core
  - used Watchtower over LAN (`192.168.0.211:35821`)
  - verified rescue start returned `200`
  - verified HASHI9 core health returned `ok=true`
  - verified Watchtower advertised `rescue_start` and `supervisor=supervised`

- HASHI2:
  - upgraded HASHI2 Watchtower code and config
  - stopped only HASHI2 core
  - used Watchtower over LAN (`192.168.0.211:8767`)
  - initial live rescue failed because `bridge-u.sh` exited in non-interactive mode
  - fixed `clear || true` and Linux rescue command `--api-gateway`
  - reran automated tests
  - reran live rescue successfully
  - verified HASHI2 core health returned `ok=true`
  - verified HASHI2 Watchtower advertised `rescue_start` and `supervisor=supervised`

## Result

The `audit_vibe_coding` pattern is updated so future loops cannot exit on smoke tests or independent review alone when live runtime behavior is part of the product promise.

