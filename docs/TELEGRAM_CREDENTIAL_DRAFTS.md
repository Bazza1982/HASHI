# Telegram credential drafts

An authenticated manager may explicitly save a Bot token as an unverified draft
while Telegram is unavailable. A draft is stored in the existing revisioned,
private secrets document under a reserved per-Agent record. It is isolated from
the active Bot credential and is never returned by the management API. The
existing secrets file uses OS access controls; this feature does not claim
additional encryption at rest.

Saving a draft does not contact Telegram, replace the active token, or request
a Worker activation. A status response separately reports pending_verification;
existing connection status continues to describe the currently adopted Bot,
not the draft. The draft survives a service restart. A manager may retry
verification without resending the token, bound to the current credential
revision, Agent incarnation and Agent-to-secret reference. A legacy Agent
without an incarnation identity receives the existing durable identity before
a draft is stored. Agent deletion removes that Agent's draft and preserves
other Agents' drafts. Successful verification promotes the token through the
existing optimistic publication and exact-Agent activation path. Provider
rejection or unavailability leaves the draft and current token intact. Unknown
response outcomes must be reconciled by reloading status.

The default verifier uses the Agent's validated extra.telegram_api_url origin
when configured and otherwise the normal Telegram API. It honors aiohttp's
configured environment proxy route and does not follow redirects. Custom
injected verifiers keep their existing one-argument contract.

A secrets-document revision changes when a draft is saved. An existing
activation waiting for the Agent to become idle continues when the active
credential and its Agent, secret reference and API origin are unchanged. A
changed active credential, Agent lifecycle, secret reference or API origin
invalidates that waiting adoption. Actual connection observations remain
authoritative. Cross-instance exclusive Bot-consumer ownership and inactive
backup activation remain governed by the separate transfer/activation work
(HA-45 and HA-58); this draft feature does not claim to settle those cases.
