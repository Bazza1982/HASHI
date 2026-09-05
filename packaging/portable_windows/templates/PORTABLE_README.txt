HASHI Portable for Windows x64
================================

Start_HASHI_TUI.bat
  On first use on a PC, requests administrator approval and installs a local
  program/runtime acceleration cache with visible progress. It then starts
  HASHI and opens the terminal interface. Closing the TUI leaves HASHI running.

Start_HASHI_Workbench.bat
  Uses the same automatic local acceleration cache, starts HASHI and the full
  browser Workbench, then opens system Edge or Chrome in app mode.

Install_HASHI_On_This_PC.bat
  Installs or checks the local acceleration cache before first use. This is
  optional because either Start file performs the same check automatically.

Uninstall_HASHI_From_This_PC.bat
  Removes only the machine's program/runtime cache and its Windows Installed
  Apps entry. It never removes the USB data directory.

Stop_HASHI.bat
  Stops HASHI, Workbench, and the portable Remote sidecar before USB ejection.

Diagnose_HASHI.bat
  Checks packaged runtimes, imports, local API health, write access, and free
  space without sending an inference request.

Runtime profile
---------------
- Windows 10/11 x64; administrator approval is expected for first-PC install.
- No global Python/Node, Git, npm, pip, WSL, or system PATH change is needed.
- Program/runtime files are installed under the machine's ProgramData folder.
  API keys, Sessions, Workspace, configuration, and user files remain on USB.
- Installation has real byte/file progress and no fixed 10-minute cutoff. It
  uses atomic staging and verifies every installed source file with SHA-256.
- If installation is cancelled or fails, launch continues from the complete
  expanded USB copy. The USB must remain inserted in both modes.
- HER v2 is the only top-level Engine.
- Official DeepSeek is the default Provider. Qwen/DashScope can be selected in
  settings after adding dashscope_api_key to data\secrets.json.
- TUI and Workbench share the same canonical conversation by default.
- LAN Remote pairing is one-click, protected actions require the issued token,
  and pairing tokens expire after seven days.
- Full host-drive access / danger-full-access mode is enabled.
- No local LLM and no offline generation.
- Tesseract OCR, Piper Chinese TTS, Edge TTS, FFmpeg, browser/desktop tools,
  multi-Agent orchestration, Nagare, Superloops, and Scheduler are included.
- Electron, Chromium, PaddleOCR, faster-whisper, BGE/vector models, CLI Engine
  adaptors, API Gateway service, package managers, tests, and developer files
  are not included.

API keys are stored as plaintext in data\secrets.json. Keep the USB secure.

For a 957 MB drive, use NTFS with its default 4 KiB allocation unit. The build
also passes a conservative 32 KiB allocation-unit capacity check; 64 KiB
allocation units waste too much space across thousands of small Python files.
