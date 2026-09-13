# HASHI Integrations

[Install](INSTALL.md) · [User guide](USER_GUIDE.md) ·
[Configuration](CONFIGURATION.md) · [Troubleshooting](TROUBLESHOOTING.md)

Integrations are enabled per instance. A capability in source does not prove
that a particular installation has its dependencies, credentials, or live
validation.

## Engines and Model Providers

An Engine turns model capability into agentic work using tools, context,
control loops, and persistence. A Model Provider supplies inference.
HASHI separates these boundaries.

HER v2 is HASHI's native Engine. It owns a durable Engine Session and can use
configured Model Providers such as OpenRouter or DeepSeek. HASHI also connects
CLI engines such as Claude Code, Codex CLI, Gemini CLI, and Grok CLI, with
their own installation and authentication requirements. Other compatibility
surfaces, including xAI and Ollama, depend on the installed runtime catalogue.

Use /backend for selectable engines and /provider, /model, and /effort for
the active route's settings. These menus reflect instance opt-ins and
capabilities. This page intentionally does not duplicate a model/version list.

## TUI and messaging

The built-in TUI is the reference terminal client. Telegram is optional;
configure the agent's Bot Token reference and authorized user through the
local connection flow.

WhatsApp uses the Python neonize/Whatsmeow connector and QR linking. It does
not use whatsapp-web.js. Install the optional whatsapp dependencies in the
chosen environment, configure allowed senders, and preserve its local
session store as a credential. Use /agent in WhatsApp for routing.

Voice, attachments, and inline controls depend on the connector. See the
[dependency profiles](DEPENDENCIES.md) and [user guide](USER_GUIDE.md).

Workbench is retired. An independently installed desktop/web/mobile client
can use HASHI protocols. hashi ui is only a compatibility launcher for an
external client, not a bundled web application.

## Backend API and model Gateway

| Service | Purpose | Access boundary |
|---|---|---|
| Backend API / Persistent Session API | Agents, conversations, runs, events, and client controls | Authenticated client protocols; health/discovery surfaces have their own rules |
| API Gateway | Supported model requests through OpenAI-compatible endpoints | No caller authentication is enforced; keep this optional service private |
| Hashi Remote | Trusted peer discovery, messaging, file transfer, and scoped rescue | Peer trust and authentication are required for protected operations |

Do not send a Backend API credential to the model Gateway expecting it to
enforce that credential. /api reports the Gateway's actual address, live
state, and persisted settings. Its /v1/models endpoint reports the current
available catalogue.

OpenAI-compatible describes supported endpoints and schemas, not complete
parity with every API feature. In the caller-owned function-tool bridge, the
caller executes its functions and returns tool results; HASHI does not execute
them on the caller's behalf.

See the
[API guide](https://github.com/Bazza1982/HASHI/blob/main/docs/API_GUIDE.md),
[function-tool bridge](https://github.com/Bazza1982/HASHI/blob/main/docs/CODEX_API_TOOL_CALL_BRIDGE.md),
and [frontend contracts](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).

## Browser, device, and media tools

/browser helps select between HASHI browser tools, supported CLI-native
browsing, search/fetch, and a logged-in extension bridge. /usecomputer loads
computer-use guidance. A route selection does not itself install or validate
the worker, browser, credentials, or permission.

Use the route's diagnostics and actual results to establish availability.
A screenshot of a desktop does not prove the browser bridge is healthy, and
a local image path does not prove a model received native image input.

Browser/device control is platform-specific; the implementation and scoped
validation records are in the
[device-control design](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md)
and [extension bridge](https://github.com/Bazza1982/HASHI/blob/main/docs/OPTION_D_BROWSER_BRIDGE.md).
Native audio and fallback requirements are described in the
[audio design](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_NATIVE_AUDIO_CHAT_DESIGN.md).

## Workflows and trusted peers

Nagare coordinates workflows; Superloop supports longer-running controllers.
Some workflows require configured tools or local skills. The bundled workflow
library does not include every operator's private workflows and resources.

HChat uses local agent names for same-instance delivery and agent@INSTANCE
for cross-instance delivery. Remote supplies peer discovery and protected
protocol routes. Discovery alone is not permission to message, execute tools,
or control a peer.

Without a shared token, Remote operates in discovery-only mode for the
shared-token protocol. Configure trust and use the advertised peer
capabilities before messaging, attachments, or file transfer. Encryption
depends on the deployed TLS/overlay configuration; discovery and HMAC alone
do not imply encrypted transport.

See [Remote installation](INSTALL.md#hashi-remote),
[Remote protocol](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_REMOTE_RESCUE_PROTOCOL.md),
and [file transfer](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_REMOTE_FILE_TRANSFER_AND_ATTACHMENTS_PLAN.md).

WatchTower is a separately deployed external rescue service. HASHI can call
it through configured client controls; it is not the in-repository Remote
runtime. Restart/rescue controls have broader operational scope than a normal
Agent request.

## Team and enterprise profiles

Profiles, identity, policy, approvals, audit, and deployment artifacts are
available for Alpha evaluation. They do not certify unattended production
operation or validation against a particular organization's identity provider,
Kubernetes cluster, or SIEM.

Use the [readiness review](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_ENTERPRISE_AAI_READINESS_REVIEW.md)
and [deployment guide](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_ENTERPRISE_DEPLOYMENT.md)
for the declared scope and remaining gates.
