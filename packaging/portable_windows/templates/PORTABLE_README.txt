HASHI Portable for Windows x64 / HASHI Windows x64 便携版
===========================================================

Quick start / 快速开始
-----------------------
1. Keep this USB drive connected while using HASHI.
   使用 HASHI 时，请始终保持此 USB 连接。

2. Double-click one of these files:
   双击以下任一文件：

   Start_HASHI_TUI.bat
     Starts HASHI and opens the terminal interface.
     启动 HASHI 并打开终端界面。

   Start_HASHI_Workbench.bat
     Starts HASHI and opens the full Workbench in Microsoft Edge or Chrome.
     启动 HASHI，并在 Microsoft Edge 或 Chrome 中打开完整 Workbench。

3. On first use on each PC, Windows asks for administrator permission. Select
   Yes and keep the USB connected. Setup shows its current stage, real
   percentage, copied data volume, and verification file count. When setup is
   complete, the selected TUI or Workbench opens automatically.
   在每台电脑上首次使用时，Windows 会请求管理员权限。请选择“是”，并保持
   USB 连接。安装窗口会显示当前阶段、真实百分比、已复制容量和验证文件数。
   安装完成后，所选的 TUI 或 Workbench 会自动打开，无需再次双击。

   When upgrading from an older HASHI Portable, setup removes its obsolete
   shared local runtime only after verifying that it belongs to this USB and
   bundle. If ownership is unclear, setup stops without deleting it.
   从旧版 HASHI Portable 升级时，安装程序只会在确认旧本机运行组件属于此 USB
   与当前程序包后才清理它；如归属不明确，安装会安全停止且不会删除它。

Other launchers / 其他启动文件
-------------------------------
Install_HASHI_On_This_PC.bat
  Installs or checks the required local runtime. Normal TUI/Workbench startup
  performs this check automatically, so manual installation is optional.
  安装或检查所需的本机运行组件。TUI/Workbench 正常启动时会自动检查，通常
  无需手动运行此文件。

Uninstall_HASHI_From_This_PC.bat
  Stops this portable instance and removes only its local runtime and Windows
  Installed Apps entry. Type REMOVE to confirm. A unique instance identity and
  matching ownership markers prevent it from touching other HASHI instances.
  USB data is never removed.
  停止此便携实例，并只删除它自己的本机运行组件及 Windows“已安装的应用”
  条目。输入 REMOVE 后才会继续。唯一实例身份和匹配的所有权标记会防止它
  触及其他 HASHI 实例；USB 数据绝不会被删除。

Stop_HASHI.bat
  Stops HASHI, Workbench, and Remote. Run this before safely ejecting the USB.
  停止 HASHI、Workbench 和 Remote。安全弹出 USB 前请先运行此文件。

Diagnose_HASHI.bat
  Checks runtimes, local services, write access, and free space without sending
  a model request.
  检查运行组件、本机服务、写入权限和可用空间，不会发送模型请求。

Your data / 您的数据
--------------------
- API keys, conversations, Sessions, Workspace, settings, logs, and user files
  remain on this USB drive. Only replaceable program/runtime files are installed
  on the PC. The USB is still required whenever HASHI is running.
  API 密钥、对话、Session、Workspace、设置、日志和用户文件始终保留在此 USB
  中。电脑上只安装可重新生成的程序与运行组件；运行 HASHI 时仍必须插入 USB。

- API keys are plaintext in data\secrets.json. Keep the USB physically secure.
  API 密钥以明文保存在 data\secrets.json 中，请妥善保管此 USB。

If setup cannot complete / 如果安装无法完成
------------------------------------------------
- HASHI will show the reason and will not start from an incomplete installation.
  You can choose Retry or Exit in the original launcher window.
  HASHI 会显示失败原因，并且不会从未完成的安装启动。您可在原启动窗口中选择
  “重试”或“退出”。

- Setup diagnostics are saved in data\logs\hashi-setup.log. You can also run
  Diagnose_HASHI.bat for a guided system check. Setup never moves or deletes
  your personal USB data.
  安装诊断保存在 data\logs\hashi-setup.log。您也可以运行 Diagnose_HASHI.bat
  进行引导式系统检查。安装过程不会移动或删除 USB 中的个人数据。

Runtime profile / 运行配置
--------------------------
- Windows 10/11 x64. No global Python/Node, Git, npm, pip, WSL, or system PATH
  change is needed.
  支持 Windows 10/11 x64；无需全局安装 Python/Node、Git、npm、pip 或 WSL，
  也不会修改系统 PATH。

- HER v2 is the only top-level Engine. Official DeepSeek is the default
  Provider; Qwen/DashScope is configurable. TUI and Workbench share the same
  canonical conversation by default.
  HER v2 是唯一顶层 Engine；默认使用 DeepSeek 官方 API，也可配置
  Qwen/DashScope。TUI 与 Workbench 默认共享同一条权威对话。

- One-click LAN Remote pairing lasts seven days. Full host-drive access is
  enabled. There is no local LLM or offline generation.
  LAN Remote 一键配对有效期为七天。已启用宿主机全盘访问；不包含本地 LLM，
  也不支持离线生成。

- Included: Tesseract OCR, Chinese Piper TTS, Edge TTS, FFmpeg, browser/desktop
  tools, multi-Agent orchestration, Nagare, Superloops, and Scheduler.
  已包含：Tesseract OCR、中文 Piper TTS、Edge TTS、FFmpeg、浏览器/桌面工具、
  多 Agent 编排、Nagare、Superloops 和 Scheduler。

For a 957 MB drive, use NTFS with the default 4 KiB allocation unit.
957 MB USB 建议使用 NTFS 与默认 4 KiB 分配单元。
