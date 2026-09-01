# HASHI Frontend Connector Architecture

| Field | Value |
|---|---|
| Status | **Authoritative Frontend Connector module specification** |
| Effective date | 2026-09-01 |
| Parent architecture | [HASHI System Architecture](../ARCHITECTURE.md) |
| Scope | Built-in TUI, messaging connectors, Backend API, Persistent Session API, Remote projection, and compatible external clients |

## 1. Definition

Frontend Connectors expose HASHI to users and compatible clients without
creating a second source of Agent, Session, Message, Run, Event, PCM, or Engine
state. A Connector translates between one user-facing transport and the typed
PAO/PCM/Engine contracts.

The Connector belongs to HASHI. A separately developed graphical product that
uses the Connector does not.

## 2. HASHI-owned connector surfaces

HASHI includes and maintains:

- the built-in reference TUI;
- Telegram and WhatsApp connectors;
- the local Backend API;
- Persistent Session API v1;
- HChat and required Hashi Remote client projections;
- shared attachment, approval, delivery, notification, and control contracts;
  and
- client-neutral capability negotiation and qualification tooling.

The built-in TUI remains part of HASHI and is not planned for extraction into a
separate product. It is the reference terminal client for local operation.

## 3. External-client boundary

Any compatible desktop, web, mobile, IDE, or operations client may use HASHI
infrastructure when it conforms to the published protocol and security rules.

HASHI owns:

- protocol versions and capability discovery;
- authentication, authorization, identity, and resource boundaries;
- canonical Conversation Sessions, Messages, Runs, and Events;
- Engine and PCM integration;
- attachments, approvals, controls, replay, and fencing; and
- transport-neutral error and terminal semantics.

An external client owns:

- window layout and presentation;
- unsent drafts and local convenience state;
- its packaging, installation, update, and release channel;
- product-specific data and final product-domain authorization; and
- disposable caches that can be rebuilt from HASHI state.

No external client name, repository revision, installer, or private release
channel may be compiled into general HASHI admission policy. Compatibility is
defined by protocol conformance and declared limits.

## 4. Authority and projection

```text
User interface
  -> Frontend Connector
  -> PAO Conversation Session / Message / Run / Event
  -> PCM and selected Engine
  -> PAO terminal state and delivery decision
  -> Frontend Connector projection
  -> User interface
```

Connectors may maintain delivery cursors, render caches, typing state, and other
bounded projections. They must not:

- maintain a competing authoritative sent-message archive;
- resend client-owned chat history as if it were canonical HASHI Context;
- infer authorization from possession of an opaque identifier;
- expose provider-native thread or request IDs as Session authority; or
- let a late client or worker overwrite a fenced or terminal Run.

## 5. Built-in TUI

The TUI is a permanent HASHI Frontend Connector and reference local client. It
supports local operation and trusted instance switching through Hashi Remote.

Current implementation boundary:

- the TUI uses the basic Backend API chat and transcript routes;
- its cross-instance path proxies only a small named operation set through
  authenticated Hashi Remote peers; and
- it does not yet implement the complete Persistent Session API v1 multi-
  Session surface.

This current limitation must be stated plainly. Future TUI development should
adopt the richer Session/Event contract without changing the rule that the TUI
stays inside HASHI.

## 6. Backend API and Persistent Session API

The **Backend API** is HASHI's local runtime API for built-in and authenticated
clients. The **Persistent Session API v1** is the canonical richer contract for
client-neutral multi-Session state, ordered Events, controls, attachments,
approvals, replay, and fencing.

The richer API is published only when its fail-closed qualification boundary is
satisfied. A client using basic chat routes must not be described as having the
full Persistent Session API contract.

The detailed state and qualification contracts are defined in:

- [HASHI Persistent Multi-Session Frontend Design](HASHI_PERSISTENT_MULTI_SESSION_FRONTEND_DESIGN.md)
- [Multi-Session Frontend Insertion Plan](MULTI_SESSION_FRONTEND_INSERTION_PLAN.md)

## 7. Retired Workbench boundary

Workbench is retired. A successor frontend is maintained separately and is not
part of HASHI.

Some compatibility identifiers remain:

- `orchestrator/workbench_api.py` implements the Backend API;
- `global.workbench_port` stores the Backend API port;
- established token/header names may retain `workbench`; and
- `/workbench/v1/*` may remain a compatibility route family in Hashi Remote.

These are implementation identifiers, not an active product boundary. New
documentation and user-facing text must say **Backend API** unless it is
explaining an exact compatibility name. Private external product names must not
appear in general HASHI architecture.

## 8. Connector neutrality

Shared contracts must be transport-neutral. A Connector may implement
transport-specific formatting, message limits, notification behaviour, or
interaction controls, but the underlying product state stays typed and owned by
PAO, PCM, or the selected Engine.

In particular:

- Telegram classes must not become the canonical representation of a generic
  Event or delivery action;
- WhatsApp limitations must not weaken another Connector's capabilities;
- TUI rendering choices must not become Session policy; and
- a Backend API response must not expose internal implementation state as a
  public contract accidentally.

## 9. Engineering-layer placement

Connector business behaviour belongs in the Functions layer. Stable process
handles may be retained by Core only when required for hot replacement.
Platform Configuration handles terminal, Windows/WSL, macOS, browser, or
sidecar adaptation. Instance Configuration supplies local ports, bind hosts,
tokens, enabled connectors, and peer identity.

No Connector may hard-code machine-specific paths or private client settings
into HASHI Functions or Core.

## 10. Current alignment debt

- The TUI has trusted multi-instance switching but still uses basic Backend API
  chat/transcript routes.
- Some orchestrator modules directly depend on Telegram types rather than a
  transport-neutral event interface.
- Retired Workbench compatibility names remain in source and configuration.
- Persistent Session API v1 remains behind its qualification gate.

These are current implementation facts, not target architecture exceptions.

## 11. Future-development rules

New Connector work must:

1. keep HASHI state authoritative and frontend caches disposable;
2. use client-neutral, versioned protocols and typed Events;
3. separate user-interface presentation from HASHI execution policy;
4. enforce identity, permission, fencing, and idempotency at every mutation;
5. label basic API support separately from full Persistent Session API support;
6. preserve the built-in TUI as a HASHI component;
7. avoid private external product names and client-specific runtime branches;
   and
8. preserve compatibility identifiers only where migration requires them.

