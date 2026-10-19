# Telegram / FC 命令一致性修复指南 — 2026-10-19

状态：已实现，等待 Zelda 独立验收。本文件记录 HASHI3 的修复规范与实现事实，
不代表任何运行中的 Worker 已加载新代码，也不代表生产发布。

## 1. 不可变施工边界

执行者开始前必须逐项确认：

- 唯一施工仓库：`C:\Users\thene\projects\HASHI3`。
- 必须保持现有分支 `debug-issues-oct`；不得新建、切换或删除分支，不得创建额外
  worktree，不得 detached HEAD 提交。
- 当前分支必须包含视觉降级基线 `f5f85001`。不得通过 reset、rebase、强制覆盖或
  挑选提交来改写现有成果。
- 不得读写部署目标 `C:\Users\thene\projects\HASHI4`，不得同步、发布或重启
  HASHI4。
- 本轮不执行 `/reboot`、`/restart`、服务重启或生产发布。实现者完成源码、测试和
  本地提交后必须停下，等待 Zelda 独立验收。
- 发现未知未提交改动、分支不符或与其他写入者冲突时，立即停下并报告；不得 stash、
  clean、reset 或覆盖他人成果。

开始检查：

```powershell
Set-Location C:\Users\thene\projects\HASHI3
git branch --show-current
git status --short --branch
git merge-base --is-ancestor f5f85001 HEAD
python scripts/check_protected_core_changes.py
```

预期：分支为 `debug-issues-oct`，工作树无未知改动，视觉降级基线在当前历史中，
Protected Core 检查通过。

施工前必须完整阅读：

- `ARCHITECTURE.md`
- `docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`
- `docs/TESTING_POLICY.md`
- `docs/HASHI_COMMAND_UI_STYLE_GUIDE.md`
- `docs/HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md`
- `docs/repairs/HERV3_VISUAL_FALLBACK_20261018.md`

## 2. 负责人、层级与验证范围

- 功能负责人：Frontend Connector（FC）负责入口规范化和各前端呈现；PAO 负责
  Session、命令身份、幂等围栏、持久结果和重放语义。
- 命令注册表及原命令处理器继续拥有具体命令行为。不得在 Telegram 复制命令清单、
  policy 或模型/effort 列表。
- HERV3 不参与本次命令架构改造，只作为图片自动降级的强制回归边界。
- 工程层：可替换 Functions 层及其测试/文档。不得修改 Protected Core。
- 聚焦验证：Telegram、Session API/Workbench、TUI 对同一标准命令给出相同的语义
  结果；连接器可以有不同呈现，但不得有不同的授权、命令存在性、policy、执行结果、
  重放或持久状态。

`orchestrator.runtime_contract.CORE_SOURCE_PATHS` 是唯一 Protected Core 来源。当前
候选文件不在已知 Core 清单中，但实现者仍须在选择文件后和提交前各运行一次检查；
若检查报告 Core 改动，立即撤销自己的该项设计并在 Functions 层重做，不得使用
`--authorized` 或环境变量绕过。

## 3. 已确认的问题与根因

连接器注册表把 `telegram.native_command` / `telegram.native_callback` 标为
`standard_fc`，但 Telegram 原生命令仍在 `FlexibleAgentRuntime._wrap_cmd` 内自行：

1. 检查命令 policy；
2. 建立持久 reservation；
3. 直接调用 handler；
4. 不读取 handler 结果便固定写入 `ok: true`。

这不是 Telegram 网络问题，而是 FC 统一入口未真正成为所有前端的单一语义入口。
已经用当前 HASHI3 代码复现：

- handler 返回 `{"ok": false, ...}`，Telegram 仍把 `frontend.command_result`
  持久化为成功；
- Telegram 本地 policy 拦截时，handler 没有执行，但没有相应的持久命令结果；
- 未知 Telegram 命令被动态 fallback 静默忽略，既无明确回复，也无规范失败结果；
- callback 的 wrapper 虽保留 Mapping 到嵌套 `result`，外层仍固定 `ok: true`，存在
  同类错误状态风险。

这些是本次修复必须消除的行为差异。不得用“提高存储上限”、修改 Telegram 文件大小、
增加错误文案或只修 Workbench 路由来掩盖。

## 4. 目标架构

标准命令必须形成以下单向路径：

```text
前端传输
  -> 连接器专属传输检查
  -> FC 兼容适配器规范化
  -> 统一命令准入/执行服务
  -> 原命令处理器
  -> 统一 typed outcome + PAO 持久结果
  -> 各连接器呈现
```

### 4.1 只允许连接器专属的检查

Telegram wrapper 可以且必须保留：

- Telegram 用户和企业频道边界；
- 消息新鲜度及现有过期确认；
- `update_id` / `message_id` / callback ID 等稳定传输身份；
- Telegram reply/edit/answer、HTML 转义和本地化呈现；
- 媒体下载前的 Telegram 文件元数据、下载错误和格式边界；
- Telegram 原生按钮自身已有的过期 token 检查。

这些检查只保护 Telegram 传输，不能决定标准命令是否存在、是否被 policy 禁用、
如何执行、是否成功或如何重放。

### 4.2 必须统一拥有的语义

以下内容必须只有一个 Functions 层所有者，并被所有 FC 路由调用：

- `normalize_compatibility_command` 和注册过的 adapter/customization；
- 标准命令是否存在，以及 connector-local override 是否适用于当前连接器；
- `_is_command_allowed` / `supported_commands` 所表达的共享 policy；
- typed invocation 的 actor、connector、endpoint、Session、generation、命令和脱敏参数；
- 副作用之前的 durable reservation；
- completed replay、pending/outcome-unknown 和 idempotency conflict；
- handler 返回值、异常和 durable completion 的真实结果；
- `frontend.command_result` 的 completed/failed 状态。

不得再由 Telegram、Session API 和 TUI 分别复制这套判断。

## 5. 推荐实施方案

### 5.1 建立一个连接器中立的执行器

优先在 `orchestrator/frontend_command_admission.py` 扩展一个窄职责的统一执行器，
或在同一 Functions owner 下建立小模块。不要把新逻辑塞进 Core，也不要另建第二份命令
注册表。

执行器输入至少应包括：

- runtime；
- 已规范化的 connector/adapter；
- Session ID、owner、context generation；
- client/request/transport identity；
- actor；
- command 与原始 arguments（持久化前使用现有脱敏 owner）；
- 一个调用原命令 handler 的 awaitable closure；
- 连接器呈现所需、但不参与语义决策的最小 metadata。

执行器返回一个稳定 Mapping，至少包含：

- `ok: bool`；
- `command`；
- `execution_state`；
- 成功时的 `result`，失败时稳定的 `error_code`；
- `request_outcome`（适用时）；
- replay/outcome-unknown 标记；
- 完成后产生的 command event ID。

实现要求：

1. 先建立规范化命令身份，再执行共享命令存在性、connector-local 和 policy 判断。
2. 未知、禁用或连接器不允许的命令是“已知的无副作用失败”：建立并完成一个
   `ok: false` 的规范结果，handler 不得被调用。
3. 允许的命令必须在任何 handler 副作用前 reservation；只允许一层 reservation，
   不得在 Telegram wrapper 与共享执行器各做一次。
4. completed replay 返回保存的语义结果，绝不再次调用 handler。是否重新发送同一张
   Telegram 卡片属于呈现决定，不能制造第二次副作用。
5. pending replay 返回 `command_outcome_unknown`，不得猜测成功、不得自动重试。
6. handler 返回 Mapping 时，若存在布尔 `ok`，它就是规范成功状态；`ok: false` 必须
   生成 failed event，并保留稳定错误码。不得再嵌套后把外层固定为成功。
7. 旧 handler 正常返回 `None` 且未抛异常时可兼容为成功。不得通过解析回复文案来猜
   成败；需要失败语义的 handler 应返回 typed Mapping。
8. handler 抛异常、结果形状无效或完成持久化失败时，不得写成成功。若无法证明副作用
   尚未发生，reservation 保持 pending/outcome-unknown，且禁止自动重放。
9. `ticket.complete()` 的 durable 失败不能被吞掉；更不能为“让 UI 成功”再次执行命令。
10. 结果大小、参数脱敏、Session generation 和 idempotency conflict 继续使用 PAO 现有
    contract，不另造数据库或旁路日志。

可复用的现有 owners：

- `orchestrator/frontend_compatibility.py`
- `orchestrator/frontend_command_admission.py`
- `orchestrator/command_interaction_bridge.py`
- `orchestrator/admin_local_testing.py`
- `orchestrator/session_store.py` 的公开 reservation/completion API

除非测试证明现有公开 contract 无法表达要求，不修改 `session_store.py`。尤其不得仅为
Telegram 增加独有持久表或结果格式。

### 5.2 改接 Telegram 原生命令

调整 `FlexibleAgentRuntime._wrap_cmd` 与必要的 binding：

- wrapper 保留第 4.1 节的 Telegram 传输检查；
- 传输检查通过后，调用已注册的 `telegram.native_command` adapter 和统一执行器；
- 删除 wrapper 自有的共享 policy 分支和固定 `ok: true` completion；
- 原 handler 仍可接收真实 Telegram `Update`/context，以免破坏原生菜单与回复；
- audit session 记录统一结果，但 audit 日志不能成为结果事实源；
- 动态命令 fallback 对未知 `/command` 返回本地化的明确错误，并持久化
  `command_not_found`；不得把它当普通聊天消息，也不得静默丢弃；
- 禁用命令返回本地化 `command_disabled`，同时存在 canonical failed result；
- 已完成和 pending replay 不再调用 handler。

不要把所有 handler 大规模重写为 Session API handler。修复共享入口和真实结果传递，
只对红测揭示的少量 handler 补 typed outcome。

### 5.3 改接 Telegram callback

`_wrap_callback` 同样不得固定外层成功：

- callback 的授权、频道边界、callback answer/edit 和原生 token 新鲜度仍由 Telegram
  transport/presentation 负责；
- 标准命令/action 的 policy、reservation、handler 结果和 completion 走同一个共享
  outcome owner；
- handler 返回 `ok: false` 时，持久 event 必须是 failed；
- 未注册 callback 必须得到安全、可见、可本地化的失败，不执行未知动作；
- Workbench 菜单的 issued action ID/revision 继续由其菜单 contract 验证。不要为原生
  Telegram 按钮伪造 Workbench revision；不同呈现协议可以不同，最终动作语义必须相同。

run-question 等明确登记为非 command 的 callback 继续使用其 PAO answer transaction，
不要强行塞进命令 admission。

### 5.4 保留 connector-local 命令差异

真正登记为 `connector_local` 的命令可以只在指定连接器执行。这是 registry 中显式的
产品差异，不是旁路。要求：

- 从现有 customization 派生，不复制名字清单；
- 其他连接器收到该命令时给出规范、持久的“不适用于当前连接器”结果；
- 不得把 connector-local handler 当 HASHI 标准命令执行；
- `/help` 和 Telegram picker 仍从同一注册 metadata 派生。

### 5.5 不做的事情

- 不放宽 Telegram 用户、频道或 freshness 安全边界。
- 不删除幂等 reservation，不把 pending 当失败后重试。
- 不通过增大审计/结果存储上限解决图片 Base64 问题。
- 不在 FC 之前根据选中模型拒绝图片；图片能力路由仍由 HERV3 统一诊断。
- 不更改默认模型，不跨 Provider 选择视觉 fallback。
- 不顺手重构全部命令 handler、UI 文案或数据库。
- 不修改共享模型目录来加入实例特例。
- 不接触 HASHI4，不 hot reboot，不做生产实测。

## 6. 必须先写的红测

优先强化已有测试；只有当一个新的组件边界确实需要独立所有者时才新增测试模块。每个
行为测试都要在提交或交接记录中说明：旧实现为何红、新实现为何绿。

### A. 命令真实结果

1. Telegram handler 返回 `{"ok": false, "error_code": "deliberate_rejection"}`：
   canonical response 为失败，`frontend.command_result` 状态为 failed，错误码保留。
2. handler 返回成功 Mapping：结果字段保留，event 为 completed。
3. 兼容 handler 返回 `None`：无异常时成功，且只执行一次。
4. handler 抛异常：不得持久化成功；结果为 outcome unknown 或可证明的 typed failure。

### B. 准入一致性

1. policy 禁用同一命令时，Telegram 与 Session API 的 handler 都不执行，二者都有
   `ok: false` 的持久结果和同一稳定错误语义。
2. 未知 Telegram 命令有明确本地化回复及 `command_not_found` failed event，不进入
   普通聊天 Run。
3. connector-local 命令只在登记的连接器执行；其他连接器规范拒绝。
4. 同一命令在 Telegram、Session API/Workbench 和 TUI 的 semantic projection
   （command、ok、error_code、execution_state）一致；HTML/按钮等呈现可以不同。

### C. 幂等与持久失败

1. 同一 Telegram update/callback 重放两次，handler 副作用计数仍为 1。
2. completed replay 使用已保存结果，不生成第二次命令 event。
3. pending replay 不执行 handler，并返回 outcome unknown。
4. 相同 request ID 配不同内容触发 conflict，不执行 handler。
5. completion 写入失败后不得报告成功或重跑 handler。

### D. callback

1. callback handler 返回 `ok: false` 时 event 为 failed。
2. 未注册或过期 callback 不执行动作，并给出安全反馈。
3. 非 command 的 run-question callback 不被错误纳入命令 reservation。

### E. 图片降级强制回归

1. 当前模型 `deepseek-v4-pro` 收到图片时，Telegram 与 Session API 都允许请求进入
   canonical HERV3 路由，并临时使用同 Provider 的 `deepseek-flash`。
2. Agent 默认模型在请求后仍为 `deepseek-v4-pro`。
3. 无同 Provider 候选且无已配置 LAN Vision 时，返回 typed
   `MODEL_MODALITY_UNSUPPORTED`。
4. 图片原始字节不进入 command/audit result；不得因 Base64 放大触发持久化失败。

### F. 原有边界回归

- 未授权用户、企业频道限制和 stale command 仍在 handler 之前拦截；
- 参数持久化仍使用现有脱敏逻辑；
- `/help`、picker、动态命令和 connector-local customization 仍从注册 owner 派生；
- Telegram 的本地化、HTML escaping、菜单 edit/answer 不退化。

## 7. 建议修改范围

优先检查并按最小差异修改：

- `orchestrator/frontend_command_admission.py`
- `orchestrator/frontend_compatibility.py`
- `orchestrator/flexible_agent_runtime.py`
- `orchestrator/runtime_command_binding.py`
- `orchestrator/command_interaction_bridge.py`
- 必要时 `orchestrator/admin_local_testing.py`
- 对应测试与本文件所指向的正式设计/FYI 文档

优先复用 `reserve_frontend_command_invocation`、`build_frontend_command_invocation`、
`normalize_compatibility_command` 和现有 command registry。不要因为文件列在这里就全部
修改；也不要编辑任何 `CORE_SOURCE_PATHS` 文件。

建议测试落点：

- `tests/test_slash_command_audit.py`
- `tests/test_runtime_command_binding.py`
- `tests/test_command_interaction_adapters.py`
- `tests/test_frontend_design_conformance.py`
- `tests/test_frontend_terminal_local_commands.py`
- `tests/test_session_api.py`
- `tests/test_runtime_media.py`
- `tests/test_her_v2_multimodal.py`

行为改变后同步更新 owning decision 和 FYI；至少让前端架构文档明确“Telegram native
adapter 共享同一个 admission/outcome owner，transport checks 不拥有命令语义”。

## 8. 验证顺序

### 8.1 选择文件后

```powershell
python scripts/check_protected_core_changes.py
```

### 8.2 红/绿证据

先运行新增/强化的精确测试节点，记录旧实现的失败原因，再实现修复并用同一节点证明
变绿。不得仅添加实现后必然通过的 mock 测试，不得把回复文案当作执行成功证据。

### 8.3 聚焦与组件测试

按实际改动选择精确节点；完成后至少运行：

```powershell
python -m pytest -q tests/test_slash_command_audit.py tests/test_runtime_command_binding.py tests/test_command_interaction_adapters.py tests/test_frontend_design_conformance.py tests/test_frontend_terminal_local_commands.py tests/test_session_api.py
python -m pytest -q tests/test_runtime_media.py tests/test_her_v2_multimodal.py
```

若改到共享 runtime/registry/Function 边界，再运行 curated Core gate：

```powershell
python -m pytest -q
```

不要用多个 pytest 进程并发共享默认临时目录。不要把 skip 报成 pass。报告每条命令的
passed、failed、skipped、deselected 数量。

### 8.4 静态与边界检查

```powershell
git diff --check
python scripts/check_protected_core_changes.py
git status --short --branch
```

对改动的 Python 文件运行仓库现有 lint/format 检查。若工具缺失，明确报告，不得静默
跳过或现场升级运行中 Core 的依赖。

## 9. 提交和交接要求

检查通过后，在当前 `debug-issues-oct` 分支创建一个连贯的本地提交。不得 push、merge、
rebase、部署或 hot reboot。提交内容应同时包含：

- Functions 层实现；
- 能证明旧缺陷的红/绿回归测试；
- owning decision/FYI 更新；
- 本指南若实现事实发生变化所需的状态更新。

交接给 Zelda 时只报告以下事实：

1. 当前仓库与分支；
2. 本地提交 ID；
3. 修改的行为；
4. 红/绿证据；
5. 每个验证命令及结果；
6. Protected Core 检查结果；
7. 已知未验证项或风险；
8. 明确声明 HASHI4 未触碰、没有 reboot、等待 Zelda 验收。

实现者不得自行宣布“已验收”或“已上线”。

## 10. Zelda 独立验收门槛

Zelda 收到提交后将独立：

1. 核对仍是 HASHI3 / `debug-issues-oct`，检查完整 diff 和提交历史；
2. 重新运行 Protected Core 检查、红/绿重点节点、组件测试与必要 Core gate；
3. 检查三条已复现缺陷确实转绿，而不是测试期望被改成错误行为；
4. 检查 Telegram 专属检查只剩传输/呈现职责，标准命令语义只有一个 owner；
5. 检查 `f5f85001` 的跨前端图片自动降级没有回退；
6. 在获得当时明确授权后，才可 hot reboot HASHI3 并做真实 Telegram +
   Session API/Workbench 验收；
7. 分别报告源码提交、运行加载、真实前端结果和用户实测，不把它们混成一个“成功”。

任何 HASHI4 同步或生产加载都属于后续独立授权，本指南不授权。

## 11. 完成定义

只有全部满足才可提交给 Zelda 验收：

- [x] 仅 HASHI3 被修改，分支始终为 `debug-issues-oct`；
- [x] 无新分支、worktree、rebase、reset 或挑选提交；
- [x] Protected Core 无改动；
- [x] Telegram 命令不再固定写成功；
- [x] 未知、禁用、connector-local 拒绝都有 canonical failed result；
- [x] command/callback 重放不重复副作用，pending 不自动重试；
- [x] Telegram、Session API/Workbench、TUI 共享 FC/PAO 语义入口；
- [x] Telegram 传输安全和呈现能力未被削弱；
- [x] HERV3 图片自动降级回归通过，默认模型不改变；
- [x] 红/绿证据、组件测试和必要 Core gate 已记录；
- [x] owning decision/FYI 已更新；
- [x] 工作树干净，变更已在当前分支本地提交；
- [x] HASHI4 未触碰，无 reboot，状态明确为“等待 Zelda 验收”。
