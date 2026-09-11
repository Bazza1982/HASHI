# Nightly repair: current-message source and immutable reply route

Date: 2026-09-12

Branch: `repair/nightly-batch-20260911`

Checklist: `HN-20260911-003`, with the source/authorization contract from
`HN-20260910-002` retained.

## Result

PAO now resolves the Session first, freezes one private
`hashi.run-delivery-route` v1 value, persists it on the Run, and projects only
its safe destination facts into `hashi.current-message-context`. The same route
drives the immutable queued Telegram flag and connector callbacks. A binding
change or an inconsistent PCM projection fails before `accept_run` publishes a
new Run.

The PCM `output_destination` now contains:

- `surface`: ordinary reply's primary surface;
- `mirrors`: optional surface names only, never channel identifiers;
- `automatic`: whether normal reply handling owns delivery; and
- compatibility-only `telegram_mirror`, which remains a plan rather than a
  success claim.

Scheduler, heartbeat, proactive and background-job events are
`hashi.internal` with `sender.kind=system`, even when their planned destination
is Telegram. HChat is `sender.kind=agent` independently of optional private
authorization. A terminal HChat reply routes to the user-facing Telegram leg
without advertising or starting an acknowledgement loop.

Telegram, WhatsApp, and HChat record route-scoped outcomes separately.
WhatsApp records the real send return and fails closed on a frozen-route
mismatch. HChat API/protocol acknowledgements are stored as `queued`, not
misrepresented as `delivered`; receiving Run evidence or a later terminal
exchange is required to prove the next hop. A failed Telegram mirror no longer
prevents an HChat primary route from running.

The `telegram_send` tool description now states that it is for an explicitly
requested additional notification. When the current reply already has an
automatic Telegram destination, the Agent should answer normally and avoid a
duplicate send.

## Focused verification

- 316 owner/direct-consumer tests passed across current-message context,
  Session/Run persistence, frontend delivery policy, background jobs,
  Workbench Session API, Remote TUI proxy, runtime delivery, HChat routing,
  WhatsApp, and route-scoped receipts.
- Bytecode compilation and `git diff --check` passed.
- `python scripts/check_protected_core_changes.py --base
  19e330f985aaf6d5523fd82a3bcc8a4256b536c0` passed.

No Core file, instance configuration, credential, or user data was changed.
Native Windows qualification and modest marked Telegram/HChat runtime
acceptance remain deployment steps; WhatsApp stays simulation-only while its
account is logged out.
