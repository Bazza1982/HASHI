# Typed `/steer` — mid-task course correction

**Status:** Implemented; source, runtime adoption, and live acceptance are recorded separately
**Updated:** 2026-10-18

## Ownership

Workbench, Telegram, and Session API submit the same typed `/steer` operation.
Frontend Connector owns ingress and presentation, PAO owns Run identity and the
receipt, and the selected Engine owns its in-flight execution. HERV3 owns its
private Provider recovery context.

A frontend must not emulate steer with `/stop` followed by an ordinary message.
That loses the typed relationship and cannot produce an authoritative receipt.

## Behavior

`/steer <direction>` adds a course correction to active work while preserving
valid progress and current authority. The execution path depends on what the
selected Engine can actually do:

| State and Engine | Execution | Run relationship | Queue |
| --- | --- | --- | --- |
| Live Codex App Server Turn | Send native `turn/steer` with the active Thread and expected Turn | The same HASHI Run and Provider Turn continue in place | Clear READY for this Session; preserve FUTURE |
| HERV3 or another Engine without native in-flight steer | Mark the old Turn `STEERED`, interrupt it, and enqueue a linked successor | PAO marks the old Run `superseded`; the new Run records it as parent plus continuation evidence | Clear READY for this Session; preserve FUTURE |
| Idle | Enqueue the direction as an ordinary request | A normal new Run | Existing queue rules |

Native steer is used only when the Codex adapter can prove that a real App Server
Turn is still active. An absent, stale, rejected, or timed-out native operation
falls back to the durable successor path.

## Codex App Server mapping

The adapter sends the documented App Server operation:

```json
{
  "method": "turn/steer",
  "params": {
    "threadId": "<active-thread>",
    "input": [{"type": "text", "text": "<direction>"}],
    "expectedTurnId": "<active-turn>"
  }
}
```

The response must acknowledge that exact Turn ID. HASHI does not report native
success from a write, click, interrupt, or newly started Turn.

OpenAI reference:
[Codex App Server](https://developers.openai.com/codex/app-server.md).

## HERV3 continuation

HERV3 API Providers do not expose Codex App Server's native operation. Their
steer path therefore creates a successor Run and preserves continuity at safe,
auditable boundaries:

- complete Provider messages are checkpointed privately after a complete
  Provider response or a complete Tool batch;
- before accepting the successor Turn, HERV3 rebinds only a durable steer
  receipt that resolves to the same open HERV3 Session and matches the full
  HASHI instance, Agent, owner, conversation, generation, and Workzone binding;
- exposed Provider reasoning fields and completed Tool results remain part of
  that private context;
- replay requires the same Provider, model, and HERV3 stage;
- partial streamed or uncommitted reasoning is never treated as replayable;
- PCM, visible messages, and steer receipts contain only boundary metadata,
  digests, and evidence references, never raw private reasoning.

The successor still receives a fresh Triage decision. Earlier plans and goals do
not remain authoritative merely because their Provider context was retained.

## Receipt contract

Every accepted busy steer returns a typed `hashi.steer-receipt` with:

- `old_run`: the active request and Run;
- `new_run`: either the parent-linked successor or the same Run with
  `relationship=continued_in_place` and `created=false`;
- `continuation`: the Provider Turn acknowledgement or HERV3 recovery evidence.

For a reconstructed steer, `new_run` and `continuation` both carry the old and
new Run relationship. The command wrapper must return this value unchanged so
Workbench and Session API receive the same receipt that Telegram renders. The
successor keeps the same HASHI Session. Its command surface remains the primary
Connector destination. An authenticated Telegram destination inherited from
the parent remains a mirror for a Workbench command and remains primary only
when the command itself came from Telegram.

A native receipt must not invent a new Run. A reconstruction receipt must not
claim success until the successor request and its Run identity exist.

## Fallback prompt

When reconstruction is required, `build_steer_prompt()` supplies the new
direction, the bounded original task, and explicit continuity rules. It keeps
workspace artefacts, completed Tool results, and valid progress, but it grants
no new authority and does not revive a cancelled action.

Intentional backend termination is marked before shutdown so its process exit is
not presented as an unexpected Backend error.

## User-visible expectations

A native steer acknowledgement states that the same Run continued in place and
shows the Old Run, New Run, and continuation evidence reference. A reconstructed
steer acknowledgement shows the distinct old and new Run identities and the
evidence linking them.

An empty `/steer` shows usage. When idle, the direction is delivered unchanged
without a mid-task wrapper.

## Implementation map

| Piece | Location |
| --- | --- |
| Shared command behavior and typed receipt | `orchestrator/runtime_control.py` |
| Native App Server operation | `adapters/codex_app_server.py` |
| Codex backend delegation | `adapters/codex_cli.py` |
| HERV3 private checkpoint and successor handoff | `orchestrator/her_v2/session_store.py`, `adapters/her_v2.py` |
| Provider boundary capture and replay | `adapters/openrouter_api.py`, `adapters/her_v2_provider.py` |
| Frontend command binding | `orchestrator/runtime_command_binding.py` |
| Tests | `tests/test_steer_command.py`, `tests/test_codex_api_tool_bridge.py`, HERV3 Provider/control tests |

## Acceptance

1. All frontends reach the same typed operation.
2. Native Codex steer targets the exact active Turn and creates no second Run.
3. Unsupported native steer uses the linked successor path.
4. HERV3 preserves complete private Provider context and does not expose raw
   reasoning.
5. Receipts identify old Run, continuing/new Run, and continuation evidence.
6. READY is cleared only for the affected Session and delayed FUTURE work remains.
7. Runtime adoption and live frontend delivery are verified separately from
   source and offline tests.

## 2026-10-18 HASHI3 correction record

- Approval: the user authorized work on HASHI3 branch `debug-herv3`, a HASHI3
  hot reboot, and live Workbench acceptance.
- Implementation: the branch restores a verified HERV3 Session binding before
  accepting the linked successor and keeps Workbench as the primary Connector
  when a parent Telegram mirror channel is recovered.
- Offline verification: the focused HERV3, steer, message-context, and request
  visibility suites passed (166 tests). The curated Core gate passed 860 tests
  with 1 conditional skip. Runtime adoption and live Workbench acceptance are
  still separate and pending.
