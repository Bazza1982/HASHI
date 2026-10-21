# HASHI `/standby` 具体施工方案

| 项目 | 内容 |
|---|---|
| 版本／日期 | 1.0 · 2026-10-15 |
| 需求基准 | [Standby PRD v1.1](HASHI_STANDBY_PRD.md)，原文不修改 |
| 施工分支 | `Bazza1982/HASHI` · `feature-standby` |
| 本次核对的分支 HEAD | `812de711241aaa0bcaa1e76820bd081c9f1eaf7f` |
| 对应产品代码基线 | `7db211eb34fcfc11dd16f33194472c2b1efe3df1` |
| 当前状态 | 方案交付；下文 S0–S7 尚未完成实施／验收，不是功能发布记录 |
| 本轮授权 | 仅在该分支保存施工与验收文档；不改产品代码、实例配置、其他仓库或运行服务 |
| 主 owner／工程层 | PAO／Functions；PCM、HERV3、Frontend Connector 在自身边界协作 |
| 配套文件 | [测试与验收手册](HASHI_STANDBY_TEST_ACCEPTANCE.md) |

## 1. 怎么使用这份方案

先读 PRD，再按本方案施工，最后按测试手册验收。PRD 定义用户承诺；本方案把它拆成依赖、代码落点、迁移与关卡；测试手册定义如何证明。出现冲突时不能偷偷选择较容易的实现，必须记录差异并遵循仓库架构和 PRD。

以下文件布局、接口、测试名称及新增字段均是**待实现设计**。表中标为“现有”的内容才是本次读到的源码／契约。源码核对不等于运行实例验证；没有完整审计全部 Engine、缓存或外部客户端。

这一功能采用 HASHI 原创开发。不得引入 Omi、Screenpipe、Frigate、Home Assistant Assist、LiveKit Agents 或供应商主动语音运行时作为待命控制层。保留现有已批准模型、编解码、操作系统媒体能力及依赖；新增第三方依赖另列必要性、许可、隔离和外发审查，不借“原创”重写成熟的底层协议。

施工是完整目标的分阶段落地，不是把隐私、删除和停止留到最后。每个阶段开放的能力必须已经满足相应保护要求。任何阶段都不通过 `if standby` 大量侵入 CallService，不复制整套 Call/Phone、聊天存储、权限或模型目录。

## 2. 总体顺序与阶段关卡

| 阶段 | 对应 PRD §17 | 做出的东西 | 主要测试 | 退出关卡 |
|---|---|---|---|---|
| S0 · 基线与契约冻结 | 阶段 0 | 复用清单、数据落点清单、接口与资格矩阵、测试样本设计 | 静态核对、接口样例、已有相关回归基线 | 每个高风险缺口有 owner、实现任务和明确禁用策略 |
| S1 · 控制与隐私底座 | 阶段 1 前置 | 会话、配置、policy/epoch、内容租期、撤权、清理、菜单控制 | 真存储＋假时钟＋失败注入 | 未授权零采集；停止先撤权；受管数据可撤销与恢复清理 |
| S2 · 原生媒体复用 | 阶段 1 | Call 原语、单一媒体租约、有界音频/视觉、健康与覆盖 | 原 Call/Phone 回归、媒体 ingress 与竞态 | 无假 Call、无重复采集、租约失效后无上传／播放 |
| S3 · Judge 与影子闭环 | 阶段 1 | 无工具 Judge、目标路由、预算、解释与反馈 | 结构错误、重试、回放、影子副作用否定测试 | 影子可解释但没有业务行动；资格矩阵决定能否 ARMED |
| S4 · 安静陪伴正式 | 阶段 2 | 原生 Agent 升级、有限对话窗口、自然语音与准确取消 | 真 PAO/Engine 边界、完整音频循环、泄漏测试 | 能保持安静、可靠停止；不突破权限和保留契约 |
| S5 · 会议与递纸条 | 阶段 3 | 连续记录、修订、缺口、笔记提升、限域检索与时机检查 | 长会议、耳机音轨、无结果、删除派生物 | 不漏记而假称完整，不查不到也硬递纸条 |
| S6 · 专用实验模板 | 阶段 4 | 定时提醒、合格姿势/声音/访客观察模板 | 每模板独立数据集与故障验证 | 每种感知单独合格；不作医疗／安全设备承诺 |
| S7 · 集成与发布验收 | 横贯各阶段 | 精确候选、平台范围、8 小时报告、恢复与回滚手册 | 聚焦集成、发布级回归、真实设备验收 | 产品、源码、构建、运行代次、设备证据分别通过 |

依赖链：`S0 → S1 → S2 → S3 → S4 → S5`。S6 复用已合格底座；S7 的检查随阶段积累，但最终必须针对同一候选重验。S6 未合格可以保留为明确不可用的实验模板，不得把“主体验通过”写成“六种模板全通过”。

外部图形客户端 F1–F5 是横向依赖，见第 11 节。只完成 HASHI 后端时可以报告“后端合同通过”，不得报告麦克风、主题、字幕与用户体验已验收。

## 3. 已核对的原生落点与缺口

以下基于本页头部固定 commit；开工时要重新检查目标分支是否前进。

| 现有文件／契约 | 已读到的能力 | 本功能怎样复用；不能假设什么 |
|---|---|---|
| `orchestrator/frontend_call/routes.py` | `register_call_api`；认证、owner、请求限额、no-store 响应；`/api/v1/call/operation`；服务清理 | 延用认证／路由安装模式，不新增未认证媒体端口；不得因 Call 已认证就认为其全部语义适用于 Standby |
| `frontend_call/adapters.py` | `MediaAdapters`；实例目标与凭据；有界请求；禁止重定向 | 复用 STT/TTS/视觉调用；所有调用必须从政策门禁后的入口进入，不直接借 adapter 绕行 |
| `frontend_call/service.py` | `CallService`、epoch、待处理帧、TTS 任务／缓存、有限协调；云媒体隐私检查 | 只提取真实共享原语；不启动假 Call，不共用开场、铃声或整场生命周期 |
| `frontend_call/ports.py` | `HashiPorts.validate`、`privacy_level`、原生 admission／cancel 关联 | 待命端口复用原生 owner；新事件必须保留 standby 来源和收窄 scope，不伪装为 call speech |
| `frontend_call/contract.py` | 有界媒体校验、自然语音分段、filler 目录 | 保留原函数行为；共享无状态原语，不能复制字符表／模型表；Call 的时长／体积限制不是 Standby 的默认配置 |
| `frontend_call/transcription.py` | `spoken_transcription`，解释 STT 的语音／噪声证据 | 它不是声学分类器；不得用它证明会识别持续哭泣、警报或访客身份 |
| `frontend_call/config.py`、`settings.py`、`billing.py`、`diagnostics.py` | 配置、媒体计量与无内容诊断的既有落点 | 开工时确认所需公开接口；引用配置 revision、物理请求用量，不复制第二份设置 |
| `orchestrator/session_store.py` | 原生 Conversation／Agent Activity／Run、媒体 attachment 依赖 | 新增 Standby 控制扩展仍由 PAO 存储边界管理；不能把环境转写默认写进普通 Message |
| `docs/HASHI_HERV3_PRIVACY_PILOT_2026-10-02.md` | Level 2 的 HERV3／合格官方 DeepSeek、本地过滤、失败阻断 | Standby Judge 尚无已证明的 Level 2 专用资格；不得走直接 ephemeral API 冒充合格 |
| `docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md` | 单 owner、Functions、配置与运行分层 | 配置用 `orchestrator.config_json`；命令在原注册 owner；不新增 Core 产品导入 |
| `docs/HASHI_CALL_CURRENT_BEHAVIOR.md` | Call 当前入口、视觉、语音、取消与发布合同 | 是不可破坏的回归基准；旧 PRD 里的固定句数、旧入口规则不得复活 |
| `docs/TESTING_POLICY.md`、`docs/HASHI_FRONTEND_LIVE_ACCEPTANCE.md` | 聚焦红绿测试、独立 live 授权、源码与运行证据分离 | 不为 docs 跑完整产品测试；不因旧任务授权而重启实例 |

**必须正面解决的缺口：**长待命租约、连续会议分片、跨阶段外发策略、有保留限制的原生 Run、Engine 恢复上下文清理、派生数据 lineage、跨客户端真实媒体释放。本次没有把这些写成“已经存在，接线即可”。

## 4. 拟定代码布局与唯一状态所有权

### 4.1 增量布局

以下为建议布局，不要求先建立一堆空文件；按真实职责与提交顺序新增。

```text
orchestrator/standby/
  contract.py        # Profile/Session/Episode/Decision/operation schema + defaults
  service.py         # 生命周期、目标调度、epoch；不装模型私有逻辑
  policy.py          # 原生隐私/权限的调用入口与会话收窄；不是第二个隐私系统
  store.py           # PAO 持久化扩展；只管理 Standby 独有控制/引用
  observation.py     # 有界片段、修订、覆盖、记录/介入分流
  judge.py           # 一次有界语义判断；调用 qualified native model port
  ports.py           # 原生 PAO/PCM/Engine/交付/媒体/清理的窄接口
  recording.py       # 会议工作稿与显式提升；按需要在 S5 新增
  disposal.py        # 撤读、lineage 协调、清理回执；不越 owner 删别人的表
  routes.py          # 认证协议与只读投影
  ui.py              # 共享卡片/本地化/导航的薄呈现
orchestrator/commands/standby.py
  # 使用现有命令注册与 command_specs 的单一元数据来源
```

允许在 S0 收敛相邻小模块，但不得把安全、UI、provider HTTP、SQL、媒体播放全堆进 `service.py`。不创建新的通用框架、微服务集群或常驻 Agent。

现有 owner 的必要扩展：SessionStore、原生隐私／qualified model factory、PAO Run scope 与取消、PCM／Engine 数据保留、原生 attachment／Artifact 处置、FC 交付、Function 注册。各由其自身 owner 实现公共方法，Standby 只调用。

仅当 Call 与 Standby 两边确实使用同一原语，才把它从现有文件小范围提取到 Functions 内的共享媒体模块。保留原 Call 导出作兼容转发，不复制逻辑。不要为尚不存在的第三个使用者设计抽象层。

### 4.2 状态分配

| 数据 | 唯一写入 owner | 说明 |
|---|---|---|
| Profile 原文、确认解释、配置引用 | PAO 配置 owner，通过 `config_json` | 可用受管 `standby_profiles.json`；位于实例数据，不入源码；不含密钥 |
| Standby 生命周期、epoch、版本、绑定 | PAO Standby 存储扩展 | 建议作为现有 PAO store 中的控制表；不新建独立 chat.db |
| 租约与跨 Agent 争用 | 同实例共享 PAO 媒体仲裁 owner | 不能各 Worker 各有一张无关联的 busy 表 |
| 原始媒体／临时观察 | 有界易失对象及原生媒体 owner | 默认不落盘；不序列化进 checkpoint |
| 会议片段、工作稿、正式产物 | 原生受管内容／Artifact owner | 控制表保存引用，不复制原文；正式产物有独立用途与期限 |
| Episode/Run/交付关联与使用量 | 现有 Events、Run、outbox、meter owner | 不建第二套投递队列或计费账本；按物理请求 ID 去重 |
| data lineage、撤读、清理进度 | 原生内容 owner＋PAO 处置协调 | 只保留受管 ID、用途、TTL、必要回执；不是另一份正文存储 |
| 菜单、最近记录、客户端字幕 | 上述权威状态的投影 | 本地可缓存视图，但不可独立更改业务真相 |

S0 必须确认可扩展事务入口；若新增控制表，schema migration、重开 store 测试和反向兼容由同一 store owner 承担。不得让新 `store.py` 直接连接别人的私有 SQLite 路径。

## 5. 先冻结的六个技术契约

### 5.1 控制协议与原生认证

建议新增 `POST /api/v1/standby/operation`，由 Functions 安装，版本 1；存在性通过现有 capability discovery 发布。下表是待实现协议，不是可立即使用的 API。

| 操作 | 语义／必要限制 |
|---|---|
| `capabilities / status / recent / operation_status` | 只读、owner-scoped；读取不续租、不开始采集、不唤醒模型 |
| `save_profile / interpret_profile` | revision-safe；解释原文也可能含敏感信息，模型解释需要处理授权；不执行目标 |
| `preflight` | 返回有效 policy、分阶段目的地、保留、能力和阻断原因；不采集、不发环境样本 |
| `start_shadow / arm / pause / resume / end` | 有界幂等控制；`arm` 需匹配校准指纹；`pause/end` 不依赖 Judge 成功 |
| `renew_lease / ingest_audio / ingest_frame` | 仅有效租约／epoch；限额独立于模型速度；不接受客户端自授 owner 或 purpose |
| `finish_notes / promote_artifact / delete_data` | 各自明确范围与确认；不是 end 的隐含副作用 |
| `feedback` | 记录授权范围内的标签；不是自动改提示词、模型或权限 |

每次 mutation 必须带操作 ID、当前 revision／epoch 和绑定；owner 从认证上下文解析。服务端返回已提交控制结果及实际数据/交付状态。请求未知时查 `operation_status`，不换入口重发。响应 no-store；错误只含安全原因码，不带请求原文、媒体、收件人或 endpoint。

现有代理／Remote 通道负责凭据，不把 admin token 下发到不可信 renderer，不新增未经审查的跨域或匿名媒体端点。profile-unsupported 等既有部署限制继续保留；新能力不能借 Standby 绕开 governed profile 的约束。

### 5.2 统一 scope 与版本

建议所有阶段持有不可变 `StandbyScope`：`owner_id, instance_id, agent_id, conversation_session_id, context_generation, standby_id, client_id, capture_group_id, epoch, profile_revision, policy_revision, pipeline_fingerprint, expires_at`。

Profile 引用的是用户设置；scope 是一次执行的权威快照；当前政策必须在每个敏感边界重读／核验。不能只检查启动时的快照。内容和模型回填的 owner、epoch、TTL 都不具有授权效力。

建议过程如下，方法名是实现目标，非现有 API：

```text
接纳事件：认证 → 绑定/租约 → 当前政策 → 容量/TTL → 入流
判断请求：取有界上下文 → 原生外发门禁 → 预算预留 → 单次模型调用
结果提交：重验 policy/epoch/输入revision/TTL → 规范化决定 → 路由
行动提交：原生权限交集 → 资源/接收者/保留资格 → 原生 Run 或动作
交付提交：重验输出许可/时机/epoch → 原生 outbox/播放 → 真实回执
暂停/结束：先持久撤权与 epoch 屏障 → 再取消/reap/释放 → 报告清理状态
```

重验和副作用提交必须有可线性化的边界（例如原生 admission transaction／短期一次性许可），不是 `if allowed: await ...` 中间任意插入撤权却仍发送。测试必须在这些窗口插入暂停/撤权。

### 5.3 媒体租约与 Call/Phone 共存

建议初始工程参数为 15 秒租约、5 秒续约，S0 冻结为可配置默认并在真实网络测试调整；这是设计值，不是现有 Call 值。服务器和客户端都使用到期屏障；resume 后先检查过期，再允许采集回调运行。控制状态刷新不代替采集健康。

同一浏览器难以可靠证明两个标签的设备 ID 指向同一物理麦克风，因此不能只信任页面提交的 `device_id`。首发对无法验证设备映射的网页使用更保守的同 owner／同实例单一活动媒体会话锁；原生桌面可用受信平台 capture broker。不同实例或其他应用不在本实例证明范围内，必须披露。

仲裁顺序：用户显式 Call/Phone 请求 → 撤销该 capture group 的 Standby 输入/播放租约 → 等待客户端停采证明或旧租约到期 → 沿原通话入口开始。交接不明确时宁可报告媒体忙，不双开。Call 结束保持 Standby PAUSED。不缩减原 Call/Phone 已有更严格 busy 规则，不结束其他设备的无关工作。

原生 Call 启动不再依赖它自己的 in-memory busy 检查单独完成仲裁，而通过同一共享 owner 取得许可；这是需要新增的原生接线，不是声称现有功能已完整共享租约。

### 5.4 数据策略与 Engine 资格

建议 `ExecutionDataPolicy`（新契约）携带用途、保留类别、data refs、禁止自动记忆、恢复资格、到期和撤权代次。所有会写数据的 owner 必须执行它，不能只是给 prompt 加一句“不保存”。

分清三种能力：`ephemeral_observation`（易失观察）、`managed_working_content`（有 TTL 的工作材料）、`promoted_artifact`（明确保存的产物）。默认环境输入是第一种，不能因为进入 PAO 就升级为持久普通聊天。

完整 Agent 的执行资格按 Engine／provider 路径验证：Message/Event 正文、工具证据、HER Turn、compact/checkpoint、provider context、缓存、Memory+、Habit、远端记忆、日志都要覆盖。没有相应能力则阻断该升级，仍可使用合格的影子或本地记录；不得隐藏换 Engine、造新 Agent，或把内容搬到另一低隐私会话。

待命事件保留与父用户授权的关系，使用原生自动化/活动 admission 语义和 typed 来源，不能伪造“用户亲自说了这段指令”。绑定 Conversation 不意味着每个 Episode 都要创建 Message；是否需要活动执行容器由 PAO 原生合同决定，不新建第二套待命聊天历史。

数据正常 TTL 到期只删除该用途对象；已有独立授权的笔记可以保留。删除／撤回用途按明确范围级联，不把普通音频 60 秒轮转误当成所有合法笔记必须立刻消失。

### 5.5 Fast Judge、资格与预算

Fast Judge 调用通过原生合格模型 factory/port；PAO 不直接构造 provider HTTP。一个有界请求、工具禁用、无 JEV 生命周期、无完整长期记忆；结果保持 PRD 六枚举与多个 target。

对 `OBSERVE_MORE` 按 PRD 默认最多 2 次、累计 15 秒，取更短 Episode TTL；可继续采集的来源仍受原授权。模型故障不是 `UNCERTAIN` 语义结果，不触发完整 Agent 兜底。

Level 0/1 仍受逐阶段外发和目的地许可；Level 2 保留当前 HERV3／合格官方 DeepSeek 和文本过滤边界。S0 资格矩阵未证明的 Standby Judge、云 STT/视觉/TTS 或 Engine 路径一律 BLOCKED。新增资格在原生隐私 owner 施工，不能复制一个较宽松的 `_privacy`。

预算按物理请求 ID 管理：预留、发送、回执、结算、未知。总费用引用原生 meter；不能把已有媒体总计再次计入 Standby 总计。未知价格不记零；硬金额上限下，无可靠上界的调用必须阻断。未知送达／费用保留预留或明确不确定，不靠 timeout 把钱退回再重试。

### 5.6 数据删除协议

先让受影响对象不可读取，再按 owner 清理。协调记录只含 ID、范围、请求 revision、必要外部回执，不含待删正文。建议进度：`ACCESS_REVOKED → PURGE_PENDING → PURGED_LOCAL`，外部不确定作为独立范围状态显示，不以一个全局绿色图标掩盖它。

删除对象集包含原始 buffer、临时转写/摘要、工作稿、正式产物（明确选中时）、搜索/向量索引、TTS、客户端缓存、Engine 恢复数据及受管备份。检索源 Workzone 原件不属于本次数据，不能误删。

Engine clean-context rebuild 由 Engine owner 在原生安全边界完成；不粗暴 `/fresh` 全部用户任务，不改写无关历史。混合摘要先撤读，优先删除而不是自动再付费重算；重算需剩余内容与处理许可。

备份恢复在对外读／索引前应用仍有效的删除记录。删除记录至少覆盖相应受管备份的最大恢复窗口；无法保证的备份路径不得宣称能防复活。物理介质擦除、外部供应商、用户下载副本明确在不同证明范围内。

## 6. S0 — 基线、调查与契约冻结

**任务 S0.1：确定真实目标。**读取本地 Git 状态、远端分支与 PRD hash；保留其他未提交工作。当前任务只授权 `feature-standby`，不是授权修改 main 或以前的调试分支。列出将改文件并运行原生 protected-Core 检查。读取 AGENTS、架构、边界、UI、Testing Policy 与 Call 当前合同。

**任务 S0.2：沿真实调用追踪一次。**从 Call route → service → media adapter，以及 ports → PAO → Engine → delivery，标出所有内容存储、外发、可取消点。特别追踪 profile 解释、ASR、Judge、工具、TTS、错误处理、memory/consolidation。每个条目标注已读符号、测试、缺口，而不是“应该已经支持”。

**任务 S0.3：冻结设计。**完成第 5 节契约、Profile schema、错误码、单 owner 状态表；建立有效隐私/Engine/模态矩阵；为每条高风险路径写“不满足时哪里阻断”。确定 supported deployment profiles，未合格 enterprise/Remote 模式不伪装可用。

**任务 S0.4：测试基线。**运行现有 `tests/frontend_call` 和受影响 Phone／原生边界测试，记录原有失败，不修无关 bug。建立合成语音、文本、图像、会议与恶意输入样本计划，禁止下载真实敏感材料作 fixture。

**交付：**代码落点差异表、数据流/处置清单、接口样例、阻断矩阵、测试 baseline。它们可作为本方案实施记录补充，不需要再造若干竞争架构文件。

**G0 验收：**不存在“待命完成以后再考虑怎么删”的路径；每个新外发都能指出原生门禁；不存在未命名 owner 的权威状态。未发现合格 Engine 时允许安排其施工，但必须明确阻止提前正式启用。

## 7. S1 — 先建能安全停止和删除的底座

**任务 S1.1：契约/配置。**实现严格 schema、默认值唯一来源、revision-safe Profile 保存、确认解释；通过原 `command_specs`／动态 handler 接入 `/standby`。只显示配置及阻断原因，未开始前不请求传感器、不做环境模型调用。

**任务 S1.2：控制状态。**实现独立 lifecycle、mode、health、delivery、disposal；乐观并发／事务约束；绑定与 epoch；幂等 start/pause/end/status。未知写入状态用查询恢复，不盲重试。关闭功能开关后仍可暂停、结束、清理已存在活动。

**任务 S1.3：统一策略入口。**委托原生 `/privacy` 与权限 owner，附用途、目的地、数据类别、当前 scope。策略未知、上游不可用、过滤失败时关门；实现运行中撤权通知与提交前检查。撤权不得依赖另一个长模型任务完成。

**任务 S1.4：数据生命周期最小闭环。**先实现易失 buffer、内容引用、TTL、访问撤销、受管清理任务与崩溃后恢复；普通诊断无内容。默认排除 memory/Habit/远端整理；没有接好排除的路径先禁用。不要为此把原始媒体写入持久队列。

**任务 S1.5：恢复与配置迁移。**旧实例无配置即 OFF；仅增量迁移，损坏配置不覆盖；恢复 ACTIVE 记录为 PAUSED；启动先恢复 purge，再允许读内容。downgrade 不删除用户正式产物，不通过还原旧数据库复活内容。

**文件：**新 `standby/contract.py, policy.py, store.py, disposal.py, service.py, ui.py`；现有原生配置、命令、PAO store、记忆/Engine owner 的必要窄扩展。

**测试：**用真实临时 store、假时钟、受控 await barrier 测 revision 冲突、重开、撤读、TTL、途中崩溃与权限收紧；扫描合成 canary 的受控落点；此阶段不接真实麦克风。

**G1 验收：**SB-01、13–16、19–23 的底座场景通过；停止提交后所有晚结果不可写回，原生其他 Run 不受误伤；清理失败显示 pending，而不是成功。需要真实音频的子项仍标 BLOCKED，不能整条涂绿。

## 8. S2 — 复用 Call 原语，接入有界媒体

**任务 S2.1：先保护 Call。**为拟提取原语补充能抓到错误的回归；提取无状态媒体校验、适配器调用、需要共享的播放/视觉调度原语。兼容导出调用同一个 owner。Call 的入口、开场、铃音、camera fallback、字幕、barge-in 与 hangup 不改变。

**任务 S2.2：共享仲裁。**实现第 5.3 节租约并把 Call/Phone/Standby 接入同一个原生仲裁点。比较并交换／事务获取；跨 Worker 竞争和两个浏览器页竞争要真测试。锁的存在不等于物理已停采，需要客户端释放回执或失效屏障。

**任务 S2.3：声音输入。**复用现有设备捕获、VAD/endpointing 与媒体目标；Standby 新增观察窗口，不每段直接变成用户消息。临时音频聚合遵循 60 秒总时间和字节上限；慢 ASR 时保持有界、显式标缺口。源音轨分别标记，禁止把系统音频和麦克风悄悄合并后假称可区分说话人。

**任务 S2.4：视觉/定时。**摄像头默认 OFF；复用帧 ingress 与语义新鲜度分离、epoch、最新待处理帧；最多一个视觉推理＋一个待处理帧。定时/无变化依据原生 Scheduler、单调持续时钟与输入覆盖，不用网络模型读时钟。

**任务 S2.5：过载/取消。**音频记录不套用视觉 latest-only；容量满要报告覆盖缺口。租约失效、背景不合格、断线、暂停时停止捕获/上传/播放并清空缓存；不离线补录。S1 的外发门禁包围每次 media adapter 调用。

**文件：**现有 Call/Phone 的最小公共提取与仲裁接线；新 `standby/observation.py, ports.py, routes.py`；外部客户端 F2 单列。

**测试：**虚拟媒体的协议/格式/乱序/重试/超长测试；真实 Functions route 测未认证、跨 owner、伪造 lease；继承原 Call/Phone 回归。物理 track 停止、耳机音轨和锁屏留给获授权的设备实验，不用 mock 代替。

**G2 验收：**SB-08、11–14、17、24、26–27 相应媒体边界通过；待命启动没有 Call opening、ringback 或正常 Call 的虚假 busy 档案；媒体失败原因可见。

## 9. S3 — Fast Judge、预算与影子模式

**任务 S3.1：原生资格。**从当前能力/实例配置解析候选，新增 Standby 用途资格，不硬编码产品模型名。Level 2 无合格 Judge 时保持 BLOCKED；需扩展时在原生隐私 owner 提供一次、无工具、完整请求过滤的入口，并用实际出站边界测试证明。

**任务 S3.2：有界判断。**生成最小上下文；校验六枚举、target IDs、引用、输入 revision、最大输出和 TTL。模型返回字段不作为 authority。一个会话最多一个 Judge；重复候选合并，但不能吞掉另一个目标。OBSERVE_MORE、timeout 和无效结构有不同原因码。

**任务 S3.3：记录/介入分离。**让两者订阅同一规范化输入，记录不等 Judge；`RECORD_ONLY` 不能创建原来没授权的保存行为。Shadow 允许已经授权的记录器写入，但不允许业务工具、完整 Agent 查资料或主动语音。

**任务 S3.4：预算与健康。**统一每阶段物理请求计量和预留；配置每小时/会话上限、频率、有限队列。无事件时不循环打模型。模型不可用及预算用尽显示对应目标 BLOCKED/DEGRADED，不把无结果当正常观察。

**任务 S3.5：影子反馈/校准。**最近记录仅展示仍获准的内容；默认显示原因码、时间和计划。加入标签和 pipeline fingerprint；重大变化使 ARMED 失效。tighten 权限立即生效，不等待重新校准。样本回放要检查用途、期限与当前模型目的地。

**文件：**`standby/judge.py, observation.py, policy.py, service.py, ui.py`；现有模型 factory、usage 和 privacy owner 的窄扩展。

**测试：**严格结构、畸形 JSON、模型乱报 owner、timeout、重复/乱序结果、多目标、样本过期、预算竞争；工具/发送/TTS spy 必须位于真实执行边界。语义 fixture 与确定性程序测试分开。

**G3 验收：**SB-02、03、06、10、15–20、24 的影子子项通过；网络／费用提示真实；多模型比较不自动切换正式模型。用户尚未确认 ARMED 时，任何 UI/重连路径都不能开启动作。

## 10. S4 — 原生 Agent 升级与安静陪伴

**任务 S4.1：收窄的原生执行。**完成 `ExecutionDataPolicy` 在实际选用 Engine 全链路的落地；由 PAO 接纳有来源的待命工作，冻结精确 Workzone、工具清单、接收者与 data policy。无法 enforce 的 CLI/Engine 路径不开放。禁止创建一个有更宽权限的隐藏 Agent 来办事。

**任务 S4.2：自然对话窗口。**Judge 命中并通过政策后，进入可见窗口；复用选定 Agent Persona、语言和语音，不重复自我介绍/开场。空闲默认 30 秒、单窗口上限 5 分钟；追问走原生语音 admission/确认，不把直接呼叫当身份认证。

**任务 S4.3：语音呈现。**复用 FIFO、完整句流式、现有回声处理和 barge-in；只播放批准的输出。普通待命、影子和递纸条等待无 filler；只有确认交谈窗口使用已有本地化填充语。填充语不写历史、会议内容和 memory。

**任务 S4.4：取消/送达。**Standby pause/end 仅取消自身关联 Run；全局 `/stop` 遵守原生 Agent fence；barge-in 仅停播放，不取消后台工作。准确关联 PAO admission 不明的 request，不能用 Agent-wide stop 代替。输出前重验政策和事件时机。

**任务 S4.5：多人和披露。**普通环境声音不变成控制指令；只能通过可信界面扩大范围/改变隐私。文件可读、音箱可播、消息可发是三次独立许可；默认在已批准私密端点提示。

**测试：**实际 PAO/store/Engine adapters 配合受控 provider，检验退出后的 checkpoint/compact/历史不会恢复临时材料；两个独立 Run 下 pause 只停一个；播放在途撤权、旧 callback、Speaker-to-mic 回流；真实“叫名字→回答→追问→回安静”。

**G4 验收：**SB-04、05、12–23、25 的正式子项通过；至少一个明确声明的模型/Engine/客户端组合端到端合格。只收到 TTS 文件不算听到了声音，只有 provider response 不算消息已送达。

## 11. S5、S6 与外部客户端工作包

### 11.1 S5 — 会议记录与递纸条

**S5.1 分片与覆盖。**在现有 batch STT 上新增原生 bounded chunk 管理，持有 `track_id/segment_id/revision/capture_range/final`；起始建议 10–20 秒语音块、必要时小重叠，由设备和 ASR 资格确定，不声称现有接口已是 streaming STT。重叠对齐后合并，重复讲话不能只按文字相同删除。记录确认后的最终片段；无远端音轨时显示不完整。

**S5.2 工作稿和保存。**笔记工作稿默认结束后 24 小时；完整转写独立 OFF；已启用转写默认 7 天；保存笔记不等于保存原音。人工修改与 AI 补充有 revision/provenance，不能把用户修改当下一次模型覆盖的临时副本。

**S5.3 整理。**普通 end 无模型总结；finish_notes 先停采，再进行授权、预算内的有界整理 Run。长会议按已保存的段落分块，摘要保留原段引用和覆盖缺口；工作稿到期不能靠再次总结续命。

**S5.4 检索。**Judge 只判断是否值得找，原生 Agent 在冻结 Workzone 精确 roots 中检索；需要网页或外部系统时另验外发权限。无内容/权限/可靠来源即保持安静，不补造链接。检索结果是低信任材料，不可修改 profile。

**S5.5 递纸条。**保留 source refs、检索时间、相关议题 ID、有效期；提交前检查仍在讨论、内容是否值得展示、接收者/通道是否获准；主题过时或来源撤回就不打扰。非声学信息不能突然转音箱播报。

**S5.6 删除。**测试多来源摘要、转写、工作稿、保存产物、索引、引文预览、TTS 和 Engine 上下文的级联；普通结束不误删保存物，delete_data 不误删 Workzone 原件。

**G5 验收：**SB-06–09、18、20–25 全部对应场景，包含一次有本地＋远端音轨的实际会议与一次故意缺轨会议。影子检索建议通过不代替真实检索测试。

### 11.2 S6 — 分别验收专用模板

先上纯计时提醒：复用 Scheduler，不开摄像头、不调用模型判断时间。姿势模式是另一资格项，要求多帧／持续证据，不用一个快照判断“坐了 20 分钟”。

婴儿声音、醒来观察、访客停留分别列支持/不支持输入。没有已经批准的感知能力时，记录原生实现任务或清楚禁用，不临时引入外部完整产品。实验模板的音乐使用原生播放链，限定来源、音量、最长播放和停止；故障健康通知与业务通知分开授权。

不得识别人脸身份、诊断情绪或疾病，不自动解锁或采取执法行为。监护类实验只在有人监督的测试中验证，不能故意制造儿童危险或真实安防风险。

**G6 验收：**每个启用模板独立声明传感器、模型/版本、语言、样本和误漏报；缺信号必须可见，必要健康通知未送达不能记成功。未合格模板保持 unavailable/experimental，不拖低其他已合格模式保护。

### 11.3 客户端 F1–F5（外部仓库依赖，不在本轮修改范围）

| 工作包 | 阶段依赖 | 客户端要做什么 | 联调验收 |
|---|---|---|---|
| F1 · 控制/隐私界面 | S1 | 沿用紧凑媒体面板、主题、导航；展示数据流、期限、隐私/模式；一键停止 | 390×640、桌面、键盘；不靠颜色；未 start 无权限弹窗 |
| F2 · 媒体与租约 | S2 | 复用既有 Call 捕获/回声/播放原语，明确租约、track release、no offline spool | 双页冲突、Phone/Call 交接、锁屏/后台/断网、实际 OS/浏览器 track 证据 |
| F3 · 影子与最近记录 | S3 | 计划/事实/结果分栏；人工标签；无内容时不伪造原文 | 模式切换确认、指纹变化、滚动不跳动、过期内容不从缓存复活 |
| F4 · 对话窗口 | S4 | 有限窗口、语音队列、可停播放、caption 单一投影 | 自触发、追问、无重复开场、旧回调、不打断用户阅读 |
| F5 · 笔记与递纸条 | S5 | 笔记/完整转写独立设置；来源链接；保存/删除预览；低打扰通知 | 完整/缺轨状态、过时纸条不弹、人工修订保留、删除显示范围 |

外部客户端的实际文件名必须先从那个仓库当前代码核对；本方案不虚构已完成的前端源码审查。未来授权其实施时再登记 commit、构建与 renderer 版本，不把旧 Workbench 目录名当 HASHI 内部产品模块。

## 12. S7 — 集成、平台与发布

**S7.1 精确候选。**冻结 HASHI commit、客户端 commit/build、Function generation、模型/目标配置指纹、privacy/profile revision。每次修复只重跑受影响节点及依赖；最终 release candidate 运行 Testing Policy 要求的发布级范围。

**S7.2 8 小时稳定性。**同机同配置分别记录 idle、无目标待命、代表性活动，报告 CPU/GPU/RSS、网络、模型次数、队列峰值、缓存、用量及设备温度/能耗（有测量才填）。固定模型驻留量和异常随时间增长分开；每天费用不能只报 Judge 单价。

**S7.3 真实故障。**在授权测试环境制造断网、客户端结束、摄像头撤权、慢 Judge、磁盘满、清理重试、Worker 代次变更；设备实测不能自动扩为生产重启许可。指定范围之外的实例不碰。

**S7.4 回滚。**先关闭新开会话能力，终结/撤权活动，结清/标记未知动作与计费，完成待清理工作，然后采用经批准的旧 Functions。没有 standby 表识别能力的旧代次不能直接接管活动数据；清理组件需要保留或由受控迁移完成。不能通过恢复旧完整备份撤销删除记录。

**S7.5 发布。**分别给出 core experience（S4）、会议/递纸条（S5）、每个实验模板（S6）的支持矩阵。默认 OFF；首次 Shadow；上线不迁移用户 `/privacy`、Agent模型、Workzone、语音或记忆偏好。没有用户授权不 merge main、不部署、不 `/reboot`。

**G7 验收：**测试手册全部适用条目有证据；BLOCKED/SKIPPED 不计 PASS；存在隐私绕过、停止失效、重复外部动作、内容复活或 Call 回归时禁止发布。未支持平台明确声明，不以关闭测试换 green。

## 13. 施工中的提交、并行与停止规则

建议提交单元：C01 schema/fixtures；C02 control/store；C03 policy/disposal；C04 原媒体公共提取；C05 lease/ingress；C06 Judge/shadow/budget；C07 Engine data policy/admission；C08 dialogue/presentation；C09 meeting；C10 paper-slip；C11 experiments；C12 integration/docs。这是粒度建议，不要求恰好十二个 commit。

同一持久化 owner、共享隐私边界、Call service 或 command registry 同时只允许一位整合者写入。可并行准备测试、UI草图和文档，但不可让多个 Agent 各自造一套 lease、privacy cache 或消息状态。每个提交描述 owner、改动、红绿证据、未验证范围与撤回方式。

出现下面任一情况应停止相应路径、记录阻断，而不是叠加补丁继续：发现必须改 Protected Core；不能防止模型绕过隐私；无法定位内容所有副本；Engine 不能执行保留策略；外部动作结果未知；共享配置冲突；无法确认实例/用户/客户端；需要新增依赖或生产操作却没有授权。

历史 docs 中 main-only 的 Call 修复操作约束不授权此次修改 main；本用户明确指定 feature-standby，是本功能的分支范围。原 Call 产品合同继续有效，不借分支授权复活旧行为。

## 14. 本地实施 Agent 的开工清单与交付格式

以下只读取 Git，不自动切换、重置、stash、合并或重启：

```bash
git fetch origin
git status --short
git branch --show-current
git rev-parse HEAD
git log -5 --oneline origin/feature-standby
```

检查原工作是否干净且在正确仓库后，按当前用户授权采用 feature-standby。原 checkout 有工作时不强行切换；不要复制用户实例形成新环境，也不要凭目录名猜 HASHI2/3/4。

- [ ] 读 PRD v1.1、本方案、测试手册、AGENTS 与原生架构/测试/Call合同。
- [ ] 声明本次做哪个 S 阶段、哪个 owner、哪些文件；更早未过关的依赖不可跳过。
- [ ] 记录真实分支 HEAD、profile/fixture 指纹、现有失败；不把前人报告当本次测试。
- [ ] 先写能抓到缺陷的聚焦测试，再实现最小增量；不用文档 grep 证明行为。
- [ ] 所有敏感副作用前验证当前 policy、epoch、资源范围、数据策略和预算。
- [ ] 根据测试手册填写实际命令、结果、失败/跳过/未测原因。
- [ ] 只提交已完成且自洽的增量；不删除 PRD、不生成竞争权威文档、不夹带配置/密钥/媒体。
- [ ] 交接写清下一个具体任务，不用“基本完成”覆盖未做的前端/设备/隐私项。

每阶段交付：

```text
阶段／候选 commit：
实现范围与 owner：
真实新增/修改文件：
复用了什么、为何必须新增：
测试命令与红绿依据：
PASS / FAIL / BLOCKED / SKIPPED / NOT_RUN：
PRD SB-xx 对应证据：
数据流/保留/权限变化：
是否改配置／部署／重启：
客户端/构建/Worker/设备分别验证了什么：
剩余阻断与下个任务：
```

**本次文档交付只完成方案编制与源码抽查，不将以上任何实施关卡标记通过。**
