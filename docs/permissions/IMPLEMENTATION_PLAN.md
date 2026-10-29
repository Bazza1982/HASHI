# HASHI 权限系统详细实施计划

版本 **1.1** · **2026-10-23** · **设计与施工计划；实际实施进度见实施状态文件**

- 仓库：`Bazza1982/HASHI`；目标分支：`feature-permission-sys`。
- 冻结参考提交：`d6b6513973bfc8f9f22c40ddd8f72802f907baa0`。
- 产品行为以 [PRD](PRD.md) 为准；当前证据以 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md) 为准。
- 功能 owner：**PAO**；工程层：**Functions 与独立平台服务**。HERV3 管等待和续跑，FC 管现有协议投影与可信入口，PCM 只投影上下文和目录。
- 验证重点：L0 等价、一次准确批准、不会重复副作用、生命周期回收、同机 OS 隔离、Core / FC 合同不变。

本文给出可以逐包交给本地开发 agent 执行的顺序、算法和退出条件。源码映射见 [CODE_MAP](CODE_MAP.md)，FC 负载与认证见 [FC_CONTRACT](FC_CONTRACT.md)，Windows 细部见 [L2_WINDOWS](L2_WINDOWS.md)，完整用例与证据要求见 [ACCEPTANCE](ACCEPTANCE.md)。这些文档共同构成施工输入；不能只读一份概述就宣称完成。

## 1. 不变的施工约束

1. **只修改 HASHI。** 不改外部 Workbench、Telegram 专用 connector / adapter 或其他外部客户端。不为权限增加公开 FC 字段、schema version、action kind 或专用 widget。
2. **Protected Core 零变动。** 每批从 `orchestrator.runtime_contract.CORE_SOURCE_PATHS` 获取保护集合并运行既有检查。不得用 `--authorized`、环境变量、移动保护名单或修改基线绕过；本项目无 Core major migration 授权。
3. **PAO 单一权威。** 不在 HERV3、PCM、FC 菜单或模型消息中建立另一套可写授权真相。L2 时独立服务持有最终权限账本，HASHI 仅保留任务权威和服务投影。
4. **L0 原路执行。** 不采用“全部允许”的 PermissionService，亦不全量初始化服务后再判断模式。无字段和显式 L0 都保持原工具、prompt、调用路径、生命周期和旧防护。
5. **临时权限归任务。** Once / Run / Task / TTL 都受任务提前结束和撤销约束；普通审批无永久选项。用户主动修改长期基础配置是不同的管理动作。
6. **缺权可询问，获批即续跑。** 申请工具本身不需要先取得业务访问权限；批准不是一条新用户 Message，不创建替代 Run 或重做已完成操作。
7. **后端兼容不硬阻断。** 手动选择或已配置 fallback 的 backend 按原选择运行，准确提示实际覆盖。`approval_required / denied / revoked` 不触发自动换后端规避。
8. **不虚报落地。** 源码实现、构建产物、运行中采用、平台安全测试、真实前端检查分别记录；未验证的 Windows 边界只能标待验证或部分受控。

历史文件名 `orchestrator/workbench_api.py` 指 HASHI Backend API。允许在它所属 Functions 中做公共入口认证接线；这不要求外部 Workbench 改动。正常功能采用通过现有 hot adoption，不更换正在运行的 Core，也不向 Core Python 安装新依赖。[架构依据](CODE_MAP.md)

## 2. 依赖、里程碑与可并行工作

| 编号 | 交付包 | 依赖 | 必须完成的可审查结果 |
| --- | --- | --- | --- |
| M00 | 冻结基线、权限入口清单、Core/FC 边界 | 无 | 源码事实、L0 比较样本、测试选择、实施状态起点 |
| M01 | 领域类型、配置、L0 真旁路 | M00 | 类型与协议接口、版本化设置、L0 等价证据 |
| M02 | 权威账本、事务 CAS、用权与恢复 | M01 | 可独立测试的权限服务、迁移、outbox 与幂等 |
| M03 | `/safety`、标准 FC、可信决定 | M01、M02 接口 | 所有权限写统一经过认证和 revision 绑定 |
| M04 | 工具执行门与全通道声明 | M01、M02 | 排队后 claim、资源绑定、取消与回执契约 |
| M05 | HERV3 等待/续跑与完整生命周期 | M02、M03、M04 | 同步骤恢复、跨 Run、quiesce、代际清理 |
| M06 | L1 全闭环、迁移、后端覆盖 | M03–M05 | 可交付的 L1，L0 回归和未受控范围说明 |
| M07 | L2 服务、身份、审批桥、窄 broker | M02 合同；M06 稳定语义 | 独立权威、安装 ACL、可信 IPC 和生命周期 |
| M08 | 受限 HERV3 host 与资源代理 | M07 | 真正替换活动执行路径，文件/网络/凭证/进程隔离 |
| M09 | L2 撤销、故障、恢复、Windows 验收 | M08 | OS 对抗和故障证据、能力发布矩阵 |
| M10 | 发布、采用、回退与移交 | 对应发布级别全部退出条件 | 合格构建、升级/回退、部署证据与遗留项 |

M03 的渲染、M04 的声明目录可在 M02 接口确定后分别安排；只读研究可并行，实际源码、测试与文档修改必须保持单一写入者；L2 平台 spike 可以提前独立验证 Windows 能力，但不得反向改变 L1 产品语义。L1 允许先完成一次 M10 发布；L2 随后独立完成 M07–M10。不因第三方 CLI 不可完全管理而拖延原生 HERV3，也不以“后续再补”省略本次要求的 L2 施工。

每个里程碑建议独立提交，包含相关实现、必要行为测试、拥有该决策的文档与状态记录。不得把 L0/Core/FC 不变量推迟到最后检查。

## 3. M00 — 基线与工程边界

### 3.1 执行步骤

1. 阅读仓库根 `AGENTS.md`、`ARCHITECTURE.md`、分层边界、测试政策、命令 UI 风格；读完本目录全部文档。确认 checkout、当前 branch、HEAD、工作区改动。保留已有用户工作，仅在指定的 HASHI3 及获准分支施工；创建新分支或额外 worktree 须有当前用户明确授权，不得通过脱离分支提交绕过，不能 reset 覆盖；同一仓库保持单一写入者。
2. 从目标 branch 开始；若 HEAD 已有后续提交，先记录 drift，重新核对下面代码落点。不要自动把设计固定 SHA 当必须 reset 的目标。
3. 执行 `python3 scripts/check_protected_core_changes.py`，并保存保护集合来源和结果；不在实现代码中复制清单。
4. 核对活动调用链：`adapters/her_v2.py` → `HERv2Runtime._run_turn / _run_direct` → `HashiStageProvider.invoke` → 实际 provider loop → `ToolRegistry.execute`。证明不是仅修改已退居兼容的 coordinator / `Stage.EXECUTION` wrapper。
5. 枚举所有副作用入口：文件、进程、shell、网络、浏览器、桌面、业务 connector、模型 provider、后台、scheduler、子 agent、skill 间接调用、恢复和重试。每项记录现在的函数、身份来源、资源、幂等、取消及是否经过 Registry。
6. 核对 FC 的 registry、菜单/action、Session command、HTTP admin、Remote relay、Telegram 共享回调与 TUI。记录谁只提供 instance proof、谁能证明用户，禁止把现有 owner 字符串当认证。
7. 形成 L0 基线 fixture：相同配置下的模型输入结构、工具列表/schema、admission结果、执行调用序列、生命周期与 fallback。使用确定性 provider/tool stub；不要要求真实模型文本逐字重现。
8. 列出已有测试可复用处与必须新增的行为测试。现有文件以 [CODE_MAP](CODE_MAP.md) 核查结果为准；本计划中的 `test_permission_*` 均为建议新增文件，不声称仓库已有。

### 3.2 产物与退出

更新本目录状态文件中的基线、drift、入口覆盖表和测试选择；不存在“完全支持”但找不到执行点的通道。Core guard 通过；L0 基线可独立复现。若出现 Core 修改诱因，先在 Functions、adapter 或 sidecar 范围重新设计，完成其他授权工作，不擅自迁移 Core。

## 4. M01 — 类型、配置和 L0 真旁路

### 4.1 模块与依赖建议

新增模块均为候选路径；最终名称可遵循仓库习惯，职责不变：

| 模块 | 唯一职责 |
| --- | --- |
| `orchestrator/permissions/models.py` | 不可变领域类型、状态、稳定错误/结果分类 |
| 规范化 | 当前由 `models.py` 实现；如需拆分则迁移唯一实现，不复制状态 |
| `interfaces.py` | PermissionAuthority / Enforcer / TrustedDecision / Recovery 合同 |
| 账本与迁移 | 当前 `authority.py` 有基础事务和 outbox；持久迁移及恢复待实现 |
| 权限服务 | 当前 `authority.py` 有部分决定与 claim/start；派生授权及真实入口待实现 |
| `task_binding.py`、`lifecycle.py` | PAO 任务归属、epoch、终结与精确代际接线 |
| `continuation.py` | PendingOperation、等待、恢复计划、回执消费 |
| `trusted_decisions.py`、`fc_bridge.py` | Principal、receipt、标准 FC 投影和统一权限写门 |
| `enforcement.py`、`backend_capabilities.py` | L1/L2 实现选择、实际覆盖与提示去重 |
| `tools/request_access.py` | 三个 agent 工具的薄适配，不拥有状态 |

共享服务在 `manager_registry.FUNCTION_MANAGER_SPECS` 对应 Functions 内按需接线。不要仅因添加 manager spec 就在所有 L0 worker 构造 SQLite、连接 service 或导入 Windows native 模块。可使用懒 facade，但 L0 执行分支不得调用它；Core 不能导入权限产品模块。

### 4.2 类型与身份

请求对象统一命名为 `AccessRequest`；`PermissionLease` 与 Grant/Lease 指同一有效授权记录，`ApprovalDecision` 是其不可静默改写的决定依据。不要分别造两套表。

可信执行上下文至少含：`instance_id / owner_id / agent_id / permission_task_id / run_id / session_id / turn_id / worker_generation / context_generation / execution_epoch / backend_id / executor_id / policy_revision`。L2 增加 `service_epoch` 和连接身份。字段由 PAO/可信 supervisor 注入；模型 schema 不接受这些身份、批准者、有效 lease 或 approved 标记。

| 标识 | 改变条件 | 不应表达的事 |
| --- | --- | --- |
| `run_id` | PAO 接纳新用户 Message | Worker PID、service 重启、按钮点击 |
| `permission_task_id` | 新目标或旧任务已终结后新工作 | 每个 Turn 都新建 |
| `worker_generation` | 现有 Functions generation 更换 | 任务权限寿命 |
| `context_generation` | 现有 session 上下文代次变动 | service 运行代次 |
| `execution_epoch` | 执行绑定替换/切后端/恢复 fencing | 自动延长批准 |
| `task_revision` | 任务事实 CAS 更新 | 单调运行时间 |
| `request_revision` | 用户待决定的范围/意义发生变化 | 菜单翻页或刷新 |
| `policy_revision` | 用户管理的安全配置变化 | UI 菜单 revision |

### 4.3 配置读写

1. `requested_level` 缺省严格等于 L0；读取旧配置不写回。新建 agent 未明确选择也为 L0，推荐 L1 通过 `/safety` 现有命令呈现即可，不改外部创建页面。
2. 设置归既有 owner/agent 配置，采用 `orchestrator.config_json` 的 revision CAS；冲突返回当前状态，不盲目重试写入。权限租约永不写入 allowed_tools / workzone / 环境变量。
3. 统一存储产品默认：任务授权建议 4 小时、申请 24 小时、日志 90 天；范围/时长上限由管理配置确定。L2 ticket 60 秒、heartbeat 5 秒/3 次、cleanup 目标 5 秒是独立参数。
4. `/safety set-level <L0|L1|L2>` 与基础范围调整复用可信管理写门。agent 不能通过工具切低等级；已有精确用户指令不再加无意义二次确认。迁移涉及实际范围时展示准确范围供用户决定。
5. 显式等级配置与 `actual_controls` 分开；后者是带观测时间、服务代次和能力版本的投影，不能让客户端写入。

### 4.4 L0 选择逻辑

```python
# 示意代码：只表达分支，不要求复制函数名。
if existing_agent_config.requested_level_or_default() == L0:
    return await legacy_execute_with_existing_controls(call)

ctx = trusted_pao_permission_context()
authority = lazy_permission_authority_for(ctx)
return await managed_execute(call, ctx, authority)
```

L0 分支在构造新上下文、扩展工具目录、加等待包装和服务连接之前选择。`/safety` 作为新用户命令可供主动查看，但默认不进入模型目录或消息。多 agent 混合配置不能改全局 executor、代理、ACL、credential 环境；L2 服务卸载或不可用不能让 L0 崩溃。

**退出条件：** 无字段/显式 L0 的 fixture 与冻结基线相等；新服务被配置为一调用就失败也不影响 L0；既有 privacy/enterprise 限制仍生效。对应 `CASE-L0-*` 和 PRD `LEG-01..08`。

## 5. M02 — 权威账本、事务与恢复

### 5.1 存储布局

L1 在 Functions 受管理的数据位置保存独立权限账本；L2 在安全服务受保护目录保存。沿用仓库可用的事务存储实现，首选已有 SQLite 栈；迁移保持可备份与 schema version。避免将全量配置 JSON 当高频授权数据库。无 L1/L2 agent 时不创建账本。

下表是逻辑 schema，具体 SQL 需遵循项目存储模式；字段不得因简化而丢失约束：

| 表 | 关键字段与约束 |
| --- | --- |
| `permission_tasks` | task PK；owner、agent、session、goal digest、state、revision、terminal reason、created/ended；终态不回 active |
| `execution_bindings` | epoch PK；task/run/worker/context/backend/executor/service；active/fenced；不能由旧退出事件覆盖新 epoch |
| `access_requests` | request PK、task、operation 可空、current revision、created/expires、state、dedup key；当前等价 pending 唯一 |
| `request_revisions` | `(request_id, revision)` PK；不可变规范化 scope、scope digest、purpose、可选寿命和原始内容 hash |
| `approval_decisions` | decision PK；request/revision 唯一最终决定；receipt nonce 唯一；approved subset、duration、principal/source、commit time |
| `permission_leases` | lease PK；decision、subject/task/run/operation、scope、parent、effective deadline、status、policy revision、fence revision |
| `pending_operations` | operation PK；task/run、工具、加密或受限保存的参数、args digest、scope、continuation、state、attempt；跨传输不换 ID |
| `execution_claims` | claim PK；operation+attempt 唯一；epoch、lease refs、prepared/started/settled、deadline、start/fence version |
| `operation_receipts` | operation+attempt、executor、start/finish、success/failure/unknown、external idempotency/result reference；不可凭覆盖消除 unknown |
| `execution_resources` | resource PK；task/operation/epoch/job/handle/proxy/session 归属、running/stopping/stopped/unknown、最后确认 |
| `decision_receipts` / 登记表 | 受保护 issuer、nonce、body hash、请求/版本/scope/choice、principal、expiry、consumed-by；不可由普通 worker写 |
| `permission_events_outbox` | 单调 event sequence、owner/task/operation、event、body reference、提交时间、投递游标；与业务事务同提交 |
| `idempotency_records` | `(owner, method, key)` 唯一、input digest、原结果引用；同 key 不同参数报冲突 |
| `authority_metadata` | schema/service/recovery epoch、最近可信时间、能力版本；不单独承担防整机快照回滚证明 |

外键和唯一约束落实到数据库；enum 和 deadline 校验同时存在于领域层。审计默认最小信息，不保存 raw secret、完整文件和无关对话。敏感 PendingOperation 参数存受限位置，模型只收到必要摘要。

### 5.2 状态与合法迁移

- Task：`active ↔ paused`，`active → waiting_permission → active`，恢复进入 `recovering`；最终为 `completed / cancelled / failed_terminal`。可恢复工具错误不结束 Task。状态可用主状态加子状态实现，避免新增公开 Run enum。
- Request：`pending → decided / denied / withdrawn / expired / superseded`。一次部分批准形成不可变决定和 subset Lease；剩余需求通过关联的新 revision/request 明确表示，不可原地扩大已批准决定。
- Lease：`active ↔ suspended`；最终 `consumed / expired / revoked`，不复活。恢复允许基于仍有效的决定创建新的执行绑定，不改旧 revoked Lease 为 active。
- Operation：`planned → waiting_permission / queued → prepared → started → succeeded / failed / unknown`。只有可信证明尚未开始时才可释放预占并重试；unknown 必须先对账。
- Resource：`registered → running → stopping → stopped`；超时为 `cleanup_failed / unknown`，不能与 Lease 的 revoked 合并。

任何迁移都检查 expected revision、当前 epoch 和终态；终止写、批准写、启动写必须具有可定义的线性化顺序。

### 5.3 规范化与 subset

1. 文件资源拆路径输入和执行时对象绑定；在 Windows 处理相对路径、大小写、盘符、UNC、junction/reparse、创建父目录与对象替换。仅字符串前缀检查不合格。
2. scope 是资源身份、动作集合和参数上限的确定性序列化；排序去重并标 `normalization_version`，计算 digest。版本升级不能悄悄解释旧批准为更大范围。
3. 子范围验证按类型实现：文件对象/后代、网络 scheme/host/port 与 redirect 规则、凭证用途、业务目标。无法证明 subset 时重新申请，不猜测。
4. 网络连接包含发送能力；脚本不能通过“只允许 GET”宣称绝无外传。已知结构化 fetch/upload 仍分别授权；实际能力见报告。
5. 操作参数摘要包括对副作用有意义的目标、内容摘要、工作目录、执行器与模式；展示文案变化不改变执行含义，目标改变必须使旧 once 绑定失效。

### 5.4 申请算法

输入为 native `request_access` 的业务范围/理由/期限建议，加可信 PAO 上下文。先执行静态合法性、可申请上限和既有明确禁止规则；无权批准的能力返回 `unavailable/denied` 及可继续建议。

在事务内：

1. 校验任务可提出需求、当前主体及 epoch；读取有效基础配置和 Lease。
2. 所需范围已经覆盖，返回 `granted_existing`，不再通知用户，不消费 once。
3. 计算未覆盖范围和去重键：owner/agent/task、normalized scope、必要 operation binding、request revision。已 pending 返回原申请。
4. 同范围先前被拒且条件未变时返回原拒绝，不刷屏；明确用户重提、范围缩小或申请已过期时，按规则创建关联新版本并展示变化。
5. 保存 Request、必要的 PendingOperation 和 outbox；提交后投影标准 FC。投递失败不删除申请，查询可以重建。

主动申请可以没有 operation；once 选项只有已有确定操作时才可用。主动申请获批恢复规划，拦截申请获批恢复绑定操作。

### 5.5 决定算法

`decide(receipt_ref, request_id, expected_revision, choice, idempotency_key)` 仅由可信用户门使用。验证 receipt 来源、原始 body/action mapping、用户/实例/任务/session/context、nonce、期限与 scope digest；客户端字段不能制造 receipt。

在单个决定事务内：重新校验 request revision、Task 非终态、scope subset、duration 合法、policy ceiling；消费 receipt、记录不可变 ApprovalDecision、建立 Lease、写 outbox 和幂等结果。相对期限从此事务提交的可信时刻起算，实际截止为用户选择/策略/必要父授权的最早期限。

相同决定重投返回原结果；同 revision 已被不同决定处理时返回真实现状，不生成第二 Lease。暂停任务允许记录有效批准但不启动；已取消或完成任务不再发权限。跨端 action、文本、admin alias 最终共用此算法。

### 5.6 用权、撤销与 dispatch 的顺序

内部合同建议为 `evaluate / request / decide / prepare_execution / claim_and_start / complete_operation / revoke / settle_task / get_effective_controls`。`evaluate` 无副作用、无计数。可合并 prepare 与 claim，但不能合并掉真正开始时的检查。

执行步骤必须如下：

1. Registry 先保留既有 admission，再 evaluate 缺少范围。需要批准时记录 continuation 并退出资源锁；不持有设备锁等人类。
2. 获批后进入既有资源排队。**排队结束后**重读任务、epoch、policy、期限、Lease 和实际资源绑定。
3. 在 authority 的 task/operation 执行栅栏内原子预占 once、登记准备状态及资源；最终 start 只在准备完成后提交一次。使用 SQLite 等存储时定义与 revoke 的事务顺序，网络业务期间不持有全局 DB 写锁。
4. 若平台有准备阶段，先建不能产生业务副作用的 suspended runner/未连通代理，加入 supervision；最终 `commit_start` 重验，再在同一受控栅栏内允许业务执行。仅取得票据或处于队列中的动作仍是未开始。
5. revoke 若先提交，start 必须失败并清理 prepared 资源；start 若先线性化，revoke 必须能找到已登记资源并中止/收尾。不得出现“数据库写 started，但未被监督的进程稍后无条件启动”。
6. 操作完成写持久回执；传输重试读取同一 operation。若开始/提交结果不确定，标 unknown 并对账，不为继续任务换新 ID 重做副作用。

`ExecutionTicket` 不是脱离账本即可离线使用的通行证。60 秒期限不能成为撤销后仍允许新启动的理由；票据还绑定 task、operation、executor/connection、epoch 和 policy。执行器收到丢失/延迟 revoke 推送时，实际 start 的权威查询仍拒绝。

### 5.7 恢复与时钟

持久 UTC 用于显示与审计，进程内使用 monotonic deadline；服务重启依据受保护的时间高水位和当前时刻保守校验，不因回拨增加剩余寿命。显著异常不得自动延长授权，可暂停受影响动作并说明。

正常重启先 fence 旧 service/execution epoch、对账活动资源，再恢复仍有效的 Task 决定。恢复旧账本需依据独立恢复 epoch / 撤销记录核查；缺乏证明时使导入的活动授权失效、保留成果和历史。**整机完整快照回滚无法仅凭同机一起回滚的数据完全识别**；L2 文档明确此范围，不写虚假的保证。

**退出条件：** 并发决定、once 争用、revoke/start 顺序、回拨、备份恢复、事务断点和 outbox 重投测试通过；L0 不触达新账本。见 `CASE-L1-* / CASE-LIFE-*`。

## 6. M03 — 标准 FC 和可信决定

### 6.1 命令与界面

新增共享 `orchestrator/commands/safety.py`，遵循既有 RuntimeCommand/RuntimeCallback 注册，参数从一份目录派生。实现 `/safety status / requests / request / approve / narrow / deny / withdraw / revoke / set-level`，详情见 [FC_CONTRACT](FC_CONTRACT.md)。生命周期动作如暂停/停止保留既有命令；权限清理在 PAO 接线，不另造新生命周期入口。

菜单、分页、文案使用既有 command UI helper 与 locale catalog。将资源及动作从规范化事实渲染，模型 reason 只是单独的解释字段。标准按钮绑定 server-side request revision/scope；文本端给出含 `--revision N` 的完整可复制命令。有按钮可点击，没有按钮可走同一语义的命令，不要求前端新 schema。

用户选择默认突出“本任务，最晚 4 小时”，不默认提交批准；一次、Run、TTL、收窄、拒绝均可达到。一次批准不弹第二次确认。部分选择通过现有多步菜单/文本构造准确 subset，不能假定旧客户端有 checkbox/form。

### 6.2 认证接线顺序

1. 在 HASHI Backend API 的现有公共入口、共享 admission/bridge 和可信 Telegram 来源上下文中分辨真正的用户身份；**不要只匹配字符串 `/safety`**，opaque `act` 须先解析服务器登记 action 才知道是否权限写。
2. 对权限写使用既有可信 session/认证机制建立私有 Principal。`_v1_owner_id` 在 personal 模式只给出配置 owner 的路径不得生成批准证明。严格鉴权仅针对新权限写及必要管理动作，不重写所有 L0 普通命令行为。
3. 保存原始认证请求或已发行 action 的不可变关联，生成短时、单次、绑定 exact scope/decision/duration 的 DecisionReceipt。共享 Functions 之间可传受保护记录引用或专用 IPC，不能把证明塞进公开 FC payload。
4. 权限服务最终验证；Worker 即使能请求一般管理 RPC，也不能签发 receipt 或使用不属于当前决定的引用。
5. 盘点 admin token、Remote HMAC、bot token、session cookie 的读取与使用者。共享给 agent 的 secret 无法证明人类意愿；对 L1 如实说明信任域限制，对 L2 必须迁入隔离控制平面，受 L1/L2 管理的执行域及新批准通路改用不含批准能力的窄代理。L0 既有凭据与工具行为不静默重配；混合 L0 破坏批准信任时隔离新的控制平面，无法隔离则报告 partial。
6. 已知 TUI direct/proxy 的认证缺口保留为能力事实。该连接普通工作继续，申请不丢失；权限写指向另一已连接可信 FC。没有可信连接则保持待批，不能伪造 owner 或静默降到 L0。

### 6.3 过期、镜像与重连

MenuStore 默认 15 分钟且只在内存；不改其公共合同来存 24 小时申请。菜单失效且 AccessRequest 仍有效时重开同一持久申请；申请本身已过期而需求仍有效时，agent 可在原 Task 下创建关联新申请，保留 PendingOperation / operation_id，不延长旧 TTL 或复活旧批准。关闭菜单只关闭表示。HTTP request_id、menu revision 和 permission request revision 分开。多端状态查询回读 authority；事件刷新仅复用客户端现有能力，离线/不支持推送时下次操作返回准确状态。

普通 `ask_user` 继续只有 clarification/preference；不得新增 `purpose=permission` 来让普通答案授权。自然语言可以帮助导航或生成精确命令，但最终写授权只走正式门。

**退出条件：** 所有权限写入口（包括 act/admin/relay/文本）无法绕过 receipt；旧卡/跨 owner/跨连接/伪造来源/重投正确；既有 FC schema 校验无变化；无外部或 Telegram 专属改动。见 `CASE-FC-*`。

## 7. M04 — 执行门、资源声明和工具覆盖

### 7.1 Registry 的最小修改

现有 `ToolRegistry.evaluate_admission` 承担旧工具/请求范围/enterprise 检查，`execute` 在 `execution_resources.tool_resource` 等待前调用 admission。[固定源码落点](CODE_MAP.md)

实施时保留旧检查顺序和 L0 分支；同步 `evaluate_admission` 不执行新的 authority I/O，L1/L2 的异步 evaluate 在 `execute` 中紧随静态 admission。claim/start 放在 resource context 成功获得后、业务函数真实 dispatch 前。设置可注入 barrier 的行为测试，精确重现“已 evaluate → 排队 → revoke → 获得资源”。第二次检查拒绝时退出 context 并进入恰当状态，不消费 once，不把缺权作为普通工具异常。

能力未批准仍能在可申请目录被发现。只将“不提供/策略不允许”与“需要用户批准”区分清楚，不通过隐藏全部工具破坏 agent 提问。申请/查询/提前释放工具属于控制功能，不要求先授予所申请资源。

### 7.2 每个执行适配器的描述

| 字段 | 要求 |
| --- | --- |
| `describe_requirements(call, trusted_context)` | 确定性的资源/动作/参数摘要，不能调用副作用 |
| `prepare_resource_binding()` | 执行时真实文件对象、网络目的地、账户/仓库/设备等绑定 |
| `start_mode` | 原子本地、受管理进程、结构化网络、外部异步；明确定义何时算开始 |
| `idempotency` | 原生幂等键、可查询结果或必须标 unknown；不能无条件 retry |
| `cancel / reconcile / cleanup` | 可取消范围、外部状态查询、资源清理及可观察确认 |
| `control_report` | L1 app check、L2 OS boundary、partial/unmanaged 的具体依据 |

复合工具计算动作并集，但 Lease 只在相同 task 主体内组合；任一必要能力缺失则不能执行整个复合动作。读目录不包含写入或上传；shell 的 argv 解析只能辅助展示，不能证明资源安全。

### 7.3 通道逐项交付

1. **文件：** structured read/list/write/move/delete 先做稳定资源绑定；写入原子替换与新建父目录检查；验证 traversal、链接替换和 UNC。不要在检查后把原始不受控路径再次交给高权限 open。
2. **Shell / Python / skill：** 记录运行描述、输入输出、网络、credential需求、后台行为；L1 无 OS 围栏的现有 shell 必须明确为宽权限例外。若用户批准，边界标 partial；不能宣传严格 workzone。L2 仅受限 runner。
3. **网络与业务 connector：** 明确 host/port/redirect、收件人/账户/仓库/动作；可复用业务 idempotency key 时绑定 operation。凭证按动作代理，避免给模型/任意脚本完整 secret。
4. **模型调用：** 已选 provider 是已声明的数据出口；在受控路径里以统一代理完成，保留既有 `/privacy`。用户切换后端不自动批准新的敏感数据去向。
5. **设备/浏览器/桌面：** 人类授权与 CapabilityBroker 的占用租约分别满足；等待用户时不占设备。能控制审批桌面的路径不能标完整 L2。
6. **后台/定时/子 agent：** 创建时登记独立任务或派生关系，实际启动再验权。回收目标是进程/会话/连接，不能只删除 scheduler 行。
7. **未知插件：** L0 保持原行为；L1/L2 明确未知/不可用/宽范围例外，不能靠旧 wildcard 自动认领所有新副作用。补元数据前不得标已强制覆盖。

**退出条件：** 所有活动入口有 owner、descriptor 和控制报告；不存在 alternate direct executor 绕过。每类选择能证明其边界的行为测试，避免给每个简单函数机械复制测试。见 `CASE-L1-* / CASE-L2-*` 及工具覆盖表。

## 8. M05 — HERV3 连续循环、等待和生命周期

### 8.1 Task 归属

PAO 在接纳新 Message 时决定关联现有未完成 PermissionTask 还是创建新任务。显式补充/恢复可关联；已终结任务的新工作必须新建；语义不明时保守新建，不自动借旧授权。模型可以提供分类建议，不能自行指定 owner 或让无关工作继承 Lease。

HERV3 每 Turn 的 `HERTaskState` 不承担跨 Run 权限寿命。通过可信执行上下文注入 task/epoch，session/turn ledger 只关联 operation、receipt 和 continuation 引用。

### 8.2 内部等待结果

在 native HERV3 活动路径新增明确的控制结果，如 `PermissionWait(request_id, operation_id?, continuation_ref, reason)`。名称可调整，但必须区别普通 `is_error`、provider故障、业务失败、终止回答和工具超时；OpenRouter/Hashi API 等实际 provider loop 都需覆盖。

可借鉴现有 terminal question guard 的无模型流量等待，不照搬 RunQuestion TTL/Run绑定。保存恢复位置后：

1. 独立且已授权工作仍可进行；没有独立工作时由 Functions 事件等待，不让模型反复 status 查询。
2. 等待不增加 cognitive stall、无进展计数、工具重试或 fallback 次数；Agent Companion 显示真实状态。
3. 事件仅通知可能有变化，醒来重读 authority；丢事件可由有界服务状态 reconcile 修复，不靠模型轮询。
4. 批准后用原 operation 与参数摘要恢复。旧 backend/epoch 绑定不能直接执行；当前 Task 仍可运行且新绑定重验通过时，可以原 operation_id 自动恢复。只有范围、目标或操作含义实质变化才需新增申请或重新规划；暂停/停止/终态不被批准唤醒。
5. 部分批准后只有所有必要 scope 满足的 operation 可运行；拒绝保留成果和替代路径，不重复同一拒绝请求。

### 8.3 等待期间 hot adoption

当前 Functions host quiesce 观察队列、`is_generating` 和后台任务；因此“Run一直running”等待不能无限卡住排空。[现有接点](CODE_MAP.md)

新增 park/drain 合同：将 PendingOperation、工具调用关联、已消耗回执、恢复位置和 Task wait 原因提交成功后，解除 generation 的活动占用；逻辑 Run/Task 仍由 PAO 持有。新 Functions generation 加载后先创建新 execution_epoch，再验证恢复计划。不要序列化 Python 栈；不要伪造用户 Message；旧 worker 醒来必须被 epoch fence 拒绝。

### 8.4 生命周期接线表

| 现有事件 / 接点 | 新权限动作 | 保留的旧行为 |
| --- | --- | --- |
| Run completion / `release_capability_task` | 结束 Run-scoped Lease；Task 仍未完成则保留有效 Task/TTL Lease | 现有设备 request 释放；不把 request_id 当 task_id |
| Task 完成/取消/终止失败 | CAS 终态、fence全部新执行、撤销本任务直接及派生PermissionLease、取消待批申请、清理资源 | 交付成果可先返回，清理单独追踪 |
| pause/resume | 冻结执行、失效票据；批准依据不延期；恢复重验 | 不等于任务终结或用户停止 |
| `AgentLifecycleManager.stop_agent` | 先fence，取消待批申请并终结活动任务授权，再quiesce/关闭；失败仍不恢复业务权限 | start 只恢复可用性，不重做用户已停任务 |
| `cancel_session_run` | 用锁/版本保证取消先于晚批准恢复 | 原 Run取消语义保持 |
| `FunctionWorkerSupervisor.handle_worker_exit` | 只回收退出的 worker_generation/execution_epoch | 不能照抄现有按 agent 名清设备的顺序 |
| backend switch | fence旧epoch、停止/对账旧operation，绑定新backend | 用户选项继续生效；未受控原生路径真实报告 |
| UI断线 | 不终结Task；按原时限继续 | 手机后台/浏览器关闭不等于停止代理 |
| clone/fork/import/change owner | 只复制允许复制的内容 | 不复制lease、receipt、epoch或执行句柄 |
| archive/delete | 先完成或可靠托管清理，再移除活动身份 | 保留最小审计与未清理资源追踪 |

后台工作若需超过父任务，必须先建立可见独立任务及明确批准。派生授权的范围、期限与subject均不超过父能力；父撤销沿依赖图传播。已独立获批子任务按自己寿命结束，不能与派生lease混淆。

### 8.5 恢复判定

恢复时三分：尚未开始 → 重验后执行；已完成 → 复用回执；结果未知 → 先对账或请求用户针对结果的决策。结果未知不是缺权限，不能用再次批准当作重复发送授权。恢复新epoch可以复用仍有效Task批准；Run-scoped能力只在原Run仍有效时适用，绝不因重启自动延长时限。

**退出条件：** 普通等待不烧模型、不误触发AC；正常批准同操作续跑；pause/stop/switch晚批准不复活；hot adoption不挂住；旧worker退出不撤新worker权；Run终结不误撤Task。见 `CASE-LIFE-*`。

## 9. M06 — L1 完整闭环与兼容迁移

1. 选择一项真实文件任务跑完“发现缺权 → 一条标准申请 → 一次准确批准 → 原步骤续跑 → Task完成 → 撤权与清理”；同任务多次访问、跨Run补充均不重问。
2. 补齐全部活动工具覆盖表，逐项写 app check / partial / unavailable。只有 UI、账本或 shell wrapper 不算 L1 交付。
3. 在 personal 和 enterprise profile 下验证组合：新系统不因personal快捷路径跳过；旧明确deny不被普通lease覆盖；已有privacy保持。
4. 后端控制报告由实际adapter/service观测生成，含版本/epoch/有效期。对新后端计算控制差异，一次说明具体缺口；详细信息通过`/safety`查询，不给L0普通Run插入新提醒。
5. Switch到不受管CLI时准确告知并继续；HASHI自己的工具仍执行当前规则。禁止把denied/revoked/waiting归类为provider故障触发fallback。切回HERV3重验有效Task授权，无需全部重批。
6. L0→L1明确基础workzone/工具/凭证范围；不把历史全盘访问包装成严格限制。L1→L0先fence旧受控执行，处理相同operation的unknown，随后原路执行。迁移失败不破坏成果或写半份设置。
7. 执行L0与FC回归、并发和默认性能场景。记录机器、负载、冷/热状态；10ms/50ms/1s是目标，不是未经测量的成绩。

**退出条件：** L1产品AC与相关CASE全部有实际证据或明确阻塞；不把关键缺口列“已完成”。L2未安装不影响L1/L0。之后可做一次M10的L1发布，不代表L2完成。

## 10. M07 — 独立 L2 服务、身份与窄 broker

详细逐包输入、OS ACL和平台用例见 [L2_WINDOWS](L2_WINDOWS.md) 的W01–W12。主拓扑唯一：现有可信PAO/FC Functions控制平面，新增受限HERV3 host，独立Windows授权服务/执行broker/监督进程。不要把现有普通multiprocessing worker加环境变量就宣称隔离。

### 10.1 服务与安装单元

1. 将平台依赖放独立构建/环境，如`services/permission_service/`及`runners/permission_host/`；协议客户端保持轻量，Core环境不安装native依赖。
2. 服务代码、父目录、配置、数据库、密钥、日志、更新包与DLL搜索路径均由OS ACL保护。agent不能改写/替换/删除、取得owner或写DACL；服务SCM stop/config/delete与process/token权限同样拒绝。
3. 建立安装/诊断/升级/卸载流程，明确只由可信用户管理。不在agent的可写workzone运行安全服务；任意脚本不进入LocalSystem。
4. 可信FC审批桥及其输入、scope快照、issued action、receipt signer处于同样受保护控制平面；不能只保护最终DB，留下可篡改审批内容或签名接口。
5. 初次启用握手验证协议版本、服务代次、runner能力、隔离凭据和健康；失败提供具体原因，用户可继续选L1/L0或兼容backend。

### 10.2 方法与连接身份

分离agent通道与human/admin通道。Agent只可请求自身Task范围、查询、提前释放自身Lease及执行有限operation；不可approve、change_level、service_control、任意route或代签决定。IPC显式ACL，连接绑定受信host/epoch/task，不信任JSON里的PID/agent名或localhost。

模型网络、业务连接器和需要可信宿主代办的功能均走窄broker的逐方法schema和scope检查。受限host不获得legacy wide Functions pipe、admin token、bot token、Remote sharedsecret或任意环境继承。受信上游也不能成为代理越权通道；服务验证谁在代表哪个task执行哪个operation。

**退出条件：** agent身份不能读写授权权威或停服务；能正常申请/查询；宽RPC、伪造owner、receipt重放被拒；普通L0/L1未受安装副作用影响。对应W01–W05及相关CASE/WAC。

## 11. M08 — 受限 HERV3 host 与资源代理

### 11.1 接入实际 HERV3

1. PAO Functions继续持有Conversation/Message/Run/Task，adapter将L2执行委托给新host。宿主启动采用可信profile，绑定worker/context/execution/service各代次；不改Corebootstrap或公开envelope。
2. host内运行实际HERV3 DIRECT连续循环；provider访问经受限模型proxy，工具通过broker；纯解析/计算可以本地，任何文件/网络/进程副作用必须处于声明边界。
3. 明确最小模型消息、context数据和已批准文件内容可流入host；不把控制平面配置、secret或人类审批会话复制过去。
4. 保持与L1一致的PermissionWait、operation ID、回执、partial approve和Task期限，不因换执行位置多问一次。

### 11.2 Windows 启动合同

优先验证AppContainer/LPAC的实际Python/模型工具兼容性；技术名不是验收。受限token/文件ACL/网络规则负责安全边界，Job Object负责进程归属与生命周期，二者不可替代。

每次runner先准备受限token、安全属性和继承句柄白名单，再suspended创建，加入受管理job并核验隔离，登记task/operation资源，最终一次`commit_start`后才resume。核对job breakaway、子进程、scheduled task/service创建、host注入、句柄复制和控制台/desktop归属；不能让一段业务代码先跑再补job或token限制。

### 11.3 资源代理

- **文件**：handle或受限身份执行，任务隔离、精确读写、对象替换防护；禁止给共享agent账户累计ACL导致权限并集。
- **网络**：默认不可任意外连，按目的地broker；解析、redirect、IP、代理、localhost/LAN均检查。获批模型endpoint不代表开放整个LAN。
- **凭证**：服务按指定provider/业务目标操作，返回最小结果；不向host、shell env、日志给可复用secret。Remote rescue等现有取secret路径须替换成有限能力代理。
- **进程**：任意代码低权限runner；退出/到期/revoke清理完整job及登记资源，不只主PID。
- **浏览器/桌面**：默认完整L2不开放可操作审批环境的桌面控制。未隔离路径仍允许用户主动选用，但actual_controls连同可信批准依赖一起降为partial。

**退出条件：** 真实host和脚本不能越界，但正常批准范围可用；混合并行Task不共享权限；无隐藏直接执行路径或完整secret泄露。L1/L2用户决定和续跑语义等价。对应W06–W09及CASE/WAC。

## 12. M09 — 撤权、故障、恢复与 Windows 验收

### 12.1 撤权与失联

权威撤销事务先fence新执行，再回收live tickets、handles、connections、jobs、device会话；独立supervisor负责服务/host失联时停止受控资源，不依赖agent自觉或FC在线。权限已撤销、执行停止中、停止失败/未知分别输出。

默认时间必须分别测量：ticket≤60秒；heartbeat每5秒、连续3次失败≈15秒识别；从显式撤销提交、可信deadline到达或确认失联起，普通可取消资源5秒内清理目标。不能把票据未过期当继续新启动依据，也不能宣称失联发生后5秒内必定全部停止。

### 12.2 故障矩阵

| 注入位置 | 必须观察 |
| --- | --- |
| 决定事务提交前进程退出 | 无Lease；重投可安全重做 |
| 决定已提交、通知丢失 | 查询恢复原决定，不重复批准 |
| admission通过、排队期间撤销 | 实际start被拒，once不误消费 |
| runner已准备、revoke先于commit_start | 无业务代码执行，prepared资源清理；若start先线性化则归在途取消，监督保持覆盖 |
| 外部提交成功、回执丢失 | unknown/查询原结果，无自动第二次提交 |
| service停止、host仍存活 | 新操作不走本地fallback；独立监督停止在途能力 |
| 旧worker晚退出、新worker已采用 | 只清旧epoch，新任务有效权限保留 |
| restore旧DB或时间回拨 | 不复活旧撤销；缺证据失效活动授权、成果保留 |
| broker协议/runner版本不兼容 | 准确unavailable/partial；保留用户后端选择 |
| FC离线、审批在另一端完成 | authority一致；当前任务按状态恢复，不依赖某客户端在线 |

### 12.3 平台证据

逐项执行WAC，而不是仅跑Linuxmock。记录Windows build、Python/runner/service版本、token/SID/ACL、连接身份、已授权正向动作、越界拒绝、资源清理和观测时间。攻防测试覆盖service stop/config、写代码/账本、抢先pipe、宽RPC、复制ticket、跨任务、desktop自批、继承句柄、重解析竞态、网络绕路、进程脱离及混合L0信任破坏。

离线能力不足时继续完成源码/构建/可运行测试，未测平台项明确留下待执行脚本和环境需求；不得用mock成功发布enforced标签。Windows隔离测试可使用独立命名test service和临时测试账户，不能操作正在用的生产service或客户Run。

**退出条件：** 对发布声称的每个L2 control都有正向与负向Windows证据；退出/撤销/失联无孤儿能力；未支持通道准确降级。对应W10、W11的恢复部分及CASE-L2/OPS与Windows WAC。

## 13. M10 — 发布、运行采用、回退与移交

Windows施工包W11的发布/回退部分与W12在本阶段完成。

### 13.1 发布门

1. 运行与本批风险相关的既有及新增行为测试，不机械扩展全仓测试。所有必需产品CASE、WindowsWAC有状态和证据路径；任何略过说明原因与影响。
2. 运行Core guard、diff whitespace和FC schema不变检查；比较改动路径，确认无外部前端/Telegram专属变更。
3. 产出版本兼容矩阵：HASHI Functions generation、HERV3、service协议、runner profile、OS、FC可信入口、支持工具和未知项。能力报告按此矩阵派生，不写死一个L2布尔值。
4. 更新拥有行为的架构/决策/FYI、使用说明与本目录状态；所有产品文案进现有locale。安装产物和业务源码分开列版本。

### 13.2 采用权限与证据

Agent Functions变更依现有范围采用`/reboot min|same`；共享Functions变更可能需要`/reboot max`所代表的更大操作范围。沿用仓库政策，不为测试方便自行冷启Core或扩大已有生产授权。若用户当前已明确授权对应采用/实例/代理则执行；没有则先完成离线实现、构建和隔离验证，记录待采用的准确范围，不能报告运行中已生效。

本次“一次性交给本地agent开发”的默认范围包括开发、测试和独立测试服务验证，不自动等于停止当前客户任务或升级生产安全服务。开发agent不得仅因某个生产采用未授权就停止所有已授权实施工作。

### 13.3 回退

代码回退与安全等级降级分开：

- L0环境可回退Functions产物，权限数据库未创建；不要求先安装/停止新服务。
- L1/L2环境先阻止新受控执行，保存回执与恢复事实，停止/对账受管资源；兼容版本再接管。DB schema变更按受支持版本迁移，不直接让旧二进制误读新账本。
- 回滚旧账本不恢复旧权限；已撤销/过期/consumed记录不能被备份重置成有效。
- 服务卸载必须保留能处理未清资源的可信监督或先清理完成；清理未知明确显示。用户可选兼容后端继续独立工作，不能重复未知副作用。

### 13.4 移交内容

最终说明列出具体提交、实现范围、测试与环境、采用/未采用、实际支持能力、剩余限制和下一项可执行操作。不可仅说“测试通过”或“L2完成”；必须能回到证据。默认不merge、不部署外部客户端、不自动扩大生产restart。

## 14. 建议测试组织与充分性

下表文件为**建议新增**，不是已存在测试事实。实现时优先在对应owner的既有测试中补behavior，避免重复：

| 新增候选 | 主要风险 / 验收族 |
| --- | --- |
| `tests/test_permission_l0_compatibility.py` | 冻结基线差异、无字段与显式L0、服务故障隔离；CASE-L0 |
| `tests/test_permission_authority.py` | 决定CAS、subset、期限、partial、去重、once；CASE-L1 |
| `tests/test_permission_dispatch_races.py` | 排队、start/revoke、prepared未resume、未知副作用；CASE-L1/LIFE |
| `tests/test_permission_fc_contract.py` | 旧schema、action绑定、认证绕过、镜像与重放；CASE-FC |
| `tests/test_permission_herv3_continuation.py` | 活动DIRECT路径、无模型空转、原operation续跑；CASE-L1/LIFE |
| `tests/test_permission_lifecycle.py` | Task/Run、旧epoch、quiesce、后台、clone、切后端；CASE-LIFE |
| `tests/test_permission_recovery.py` | 事务断点、DB恢复、时钟、outbox与unknown；CASE-LIFE/OPS |
| `tests/test_permission_backend_controls.py` | 真实报告、失效依赖、警告去重与继续；CASE-L1/OPS |
| `tests/windows/permissions/` | 隔离测试host/service/IPC/ACL/网络/凭证/清理；WAC、CASE-L2 |

状态机用确定性时钟/事件barrier验证边界，避免真实sleep数分钟。事务竞争至少实际并发到存储层；Windows边界必须实际不同token/SID/ACL，mock只证明调度而不能证明OS拒绝。对不确定外部副作用使用可控测试服务模拟“提交完成但响应丢失”。

性能测试只在相应实现有实质路径后进行；记录100已注册agent、20并发Task等PRD负载与机器配置。L0验证主要证明没有新增权限调用，不给无意义空函数微基准。

## 15. 行为不一致时的裁决

| 冲突 | 必须采用的行为 |
| --- | --- |
| 用户想继续，但具体受控动作尚未批准 | 继续独立工作；保留该动作等待，可正常申请，不假批准 |
| 用户选择不完全可控的backend | 说明实际缺口并继续；不把新权限规则冒称适用于其原生工具 |
| 暂停/取消之后晚到批准 | 记录仍合法的决定或拒绝过期对象；绝不自动复活被停工作 |
| Task完成但清理未完成 | 权限先失效；成果可交付，清理状态继续追踪 |
| 多端都点击批准 | 一个最终决定，重复返回原结果；不增量扩权 |
| 前端无按钮 | 同一标准命令和返回文本；无客户端改造 |
| 前端无可信人类认证 | 申请/普通工作继续；当前入口不能授权，转已连接可信入口 |
| 完整L2与L0混合桌面/secret冲突 | 隔离L2控制平面或如实partial；不能改变L0承诺 |
| 实现方便要求修改Core/公开FC | 重构Functions/adapter/sidecar方案；不改既定边界 |

## 16. 开工与完成记录模板

每一里程碑在 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md) 补充：开始HEAD、owner/工程层、修改路径、行为变化、风险对应CASE、命令与结果、平台/实例、产物版本、采用事实、未完成项。设计里程碑全部写完不等于功能里程碑完成。

开发时发现源码与设计不符，应给出具体证据并在既定边界内修正实现及文档；不要默默改变产品行为或为了好通过而删验收。确实超出范围的改动单独说明，同时完成其余可以安全推进的工作。
