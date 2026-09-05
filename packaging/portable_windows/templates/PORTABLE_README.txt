HASHI Portable for Windows x64
================================

Start_HASHI_TUI.bat
  Starts HASHI if needed, then opens the terminal interface. Closing the TUI
  leaves HASHI running.

Start_HASHI_Workbench.bat
  Starts HASHI and the full browser Workbench, then opens the system Edge or
  Chrome in app mode. No bundled browser is required.

Stop_HASHI.bat
  Stops HASHI, Workbench, and the portable Remote sidecar before USB ejection.

Diagnose_HASHI.bat
  Checks packaged runtimes, imports, local API health, write access, and free
  space without sending an inference request.

Runtime profile
---------------
- Windows 10/11 x64; no installed Python, Node, Git, npm, pip, or WSL needed.
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
