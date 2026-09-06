HASHI Portable for Windows x64 / HASHI Windows x64 便携版
===========================================================

Quick start / 快速开始
-----------------------
1. Double-click:
   请双击：

   Install_HASHI_On_This_PC.bat

2. Approve the Windows administrator request. HASHI copies its complete working
   folder to the local PC and verifies every file. The window shows real copy
   and verification percentages. Do not remove the USB until installation is
   complete.
   请批准 Windows 管理员权限。HASHI 会把完整工作文件夹复制到本机并验证每个
   文件。窗口会显示真实的复制与验证百分比。安装完成前请勿拔出 USB。

3. After successful installation, the same window displays:
   安装成功后，同一个窗口会显示：

   Press any key to launch HASHI.
   按任意键启动 HASHI。

   Press a key to open the preset TUI directly. There is no second selection
   menu. HASHI inherits the installer's administrator privileges.
   按键后会直接进入预设 TUI，不会出现第二个选择菜单。HASHI 会继承安装器的
   管理员权限。

Local installation / 本机安装
-------------------------------
HASHI is installed here:
HASHI 安装在：

C:\HASHI-Portable\

All day-to-day reads and writes—including conversations, configuration, logs,
SQLite state, browser profile, and API tokens—use this local copy. HASHI does
not run from the USB, so the USB can be removed after installation completes.
日常所有读写——包括对话、配置、日志、SQLite 状态、浏览器配置和 API 密钥——
都使用本机副本。HASHI 不会从 USB 运行，因此安装完成后可以拔出 USB。

Desktop shortcuts / 桌面快捷方式
-----------------------------------
Start HASHI
  Starts elevated HASHI and opens the terminal interface.
  以管理员权限启动 HASHI，并打开终端界面。

Stop HASHI
  Stops this local HASHI instance and its dedicated Workbench browser.
  停止此本机 HASHI 实例及其专用 Workbench 浏览器。

Start HASHI Workbench
  Starts elevated HASHI and opens the full Workbench in Edge or Chrome.
  以管理员权限启动 HASHI，并在 Edge 或 Chrome 中打开完整 Workbench。

The shortcuts point to launchers inside the local installation, not the USB.
快捷方式指向本机安装目录内的启动文件，而不是 USB。

Running the installer again / 再次运行安装程序
------------------------------------------------
If the matching local installation is already valid, no files are copied. The
desktop shortcuts are repaired and HASHI opens from the existing local copy.
If the destination exists but its identity or ownership marker is invalid, the
installer stops without overwriting it.
如果匹配的本机安装已经有效，则不会再次复制文件；安装程序只会修复桌面快捷
方式，并从已有本机副本打开 HASHI。如果目标目录存在但身份或所有权标记无效，
安装程序会停止，绝不会覆盖该目录。

Local address and logs / 本机地址与日志
-----------------------------------------
HASHI always uses the safe loopback address 127.0.0.1. It prefers the configured
port and automatically asks Windows for a free port when necessary. TUI and
Workbench read the verified endpoint written by this exact HASHI process; they
never guess a WSL/172.x address or connect to an unrelated service.
HASHI 始终使用安全的回环地址 127.0.0.1。它会优先使用配置端口；端口被占用时，
会自动请 Windows 分配空闲端口。TUI 与 Workbench 只读取由这个 HASHI 进程写入
并通过验证的端点，绝不会猜测 WSL/172.x 地址或连接无关服务。

Runtime endpoint:
运行端点：

C:\HASHI-Portable\data\state\local-endpoint.json

Main log:
主日志：

C:\HASHI-Portable\data\logs\bridge.log

Other USB launchers / USB 上的其他启动文件
------------------------------------------------
Start_HASHI_TUI.bat and Start_HASHI_Workbench.bat open the matching installed
local copy. They do not run HASHI from USB. Diagnose_HASHI.bat checks the local
installation without sending a model request.
Start_HASHI_TUI.bat 与 Start_HASHI_Workbench.bat 会打开匹配的本机安装，不会从
USB 运行 HASHI。Diagnose_HASHI.bat 会检查本机安装，且不会发送模型请求。

Uninstall / 卸载
------------------
Run this file from the original matching USB:
请从原始且身份匹配的 USB 运行：

Uninstall_HASHI_From_This_PC.bat

Type REMOVE when prompted. Uninstall stops HASHI, removes its desktop shortcuts,
and deletes only the verified folder C:\HASHI-Portable\. This permanently deletes
the local conversations, settings, logs, and plaintext API tokens. The USB is
not changed. An invalid marker, different identity, link, or unexpected target
causes uninstall to stop without deleting anything.
提示时输入 REMOVE。卸载会停止 HASHI、删除桌面快捷方式，并且只删除经过验证的
C:\HASHI-Portable\。本机对话、设置、日志及明文 API 密钥会被永久删除；USB 不会
被修改。若标记无效、身份不同、目录为链接或目标异常，卸载会停止且不删除任何内容。

If setup cannot complete / 如果安装无法完成
------------------------------------------------
Installation and startup are separate states. If installation succeeds but the
subsequent launch fails, HASHI reports only “HASHI startup failed / HASHI 启动
失败”; it does not incorrectly relabel the completed installation as failed.
安装与启动是两个独立状态。如果安装成功但后续启动失败，HASHI 只会报告
“HASHI 启动失败”，不会把已经完成的安装误报为失败。

Installation diagnostics are saved temporarily at:
安装诊断临时保存在：

%TEMP%\HASHI-Portable-install.log

Runtime profile / 运行配置
--------------------------
- Windows 10/11 x64. No global Python/Node, Git, npm, pip, WSL, Windows service,
  registry installation, or system PATH change is needed.
  支持 Windows 10/11 x64；无需全局安装 Python/Node、Git、npm、pip 或 WSL，
  不创建 Windows 服务，不写入安装注册表，也不修改系统 PATH。

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
