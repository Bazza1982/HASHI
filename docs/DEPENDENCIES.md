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
| `transcription` | Local faster-whisper transcription |
| `ocr` | Paddle-based local OCR; large platform-sensitive install |
| `vector` | Semantic vector memory and local encoder runtime |
| `postgres` | Enterprise PostgreSQL lease store and pooling |
| `kubernetes` | Kubernetes Lease scheduler backend |
| `test` | Pytest and async test support |

System executables and model files remain separate from Python packages. For
example, FFmpeg, browser binaries, local TTS models, and vector model weights
must still be installed or supplied when their selected feature requires them.

### Isolated transcription runtime

Local transcription may be added or upgraded while HASHI is online only in a
separate Python environment. Point the Function layer at that interpreter with
`HASHI_TRANSCRIPTION_PYTHON` or the instance-private file
`state/platform/transcription.json`:

```json
{
  "python": "C:\\path\\to\\transcription-runtime\\Scripts\\python.exe"
}
```

Prepare that environment from `constraints/transcription-py312.lock`; never
install the lock into the running Core environment. `VoiceTranscriber` starts
the generation-pinned helper lazily and exchanges only versioned JSON over
stdio. Closing the Function Worker closes the pipe and retires the helper.
