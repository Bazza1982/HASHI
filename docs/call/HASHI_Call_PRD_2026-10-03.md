<!-- Documentation-only mirror of the approved Word PRD. -->

> **Repository copy / 仓库副本**
> Source: `HASHI_Call_PRD_2026-10-03.docx` (v2.0, 2026-10-03).
> This file preserves the Word document's text and tables in their original order.
> The three approved floating-UI mockups remain embedded in the original Word file;
> they are **not embedded in this text mirror**. The original `.docx` has **not** been
> uploaded to this branch because the current connector cannot transfer the local attachment directly.
> Original Word SHA-256: `30aba900b13ce12eef49dff3f3d25943c785ac5c75753edeb522d72f01341aec`.
> Planned original-file location: `docs/call/HASHI_Call_PRD_2026-10-03.docx`.
> No implementation, deployment, runtime, or `/phone` changes are included in this branch.

---

HASHI /call<br>
产品需求文档（PRD）

Workbench 第二通话路径：可插拔语音与视觉通话<br>
保留 /phone，新增 /call

版本：v2.0<br>
日期：2026-10-03<br>
面向：Barry / HASHI 本地实施与后续 PR

*封面图：/call 语音展开态。新的 /call 与当前 Workbench 聊天布局共存，采用悬浮覆盖而非占领整个聊天窗。*

说明：本文为产品需求文档，包含目标、范围、交互、技术架构、实施分期、验收标准，以及更新后的界面效果图。<br>
本文不修改 /phone 的既有实现；/call 作为独立路径设计与实施。

## 1. 文档概览

本 PRD 以“保留现有 /phone 成功路径，同时新增一条灵活、低成本、可扩展的 /call 路径”为核心原则，形成可直接交给本地 Agent 或人工开发的详细产品与工程说明。

| 版本 | 日期 | 作者/用途 | 备注 |
| --- | --- | --- | --- |
| v1.0 | 2026-10-03 | 初版设计稿 | 建立 /call 的总体设计与架构。 |
| v1.1 | 2026-10-03 | 第一次修订 | 引入与现有 /phone 一致的悬浮/收起思路。 |
| v2.0 | 2026-10-03 | 本次 PRD 整编 | 采用更新后的三张效果图；重写为完整 PRD，细化功能、技术与验收。 |

代码与设计基线：

- Workbench 基线：main 分支 7d94b514e6595fb47eae871dbe9c0cf4b9ca2a1a。

- HASHI 基线：main 分支 6a4b29b881dd3077c1c2302e4bbc6e41b0277bf9。

- 已核实的 Workbench 关键前端组件：LiveCallUI.jsx、liveCall.css、GlobalLiveCallDock.jsx、useLiveCallController.js、liveCallApi.js、App.jsx。

- 已核实的 HASHI 架构说明：ARCHITECTURE.md（PAO、HERV3、Frontend Connectors 的所有权边界）。

## 2. 执行摘要

现有 /phone 已证明：Workbench 内嵌式实时通话在体验上成立，而且悬浮卡片 + 实时字幕 + 收起/返回聊天的整体方向是正确的。问题不是“/phone 不好”，而是它面向 Realtime provider 和双工交互，成本更高、供应商耦合更强、也不适合承载你希望实现的可插拔 STT → 文本 Agent → TTS → 可选视觉快照架构。

因此，本 PRD 定义一条完全独立的新路径：/call。/call 不是对 /phone 的替换，也不是对 /phone 的重写，而是与 /phone 并行存在的第二条通路：

- 保留 /phone 作为 Realtime、可打断、音频优先的高级模式。

- 新增 /call 作为半双工、一人一句、低成本、可插拔、支持可选视觉快照的通话模式。

- 界面继承 现有 /phone 的简洁与熟悉感：页内悬浮、可收起、实时字幕、圆形控制按钮、桌面右侧浮层和移动端全屏方向。

- 技术解耦 /call 不把推理绑定到某个 Realtime provider；STT、视觉观察、主 Agent、TTS 均可以本地或云端替换。

## 3. 背景与问题定义

- 你已经明确决定：不破坏当前已成功的 /phone 路径，因为它虽然贵，但值得保留。

- 与此同时，日常大部分通话其实并不需要 full duplex 打断；一人一句的交互是大多数用户更熟悉、更稳妥、也更便宜的模式。

- 你还希望这条新路径保留 HASHI 最关键的优势：文本 Agent 仍是大脑，语音和视觉只是输入输出层，而不是反过来被某个多模态实时模型接管。

- 因此需要一份既面向产品、又足够工程化的 PRD，指导 Workbench 和 HASHI 两侧如何新增 /call。

## 4. 产品目标

- 提供一条清晰、低成本、可靠的 /call 通话模式。

- 保证 /call 的 UI 与当前 Workbench 与 /phone 的成熟视觉语法一致。

- 使 STT、视觉、TTS 均可插拔，支持本地或云端。

- 使 /call 的文本推理仍通过现有 HASHI Conversation Session / PAO / Engine 进行。

- 提供语音通话与可选“视频/视觉”模式，但首版视觉只依赖本地预览 + 按轮快照，不引入复杂的视频流后端。

## 5. 非目标（V1 不做）

- 不替换 /phone。

- 不要求 /call 首版支持真正连续视频流理解。

- 不要求 /call 首版支持 full duplex 打断。

- 不引入新的独立 Agent 人格、记忆系统或第二套任务系统。

- 不把美颜、滤镜、背景替换、群组视频、多人会议列入首版范围。

## 6. 目标用户与核心场景

首版 /call 不是面向“大众社交视频通话”，而是面向 HASHI 用户与 Agent 的工作型通话。因此，产品优先服务以下场景：

| 用户类型 | 核心需求 | /call 价值 |
| --- | --- | --- |
| 你自己（Barry） | 与 Agent 快速确认任务、进度、日报、研究计划。 | 低成本语音来回确认；可随时开启视觉给 Agent 看屏幕/物品。 |
| 未来 HASHI demo 用户 | 体验“像打电话一样与 Agent 协作”。 | 上手直观；无需理解复杂 CLI 或配置。 |
| 专业工作用户 | 一边保留主界面工作，一边与 Agent 通话。 | 悬浮卡片不占满聊天窗；可边看文档边通话。 |
| 弱设备用户 | 本地能力有限，但仍想获得好体验。 | 前后端 adapter 允许用云 STT/TTS，同时保持主 Agent 和视觉路径灵活。 |

### 核心用户故事（User Stories）

- 作为用户，我希望点一下 /call 就能开始一人一句的语音通话，不必切换到另一个独立页面。

- 作为用户，我希望在通话时仍能看见原来的聊天与右侧信息栏，而不是被一个整页通话界面霸占。

- 作为用户，我希望通话中能看到实时字幕或本轮确认文本，方便在嘈杂环境或跨语言环境下理解。

- 作为用户，我希望在必要时开启相机，让 Agent 看到一个画面快照，但不要求它一直盯着连续视频流。

- 作为管理员/开发者，我希望换 STT、换 TTS、换视觉模型不会重写整套 /call。

- 作为系统，我必须保证 /call 只影响 /call，自身的设置、状态和失败恢复不能破坏 /phone。

## 7. 产品原则与设计原则

- 独立路径原则：/call 是新路径，不复用 /phone 的 Realtime 协议和 provider 假设。

- 熟悉体验原则：虽然架构不同，但界面语言应尽量沿用 /phone 已建立的熟悉感。

- 页内工作优先：通话是协作方式，不应强迫用户离开 Workbench 主工作流。

- 文本 Agent 为核心：推理通过现有 HASHI 文本会话进入，不搞第二套“语音 Agent”。

- 能力驱动配置：不同 STT/TTS/视觉 adapter 的能力不同，UI 只呈现实际支持的配置。

- 诚实降级：不是所有 provider 都能实时字幕、不是所有模型都能逐句流式 TTS，界面不能假装存在。

- 安全与隐私优先：相机、麦克风、云端上传都要显式可见、可关闭、可配置。

## 8. 界面设计（更新版）

以下三张效果图采用你最新确认的方向：/call 不再像“覆盖一大片独占聊天窗口”的大面板，而是明确贴近当前 /phone 的 Workbench 悬浮风格。即：原工作区仍然可见，/call 以浮层卡片或收起条的形式存在。

### 8.1 语音展开态（主参考图）

*图 1｜/call 语音展开态。与当前 Workbench 并存的右侧悬浮卡片；保留聊天区、右栏和顶部工具。*

- 卡片位于主内容区右侧浮层，宽度受控，不覆盖整个中部聊天文档。

- 顶部明确标识“/call”，避免与 /phone 的 Realtime 面板混淆。

- 中部使用头像 + 当前状态 + 时长 + 实时字幕盒子，延续熟悉的通话层级。

- 底部保留 3 个主按钮：静音、结束通话、开启视频。首版最重要的迁移动作就是从语音态一键切换到视觉态。

### 8.2 视觉展开态（视频/视觉模式）

*图 2｜/call 视觉展开态。仍是同一张通话卡，只是中部区域切换为本地相机预览与字幕。*

- 这不是“另一个视频会议应用”，而是同一套 /call 通话卡的视觉模式。

- 主视频区展示本地预览；小窗可以展示“你”或通话对侧的辅助预览，具体显示策略由实现阶段决定。

- 字幕区仍然位于卡片下方，形成“看图 + 看话”的统一交互。

- 底部按钮变为：静音、关闭摄像头、结束通话、返回聊天。

### 8.3 收起/悬浮态

*图 3｜/call 收起态 / 页内悬浮条。用于边工作边保持通话。*

- 收起后应仍保留头像、名称、/call 标识、通话时长与实时字幕状态。

- 应保留至少 3 个核心操作：恢复/展开、静音、挂断。

- 收起条应固定在 Workbench 页面内部，而不是浏览器外部悬浮窗。

- 用户切换会话或继续阅读文档时，/call 仍然可见且可恢复。

## 9. 交互说明

### 9.1 入口与启动

- 在现有聊天头部保留 /phone 图标，同时新增 /call 入口。

- /call 可以放在现有 phone 图标附近或更多菜单中，但必须清晰区分“Realtime /phone”与“半双工 /call”。

- 首次启动 /call 时，需要做轻量预检：麦克风权限、默认 STT/TTS 配置、可选视觉配置。

- 预检通过后进入 calling 状态，页内浮层卡片出现；主聊天页保持不变。

### 9.2 通话生命周期

- 建议状态：idle → preflight → connecting → active(audio) → active(video) → minimized → ending → ended。

- “最小化/恢复”只是 presentation state，不应改变 call session 的逻辑状态。

- 挂断必须是显式操作，不能因为切换聊天列表或切换布局就自动挂断。

### 9.3 一人一句流程

- 用户点击麦克风并说话，或自动开始监听本轮输入。

- 本轮结束后，STT 输出一段文本；这段文本作为正常用户消息进入当前 HASHI 会话。

- 当前 Agent 正常推理、用工具、生成答案。

- 当答案可交付后，TTS 播放；必要时字幕同步显示。

- 播放结束后，界面回到“等待你发言”或“再次说话”。

### 9.4 视觉模式流程

- 视觉默认关闭；用户主动开启视频/视觉模式后，浏览器请求相机权限。

- 本地预览持续显示，但上传给模型的内容不一定是连续视频流。

- 首版建议每轮取一张快照或显式“拍这一张”。

- 图片进入视觉 adapter 后，要么先转换为文字观察，再交给纯文本 Agent；要么在未来由可看图主模型直接消费。

### 9.5 字幕行为

- 若 STT 支持 streaming 结果，可显示“临时字幕 + 最终字幕”。

- 若 STT 仅支持整句结果，则在用户发言时只显示“正在识别/本轮转写中”，不要伪造逐字字幕。

- Agent 回答文本在送入 TTS 后可显示为 AI 字幕；若 TTS 不支持精确词级对齐，则按句段滚动显示。

### 9.6 收起与恢复

- 点击最小化按钮后，通话卡折叠为小型悬浮条。

- 悬浮条保留头像、名称、通话时长、字幕状态，以及挂断/恢复核心控制。

- 点击悬浮条或展开按钮后，恢复到先前的 audio 或 video 展开态。

## 10. 功能需求（Functional Requirements）

| 需求编号 | 详细要求 |
| --- | --- |
| FR-01 /call 路径独立 | 系统必须保留 /phone 现有行为，不得通过修改 /call 需求而回归或破坏 /phone。 |
| FR-02 启动方式 | 系统必须允许用户从 Workbench 中启动 /call，且启动后保留原有 Workbench 工作区。 |
| FR-03 悬浮卡片 | 系统必须以悬浮覆盖卡片方式展示 /call 展开态，不得默认占据整个中央聊天区域。 |
| FR-04 收起条 | 系统必须支持将 /call 收起为页内小型悬浮条，并支持恢复。 |
| FR-05 语音输入 | 系统必须支持采集用户麦克风音频，并交由可插拔 STT adapter 处理。 |
| FR-06 文本推理 | STT 结果必须通过当前 HASHI 会话进入现有 Agent 推理链路；/call 不得旁路或复制 PAO/Engine 逻辑。 |
| FR-07 语音输出 | 系统必须支持将 Agent 回复交由可插拔 TTS adapter 朗读。 |
| FR-08 字幕 | 系统必须在 UI 中提供用户与 Agent 的字幕/文本展示区域。 |
| FR-09 视觉模式 | 系统必须支持可选视觉模式，并允许开启/关闭相机。 |
| FR-10 视觉输入策略 | V1 必须至少支持本地预览 + 按轮快照，不要求连续视频语义流。 |
| FR-11 设置 | 系统必须提供 /call 专属设置，使 STT、视觉、TTS 的 provider/model/voice 可配置。 |
| FR-12 状态与错误 | 系统必须展示连接中、转写中、处理中、朗读中、错误、结束等状态。 |
| FR-13 隐私 | 系统必须在使用麦克风和相机前请求权限，并清楚说明何时上传到云端。 |
| FR-14 历史与上下文 | /call 的消息结果应落入正常会话记录，字幕与通话视图应与该会话保持一致。 |
| FR-15 移动端 | 系统必须在移动端提供与现有 /phone 方向一致的全屏/安全区适配。 |

## 11. 技术架构

### 11.1 架构总览

建议架构遵循你已经确认的思路：固定 Workbench 的设备输入/输出通道，中间媒体与模型能力均可插拔。

- 麦克风 → STT adapter → 用户文本 → HASHI 当前会话 / Agent → 回复文本 → TTS adapter → 播放器。

- 可选相机 → 本地预览 → 每轮快照 → 视觉 adapter（observer 或 native image path）→ 相关观察文本或图像输入。

- 播放器、字幕显示、窗口形态（展开/收起）由 Workbench 统一控制。

- Conversation Session、消息准入、工具权限、取消、记忆、模型绑定仍由 PAO 与当前 Engine 负责。

### 11.2 前端组成（Workbench）

- CallLauncher：负责从聊天头部或菜单触发 /call。

- CallController：/call 的前端状态协调器，独立于 useLiveCallController。

- AudioCapture：采集麦克风、VAD 或手动结束录音。

- CameraPreview：相机预览、前后镜头切换、快照拍摄。

- CaptionPane：展示实时字幕/本轮转写/AI 回复句段。

- PlaybackManager：串行播放 TTS 音频，支持停止播放与完成回调。

- CallCard / MinimizedCallBar：展开态与收起态 UI。

### 11.3 后端组成（HASHI / Frontend Functions）

- Call Coordinator：顺序控制本轮媒体处理，不拥有自己的 Agent 或会话。

- STT Adapter Interface：封装云端或本地 STT。

- Vision Adapter Interface：封装视觉观察或原生图片路径。

- TTS Adapter Interface：封装云端或本地 TTS，返回音频文件或流。

- Call Settings Store：按实例/用户/Agent 保存 /call 的配置。

- Preview Endpoint（可选）：用于音色试听、相机测试等不进入正式会话的预览操作。

### 11.4 推荐的适配器接口

```text
stt.transcribe(audio, config) -> {text, partials?, language?, confidence?}
vision.observe(image, prompt, config) -> {observations, visible_text?, limitations?}
tts.synthesize(text, config) -> {audio_url|audio_bytes, markers?, duration?}
call.runTurn(turnInput) -> orchestrates STT -> Agent -> TTS with optional vision side path
```

## 12. 配置模型

建议 /call 采用独立的 Call Profile 配置对象。其目标不是统一所有供应商的所有参数，而是提供“通用字段 + adapter 自身能力”的结构。

```text
CallProfile
{
  enabled: true,
  stt:    { adapter_id, target_ref, options },
  agent:  { binding: "current_session" },
  vision: { enabled, adapter_id, target_ref, mode: "observer"|"native_image", options },
  tts:    { adapter_id, target_ref, voice_id, style_prompt?, rate?, options },
  ui:     { captions: true, auto_send: true, start_in_audio: true }
}
```

- stt / vision / tts 均支持 provider_id + model_id 或本地 target_ref。

- voice_id 必须保存为稳定 ID，而不是单靠自然语言名字。

- 只有某个 adapter 明确支持的参数才在设置面板显示；不支持的参数不应显示为可用。

- /call settings 只影响 /call，不应改写 /phone 或普通 /voice 功能。

## 13. 数据与状态模型

- call_id：/call 会话 ID。

- call_mode：audio | video。

- presentation：expanded | minimized。

- turn_id：本次语音轮次 ID。

- turn_phase：listening | transcribing | submitting | thinking | speaking | complete | failed。

- caption_rows：字幕行集合，区分 user / assistant / system。

- attachments：当前轮次附带的图片/快照数组。V1 建议每轮最多 1 张。

- call_config_snapshot：本次 /call 开始时生效的配置快照，供审计与恢复使用。

## 14. 详细交互与边界条件

### 14.1 录音与说话结束判定

- 首版默认支持手动结束和静默阈值自动结束两种方式。

- 默认静默阈值建议 800 ms，可在设置中调整。

- 过长发言需要在接近上限时给出提示。

### 14.2 取消与停止

- “停止朗读”与“结束通话”必须是两个不同操作。

- 用户停止朗读时，不应自动结束整个 /call。

- 用户结束通话时，应停止录音、停止播放、关闭相机并回收资源。

### 14.3 切换聊天/切换 Agent

- 如果 /call 与当前 Agent 强绑定，切换到其他 Agent 时必须提醒用户是否结束或继续保持该通话。

- 首版建议一个 Workbench 实例同时只允许一个前景 /call。

### 14.4 视觉权限

- 首次开启视觉模式时才请求相机权限。

- 关闭视觉模式后，应停止 camera track，不只是隐藏预览。

- 若权限被拒绝，应提供明确的恢复提示与系统设置指引。

### 14.5 失败恢复

- STT 失败：允许重新说一次。

- TTS 失败：保留文字答案，允许重试朗读。

- 视觉失败：语音通路应继续可用，并明确提示本轮未获得视觉输入。

- HASHI 推理失败：按正常消息错误路径处理，并在 /call 卡片中可见。

## 15. 非功能需求（NFR）

| 类别 | 要求 |
| --- | --- |
| 性能 | 展开/收起动画流畅；普通桌面浏览器中 UI 响应不应因 /call 卡片而显著卡顿。 |
| 延迟 | 整句模式下，应尽量减少“说完后等待过久”的空白时间；系统应优先减少无意义中间等待。 |
| 可靠性 | 单个 adapter 崩溃不应导致整个 Workbench 崩溃；必须能安全回到文字聊天。 |
| 可观测性 | 记录 call start/end、turn latency、stt latency、tts latency、vision enabled 等关键事件。 |
| 可维护性 | 所有 provider-specific 逻辑应收敛在 adapter 层；UI 不直连供应商私有字段。 |
| 隐私与合规 | 明确本地处理与云端处理边界；禁止在用户选择本地模式时静默回退到云端。 |
| 可访问性 | 键盘可操作、按钮可读、字幕区域可滚动，移动端触控目标足够大。 |

## 16. 实施建议与分期

### Phase 0｜设计与边界冻结

- 冻结 /call 与 /phone 的边界。

- 确定前端组件命名与目录结构。

- 确定配置对象结构与 adapter 接口。

### Phase 1｜语音版 /call 最小可用产品

- 完成 /call 启动、展开卡片、最小化条。

- 打通 STT → 当前 Agent → TTS 的完整链路。

- 实现字幕盒子、错误状态、挂断与停止朗读。

### Phase 2｜视觉模式

- 加入相机预览与权限请求。

- 实现按轮快照与视觉 adapter。

- 完成 audio ↔ video 模式切换。

### Phase 3｜设置与 provider-agnostic 打磨

- 完成 /call settings UI。

- 接入多个 STT/TTS adapter。

- 加入 preview/试听与能力检测。

### Phase 4｜验收与优化

- 桌面与移动端体验打磨。

- 日志与监控。

- 回归测试，确保 /phone 不受影响。

## 17. 验收标准

- AC-01  用户可以在 Workbench 中启动 /call，而不离开聊天布局。

- AC-02  语音展开态显示为右侧悬浮卡片，聊天区与右栏仍然可见。

- AC-03  用户可以将 /call 收起为页内悬浮条，并可恢复。

- AC-04  说一句 → 转写 → 当前 Agent 回答 → TTS 朗读 的链路可完整跑通。

- AC-05  用户可以从语音态切换到视觉态，并看到相机预览。

- AC-06  视觉态不会把整个 Workbench 变成单独的视频会议页。

- AC-07  关闭视觉后，相机权限资源被释放。

- AC-08  更换 STT/TTS 适配器不会要求重写 UI。

- AC-09  /phone 的现有入口与主要体验未被破坏。

## 18. 风险与待决问题

- 是否允许 /call 在同一时刻与 /phone 并存？建议首版不允许。

- 视觉展开态中，大图区域究竟默认显示“对侧/本地”哪一方，需要在实现时再定。当前效果图用于表达布局，而非锁死媒体语义。

- 字幕与消息记录的最终投影策略：是完整同步到聊天消息，还是单独保留为 call transcript，需要进一步确定。

- 本地 TTS / STT 的编解码与浏览器兼容问题可能带来额外工程复杂度。

- 移动端浏览器对相机、后台音频和页面生命周期的限制，需要专项测试。

## 19. 附录：与当前 /phone 的关系

- 当前 /phone 已经是页内通话体验，而不是霸占整个聊天窗口的大整页模式；因此 /call 必须沿这个方向继续前进。

- 本次更新后的三张效果图，已经明确把 /call 调整为“悬浮 Workbench 内部”的设计。

- 你指出的问题非常关键：如果新设计画成“占满中央窗口的一大块通话 UI”，就会背离当前成功的 /phone 经验。本文已据此全面修正。

—— End of PRD ——
