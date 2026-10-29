# 权限系统验收计划

| 项目 | 值 |
|---|---|
| 文档版本 / 日期 | 1.1 / 2026-10-23 |
| 代码基线 | `d6b6513973bfc8f9f22c40ddd8f72802f907baa0` |
| 文档与功能状态 | **内部基础实施中；完整功能与平台 CASE 待执行** |
| 本次范围 | 历史设计交付曾为 docs-only；当前实施与证据见状态文件，不自动部署或重启 |
| 权威与边界 | [PRD](PRD.md)、[施工计划](IMPLEMENTATION_PLAN.md)、[代码接入图](CODE_MAP.md)、[FC 契约](FC_CONTRACT.md)、[Windows L2](L2_WINDOWS.md) |

本文件是一份可执行的验收目录，不是通过报告。下面的现有测试确实存在于基线，供未来按改动范围选择；拟新增测试明确标为 **proposed**。现有测试通过只能证明原合同未回归，不能代替新权限行为或真实平台证据。所有实现状态与证据最终登记到 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md)。

## 1. 执行规则与状态

遵守 [TESTING_POLICY](../TESTING_POLICY.md)：测试可观察行为、持久化、权限与恢复边界；新增/重写测试必须有可信 red/green 原因。mocks 可安排输入或观察调用，不能把 mock 自身或文档字符串当被测功能。

- 原始文档交付只做 link/schema/diff/保护清单检查；当前实现测试与尚未覆盖的范围见状态文件。
- 实施后先跑 owning node/module；仅在跨合同边界时加入直接消费者。共享 registry、配置、Worker、生命周期、gateway/central runtime 改动再跑 curated core gate。
- 不默认跑全库 `python -m pytest -q tests` 或 offline product suite。下面的 `python -m pytest -q` 专指仓库 `pyproject.toml` 定义的 curated core gate，不是全库扫描。
- 不并发启动共享默认 pytest 临时根的多个进程；需要并行时为每进程指定独立 `--basetemp`。
- live/platform 检查只能在明确授权的测试实例/设备运行。不得自动生产 reboot、扩大 Agent 范围、启动真实外部消息发送或使用生产凭据。
- **L2 完成必须有实体平台证据。** 只在 Linux 测过 Windows launcher 参数、mock token/pipe/ACL，或 Windows 测试被 skip，都不能标 L2 完成。
- Run = PAO 接受一条 Message；Turn = Engine 处理单元；PermissionTask 可跨 Run/Turn；ExecutionEpoch 是独立执行绑定。验收记录不得混用这些 ID。

状态只允许 `待执行 / 通过 / 失败 / 阻塞 / 不适用`。`不适用` 必须附负责人、理由及无法宣称的覆盖；skip 记录为阻塞或不适用，不记录通过。本文初始所有 CASE 均为 **待执行**。

## 2. 统一准备与证据

### 2.1 可复现测试夹具

未来测试实现须提供下列夹具，名称仅描述角色，**不假定这些 API 现在存在**：

| 标识 | 准备 |
|---|---|
| 实例 I1 | 隔离临时 bridge home、干净旧配置副本；无生产 token；provider 用可记录真实请求 payload 的确定性测试入口 |
| Owner A/B | 两个不同受信测试 principal；另有一个仅自报 actor_id 的未认证连接 |
| Agent A/B | 不同 workspace 和任务；旧 Worker W1、新 Worker W2，独立 ExecutionEpoch E1/E2 |
| 资源 Z1/Z2/Z3 | 临时允许工作目录 Z1；额外目录 Z2；禁止目录 Z3；每处放唯一 canary；路径测试包括相对路径和 OS 支持的 alias/link |
| Task T1 / Run R1,R2 | 同一 PermissionTask 下两条不同用户 Message 被 PAO 接受为两个 Run；另有独立 Task T2 |
| 有副作用 probe | 在测试目录追加 operation_id 的真实小工具/脚本；通过文件内容、子进程、受控 HTTP sink 和 receipt 共同判断是否实际执行 |
| 资源等待 | 用既有 execution resource lease 占住目标资源，使工具已通过预检但尚未 dispatch；用事件握手确认阶段，不用猜测 sleep |
| 时间控制 | L1 单元/集成使用注入的受信时钟推进到边界；L2 验证使用真实平台时钟/进程并记录实际时间，不以 mock 时钟证明 OS 行为 |
| FC 会话 | 同一 request 的两个合法 channel/endpoint 投影，各有独立 menu/action；一个未认证 TUI direct/proxy 路径 |
| L2 设备 | 独立、明确授权的 Windows 测试环境；OS build、服务账户、runner 身份、签名/包版本、ACL、Job/handle、网络证据均可记录 |

### 2.2 每个 CASE 的最小证据

记录测试 commit、文档版本、命令/node 或人工步骤、环境/实例、起止时间、真实对象 ID（脱敏）、预期与实际、外部副作用、canonical request/grant/operation 状态、失败/skip 原因。L0 额外记录 provider payload、catalogue、文件/DB/RPC/进程观察差异；L2 额外记录实体隔离证据。

对竞态用 barrier/event 或真实受控进程握手建立顺序。不要用“最终文件不存在”单一断言证明操作没有执行；检查 dispatch/invocation receipt、真实 sink 及状态迁移。审计输出不包含明文凭据或完整敏感文件。

## 3. 当前 docs-only 静态护栏

在仓库根目录运行下列命令；检查基线和完整 diff，不加 Core 授权开关：

```bash
git diff --check
python scripts/check_protected_core_changes.py
python scripts/check_protected_core_changes.py --base d6b6513973bfc8f9f22c40ddd8f72802f907baa0
```

提交前由提交者检查已暂存范围：

```bash
git diff --cached --check
python scripts/check_protected_core_changes.py --cached
git diff --cached --name-status
```

原始 docs-only 交付的暂存内容限定为设计/施工/测试/接手文档，不含实现、配置、测试行为或受保护 Core 变化。校验同目录链接以及 `../../` 源码链接存在，固定源码链接使用基线 SHA；proposed 模块保持代码文字而非假装已经存在的可点击文件。核对全部 CASE 仍是待执行，没有把源码审查或文档静态检查写成实现验收。

## 4. 实施后可运行的现有测试

以下命令只引用原始基线真实存在的文件/节点。 依据改动选择一个或几个直接相关组，不能把整表当作每次提交的测试菜单。

### EX-CORE — 架构与保护边界

```bash
python -m pytest -q tests/test_minimal_core.py tests/test_check_protected_core_changes.py tests/test_function_contract.py
```

重点节点包括 `test_core_import_closure_includes_lazy_dependencies`、`test_entrypoint_does_not_load_product_code`、`test_explicit_authorization_alone_does_not_bypass_major_policy`。相关现状证据：[CODE_MAP MAP-01](CODE_MAP.md)。

### EX-TOOLS — 工具准入、上下文和资源排队

```bash
python -m pytest -q tests/test_tool_audit.py tests/test_execution_resources.py
```

已有真实节点包括：

- `test_tool_registry_task_local_audit_context_does_not_mutate_base`
- `test_tool_registry_writes_denied_audit_record`
- `test_file_tools_wait_for_shell_scope_but_independent_files_can_overlap`
- `test_os_resource_lease_excludes_a_separate_worker_process`

临时 Workzone overlay 改变其直接消费者时再运行：

```bash
python -m pytest -q tests/test_runtime_workzone.py tests/test_workzone.py
```

### EX-HER — 当前单循环、等待与副作用恢复

```bash
python -m pytest -q tests/test_her_v3_runtime.py tests/test_her_v3_contract.py
python -m pytest -q tests/test_her_v2_cognitive_control.py::test_pending_run_question_blocks_terminal_response_until_answer_consumed tests/test_her_v2_cognitive_control.py::test_closed_run_question_releases_guard_without_inventing_answer
python -m pytest -q tests/test_her_v2_control_plane.py tests/test_her_v2_fixed_backend.py
```

仅工具 wrapper/adapter 契约改动需要的直接证据：

```bash
python -m pytest -q tests/test_her_v2_adapter.py::test_hashi_stage_provider_records_exact_completed_tool_evidence_receipts tests/test_her_v2_adapter.py::test_hashi_stage_provider_installs_full_direct_contract_and_tools
```

这些兼容文件名仍测试现有 HERV3，不允许为了新权限功能恢复旧 staged workflow。

### EX-FC — 公共命令、幂等、普通问答与 mirror

```bash
python -m pytest -q tests/test_frontend_contracts.py tests/test_frontend_command_admission.py tests/test_command_interactions.py tests/test_command_interaction_transport.py tests/test_command_interaction_adapters.py
python -m pytest -q tests/test_run_questions.py tests/test_run_questions_review.py tests/test_run_question_connectors.py
```

只有公共 delivery/mirror 消费边界确实改动时再加入：

```bash
python -m pytest -q tests/test_frontend_standard_interface.py tests/test_frontend_whatsapp_mirror.py
```

已有“普通 answer 不授权”、command replay/冲突和 mirror delivery 测试只是回归基础；它们**没有**证明新的跨 channel permission decision CAS 或 human principal 来源。

### EX-LIFE — 精确取消、quiesce 与代际

```bash
python -m pytest -q tests/test_runtime_lifecycle.py tests/test_agent_lifecycle.py tests/test_function_worker_quiesce.py
```

Worker/shared Functions 修改按实际范围追加规范最小组：

```bash
python -m pytest -q tests/test_runtime_contract.py tests/test_function_generation.py tests/test_function_worker_protocol.py tests/test_function_worker_supervisor.py tests/test_reboot_manager.py
python -m pytest -q tests/test_kernel_process.py tests/test_minimal_core.py tests/test_runtime_handoff.py
```

上述测试使用隔离实例/进程；不要求也不授权生产重启。改动共享 registry、配置、Worker、lifecycle 或 central runtime 后再运行规范 curated core gate：

```bash
python -m pytest -q
```

## 5. 拟新增测试入口（proposed）

以下文件/目录**基线不存在、尚未创建**，统一采用 [IMPLEMENTATION_PLAN](IMPLEMENTATION_PLAN.md) 第 14 节的候选结构，不创建第二套平行测试。实施者可以调整拆分，但必须同步两份文档、维护 CASE 覆盖表与 red/green 证据，不能只测试实现细节或复制文档文字。

| Proposed 文件/目录 | 主要负责 CASE | 未来 focused 命令 |
|---|---|---|
| `tests/test_permission_l0_compatibility.py` | CASE-L0-01..08 | `python -m pytest -q tests/test_permission_l0_compatibility.py` |
| `tests/test_permission_authority.py` | CASE-L1-03/04/06/07/09/10 的决定与用权边界 | `python -m pytest -q tests/test_permission_authority.py` |
| `tests/test_permission_dispatch_races.py` | CASE-L1-01/04/05/08/10/11、CASE-LIFE-02/08 | `python -m pytest -q tests/test_permission_dispatch_races.py` |
| `tests/test_permission_fc_contract.py` | CASE-FC-01..09 | `python -m pytest -q tests/test_permission_fc_contract.py` |
| `tests/test_permission_herv3_continuation.py` | CASE-L1-01..03/09、CASE-LIFE-03/04/06/07 | `python -m pytest -q tests/test_permission_herv3_continuation.py` |
| `tests/test_permission_lifecycle.py` | CASE-LIFE-01..11、CASE-OPS-03 的直接生命周期边界 | `python -m pytest -q tests/test_permission_lifecycle.py` |
| `tests/test_permission_recovery.py` | CASE-LIFE-07/08/11、CASE-OPS-07 | `python -m pytest -q tests/test_permission_recovery.py` |
| `tests/test_permission_backend_controls.py` | CASE-L1-12、CASE-OPS-05/06；CASE-L2-09 的离线控制报告 | `python -m pytest -q tests/test_permission_backend_controls.py` |
| `tests/windows/permissions/` | CASE-L2-01..09、CASE-OPS-04 的实际 Windows 部分 | `python -m pytest -q tests/windows/permissions/ -m platform` |

每项 CASE 可以同时需要既有回归、上述 owning 测试和人工/平台证据；该表不表示一个文件单独证明全部要求。CASE-OPS-01/02/08 的静态护栏、测试充分性及性能/移交证据按其具体步骤记录，不另外建立仅复述清单的测试。

Windows test marker、环境前置和证据收集要求由 [L2_WINDOWS](L2_WINDOWS.md) 决定；未来实际执行时优先选择目录中的 owning node/module，上表目录命令用于明确范围的整批平台验收。没有真实受限进程/服务可用时应明确阻塞，不能落到模拟分支后仍报告该 CASE 通过。涉及真实 canary 的部分需单独记录授权和人工/自动化步骤，不能从普通 offline pytest 传递触发。

## 6. L0：零新增运行副作用

适用 PRD `LEG-01..08`、`LEVEL/MIG`；对应 [CODE_MAP MAP-02](CODE_MAP.md)。下面“无新增”指权限系统引入的影响，既有数据库、工具、恢复逻辑继续按基线运行。

| CASE / 状态 | 准备 | 动作 | 可观察结果 | 失败条件 |
|---|---|---|---|---|
| **CASE-L0-01** / 待执行 | I1 使用完全没有安全级别字段的旧配置；另建显式 L0 配置；基线与候选使用同一确定性 provider | 分别运行读文件、写文件、shell 和普通问答任务 | 两种配置均沿用基线授权、错误、取消与完成语义；不要求配置迁移或启用服务 | 旧配置默认为 L1/L2、任务新增审批、启动失败或既有能力被收窄 |
| **CASE-L0-02** / 待执行 | 记录基线工具 schema/catalogue；权限服务配置存在和不存在各一组 | L0 启动及执行普通模型请求，捕获真正发给 provider 的工具定义 | 工具集与语义和基线相同；无 `request_access` 或新权限控制工具 | 仅因为代码安装就加工具、改 schema、改变可调用集合 |
| **CASE-L0-03** / 待执行 | 相同 message/PCM/模型设置；归一化时间及随机相关 ID，保留所有语义字段 | 捕获基线与候选的全部 provider 请求 | 无权限 prompt、隐藏审批说明、额外 provider 请求或新等待回合 | 增加系统约束、把未决权限说明塞进历史、无任务需要也多调模型 |
| **CASE-L0-04** / 待执行 | 干净临时目录；对权限 store 构造/连接和文件创建做观察；配置为 L0 | 启动、两次 Run、正常退出和重启恢复 | 新权限库、grant/task/epoch 记录和迁移均不创建/读取；既有数据按基线保存 | L0 打开权限库、启动迁移、写 grant/审计任务或依赖权限 schema |
| **CASE-L0-05** / 待执行 | 权限服务未安装；随后模拟端口拒绝、不可读配置、无相关 token | 启动并完成普通 L0 工作 | 不探测/依赖该服务，任务按基线完成；不要求客户先修权限服务 | 无关服务故障导致 L0 启动/工具阻塞、轮询或警告风暴 |
| **CASE-L0-06** / 待执行 | 记录 shared IPC/HTTP 方法与权限客户端构造次数 | 并发两个 L0 Agent、正常取消一项、完成另一项 | 无新增 permission RPC、审批/续租/heartbeat；已有 RPC 序列保持语义 | unconditional manager/client init、每工具权限 allow-all RPC、后台续租 |
| **CASE-L0-07** / 待执行 | 记录基线 backend、进程身份、runner/子进程创建方式 | 同一 L0 shell 与第三方后端任务 | 使用基线 runner/adapter/身份；不附加 AppContainer/专用 broker 或换后端 | 自动切受限 runner、隐式换 backend、权限模块改变环境/凭据注入 |
| **CASE-L0-08** / 待执行 | 基线存在未完成 Run 和可恢复证据；保留独立历史 L1 存档但当前 L0 | 重启测试实例，执行下一普通 Run、停止并恢复 | 仍是基线恢复/取消路径；不创建 PermissionTask，不因旧存档发权限恢复/撤销调用 | 新权限 recovery hook 改写 L0 Run、自动重放/撤权、误把 Run 等同 Worker |

## 7. L1：申请、最小范围、执行与回收

适用 PRD `SCOPE/LEASE/UX/APPROVAL/L1/NFR`；对应 CODE_MAP MAP-03..06、MAP-10。

| CASE / 状态 | 准备 | 动作 | 可观察结果 | 失败条件 |
|---|---|---|---|---|
| **CASE-L1-01** / 待执行 | Agent A 基线只能访问 Z1，Z2 无 grant；准备真实写入 probe | 请求写 Z2，随后从可信 FC 批准精确路径/写能力 | 批准前无 dispatch/写入；仅产生一个关联请求；批准后同一 operation 执行一次并返回结果 | 先执行后询问、批准错误路径、重新开始无关任务或重复执行 |
| **CASE-L1-02** / 待执行 | T1 同时有 Z2 受限步骤和 Z1 独立读取 | agent 主动申请 Z2；未批准时继续 Z1 工作；随后批准 | 申请不要求预先拥有目标权限；独立工作完成；等待阶段 provider 调用数不因轮询增加 | 申请工具自身被目标权限挡住、整个系统/Agent 无关任务停住、模型反复问 |
| **CASE-L1-03** / 待执行 | 同一 task/scope 请求由两个并发步骤触发 | 并发申请、重试发送、批准后再请求相同授权内操作 | 未决请求合并；活跃 grant 复用；用户只需处理一次；新扩展范围才新问 | 每 turn/工具重问、重复 card 产生多个 grant、scope 被隐式扩大 |
| **CASE-L1-04** / 待执行 | 只有 once grant；两个并发操作争用同一能力，另有同 operation 重传 | 同时触发，并重放其中请求 | 只有一次合法消费/执行；同 operation 重传得到已知状态/receipt；另一个需新授权 | once grant 被消费两次、预检已消费、重传产生第二副作用 |
| **CASE-L1-05** / 待执行 | 工具预检通过后被 resource lease 阻塞；barrier 确认尚未 dispatch | 在排队时撤销 grant，或推进到 expiry；再释放资源 | 工具在真实 dispatch 前重新验权；无副作用；清楚报告 revoked/expired | 只信排队前结果、释放资源后仍执行、悄悄续租 |
| **CASE-L1-06** / 待执行 | 申请创建与批准间隔一段时间；批准限时 grant，保存绝对截止点 | 验证合法自定义时长及零/负数/超上限输入；从批准提交计时；跨 R1/R2 使用；到边界再尝试 | 无效时长拒绝；时长从可信批准提交起算；新 Run/heartbeat/重试不延长；期限或任务更早终态挡新动作 | 无效值变无限、从申请时间误起算、滑动延长、时钟回退使过期票据复活 |
| **CASE-L1-07** / 待执行 | 分别选择 once、当前 Run、本 Task、限时；另准备带 indefinite 的普通审批 payload | 覆盖 R1 结束、R2 同 Task、T1 结束、显式撤销；提交非法永久期限 | 按 PRD 已批准寿命精确结束；全部临时 grant 在所属 Task 提前结束时失效；普通审批不展示或接受永久期限；执行资源用完释放 | task grant 随每 Turn 错撤、Run grant 进入 R2、永久 payload 被接受、短期选择变永久或资源一直持有 |
| **CASE-L1-08** / 待执行 | 记录长期 Workzone/config revision；只批准临时 Z2 读取 | 在 Z2 读后撤权/完成；读取长期配置和下一任务能力 | 临时 overlay 可用后消失；长期 roots/revision 无越权变更；Z2 写入仍拒绝 | 临时授权写回长期 Workzone、撤权等配置刷新、目录 common-parent 扩大 |
| **CASE-L1-09** / 待执行 | 已登记请求和未执行操作，用户可拒绝/缩小/超时 | 分别拒绝、缩小范围、让批准卡过期；再提交合法新请求 | 未授权步骤不执行；工作上下文保留；允许独立工作和新申请；无普通错误重试风暴 | 拒绝被当同意、过期自动默认批准、要求用户重写全部任务、等待计失败预算 |
| **CASE-L1-10** / 待执行 | Z2 只授具体资源/动作；准备相对路径、大小写/alias/link 和不匹配参数 | 将已批准 operation 参数、资源或工具替换后执行 | canonical scope/digest 与真实目标匹配；越界拒绝或新问；实际读取对象可核对 | 参数批准后被换掉、路径归一化拓宽权限、工具名 alias 绕过 |
| **CASE-L1-11** / 待执行 | 真实工具调用链含 wrapper、tool gateway、子工具；记录全部 dispatch | 尝试绕开顶层 registry 直达副作用通道 | 每个承诺受控的通道都检查同一 PAO 权威；无法证明的通道标 partial/unavailable | 多个授权 writer、内部调用绕过、只验证顶层而声称所有通道 enforced |
| **CASE-L1-12** / 待执行 | HERV3 工作中有 pending/grant；配置允许切到不支持完整控制的第三方后端 | 按既有授权路径切 backend，并继续任务 | 告知实际覆盖、保存工作；原 grant 不扩大为新 backend 能力；既有 privacy/接收方约束仍有效，新增数据去向按需另批；无关工作可继续；配置等级与有效覆盖分开 | 全局硬阻断、自动切 L0、静默宣称 enforced，或把旧 epoch 票据带到新 backend |

## 8. FC：既有命令、可信批准与跨通道幂等

适用 PRD `FC-01..04/APPROVAL/UI/API`；详细合同以 [FC_CONTRACT](FC_CONTRACT.md) 为准。申请对象统一称 AccessRequest（旧稿 PermissionRequest 为同一对象）。测试使用既有 RuntimeCommand/RuntimeCallback、generic menu/action、既有公开 envelope，不新建测试专用公开协议。

| CASE / 状态 | 准备 | 动作 | 可观察结果 | 失败条件 |
|---|---|---|---|---|
| **CASE-FC-01** / 待执行 | 原有 generic action 客户端/测试 harness；保留基线公开 FC normalizer | 通过 `/safety` 打开请求、查看范围/寿命、批准/拒绝/撤销 | 既有 generic UI 能渲染并回送；授权经过 HASHI 公共 Functions；没有外部客户端或 Telegram 专用 adapter/schema 变更 | 需要新公开字段/版本/专用 adapter、现有客户端无法操作基本流程 |
| **CASE-FC-02** / 待执行 | 普通 ask_user question、model 伪造 answer、无认证答案各一组 | 回答“同意”、传 option_id 或伪造 `authenticated_user`/`grants_permissions`，随后执行受限操作 | 普通 question 的 `grants_permissions=False` 不变；未产生 grant；仅权限服务已验证的决定可授权 | 解析自然语言/option 即授权、信任客户端布尔字段、把普通问答升级为 approval proof |
| **CASE-FC-03** / 待执行 | Owner A 请求；B、未认证连接、agent 可持有的 shared token；合法 A principal/receipt | 各自尝试批准；再重放合法 receipt 和更改 request/scope | 只有受信 principal 在正确 owner/request/scope/revision 上生效一次；不泄漏其他 owner 详情 | 配置 owner/loopback/token 即冒充人类；跨 owner、改 scope 或旧 receipt 能批准 |
| **CASE-FC-04** / 待执行 | 同一 AccessRequest 在两个 channel/mirror 展示，独立 menu/action ID | 一端批准、另一端同时拒绝或重复批准；重放两端交付 | canonical decision CAS 只提交一次；各投影最终显示同一 revision；一次性执行不重复 | delivery/menu ID 被当授权身份、每 channel 独立 grant、互相覆盖已定决策 |
| **CASE-FC-05** / 待执行 | 一个有效 action 和绑定的 immutable scope digest/revision | 通过按钮及带 `--revision N` 的复制批准命令，修改 task、owner、scope、ttl 或 revision；再提交不带 revision 的文本 | 受信服务端快照决定范围；缺失/过时 revision 或修改被拒绝；scope 收窄生成新 revision；无额外确认同一合法决定 | 请求 ID 自动指向最新范围、callback 自带范围覆盖原请求、批准内容与用户看到的不一致 |
| **CASE-FC-06** / 待执行 | TUI direct 与 proxy 均无可证明批准者的身份；另有已连接可信 FC | TUI 查看/申请并尝试 approve/revoke；继续普通任务；在可信 FC 批准同请求 | TUI 清楚说明连接无法验证批准者；普通工作/独立步骤继续；可信 FC 成功后只按当前合法状态续跑 | 伪造 authorized_id、静默批准/切 L0/标 L2 enforced，或连普通工作也一并阻断 |
| **CASE-FC-07** / 待执行 | 打开旧 revision 卡片；随后缩小范围、撤销或让请求过期 | 推进超过菜单 15 分钟但不到请求 24 小时；点击旧卡、重开菜单、复制 action 到别的 session/context generation | 旧菜单失效、原请求仍在；重开不延长 24 小时；菜单/request revision 分开；新范围需审阅 | 旧卡复活 grant、refresh 重置请求 TTL、混用两种 revision、跨 session/context 复用 action |
| **CASE-FC-08** / 待执行 | 请求仍 pending，用户关闭一个前端或发生投影发送失败 | 关闭菜单、重连/刷新或从另一合法 endpoint 查看；重复 delivery retry；显式 withdraw pending 请求 | close 仅关闭投影；withdraw 只撤未决请求而不倒推 revoke；权威持久化可重建；无自动批准 | close 被当 deny/revoke、withdraw 改已授 grant、发送失败导致授权事实丢失或重复 |
| **CASE-FC-09** / 待执行 | agent 已能读取旧共享 admin/API token；新批准边界有独立身份/receipt | agent 用旧 token 经 Session command、admin command、简化 agent command、opaque act 及 generic Worker RPC 尝试批准 | 所有入口到同一权限写门；不能产生 human approval；普通业务按既有权限保持；审计有来源 | 某个旁路入口绕过、旧可见 token 变万能钥匙、执行主体可自行 approve/admin |

## 9. 生命周期：跨 Run、取消、恢复和代际

适用 PRD `TASK/LIFE/LEASE/UX/MIG`；对应 CODE_MAP MAP-06..09。

| CASE / 状态 | 准备 | 动作 | 可观察结果 | 失败条件 |
|---|---|---|---|---|
| **CASE-LIFE-01** / 待执行 | T1 的本任务授权；R1、R2 为两条 Message；分别记录 Turn 和 Epoch | R1 完成后同 Task 执行 R2，再完成 T1 | R1 结束不误撤 task grant；T1 终态后新动作拒绝；本 Run grant 仅覆盖 R1 | Task=Turn/Run/Worker、每 turn 重问、T1 完成后权限继续存在 |
| **CASE-LIFE-02** / 待执行 | T1 有活动工具/长连接；注入 cleanup 失败 | 将 T1 标记完成/取消，立即尝试新动作，随后检查清理状态 | 终态先 fence 新动作；cleanup pending/failed 可见；失败不延长业务权限 | 等 cleanup 成功才撤权、账本已撤但执行仍可进、用“已撤销”掩盖资源未清 |
| **CASE-LIFE-03** / 待执行 | PendingOperation 等批准；Task 用户暂停 | 批准请求，再刷新/重连；最后用户明确恢复 | 批准只保存合法 grant；暂停期间无执行/模型唤醒；显式恢复后重验并续跑一次 | 批准自动解除暂停、重连唤醒旧 runtime、暂停时消费 once |
| **CASE-LIFE-04** / 待执行 | PendingOperation 等批准；取消已线性化 | 发送晚到批准、重复 callback、此前在途结果 | 已取消任务不恢复；记录晚决定或拒绝原因；在途结果对账但不触发新工作 | 晚批准创建新 Run/Task、旧票据执行、取消后偷偷重试 |
| **CASE-LIFE-05** / 待执行 | W1/E1 已退出前准备 W2/E2，当前 Task 在 W2 合法绑定 | 延迟投递 W1 或未采用候选的 exit/cleanup 通知 | 只回收 E1；E2 绑定和合法工作保持；旧 token 不能被 W2 当新票据 | 按 agent 名 blanket revoke、旧进程事件撤销新代、票据无 epoch 绑定 |
| **CASE-LIFE-06** / 待执行 | HERV3 在 permission wait；另有真实背景任务作对照；隔离 Worker 允许 quiesce | 对选定 Agent 执行测试 quiesce/候选切换；期间批准或不批准 | 等待可持久化 park/drain，不因无限 is_generating 卡死；真实未完成 background 按既有规则处理；取消可唤醒 | permission wait 导致排空超时、强行杀死才切换、丢上下文或解除无关任务限制 |
| **CASE-LIFE-07** / 待执行 | 待批准记录已 durable；实际 side effect 未发生 | 杀死测试 Worker，恢复新 Worker；在恢复前撤权/过期，再批准另一次合法请求 | 恢复原 task/context；重新核对 owner/epoch/grant；过期操作不执行；合法恢复只一次 | 恢复 Python 栈幻觉、旧授权直接执行、重复创建审批、把恢复内容当新用户指令 |
| **CASE-LIFE-08** / 待执行 | probe 分别在未执行、已完成有 receipt、已执行但 receipt 丢失三个点停下 | 注入中断后恢复，重复 operation_id/工具响应 | 未执行可重验后执行；已完成不重放；unknown 先对账并如实显示不确定 | 一律重试、凭批准推断副作用未发生、同操作产生重复写入 |
| **CASE-LIFE-09** / 待执行 | 父 T1、受限子任务、独立获批后台 T2；各有不同 grant/期限 | 派生子任务尝试扩大范围/寿命；结束父任务 | 子能力至多为允许的子集；父撤销影响其依赖子票据；独立 T2 按自己的批准继续并可见 | 子继承永久大权限、父结束撤销独立 T2、后台任务未单独批准却脱离回收 |
| **CASE-LIFE-10** / 待执行 | 有活跃 grant 的 Agent/Session，可复制内容 | clone Agent、复制 Session、转移工作内容并尝试执行 | 内容可复用；主体、task/epoch/活动权限不复制；新主体按需申请 | 复制数据库/消息后能重用 ticket、同名 Agent 被视为同身份 |
| **CASE-LIFE-11** / 待执行 | 当前 Worker owner 活着，候选 Worker 启动；另有真实死 owner | 分别执行 reconciliation、迟到 receipt 和重复恢复通知 | 活 owner 不被候选抢占；死 owner 精确回收执行绑定；receipt 只结算匹配操作；Task grant 按定义保留或结束 | 活任务被误终止、错误 operation receipt 清掉 unknown、副作用证据丢失 |

## 10. L2：实体平台与真实覆盖

本节只定义产品级验收，Windows 身份、ACL、进程、IPC、网络和安装细节遵循 [L2_WINDOWS](L2_WINDOWS.md)。每一项必须记录实际 OS build、服务/runner 身份、包版本和可核查证据；对应 CASE 的 mock/unit 测试只作先行证据，**不能完成本节**。

| CASE / 状态 | 准备 | 动作 | 可观察结果 | 失败条件 |
|---|---|---|---|---|
| **CASE-L2-01** / 待执行 | 真实 Windows 服务与受限 runner；记录账户/SID、ACL、服务及二进制版本 | 从 agent/runner 身份读写控制策略、密钥、服务配置/二进制和启动参数 | 不得篡改权威控制面或批准身份；受信管理路径仍正常；失败有 OS 证据 | 同用户目录写保护冒充隔离、服务可被 agent 停改/替换、只给 mock permission denied |
| **CASE-L2-02** / 待执行 | 真实受限 HERV3 execution host 运行任务；Z1/Z2/Z3、允许与禁止的真实测试网络 endpoint；只批准 Z2 读取和一个目标 | 由 HERV3 调受限 runner 读写/联网，并分别从 host 和脚本尝试绕过 broker | 精确批准范围正常；host 和 runner 均不能越界直访 Z3、Z2 写或未批准网络目标；记录两者进程身份、broker 票据及目标 | 仅 shell runner 受限而 HERV3 host 可直访、只设置 cwd/env、靠提示词限制、绕过 registry/broker 访问成功 |
| **CASE-L2-03** / 待执行 | sandbox 可见的全部 IPC/handle 清单；无批准凭据 | 尝试 legacy generic start/service/route/admin/approve，以及伪造 agent/task/epoch | 只有专用允许操作可达；受信 binding 拒绝伪造；原宽 RPC 不可用作代执行绕过 | sandbox 借 shared manager 获得更高权限、approve 与 execute 共用信任身份 |
| **CASE-L2-04** / 待执行 | 长运行子进程/孙进程、打开文件与长连接；授权即将撤销 | 撤权、到期或终止 task；观察进程树/handle/连接与再次写入 | 新动作立即挡住；存活资源按规定关闭/终止或进入可见清理；无法派生逃逸子进程 | 只删除账本、旧 handle 无限使用、子进程留存不报、Job/清理证据缺失 |
| **CASE-L2-05** / 待执行 | 真正 named pipe/IPC 服务，合法 runner 和非授权本机进程 | 对合法连接、伪造身份、跨实例/旧 epoch、重放请求分别发起真实连接 | 服务以 OS/受信登记身份验证，并校验 task/operation；跨主体/旧票据拒绝；没有认证竞态 | 仅相信 JSON 的 agent_id、pipe 可被非授权连接、重放产生副作用 |
| **CASE-L2-06** / 待执行 | L2 task 活动；ticket 有短有效期；另有 L0 Agent 正常运行 | 断开安全服务、停服务或使能力健康证明过期 | L2 不发新无约束执行，旧票据受真实期限/fence约束；显示等待/恢复/实际覆盖；L0 不受牵连；可明确选择兼容路径 | 静默无沙箱重试、健康状态永久缓存、全局阻断、标 enforced 但服务已失联 |
| **CASE-L2-07** / 待执行 | 真实 alias/link/reparse、UNC/网络路径、子进程、继承 handle 场景，使用非敏感测试资源 | 尝试把已批准路径/进程边界重定向到未批准资源 | 实际访问对象仍受限制，不能通过继承或别名扩大；对不支持情形诚实声明 | 字符串前缀比较可绕过、已验证路径被替换后越界、未测能力宣称安全 |
| **CASE-L2-08** / 待执行 | 生产等价但仅测试用凭据；可观察 runner 环境、文件、命令行和 IPC payload | 在 runner 中枚举可见资源、打印环境到受控日志、尝试获取批准/服务密钥 | 不能获取批准权威或高权限服务密钥；执行仅拿短期最小凭据；日志不泄漏 | 继承整包 secrets/admin token、在 argv/env 暴露密钥、假定加密文件等于无法使用 |
| **CASE-L2-09** / 待执行 | 一条验证过的受控通道和一个不支持完整 L2 的 backend/browser/connector | 用户按已授权流程切换、再切回受控通道 | 实际 coverage 按通道显示；普通工作保持；切回需有效 epoch/服务证明；无静默降级或冒称全覆盖 | unsupported 导致所有任务锁死、自动替用户切 L0、任一未验证通道被标 L2 enforced |

## 11. 操作、采用、回滚与交付

适用 PRD `CORE/MIG/BACKEND/NFR`；对应 CODE_MAP MAP-01/02/09/12。这里的“采用”均指未来明确范围的测试实例，不授权当前或生产操作。

| CASE / 状态 | 准备 | 动作 | 可观察结果 | 失败条件 |
|---|---|---|---|---|
| **CASE-OPS-01** / 待执行 | 约定 docs-only 分支和基线，完整 staged/unstaged/untracked 差异 | 运行第 3 节静态命令、检查链接与状态 | 只有约定文档；Core tuple 来源唯一；无代码/配置/公开 schema/专用 adapter 改动；行为状态仍待执行 | 实现夹带、保护清单漂移、文档称已验证但无证据、自动运行 pytest/reboot |
| **CASE-OPS-02** / 待执行 | 后续一个有明确 owner 的实现提交；写出 defect/invariant 与 red 方案 | 跑 owning proposed/既有 focused 测试；共享边界改动再跑 curated core gate | 记录准确命令/count/skip、red/green、受影响直接消费者；失败不通过改文案隐藏 | 全库测试替代范围分析、测试只镜像源码、skip 当 pass、没有 red 原因 |
| **CASE-OPS-03** / 待执行 | 授权的隔离实例，选定 Agent A；另有 Agent B 与 shared 服务 | 验证 Agent-only min/same；另独立验证 shared 变更 max handoff、候选失败回滚 | Agent-only 不动 B/shared/Core；shared 变更由正确 handoff 采用；Core PID/lock 不变；失败回原代 | 自动扩大 max、Worker-only 冒充 shared 采用、冷启 Core、生产 reboot 充当测试 |
| **CASE-OPS-04** / 待执行 | 独立 L2 包/配置、现有 Core runtime/dependency fingerprint | 在授权测试机安装/升级/卸载 L2 服务，采用 Functions 客户端 | 服务独立身份、版本与 rollback 可查；Core interpreter/依赖和 protected source 不变；本地配置保留 | 把可选 native 依赖装进 Core、改 bootstrap、卸载删除工作成果或实例身份 |
| **CASE-OPS-05** / 待执行 | 同实例 L0、L1、L2 不同工作；权限服务可故障 | 关闭相关服务、损坏一条证据、恢复服务 | 影响局限于需该权威的操作；L0 完全可用；L1/L2 真正受控路径不静默 fail-open；故障/覆盖如实显示 | 全局锁死、不支持后端也阻客户普通工作、隐藏服务故障或无条件放行 |
| **CASE-OPS-06** / 待执行 | 有长期 Workzone、待审请求、活动 grant、用户成果 | 显式 L0→L1、L1→L2、L2→L1/L0；用户取消一次迁移 | 只在明确切级时迁移/清理；保留成果；无永久权限默认；取消保持原态；后续 L0 满足零新增副作用 | 自动切级、旧 ticket 在新级别继续生效、重复每 Run 迁移、临时授权写回永久配置 |
| **CASE-OPS-07** / 待执行 | 上一版/候选版，含 pending、revoked、expired、used 票据和 unknown 操作 | 执行可回滚采用失败与版本回退，随后恢复业务 | 撤销/expiry/once消费事实不能复活；unknown 保留待对账；不兼容存储明确停在受影响边界 | rollback 还原已消费权限、回旧缓存把权限放大、为了可用静默重放副作用 |
| **CASE-OPS-08** / 待执行 | PRD NFR 目标、并发负载、操作审计和平台证据 collector | 运行约定负载/审批/撤权测试，导出脱敏验收包并由第二人核对 | 延迟/并发按 PRD 目标记录；请求到决定到执行可追踪；真实覆盖/限制和未执行项清晰 | 只报平均值遮住撤权竞态、日志泄密、单元结果冒充平台结果或未测项标完成 |

## 12. Traceability 与完成条件

此表按需求族/主题关联，不伪造其他文档尚未稳定的标题锚点。具体实施里程碑 M00..M10 以 [IMPLEMENTATION_PLAN](IMPLEMENTATION_PLAN.md) 为准；每一实施提交在状态表写入下表的实际 M 编号和 CASE，不能仅填“全部测试通过”。

| 需求主题 | 代码接入图 | CASE 范围 | 现有基础 / 拟新增证据 |
|---|---|---|---|
| CORE-01..03：仅 HASHI、Core/公开协议/外部产品边界 | MAP-01、05、09、12 | FC-01，OPS-01..04 | EX-CORE、EX-FC、EX-LIFE；静态 diff 和真实采用 |
| LEG-01..08：L0 行为保持 | MAP-02 | L0-01..08，OPS-05/06 | proposed legacy 对照；实际 payload/DB/RPC/runner 差异 |
| TASK/LEASE：跨 Run 生命周期、一次性/期限/撤销 | MAP-04、07、08 | L1-03..07，LIFE-01..11 | EX-TOOLS、EX-HER、EX-LIFE；proposed lifecycle |
| SCOPE/L1：精确资源与执行门 | MAP-04、10 | L1-01/04/05/08..12 | EX-TOOLS；proposed enforcement 与真实副作用 probe |
| UX/APPROVAL：按需问、独立工作、不中断上下文 | MAP-03、05、06 | L1-01..03/09，FC-02/06..08，LIFE-03/04/06/07 | EX-HER、EX-FC；proposed wait/park |
| FC-01..04/UI/API：generic 命令、真实身份、mirror | MAP-05 | FC-01..09 | EX-FC；proposed principal/CAS/end-to-end |
| L2：独立控制与 OS 隔离 | MAP-09 | L2-01..09，OPS-04/05 | L2_WINDOWS 实体平台证据；mock 不能完成 |
| BACKEND：能力覆盖与诚实继续 | MAP-09、10 | L1-12，FC-06，L2-06/09，OPS-05/06 | 真实 backend/通道结果及 coverage 状态 |
| MIG/NFR：迁移、回滚、延迟与可追溯 | MAP-02、07、12 | OPS-06..08，L1-04/05，LIFE-05/08 | proposed operations、并发/故障/采用证据 |

完成一个级别需同时满足：该级别适用 CASE 均有通过证据；原级别无回归；阻塞/不适用均明确限制对外声明；Core/FC/范围护栏未破坏；热采用范围与真实生效版本已验证。L1 通过不意味着 L2 通过；某条 L2 执行通道通过不意味着全部 backend、browser 或 connector 受控。

### 里程碑 → CASE

| 里程碑 | 名称 | 主要 CASE；跨阶段案例保留同一编号 |
|---|---|---|
| M00 | 基线与 Core/FC 范围锁定 | OPS-01、FC-01 |
| M01 | 类型、配置与 L0 真旁路 | L0-01..08 |
| M02 | 权威账本、授权 CAS 与恢复 | L1-03..07、FC-04、LIFE-07/08 |
| M03 | /safety 标准 FC 和可信决定 | FC-01..09 |
| M04 | 执行拦截、排队后原子 claim 与工具覆盖 | L1-01/04/05/08/10/11 |
| M05 | HERV3 等待/续跑与生命周期 | LIFE-01..11、L1-02/09 |
| M06 | L1 验收、迁移和后端覆盖 | 全部 L0/L1/FC/LIFE 适用项、OPS-05/06 |
| M07 | L2 服务、身份与窄 broker | L2-01/03/05/08 |
| M08 | L2 受限 HERV3 host 和资源代理 | L2-02/04/07 |
| M09 | L2 撤销/故障/恢复与 Windows 攻防验收 | 全部 L2、LIFE-05/07/08 |
| M10 | 发布、采用、回退与移交 | OPS-01..08，汇总各级别全部适用 CASE |

### FC_CONTRACT 场景交叉索引

FC_CONTRACT 第 11 节的局部 `FC-C01..16` 是场景标签，与 PRD 需求 `FC-01..04`、本文件全局 `CASE-FC-*` 分开编号。

| FC_CONTRACT 场景 | 本文件 CASE |
|---|---|
| FC-C01 普通问答 | CASE-FC-02 |
| FC-C02 可信批准 | CASE-FC-01/03、CASE-L1-01 |
| FC-C03 一次授权重试 | CASE-L1-04、CASE-LIFE-08 |
| FC-C04 范围收窄 | CASE-L1-09/10、CASE-FC-05/07 |
| FC-C05 自定义时长 | CASE-L1-06/07 |
| FC-C06 菜单过期 | CASE-FC-07 |
| FC-C07 跨 Run/重启 | CASE-LIFE-01/05/07 |
| FC-C08 镜像并发 | CASE-FC-04/08 |
| FC-C09 纯文本 | CASE-FC-05 |
| FC-C10 无认证 TUI | CASE-FC-06 |
| FC-C11 伪造身份 | CASE-FC-02/03/05/09 |
| FC-C12 绕行入口 | CASE-FC-09 |
| FC-C13 凭据隔离 | CASE-FC-09、CASE-L2-03/08 |
| FC-C14 取消/拒绝 | CASE-L1-09、CASE-FC-08、CASE-LIFE-04 |
| FC-C15 无模型等待 | CASE-L1-02/09、CASE-LIFE-06 |
| FC-C16 兼容事实 | CASE-L1-12、CASE-FC-01/06、CASE-L2-09 |

### 单 CASE 记录模板（复制后填写；初始待执行）

```text
case_id:
status: 待执行
prd_topic:
implementation_milestone: 待关联 M00..M10
code_map_id:
implementation_commit:
baseline_commit: d6b6513973bfc8f9f22c40ddd8f72802f907baa0
command_or_steps:
environment_and_instance:
authorization_scope_for_live_or_platform: 未执行
fixture_and_object_ids:
expected_observation:
actual_observation: 待执行
side_effect_and_receipt_evidence:
red_reason_and_evidence:
green_evidence:
passed_failed_skipped_counts: 未执行
unverified_coverage_and_limitations:
reviewer:
started_at:
finished_at:
```

证据文件名、内容和日志位置以实际产物填写；不能预填不存在的 pass、截图或 receipt。接手者按 [LOCAL_AGENT_PROMPT](LOCAL_AGENT_PROMPT.md) 和 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md) 继续，先确认已有进展再选择下一项。
