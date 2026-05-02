# OLL for HASHI Integration Plan

Date: 2026-04-26

Status:
- proposed
- ready for staged implementation

Owners:
- browser UX: OLL extension
- public edge: HASHI Browser Gateway
- agent execution: existing HASHI Workbench and runtimes

## Executive Summary

Goal:
- turn the old OpenClaw-based OLL Chrome extension into a first-class HASHI browser companion
- let the user connect to HASHI1 over the internet with stable addressing
- support continuous chat with any HASHI agent
- optionally let HASHI read the current browser page, accept encrypted uploads, and later perform browser actions

Recommendation:
- reuse the old OLL sidepanel UX and browser interaction ideas
- do not reuse the old OpenClaw relay protocol
- build a new HASHI Browser Gateway in front of the existing local Workbench API
- keep the core minimal and move optional features into plug-in modules

Core principle:
- browser chat and browser control are separate capabilities
- chat must work even if page tools are disabled
- page read/write must be independently permissioned and independently deployable
- the extension is a thin touchpoint, while durable capability lives on the HASHI server side
- the system is designed for change: new clients, tools, transports, and policies should be addable without rewriting the core

## Why Rebuild Instead of Porting the Old Gateway

The old OLL extension was useful as a product shape:
- sidepanel chat
- page analysis
- screenshots
- element labels
- lightweight browser automation

But its architecture was tightly coupled to:
- temporary relay addresses
- custom `/chat`, `/reply`, `/page` endpoints
- OpenClaw-specific relay behavior

HASHI already has stronger primitives:
- local agent ingress: `orchestrator/workbench_api.py`
- cross-network secure transport and pairing: `remote/api/server.py`
- browser-native bridge and Chrome native messaging path:
  - `tools/browser_native_host.py`
  - `tools/browser_extension_bridge.py`
  - `tools/chrome_extension/hashi_browser_bridge/service_worker.js`

So the correct move is:
- preserve the OLL product surface
- replace the transport and session model
- integrate with HASHI-native routing, auth, logging, and browser tooling

## Product Definition

OLL becomes:
- a Chrome extension with a sidepanel UI
- a secure browser companion for HASHI
- a stable client for internet chat to HASHI1
- an optional remote page sensor and action surface

Primary user stories:
- chat with `lily`, `hashiko`, or any active agent from a work browser
- continue conversations across sessions from the same browser profile
- send page context or screenshot to the selected agent
- upload files privately to HASHI
- optionally let HASHI act on the active tab after explicit permission

Non-goals for phase 1:
- replacing the existing Workbench UI
- exposing direct public access to the Workbench API
- full zero-trust protection against local endpoint monitoring on the office computer

## Architecture

```text
Chrome Extension (OLL)
  -> HTTPS + app-layer encryption
HASHI Browser Gateway (public edge on HASHI1)
  -> localhost only
HASHI Workbench API
  -> runtimes / transcripts / browser tools / hchat
HASHI Agents
```

### Components

1. OLL Chrome Extension
- sidepanel chat UI
- settings and device identity
- encryption and key storage
- page capture modules
- optional browser action executor

2. HASHI Browser Gateway
- public internet entrypoint
- authentication, pairing, token issuance
- thread and session registry
- browser message correlation
- encrypted file ingest
- audit logging
- bridge to local Workbench API
- runs as a separate service/process from the HASHI core so it can be started, stopped, upgraded, or rolled back independently

3. Existing HASHI Workbench API
- agent discovery
- message enqueue
- transcript polling / reply capture
- bridge routing

4. Optional Browser Capability Modules
- page-read module
- screenshot module
- file-upload module
- action-execution module

## Design Goals

1. Privacy by default
- all internet traffic encrypted
- optional application-layer end-to-end encryption above TLS
- least privilege by capability scope

2. Stable addressing
- extension points to one fixed domain
- no temporary tunnel URLs in normal use

3. Minimal core
- chat path must be small and restartable
- page tooling must not be hardcoded into the core request path
- Browser Gateway should stay detachable from core HASHI so most updates do not require touching orchestrator internals

4. Modular capability model
- read-only and write-enabled browser features can be enabled separately
- files, screenshots, and actions use separate handlers and scopes

5. Compatibility
- gateway should internally reuse existing Workbench behavior
- preserve future compatibility with local browser bridge and cross-instance routing

6. Expandability by design
- the Browser Gateway owns durable protocols, policy, thread state, and audit behavior
- the extension should stay replaceable and should avoid becoming a capability monolith
- future clients such as mobile, desktop, or internal web apps should be able to reuse the same server contracts
- new modules should be attachable behind narrow interfaces rather than patched into the chat core

7. Strong logging
- enough detail for debugging, audit, replay, and support
- no unnecessary plaintext retention when encryption-sensitive mode is enabled

8. Operational isolation and control
- OLL public-edge functionality must run separately from the HASHI core runtime
- it must be possible to turn OLL on or off without stopping the main HASHI agent system
- operator control should be available from Telegram or another HASHI control surface

## Security Design

Important limitation:
- we can strongly protect network transit and intermediaries
- we cannot guarantee secrecy against deep monitoring on the office machine itself

### Threat Model

Protect against:
- network interception
- proxy inspection without endpoint control
- tunnel provider visibility into application plaintext
- replay of old browser requests
- stolen long-lived bearer tokens

Do not claim to protect against:
- compromised browser profile
- enterprise endpoint surveillance on the office PC
- malicious extensions already installed in the same profile

### Security Layers

1. Transport security
- HTTPS only
- TLS 1.3
- HSTS
- no plaintext HTTP fallback

2. Application-layer encryption
- extension and HASHI1 perform device pairing
- derive per-device long-term identity keys
- derive per-thread or per-session content keys
- encrypt:
  - chat messages
  - file uploads
  - screenshots
  - page-analysis payloads when enabled

3. Request authentication
- device registration with revocable device ID
- short-lived access token
- refresh token or re-pair flow
- optional signed request nonce to prevent replay

4. Capability scopes
- `chat`
- `file_upload`
- `page_read`
- `page_write`
- `browser_action`
- `admin_debug`

5. Confirmation and policy gates
- explicit opt-in before page-write mode
- per-domain allowlist / denylist
- optional approval prompt for destructive actions

### Recommended Cryptographic Shape

Use:
- WebCrypto in the extension
- X25519 or P-256 device key agreement
- AEAD payload encryption such as AES-GCM

Suggested model:
- device pairing creates browser device keys
- gateway stores public key and device metadata
- each thread negotiates a content key or derives one from a session secret
- ciphertext envelopes are passed through the public edge
- decrypt only inside the Browser Gateway process memory before Workbench injection

Optional future hardening:
- split decryption service from the public HTTP process
- memory-only plaintext mode with no plaintext disk persistence

Current limitation to state explicitly:
- even with transport and app-layer encryption, plaintext chat content will still enter existing HASHI transcript persistence after Workbench injection unless Workbench storage behavior is changed later

### Logging and Audit

Log every request with:
- request_id
- device_id
- user-visible thread_id
- selected agent
- capability scope
- ciphertext size
- decrypted payload type
- timestamp
- result status
- latency

Do not log plaintext by default for:
- uploaded files
- screenshots
- encrypted page snapshots

Allow debug mode to log sanitized excerpts only when explicitly enabled.

## Network and Public Access Design

### Stable Internet Entry

Requirement:
- the extension must use one fixed hostname, not a rotating tunnel URL

Recommended production path:
- user-owned subdomain such as `hashi.example.com`
- public edge backed by a stable tunnel or reverse proxy

Preferred option:
- Cloudflare Tunnel or equivalent stable tunnel bound to a fixed hostname

Why:
- no fixed public IP required
- no need to expose inbound ports directly
- extension stores a stable domain once

### Why Not Simple Redirects

A website redirect alone is insufficient because it does not solve:
- request signing
- websocket or streaming stability
- uploads
- auth continuity
- fixed backend identity

Use:
- direct stable API hostname for the gateway

Not:
- manual entry of rotating relay URLs

### Connection Strategy

For browser-to-gateway communication:
- REST for setup and low-frequency actions
- Server-Sent Events or WebSocket for streaming replies

Recommendation:
- use REST for:
  - login
  - pairing
  - thread creation
  - uploads
  - action acknowledgements
- use SSE first for reply-ready events and future control events

Reason:
- simpler than full duplex websocket
- easier debugging
- enough for chat and progress updates in phase 1

Clarification:
- phase 1 does not assume token-level model streaming from Workbench
- phase 1 SSE is a structured event channel for:
  - keepalive events
  - reply-ready events
  - error events
  - future action/control events

Operational note:
- SSE responses must emit periodic keepalive comments to survive tunnel or proxy idle timeouts
- access token lifetime and SSE lifetime must be designed together to avoid mid-wait expiry races

Fallback:
- long poll endpoint for compatibility if SSE is unavailable

## Conversation Model

The old OLL `/reply` polling model was too weak because it did not reliably correlate replies.

HASHI OLL should use an explicit session model:

- `device_id`: browser installation identity
- `agent_id`: selected HASHI agent
- `thread_id`: durable conversation thread
- `message_id`: unique outbound message
- `source_tag`: internal correlation key injected into Workbench

### Proposed Flow

1. Extension authenticates and lists agents.
2. User selects an agent.
3. Extension opens or resumes a `thread_id`.
4. Extension sends a message to Browser Gateway.
5. Browser Gateway:
   - decrypts payload
   - writes audit event
   - injects message into Workbench with a unique source tag
6. Gateway waits for the matching assistant completion via a Workbench completion primitive.
7. Gateway emits reply-ready SSE or returns final reply to the extension.
8. Thread state is updated for continuation.

### Source Correlation

Internal message source format:
- `browser:<device_id>:<thread_id>:<message_id>`

This lets the gateway:
- distinguish browser-originated messages from Telegram, TUI, or hchat
- wait only for the correct assistant reply
- avoid cross-talk when the same agent is active on multiple channels

### Required Workbench Primitive for Phase 1

Phase 1 depends on an integration point that does not exist today.

Requirement:
- Workbench must expose a source-safe completion primitive for browser-originated requests

Minimum acceptable shape:
- `await_completion(request_id)` or equivalent internal helper

Behavior:
- enqueue the browser request and retain request identity
- wait for the corresponding assistant completion for that exact request
- return completion status, final assistant text, optional metadata, and timeout or cancellation result

Non-goal:
- phase 1 does not require token-by-token streaming from Workbench

Why this must exist:
- transcript timing alone is not safe when the same agent is active on Telegram, browser, TUI, or hchat at the same time
- source tags identify request origin, but they do not identify the matching assistant completion unless Workbench preserves that linkage

## Public API Proposal

All endpoints below are for the new Browser Gateway, not the Workbench API.

### Session and Auth

- `GET /browser/health`
- `POST /browser/pair/request`
- `POST /browser/pair/complete`
- `POST /browser/auth/refresh`
- `POST /browser/auth/logout`

### Agents and Threads

- `GET /browser/agents`
- `GET /browser/threads`
- `POST /browser/thread/create`
- `POST /browser/thread/rename`
- `GET /browser/thread/{thread_id}`

### Chat

- `POST /browser/chat/send`
- `GET /browser/chat/stream/{thread_id}`
- `POST /browser/chat/ack`
- `POST /browser/chat/recover`

### Page Context

- `POST /browser/page/analyze`
- `POST /browser/page/screenshot`
- `POST /browser/page/selection`

### Files

- `POST /browser/files/upload`
- `GET /browser/files/{file_id}`

### Actions

- `POST /browser/action/request`
- `POST /browser/action/result`

### Admin and Audit

- `GET /browser/device/status`
- `POST /browser/device/revoke`
- `GET /browser/audit/recent`

## Browser Capability Modules

Each capability is optional and should be implemented behind a narrow interface.

### Module: Chat Core

Responsibilities:
- auth
- agent list
- thread list
- message send
- reply stream

Must not depend on:
- screenshot
- page analysis
- uploads
- action execution

### Module: Page Read

Responsibilities:
- current URL and title
- page text summary
- interactable element summary
- selected text

Permissions:
- read-only

### Module: Screenshot

Responsibilities:
- visible tab screenshot
- optional full-page capture later

Permissions:
- read-only but privacy-sensitive

### Module: File Upload

Responsibilities:
- encrypted upload packaging
- upload progress
- metadata extraction

Permissions:
- explicit user action only

### Module: Browser Action

Responsibilities:
- execute structured actions from gateway
- return structured result

Permissions:
- write-enabled
- can be disabled entirely

## Integration with Existing HASHI Components

### Reuse

Reuse as-is where possible:
- agent discovery from existing config/runtime metadata
- message injection through local Workbench API
- transcript observation logic
- browser native host and extension bridge concepts for future page/action convergence
- remote pairing concepts for device onboarding

### New Components to Build

1. `browser_gateway/`
- public HTTP service
- auth, threads, audit, encryption, uploads

2. `tools/chrome_extension/oll_hashi/`
- new extension UI
- settings, auth, threads, page modules

3. `state/browser_gateway.sqlite` or equivalent
- devices
- threads
- message ledger
- nonces
- audit metadata

4. optional adapter layer in Workbench API
- source-safe completion helper for browser conversations

5. optional service controller
- enable or disable the OLL module independently from HASHI core
- surface operator controls such as `/oll on`, `/oll off`, `/oll status`

## Expandability Model

OLL should be treated as one client surface, not as the permanent home of system capability.

Design rule:
- move durable intelligence, policy, routing, storage, encryption coordination, and audit to HASHI
- keep the extension focused on:
  - rendering UI
  - collecting explicit browser-side inputs
  - executing approved browser-side actions

This keeps the system flexible in several ways:

1. Client flexibility
- the same Browser Gateway can later support:
  - another Chrome extension variant
  - Edge with the same extension codebase
  - a desktop app
  - a mobile companion
  - an internal web console

2. Capability flexibility
- new server modules can be added without redesigning the extension core:
  - richer file exchange
  - structured workflows
  - new action policies
  - cross-instance routing
  - memory-aware thread continuation

3. Deployment flexibility
- the public edge can change from one tunnel or reverse proxy strategy to another without changing the browser product model

4. Runtime flexibility
- the selected agent backend can change inside HASHI without changing the browser client contract

In short:
- OLL is not the ultimate capability container
- OLL is the user-facing touchpoint
- HASHI is the capability platform

## Data Model

### Devices

Fields:
- `device_id`
- `device_label`
- `public_key`
- `created_at`
- `last_seen_at`
- `status`
- `scopes`
- `recovery_state`
- `last_pairing_version`

### Threads

Fields:
- `thread_id`
- `device_id`
- `agent_id`
- `instance_id`
- `title`
- `created_at`
- `updated_at`
- `last_message_at`
- `status`
- `agent_transcript_checkpoint`

### Messages

Fields:
- `message_id`
- `thread_id`
- `direction`
- `ciphertext_ref`
- `plaintext_summary` optional and sanitized
- `source_tag`
- `status`
- `created_at`
- `completed_at`

### Files

Fields:
- `file_id`
- `device_id`
- `thread_id`
- `filename`
- `ciphertext_path`
- `media_type`
- `size_bytes`
- `created_at`

## Logging Plan

### Browser Gateway Logs

- access log
- auth log
- device pairing log
- message ledger log
- upload log
- page module log
- action execution log
- structured error log

Recommended structured fields across all gateway logs:
- `module`
- `event`
- `request_id`
- `device_id`
- `thread_id`
- `agent_id`
- `instance_id`
- `scope`
- `status`
- `latency_ms`
- `bytes_in`
- `bytes_out`
- `error_code`
- `error_detail_sanitized`

### Extension Logs

- connectivity status
- auth events
- thread lifecycle
- upload progress
- page module errors
- action execution results

Privacy rule:
- extension should expose a user-debug export mode
- default logs should avoid plaintext content

### Operational Control Logs

The OLL module should log:
- gateway start
- gateway stop
- config reload
- `/oll on`
- `/oll off`
- `/oll status`
- operator identity
- reason when provided

These events should be stored separately from routine chat traffic so service-state changes are easy to audit.

## Operational Isolation

OLL should run as an optional module, not as part of the mandatory HASHI core startup path.

Requirements:
- Browser Gateway runs as a separate process or service
- its config, logs, and state storage remain logically separate from core HASHI runtime state
- it can be restarted independently
- it can fail independently without taking down agent runtimes

Suggested separation:
- separate service entrypoint
- separate state file or sqlite database
- separate log file family
- separate enable flag in configuration

## Operator Controls

Phase 1 should include a control surface equivalent to:
- `/oll on`
- `/oll off`
- `/oll status`

Recommended behavior:
- `/oll on`
  - starts or enables Browser Gateway if configured
  - records an operator log event
- `/oll off`
  - stops or disables the public Browser Gateway
  - keeps durable state unless explicit cleanup is requested
  - records an operator log event
- `/oll status`
  - reports whether Browser Gateway is enabled, running, reachable, and on which hostname

Preferred integration path:
- implement these as Telegram-accessible HASHI operator commands that manage the separate OLL service rather than embedding OLL into the core process

## Backward and Forward Compatibility

Backwards-compatible defaults:
- if no page capabilities are granted, chat still works
- if SSE fails, long poll can be used
- if app-layer encryption is disabled for early development, TLS-only mode can still function in dev
- browser action module can be omitted without affecting chat threads

Forward compatibility:
- support agent addressing that can later extend to `agent@INSTANCE`
- keep thread model independent from a specific runtime backend
- allow future mobile or desktop clients to use the same Browser Gateway API

## Phased Delivery Plan

### Phase 0: Architecture Spike

Deliverables:
- this design doc
- API contract draft
- threat model and logging requirements

Exit criteria:
- user approves fixed-domain and auth direction

### Phase 1: Stable Chat Core

Scope:
- Browser Gateway skeleton
- fixed-domain deployment path
- auth and device registration
- list agents
- create/resume threads
- send message
- source-safe completion wait path from Workbench
- SSE keepalive and reply-ready event delivery
- `/oll on`, `/oll off`, `/oll status`
- separate process/service deployment and logs

Do not include:
- screenshots
- uploads
- page actions
- token-level streaming from Workbench

Exit criteria:
- extension can talk to any local HASHI1 agent over the internet using one stable hostname
- conversations survive browser restarts
- operator can enable or disable OLL without stopping HASHI core

### Phase 2: Encrypted File Uploads

Scope:
- client-side file encryption
- file upload API
- agent message attachment references
- device recovery path for lost browser keys or re-pair

Exit criteria:
- extension can upload encrypted files and attach them to a thread
- user can recover from lost device keys without silent orphaned state

### Phase 3: Page Read Features

Scope:
- page summary
- visible screenshot
- selected text capture
- explicit user-triggered send-to-agent
- thread consistency checks against agent transcript checkpoints

Exit criteria:
- user can send page context from office Chrome to HASHI agents
- resume flow detects when browser thread state and agent context have drifted

### Phase 4: Browser Actions

Scope:
- request/approve/execute action loop
- structured actions and results
- policy gates

Exit criteria:
- HASHI can perform controlled browser actions with clear audit trails

### Phase 5: Cross-Instance Routing

Scope:
- agent selection by instance
- use HASHI1 as exchange or direct route chooser

Exit criteria:
- browser client can reach agents beyond local HASHI1 when policy allows

## Implementation Work Breakdown

### Workstream A: Browser Gateway

Tasks:
- create gateway service package
- add auth, token, device registry
- add thread registry
- add source-tagged chat send path
- add matching completion path backed by Workbench request identity
- add SSE keepalive and structured event channel
- add structured logging
- add rate limiting per device
- add independent service lifecycle management

### Workstream B: Extension

Tasks:
- create new extension skeleton
- implement login and pairing
- implement thread list and chat UI
- implement connectivity and retry UX
- implement upload and page modules later

### Workstream C: Workbench Integration

Tasks:
- expose or reuse agent list
- add browser-safe reply capture helper
- define and implement `await_completion(request_id)` or equivalent
- ensure transcript correlation does not cross channels

### Workstream D: Deployment

Tasks:
- choose public hostname
- set up stable tunnel/reverse proxy
- issue TLS certificates
- define secret storage and rotation plan

### Workstream E: Security Hardening

Tasks:
- finalize key exchange design
- implement encrypted payload envelope
- add replay protection
- add device revocation and session expiry

## Risks

1. Endpoint privacy expectations too strong
- risk: user assumes office IT cannot see anything at all
- mitigation: document exact threat boundary clearly

2. Transcript correlation race conditions
- risk: wrong reply attached to wrong browser thread
- mitigation: explicit source tags plus a Workbench completion primitive bound to request identity

3. Browser action safety
- risk: unintended clicks or form submissions
- mitigation: read-only default and domain policy gates

4. Tunnel/vendor dependency
- risk: deployment coupled to one provider
- mitigation: keep hostname and gateway protocol provider-agnostic

5. Extension store and native host divergence
- risk: old browser bridge and new OLL extension overlap confusingly
- mitigation: treat OLL as a separate extension with a separate purpose

6. Lost browser keys and orphaned threads
- risk: browser storage reset breaks device identity and thread continuity
- mitigation: re-pair and recovery workflow with explicit thread reassociation policy

7. Gateway abuse or token theft
- risk: public edge can be spammed or abused
- mitigation: rate limiting, short-lived tokens, revocation, and operator disable switch

8. Agent context drift
- risk: browser thread appears resumable while agent transcript was reset
- mitigation: transcript checkpoint tracking and resume-time warning or reseed flow

## Recommended Immediate Next Steps

1. Approve the product boundary:
- OLL is a HASHI browser client, not a public Workbench clone

2. Approve the network shape:
- fixed hostname on user-owned domain
- Browser Gateway in front of local Workbench

3. Approve the security baseline:
- TLS mandatory
- app-layer encryption for messages, files, screenshots

4. Start implementation in this order:
- Workbench completion primitive first
- Browser Gateway phase 1
- extension chat shell
- thread persistence
- encrypted uploads
- page read
- browser actions

## Proposed File Layout

```text
docs/
  PLAN_OLL_HASHI_INTEGRATION.md

browser_gateway/
  __init__.py
  app.py
  service_control.py
  auth.py
  devices.py
  threads.py
  crypto.py
  uploads.py
  audit.py
  schemas.py

tools/chrome_extension/
  oll_hashi/
    manifest.json
    service_worker.js
    sidepanel.html
    sidepanel.js
    styles.css
```

## Decision Summary

Decisions made in this proposal:
- rebuild OLL transport for HASHI rather than porting the OpenClaw gateway
- use a dedicated Browser Gateway as the public edge
- keep Workbench private and local-only
- use stable hostname instead of rotating relay URLs
- support app-layer encryption on top of TLS
- separate chat core from page and action modules
- use explicit thread and source correlation for reliable continuous conversations
