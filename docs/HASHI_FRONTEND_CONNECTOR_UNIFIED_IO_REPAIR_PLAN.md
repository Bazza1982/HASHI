# HASHI Frontend Connector 统一出入口与治理修复计划

Status: **实施中；HASHI1 已完成若干 Functions 纵向切片，但未达到第 20 节完成定义**

Baseline: HASHI1 `main` at `6a36caf5`（2026-09-25）

Functional owner: **Frontend Connector（FC）**

Engineering layer: **Functions**。PAO 继续拥有 Session、Message、Run、Event、权限、命令执行与工具控制；Remote、Exchange 和设备 Worker 保留各自的传输或执行职责。**Protected Core 不变。**

Focused validation for future implementation: 统一契约、现有行为等价、每个 Connector 独立回执、媒体生命周期、安全边界、跨 Connector 现场验收，以及 `scripts/check_protected_core_changes.py` 全程通过。

Related authoritative documents:

- [`HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md`](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md)
- [`HASHI_PAO_SYSTEM_DESIGN.md`](HASHI_PAO_SYSTEM_DESIGN.md)
- [`HASHI_PERSISTENT_MULTI_SESSION_FRONTEND_DESIGN.md`](HASHI_PERSISTENT_MULTI_SESSION_FRONTEND_DESIGN.md)
- [`MULTI_SESSION_FRONTEND_INSERTION_PLAN.md`](MULTI_SESSION_FRONTEND_INSERTION_PLAN.md)
- [`HASHI_COMMAND_UI_STYLE_GUIDE.md`](HASHI_COMMAND_UI_STYLE_GUIDE.md)
- [`FRONTEND_COMMAND_MENUS_V1.md`](FRONTEND_COMMAND_MENUS_V1.md)
- [`HASHI_REMOTE_PROTOCOL_SPEC.md`](HASHI_REMOTE_PROTOCOL_SPEC.md)
- [`HASHI_EXCHANGE_INTEGRATION.md`](HASHI_EXCHANGE_INTEGRATION.md)
- [`HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md`](HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md)
- [`HASHI_FRONTEND_LIVE_ACCEPTANCE.md`](HASHI_FRONTEND_LIVE_ACCEPTANCE.md)

## 1. 决策摘要

HASHI 需要把 FC 从“Telegram 适配器加若干兼容 API”升级为**所有用户可见通信的统一边界**。这里的统一同时包含入口、出口和管理，不只是发送答案：

- Telegram、TUI、Backend API、外部桌面客户端、WhatsApp、未来 Connector 的文本、命令、回调、控制、媒体和确认，都先进入同一套类型化入口契约；
- PAO 先将被接受的输入和输出写成权威的 Session、Message、Run、Event 与媒体引用；
- FC 再把同一份语义事件投影、渲染和投递到一个或多个 Connector；
- Telegram、Workbench-compatible client 或任何未来客户端都不再决定“什么算一条 HASHI 消息”，也不再成为其他前端的隐式中心；
- Remote、Exchange、HChat、浏览器控制、Computer/usecomputer 和工具系统与 FC 有明确交集，但不被 FC 吞并。FC 管用户可见的入口、确认、进度、媒体和出口；它们各自保留网络传输、信任验证或实际执行权；
- Persistent Session/Event API 成为外部交互的首选协议。现有基本 `/api/chat`、transcript、Telegram 工具和旧 Remote 路径先作为兼容适配器保留，再按门禁退役；
- 迁移期间允许双读和影子比较，**禁止双发**。任何一个目的地在同一时刻只能有一个发送者。

“一个源”不应理解为所有前端直接读取同一张数据库表，也不应让 FC 取代 PAO。准确含义是：

> PAO 的权威 Session/Event 状态是唯一语义源；FC 提供一个统一订阅面和一组 Connector 适配器。客户端缓存、JSONL transcript、Telegram message ID、Remote spool 和 Workbench 本地状态都只能是派生状态。

## 2. 调查结论

### 2.1 已经具备的正确基础

当前 `main` 并非没有统一设计，以下能力应直接复用：

- PAO 已有持久化 Session、Message、Run、Event、事件消费者与 ACK；
- Run 在接纳时冻结 `hashi.run-delivery-route`，并能分别记录各 surface 的 delivery outcome；
- Persistent Session API 已支持快照、事件回放、幂等 Run 接纳、附件 stage/upload/commit、原子多附件和有序输出附件；
- 当前消息上下文已区分声明来源、运行时观察来源、Connector evidence、远端身份、relay chain 和 private authorization；
- command catalogue 和 callback owner 已有共享来源，命令卡可作为 presentation-only Session 行保存；
- Telegram polling 已做到 Worker 接纳后才推进 offset，发送侧已有分片、格式化、重试与 failover；
- Exchange 已有经过验证的远端主体、持久 inbox/outbox、去重和 body-free correlation；
- Remote、设备控制和工具系统已有实例身份、授权、租约、审计与能力发现基础；
- 外部 Workbench 当前已经在含附件消息上使用 Persistent Session API，并能展示 Session attachment、命令卡、meter/HER v2 presentation channel 和 live request activity。

这些不是要推翻的旧功能，而是新 FC 的骨架。

### 2.2 根本性错位

调查确认，当前问题不是单一 UI 回归，而是历史上形成了多条平行管线：

1. **Telegram 仍是部分运行时输出的隐式中心。** 普通回复、后台通知、meter cost tail、HER v2 card 等路径仍直接调用 Telegram 发送函数；meter 还受 `deliver_to_telegram` 条件控制，并在 Telegram 发送后才补记 presentation message。于是 Telegram 可见并不保证 Workbench/TUI 可见，反之亦然。
2. **持久 delivery outbox 尚未成为发送中枢。** SessionStore 会写 `delivery_outbox`，但当前主发送链仍在运行时内联完成；该表没有形成统一的 claim、dispatch、retry、receipt 消费循环。
3. **Workbench 输入是双轨的。** 当前纯文本仍走兼容 `/api/chat`，文件消息才走 Persistent Session API；两条路径的 source、idempotency、session addressing、错误和投递行为并不完全相同。
4. **TUI 仍有独立策略。** 它有单独的 `hashi.frontend-delivery` 策略和本地 Telegram mirror 开关，并通过兼容绑定共享 `workbench/default` Session。正确体验应保留，但意图、作用域和状态所有者需要类型化，而不是靠前端名称分支。
5. **命令入口不统一。** Telegram slash/callback、`/api/chat`、admin command、agent command 和命令卡 transport 都能进入命令系统，但接纳、幂等、身份、作用域和回执模型不同。
6. **前端展示由多个源拼接。** 基本 transcript projection 合并 JSONL 和 Session Message；live activity 又来自独立的内存 store。Workbench 还需在客户端合并 transcript、activity 和直接响应。最终消息、临时进度与卡片没有一个统一的顺序协议。
7. **来源身份仍含历史推断。** `source`、`chat_id`、`session_surface` 和名称前缀仍参与来源判断。Connector evidence 已存在，但只覆盖部分 Remote/Workbench hop，尚未成为所有入口的标准证明。
8. **媒体和文件存在多个岛。** Session attachments、audio assets、TUI base64/workzone upload、Telegram file tool、Remote spool、Exchange body retention 和设备截图各自有入口与保管规则。
9. **工具出口绕过 FC。** `telegram_send`、`telegram_send_file` 直接调用 Telegram；`frontend_send_attachments` 又明确排除 Telegram/TUI。其实现接受 `access_root`，但没有以该参数完成最终路径权限门禁，与架构文档中“仅授权 Workzone”表述不一致。
10. **HChat/Remote/Exchange 的类型信息会退化。** 已验证的 Exchange delivery 最终被渲染为 prompt 再 `enqueue_api_text`；旧 Remote 仍有 `/api/chat` 和 transcript-based reply correlation；`hchat_send` 同时维护 Backend API、直接协议、Remote 和 Exchange 路由。
11. **传输状态和语义状态容易混用。** `queued`、`sent`、`delivered`、`reply_sent`、前端已显示和用户已读并非同一事实，但不同路径使用的词汇和回执强度不一致。
12. **Connector 能力不是一个完整注册表。** 目前有若干 capabilities、surface 名和特殊分支，但缺少统一的协议版本、内容能力、大小限制、render 能力、delivery 语义、健康和保留策略描述。

这正是 meter card 在 Telegram 有、Workbench 无的同类根因：语义事件没有先成为统一源，前端看到什么仍取决于哪条传输路径先执行。

## 3. 不可破坏的架构边界

### 3.1 Protected Core

- 本计划不授权 Core major-version migration；任何 `CORE_SOURCE_PATHS` 变化都属于越界。
- 新契约、注册表、dispatcher、媒体协调和兼容 adapter 都在 Functions 或平台/实例配置中实现。
- Core 不导入 FC、Telegram、Workbench、Remote product module 或媒体实现。
- 若需要共享常驻服务，它必须是可替换 Function process；常规采用通过 `/reboot`，不得以 Core cold restart 作为测试捷径。

### 3.2 PAO 与 FC 的分工

| 责任 | 权威 owner |
|---|---|
| Session、Message、Run、Event、顺序、幂等、fencing | PAO |
| 当前消息来源事实、权限决定、命令业务执行、工具授权 | PAO |
| 发送路线在接纳时冻结 | PAO |
| Connector 注册、能力协商、入口规范化、目标 renderer、发送与 transport receipt | FC |
| 媒体字节的权威引用、绑定关系和保留决定 | PAO/共享媒体 Function |
| 媒体上传、下载、外部 file ID 和短期 transport cache | FC adapter |
| Remote/Exchange 的认证、网络重试、peer 路由 | Remote/Exchange transport |
| 浏览器/Computer 实际操作、设备租约和能力 | Capability Broker/对应 Worker |
| PCM 中展示当前消息事实 | PCM；只消费 PAO 冻结快照 |
| 模型输出语义与 HER stream event | Engine/HER；不得直接选 Telegram 或其他前端 |
| 草稿、布局、窗口、未发送本地状态 | 外部客户端 |

FC 是统一边界和投递控制面，但不是新的业务数据库，也不是权限绕行层。

## 4. 目标数据流

```text
Telegram / TUI / Backend API / external client / WhatsApp / Remote / Exchange
                                |
                      Connector edge adapter
                                |
                     Frontend Ingress Envelope
                                v
                 FC admission + capability gate
                                |
                     PAO accept / reject / dedupe
                                |
        Session + Message + Run + Event + Media references
                                |
               canonical event feed + durable outbox
                                |
                   FC route and dispatch coordinator
                    /          |          |          \
             Telegram        TUI/API    client    Remote/Exchange
               adapter       adapter     adapter       adapter
                    \          |          |          /
                  per-destination typed receipts
                                |
                              PAO
```

工具和设备控制从侧面接入：用户确认与控制指令经统一入口进入；工具进度、截图、下载文件和生成物经 Event/Media 契约进入统一出口。实际浏览器或桌面动作仍由原 Worker 执行。

## 5. 统一协议族

计划采用一个逻辑版本族 `hashi.frontend.* v2`。各对象独立版本化，避免为了增加一种卡片而整体升级协议。

### 5.1 ConnectorDescriptor

每个 Connector endpoint 注册以下事实：

- 稳定的 `connector_id`、`endpoint_id`、类型和实例；
- ingress/egress 协议版本；
- 认证方式、已验证主体类型、信任域和允许的 target scope；
- 支持的 text、command、callback、control、approval、audio、image、video、document 和 ordered media group；
- 是否支持 edit、delete、buttons、streaming、event ACK、delivery receipt、read receipt；
- 单文件、单组、单消息和速率限制；
- renderer 能力，如 plain text、Markdown、escaped HTML、structured card；
- 在线、degraded、offline、draining 状态和 generation；
- Connector cache 和 transport spool 的保留策略；
- locale 与无障碍能力。

能力由服务端注册和验证，不接受浏览器自报后直接提升权限。客户端可以请求 capability，不能声明自己拥有 capability。

### 5.2 FrontendIngressEnvelope

所有外部输入先规范化为一个 envelope。最低字段为：

| 字段组 | 内容 |
|---|---|
| schema | type、version、created_at、expires_at |
| identity | ingress_id、idempotency_key、connector_id、endpoint_id、transport message id |
| source | 声明主体、已验证主体、assurance、network authentication、relay chain |
| target | instance、Agent、owner、Session 或新 Session 意图 |
| intent | `message`、`command`、`callback`、`control`、`approval`、`ack` |
| content | 有序 typed content blocks；不包含本机绝对路径 |
| context | reply_to、thread、locale、client generation、context generation |
| route | primary/mirror 请求；只是请求，最终路线由 PAO 冻结 |
| authorization | 可验证 proof/reference，不传明文凭据 |
| evidence | Connector 签名/绑定摘要、大小和内容 digest |

入口规则：

- Adapter 只解析传输格式，不执行业务命令；
- Connector 名、display name、chat ID、文件名和文本都不能形成授权；
- 同一 `ingress_id + endpoint_id` 重放必须得到同一接纳结果；相同 key 不同 digest 必须冲突；
- 过期、错实例、错 Agent、错 Session、签名不符、relay 超限或能力不支持时 fail closed；
- PAO 返回 `accepted`、`duplicate`、`rejected` 或 `conflict` AdmissionReceipt，并明确是否创建 Message/Run；
- 只有 durable acceptance 后，Telegram 才推进 update offset，Remote/Exchange 才确认接纳，客户端才清除已发送草稿。

### 5.3 FrontendEventEnvelope

所有用户可见输出先成为语义事件，再做 Connector 渲染。事件至少包含：

- `event_id`、Session sequence、Run/request correlation 和 schema version；
- audience、visibility、sensitivity、durability 与 retention class；
- semantic kind 和 typed payload；
- presentation channel 与允许的 fallback；
- immutable content refs / MediaGroup refs；
- frozen DeliveryIntent reference；
- replaces/supersedes/reply_to 关系；
- created/committed time，绝不使用客户端本地时间决定顺序。

事件分两条保留等级，但通过同一个订阅面呈现：

1. **Durable lane**：用户/助手 Message、最终答案、错误、命令卡、审批、媒体组、路线、投递回执、Run terminal state。必须可 snapshot + replay。
2. **Ephemeral lane**：thinking delta、细粒度 tool progress、answer preview 等。允许有界保留和重启丢失，但 envelope 必须标明 `durability=ephemeral`、replay window 和 replacement point；它不能取代 durable final event。

客户端不再自行猜测 JSONL、Session 行和 activity 谁更权威。服务端统一投影给出顺序、替换关系和 replay completeness。

### 5.4 DeliveryIntent 与 DeliveryReceipt

`DeliveryIntent` 在 Run 接纳时冻结：

- primary destination 和零到多个 mirror；
- 每个 destination 使用 `connector_id + endpoint_id + channel/thread` 作为身份，而不是仅用 surface；
- delivery class、locale、format fallback、大小策略和是否允许 edit；
- 不可因后来切换前端或重连而偷换目的地；
- mirror 之间完全独立，一个失败不能覆盖另一个的成功。

每个 destination 单独经历：

`planned -> queued -> attempting -> accepted/delivered/failed/expired/cancelled/suppressed`

词义必须固定：

- `accepted`：下游 Connector/peer 已接纳，不代表用户看到；
- `delivered`：目标 transport 给出本协议定义的成功回执；
- `rendered` 或 `read`：只有目标真的提供该证明时才可记录；
- 网络超时且副作用未知是 `unknown`，不得自动宣称失败或盲重试；
- 任何用户文案必须从实际 receipt 生成，不能由模型猜测。

### 5.5 MediaAsset 与 MediaGroup

媒体统一为两层：

- `MediaAsset`：不透明 asset id、digest、字节数、MIME、modality、owner、provenance、安全状态和 retention；
- `MediaGroup`：有序 asset refs、caption、semantic role、group id、idempotency key 和原子绑定状态。

一个附件、相册、语音加文字、工具生成的多文件报告、Remote 文件组都走同一模型。Connector 的外部 file ID、Telegram message ID、临时 URL 或 spool path 只放在 transport receipt/cache，不能成为权威媒体身份。

### 5.6 CommandInvocation 与 ControlAction

所有 slash command、按钮 callback、TUI action、Workbench command card、approval response 和 cancel/stop 控制使用类型化 invocation：

- command id 来自共享 `COMMAND_SPECS`/有效 runtime registration；
- 参数、actor、Session、context generation、issued action id、revision 和 request id 均有 fence；
- label 和显示文本不可反解析为命令；
- mutation 在执行前先持久记录 pending/consumed 状态；不确定结果不自动重试；
- Connector 只能表达其 capability 允许的 action，不能通过 admin endpoint 获得额外权限；
- `/telegram` 等旧入口保留为兼容别名，内部转换为通用 delivery preference 操作，并明确 `owner_default`、`session` 或 `next_run` 作用域。

### 5.7 RelayEnvelope

HChat、Remote 和 Exchange 携带用户/Agent 可见消息时使用 typed relay：

- 保留 origin message id、conversation id、sender/recipient、authenticated peer、origin instance、relay chain、TTL、authorization expiry 和 content refs；
- 接收端验证后直接构造 FrontendIngressEnvelope，不先渲染成可执行 prompt；
- human-readable envelope 仅是展示投影，永远不是身份或命令来源；
- reply 通过 correlation/event 回路返回，不再依赖 transcript 文本轮询；
- Remote/Exchange 继续拥有网络认证、离线队列和重试，FC/PAO 拥有本地接纳、Session 与最终 delivery receipt。

### 5.8 ToolInteractionEnvelope

浏览器、Computer/usecomputer 和普通工具不是 Frontend Connector，但它们的用户交互面采用同一契约：

- tool permission、实际调用和设备租约仍由 PAO/Tool Registry/Capability Broker 决定；
- approval request、progress、handoff、result 和 error 投影为 FrontendEvent；
- screenshot、download、录音、生成图片和报告登记为 MediaAsset/MediaGroup；
- 从前端回来的允许/拒绝/取消是 ControlAction；
- Worker 不直接向 Telegram 或 Workbench 发消息，也不持有 Connector credential；
- FC 失败不能改变工具实际是否执行，二者分别记录，避免“界面没显示”被误报为“工具没运行”。

## 6. 内容、展示与订阅规范

### 6.1 语义先于样式

标准事件不得以 Telegram HTML、某个 React component 或终端 ANSI 作为唯一内容。每类事件包含语义 payload 和有限 presentation hints，由目标 adapter 决定最终形态：

| 语义类型 | 建议 channel | 结构化能力 | 必须有的 fallback |
|---|---|---|---|
| 助手最终答复 | `final` | text + attachments | plain/Markdown text |
| 必要进度说明 | `commentary` | incremental block | plain text |
| 可选推理展示 | `reasoning` | bounded stream block | 可完全隐藏 |
| 技术详情 | `technical` | expandable block | 简短摘要 |
| 成本与耗时 | `meter` | metrics card | 可读文本 |
| HER 路由/阶段摘要 | `herv2` | strategy/status card | 可读文本 |
| 命令交互 | `command` | revisioned card/buttons | 编号选项或只读文本 |
| 审批 | `approval` | typed actions | 明确允许/拒绝文本 |
| 系统/错误/交付状态 | `status` | severity/status fields | plain text |

Renderer 必须：

- 使用目标 locale 和共享词条，不在业务层复制文案；
- 对 HTML/Markdown/URL 做对应 surface 的 escaping 和 allowlist；
- 保留 message/event identity，使 edit/upsert/delete 不生成重复消息；
- 超过 transport 限制时做确定性分片，并把所有片段归属于同一 delivery attempt；
- 在不支持按钮或 edit 时降级为安全、可理解的文本，不猜测操作成功；
- 不把 presentation-only card 注入模型历史。

meter 修复的验收例就是：Run 结束时先提交一个 `meter` durable presentation event；Telegram adapter、Workbench/TUI 订阅者各自消费。Telegram 是否在线不再决定事件是否存在，关闭某个 Connector 也不会删除其他前端应看到的卡。

### 6.2 单一订阅面

Persistent Session API 下一版本提供统一 snapshot + event feed：

- snapshot 给出当前 Session、可见 Message/card、attachment refs、active Run 和最新 durable sequence；
- event feed 同时返回 durable 与 ephemeral envelope，并明确各自 cursor；
- durable consumer 使用 ACK，断线后从已确认位置恢复；
- ephemeral cursor 超出窗口时返回 `replay_incomplete`，客户端回到 snapshot，而不是伪造丢失内容；
- final event 明确替换 answer preview，terminal Run 明确结束 activity；
- 服务端完成 JSONL legacy rows 与 canonical Message 的兼容投影，客户端不再维护来源优先级算法；
- 一个 client 可以订阅多个 Session，但每个事件仍保留 instance、Agent、owner、Session 边界。

基本 transcript/poll 路由在迁移期继续存在，内容由统一投影派生；不得再新增只对该路由可见的新功能。

## 7. 媒体、文件、分组与保管周期

### 7.1 入口流程

统一入口为：

`declare -> stage -> upload/stream -> verify -> commit -> atomically bind group to Message -> admit Run`

约束：

- stage 只预留身份，不代表内容已接纳；
- commit 校验 size、digest、MIME、配额和 owner/Session；
- 多附件必须整组成功或整组失败，顺序不能因并行上传改变；
- caption、semantic role 和 reply context 属于 MediaGroup/Message，不写进文件名；
- Connector 只能提交不透明 asset id，不能把本机路径当成跨机引用；
- voice 仍可先转录，但原音频、transcript 和 Run 必须有明确 correlation；
- Telegram album、Remote file batch、工具一次发布多文件都映射到同一 ordered group。

### 7.2 出口流程

统一出口为：

`tool/runtime produces bytes -> MediaAsset commit -> MediaGroup bind -> assistant Message/Event commit -> per-Connector fetch/render/send -> receipt`

`frontend_send_attachments` 最终应成为“发布到当前 Run 冻结路线”的通用工具，不再排除 Telegram 或 TUI。为了保留明确的额外通知能力：

- `telegram_send` / `telegram_send_file` 可继续作为显式目的地工具；
- 其实现改为创建 destination-scoped FC delivery，而不是直接调用 Bot API；
- 它们不会自动成为当前答复的第二份副本；
- 工具回执包含 asset/group/delivery id 和实际 transport state；
- 旧名称保留兼容期，文档明确“显式额外通知”与“当前前端答复”的差别。

所有本地文件发布必须经过真实 authority check。`access_root`/Workzone/明确授权资源是门禁输入，不只是函数参数；symlink、junction、UNC、大小变化和 digest 变化都需 fail closed。

### 7.3 保管类别

初始策略以“不缩短目前正确可见内容寿命”为原则：

| 类别 | 初始建议 | 说明 |
|---|---|---|
| `staging_unbound` | 1 小时 | 与当前默认临时媒体 TTL 对齐；未绑定自动清理 |
| `message_bound` | 跟随 Message/Session；当前无删除策略时视为长期 | 已显示的音频继续保持现有长期语义，推广到所有可见附件 |
| `delivery_spool` | 至所有目标进入 terminal state，再保留 24 小时 | 支持恢复与核对；若现有 Remote/Exchange 合同更长则取更长值 |
| `preview_cache` | 最多 1 小时，可重新拉取 | Connector 派生缓存，不是权威副本 |
| `quarantine` | 配置化，默认 24 小时 | 拒绝或待检查字节，与正常 Session 内容隔离 |
| `correlation_marker` | 30 天、无消息正文 | 保留 Exchange 现有 body-free 去重/关联能力 |
| `audit_metadata` | 遵循实例审计策略 | 只保留 digest、大小、状态与关联，不默认保留敏感内容 |

实施前要把这些值放入一个带 revision 的实例配置 owner，不散落常量。迁移不能把当前 message-bound 音频降回一小时。显式删除 Session/Message 时，PAO 生成 deletion/tombstone event；FC 清理各 connector cache 和外部可撤回对象，但外部平台无法删除时要记录事实，不能假装完成。

## 8. 各入口的目标行为

### 8.1 Telegram

- poller 保留当前“Worker durable accept 后推进 offset”的正确边界；
- Update 先转换成 IngressEnvelope，Bot API object 不进入 PAO 业务层；
- text、media、album、voice、slash、callback、reply 和 edit 使用同一入口 ID/证据模型；
- Telegram user/chat/message ID 是 transport evidence，不单独授予 HASHI 权限；
- 回调只回传 issued action id 和 revision，不能把 label 当命令；
- Bot 发送、分片、parse mode、rate limit 与 failover 移入 Telegram adapter；
- 发送成功后写该 destination 的 receipt，不再控制 canonical event 是否创建。

### 8.2 TUI

- TUI 迁移到 Session/Event API，显式持有 Session id 和 consumer cursor；
- 当前草稿、历史、附件、mirror 开关和本地偏好继续保留；
- `workbench/default` 兼容绑定在过渡期保留，但不再是 TUI 的隐式身份；
- TUI 的 Telegram mirror 解释为明确 scope 的 DeliveryPreference，不与 owner default 混为一谈；
- TUI 不再有专用附件发送语义，使用同一个 MediaGroup admission；
- 断线恢复靠 snapshot + ACK，不靠读取另一个前端的 transcript 文件。

### 8.3 Backend API 与外部桌面客户端

- 所有文本和附件统一使用 Session `runs` 接纳，不再“文本走 `/api/chat`、文件走 v1”；
- Server-side proxy 继续保管 HASHI credential，浏览器不接触 HMAC/shared token；
- connection、instance、Agent、owner、Session 形成复合身份，不能只靠 agent name；
- Workbench 的 drafts、layout、pins、unread/read cursor 等客户端状态继续归客户端；
- transcript、history、request activity、command cards、attachments 改为消费统一 snapshot/feed；
- Safe Voice、搜索、Canvas 等功能只能通过已声明 capability 接入，不新增隐藏的 `/api/chat` 旁路；
- 外部产品名称不进入 HASHI 的共享契约，Workbench 作为一个 conforming client 适配该协议。

### 8.4 WhatsApp 与未来 Connector

新增 Connector 只能通过注册表和标准 adapter 加入，不能在 runtime pipeline 新增 `if surface == ...` 业务分支。最低资格门禁包括：认证、幂等入口、text fallback、delivery receipt 定义、大小限制、错误映射、secret redaction、离线/恢复测试和 capability negotiation。

若目标不支持某内容类型，FC 只能按注册的安全 fallback 转换，或返回 `capability_unsupported`；不得静默丢弃卡片、附件或控制动作。

### 8.5 Scheduler、background 与 proactive 输出

这些不是人类 Connector 入口，而是 HASHI internal source。它们仍必须创建明确的 DeliveryIntent，并通过 FC 出口发送。内部任务不得因为持有 Telegram chat id 就被误判为 Telegram 用户输入；失败重试遵循任务与 delivery 两套独立状态。

## 9. HChat、Remote 与 Exchange 的交集

### 9.1 保留的职责

- HChat/PAO：Agent 间消息语义、Conversation/Run、回复关系；
- Remote：实例发现、点对点认证、协议 transport、受限代理；
- Exchange：跨网络身份、WSS、durable inbox/outbox、离线恢复与准确重试；
- FC：把这些 transport 携带的可见消息接入统一入口，并把 reply/status/media 从统一出口送回。

### 9.2 必须消除的旁路

- 不再把已验证 delivery 先拼成 display prompt，再靠文本恢复身份；
- 不再以目标 transcript 出现某段文本作为 reply delivery 的权威证明；
- 不再由 `hchat_send` 自行在 Backend API、direct protocol、Remote 和 Exchange 之间复制业务规则；
- 不再默认 `deliver_to_telegram=True` 来保证远端消息“有人看见”；是否镜像 Telegram 由冻结路线决定；
- Remote file transfer 不再产生一套独立可见附件身份，完成后绑定到 MediaAsset/MediaGroup。

### 9.3 兼容策略

旧 `agent_message`、`agent_reply`、Remote v2 和 `/api/chat` 接收器先转换为 RelayEnvelope，再进入统一 gateway。旧 sender 仍能工作；新 receiver 不从正文执行命令。只有双方 capability 都声明 typed relay 后，才切换新发送协议。Exchange 现有 inbox、去重与 30 天 body-free marker 保留，不因 FC 迁移降低可靠性。

## 10. 命令、审批与控制管理

建立一个 Functions 层 `Command Admission Service`，复用现有 command registry、callback owner 和权限判断：

1. Connector adapter 识别 transport 级 slash/callback/control；
2. 生成 CommandInvocation/ControlAction；
3. 服务验证 actor、scope、issued action、revision、Session generation 与 idempotency；
4. 原有命令 handler 执行一次；
5. 结果生成 durable command event/card；
6. 各 Connector 渲染按钮或安全文本 fallback；
7. delivery receipt 与 command result 分开保存。

必须保留当前命令目录、动态有效命令、locale、Back/Refresh、确认卡、15 分钟 menu lifetime 与 stale revision fail-closed。需要改变的是入口和回执一致性，不是重写命令业务。

生命周期操作继续遵循原 scope：`/reboot` Agent-scoped，共享 replacement 或 `/restart` 需要其既有授权。FC 不能因为某客户端有 admin HTTP route 就扩大权限。

## 11. 安全与隐私规范

### 11.1 信任

- `source`、`surface`、display name、文件名、文本头和客户端 ID 都是输入，不是证明；
- 内建 Connector 使用运行时观察或短期签名 evidence；远端使用 peer principal 和 relay proof；
- evidence 绑定 content digest、目标、时间窗和协议版本，防止换正文重放；
- 保留 `declared`、`connector_asserted`、`runtime_observed`、`exchange_verified` 等 assurance，不把低保证来源升级；
- 任意客户端都不能提交 PAO 冻结字段、private authorization results 或 delivery route；只能提交可验证 proof/request。

### 11.2 内容与媒体

- 限制解压后大小、数量、总量、MIME 和 duration；不信任扩展名；
- 文件服务只返回已绑定且当前 owner/Session 可见的 asset；
- 不在事件、URL、日志或远端 envelope 暴露本机路径；
- HTML、Markdown、URL、callback data 按目标 renderer 处理；
- screenshot、voice、browser page 和 tool output 默认视为敏感内容，普通日志只记录 metadata/digest；
- Connector 临时 URL 有短 TTL、单一 audience 和防重放绑定；
- 删除、过期和 quarantine 生成审计事件，但不保留被删正文作为“证据”。

### 11.3 可靠性与滥用防护

- ingress、delivery、media group、command action 分别有稳定幂等键；
- 每个 endpoint 有 rate、并发、队列字节和重试预算；
- backpressure 先拒绝新低优先级工作，不丢已提交 durable event；
- poison event 进入隔离队列并报警，不能阻塞整个 Connector；
- retry 只在协议证明安全时执行；unknown side effect 需要查询或人工处理；
- Connector self-error 不得递归生成无限通知；
- 多实例 endpoint 必须绑定 instance identity，健康的其他 HASHI 实例不能代替目标。

## 12. 现有正确功能保留清单

施工前把下表转成 characterization tests 和 live acceptance items。没有对应证据的行为不得直接声称“已保留”。

| 能力 | 必须保留的事实 | 新架构中的位置 |
|---|---|---|
| Telegram polling | durable accept 后才推进 offset；重复 Update 不重复 Run | Telegram ingress adapter + PAO idempotency |
| Telegram 文本 | 长消息分片、format fallback、rate limit、failover、错误可见 | Telegram renderer/transport adapter |
| Telegram media/voice | 图片、文件、视频、音频、caption、voice transcript 与回复 | MediaGroup + Telegram adapter |
| Telegram commands | 当前有效命令、callback、菜单、locale、确认与 lifecycle scope | Command Admission + renderer |
| TUI chat | 发送、取消、历史、状态、附件和自然完成 | Session/Event client |
| TUI mirror | 用户可选择是否 Telegram 镜像，重启后偏好正确 | scoped DeliveryPreference |
| Backend API | 现有认证、Agent 路由、错误码与兼容请求 | compatibility ingress adapter |
| Persistent Session API | snapshot、events、ACK、fencing、幂等、附件原子接纳 | 新标准协议的基础 |
| Workbench chat | 文本、附件、语音、进度、历史、搜索、命令卡 | conforming external client |
| Workbench 本地体验 | 草稿、未读、pins、布局、连接状态不被 HASHI 接管 | client-owned state |
| command UI | Session-backed card、revision、TTL、issued action、safe URL | durable command event |
| meter/HER card | 配置开启时所有目标前端可见，关闭时都不出现 | semantic presentation event |
| live activity | commentary/reasoning/technical/answer preview 的开关与有界流 | unified ephemeral lane |
| Session 隔离 | owner、Agent、Session、generation、history/fresh boundaries | PAO unchanged |
| 当前消息上下文 | source assurance、output destination、private authorization、relay facts | PAO snapshot from ingress evidence |
| 多附件 | 16 件和当前 byte limits、原子接纳、原序输出、重复工具调用幂等 | MediaAsset/MediaGroup |
| 音频寿命 | 已绑定助手 Message 的音频不被临时 TTL 清掉 | message-bound retention |
| HChat local | 目标解析、payload、queued/failed 真实状态、回复关系 | RelayEnvelope + local adapter |
| Remote | discovery、认证、wrong-instance 拒绝、去重、离线恢复 | Remote transport + FC bridge |
| Exchange | verified principal、durable inbox/outbox、publish fence、30 天 marker | Exchange transport + FC bridge |
| private authorization | scope/resource/expiry 在接收端重新验证，正文不能授权 | PAO/security unchanged |
| Scheduler/background | 原 task 状态、自然完成、取消、通知目标与错误 | internal source + FC egress |
| Tool security | allowlist、Tier、审批、side-effect 状态、审计 | PAO/Tool owner unchanged |
| Browser/Computer | capability discovery、instance identity、lease、handoff、无弹窗 | Capability Broker unchanged；FC 只投影 |
| 故障边界 | 一个 Connector 离线不拖垮 Run 或其他 Connector | per-destination dispatcher |
| locale/accessibility | 当前 UI locale、plain fallback、可读状态 | renderer catalogue |

额外回归约束：

- 不重复写模型历史，不把 card/progress 伪装成用户或助手语义消息；
- 不因前端刷新、poll race 或 reconnect 丢失 durable card/attachment；
- 不因新 FC 让 Telegram 成为 Workbench 的隐式 mirror，或反向自动发送；
- 不把 `accepted` 报成 `delivered`，不把 transport failure 报成 Run failure；
- 不把现有的 client draft/read/unread 状态迁入 HASHI 后端；
- 不把 external Workbench 私有产品模型写入 HASHI 共享 architecture。

## 13. 迁移总原则

1. **先冻结行为，再改路径。** 先补红色 characterization tests 和跨前端行为表。
2. **契约先行。** 先加入 schema、validator、capability registry 和事件投影，不改变生产 route。
3. **双读可以，双发禁止。** shadow dispatcher 只能比较，不得调用外部 API。
4. **一端一开关。** cutover key 至少包含 instance、Agent、connector endpoint；不能用全局大开关一次迁移全部。
5. **权威写入只保留一个。** compatibility adapter 只能调用 canonical service，不得双写 Session/JSONL。
6. **回滚不倒放副作用。** 回到 legacy sender 时，不自动重发 canonical outbox 中状态不明的 delivery。
7. **路线与内容解耦。** 内容事件提交后才排队投递；改变 connector 健康不会改写 Message。
8. **先 presentation canary，再 final，再 media。** 用 meter/HER/card 验证统一出口，降低重复最终答复风险。
9. **每阶段保持 Core hash 不变。** 任何需要 Core 变化的设计立即退回重构。
10. **旧 API 有明确 sunset gate。** 未达到使用率、错误率和现场验收门禁前不删除。

每个 Connector endpoint 使用迁移状态：

`legacy -> shadow -> canonical_canary -> canonical -> legacy_disabled`

- `shadow` 只生成预期 route/render/receipt，不发送；
- `canonical_canary` 只承载明确 allowlist 的 event kind 或测试 Agent；
- 切换前写 generation fence，防止 legacy 与 canonical Worker 同时认为自己是 sender；
- rollback 需要记录原因、最后 delivery id 和 unknown attempts；
- deployment、adoption 与 live verification 分别记录。

## 14. 分阶段实施计划

### Phase 0 — 行为基线与边界清单

目标：在任何实现前，把“目前正确”变成可执行证据。

工作：

- 枚举所有 ingress：Telegram update、TUI、`/api/chat`、Session API、admin/agent command、voice、HChat、Remote、Exchange、scheduler/internal；
- 枚举所有 egress：final、error、background、meter、HER card、command card、approval、tool progress、attachment、voice、explicit notification；
- 生成 source-to-sink 矩阵，标注 authority、idempotency、storage、retention、receipt strength 和 bypass；
- 给 meter-only-on-Telegram、纯文本/附件双轨、TUI mirror scope、Remote reply polling、attachment access root 写失败基线；
- 记录 HASHI1 `main`、运行 generation、external client version 和当前 API capability；
- 把 live acceptance manifest 扩展草案与现有行为一一对应。

验收：每个已知入口/出口都有 owner、当前路径和测试；未分类旁路阻止 Phase 1。此阶段无生产行为变化。

### Phase 1 — v2 契约与 Connector Registry

目标：加入统一类型系统和 capability truth，不切换任何发送。

建议 Functions 组件：

- `frontend_contracts`：Ingress/Event/Delivery/Media/Command/Relay schema 与 validator；
- `frontend_connector_registry`：endpoint registration、capability、health、generation；
- `frontend_projection`：从现有 Session/Event 和 activity 构造统一 feed；
- 现有 `frontend_delivery`、`message_context`、command/menu contracts 作为输入，不复制常量。

工作：

- 发布 `/api/v2/frontend/capabilities` 和 schema version；
- 建立 legacy source/surface 到 descriptor 的只读映射；
- 为所有 event 定义 semantic kind、durability、fallback 与 sensitivity；
- 定义 receipt vocabulary 和 transport-specific proof；
- 加 schema golden fixtures、forward-compatible unknown-field tests、wrong-version tests；
- 注册表配置复用 revisioned configuration writer，不直接编辑分散 JSON。

验收：现有 runtime 输出不变；registry snapshot 可完整描述所有已启用 Connector；Core guard、契约测试和配置冲突测试通过。

回滚：移除/关闭 v2 capability 暴露即可，无外部副作用。

### Phase 2 — 统一入口与命令接纳

目标：所有入口在进入 PAO 前都形成 IngressEnvelope，但现有 API 继续可用。

工作：

- 建立 FC admission gateway，统一 normalize、verify、dedupe、target resolve、media bind、PAO accept；
- Telegram Update、TUI、`/api/chat`、Session API、command endpoints 先做 adapter；
- command/callback/control 全部进入 Command Admission Service；
- MessageContext 只从已验证 envelope 构造，逐步停止 chat_id/字符串前缀推断；
- `/api/chat` 返回 compatibility receipt，同时内部走 canonical admission；
- Workbench 纯文本改用 Session Runs API，附件路径保持相同入口；
- TUI 使用显式 Session/consumer，保留兼容 binding migration；
- 加入 shadow comparison：legacy normalization 与 canonical result 不同即报警，不自动纠正。

验收：

- 相同输入经 Telegram/TUI/API 产生等价 Message/Run 语义；
- slash/callback 只执行一次，错误和 stale action 一致；
- duplicate、conflict、expired、wrong-instance、wrong-owner 全部 fail closed；
- Telegram offset、客户端草稿清理和 Remote ACK 仍只发生在 durable acceptance 后；
- external client 的 text 和 media 不再产生不同 source/route 语义。

回滚：endpoint 回到 legacy adapter；已经 canonical 接纳的 Message/Run 不撤销、不重放。

### Phase 3 — 统一事件源与 presentation canary

目标：先让无业务副作用的用户可见卡片从 canonical event feed 出发。

工作：

- 将 meter、HER v2 card、command card、approval/status/error 写成 durable FrontendEvent；
- 将 request activity 暴露为同一 feed 的 ephemeral lane；
- 激活 outbox claim/lease/attempt/receipt 机制，但 canary event 不发送 legacy 副本；
- 为 Telegram、TUI/API 和 external client 建 renderer；
- 将 Workbench 的 transcript/activity/direct-response merge 改为消费服务端 sequence/replacement；
- 每个 presentation event 提供 plain fallback；
- 对关闭 meter/HER、无按钮、断线、刷新、重启和 cursor gap 做测试。

首个 canary：`meter`。同一 Run 在 primary Workbench + Telegram mirror 时应产生一个 canonical meter event、两个独立 delivery attempts，任一失败不影响另一个，也不生成两条 Session card。

验收：meter/HER/command card 在所有允许的目标上语义一致；Telegram 不再是事件创建前提；刷新和重连不丢 durable card；shadow 与 canonical render 差异在预算内。

回滚：按 event kind 将 endpoint 切回 legacy renderer；canonical card 保留，禁止补发已成功的目标。

### Phase 4 — 最终答复、错误与后台通知统一出口

目标：普通最终答复和所有通知先 commit event，再由 FC dispatch。

工作：

- runtime/Engine/HER 只提交 final/error/status semantic result，不调用 Telegram sender；
- DeliveryIntent 从现有 route contract 演进为 endpoint-level destinations；
- dispatcher 从 durable outbox claim event，渲染后发送，并记录每个 destination receipt；
- Telegram 的分片、HTML fallback、rate limit、failover 移入 adapter；
- background/scheduler/proactive 输出采用 internal source + frozen route；
- voice reply 作为同一 assistant Message 的可选媒体 rendition，避免文本和语音重复语义；
- transport unknown、retry budget、poison event 和 dead-letter 有明确操作界面。

验收：

- final Message 只创建一次；primary 和 mirrors 各自最多一次可见投递；
- Telegram 离线不阻止 Workbench/TUI 收到答复，反之亦然；
- Run success 与 delivery failure 分别报告；
- crash 发生在 commit/send/receipt 任一边界时，恢复不双发；
- background 和 error 文案保持当前 locale 与必要信息；
- legacy sender 在 cutover generation 中被 fence，不能竞争发送。

回滚：按 endpoint 恢复 legacy sender；对 `unknown` attempts 先查询/人工判定，不自动 replay。

### Phase 5 — 媒体、文件与工具产物统一

目标：所有 Connector 和工具共享 MediaAsset/MediaGroup 生命周期。

工作：

- 统一 Session attachment、audio asset 和 run output binding 的公共服务；
- 将 TUI upload、Telegram media、Remote transfer、设备 screenshot/download 适配到相同 stage/commit/bind API；
- `frontend_send_attachments` 改为 current-route publisher，并执行 Workzone/resource authority；
- `telegram_send(_file)` 改成显式 destination delivery wrapper，保留工具名和用户功能；
- Connector transport cache 与 PAO asset 分离，缓存 miss 可重新获取；
- 实施 retention classes、cleanup、tombstone、quota 和 orphan repair；
- 保留 MIME 驱动 inline/download、ordered groups、caption、audio duration 和 digest verification。

验收：

- 同一有序文件组可从 Telegram、TUI、external client、Remote 输入并产生等价 Message；
- 工具一次发布多文件时，各前端顺序、caption 和数量一致；
- 重复 tool call 返回同一 group，不复制字节或 Message；
- 越权路径、symlink/junction escape、上传中变更、digest 不符被拒绝；
- staging、message-bound、spool、cache 和 marker 按策略清理；
- 当前长期可见音频与文件不因迁移过早过期。

回滚：媒体 adapter 可回到 legacy transport，但 canonical asset/group 不删除；已迁移长期 asset 不降低 retention。

### Phase 6 — HChat、Remote 与 Exchange typed relay

目标：消除“验证过的消息再降级成 prompt/ transcript correlation”的路径。

工作：

- 定义 RelayEnvelope 与双端 capability handshake；
- local HChat、Remote protocol、Exchange transport 共享本地 ingress bridge；
- verified principal、authorization proof、origin/relay chain 直接进入 MessageContext；
- reply、error、media 和 delivery outcome 使用 correlation event，不轮询 transcript 文本；
- 收拢 `hchat_send` 的路由决策，只保留 transport adapter 与兼容 CLI；
- Remote spool 绑定 MediaAsset；Exchange 保留 durable inbox/outbox 和 publish decision；
- 对旧 peer 保留 versioned compatibility bridge，禁止正文成为命令。

验收：local、same-machine remote、cross-instance Remote、Exchange offline/recovery、reply、duplicate、expired、unpublished、wrong peer、附件组全部通过；`queued`/`delivered` 词义与 proof 一致。

回滚：按 peer capability 回到旧协议；已接纳 message id 保留在 dedupe store，不因 downgrade 重发。

### Phase 7 — TUI 与外部 Workbench 完整收敛

目标：所有一线客户端只依赖标准 FC 协议，兼容路由不再承载新功能。

HASHI TUI：

- 迁移至 v2 snapshot/feed、CommandInvocation 和 MediaGroup；
- 保留当前快捷键、显示、草稿、本地偏好和 mirror UX；
- 对 ephemeral gap、server restart、Session fresh/delete 提供明确状态；
- 不再以 Workbench 名称或 default binding 表达自身身份。

External Workbench：

- server proxy 的 text/media/voice 全部走相同 Session admission；
- roster identity 使用 connection + instance + Agent；
- transcript/history/activity/command card/attachments 从统一 feed 派生；
- 移除客户端对 JSONL/Session/direct response 的优先级猜测；
- HMAC/token 继续只在 server，浏览器不持有；
- drafts、unread、pins、layout、search index 等产品状态继续由客户端负责；
- 多连接、离线和缓存逻辑按 endpoint/cursor 隔离，不能让慢连接污染其他实例。

验收：相同 Session 在 Telegram、TUI 和 Workbench 的 durable 内容、卡片、附件与 delivery 状态一致；允许的视觉差异只来自 capability/renderer。所有当前 Workbench 产品测试与 HASHI live acceptance 均通过。

回滚：客户端按 capability 自动回退 v1/basic projection；server 不返回半个 v2 contract。

### Phase 8 — 退役旁路与文档收口

目标：删除重复 owner，防止回归到 Telegram-first。

只有以下条件全部满足才进入：

- 所有生产 Connector 已 canonical 至少一个观察窗口；
- legacy route 使用率为零或仅剩明确 allowlist peer；
- unknown/double-delivery 指标为零；
- live acceptance 覆盖每个生产 Connector；
- 回滚演练完成；
- Core/source invariant 全绿。

工作：

- 禁止 runtime 和普通工具直接调用 Telegram/WhatsApp transport；
- 移除业务层 `deliver_to_telegram`，由 DeliveryIntent 替代；
- 退役 TUI 专用 delivery schema 和 `workbench/default` 隐式身份；
- 退役 transcript-based reply correlation 与 display-prompt identity；
- 把 basic `/api/chat`、legacy transcript 标为只读/兼容并给出 sunset；
- 删除复制的 model/command/port/surface 常量；
- 更新 architecture、PAO design、Remote/Exchange、command UI、testing policy 和 Agent FYI。

验收：静态检查可阻止新增 direct connector send、client-name branching、未经 registry 的 surface、未绑定 asset path 和 Core import；删除旧代码后全套行为与现场门禁仍通过。

## 15. 代码与仓库影响范围（未来施工）

### 15.1 HASHI1 Functions

预计会调整但不限于：

- FC contracts、delivery route、message context、runtime session、SessionStore/EventStore；
- Backend/Persistent Session API、Telegram ingress/egress adapter、TUI API client；
- command interaction bridge/transport；
- runtime final/presentation/background delivery；
- media asset/attachment/tool publication；
- HChat、Remote bridge、Exchange ingress/receipt projection；
- tests、live acceptance manifest、architecture/FYI 文档。

具体文件由每阶段的 protected-core guard 和 owner review 决定。本计划不预先授权任何 protected path。

### 15.2 External Workbench

Workbench 可按 Phase 2、3、7 调整 server proxy、Session client、event merge、attachment pipeline 和多连接 identity。它必须先做 capability negotiation，不能假设 HASHI1 已采用新版本，也不能把私有 UI 状态推回 HASHI 作为权威状态。

### 15.3 Telegram 与其他 Connector

现有 Bot token、chat mapping、polling lifecycle 和 failover 配置保留，通过 adapter 接入。未来新增 Connector 只实现标准接口与 renderer，不复制 PAO、command、media 或 retry policy。

## 16. 验证计划

### 16.1 静态与架构门禁

- 每次选文件、每个提交和收尾都运行 `python scripts/check_protected_core_changes.py`；
- 加 import/AST gate：Core 不导入 FC product module；runtime/工具不得新增 direct Telegram/WhatsApp send；
- schema、command、model、surface、port、retention 不得复制权威常量；
- docs link、JSON schema、OpenAPI/capability fixtures 和 migration table 校验；
- external client contract fixtures 必须来自 HASHI 导出的版本包，而非手抄。

### 16.2 入口契约矩阵

对 Telegram、TUI、Session API、compat `/api/chat`、Workbench proxy、HChat/Remote、Exchange 分别测试：

- text、reply、edit、slash、callback、cancel、approval；
- 单附件、多附件、caption、voice、空文本加文件；
- duplicate、same-key-different-body、out-of-order、expired；
- wrong instance/Agent/owner/Session/generation；
- declared source spoof、evidence tamper、relay overflow、private authorization expiry；
- offline/reconnect 和 durable acceptance ACK 边界。

### 16.3 出口契约矩阵

对每个 destination 测试：

- final、error、commentary、reasoning、technical、meter、HER、command card、approval、background、proactive；
- text only、media only、text + ordered group、voice rendition；
- renderer fallback、分片、edit/upsert/delete、unsupported capability；
- primary success/mirror fail、primary fail/mirror success、两个 mirror 独立；
- send 前 crash、send 后 receipt 前 crash、receipt 后 commit 前 crash；
- rate limit、timeout、unknown side effect、poison event、queue saturation；
- consumer cursor gap、snapshot recovery、restart 后 durable replay。

### 16.4 媒体与保留

- 当前 16 件/64 MiB 边界及 Connector 更小限制；
- Unicode/空格文件名、MIME 欺骗、digest/size race、symlink/junction/UNC；
- group ordering、atomicity、idempotent replay 和 orphan cleanup；
- staging 过期、message-bound 长期存在、spool terminal+grace、cache miss refetch、30 天 marker 无正文；
- Session delete/tombstone 与外部无法撤回的诚实回执；
- Remote transfer 和设备 artifact 进入同一 asset identity。

### 16.5 命令与工具

- 同一命令从 Telegram、TUI、Workbench 执行得到同一业务结果与不同 renderer；
- issued action、revision、TTL、replay、uncertain mutation、权限改变；
- `/reboot`、共享 replacement、`/restart` 权限不被前端扩大；
- browser/computer approval、lease、handoff、artifact、取消和 connector failure 独立；
- `frontend_send_attachments` 与显式 destination tool 的不重复语义。

### 16.6 Remote/Exchange

- local/remote typed relay、旧 peer downgrade、wrong peer/instance、replay、TTL；
- Exchange offline queue、publish/unpublish fence、restart recovery、exact retry；
- reply correlation 不依赖 transcript；
- verified principal 和 private authorization 在接收端保持强度；
- body 清理后 marker 仍可去重，marker 不泄露正文。

### 16.7 现场验收

扩展 [`HASHI_FRONTEND_LIVE_ACCEPTANCE.md`](HASHI_FRONTEND_LIVE_ACCEPTANCE.md) 的 versioned manifest，至少完成：

1. 同一测试 Agent 分别由 Telegram、TUI、Workbench 发出文本、命令和多附件；
2. primary + mirror 路线验证 final、meter、HER card、error 和 attachment；
3. 断开一个 Connector，确认其他 Connector 与 Run 不受影响，恢复后不双发；
4. command card 刷新、重连、stale click、确认与 lifecycle scope；
5. Remote/Exchange 一次真实 typed message + reply + file group；
6. Browser/Computer 一次 approval + artifact，证明执行与展示状态分离；
7. `/reboot min`、`/reboot max`、`/restart` 按现有授权和 PID 规则；
8. baseline/final checkout、working tree、Core hash 全部不变；
9. 每个 receipt、截图和 observation 分开保存，不用截图代替运行事实。

现场通过必须逐 Connector、逐实例记录；在 HASHI1 通过不能自动推断 HASHI2/3/4 已采用。

## 17. 可观测性与运维

每条链路应能用以下 ID 串联，但普通日志不记录正文：

`ingress_id -> message_id -> run_id/request_id -> event_id -> delivery_id -> attempt_id -> transport receipt`

最低指标：

- 每 endpoint ingress accepted/rejected/duplicate/conflict；
- durable outbox depth、oldest age、claim lease、retry/unknown/dead-letter；
- delivery latency 和 receipt strength；
- event projection/render error 与 capability mismatch；
- media staged/committed/orphan bytes、retention cleanup、cache hit；
- cursor lag、replay gap、snapshot fallback；
- mirror divergence、legacy usage、shadow mismatch；
- redaction failure 和 wrong-instance/security rejection。

运维命令只读显示 Connector generation、health、capabilities、queue age、last receipt 和 migration mode。清队列、重发 unknown、删除 asset 或切换 sender 都是独立受权操作，不能藏在 status 命令中。

## 18. 风险与控制

| 风险 | 后果 | 控制 |
|---|---|---|
| legacy 与 canonical 同时发送 | 用户收到重复答复/文件 | generation fence、per-endpoint single sender、禁止 dual-send |
| outbox commit/send 崩溃窗口 | 丢失或重复 | stable delivery id、claim lease、transport idempotency、unknown state |
| 把 FC 做成新 PAO | 两套权威状态 | ownership tests、FC 只持 derived queue/cache/receipt |
| 统一协议过度复杂 | 所有 Connector 被最强能力绑架 | capability negotiation、plain fallback、独立对象版本 |
| 迁移缩短媒体寿命 | 历史附件消失 | message-bound 保留、先迁 metadata、cleanup dry-run |
| 外部客户端版本不同步 | 聊天不可用 | v1/basic fallback、capability gate、分阶段 adoption |
| Remote/Exchange 降级丢身份 | 冒充或误路由 | typed relay、proof binding、legacy body 永不授权 |
| 工具路径越权 | 泄露本机文件 | resource authority、canonical resolve、symlink/junction tests |
| ephemeral 与 durable 混淆 | preview 被当最终答复 | durability 字段、replacement point、terminal invariant |
| Connector 故障反压 Run | 模型完成却整体卡死 | commit-before-dispatch、独立队列和预算 |
| Core 被便利性修改 | 破坏稳定边界 | protected guard、Functions sidecar、review gate |

## 19. 文档与决策记录

实施时每一阶段都要分别记录 approval、implementation、adoption、live verification。行为改变后更新：

- FC architecture：将本计划通过的 v2 契约提升为 authoritative design；
- PAO design：事件、route、receipt 和 media ownership；
- Persistent Session API/OpenAPI/capability docs；
- command UI、Remote、Exchange、HChat、device control 边界；
- testing policy 与 live acceptance manifest；
- owning decision 和受影响 Agent 的简短 FYI，且不得把完整设计塞入 prompt。

旧文档若与新契约冲突，必须明确 superseded section 和兼容期限，不能留下两个“权威”版本。

## 20. 完成定义

只有同时满足以下条件，FC 统一修复才算完成：

- 所有外部 text/command/control/media 入口都经过 FrontendIngressEnvelope 和 PAO acceptance；
- 所有用户可见 final/card/status/media 都先成为 canonical event，再投递；
- Telegram、TUI、Backend API、Workbench-compatible client、Remote/Exchange 不再各自拥有业务真相；
- primary/mirror 每个 destination 有独立、真实、可追踪的 receipt；
- MediaAsset/MediaGroup 覆盖入口、工具产物、Remote transfer 和保管周期；
- Browser/Computer/tool 的用户交互经过 FC，但执行权仍在原 owner；
- 命令从所有前端共享 registry、权限、幂等和 card state；
- legacy direct send、transcript reply correlation、text/media 双轨和 client-name business branching 已退役或只剩有期限的兼容 allowlist；
- 当前正确功能清单全部有自动与现场证据；
- 任何一个 Connector 离线时，其他 Connector、Run 和 canonical history 仍正确；
- Protected Core 零变化，Core hash 与 source invariant 通过；
- rollback 演练证明不会双发、丢历史或误重试 unknown side effect。

## 21. 推荐施工顺序

推荐严格按 `Phase 0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8` 推进，不并行切换多个出口。第一条端到端 vertical slice 选择 **meter presentation event**：它能直接复现当前 Telegram/Workbench 不一致，又不会重复执行用户任务。meter 在 Telegram、TUI/Workbench 同源成功后，再迁 command/HER card，随后才迁普通 final response。

每个 Phase 单独分支、单独 review、单独 adoption；不要把 HASHI Functions、Telegram cutover、Remote protocol 和 Workbench 大改合在一个不可回滚提交中。外部 Workbench 应在 HASHI capability 稳定后跟进，并始终保留旧协议 fallback，直到 HASHI1 live acceptance 完成。

## HASHI1 执行记录（2026-09-25）

**执行状态：未完成，仍在施工。** 当前分支 `feature/fc-unified-io-20260925` 已实施下列 Functions 纵向切片，但未达到第 20 节完成定义。WSL 已在用户明确授权下重启一次，HASHI1/HASHI2 均通过原有受管任务恢复并经各自健康端点确认就绪；Linux 命令现可执行。当前运行服务仍未采用未提交的 FC 工作树。此记录用于区分已落地内容、运行态与后续工作。

### 已实施

- 抽出共用 frontend_command_admission reservation/complete helper，并把 Telegram 原生 slash 与 callback wrapper 纳入 typed frontend-command 持久 fence。稳定 Telegram Update/Callback ID 绑定 endpoint 与请求摘要；命令参数在 invocation 中按 slash audit 规则脱敏；完成后原子写入 frontend.command_result。已完成的重复请求不再次执行；内容冲突拒绝；pending/unknown 不重放。Telegram 的 transport_delivery_state=not_observed 明确不冒充 Telegram 已送达回执；Workbench callback surface 不重复经过 Telegram fence。HASHI1 Linux 聚焦跨入口回归 259 passed / 4 subtests passed，另有 2 个依赖弃用警告；未做运行态采用或重启。
- 增加版本化 Frontend Ingress、Delivery Intent、Delivery Receipt、Media Group、Command Invocation、Relay 与 Tool Interaction 契约及验证器；增加静态 Connector 能力目录和 `/api/v2/frontend/capabilities`。这尚不是带健康、generation 与动态 endpoint 注册的完整 Registry/统一订阅服务。
- 把旧 TUI 专用 Telegram mirror 策略转为 connector-neutral delivery preference；旧配置只读兼容，首次成功写入时按配置 revision 迁移，损坏/冲突时不覆盖。原 TUI primary 与 mirror 语义保留。
- 将命令执行从重新解析原始 slash 文本改为执行规范化的 typed invocation；新增 command envelope 的身份摘要、action/revision fence 测试。
- 将 meter 与 HER v2 presentation 先写入 canonical Session event，再尝试 Telegram 投影；补充后台最终答复、错误与取消的 outbox claim/receipt 垂直切片，并阻止同一已提交 Run 的重复直接发送。
- 加固 `frontend_send_attachments` 的授权根目录与路径解析检查，补充有序 Media Group 标识、digest 与 retention 绑定测试。
- 在已验证的 Exchange/HChat 消息上下文中保留 typed relay 相关性，不把显示用 prompt 投影当作授权来源。
- 更新命令菜单、FC 架构文档、TUI 兼容策略和迁移测试，移除若干 Telegram/Workbench 专属的旧断言。
- TUI 普通文字在 HASHI1 v2 capability 可用时，经 owner-scoped primary Session 定位并使用 Session Runs 接纳；跨实例 TUI 通过经认证 Remote proxy 使用相同入口。旧实例、旧代理在尚未提交前保留兼容回退；命令与远端/workzone 附件仍走兼容路径。请求提交后的超时不回退，以免双发。Session API 对 TUI 逐次镜像策略作服务端验证，目标实例验证会话所属实例并保留签名的远端来源证据。该切片目前只有离线验证，尚未在 HASHI1 运行服务上采用。
- TUI 的本地备用地址只对可证明尚未连接的写请求尝试切换；写请求超时或连接中断且结果不明时返回 unknown，不向另一地址或旧接口重放，以防跨实例误投和重复执行。
- TUI 新入口单独在 capability 中声明 `tui_session_ingress`；客户端不得从笼统的 v2 schema 版本推断该端点存在。Remote proxy 的支持仍由目标代理操作结果确认，未把另一常驻服务的状态冒充成本服务能力。
- TUI 单附件加文字在 `attachment_runs` 明确能力存在时，经目标 Session 的 stage/upload/commit + Run 接纳；本地路径固定在已解析 Session 的地址，跨实例路径通过经认证的 Remote 代理在目标实例串行完成相同四步。目标代理在 stage 前再核对 Agent、实例和 primary Session；任何步骤回执不明均停止，不切换地址或重放。旧实例及旧代理在明确拒绝新操作且尚未尝试 canonical 写入时继续使用兼容入口。
- `@workzone` 引用在单独声明 `workzone_attachment_runs` 能力时，也经 owner-scoped Session 接纳：只由目标 Agent 已启用的 Workzone 解析目标相对路径，将读取的 bytes 提交为受管附件，再绑定一个 Run；跨实例仅传相对引用，不从来源机猜路径。过渡期旧实例和旧代理仍能在明确未接纳 canonical 操作时回退；目标代理需要再次核对 Session/Agent/实例，结果不明不重放。该切片尚未现场采用，也不等于多附件 MediaGroup 完成。
- Remote TUI proxy 对 mutating 操作先核对目标本地 Workbench 实例身份；无论 peer 线路还是目标本地地址，结果不明时均停止候选线路重试并返回 unknown。畸形附件大小、跨平台路径分隔符、控制字符和特殊文件名在入口拒绝。此处仍不等于 Remote/Exchange 全链路 typed relay 完成。
- Workbench voice audio 通过 Session attachment stage/upload/commit + 一个 Session Run 接纳，保留 `voice_message` 语义及服务端冻结的 Telegram mirror policy。Safe Voice transcript 从该 Run 的 durable event 读取；确认/丢弃走 Session API typed decision，要求原始 Run 与当前 Session context generation 一致。Attachment stage 支持 digest-bound idempotency，同字节上传和 commit 可安全重放；过期附件不可复活。WorkBench 在目标未声明 stage 幂等能力时 fail closed。该切片已离线验证，未采用到运行 Worker。

### 阶段状态

| 阶段 | 当前状态 | 未完成的关键内容 |
|---|---|---|
| 0 行为基线 | 部分完成 | 全量 source-to-sink 矩阵、所有 Connector 的现场基线与现行 manifest 对照仍缺 |
| 1 契约与 Registry | 部分完成 | 第三方 descriptor 已可动态注册并区分 endpoint 注册、健康、ready 与 generation；实际 adapter 生命周期注册、统一 projection/feed、未知版本与完整 capability golden tests 仍缺 |
| 2 统一入口 | 部分完成 | TUI 普通文字、单附件、Workzone 引用和 Session 命令已有 typed/durable 路径；Workbench 纯文本、附件、Canvas handoff/approval、Safe Voice 与菜单命令结果已有 Session 接纳/事件切片；Telegram 原生 slash/callback 已有 typed 持久预留与未知结果禁止重放，但完整 admission gateway 和 Remote/Exchange 尚未完成 |
| 3 canonical presentation | 部分完成 | meter/HER、少量后台路径及 Workbench/Telegram 命令结果事件已有切片；所有 card/status/error 的多 destination dispatcher 与统一 feed 未完成 |
| 4 普通答复与通知 | 部分完成 | background final/error/cancel 已接入受限 outbox 路径；foreground、scheduler、proactive、独立 endpoint 发送与恢复仍需迁移 |
| 5 媒体 | 部分完成 | TUI/Telegram/Remote/Exchange/browser/computer 的统一 Asset/Group stage-bind、保管清理、缓存与重放未完成 |
| 6 Remote/Exchange | 部分完成 | Exchange typed context 有切片；Remote 与 HChat 全链路、reply correlation、跨实例文件及离线恢复未收敛 |
| 7 TUI/Workbench | 部分完成 | HASHI1 的 TUI 普通文字、单附件、Workzone 引用及 Session 命令已有 canonical/durable 接纳；展示仍用 transcript 轮询。Workbench 分支已迁移普通文本、附件、Canvas handoff/approval、Safe Voice 与菜单命令结果；最新服务端 579 项、UI 策略 589 项通过且生产构建成功。旧 HASHI 只在标准能力探测明确不支持且尚未开始写入时回退一次文字兼容入口。运行服务 adoption、真人麦克风/文件交付、Safe Voice 确认现场验收仍未完成 |
| 8 退役旁路 | 未开始 | Telegram 直接发送、旧 transcript correlation、兼容双轨和 legacy policy 均未退役 |

### 已有验证与限制

- 最新 HASHI1 工作树的相关回归分三组共 **506 passed**：FC/Session/命令/附件 189 项；投影/Remote/HChat/Telegram 与菜单 175 项；工程与 runtime 门禁 142 项。测试由 Windows Python 对同一 HASHI1 共享工作树执行；它们验证代码行为，但不等同于 HASHI1 Linux 运行环境或线上 adoption 验收。
- 后续 TUI 文字入口与 Remote proxy 切片新增红/绿证据；FC 契约、TUI、Remote、Session、MessageContext、运行时投递和附件合并回归 **125 passed**（与上述 506 项有重叠，不相加）。
- TUI client、实例切换、Remote proxy 与 Session API 扩展回归 **64 passed / 1 skipped**（同样与前述集合重叠）。Windows 测试环境缺少 `textual`，TUI 图形渲染测试在收集阶段无法运行；不把该项记为通过。
- 通过只读静态检查：本次修改的 **28 个 Python 文件 AST 语法解析通过**、`git diff --check` 通过、`python scripts/check_protected_core_changes.py` 报告 `protected core check: ok`。
- 早前 WSL 命令曾遇到 Service `0x8007274c` 超时；本次在用户许可下执行**一次** `wsl --shutdown`，随后分别启动 `HASHI1-User-Runtime` 与 `HASHI2-User-Runtime` 受管任务。两者各自健康端点回报 `status=ready` 且实例身份正确，Linux `lily` 命令恢复。该操作恢复访问，不证明底层超时根因已永久消失。运行服务尚未采用当前 FC 工作树，也未执行 Function hot `/reboot`；后续不再以 WSL/冷重启作为采用手段。
- HASHI1 Linux 虚拟环境聚焦回归：FC 契约、TUI、Remote proxy、Session API 与 policy 为 **87 passed**；TUI 图形展示为 **27 passed**。扩展组合为 **130 passed / 1 failed**，失败是 `recent_agent_exchanges` 跨 Session 时间排序测试，单独重跑通过，需后续排查其时间排序稳定性。Core gate 为 **709 passed / 3 failed**；三个失败均由当前 Function 源码未提交触发 generation 的 clean Git HEAD 限制，提交候选变更后必须重新运行，不应计作功能通过。
- 修改目前仍是未提交工作树；Core 未改。HASHI1 尚无 connector-by-connector live acceptance、断线恢复/无双发实测、媒体保留清理实测或 rollback 演练。
- 此后补充的 Linux 聚焦回归：TUI client、Remote TUI proxy、Session API、TUI rendering **93 passed**；扩展到 FC 契约、媒体工具、MessageContext、SessionStore、运行时投递、Telegram 命令与 meter/HER 的组合回归 **266 passed**。包括本地附件 canonical Run、已确认 Session 地址绑定、备用地址实例验证、远程 mutation unknown 不重放以及畸形附件拒绝的红/绿证据。上述集合与此前测试有重叠，不相加。
- 本轮跨实例 TUI 单附件迁移新增 **4 个先失败后通过**的定向用例，并补充旧代理回退、canonical 写入后禁止回退和错误 Session 拒绝。HASHI1 Linux 虚拟环境的 TUI/Remote/Session/FC 聚焦回归 **91 passed**，上述 14 组组合回归 **274 passed**。这是工作树离线验证，不代表 HASHI1 运行服务已采用；测试集合与前述回归有重叠，不相加。
- 随后 `@workzone` 迁移新增 Session 存储、越界拒绝、直接 TUI 和跨实例 Remote 的红/绿用例；更新显式能力清单的旧断言，并验证旧实例/旧代理安全回退。含 TUI、Remote、Session、媒体、命令、meter/HER 与作用域可靠性的 **16 组 Linux 组合回归 302 passed**。目标 Workzone 内容经受管 Session asset 进入单个 Run，未授权/越界引用和结果不明不触发第二次提交。仍无运行服务采用或现场附件交付证明，测试数与前述集合重叠，不相加。
- 本次补入 Workbench Safe Voice Session Run 路径、按 run_id 的 durable-event 查询及 atomic Session-generation 决策 fence；附件 stage/upload/commit 增加安全幂等重放和显式 capability。HASHI1 Linux 定向 `tests/test_session_store.py tests/test_session_api.py` **74 passed**；Workbench Session/voice/React 集成定向 **50 passed**；保护 Core 检查通过。结果仍是离线源码验证，HASHI1/2 运行服务未重启或采用本次更改。
- 2026-09-25 将 Workbench Worker 的命令菜单执行接入 typed `frontend-command` 身份与持久 Session 幂等登记：执行前 reservation，完成结果与 `frontend.command_result` 事件原子写入；冲突拒绝，挂起/未知不重执行。完成态可跨 Worker 本地菜单缓存丢失回放；回放去掉失效按钮并要求刷新。共享命令 ID 验证器也接受 command-menu 实际生成的 URL-safe 随机按钮 ID（包括首位下划线）。HASHI1 聚焦交叉回归 **178 passed / 4 subtests passed**；未做运行态采用或重启。

### 下一步门槛

Telegram 原生 slash/callback 的持久去重切片已完成离线验证，但仍未覆盖统一 gateway、Telegram update offset 的 durable-accept fence，或真实 transport delivery receipt。下一阶段应推进规范 Session feed 与 durable outbox 的多目的地投递，再扩展 Remote/Exchange 与媒体生命周期。
下一步不再以单一 TUI 路径作为主线：推进 Session 规范事件/feed 的共同消费契约及 Telegram 原生 slash ingress，再迁 Remote/Exchange、工具媒体和通用 outbox 旁路；每个切片做完离线验证后再申请运行态 adoption。真人麦克风/文件交付、外部 Workbench 手动确认、跨 Connector 现场矩阵和 rollback rehearsal 不能由离线测试代替。只有第 20 节所有条目和 `HASHI_FRONTEND_LIVE_ACCEPTANCE.md` 必需现场项通过，才可把状态改为完成。HASHI1 hot `/reboot` 需另行明确授权且健康检查可执行；不进行 Core cold restart，并保持 HASHI2 在线。

### 独立终审补记（2026-09-25）

- 撤销了执行计划第 7 节此前没有证据支持的 10/10 自签。静态搜索确认新建的统一 ingress、projection 与 dispatcher 仍主要由符合性测试调用，尚未覆盖所有生产 source-to-sink 路径；本文件的“未完成”状态继续有效。
- 修正能力真值：静态 Connector 声明不再自动返回 `ready=true/online`；第三方注册不能覆盖内置 ID，只有匹配的已注册 endpoint 才能报告健康与 generation。
- 修正投递真值：写入本地 pull feed 只记录 `accepted`，不再用本地 sequence 冒充用户已收到；adapter 普通异常视为副作用可能未知并记录 `unknown`。
- 取消控制动作已通过持久 reservation 防重；完成态重放返回原结果，不会误停后来创建的新 Run，pending/unknown 不自动重执行。
- Workbench Minato E2E 使用独立数据根目录，修复全量套件共享项目目录造成的次序波动。文字兼容回退只发生在标准能力探测明确不支持且任何 canonical 写入尚未开始之前；超时、连接中断或已开始 Run 接纳均不回退。
- 修正后 HASHI 聚焦候选回归为 **322 passed / 4 subtests passed**；完整提交前门禁为 **709 passed / 3 failed**，三个失败均明确来自 generation 门禁拒绝未提交 Function 源码，未发现其他行为失败。必须在干净提交后重跑，不能提前记通过。
- Workbench 完整服务测试为 **579 passed / 0 failed / 0 skipped**，UI policy 为 **589 passed / 0 failed / 0 skipped**，生产构建成功。HASHI1 与 HASHI2 的只读健康端点均为 `ready`；HASHI2 未被修改或重启。HASHI1 运行 generation 仍是旧候选，以上仍是源码/离线证据，不是采用或现场交付。
