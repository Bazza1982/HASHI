# MCP/media repair rollout

On 2026-10-02 the user authorized applying HASHI3 repair ea636917 to HASHI4
and HASHI1, rebooting both and validating them. HASHI2 is excluded. This aligns
the repair, not unrelated branch features or instance configuration.

PAO Functions owns Codex MCP isolation: enumerate standalone servers with
plugins disabled, matching CLI and app-server execution. Frontend Connector
Functions owns explicit trusted Telegram ingress identity for media and /long.
The unknown-source registry remains strict. Protected Core is unchanged.

The exact same repair patch applied to both checkouts without conflicts.
Focused checks: HASHI1 native WSL environment 82 passed; HASHI4 76 passed and
2 platform skips using HASHI3's test environment because HASHI4 has no pytest.
HASHI4's runtime dependencies were not installed or upgraded.

Existing unrelated work is temporarily preserved in scoped Git stashes during
committed-source qualification and restored after adoption. This repair does
not commit that work. Live adoption, receipts, health and test limits are
reported separately in the task's final verification record.
