# HASHI dependency profiles

HASHI keeps its default source-checkout experience complete while allowing
smaller environments to install only the features they use. Package metadata
and named extras in `pyproject.toml` are authoritative; `setup.py` is only a
compatibility shim.

## Recommended profiles

| Need | Command | Includes |
|---|---|---|
| Normal local HASHI | `python -m pip install -r constraints/standard-py312.lock` | Core, media, Hashi Remote, TUI at the approved versions |
| Development and tests | `python -m pip install -r requirements-dev.txt` | Standard profile and test tools |
| Minimal source-checkout environment | `python -m pip install -e .` | Base Python dependencies; optional APIs, Remote, and TUI need their extras |
| Every declared integration | `python -m pip install -e ".[all]"` | All optional profiles; potentially very large |
| HERV3 Privacy Level 2 | `python scripts/provision_privacy_runtime.py` | Separate Presidio/spaCy detector and English model; never Core |

## Feature extras

These commands assume a source checkout and a disposable environment being
prepared for that feature profile. They must not target the interpreter of a
running HASHI instance. The published Python artifact contains the extracted
Nagare/Flow packages, not the full HASHI application. The runtime contract
still applies; native Function profiles use their owning isolated sidecar.
See [installation](INSTALL.md) and [distribution scope](RELEASES.md).

Install one or combine several extras in one command, for example:

```bash
python -m pip install -e ".[media,remote]"
```

| Extra | Purpose |
|---|---|
| `standard` | Media, Hashi Remote, and TUI together |
| `media` | Image normalization and PDF/media inspection |
| `remote` | Remote API, TLS, and LAN discovery |
| `tui` | Rich terminal interface |
| `browser` | Playwright browser automation; Chromium still needs `playwright install chromium` |
| `whatsapp` | WhatsApp transport and QR linking |
| `voice` | Edge and Piper text-to-speech providers |
| `transcription` | Dependency metadata for an explicitly isolated local faster-whisper helper; never install it into Core |
| `ocr` | Paddle-based local OCR; large platform-sensitive install |
| `vector` | Semantic vector memory and local encoder runtime |
| `postgres` | Enterprise PostgreSQL lease store and pooling |
| `kubernetes` | Kubernetes Lease scheduler backend |
| `test` | Pytest and async test support |

System executables and model files remain separate from Python packages. For
example, FFmpeg, browser binaries, local TTS models, and vector model weights
must still be installed or supplied when their selected feature requires them.

## Isolated Privacy Level 2 detector

`requirements-privacy.txt` pins Presidio 2.2.364 and spaCy 3.8.16; its
English `en_core_web_sm` 3.8.0 wheel has a SHA-256 fragment. Transitive Python
packages are resolved during installation, so this profile is not a fully
hash-locked offline bundle. Run it with the approved Python 3.12.13 interpreter
from a source checkout:

```bash
python scripts/provision_privacy_runtime.py
python scripts/provision_privacy_runtime.py --check
```

The commands create `.venv-privacy` beside the source, not inside the Core
environment. The readiness check executes the actual local detector on
synthetic name and email values and verifies package versions. An alternate
runtime location can be selected with `--runtime-dir`; launchers must then set
`HASHI_PRIVACY_FILTER_PYTHON` to that runtime's Python. The npm installer does
both automatically in a versioned user directory. The enterprise image
prepares the same separate environment during its build. The size-limited
Portable Windows image provides an on-target installer instead of bundling the
model; it needs network access after the local copy is installed.

This installation provides **eligibility**, not automatic activation. Level 2
is HERV3/qualified DeepSeek only, requires explicit risk acceptance, and may
miss PII, especially in non-English text. If the detector is absent or fails,
the outbound request is blocked rather than sent unfiltered.

## Isolated transcription runtime

Speech-to-text native packages are deliberately outside the standard Core
profile. Never install `requirements-transcription.txt` or the `transcription`
extra into a running HASHI environment. Prepare the instance-owned helper from
the source root instead:

```bash
python scripts/provision_transcription_runtime.py --bridge-home /path/to/instance
python scripts/provision_transcription_runtime.py --bridge-home /path/to/instance --check
```

On Windows, pass the instance directory with PowerShell syntax:

```powershell
python .\scripts\provision_transcription_runtime.py --bridge-home C:\path\to\instance
python .\scripts\provision_transcription_runtime.py --bridge-home C:\path\to\instance --check
```

The provisioner creates a separate Python 3.12.13 environment from
`constraints/transcription-py312.lock`, verifies `faster-whisper`,
`ctranslate2`, and `av`, then atomically writes
`state/platform/transcription.json`. It refuses any target overlapping the
active Core environment. The model cache remains a separate user/platform
asset and is populated by faster-whisper when the selected model is first
used. `VoiceTranscriber` communicates with the generation-pinned helper over
versioned JSON and retires its pipe when the Function Worker closes.

## Security maintenance of optional runtime locks

Decision: 2026-10-26; source repair: 2026-10-28. Owner: PAO runtime
deployment, Platform Configuration. The Core standard dependency baseline is
unchanged.

The transcription and full TTS locks pin `filelock==4.2.1` and
`setuptools==84.1.0`. Earlier transcription `filelock==3.25.2`, full TTS
`filelock==4.2.0`, and both profiles' `setuptools==84.0.0` have published
security advisories. The compact Edge-only profile does not contain these
packages and does not acquire the full Kokoro/Torch profile for this repair.

Git checks out these locks with LF so their byte-level runtime identities stay
stable across platform Git settings. Changing lock contents still selects a new
isolated runtime.

An audit receipt must identify its interpreter, installed versions and audit
input. Auditing a requirements file or another Python environment does not
prove that the running Core contains the reported packages. A repair of a
standalone audit/development environment is a separate operation from fixing
the deployment lock that creates an isolated Function environment.

For an installed transcription helper, the owning provisioner prepares the new
lock-derived runtime directory, probes it, and publishes its platform selection
through revision-checked configuration persistence. It leaves the earlier
runtime intact. Without an explicit `HASHI_TRANSCRIPTION_PYTHON` override,
`VoiceTranscriber` reads that platform selection for each transcription request;
that configuration path does not require restarting Core or a Worker. An
explicit interpreter override remains authoritative and must not be silently
replaced or described as having adopted a platform selection.

A source lock edit does not update an existing helper or rebuild an already
published installer. Record source validation, installed-environment readiness,
and actual use separately. Never install this optional profile into a running
Core, or restart any instance merely to make a dependency audit green.
