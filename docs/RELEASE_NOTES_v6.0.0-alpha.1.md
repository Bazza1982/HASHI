# HASHI v6.0.0-alpha.1 startup recovery candidate

Prepared 2026-11-15. Python metadata: `6.0.0a1`. This is an unpublished,
non-production candidate.

## Why the Core major changes

The v5 installation identity contract put complete runtime and generation
verification on routine startup paths. Harmless Python cache creation could
therefore reject both Core and Functions, while 24 Workers repeated expensive
directory hashing. Cold restart could also wait for readiness after its fixed
launcher had already rejected the request. Those are Core execution-contract
changes, so the recovery is delivered as a governed major migration.

## Recovery boundary

- Full runtime and product-byte verification remains mandatory for a new
  installation, release qualification, adoption and explicit integrity audit.
- Routine Core, Functions and Worker admission checks the selected sealed
  identity, interpreter/ABI/protocol, critical entry files, actual process,
  parent/PID/nonce and requested generation without recursively rescanning the
  complete installation or rehashing the unchanged protected-source set.
- Additional PEP 3147 bytecode in `__pycache__` is tolerated only when a sealed
  source file backs it. Sourceless bytecode, `.pth` files, startup hooks,
  executables, DLLs and unrelated import roots remain rejected.
- A generation receives one full byte qualification before adoption. Worker
  admission reuses that qualified identity and private process contract.
- A bound Functions Worker reuses its parent admission when preparing an MCP
  invocation; each newly created MCP process still performs fast admission,
  but neither side repeats the complete Functions installation scan.
- Cold restart validates its fixed tasks before deep qualification, waits for
  the launched Core to acquire the instance lock and publish its PID, then uses
  the Remote transaction's single bounded readiness deadline.
- Permanent Telegram credential rejection stops only that connector until its
  configuration fingerprint changes. Connector activation is no longer part
  of topology commit or Core/Agent readiness.

`-B` prevents HASHI-managed Python processes from writing ordinary bytecode;
it does not prevent Python from reading existing bytecode. The narrow cache
exception therefore does not classify arbitrary bytecode as trusted.

## Residual risk and publication status

The qualified generation directory and protected-source identity are reused,
not fully rehashed for every Worker. A local writer able to alter qualified
product or Core source bytes between adoption and execution remains outside
this simplification; no new ACL daemon, background scanner or packaging layer
is introduced. Installation audits and new-generation adoption retain full
hashing.

This candidate does not adopt itself into production, publish an installer,
or modify another HASHI instance. Operational timing and 24-Agent acceptance
must be recorded before any publication decision.
