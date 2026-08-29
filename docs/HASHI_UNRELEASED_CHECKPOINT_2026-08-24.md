# HASHI Unreleased Integration Checkpoint — 2026-08-24

> **Historical record:** this checkpoint is retained as evidence of the
> 24 August integration boundary. It is superseded for current operation and
> publication status by the
> [27 August checkpoint](HASHI_UNRELEASED_CHECKPOINT_2026-08-27.md). Statements
> below about an unpushed corrective tree or branch divergence describe that
> earlier moment and must not be read as current repository status.

Status: **the original integration baseline was published at `2e3a3f0`; the
current working tree contains a corrective compulsory-Replanning update that
has not been rebooted, deployed, tagged, or pushed**

This checkpoint records the broader HASHI v4 platform line. It does not change
the independent HASHI AAI Enterprise package line at
`v0.1.0-alpha.1` / `0.1.0a1`.

## Integration identity

| Field | Value |
| --- | --- |
| Approved GitHub destination | `https://github.com/Bazza1982/HASHI.git` |
| Publication branch | `main` |
| Fetched remote base | `112d0093c56156acf710f10aeb70630abdb1ceb1` |
| Integrated implementation baseline | `cc010d11d69b4eb24c62c134dc57ac62ea42c277` |
| Divergence before this documentation commit | `0` behind, `79` ahead of `origin/main` |
| Expected divergence after this documentation commit | `0` behind, `80` ahead of `origin/main` |
| Push shape | ordinary fast-forward; no force push or history rewrite |

The identity table records the already-published integration event. It is not a
claim that later corrective working-tree changes have been published.

The feature histories remain represented by their original commits and merge
records. The consolidation did not resurrect source or assets explicitly
retired by the HER v2 and repository-slimming architecture.

## Integrated scope

### HASHI Engine Runtime v2

- HER v2 is the sole supported HER backend. `her` resolves forward to
  `her-v2`; `claw-cli` is rejected, and `/rebuild` is a side-effect-free
  retirement notice.
- Direct, Fast path, Planned, Adaptive, Reviewed, and Assured use the canonical
  `zero` through `max` wire values while separating orchestration depth from
  provider reasoning. Direct is one fully capable Quick-model call at default
  provider reasoning `high`, never upgrades, and invokes no other HER stage.
- Reviewed uses independent read-only inspection and one closure Review after
  remediation. Assured adds validation-only Verification in the authoritative
  workspace with exact current-invocation receipts and at most three attempts.
- Adaptive (`high`), Reviewed (`xhigh`), and Assured (`max`) Execution now
  unconditionally enters Replanning at the next safe boundary after 10
  completed results or 300 monotonic seconds. Each Replan assesses completion
  and plan suitability, activates a plan version, and sends one Persona-rendered
  or deterministic fallback commentary. Triage's immutable `STANDARD` or
  `HIGH_RISK` field remains risk metadata and cannot gate the cadence.
- Legacy execution ceilings were removed. Immediate policy and approval
  decisions, explicit user control, meaningful-progress idle detection, scoped
  transport guards, explicitly requested single-tool timeouts, and bounded
  policy stages remain the authorised interruption boundaries.
- Automatic context maintenance follows the initiating Agent's active
  Quick/Light route. Compaction failure or insufficient reduction warns the
  user and continues the selected-model request.

### Providers, tools, and media

- Quick and Pro model slots support Single or Hybrid stage routing. The HASHI
  API provider can route HER through another HASHI OpenAI-compatible Gateway
  without discarding reasoning, usage, media, tool, or typed failure metadata.
- The OpenAI-compatible Gateway bridges caller-owned function tools to isolated
  Codex app-server dynamic tools, including structured history, named/automatic
  tool choice, parallel calls, streaming responses, and caller-visible name
  restoration.
- One provider-neutral multimodal contract now spans intake, HER stages, the
  Gateway, Codex, OpenRouter, and HASHI API. Supported media is sent natively;
  unsupported media uses only an authorised local fallback and otherwise fails
  explicitly.
- On-demand local image inspection is available through a bounded audited skill
  and tool path.

### Runtime and repository operations

- Targeted `/reboot min` and numbered adoption can load valid public interface
  changes without widening to unrelated Agents. Invalid targets and failed
  preflight close before lifecycle side effects.
- API Gateway shutdown drains admission and protects HASHI's own process group;
  foreground shell process groups and remote supervisor identities now have
  explicit lifecycle cleanup.
- The Backend API exposes a canonical read-only Agent Overview, and signed
  shared-token requests may use the remote terminal endpoint alongside pairing
  bearer authentication.
- Python dependencies are split into core, standard, feature, development, and
  complete profiles. Large optional EXP binary assets moved to a checksummed
  on-demand pack while text guidebooks remain in Git.

## Verification evidence

The compulsory-Replanning corrective working tree completed these local gates:

| Gate | Result |
| --- | --- |
| Complete offline product suite | `2685 passed`, `2 skipped`, `40 deselected` after integration with the retained Auto Compact commits |
| Core deterministic gate | `237 passed` |
| All HER v2 test modules | `346 passed`, `1 skipped` |
| Combined Auto Compact and HER v2 focused suite | `403 passed`, `1 skipped` |
| Focused Ruff correctness rules | passed |
| Python compilation | passed |
| Git diff/whitespace check | passed |
| Internal Markdown targets | `207` checked; `0` missing |
| Runtime code adoption | authorised Arale `/reboot min` passed; target set was exactly `('arale',)` and the current HER runtime contract was verified |
| Runtime health after adoption | Arale online on HER v2; all 20 configured Agents online; Backend API and API Gateway recreated with reloaded code |
| Capability-level canary | not performed; real threshold Replan, Auto Compact, and provider multi-image canaries remain separate |

The already-published integrated implementation baseline had completed these
historical gates before its documentation pass:

| Gate | Result |
| --- | --- |
| Offline product suite | `2663 passed`, `2 skipped`, `40 deselected` |
| Core publication gate | `232 passed` |
| Focused Ruff correctness rules | passed |
| Python compilation | passed |
| Git diff/whitespace check | passed |
| `/reboot min` adoption | passed; implementation baseline loaded |
| `/reboot max` adoption | passed; implementation baseline retained |
| Runtime health after adoption | configured Agents online; Backend API, API Gateway, Telegram, and scheduler healthy |
| Historical HER v2 canary | passed on `main` at `cc010d1`; it predates the compulsory-Replanning correction and does not certify its live cadence |

The documentation and publication-hygiene pass completed these additional
gates:

| Gate | Result |
| --- | --- |
| Focused runtime-contract regression | `301 passed` |
| Publication-boundary contract smoke | `25 passed` |
| Core publication gate rerun | `232 passed` |
| Outbound Markdown links | `156` internal targets checked; `0` missing |
| Protected-core manifest | passed |
| Focused Ruff correctness rules | passed |
| Python compilation | passed |
| Added binary paths | `0` |
| Largest newly reachable implementation blob | `371,552` bytes; below GitHub's 100 MiB single-file limit |
| Final whitespace/diff check | passed |

The exact outbound additions also received high-signal credential, credential
assignment, URL/host, private path, account identifier, and risky-file-name
scans. Three high-signal matches were reviewed: two occurrences of the existing
HASHI private-gateway default already present in `origin/main`, and one
deliberately synthetic API-key-shaped redaction fixture. Seven literal
credential assignments were test/example placeholders. No live credential,
personal home path, chat/account identifier, credential-bearing URL, local
runtime configuration, workspace transcript, operator log, or generated binary
was added to the publication range.

## Known limits and unclaimed evidence

- The deterministic guard preventing an Immediate Response from claiming
  unperformed action remains a separate open design item. Prompt instruction
  alone is not treated as proof that this case is impossible.
- Arale has loaded the corrective source through an authorised targeted
  `/reboot min`; no other Agent was selected for restart. A real
  Adaptive-or-above 10-result/300-second compulsory Replan, a
  threshold-triggered Auto Compact cycle, and provider-specific multi-image
  canaries remain separate capability-level rollout evidence.
- A broad full-repository Ruff sweep still reports `1696` pre-existing style
  findings. The focused correctness rules used by this integration passed; the
  style backlog is not represented as newly clean.
- This checkpoint does not certify production enterprise deployment,
  high-availability infrastructure, every optional provider, or unattended
  Superloop operation.

## GitHub publication boundary

The reviewed integration range was published as an ordinary fast-forward ending
at `2e3a3f0`; no force push, tag, or Release was used. The credential, private
state, binary, size, link, and unintended-file checks above apply to that
historical publication range.

The later compulsory-Replanning correction is outside that reviewed publication
range. It is committed on local `main` as `7f2c1ac`, directly after the retained
Auto Compact commits `3950cc0` and `81f5d76`, and its targeted Arale code
adoption is recorded above. These local commits have not been pushed; wider
rollout and capability-level live canaries still require separate authority.
