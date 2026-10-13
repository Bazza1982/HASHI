# HASHI Model Capability Discovery

Status: accepted PAO/Engine Adapter decision; Functions implementation candidate
updated 2026-10-05. Live adoption is recorded separately per instance.

Parent specifications:
[HASHI System Architecture](../ARCHITECTURE.md),
[PAO System Design](HASHI_PAO_SYSTEM_DESIGN.md), and
[Provider-Agnostic Multimodal Input](PROVIDER_AGNOSTIC_MULTIMODAL_INPUT_UPGRADE_TEST_PLAN.md).

## Ownership and boundary

PAO owns the derived, cache-backed fact for an exact Engine Provider/model
selection. An Engine Adapter owns whether it has a physical transport for a
modality. Instance configuration owns media policy and an exact manual
override. A Frontend Connector renders the resulting rejection. HERV3
consumes the same resolver independently for each configured stage model.

All implementation is in the replaceable Functions layer. Core owns none of
the source catalogue, model mapping, cache, media policy, or wording.

## Capability fact

One fact is bound to the exact requested Engine ID and model ID. It records:

- the exact external source model and canonical model IDs;
- input and output modality state as `supported`, `unsupported`, or `unknown`;
- source URL and source kind;
- fetch timestamp and, only for an unknown lookup, a retry deadline;
- adapter revision, content revision, and SHA-256 evidence digest; and
- an explicit unknown reason plus any historical last-known diagnostic snapshot.

Missing fields, malformed responses, 404s, timeouts, network failures, ID
mismatches, ambiguous aliases, and unqualified sources are `unknown` when no
verified exact-model fact exists. They are never converted to `unsupported`.
Once verified, both supported and unsupported modality facts remain valid
without a clock deadline. A failed later lookup cannot erase a verified fact;
only newly validated evidence or an explicit model-identity/adapter-revision
change may replace it. Truly unknown models still grant no native media route.

The derived cache is not configuration. It uses the shared revision-aware,
BOM-tolerant UTF-8/LF persistence boundary under the
instance `tmp` directory and never overwrites `agents.json`, state selection,
or a user-authored override. Existing known facts with a historical 24-hour
`expires_at` are accepted without time-based revocation; newly verified facts
have no expiry. Unknown lookup failures retry after 15 minutes. Price facts
retain their separate freshness policy. Whole-cache file locks cover only cache
read/replace operations, never network I/O; different models may refresh in
parallel while duplicate refreshes for one exact fact remain coalesced.

## Sources and exact mappings

OpenRouter's exact model endpoint is the primary source. Capability discovery
reads `architecture.input_modalities` and
`architecture.output_modalities`. It shares the allowlisted, byte-bounded,
timeout-bounded raw HTTP evidence fetch and concurrent request coalescing used
by pricing discovery. Capability and pricing then validate and persist that
evidence independently, so a pricing-schema failure cannot discard a valid
capability fact and a capability-schema failure cannot alter a price fact.

An already qualified model uses its exact route ID. Public Engine families map
to their Provider namespace (for example, `codex-cli` to `openai`) rather than
to a hard-coded list of model names, so a newly published exact model needs no
code row. Broker/private Engines may resolve an unqualified name only when the
bounded OpenRouter catalogue contains one unique exact ID, canonical ID,
basename, or declared alias. Ambiguity fails closed. There is no prefix,
family, similarity, or fuzzy-name matching; genuinely private aliases require
an explicit reviewed override.

A native Provider's documented moving model alias may use one explicit shared
mapping to the corresponding OpenRouter-managed `~provider/...-latest`
identity. The same mapping serves capability and price consumers so the two
facts cannot drift. It does not authorize fuzzy matching, an official Provider
pricing API, or a copied Provider price schedule.

Price and capability consumers share this identity resolver and raw evidence.
For a direct or CLI Engine, OpenRouter pricing is labelled a cross-channel
reference estimate; it is never evidence that the request used OpenRouter or
that the displayed amount was billed by the execution Provider.

OpenRouter's `file` modality remains a source fact named `file`. It does not
mean that HASHI may send every document, local path, PDF, audio file, or video
through an arbitrary Adapter. Provider limits are used only when a qualified
source or exact override supplies them; they are not inferred from modality
names.

## Effective native support

For each attachment, native support is the intersection of three independent
facts:

```text
exact model semantic capability
  AND Engine Adapter transport implementation
  AND current instance media policy
```

Authorized local tools are evaluated afterward as a separate fallback. A
tool fallback does not change the model fact, and native support does not grant
a tool. One attachment receives one route decision.

Manual declarations remain exact-model configuration and have priority over a
derived catalogue fact. Changing models immediately rebuilds the Adapter's
compatibility view from the new exact model before any asynchronous refresh,
so a former image model's startup snapshot cannot reopen a text-only route.

## Refresh and consumption

Model selection/configuration schedules a best-effort asynchronous refresh
after the selection is durably accepted. Startup loading, direct model
changes, API Gateway configured/default models, and the selected HERV3 main target
use the same cache path. Message admission and Provider invocation never wait
for a capability network request. Re-reading or reselecting a known exact model
does not trigger a time-based requalification; an explicit forced refresh may
replace it only with newly validated evidence, and transient refresh failure
preserves the known fact.

After refresh, direct adapters, Telegram media intake, API Gateway structured
conversation validation, and HERV3 main-target resolution all read the same exact
fact revision. HER's per-Turn capability cache includes that fact identity, so an
unknown-to-known transition or a new content revision invalidates the old
projection. Every caller joining one asynchronous refresh retains its own
completion callback. A callback updates the still-current Adapter snapshot
only when both Engine and model still match; a late result for a superseded
model is ignored.

## Rejection contract

Failures retain a stable machine code and name the exact model and media type
in user-facing English/Chinese notices:

| Condition | Code |
|---|---|
| catalogue confirms the model lacks the modality | `MODEL_MODALITY_UNSUPPORTED` |
| current exact capability is unconfirmed | `MODEL_CAPABILITY_UNKNOWN` |
| model supports it but the Adapter has no transport | `ADAPTER_MEDIA_ROUTE_UNIMPLEMENTED` |
| instance policy blocks native media | `MEDIA_POLICY_BLOCKED` |
| a required authorized local fallback is unavailable | `MEDIA_FALLBACK_UNAVAILABLE` |

An actual Provider response that rejects a modality remains
`PROVIDER_MODALITY_UNSUPPORTED`; it is distinct from preflight discovery.
Size, integrity, signature, and authorization failures retain their existing
codes.

## Verification boundary

Focused verification covers exact mapping, text-only facts, unknown retries,
legacy expired-known facts that remain routable, forced-refresh failure that
preserves known facts, 404/timeout/network/schema/ID/alias failures, manual override priority,
atomic concurrent refresh, independent pricing failure, model-switch cache
invalidation, API Gateway, HER stage consumption, image/audio/video/file
boundaries, local fallback, and Codex's actual `--image` command argument.

Offline tests prove source and consumer behavior. A real attachment response
proves live Provider use only after the target Worker generation is adopted
and the attachment is observed on that instance; source, cache, running
generation, and Provider acceptance are reported separately.
