# Agent model availability

Owner: PAO configuration and effective Agent choices, Functions layer. Frontend
Connectors derive controls; adapters observe native Engine catalogues. Core is
unchanged.

## Decision

An enabled backend grants all models in its qualified compatibility catalogue,
plus observed native choices and Agent-local opt-ins. Selecting `model` does
not restrict the Agent to that model. `models` remains additive. No frontend or
Agent needs its own copy of the default model/effort catalogue.

An explicit `available_models` array on an `allowed_backends` row restricts
that Agent to the listed known models. An empty array denies all model choices;
malformed arrays are rejected. An unregistered opt-in is declared in `models`
before it can be included in the restriction. Model commands, callbacks and
adapter construction honor the restriction. A saved/configured selection
outside the restriction blocks adapter construction; it never silently grants
the model or overwrites the user's choice.

`orchestrator.runtime_effort_options` resolves models and reasoning efforts.
Explicit `model_efforts` overrides remain authoritative, including empty
arrays. Its public `backend_model_view` is derived metadata, not configuration
to write back. Configuration publication retains the owner's read revision
through `orchestrator.config_json`.

## HERV3 creation and Provider policy

The Agent creation template snapshots model selection policy, not tool,
filesystem or standalone Engine permission. A concrete Provider such as Ollama
can also be a selectable Engine. Selected/restricted HERV3 creation preserves
that Provider's sanitized selection row in
`allowed_backends[].her_v2.provider_policies` instead of granting its Engine or
discarding its `available_models` and exact-model `model_efforts`.

These rows use the same selection fields as ordinary backend policy. They are
scoped by `v3_provider_allowlist`; invalid or out-of-allowlist policies are
rejected. Provider model menus, effort validation, saved-state restoration and
live/offline metadata derive from `runtime_effort_options`. Direct Engine
selection never expands this nested policy into an Engine grant. Reusing the
created Agent as a template retains the sanitized policy.

Shared HERV3 catalogue choices derive from
`global.her_providers.providers.<provider>`. Register verified exact-model
`model_efforts` there when the Provider has no qualified reasoning baseline.
Agent policy can narrow these choices, including an explicit empty list; a
shared declaration must never override such a denial. Unknown Ollama models
do not receive a blanket reasoning capability. An empty shared effort list can
hide an external client's control even if the creation template offers choices;
validate the shared, creation and effective Agent catalogues together.

The creation contract covers selected/restricted creation, second-generation
templates, binary effort persistence, explicit model/effort denials and offline
projection. Existing Agents with missing policy require a revision-aware
configuration repair. Passing offline tests does not establish adoption by
running Workers or an external UI.

## Codex CLI discovery

`adapters.codex_models` reads the bounded `models_cache.json` maintained by the
installed CLI, from its inherited absolute `CODEX_HOME`, otherwise its user's
default Codex home. Only `visibility: list` choices and their reasoning levels
are projected. Identity, credentials and hidden review models are not exposed.
No CLI or model request is started for discovery.

The observation augments the qualified baseline rather than removing it. File
revision changes refresh the view. Missing, unreadable, oversized or malformed
cache data falls back to the baseline and explicit opt-ins; it never removes
an explicit restriction. A relative CLI home is not guessed using another
Agent's working directory. External execution may still reject a stale choice;
the cache is discovery evidence, not proof of a completed model request.

The same resolver feeds `/model`, actual model selection, Agent metadata,
Backend API catalogue and ordinary Agent creation. Workbench consumes the
Agent's `available_models` projection, preserving explicit empty restrictions.
It does not add the current model or custom opt-ins back around a restriction.

## Approval, implementation and verification

2026-10-05: the user requested adding a missing native Codex model and made
all available models the default for an enabled backend. Implementation is
qualified on `fix/default-backend-models`; Workbench's projection change is
qualified separately on `fix/herv3-workbench-controls`. No Core migration or
operational reboot authorization is implied by that feature approval.

Focused red evidence: the real command menu and Backend API catalogue omitted
the native model before the fix. The Agent restriction test also demonstrated
that adapter construction ignored a restriction. The Workbench policy test
demonstrated that a restriction was lost while merging the catalogue.

Green evidence covers menu selection and persisted restoration, native effort
levels, explicit effort overrides, restrictions and empty denial, hidden cache
entries, cache revisions, malformed/missing cache fallback and Agent creation.
Running Worker adoption and external model calls are separate evidence; offline
validation does not establish them.
