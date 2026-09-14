# HCC Refresh

Deterministically update one named HASHI Context Cache entry in the owning Agent's `agent.md`.

Use this skill from Scheduler/Cron refresh jobs after the new context has been fetched and prepared successfully. A failed fetch or preparation must not clear the last successful cache entry.

## Write contract

Call `scripts/hcc_update.py replace <workspace> <entry> --file <prepared-utf8-file>` only after preparation succeeds. Each replace operation preserves Persona, System, Memory, and all other HCC entries. The operation is cross-process serialized and atomically publishes the complete `agent.md` candidate.

Use `scripts/hcc_update.py inspect <workspace> <entry>` when optimistic conflict evidence is required. Pass the returned digest to `replace --expected-digest` to reject a stale update.
