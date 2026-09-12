# Nightly repair: unified model metadata and Codex cache usage

Date: 2026-09-12

Branch: `repair/nightly-batch-20260911`

Checklist owner: HN-20260911-001

## Candidate behavior

- Price and capability discovery use one exact OpenRouter identity resolver.
  Qualified IDs remain exact; public Engine families use a Provider namespace;
  other Engines require one unique exact catalogue ID/canonical ID/basename or
  declared alias. No prefix, family, or fuzzy match is permitted.
- The configured execution Engine and the OpenRouter source model are stored
  separately. Provider-reported values, including zero, remain actual. A
  catalogue valuation for Codex or another channel is rendered as an
  `OpenRouter reference estimate` and cannot masquerade as a billed amount.
- Successful and negative facts retain the existing 24-hour/15-minute policy.
  Per-model refreshes coalesce, network I/O occurs outside the whole-cache lock,
  and both caches use the shared BOM-tolerant, UTF-8/LF, revision-aware atomic
  writer.
- Pricing retains cache read/write, reasoning, request, additional dimensions,
  and supported `min_prompt_tokens` conditional tiers. A tier is selected per
  physical call; an unknown condition fails closed.
- Codex CLI and app-server cache-hit counts survive the Adapter contract. A
  cache miss is derived only when both total input and cached input are present;
  an omitted metric remains `None`, while a reported zero remains zero. Direct,
  background and HER meter paths preserve those values and their price revision.
- Startup, successful model selection, HER route selection and API Gateway
  configured/default models all schedule best-effort metadata refresh without
  blocking or rolling back usable configuration.

## Focused evidence

The selected tests cover a previously unlisted model, direct and OpenRouter
execution channels, exact catalogue ambiguity, shared price/capability source,
24-hour/15-minute cache behavior, concurrent refresh, BOM/CRLF migration,
conditional pricing, the observed Codex cache value `25,472`, HER propagation,
and localized reference-estimate rendering.

A bounded real public query for `openai/gpt-5.4` returned known price and
capability facts with the same exact source identity. Runtime adoption and
model-call acceptance are recorded in
`NIGHTLY_20260912_RUNTIME_ACCEPTANCE.md` and the nightly checklist.

## Protected boundary

No protected Core file is modified.
