# TTS Language Routing and Degradation Decision (2026-10-15)

Status: language routing and concrete `/voice` preview extension adopted and
accepted on HASHI3 `debug-voice` on 2026-10-15.

## Ownership and boundary

- Functional owner: HERV3 voice Function.
- Presentation owners: Telegram Frontend Connector and Workbench speech presentation.
- Engineering layer: replaceable Function/runtime UI plus Workbench frontend.
- Protected Core: unchanged.

The shared routing decision is made once in HASHI. Frontends provide a locale hint and
present returned evidence; they do not maintain their own language or fallback rules.

## Approved behavior

1. Every known TTS provider and configured voice has a capability profile. The profile
   records native language, whether other non-English languages are explicitly
   unsupported, and style-control support. English is compatible with every voice.
2. `/voice` separates the preferred TTS voice from the optional semantic voice style.
   Choosing one no longer overwrites the other. Unsupported style controls are ignored
   and audited instead of failing synthesis.
3. When no voice is selected, Workbench supplies its display locale. Other surfaces use
   the HASHI `/language` preference. The text itself remains the stronger signal when
   one language reaches the dominance threshold.
4. Language switching requires at least 90% dominance among meaningful CJK script
   content. Code/markup cleanup remains in the synthesis provider. English embedded in
   CJK prose is neutral because all voices support it. Han-only text uses the selected
   voice language first, then the source locale, to resolve Chinese/Japanese ambiguity.
5. An explicitly incompatible selected voice is replaced only for that synthesis:
   HASHI first chooses a compatible voice from the same provider. The saved default is
   never rewritten.
6. Provider fallback is explicit and monotonic: paid network -> free network -> free
   local, or free network -> free local. A free/local route never escalates to a paid or
   network route. The detected target language is preserved across fallback.
7. Any temporary voice adaptation or provider fallback is returned as structured
   evidence. Workbench shows it beside the player and Telegram places it with the voice
   message, so a fallback voice cannot be mistaken for the configured normal voice.
8. Voice buttons use short language, gender, and provider annotations, for
   example `晓晓（简中 · 女声 · Edge）` and `七海（日语 · 女声 · Edge）`.
9. Selecting a concrete `/voice` TTS preset saves it and immediately publishes
   a prerecorded sample synthesized with that exact provider, voice, and native
   language. Telegram receives OGG/Opus and browser Frontend Connectors receive
   MP3. Selection never invokes live TTS and never substitutes a different
   voice. A missing, corrupt, or provenance-mismatched sample leaves the saved
   selection intact and gives the user a clear unavailable notice.

Preview assets use a versioned manifest containing the preset alias, provider,
stable voice identity, locale, fixed script, byte size, and SHA-256 digest.
HASHI validates that provenance before publication. The generated bundle covers
all 31 non-macOS presets. macOS-native samples must be generated and verified on
macOS; until then those selections fail safely with the same visible notice.
The independent `/call` voice menu and previews are unchanged.

## Audit contract

Each attempted synthesis writes one JSONL decision record under the Agent workspace at
`logs/voice_synthesis.jsonl`. The record includes:

- request and source surface identifiers;
- source locale and configured provider/voice/style;
- text SHA-256 and character count, never the private text itself;
- language scores, threshold, confidence and ambiguity reason;
- primary route, same-provider adaptation, fallback candidates and capability source;
- every attempt, cooldown bypass, safe failure code/reason and duration;
- final provider/voice, fallback evidence and the user notice.

Audit-write failure is reported to the ordinary runtime logger and never turns successful
audio generation into a user-visible failure.

## Focused validation

Red evidence was recorded before implementation: the new routing contract test failed
during collection because `orchestrator.tts_routing` did not exist.

Green evidence after implementation:

- expanded HASHI3 routing, menu, provider/runtime, Worker, `/say`, locale, and
  Workbench API suite: 220 passed;
- Workbench speech proxy and presentation integration suite: 69 passed;
- complete Workbench server suite: passed;
- complete Workbench UI policy suite: 744 passed;
- Workbench production build: passed;
- protected Core check: passed.

The concrete `/voice` preview extension additionally recorded four focused
failures before implementation. Green evidence covers exact preset provenance,
gender/locale metadata, Telegram publication, browser MP3 publication,
missing-preview behavior, `/call` isolation, locale catalogs, routing/runtime,
and portable packaging: 71 pytest checks passed. All 62 bundled OGG/MP3 files
decoded successfully; Python compilation, focused Ruff checks, patch hygiene,
and the protected Core check also passed.

Live HASHI3 and Workbench validation was recorded separately after hot adoption;
the user reported acceptance success on 2026-10-15. The subsequent free TTS
deployment expansion is governed by
[the free TTS deployment decision](FREE_TTS_DEPLOYMENT_DECISION_20261015.md).

## 2026-11-02 advanced voice repair

The MSI user requested repair after exact diagnosis: selecting an advanced
voice saved the choice but its prerecorded entry was absent, and the service's
Chinese language default replaced other explicitly selected Chinese voices.

A selected voice native to the detected language now retains both its identity
and provider options. Per-language character defaults still resolve missing or
non-native selections ahead of generic English compatibility. Existing
Chinese/English/Japanese character switching remains covered by the owning
tests; a fixed language default is not permission to replace another
same-language manual choice.

The offline preview generator reads the supplied instance catalogue and builds
its configured service adapter, rather than enumerating only class-level
presets. It invokes the exact provider/voice directly, without language
adaptation or provider fallback. Resuming requires exact provider, voice, locale,
script and verified OGG/MP3 provenance; a still-valid file with stale identity is
regenerated. Credentials and instance paths are not copied into the bundle.

The repair adds both renderer formats for the 23 advanced MiniCPM voices in the
MSI catalogue, preserving the original 62 shipped files. Canonical product
sample text is the only synthesis input. Generated samples are product preview
assets, not archives of conversation speech.

Focused red evidence records the manual voice being replaced and the dynamic
alias being rejected by the generator. Source tests, real model generation,
manifest/audio verification, adoption and actual frontend publication are
reported separately. The source candidate does not imply the active Functions
generation has changed; MSI's user owns any necessary hot reboot.
