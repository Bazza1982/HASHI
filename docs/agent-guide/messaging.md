# Conversations, delivery and frontend contracts

## Purpose and when to read
Read for Sessions, Runs, messages, delivery, questions, attachment publication, Telegram, HChat and Remote protocol changes.

## Ownership and layer
PAO owns canonical Conversation Sessions, Messages, Runs, routing and delivery coordination in Functions. Frontend Connectors own authenticated transport, rendering and receipts; client caches remain disposable views.

## Distinctions
A HASHI Conversation Session is not an Engine Session or Provider Context. Claimed identity, authenticated peer and current authorization differ. Acceptance, execution, partial delivery and successful receipt are separate outcomes; uncertain effects must not be replayed.

## Authoritative documents
- [PAO conversations](../HASHI_PAO_SYSTEM_DESIGN.md#22-hashi-conversation-sessions).
- [Frontend Connector architecture](../HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).
- [Remote protocol](../HASHI_REMOTE_PROTOCOL_SPEC.md).
- [Run questions](../HASHI_RUN_QUESTIONS.md).
- [Command UI and locale contract](../HASHI_COMMAND_UI_STYLE_GUIDE.md).

- [Telegram rate-limit identity and recovery boundary](../TELEGRAM_DELIVERY_FAILOVER_DESIGN.md#configuration-and-retryafter-failure-boundary-2026-11-05): canonical lifecycle loading, notification isolation, typed delivery failures and the focused lifecycle regressions.
## Source and verification
Start with `orchestrator/session_store.py` and its API consumers. Other entry points include `orchestrator/frontend_delivery.py`, `orchestrator/message_context.py`, `remote/api/server.py` and `transports/chat_router.py`. Use the owning Session/API/delivery tests; `tests/test_runtime_pipeline.py` exercises context and delivery boundaries. Prefer a real serialized protocol test over a mocked acknowledgement.

## Adoption
Match owner, Agent, canonical Session, context generation and request identity across API, rendered projection and re-entry. Frontend silence does not prove backend failure. Historical channel incidents retain their original version and verification scope.
