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

## Feature extras

These commands assume a source checkout and an environment being prepared
for that feature profile. The published Python artifact contains the extracted
Nagare/Flow packages, not the full HASHI application. The runtime contract
still applies; do not modify dependencies in a running instance's environment.
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
used.
