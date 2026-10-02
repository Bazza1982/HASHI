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
Committed-source retest and runtime adoption evidence are pending.

HASHI3 was already offline when operational checks began: configured Backend
API port 18804 refused connections and saved Core PID no longer existed;
Remote remained running. The registered exact-instance runtime task points to
this checkout's bridge controller with start/resume. Startup is distinct from
hot reboot and must be reported accurately.
