# AgentPresence input feedback

Owner: Frontend Connectors. Engineering layer: per-Agent Functions.
PAO remains the sole authority for messages, Run admission and frozen delivery routes.

After a new human/client message is accepted by the canonical runtime enqueue
path, the Worker schedules one optional AgentPresence receipt notification.
The trigger is admission, before Provider execution or reply audio. The frozen
owner route must include agentpresence. Replayed admissions, Agent/system
messages and authenticated AP-local input do not create this notification;
AP-local input already animates before its outbound delivery.

The adapter sends POST /v1/hashi/message-received with only instance_id and
request_id, using the existing local AP token and two-second bounded transport.
It retains the async task through completion, limits admission to eight pending
optional frontend tasks and never blocks or fails PAO execution for AP failure.
No body, attachment, Agent selector or Session selector crosses this endpoint.
No message state store or generic external-event permission is added.

HAP 1.9.1 validates its configured instance, resolves the current phase's
hashi.message_received conditional (falling back to device.text_submitted),
and gives fresh input immediate priority over ambient playback. Active input
feedback coalesces; calls, holds and reply speech keep presentation ownership.
Paused, hidden, transitioning or unavailable presentation is skipped without
a late queue. HAP caches 512 scoped request receipts in memory. Persistent
idempotency belongs to PAO, not to this presentation cache.

Implementation owners are frontend_agent_presence.py (admission policy/task
lifetime), agent_presence_notify.py (HTTP transport) and one enqueue hook in
flexible_agent_runtime.py. This hook runs in the selected Agent Worker.
Adopt through that Agent's hot /reboot min after committed-source qualification;
never replace Core or unrelated Workers for this feature. HAP deployment, source
qualification, Worker adoption and actual frontend delivery are separate facts.

Focused evidence lives in test_audit_run_handoff.py and test_agent_presence_notify.py.
The real enqueue scenario checks one pre-execution notification, replay,
owner opt-out, AP-local echo prevention and optional-transport failure.
test_agentpresence_ingress.py protects existing AP authentication/routing.
The pre-fix scenario produced no notification and failed; the fixed scenario
passes. Native HAP tests additionally exercise complete playback, request
deduplication, scoped rejection, ambient preemption and speech ownership.

See [runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md) and
[Frontend Connector architecture](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).
