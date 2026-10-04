# PAO Agent creation selection policy

Owner: PAO. Engineering placement: shared Functions and ignored Instance
Configuration. Parent: `HASHI_PAO_SYSTEM_DESIGN.md`. This decision changes no
Protected Core, credentials, tool grant, identity or workspace permission.

## One explicit source

`global.agent_creation = {"template_agent": "<configured Agent>",
"grant_mode": "template"}` names the authority for new Agent backend/model
selection. No implicit first-Agent fallback and no union of unrelated Agents
is used by the public creation service. `grant_mode=selected` or the public
`restricted=true` intent limits the new top-level Engine grant to its initial
selection. Otherwise the explicit template authorizes every available,
selectable Engine in its own policy; the selected Engine is only the initial
active choice. Provider-only rows never become top-level Engine choices.

The template projects only engine, model/default_model, models, effort and
model_efforts, plus the HERV3 main Provider/model and Provider allowlist. Tool
permissions, access scope, permission modes, credentials, Agent state, identity
and workspace data never cross the template boundary. HERV3 concrete Provider
model/effort rows are retained only for the granted Provider set.

An absent policy preserves the explicitly identified `legacy_selected` public
baseline, granting just the selected Engine. An invalid explicit policy fails
closed with `creation_policy_invalid` before scaffold creation. The UI fallback
is never persisted as a valid template. An instance's opt-in model is not added
to the shared compatibility registry or another Agent's policy.

## Effective selection and publication

`runtime_effort_options` owns ordinary effective models and per-model efforts:
the qualified shared baseline plus the selected Agent/template opt-ins.
`agent_creation_policy` derives a public directory from that authority. CLI
installation and OAuth/key availability reuse BackendPreflight; configured
Provider credential references, disabled status, privacy and HERV3 allowlists
are applied before a new grant. Unavailable entries carry bounded reason codes
and cannot be submitted. Installation/credential availability is not a live
Provider inference result.

The existing Backend API catalogue includes `creation` with `ok`, `source`,
`template_agent`, `grant_mode`, `allowed_backends` and `backends`. Each Engine
has models, defaults, model_efforts, available/reason and creation.mode;
HERV3 has Provider-keyed models/defaults/efforts and availability. Ordinary
selection and HERV3 Provider -> model -> effort are validated again on submit.
The public intent carries `provider` and `restricted`, never arbitrary config.
The chosen model is saved as both model and default_model. Only the chosen
Engine row changes its initial selection; the remaining template policies are
preserved with unavailable grants removed.

The service reads current policy from the revisioned config document. It
passes that same document to ConfigAdmin publication so a policy edit between
validation and write is a conflict, not a stale authorization grant. Conflict
cleanup removes only this attempt's scaffold. A committed durability error is
not blindly retried or rolled back. ConfigAdmin/config_json remain the writer.

Agent runtime metadata includes `backend_catalogue={ok,backends}` derived from
that Agent's own current permission, availability, privacy and HERV3 runtime
Provider view; `active_provider` reflects current authoritative selection.
Creating a new policy does not widen or migrate any existing Agent.

## 2026-10-05 HASHI1 implementation evidence

The October 4 inbox's HN-005/006 failures were reproduced before repair: an
explicit gpt-6.1-sol model/ultra opt-in was rejected; a missing explicit template
silently created an Agent. The repaired scenario accepts and persists the
model and complete permitted policies, rejects unavailable/unauthorized choices,
keeps explicit empty effort lists and survives reread. Three additional isolated
in-memory mutations proved missing availability, stale revision and stale Session
regressions fail their boundary tests. No mutation changed checkout bytes.

Focused owner: 55 passed plus 4 subtests. Direct-consumer component scope:
294 passed plus 11 subtests on native WSL CPython. Runtime adoption and external
client evidence are separate and are recorded by the task coordinator after
adoption. The explicit HASHI1 template selection is sunny, whose current model
policy is used as-is; the gpt-6.1 opt-in above is synthetic QA evidence, not an
authorization copied from another instance.
