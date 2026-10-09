# In-run questions: completion and adoption, 2026-10-10

## Approved scope and ownership

The user approved HASHI3 and Workbench implementation, followed by HASHI4
deployment. HASHI3 reboot is allowed; HASHI4 reboot and actual Telegram client
button testing are reserved to the user. PAO owns question state, Codex/HERV3
own Engine continuation, and Frontend Connectors own authenticated rendering
and answers. All changes are Functions/frontend; Protected Core is unchanged.

## Cause and resulting behavior

The PAO tools and Workbench answer API already existed. Fixed Codex used an
output-only exec stream, so native question RPCs had no answer return path.
Telegram had no question-specific delivery/callback route, and its generic
text delivery failed to attach the markup it could already generate.
Workbench placed questions at the top of scrollable history. HERV3 had tools
but lacked explicit main-loop guidance for waiting and collecting answers.

Native Codex now sends question batches through PAO and receives explicit
answers on the original RPC in the original Turn. HERV3 uses ask_user and
get_user_answer. Workbench cards sit above the composer. Telegram supports
option buttons and replying to the question message with free text. Canonical
scope, expiry, cancellation, idempotency and no-permission semantics remain.
App-server has no exec invocation-scoped hook trust; its local shell/hooks are
disabled, and execution remains with HASHI's managed MCP tools.

## Evidence

- Red: missing question delivery/projection/callback behavior failed focused
  scenarios. The real FC transport test also caught missing reply_markup on the
  actual send path; the repaired transport passes that scenario.
- Focused backend: 177 passed, 3 skipped. Scope: question owner/review, Telegram
  delivery and callbacks, native RPC, Codex adapter, runtime delivery, frontend
  attachments and callback registry. Existing optional skips are not passes.
- Curated Core gate: 812 passed, 1 skipped (414.41 seconds). Initial qualification
  failures were the required uncommitted-source fence; this pass used the
  committed source. Receipt: state/run-question-upgrade/core-gate.xml.
- Workbench React card and Express proxy: 4 passed; production build passed.
- Real HASHI3 Workers: separate new Sessions on the existing Codex and HERV3
  canary Agents each generated a question. Chromium rendered the actual
  RunQuestionCards component and Express question proxy in a bounded harness,
  connected to the real HASHI3 authenticated PAO API. Neither card preselected
  an answer. The harness clicked Blue and submitted both forms. Each original
  Run consumed its answer and completed with `QUESTION-ACK: Blue`.
- The browser harness exercises real components, real HTTP and real models;
  its surrounding history is synthetic. It is not an external Telegram-client
  test or a claim about every Workbench shell interaction. Screenshots, API
  receipts and model completion records are in state/run-question-upgrade.

## Delivery and adoption

Backend implementation: HASHI3 1d9c72cf; HASHI4 60571a20. Fourteen changed Python
files compare byte-for-byte. Workbench checkpoint: 6cb4025. Its deployed bundle
was built with the existing call, voice and history repairs preserved; the
served index matches the built index and browser loading has no script errors.
Previous index/source copies remain in the ignored deployment backup.

HASHI3 was already stopped before this work; its registered runtime was started
under the user's authorization. Its live Function receipt identifies 1d9c72cf,
and the real model acceptance above used that generation. No active HASHI3
Core was cold-restarted as a testing shortcut.

HASHI4 source is delivered, but its existing running generation remains active.
The user's manual `/reboot max` must adopt both the shared Functions and Agent
Workers; an Agent-only reboot does not cover the shared backend changes.
Refresh Workbench after adoption. Telegram client option clicks and free-text
replies remain the user's acceptance step. No HASHI4 reboot was issued.
