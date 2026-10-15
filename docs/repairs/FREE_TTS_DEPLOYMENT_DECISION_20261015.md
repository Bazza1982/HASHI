# Free TTS Deployment Decision (2026-10-15)

Status: implemented and offline-verified on HASHI3 `debug-voice`; standard
deployment adoption and merge to `main` remain separate states.

## Ownership and boundary

- Functional owner: HERV3 voice Function.
- Engineering layer: platform/deployment configuration and replaceable isolated
  TTS runtime.
- Protected Core: unchanged.
- `/call`: unchanged.

## Adopted catalogue

HASHI's free concrete voice catalogue now gives both female and male choices for
Edge in US English, UK English, Simplified Chinese, Traditional Chinese, Hong
Kong Chinese and Japanese. Kokoro provides both genders for US English, UK
English, Simplified Chinese and Japanese. Windows SAPI exposes only exact voices
installed on the current machine; it never advertises or fabricates a missing
system voice. HASHI3 currently qualifies Zira and David for US English and
Huihui for Simplified Chinese.

The versioned preview bundle contains the exact OGG and MP3 sample for all 31
non-macOS presets (62 files). The `jm_kumo` provenance and attribution are
recorded in `THIRD_PARTY_NOTICES.md`.

## Deployment contract

Standard npm, native Windows and WSL deployment paths prepare an isolated,
pinned free TTS runtime containing Edge and Kokoro plus the media converter.
They download a pinned Kokoro-82M revision and locally verify the model and every
Kokoro voice derived from the authoritative voice catalogue before publishing
`state/platform/tts.json`.
Windows voices require no Python package: the Function discovers installed SAPI
voices at runtime. No paid TTS service is installed or selected.

Preparation never installs native speech dependencies into Core. If the local
platform cannot install or verify the helper, the deployment reports incomplete
media capability instead of publishing a false-ready runtime. Installing these
providers does not enable voice replies or choose a user voice; `/voice` remains
an explicit user preference.

The qualified HASHI3 helper occupies about 1.81 GiB of logical file content,
primarily CPU-only Torch, language front ends and the pinned model cache.
Standard deployments must plan for that footprint.

The 957 MB Windows portable image is a documented capacity exception. It keeps
an isolated Edge-only helper, installed Windows SAPI voices and the bundled
Piper voice. Kokoro/Torch cannot fit within that artifact's enforced size, so
the portable build uses and verifies a separate pinned Edge-only lock rather
than silently exceeding capacity.

## Focused validation

The implementation is accepted only when catalogue coverage, exact system-voice
discovery, Kokoro isolation/model-cache coverage, Edge-only portable packaging,
all preview hashes and decodes, deployment locks, protected-Core invariants and
the HASHI3 live synthesis checks pass. Source verification, deployment adoption,
GitHub publication and user acceptance are reported separately.

HASHI3 evidence at implementation closeout: 107 focused catalogue, runtime,
deployment and portable checks passed; 73 routing, `/say`, UI-language, Windows
TTS and unchanged `/call` preview checks passed; all 62 preview files passed
hash and decode validation. Both dependency locks passed dry-run installation,
the full sidecar check qualified the pinned model and all 10 configured Kokoro
voices, all 28 requested Edge/Kokoro/Windows presets reported ready, and the
protected-Core check passed.
