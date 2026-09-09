# HASHI Portable Windows x64

This builder produces an allowlisted, self-contained Windows installation
bundle for a USB drive whose total capacity is 957,000,000 bytes. It does not
write to or format a USB device.

The USB is installation and transfer media, not the runtime disk. The user
runs `Install_HASHI_On_This_PC.bat`, approves Windows administrator access, and
the installer copies and verifies the complete program, private Python runtime,
and clean configuration templates to:

```text
C:\HASHI-Portable\
```

Copying uses a unique staging directory on the selected destination drive. The
installer verifies the static program image against `SHA256SUMS.txt`, verifies
mutable files against their authoritative source, writes an identity-bound
ownership marker, and only then activates the staging directory. A valid older
installation is moved to the rollback slot; the new version adopts a verified
copy of its local data. The same bundle performs no copy and only repairs
shortcuts. An invalid, incomplete, linked, or differently owned destination
fails closed.

The public image contains no concrete HASHI instance identity, conversation,
credential, or source-machine path. First installation generates the local
instance identity and identity-lineage ID on the destination PC, creates fresh
local and Remote authentication tokens, and asks for the installation/initial
runtime language. Runtime language changes remain in the local data tree.

The installer creates two Windows shortcuts on the invoking user's desktop:
`Start HASHI` and `Stop HASHI`. Each shortcut targets
the corresponding batch launcher inside the local installation, so it cannot
become stale after the USB is removed. First installation reports real copy and
verification percentages. After success, the same elevated visible window
shows “Press any key to launch HASHI / 按任意键启动 HASHI” and opens the default
TUI without another menu. HASHI and all child runtime processes inherit the
administrator token. Installation success and subsequent startup failure are
reported independently.

Local API discovery is strict. HASHI always binds its Backend API to
`127.0.0.1`, prefers the configured port, and asks Windows for a free port if
that port is occupied. Once health and instance identity are verified, the
launcher atomically writes PID, process start time, actual API port, Portable
identity, HASHI instance identity, build identity, and a random launch nonce to
`data\state\local-endpoint.json`. TUI, Diagnose, and Stop use that
record; they do not scan adapters, guess WSL gateways, or fall back to a
`172.x` address. Logs are always resolved from the local copy at
`data\logs\bridge.log`.

`Uninstall_HASHI_From_This_PC.bat` may be run from the transfer image or the
verified local installation. It requests confirmation, stops only processes whose executable or dedicated
browser profile belongs to the validated local installation, removes the two
known shortcuts, and deletes only the exact marked local directory. A marker,
Portable identity, build identity, target path, or reparse-point mismatch stops
the uninstall without deletion. Uninstall permanently deletes the local
conversations, settings, logs, and plaintext API keys; it does not alter the
USB bundle.

The image contains HER v2, official DeepSeek defaults, configurable Qwen via
the official DashScope OpenAI-compatible endpoint, TUI, the HASHI Backend API,
Remote/LAN/HChat, Scheduler, Nagare, Superloops, browser/desktop
tools, Tesseract OCR, FFmpeg, Edge TTS, and one Chinese Piper voice. It uses the
host Edge or Chrome and does not bundle Electron or Chromium. There is no CLI
Engine, local LLM, semantic vector runtime, system Python or Node installation,
global PATH change, Windows service, registry installation, or ProgramData
runtime cache. The retired Workbench frontend and Node server are not included.

The launched runtime is Windows-native-only: its isolated process environment
includes the system Windows PowerShell directory explicitly, while Bash/WSL
shell selection and direct WSL launcher execution are blocked. A missing native
Windows executor therefore fails visibly instead of crossing into WSL.

Remote discovery remains visible on the LAN. Pairing is approved in one click,
then protected operations require the issued bearer token, which expires after
seven days; the LAN itself is not treated as authenticated.

Build from the HASHI repository root:

```bash
python3 packaging/portable_windows/build.py \
  --expected-revision <full-commit-id> \
  --expected-tree <full-tree-id>
```

The builder uses an existing `7z`/`7zz` command when one is available. On a
Windows host without 7-Zip, it downloads the hash-pinned official 7-Zip MSI
and administratively extracts a build-only copy into the temporary staging
area; it does not install 7-Zip on the PC.

The expected Git identities are optional for local development and required
for a release build. Before downloading or staging an image, the builder
checks them against the clean source worktree and verifies that the Portable
hash lock contains every dependency from the runtime policy's standard lock
at the exact approved version. It checks the source identity again before the
staging directory is published.

The default build is public and contains no credentials. To privately finalize
an image with one user-supplied DeepSeek key, put only that key in a protected
file and pass `--private-deepseek-key-file <path>`. The key is never accepted as
a command-line value and is never printed. No source `secrets.json`,
DashScope/OpenRouter key, Remote shared token, identity, or conversation is
copied. Private finalization creates independent local authentication tokens;
first installation rotates the local tokens again for the destination PC.

Re-running Install with a different verified bundle performs an update. Static
program/runtime files come from the new bundle while the complete authoritative
local `data` tree—identity, lineage, language, settings, credentials, and
conversations—is verified and preserved. The former program version is kept at
`<install-root>.previous`; `Rollback_HASHI_On_This_PC.bat` swaps versions while
carrying forward the latest user data. Any activation failure automatically
restores the former installation. `Update_HASHI_On_This_PC.bat` requires an
existing installation and never silently creates a new identity.

The build fails closed if the result exceeds 957,000,000 bytes or if a CLI
Engine adaptor/package manager is present. Every downloaded asset is pinned by
SHA-256, Python dependencies are hash locked, and `SHA256SUMS.txt` covers the
fresh image. Because `data` is intentionally mutable after publication, the PC
installer uses static manifest hashes for non-data files and live source/dest
hash comparison for `data`. Capacity is checked conservatively using 32 KiB
allocation units; NTFS with its default 4 KiB allocation unit is recommended.

Only Git-tracked files from the explicit source allowlist enter `app/hashi`.
The allowlist includes `__main__.py`, `runtime-entry.json`, `pyproject.toml`,
the standard dependency lock named by `[tool.hashi.runtime]`, and every
protected Core source required to recompute the runtime fingerprint. The
Function qualification/execution profile in `runtime-entry.json` is copied
byte-for-byte and is a required runtime-contract input. The bundled Python
executes both the shared runtime-contract checker and the shared Function
contract after dependencies are installed and before the image can be
published. The adapter registry must still cover the complete shared backend
catalogue, while the Function contract resolves only adapter modules physically
present in the selected distribution. A full source checkout therefore checks
every adapter; this intentionally pruned HER-only image checks every adapter it
ships without requiring excluded CLI Engines.
Untracked development state, logs, local secrets, project-private workflows,
instance-local Skills, and generated workflow runs cannot leak into a newly
built image. The tracked HASHI input must be clean, and its revision is checked
again before publication.
