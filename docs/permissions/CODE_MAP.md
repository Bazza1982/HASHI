# 权限系统代码接入图

| 项目 | 值 |
|---|---|
| 文档版本 | 1.1 |
| 日期 | 2026-10-23 |
| 源码核查基线 | `d6b6513973bfc8f9f22c40ddd8f72802f907baa0` |
| 状态 | **设计待实施；下述新模块、钩子、状态与行为均未实施、未验收** |
| 功能 owner / 工程层 | **PAO / Functions**；HERV3、公共 Frontend Connector、平台执行器为明确边界内的协作者 |
| 本次交付 | 文档；不实现代码、不部署、不重启 |

本文是 [PRD](PRD.md) 与 [施工计划](IMPLEMENTATION_PLAN.md) 的源码证据索引。验收见 [ACCEPTANCE](ACCEPTANCE.md)，公共 FC 边界见 [FC_CONTRACT](FC_CONTRACT.md)，Windows 实体隔离见 [L2_WINDOWS](L2_WINDOWS.md)。所有现状只针对上述 SHA；后续接手者须在目标分支重新核对符号及调用链，不能把本表当成长期不变的接口。

### 2026-10-29 新基线接手提示

- 权限分支已合入当时的 main `d2a6f36d4597181800c2eb671381a1079a872bae`。下文固定链接保留原始设计基线，不声称其行号适用于新基线。
- 相对原始基线，`adapters/her_v2.py`、`adapters/her_v2_provider.py`、`orchestrator/command_interactions.py`、`orchestrator/her_v2/runtime.py`、`orchestrator/run_questions.py`、`orchestrator/workbench_api.py` 与 `tools/registry.py` 均已有变化；施工前须重新核对完整调用链及所有相关入口。
- 有限复核确认普通 RunQuestion 仍不授予权限，工具资源存在排队等待；后续实现须在排队后实际执行前重新验权，不能假定排队前审批仍有效。
- 本提示不是完整 M00 复核，也不是 L0 行为比较或权限功能测试；M00 的剩余要求与状态见 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md)。

**状态用语：**“已存在”指可在基线源码定位；“建议接入”指后续允许实现的窄位置；“新增”指基线尚无该能力。源码存在或已有测试名称均不表示新权限功能已经通过验收。

## MAP-01 — 单一 owner 与不可改边界

### 已存在的架构约束

- [AGENTS.md](../../AGENTS.md) 要求普通功能放 Functions，Core 不引入产品模块，禁止通过普通特性变更重启或修改 Core。[固定证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/AGENTS.md#L1-L41)
- [ARCHITECTURE.md](../../ARCHITECTURE.md) 将 HASHI 级工具能力、权限、调用和执行控制归 PAO；HERV3 持有 Engine Session/Turn，而不持有外层 Run 或权限权威。**Run 是 PAO 接受一条 Message 的执行，不是 Worker 进程实例。**
- 受保护源文件只由 [runtime_contract.py 的 CORE_SOURCE_PATHS](../../orchestrator/runtime_contract.py) 定义；[固定 tuple 证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/runtime_contract.py#L28-L38)。本文不复制保护路径列表，不新增另一份排除清单。
- [Layered Runtime Boundaries](../HASHI_LAYERED_RUNTIME_BOUNDARIES.md) 区分产品 owner 与工程层；[Functions/热采用边界证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md#L182-L220)。

### 实施边界

权限 Grant、AccessRequest（旧稿亦称 PermissionRequest，是同一申请对象）、PermissionTask、撤销和执行票据由**一个 PAO 权威服务**管理。ToolRegistry 做执行门，HERV3 接入等待/恢复，FC 递交受信批准输入；它们不得成为第二个 grant writer。

允许按本文修改 HASHI 的公共 FC Functions，包括历史命名的 `orchestrator/workbench_api.py`。这个名称代表 Backend API，不代表修改外部 Workbench 产品。**不得修改外部客户端、Telegram 专用 adapter、公开 FC schema 或 protected Core。** 既有普通问答协议及其 `grants_permissions=False` 语义不变。

保护清单是开发流程约束；它不证明 L2 具有 OS 隔离。L2 的可信服务身份、ACL、受限 runner 和 IPC 必须另外落地及实测。

追踪：PRD `CORE-01..03`、`BASE/LEVEL/L1/L2`；验收 `CASE-OPS-01..03`、`CASE-L2-01..09`。

## MAP-02 — L0 必须在接入点之前分流

### 已存在

基线没有本设计的 PermissionService、PermissionTask、ExecutionEpoch 或 `request_access`。当前 [ToolRegistry](../../tools/registry.py) 从 `allowed_tools`、Workzone 和既有 policy 派生能力；schema 名称由 [tools/schemas.py](../../tools/schemas.py) 的 `TOOL_SCHEMA_MAP` / `ALL_TOOL_NAMES` 派生。[registry 构造证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tools/registry.py#L255-L334)

### 建议接入

在 Functions 的配置解析/服务接线和 Agent 请求入口使用同一 enabled/level 判定。旧配置缺省及显式 L0 均走现有路径。**先分流，再构造权限服务、添加 agent 工具或 prompt、开库、发权限 RPC、选择受限 runner、登记权限恢复工作。** 只在执行时传一个“allow all”的权限对象，仍可能引入启动依赖或副作用，不能证明 L0 等价。

`/safety` 是用户主动打开的管理入口；普通 L0 Run 不因此获得新 agent 工具、系统 prompt 或权限后台轮询。用户明确切级时的一次性迁移/回收与后续 L0 Run 分开记录，不把切级清理变成每次 L0 请求的隐式权限恢复流程。

| L0 观察面 | 必须保持的行为 |
|---|---|
| 工具 catalogue/schema | 不向模型增加权限工具；既有工具集、schema 与可调用性不被新系统重排或收窄 |
| 模型输入 | 无权限系统提示词、等待说明、隐藏审批约束或新 provider call |
| 持久化 | 不为正常 L0 Run 创建/读取/迁移新权限库或租约记录 |
| RPC/服务 | 不依赖权限服务健康检查、端口、密钥或 IPC；服务完全不存在仍正常工作 |
| 执行器 | 沿用既有 adapter/runner；不自动切受限用户、容器或 L2 launcher |
| 恢复/回收 | 保留基线恢复、取消、错误语义；不加入新权限任务和回收副作用 |

追踪：PRD `LEG-01..08`、`LEVEL/MIG`；验收 `CASE-L0-01..08`、`CASE-OPS-06`。

## MAP-03 — HERV3 的真实活跃路径

当前路径为：

1. [adapters/her_v2.py::generate_response](../../adapters/her_v2.py) 构造本次执行的 `HERv2Runtime`。[固定证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/adapters/her_v2.py#L2396-L2435)
2. [HERv2Runtime._run_turn / _run_direct](../../orchestrator/her_v2/runtime.py) 进入单模型循环，以 `Stage.DIRECT` 调用 `_invoke_stage`。[固定证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/her_v2/runtime.py#L715-L817)
3. [HashiStageProvider.invoke](../../adapters/her_v2_provider.py) 将 Agent registry 包装为 evidence、cognitive、unbounded registry，然后赋给模型 backend。[固定证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/adapters/her_v2_provider.py#L4703-L4745)
4. 模型适配器调用 `await tool_registry.execute(...)`，如 [OpenRouterAdapter._run_tool_calls](../../adapters/openrouter_api.py)。[固定证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/adapters/openrouter_api.py#L3252-L3287)
5. 统一进入 [ToolRegistry.execute](../../tools/registry.py) 的准入、资源排队与 dispatch。

`her_v2`/`HERv2*` 是当前 HERV3 的兼容命名，不是新增 v2 workflow 的理由。旧 `_CompulsoryReplanToolRegistry` 只有 `checkpoint_coordinator` 非空、且 `Stage.EXECUTION` 才安装；DIRECT 路径没有这个 coordinator。**不得将旧 Replan/checkpoint 当成已存在的权限等待/续跑钩子。**

建议接入：通过新的 PAO permission port 向当前 DIRECT 工具链提供可信 Task/ExecutionEpoch 上下文。等待只暂停受影响操作；不得增加 planner、replanner、reviewer 或改变 `/effort` 的含义。

追踪：PRD `TASK/UX/L1/BACKEND`；验收 `CASE-L1-01..03`、`CASE-LIFE-01/06..08`。

## MAP-04 — ToolRegistry 的两阶段权限检查

| 已存在的接口 | 当前能力 | 建议新增的职责 |
|---|---|---|
| `evaluate_admission(tool_name, arguments, tool_call_id)` | 同步、无 dispatch 的 allowed-tools/request allowlist/live-runtime/enterprise/smart policy 检查 | 保留同步纯旧检查，不引入权限服务 I/O、等待或 once 消费 |
| `execute_with_audit_context(..., audit_context=...)` | ContextVar 保存调用本地上下文，finally 恢复 | 传播受信 PAO 注入的 Task、操作、ExecutionEpoch 关联；不信任模型参数里的身份 |
| `execute(...)` | 先 admission，再 `tool_resource`，再 `_dispatch_observed` | 在异步方法内紧随旧 admission 调用新的权限 evaluate；审批前登记 PendingOperation；资源排队后再次验权并原子 claim；批准后只执行匹配操作 |
| `_dispatch_observed / _dispatch` | 真实工具执行、结果审计 | 根据已验证 ticket 调受控 executor；不能允许另一路未验权 dispatch |

源码：[上下文接口](../../tools/registry.py)、[固定 417–454](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tools/registry.py#L417-L454)；[准入固定 749–857](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tools/registry.py#L749-L857)；[排队及 dispatch 固定 1002–1019](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tools/registry.py#L1002-L1019)。

新的权限 evaluate 可读取权威服务，但不消费授权额度；需要 I/O 时由异步 `execute` 等待，不能把同步 `evaluate_admission` 改成 RPC 门。L0 跳过这一新分支；原有纯 admission 调用者的返回类型与同步语义保持。

[execution_resources.tool_resource](../../orchestrator/execution_resources.py) 会等待 shell/file-write/patch 的资源 lease。[固定证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/execution_resources.py#L178-L210) **只有排队前验证是不够的**：排队期间 grant 可以过期、撤销或 task/epoch 已变。审批等待不可长期占用执行资源；若二次检查需要重新批准，释放资源后再等。

未知工具、工具内再调用、shell 子进程、browser/desktop 和外部 connector 不因顶层检查存在便自动证明完全受控。覆盖声明必须来自真实执行通道证据；L1 的应用级门控与 L2 的 OS 保证分别报告。

追踪：PRD `SCOPE/LEASE/L1/NFR`；验收 `CASE-L1-04..12`、`CASE-L2-02/04/07`。

## MAP-05 — 申请工具与公共 FC 批准接入

### 已存在、可复用的 Functions

| 文件 / 符号 | 可复用范围 | 现状证据 |
|---|---|---|
| [command_registry.py](../../orchestrator/command_registry.py) — `RuntimeCommand`、`RuntimeCallback`、`_iter_command_modules` | 在 `orchestrator/commands/` 增加 `/safety` 命令与回调，复用现有发现机制 | [27–58](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_registry.py#L27-L58) |
| [command_interaction_bridge.py](../../orchestrator/command_interaction_bridge.py) — `dispatch_command_interaction`、`build_frontend_command_invocation` | generic menu/action 与 canonical invocation 关联；可做最小私有上下文接线 | [155–204](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interaction_bridge.py#L155-L204) |
| [command_interaction_transport.py](../../orchestrator/command_interaction_transport.py) — `try_dispatch_command_interaction_transport` | 现有 Worker 命令传输，沿用原 envelope | [70 起](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interaction_transport.py#L70-L110) |
| [frontend_command_admission.py](../../orchestrator/frontend_command_admission.py) — `reserve_frontend_command_invocation`、`execute_frontend_command_invocation` | 命令 admission、幂等和完成记录；不能独自替代权限服务 CAS | [50–81](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/frontend_command_admission.py#L50-L81) |
| [workbench_api.py](../../orchestrator/workbench_api.py) — `handle_v1_session_command_invocation` | HASHI Backend API 的公共 FC 入口；允许在此接线可信 principal/receipt | [8419 起](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/workbench_api.py#L8419-L8460) |

### 新增与禁止假定

- 新 agent 申请工具在 `tools/schemas.py` 登记，薄适配委托 PAO；权限不足也必须能发起申请。L0 不向模型暴露此工具。
- 新 `/safety` RuntimeCommand/RuntimeCallback 和既有 generic action 承载查看、批准、拒绝、缩小范围和撤销。**不新增公开 FC 字段、schema version、专用 Telegram adapter 或外部客户端页面。**
- 可信 principal/decision receipt 是新增的服务端安全能力。现有 generic command 的 `actor_id`、配置 owner、loopback、共享 token 或可被 agent 读取的 admin token，不能直接宣称是人类审批证明。
- 同一 AccessRequest 可在多个 channel/mirror 显示；全局 request ID、不可变 scope digest、request/decision revision、decision/operation 幂等键由权限服务持有；各 channel 的 menu/action ID 仅用于界面绑定。菜单 revision 与权限请求 revision 分开验证。纯文本批准必须携带服务生成的 `--revision N`；不能只收到 request ID 就批准最新范围。
- TUI direct/proxy 的当前连接若不能验证批准者，保留普通任务、查看、申请和独立工作；权限写明确返回“此连接不能验证批准者”，提示通过其他已连接可信 FC 处理同一请求。不得伪造 owner、静默切 L0 或冒称 L2 enforced。

[run_questions.py](../../orchestrator/run_questions.py) 和 [tools/schemas.py](../../tools/schemas.py) 的普通问答始终不授权。[固定问答声明](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/run_questions.py#L1-L20)。`question_id` 可以关联澄清，但 answer/option_id 不构成批准证明。具体契约以 [FC_CONTRACT](FC_CONTRACT.md) 为准。

追踪：PRD `FC-01..04`、`APPROVAL/UI/API/UX`；验收 `CASE-FC-01..09`。

## MAP-06 — 已有等待 guard 与新增 PermissionWait

已存在的 `_CognitiveControlToolRegistry.wait_before_terminal_response` 会等待普通 RunQuestion 解决，使用长轮询保持 Run，等待期间不产生额外模型调用。[源码](../../adapters/her_v2_provider.py)；[固定 1935–2009](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/adapters/her_v2_provider.py#L1935-L2009)。[OpenRouterAdapter._wait_before_terminal_response](../../adapters/openrouter_api.py) 会调用 registry waiter；[固定 2063–2105](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/adapters/openrouter_api.py#L2063-L2105)。[HashiApiAdapter](../../adapters/hashi_api.py) 活动路径也调用此 guard。[固定 1400–1435](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/adapters/hashi_api.py#L1400-L1435)

它是普通问答模式，不是可直接启用的权限系统。新增 PermissionWait/PendingOperation 需满足：

- 由 PAO 权威持久化、HERV3 订阅；未被阻塞的工作可继续。
- 相同 task/scope 的未决请求合并，活跃 grant 内不重复问。
- 等待不会形成模型轮询、普通工具失败或 cognitive stall。当前 `_process_tool_result` 会把 `is_error` 等结果送入 controller，需要明确新控制分支。[固定 2011–2089](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/adapters/her_v2_provider.py#L2011-L2089)
- 取消、暂停、切 backend、失联与热采用均有独立的唤醒/不唤醒规则；晚到批准不复活旧运行。
- 不通过添加旧 staged lifecycle 来模拟等待；新等待事实与单循环执行状态的映射要显式实现和测试。

追踪：PRD `UX/APPROVAL/TASK/LIFE`；验收 `CASE-L1-02/03/09`、`CASE-LIFE-03..08`。

## MAP-07 — 持久化、Task 与 ExecutionEpoch

| 已存在 | 可复用事实 | 不能假定已有 |
|---|---|---|
| [HERv2Runtime.run_turn](../../orchestrator/her_v2/runtime.py) | 每 Turn 构造 ledger/control/HERTaskState；[339–374](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/her_v2/runtime.py#L339-L374) | HERTaskState 不是跨 Run 的 PermissionTask |
| [HerSessionStore.consume_recovery_context](../../orchestrator/her_v2/session_store.py) | 中断证据事务性绑定到下一 Turn，标为 quoted recovery context；[1763–1833](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/her_v2/session_store.py#L1763-L1833) | 不提供任意 Python 栈恢复或自动安全 replay |
| `HerSessionStore.reconcile_interrupted_turns` | 依据执行 owner 和存活状态处理死 Worker；[3520–3568](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/her_v2/session_store.py#L3520-L3568) | 不包含 permission grant/epoch 恢复权威 |
| [HerBackendSessionCoordinator](../../orchestrator/her_v2/backend_session.py) | Session accept/complete/steer/cancel；[729–767](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/her_v2/backend_session.py#L729-L767) | 不可用新 Message/Run 伪造一次审批恢复 |

新增 `PermissionTask` 可跨 Run/Turn；`ExecutionEpoch` 标识一次可执行绑定，须包含足够的 worker/generation/backend 身份，不能等同 Run。Task grant、Run grant、限时 grant、once ticket 各自按已批准生命周期结束；新 Run/Worker 不隐式延长期限。

恢复持久记录至少关联 request、task、run、operation、scope digest、grant、execution epoch 和执行 receipt。未执行操作可在重新验权后恢复；已完成不重放；副作用 unknown 先对账。保存过审批不代表重启后可以直接执行。

追踪：PRD `TASK/LEASE/LIFE/MIG`；验收 `CASE-LIFE-01/02/05/07..11`。

## MAP-08 — 生命周期与代际回收接点

| Functions 接点 | 已存在行为 | 新接入要求 |
|---|---|---|
| [runtime_lifecycle.release_capability_task](../../orchestrator/runtime_lifecycle.py) | Run 最终清理时按 request_id 释放设备绑定；[769–801](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/runtime_lifecycle.py#L769-L801) | 分清 Run 终态和 Task 终态，不能误撤仍活跃 task grant |
| [runtime_cancel.cancel_session_run](../../orchestrator/runtime_cancel.py) | owner/session/run/request 精确校验、transition lock；[81–110](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/runtime_cancel.py#L81-L110) | 取消线性化后阻止新执行和晚批准唤醒 |
| [AgentLifecycleManager.stop_agent](../../orchestrator/agent_lifecycle.py) | gate、quiesce、shutdown；quiesce 失败恢复旧 route；[245–299](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/agent_lifecycle.py#L245-L299) | 先 fence，再清理；失败不得延长已撤权限 |
| [FunctionWorkerHost.quiesce](../../orchestrator/function_worker_host.py) | 等 queue/is_generating/background tasks 清空，超时 resume；[1286–1336](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/function_worker_host.py#L1286-L1336) | 新 PermissionWait 必须可持久化 park/drain，不无限卡排空 |
| [FunctionWorkerSupervisor.handle_worker_exit](../../orchestrator/function_worker_supervisor.py) | 先 cancel_agent 设备绑定，再判断是否当前 client；[2462–2478](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/function_worker_supervisor.py#L2462-L2478) | 新权限回收按退出的 execution epoch，不能照搬按 agent 名的 blanket cancel |

父子 Task 传播只允许缩小范围/期限；独立获批后台 Task 必须有自己的 owner、grant 和生命周期。clone/session copy 可复制内容，不复制活跃 grant、票据或执行身份。

追踪：PRD `LIFE/TASK/LEASE`；验收 `CASE-LIFE-01..11`。

## MAP-09 — Functions 服务、generic RPC 与 L2 启动

[manager_registry.FUNCTION_MANAGER_SPECS / build_function_manager_bundle](../../orchestrator/manager_registry.py) 是 shared manager 构造 owner。[固定证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/manager_registry.py#L22-L85)。新增服务需服从 L0 懒接线、共享进程生命周期和 hot adoption。

[function_worker_protocol.validate_envelope](../../orchestrator/function_worker_protocol.py) 只验证非空 method 与 JSON params；可复用现有通用传输，在 Functions 内接入私有业务 handler 而不改 Core envelope；这不授权新增 Core-owned RPC，也不授权执行主体调用批准 handler。[固定证据](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/function_worker_protocol.py#L82-L133)。但 [FunctionWorkerSupervisor.handle_worker_request](../../orchestrator/function_worker_supervisor.py) 当前能路由 start/stop/service/route/scheduler/capability；它不是按安全级别隔离的通用能力网关。[固定 2016–2093](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/function_worker_supervisor.py#L2016-L2093)。

`_capability_request` 由 `client.agent_name` 绑定 Agent 并覆盖调用方提供的 roots，可借鉴受信身份来源，但没有 human grant 语义。[固定 2138–2195](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/function_worker_supervisor.py#L2138-L2195)。

**L2 新增要求：** sandbox 不能直接获得 legacy 宽 RPC 或 approve/admin 凭据。必须有专用受限 broker IPC，或在 Functions dispatcher 用受信 worker/task/operation binding 限制方法和对象，防止 shared manager 成为代执行绕过。执行主体不得调用批准接口。

本期选定的 Windows 架构保留可信 PAO／FC Function 宿主，新增受限 **HERV3 execution host** 承载模型循环，再经独立安全服务／窄 broker 和受限操作 runner 执行资源访问。HERV3 host 不持有完整实例配置、FC 凭据、原始 provider 密钥或 legacy generic worker 管道；不能仅限制 shell runner 而把模型循环留在可直访资源的可信进程内。具体职责和可用性证明见 [L2_WINDOWS](L2_WINDOWS.md)。

`prepare_worker` 现在仅使用 `multiprocessing.get_context("spawn")`、Pipe 和普通 Process；不存在受限 token/AppContainer launcher 参数。[源码](../../orchestrator/function_worker_supervisor.py)；[固定 1748–1815](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/function_worker_supervisor.py#L1748-L1815)。不能假定直接把现有 Worker 改成 AppContainer 就完成上述拆分；受限 host、launcher、窄 IPC 和 runner 都是新增实现，需对接既有 Functions 代际、停止与恢复流程，保持 verified generation、protected bootstrap 和 Core envelope 不变。

追踪：PRD `CORE/L1/L2/API/BACKEND`；验收 `CASE-L2-01..09`、`CASE-OPS-03..05`。

## MAP-10 — Workzone 与后端覆盖

[runtime_workzone.sync_workzone_to_backend_config](../../orchestrator/runtime_workzone.py) 会更新长期 config 与 registry `access_roots`。[固定 113–165](https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/runtime_workzone.py#L113-L165)。临时 grant 不能写回长期 Workzone；单次操作使用 admission-time 基线和当前有效 grant overlay，撤销无需等待长期 Workzone 刷新。

权限等级、当前 backend、工具/资源受控能力必须分开表示。HERV3 自有循环能够接入完整控制；第三方后端只能报告已实测的覆盖。用户按既有已授权路径切换不兼容后端时，保留工作、明确实际覆盖和剩余选择，不全局阻断，也不自动降低用户配置等级。受控执行器/服务坏了不能偷偷在同一路径无约束执行；应等待、恢复，或由用户明确选择兼容路径。

追踪：PRD `SCOPE/BACKEND/UX/MIG`；验收 `CASE-L1-08/12`、`CASE-L2-06/09`、`CASE-OPS-05/06`。

## MAP-11 — 拟新增模块与边界

以下均为**建议新增、尚不存在**，路径统一采用 [IMPLEMENTATION_PLAN](IMPLEMENTATION_PLAN.md) 第 4.1 节的候选结构，不另建平行模块。名称允许实施时小幅调整，职责、owner 和测试关系不得丢失；更名须同步施工计划、本图和接手记录。不为未来假设预建通用插件框架。

| 建议路径 | 唯一职责 | 消费者 / 验收主题 |
|---|---|---|
| `orchestrator/permissions/models.py` | 不可变领域类型、状态、稳定错误与结果分类 | service、registry、FC；SCOPE/LEASE |
| `orchestrator/permissions/normalization.py` | 资源规范化、subset、scope digest、版本化参数摘要 | authority、执行绑定；SCOPE |
| `orchestrator/permissions/interfaces.py` | PermissionAuthority / Enforcer / TrustedDecision / Recovery 合同 | L1/L2 实现与消费者；单一权威 |
| `orchestrator/permissions/store.py`、`orchestrator/permissions/migrations/` | CAS、幂等、期限、撤销、receipt、outbox 及 schema 迁移 | service；并发/恢复/迁移 |
| `orchestrator/permissions/service.py` | request/PermissionLease 决策、claim/start、撤权与派生授权单一 owner | 工具申请、/safety、执行器；L1/APPROVAL |
| `orchestrator/permissions/task_binding.py`、`orchestrator/permissions/lifecycle.py` | PAO 任务归属、epoch、终结与精确代际接线 | PAO、HERV3、supervisor；CASE-LIFE |
| `orchestrator/permissions/continuation.py` | PendingOperation、PermissionWait、park、恢复计划与回执消费 | HERV3、PAO lifecycle；CASE-LIFE |
| `orchestrator/permissions/enforcement.py` | L1/L2 执行 adapter、dispatch 前验票 | ToolRegistry/runner；CASE-L1/L2 |
| `orchestrator/permissions/backend_capabilities.py` | 从实际 backend/通道证据派生有效覆盖 | /safety、切后端；BACKEND |
| `orchestrator/permissions/fc_bridge.py` | 私有 principal/receipt 与命令输入到权限服务的桥接 | 公共 FC Functions；CASE-FC |
| `orchestrator/permissions/trusted_decisions.py` | 验证 VerifiedPrincipal 和一次 DecisionReceipt；与执行票据分离 | 公共 FC ingress/service；APPROVAL |
| `orchestrator/commands/safety.py` | `/safety` RuntimeCommand/RuntimeCallback | 既有 generic action；FC_CONTRACT |
| `tools/request_access.py` | 三个 agent 控制工具的薄适配，不拥有授权状态 | schema/ToolRegistry；UX |
| L2 trusted service / restricted runner | OS 受信控制边界、受限进程及专用 IPC | 包位置/部署详见 L2_WINDOWS；真实平台验收 |

不得为了上述模块改 protected tuple、Core bootstrap、Core envelope 或 dependency baseline。新的可选/原生依赖使用隔离 sidecar/environment，不能安装到运行中的 Core interpreter。

enabled/level 解析及旧配置默认 L0 的接线遵循施工计划第 4.3 节，设置写入复用既有配置 owner 和 `orchestrator.config_json` revision CAS；不另造一份权限配置权威。

## MAP-12 — 精确 hot adoption 与验证范围

| 后续变更实际 owner | 必要采用边界 | 不允许的扩大 |
|---|---|---|
| 仅 Agent-local HERV3/工具/命令 Functions | 对选定 Agent 做候选资格检查和 `/reboot min` 或 `/reboot same` 的离线集成验证；需要在线 canary 时另有明确实例授权 | 不因私有类/字段变化自动升为 max；不碰无关 Agent/共享服务/Core |
| shared PAO 服务、公共 Backend API、shared supervisor wiring | 验证既有 `/reboot max` shared Function handoff，Core PID/lock 保持；所有受影响 Agent 的采用证据可追溯 | 不以 Worker-only reboot 冒充 shared 采用；不冷启 Core |
| 独立 L2 安全服务/平台 runner | 按独立服务包版本、身份、ACL、进程与 protocol handshake 采用；HASHI Functions 客户端仍走其实际范围 | 不把权限服务安装/重启藏进 Agent reboot；不改 Core Python/依赖 |
| 文档-only（本次） | 链接/状态/源证据检查、`git diff --check`、protected-Core guard | 不运行行为套件、不宣称实现通过、不自动生产 reboot |

具体命令与 CASE 状态统一记录在 [ACCEPTANCE](ACCEPTANCE.md)，完成证据登记 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md)。共享 registry/lifecycle/Worker 代码未来发生变化时按 [TESTING_POLICY](../TESTING_POLICY.md) 跑 focused 和 curated core gate；不把全库 pytest 当默认动作。

### 接手时必须复核

1. 目标 commit 与此基线差异、源符号和 protected tuple。
2. 当前改动 owner 和真实采用范围，不以文件名推断 Core 或 frontend 产品。
3. L0 在所有新副作用之前分流；新模块没被无条件实例化。
4. DIRECT 活跃链、dispatch 二次验权、permission wait 与证据 receipt 的顺序。
5. FC 可信 principal 的真实来源、无认证连接诚实处理、跨 channel 幂等。
6. task/run/turn/epoch 分离、worker 代际回收、park/quiesce 和 unknown side effect。
7. L2 实体证据与 coverage 声明一致；mock/unit pass 不能标记 L2 完成。

所有实施与测试项在本版本均为待执行，不能从本文的源码事实推导出上线状态。

## 里程碑对应关系

本表按 [IMPLEMENTATION_PLAN](IMPLEMENTATION_PLAN.md) 的 M00..M10 对接，不改变各阶段退出标准。

| 里程碑 | 负责主题 | 本图落点 | 首要验收 |
|---|---|---|---|
| M00 | 基线与 Core/FC 范围锁定 | MAP-01、05、12 | CASE-OPS-01、CASE-FC-01 |
| M01 | 类型、配置与 L0 真旁路 | MAP-02、11 | CASE-L0-01..08 |
| M02 | 权威账本、授权 CAS 与恢复 | MAP-04、07、11 | CASE-L1-03..07、CASE-LIFE-07/08 |
| M03 | /safety 标准 FC 和可信决定 | MAP-05、06 | CASE-FC-01..09 |
| M04 | 执行拦截、排队后原子 claim 与工具覆盖 | MAP-03、04、10 | CASE-L1-01/04/05/08/10/11 |
| M05 | HERV3 等待/续跑与生命周期 | MAP-03、06、07、08 | CASE-LIFE-01..11、CASE-L1-02/09 |
| M06 | L1 验收、迁移和后端覆盖 | MAP-02、10、12 | CASE-L0、CASE-L1、CASE-OPS-05/06 |
| M07 | L2 服务、身份与窄 broker | MAP-09、11 | CASE-L2-01/03/05/08 |
| M08 | L2 受限 HERV3 host 和资源代理 | MAP-03、04、09 | CASE-L2-02/04/07；L2_WINDOWS |
| M09 | L2 撤销/故障/恢复与 Windows 攻防验收 | MAP-07、08、09、10 | CASE-L2-01..09、CASE-LIFE-05/07/08 |
| M10 | 发布、采用、回退与移交 | MAP-12 | CASE-OPS-01..08及最终适用 CASE 汇总 |
