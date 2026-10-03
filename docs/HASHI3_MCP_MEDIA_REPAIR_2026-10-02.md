# HASHI3 MCP and Telegram media repair

User authorized repair and HASHI3 reboot on 2026-10-02. PAO owns Codex MCP
isolation; Frontend Connectors owns trusted Telegram media admission. All
implementation changes are Functions; protected Core and HASHI4 are unchanged.

MCP inventory now disables plugins before enumerating standalone servers, as
both consumers already disable plugins. This avoids top-level enabled-only
overrides for transports removed with their plugins while retaining standalone
MCP isolation. Trusted Telegram upload, native-audio fallback, transcription,
sticker, and `/long` submission explicitly carry ingress transport. The global
connector registry remains strict for unknown sources.

Regression evidence: before implementation, both plugin-inventory scenarios
and document admission failed (3 failures). After implementation, focused Codex
CLI/app-server bridge and media/long tests passed: 77 passed, 2 platform skips.
Real local Codex CLI 0.156.1 inventory through the updated adapter returned only
the standalone node_repl server; applying its disabled override with plugins
disabled parsed successfully, with node_repl disabled.

The precommit curated gate reported 728 passed, 1 skip and 5 failures: three
generation/product-probe cases require committed source; two connector-health
cases require further investigation. Protected Core and whitespace checks pass.
After commit, all three generation/product-probe failures passed. The two
remaining health-test failures are in the unchanged fixture: its `get_updates`
never returns, whereas the earlier 9b1e23f4 change marks Telegram connected only
after a successful poll. These checks were run and remain failed; the curated
gate is not reported as entirely green. Production health is verified below.

HASHI3 was already offline when operational checks began: configured Backend
API port 18804 refused connections and saved Core PID no longer existed;
Remote remained running. The registered exact-instance runtime task points to
this checkout's bridge controller with start/resume. Startup is distinct from
hot reboot and must be reported accurately.

## Required Fixed Codex gateway follow-up (2026-10-03)

The request-scoped `hashi_tools` MCP server is a required part of Fixed Codex,
not an optional enhancement. Both new and resumed `codex exec` invocations set
`mcp_servers.hashi_tools.required=true`. Codex CLI therefore rejects the
session before a model Turn when this enabled server cannot initialize, as
defined by the official OpenAI configuration reference:
<https://learn.chatgpt.com/docs/config-file/config-reference>.

HASHI also fails before MCP inventory or subprocess launch when Fixed Codex is
enabled but its request-local gateway descriptor is absent. It returns the
non-retryable `CODEX_TOOL_GATEWAY_UNAVAILABLE` adapter result with no possible
side effects; it does not silently run a model without the advertised HASHI
tools and does not automatically replay the request.

## Runtime verification

The exact-instance runtime task recovered HASHI3. Its adopted generation is
`sha256:d34366142bf0874de4cb1dd062adedef29c624c9a359f07fb4211c809e543af1`,
with manifest source commit `ea63691753fe059d18f8a51cce531dc8d7699af5`.
Nine Workers loaded this generation. A real Telegram request
`req-agent1-2026-10-02_122651-0001` finished successfully via Codex in 11.08s
and completed Telegram delivery at 12:27:17, without the MCP transport error.

An existing `/reboot max` request was processed after recovery; receipt
`6081be8e1d7f4036a67842039db5532d` finished `succeeded`. A separate attempted
admin Agent reboot was rejected as busy during this broad operation and was
not retried. Core stayed PID 3452; shared Functions changed 10628 -> 30100;
nine Workers remained alive on the repaired generation. Final health reports
ready, no degradation/issues, and agent1 Telegram ingress running/connected.

The actual adopted artifact's photo and PDF handlers passed download-to-admission
checks with the real connector admission function and a simulated Telegram
download. A fresh physical Telegram attachment upload is not verified by this
check. Protected Core and HASHI4 remain untouched. No push or deployment to
other instances was performed.
