# HChat Delivery Boundary Plan

Status: planned after the 2026-05-07 `bridge:hchat` wrapper-bypass quick fix.

Owner: HASHI1 runtime.

Related docs:

- `docs/WRAPPER_AGENT_MODE_PLAN.md`
- `docs/ROADMAP.md`
- `README.md`

## 1. Problem

The current `/hchat` implementation lets the core model perform a delivery side effect.
The core prompt instructs the model to compose a message and run `tools/hchat_send.py`.

That shape works for delivery, but it creates a bad boundary:

```text
user /hchat
 -> core model composes message
 -> core model invokes hchat_send.py
 -> message is already sent
 -> wrapper post-processing sees the task context afterward
 -> wrapper may mistake command text for work that still needs approval
```

The 2026-05-07 incident with zhaojun demonstrated this failure mode. The HChat
message was delivered successfully, but the wrapper replaced the correct visible
"sent" report with a false approval prompt because it saw `hchat_send.py` in the
task context.

Quick fix:

- `bridge:hchat` is wrapper-bypassed.
- `hchat-reply:*` can remain wrappable because it is a user-facing summary of an
  incoming reply, not an active send task.

Long-term fix:

- Move side effects out of the core model and into the runtime delivery boundary.

## 2. Design Principles

Keep responsibilities separated:

- Core model: decide message content.
- Wrapper model: rewrite explicit visible text only.
- Runtime: validate target, select route, send, retry, and audit.
- Tools/scripts: provide reusable transport primitives, not prompt-level control flow.

Keep HASHI maintainable:

- Minimal runtime core: put reusable HChat delivery helpers in a small module.
- Hot-reboot friendly: avoid moving this into `main.py`; keep changes under
  orchestrator/runtime modules and tools.
- Modular transport: local Workbench/API delivery and remote delivery should be
  swappable under one helper.
- Backward compatibility: preserve existing `/hchat` user behavior while the new
  pipeline is phased in.
- Comprehensive logging: every draft, final payload, route, and delivery status
  should be auditable without relying on wrapper-visible prose.

## 3. Target Pipeline

Target runtime flow:

```text
user /hchat <target> <intent>
 -> core model returns a draft only
 -> runtime parses draft
 -> runtime validates target and message
 -> optional wrapper pass polishes allowed visible text
 -> runtime calls send_hchat()
 -> runtime records delivery audit
 -> runtime returns a user-facing delivery report
```

The core model must not run `hchat_send.py` directly in the final design.

## 4. Data Contract

Prefer a structured draft shape over free-form command text.

Candidate JSON shape:

```json
{
  "target": "ying",
  "message": "Message text to send to the peer agent.",
  "user_report": "Short report for the user after successful delivery."
}
```

Rules:

- `target` must be parsed and validated by runtime.
- `message` is the only peer-agent payload candidate.
- `user_report` is optional; runtime can generate a report if absent.
- Runtime rejects malformed drafts instead of guessing.
- Runtime must never execute shell commands found in model output.
- Runtime should reject command-shaped drafts before delivery. The final peer
  message is data passed to `send_hchat()`, not shell text to execute.

Draft parser output should be logged separately from raw model output:

```json
{
  "hchat_draft_parsed": {
    "target": "ying",
    "message": "Message text to send to the peer agent.",
    "user_report": "Short report for the user after successful delivery."
  }
}
```

Malformed draft user-facing error format:

```text
[hchat] Draft parse error: missing required field "target". Message not sent.
```

This error must be the only user-visible output for the failed send attempt. It
must not use phrasing such as "tried to send" or "sent" that could be mistaken for
a delivery confirmation.

## 5. Wrapper Policy

Current safe rule:

- `bridge:hchat`: bypass wrapper.
- `hchat-reply:*`: may be wrapped.
- raw bridge/protocol envelopes: bypass wrapper.

Future policy should use explicit delivery metadata instead of source-only routing:

```text
should_wrap =
  agent_mode == wrapper
  and kind == final_plain_text
  and audience in {human_user, peer_agent_draft}
  and not control_payload
```

Initial rollout should wrap only:

- final user-facing delivery reports,
- incoming hchat reply summaries shown to the user.

Peer-agent draft wrapping should stay disabled until tests prove the wrapper cannot
change target metadata, protocol metadata, or delivery instructions.

## 6. Phased Implementation

### Phase A: evidence and contract

Goal: freeze current behavior before changing delivery.

Tasks:

- Add regression coverage proving `bridge:hchat` bypasses wrapper.
- Capture current `/hchat` local send behavior.
- Capture current remote `agent@INSTANCE` behavior.
- Define the structured draft schema and malformed-draft error behavior.
- Add transcript fixture examples for successful send, failed send, and invalid target.

Acceptance:

- Tests prove active `/hchat` sends are not wrapper-polished.
- Existing `/hchat` still works before the new pipeline is enabled.

### Phase B: runtime sender helper

Goal: create a runtime-owned delivery boundary without changing prompts yet.

Tasks:

- Add a helper module, for example `orchestrator/hchat_delivery.py`.
- Helper responsibilities:
  - validate target format only,
  - reject empty message,
  - call existing `tools.hchat_send.send_hchat()`,
  - return structured delivery status.
- Do not duplicate target resolution or local/remote routing logic in the helper.
  `send_hchat()` remains the single address-resolution path for local Workbench,
  contact cache, remote instance discovery, and relay behavior.
- Keep `tools/hchat_send.py` as the transport primitive.
- Add logging around target, route, delivery method, success/failure, and latency.

Acceptance:

- Helper tests cover local delivery, remote delivery, invalid target, send failure,
  and no duplicate send.
- Existing `/hchat` behavior remains unchanged.
- Retry logs distinguish one logical delivery attempt from duplicate sends.

### Phase C: draft-only prompt behind a compatibility flag

Goal: test the new prompt without forcing all agents onto it.

Tasks:

- Add a config/feature flag such as `hchat_draft_delivery`.
- When enabled, `/hchat` asks the core for a structured draft, not a shell command.
- Runtime parses and validates the draft.
- Runtime sends via the Phase B helper.
- Runtime emits a deterministic user report.

Acceptance:

- Flag off: legacy behavior works.
- Flag on: `/hchat ying ...` sends exactly once through runtime.
- Malformed draft does not call delivery.
- Delivery failure is reported clearly.
- `hchat_draft_raw`, `hchat_draft_parsed`, and `hchat_payload_final` are logged
  distinctly before Phase C goes live.

### Phase D: wrapper integration

Goal: let wrapper polish safe text only.

Tasks:

- Add explicit output metadata fields:
  - `audience`,
  - `kind`,
  - `control_payload`,
  - `delivery_side_effect_done`.
- Apply wrapper only after runtime has isolated plain text.
- Start by wrapping only the user-facing report.
- Consider peer-agent draft wrapping only after metadata mutation tests pass.

Acceptance:

- Wrapper failure uses this fallback priority:
  1. runtime delivery report if already computed,
  2. core draft `user_report` if present,
  3. deterministic fallback string: `Message delivered to <target>.`,
  4. never silence or a blank turn.
- Wrapper cannot change target.
- Wrapper cannot trigger or suppress delivery.
- `/verbose on` shows core draft, wrapper result, and actual delivery result separately.
- Wrapper failures should expose `fallback_reason="wrapper_error:<exception_type>"`
  where practical so `/verbose on` explains why fallback happened.

### Phase E: remove legacy core-command send

Goal: make runtime-owned delivery the only `/hchat` implementation.

Tasks:

- Remove prompt text instructing the core to run `hchat_send.py`.
- Remove compatibility flag after stable validation.
- Update README, roadmap, and wrapper docs.
- Keep `tools/hchat_send.py` for direct CLI/admin use.

Acceptance:

- No `/hchat` prompt asks a model to execute shell delivery commands.
- All active sends go through runtime delivery helper.
- Direct CLI `tools/hchat_send.py` still works for operator/debug use.

## 7. Logging And Audit

Add structured fields where practical:

- `hchat_target`
- `hchat_route`
- `hchat_delivery_method`
- `hchat_draft_raw`
- `hchat_draft_parsed`
- `hchat_payload_final`
- `hchat_delivery_attempt_id`
- `hchat_delivery_status`
- `hchat_delivery_error`
- `hchat_delivery_latency_ms`
- `hchat_retry_count`
- `wrapper_used_for_report`
- `wrapper_used_for_peer_payload`

Audit rules:

- Log raw draft and final payload separately.
- Log parsed draft immediately after parsing succeeds and before wrapper runs.
- Log whether delivery happened.
- Log exactly once per logical delivery attempt. Retries share the same
  `hchat_delivery_attempt_id` and increment `hchat_retry_count`.
- Do not rely on final user-facing prose as the only evidence.
- HChat delivery events are audited by these structured delivery fields. The
  source-level audit wrapper may continue to bypass `bridge:hchat` for raw core
  conversation turns until Phase E explicitly re-evaluates whether it adds value.

## 8. Compatibility And Rollback

Compatibility:

- Preserve `/hchat <agent> <intent>` user syntax.
- Preserve `agent@INSTANCE` addressing.
- Preserve existing remote/local route resolution.
- Preserve direct `tools/hchat_send.py` CLI use.

Rollback:

- Phase C should be feature-flagged.
- If draft parsing or runtime send fails in live validation, turn the flag off and
  fall back to legacy behavior.
- Do not remove the legacy prompt until Phase E.

## 9. Test Matrix

Required tests:

- local target sends once,
- remote target sends once,
- invalid target does not send,
- malformed draft does not send,
- transport failure reports failure,
- wrapper success changes only visible report,
- wrapper failure falls back safely,
- wrapper cannot change target,
- wrapper given peer-agent draft cannot modify the `target` field,
- wrapper given peer-agent draft cannot modify the message protocol envelope,
- `bridge:hchat` bypasses wrapper in legacy mode,
- `hchat-reply:*` summaries remain wrappable,
- transcript/core transcript/listener payloads agree on sent vs shown text.

## 10. Live Validation Checklist

Phase C gate, with `hchat_draft_delivery` flag on:

```text
[ ] /hchat local agent with simple text
[ ] /hchat local agent with markdown/code/path text
[ ] /hchat agent@remote-instance
[ ] invalid target
[ ] malformed draft (missing "target" field): message NOT sent, user sees parse error
[ ] remote offline target
[ ] wrapper mode on, wrapper succeeds
[ ] wrapper mode on, wrapper backend disabled/fails
[ ] duplicate delivery: send once, receiving agent sees exactly one message even if runtime retries
[ ] /verbose on confirms three-way separation: core draft != wrapper output != delivery payload
[ ] receiving agent sees exactly one message
[ ] sender receives accurate delivery report
```

Phase E gate, before removing legacy behavior:

```text
[ ] no `/hchat` prompt references `hchat_send.py` directly
[ ] all active sends go through runtime delivery helper
[ ] direct CLI `tools/hchat_send.py` still works for operator/debug use
[ ] audit/log output includes attempt id, parsed draft, final payload, status, and retry count
[ ] legacy compatibility flag can be removed without changing user syntax
```
