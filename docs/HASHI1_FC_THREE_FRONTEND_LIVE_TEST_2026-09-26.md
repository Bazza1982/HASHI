# HASHI1 FC 三端现场测试记录（2026-09-26）

## 范围

- 目标：HASHI1 的 `Test` / `testing` Agent。
- 前端：TUI、外部 Workbench、Telegram Web 的 `@helper_sunny_li_bot`。
- 覆盖：文字、命令、Agent 发出的图片/文件/音频展示；不测试语音输入或麦克风。
- 测试期间不修代码；问题集中到测试结束后处理。

## 冻结基线

- 分支：`feature/fc-unified-io-20260925`；开始时提交：`1c5084f6`。
- HASHI1 Core PID：`910`；Functions generation：`sha256:00be86a9ae4525ed1d3fe2495d0307b6a1dd6cb69754b6998003f1a00b70abbd`。
- Protected Core 检查通过。HASHI1、HASHI2 均在线。
- Workbench 和 Telegram 的 `/status full` 指向同一个 Test 会话；TUI 以 attach-only 接入并使用同一 Test 会话。

## 已通过

- 同一唯一文字标记分别从 TUI、Workbench、Telegram 发出，三端收到相同回复 `FC-LIVE-TEXT-A`，未见重复回复。
- Workbench `/help` 与 Telegram `/help` 显示相同命令分类；Telegram 报 94 条可用命令。命令目录有 95 项，其中 77 项菜单可见，具体计数/隐藏项映射待核对。
- Telegram `/status full` 与 Workbench 会话显示相同 Test workspace 和 Session；TUI 状态确认连接 HASHI1/Test。
- Agent 语音输出：TTS 预览与 `/say` 均送达 Telegram；Workbench 显示 Audio 播放器，播放进度从 0:00 前进到 0:01/0:02。未使用麦克风或语音输入。
- 测试结束后通过 Telegram `/voice off` 将 Test Agent 语音模式恢复为 OFF。

## 待修复或继续核对

### FC-LIVE-001：公共 `/logo` 命令返回终端专用结果

- `/logo` 出现在 Workbench/Telegram 的公共帮助中。
- Telegram 收到“已在终端显示标志。”；Workbench 也显示终端专用文案，没有图片/媒体。
- Workbench 时间线上另外出现一条英语回复及一条额外中文回复；需用事件/回执确认是否为一次请求的重复结果。
- 结束后应依据单一命令事实所有者决定：让 `/logo` 通过 FC 发布媒体，或把它登记为明确的 TUI 本地例外并从外部菜单隐藏。

### FC-LIVE-002：Telegram Bot 显示名与 HASHI1 目标不一致

- Telegram 聊天显示名曾显示 `大白 (MSI)` / `大白 (HASHI2)`。
- 同一聊天 `/status full` 显示的是 HASHI1 `testing` workspace，因此暂未发现错误路由证据。
- 先保留为外部 Bot 显示名待核对项；测试期间不修改 Telegram Bot 配置。

### FC-LIVE-003：命令目录与帮助计数映射待核对

- Runtime `/help` 显示 94 条可用命令；规范目录包含 95 个项目，77 个标记为菜单可见。
- 待确认隐藏项、别名及动态命令如何计入，特别是 `/wol` 在帮助中可见但目录标记为隐藏。

## 后续记录

测试完成后追加剩余矩阵、修复结果、复测证据、提交及采用状态。以上编号仅作测试记录，不代表问题已归因或修复。

## 继续测试记录（2026-09-26）

### 已完成的新增覆盖

- 文字格式用例 `FC-LIVE-TEXT-B` 分别从 TUI、Workbench、Telegram 提交；三端语义一致，中文、emoji、符号和尖括号内容均保留，没有执行 HTML 标签。
- Workbench `/status` 通过发送按钮返回详细 Test 状态，并显示与 Telegram 相同的 Session ID。
- Workbench `/backend`、`/provider`、`/model`、`/effort` 可打开对应选择卡；当前选项标记正常。本轮没有选取新配置。
- Telegram `/version` 返回 HASHI1 实例、当前分支和 Functions 代次，与冻结基线一致；唯一工作区差异是本测试记录文件。
- Telegram `/privacy` 展示等级卡与当前等级 1；没有修改设置。此前发送的只读命令批次还包含 `/backend`、`/provider`、`/model`、`/effort`、`/mode`。

### 新增观察与待核实项

#### FC-LIVE-004：相同 Markdown 内容的跨 Connector 呈现不一致（待复测）

- Workbench 中同一响应把粗体和代码渲染为格式；Telegram 对直接 Telegram 请求的响应也渲染了粗体/代码。
- Telegram 收到的一条跨端响应保留了 `**粗体**` 与反引号字面标记，而同内容在 Workbench 已渲染。需再用唯一标记确认该 Telegram 回复确由 Workbench/TUI 请求产生，并判定标准内容契约要求的显示语义。

#### FC-LIVE-005：Workbench 快速键盘命令批次出现内部拒绝文案（待单条复测）

- 快速输入 `/backend`、`/provider`、`/model` 的一次批次出现两条原文 `command_menu_forbidden`，草稿曾将命令拼接。
- 通过 Workbench 发送按钮单独执行 `/status`、`/backend`、`/provider`、`/model`、`/effort` 均能成功显示状态/菜单。
- 目前不确定错误是 Enter 与命令补全交互、连续输入过快，还是 Connector 逻辑。修复前先用慢速、单条、分别按 Enter 与发送按钮复测。

#### FC-LIVE-006：`/logo` 对单次 Telegram 请求在 Workbench 出现三条终端专用回复

- Workbench 时间线上仅看到一条 `/logo` 输入，随后出现中文、英文、中文三条“终端显示标志”回复；Telegram 端只显示一条回复。
- 记录为重复/额外投递及公共命令效果不适用于外部 Connector 的双重问题，根因待读事件与投递回执后确定。

#### FC-LIVE-007：Telegram 跨端 Markdown 展示差异

- 与 FC-LIVE-004 同一组证据；合并归类，确认是否影响统一显示契约后再定问题范围。

### 设置恢复与测试边界

- Test Agent 的 voice mode 已恢复 OFF；meter、HER、backend/provider/model/effort/privacy 均未选择或修改。
- 未测试麦克风、语音输入；未发起 WhatsApp、HChat、IT 工单或其他外部消息。

### 继续覆盖：帮助、取消与多行

- Telegram `/help` 确认显示 94 个命令，动态“其他”组另列 `anatta, api, bg, compact, delay, hibernate, notify, oll, pswd, queue, rebuild, restart, sleep, telegram, wiki, xiauth`；`/wol` 也出现在帮助中。需与规范目录逐项对表，不能只用总数判错。
- 从 Workbench 发起的 2000 行取消用长任务，由 Telegram `/stop` 停止；Workbench 收到相同的停止结果，状态回到 IDLE、队列 0。未完成请求以 4,453 字节恢复记录保留，标记为“可恢复”，系统提示用 `/compact` 清理/保存该记录。
- 停止后发送新的多行文本时，先看到系统的受限恢复说明，随后新文本仍得到正确答复；第二次 `/stop` 后没有长内容继续生成。是否“自动恢复说明”符合预期，待产品行为核对，不判为回归失败。
- `FC-LIVE-TEXT-C` 的 Workbench 输入成功携带两行；Telegram 收到的 Agent 回复保留换行，Workbench 同一回复显示成一行。补记为明确的跨端换行呈现差异，等待修复批次。
- Agent 生成的 `FC-LIVE-MEDIA-A.png` 通过 FC 到达 Telegram 并显示图像，在 Workbench 显示带缩略图的图像附件卡。TUI 当前无法从可读屏幕输出确认附件展示，列为待确认。

## 再验证记录（2026-09-26 16:00–16:03）

- `/compact` 后 `/status full` 显示 Test 空闲、队列 0、无活动请求。未发现仍运行的停止任务；恢复记录在后续提示中仍可见，是否应由 `/compact` 清除需要结合它的语义另行核对。
- `FC-LIVE-TEXT-D` 从 Telegram 发送后回复一次，中文、尖括号、空行、项目符号、代码与粗体内容均可读。`FC-LIVE-TEXT-E` 从 Workbench 发送同等内容，也回复一次。Workbench 将输入中的换行/列表呈现为一行或列表项；Telegram保留了行间距。语义内容一致，布局呈现差异已复现，保留为 FC-LIVE-007。
- `FC-LIVE-TEXT-F` 从 TUI 发送，包含中文引号、Windows 路径、URL 查询参数、反斜杠、& 和 emoji；Workbench 收到一条相应回复，没有重复或错误链接跳转。
- Workbench 慢速单条命令复测：第一次 Enter 只关闭补全，草稿仍在；第二次 Enter 才提交。`/status` 返回状态卡，`/backend` 返回菜单卡；没有再次出现 `command_menu_forbidden`。先前快输入问题本次未复现，优先记录为交互步骤/速度相关，待修复前不判作代码缺陷。
- Agent 图片测试已在 Telegram 与 Workbench 两端确认。TUI 的附件显示尚不能从 PTY 文本抓取结果独立确认，继续列为未验收。

### 附件与回复关系（2026-09-26 16:08–16:19）

- Agent 从系统临时目录发送文档时被拒绝，界面明确说明“系统不允许访问临时目录中的文件”；这是有效隔离。改在 Agent 专用工作区创建 `FC-LIVE-FILE-C.txt` 后，通过 FC 发送成功。Telegram 显示 34 B 文档卡与正文预览，Workbench 显示 `FC-LIVE-FILE-C.txt` 附件卡和说明。文件测试未写入产品仓库。
- Telegram 原生回复 UI 显示了对 `QREF-7319` 的引用预览；提示 Agent 仅复述被引用原文、无引用则回答“无引用”，Agent 回答“无引用”。WorkBench 时间线也只显示回复文本，没有引用卡片。确认回复关系没有进入标准消息/Agent 上下文，是待修复缺陷（FC-LIVE-008）。
- Telegram 对 `/status` 等只读命令和 `/backend` 菜单正常；Workbench 慢速键盘操作复测通过。未选择菜单项。

## 批量修复与采用复测状态（2026-09-26）

- 回复引用修复：将 Telegram 目标端点的外部消息 ID 绑定到已投递的 Session Event；入站原生回复会携带 typed reply_ref 并加入 Agent 上下文，Workbench 时间线读取同一结构呈现引用卡。跨 Session、端点、Agent 或 隐藏事件的引用会被拒绝。
- 换行修复：Workbench 的 Markdown renderer 保留单换行，不合并普通文本行。
- /logo 修复：共享命令菜单隐藏该命令；所有非 TUI 内建 Connector 注册 connector_local 例外；Telegram 原生入口只返回本地终端限制说明，不会运行终端动画。
- 定向与组合证据：首次红测暴露了 /logo 菜单/入口策略缺失和引用提示字符串语法错误；修复后 HASHI1 相关组合 302 passed / 11 subtests passed，独立命令目录回归 9 passed，Workbench 展示 19/19 passed。Python 编译、中英文目录解析、Protected Core 与 whitespace 检查通过。
- 现场采用待完成：/reboot min testing@HASHI1 因额外 Agent 参数被语法拒绝；随后有效的 /reboot min 被正确阻止，原因是 Functions 源码尚未提交（source_update_incomplete）。Test Worker 继续使用旧代次，没有发生运行态切换；语音输入未测试。
