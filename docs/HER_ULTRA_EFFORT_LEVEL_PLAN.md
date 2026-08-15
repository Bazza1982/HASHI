# 🏯 HER `/ultra` Effort 实施计划

> **状态**：架构已确认，实施中<br>
> **日期**：2026-08-15<br>
> **适用范围**：仅限 HER Backend<br>
> **规范地位**：本文件是 HASHI 仓库中的 canonical implementation plan

## 1. 已确认且不可变的决策

1. `ultra` 是 HER 正式 effort，位于 `max+` 之上，不是 profile。
2. `max+` 是最高单 Agent 线性执行 effort；`ultra` 是最高多 Agent 协同执行 effort。
3. Ultra 的规划、并发调度、Evidence、Assembly 和 Run Ledger 全部由 HER Backend 内部拥有。
4. Ultra 不依赖 Nagare、HChat 或其他 HASHI Backend。
5. 单次 Ultra run 最多同时运行 10 个内部 HER Sub-agents；Primary 的规划和 Assembly 不与 worker fan-out 同时调用模型。
6. HASHI Runtime 只提交一个 Primary turn，并最终收到一个 `BackendResponse`。
7. Sub-agent 不直接向用户发送 Persona 消息、问题或最终答案。
8. `/verbose`、`/commentary`、`/think`、final 和 control 继续使用现有 HER message router。

## 2. Effort 语义

| Effort | 执行方式 | 主要资源含义 |
|---|---|---|
| `low` | 单 Agent | 最多 12 次执行迭代 |
| `medium` | 单 Agent | 最多 32 次执行迭代 |
| `high` | 单 Agent | 最多 96 次执行迭代 |
| `xhigh` | 单 Agent | 最多 192 次执行迭代 |
| `max` | 单 Agent | 最多 384 次执行迭代 |
| `max+` | 单 Agent | 最多 512 次执行迭代及延长时间窗口 |
| `ultra` | Primary + 并行 Sub-agents | 多 session、DAG、验证、重试和 Assembly 的总协调投入 |

`ultra` 是用户和 HASHI 所见的正式 effort。底层单个 Claw 进程仍只接受
`low` 到 `max+`；HER Adapter 必须先截获 `ultra`，再为 Primary 和每个
Sub-agent 分配具体的单 Agent effort。禁止把
`CLAW_EXECUTION_EFFORT=ultra` 直接传给底层 Claw 进程。

## 3. 与 Nagare 的边界

| HER Ultra | Nagare |
|---|---|
| HER Backend 内部执行能力 | HASHI 层工作流系统 |
| 生命周期是一条 Primary request | 生命周期是独立 workflow/run |
| 只创建临时 HER sessions | 可协调配置 Agent 和不同 Backend |
| 最终只返回一个 `BackendResponse` | 公开 workflow 状态、步骤和 artifacts |
| Run Ledger 是 Backend 私有执行状态 | Flow state 是 HASHI workflow 状态 |
| 不依赖 Nagare | 不依赖某一个 Backend |

两者都需要 DAG、重试和状态记录，但 authority、生命周期、协议边界和用户
表面不同。`HERUltraOrchestrator` 与 `HERUltraRunLedger` 因此不是 Nagare
的重复运行时，也不得导入 Nagare。

## 4. 组件架构

```text
HASHI Runtime
  └─ HERAdapter.generate_response(effort="ultra")
       └─ HERUltraOrchestrator
            ├─ Primary Planner
            ├─ HERUltraTaskContractValidator
            ├─ HERUltraRunLedger
            ├─ HERUltraWorkerExecutor
            │    └─ Semaphore(max=10) + isolated HER sessions
            ├─ HERUltraEvidenceRegistry
            └─ Primary Assembly
                 └─ one BackendResponse
```

建议文件边界：

| 文件 | 职责 |
|---|---|
| `adapters/her_ultra.py` | contract、validator、ledger、DAG scheduler、assembly protocol |
| `adapters/her.py` | effort 入口、底层 HER invocation、session/process ownership |
| `adapters/stream_events.py` | 复用现有事件及 presentation ownership，不新增平行协议 |
| `orchestrator/flexible_backend_registry.py` | 对 HER 公开 `ultra` effort |
| `tests/test_her_ultra.py` | 纯离线 orchestration/ledger/validation 测试 |
| `tests/test_her_adapter.py` | Adapter 接入、session、stop 和 metadata 测试 |

## 5. 执行流程

### 5.1 Primary Planning

Primary 使用当前 HER persistent session 理解完整请求，生成严格 JSON plan。
Planning 返回的 session checkpoint 只属于该 Ultra run，Assembly 必须继续该
checkpoint；中间 worker 不能替换 Primary session。

简单请求可以返回 `ultra_not_beneficial=true`，由 Primary 直接生成答案，
但外部 effort 仍记录为 `ultra`。

### 5.2 Contract Validation

模型生成的 plan 在 dispatch 前必须经过确定性校验：

- `authoritative_goal` 与 Runtime 注入值完全一致；
- subtask ID 唯一且依赖存在；
- DAG 无环；
- required/optional、deliverables 和 acceptance 完整；
- model/provider 只能来自当前 HER Agent 的允许范围；
- 权限不能高于 Runtime 提供的 authority envelope；
- 写入任务声明 workspace strategy、write set 和 retry safety；
- subtask 数、并发数、replan 和 retry 均在断路器内。

无效计划 fail closed，由 Primary 在有限次数内修正，不能带病 dispatch。

### 5.3 Worker Fan-out

每个 Sub-agent 使用不可变 `HERWorkerExecutionSpec`：

```json
{
  "parent_request_id": "req-123",
  "run_id": "ultra-...",
  "subtask_id": "research-auth",
  "attempt": 1,
  "model": "local/deepseek-v4-pro",
  "effort": "high",
  "session_scope": "isolated_per_run",
  "permission_mode": "read-only",
  "allowed_tools": ["Read", "Grep", "Bash"],
  "workspace_strategy": "shared_read_only",
  "timeout_sec": 300
}
```

并发执行不得修改共享 Adapter 的 model、provider、effort、cwd 或 environment。
底层 invocation 必须接受 per-call override，并使用层级 ID：

```text
{parent_request_id}:ultra:{run_id}:{subtask_id}:attempt:{n}
```

### 5.4 Evidence 和 Assembly

Sub-agent 结果必须是结构化 JSON，并至少包含：

- `subtask_id`、`status`、`claims`；
- `evidence`、`artifacts`、`validation`；
- `uncertainty`、`unresolved_items`；
- `retry_safe` 和已知副作用。

缺少结构化结果或必要 evidence 视为 `malformed_output`，不能当作成功。

Primary Assembly 在模型综合前先执行确定性检查：required subtasks、artifact
存在性、结果版本、冲突、验证结果及 cancellation generation。通过后才把
有界结果送入 Primary session 完成最终回答。

## 6. HERUltraRunLedger

Run Ledger 是 HER 私有、持久、append-only 的 transition journal，并维护
可原子恢复的 snapshot。它至少记录：

```text
run_id / parent_request_id / authoritative_goal_hash
plan_revision / primary_session_checkpoint
subtask_id / dispatch_id / attempt_id / result_version
state / optional / dependencies / retry_safe
model / inner_effort / workspace / artifact hashes
cancellation_generation / delivery state / error history
```

重启恢复时只从已验证 checkpoint 继续。相同 dispatch/result ID 不重复执行或
接纳；未知副作用状态不自动重试。

## 7. Stop、失败与重试

- `/stop` 取消 Primary、所有 active workers、等待中的 retry/replan 和 Assembly。
- HER 保存 `parent_request_id -> child process/task` 所有权，拒绝旧
  `cancellation_generation` 的迟到结果。
- read-only 且 transient 的失败可按配置重试。
- write/run/send 等有副作用任务只有 `retry_safe=true` 且幂等键有效时可重试。
- required subtask 失败必须有限 replan 或向用户报告；不能静默跳过。
- optional subtask 可有记录地降级，并在最终回答说明影响。

## 8. Session 与用户交互

- Primary：一个 run 内沿 planning checkpoint 继续到 Assembly。
- Sub-agent：独立 session，不写入 Primary session identity。
- Sub-agent 禁止直接询问用户；只能返回 `requires_user_input`。
- Primary 把多个内部问题合并为一个有界 interaction，并生成唯一
  `interaction_id/receipt_id`。
- A/B/C、短回答和 CONTINUE 必须绑定该 receipt，不能解析为旧 Primary 或
  scheduler 选项。
- 暂停后的 Ultra run 恢复原 run/session/checkpoint，不重新执行已完成副作用。

## 9. 文件系统与权限

- research/review 默认共享只读 snapshot；
- mutating subtask 使用独立 Git worktree 或等价 immutable-base snapshot；
- Sub-agent 返回 patch/commit + validation，不直接合并；
- Primary 检查 base revision、dirty state 和重叠 write set 后整合；
- Tool Gateway 的 allowlist、access root、risk/approval 和 secret scope 来自
  Runtime，不由模型生成的 `allowed_actions` 提权。

第一可运行切片允许 read-only Ultra；在 worktree contract 完成前，任何模型
生成的 write subtask 必须 fail closed，而不是退化成共享写入。

## 10. 消息与可见性

Ultra 只发出现有 `StreamEvent`：

| 内容 | delivery class |
|---|---|
| dispatch、模型、工具、重试、验证 | `technical` → `/verbose` |
| Primary 明确创作的 Persona 进展 | `user_commentary` → `/commentary` |
| Provider 真正返回的 reasoning | `reasoning` → `/think` |
| 最终回答 | `final` mandatory lane |
| stop、等待用户、不可恢复失败 | `control` mandatory lane |

Sub-agent reasoning 默认只进入有界 audit，不直接展示。系统 timer/lease 不能伪装
成 Primary Persona commentary。

## 11. 配置

```python
HER_ULTRA_DEFAULTS = {
    "enabled": True,
    "max_concurrent_subagents": 10,
    "primary_inner_effort": "max+",
    "subagent_default_effort": "high",
    "subagent_timeout_sec": 300,
    "subagent_retry_limit": 1,
    "max_plan_revisions": 3,
    "max_subtasks": 32,
    "stall_detection_enabled": True,
    "strict_structured_results": True,
    "write_tasks_enabled": False,
}
```

`10` 是 Ultra effort 的内部并发硬上限，不是 HASHI 配置 Agent 的产品上限。
实际并发为 `min(ready subtasks, 10, deployment/provider/resource limit)`。

Ultra 不设置会促使模型提前降低质量的软 token 目标，但仍保留 timeout、retry、
replan、provider capacity、工具次数和无新增证据等安全断路器。

## 12. 实施切片

### 2026-08-15 实施检查点

- ✅ Slice 1 已实现：正式 effort 注册、严格 plan/result contract、最多 10 个
  read-only isolated workers、DAG、有限 retry、durable Run Ledger、usage 聚合及
  单一 `BackendResponse`。
- ✅ Slice 2 已实现首个可运行版本：persistent Primary planning checkpoint、有限
  plan correction、required-task 检查、Primary Assembly，以及内部 final/
  acknowledgement 防泄漏。
- 🟡 Slice 3 已实现 interaction receipt、稳定 `interaction_id`、Primary session
  内问题渲染、isolated-resume 绑定和 late-result cancellation fence；进程崩溃后从
  ledger 恢复同一个未完成 run 尚未实现。
- ⏳ Slice 4 与 Slice 5 尚未开始；因此 write subtask 继续 fail closed，尚未完成
  live canary，也不能把当前检查点称为完整 Ultra 发布。

### Slice 1 — Contract 与只读核心

- 注册 `ultra` effort，但不向底层 Claw 传递该值；
- 实现 plan/result dataclass、validator、DAG scheduler 和 Run Ledger；
- 实现最多 10 个 read-only isolated workers；
- 聚合 usage，并只返回一个 `BackendResponse`；
- 离线测试 invalid plan、dependency、并发、retry、cancel、duplicate result。

### Slice 2 — Primary Planning/Assembly 接入

- persistent Primary planning checkpoint；
- structured plan correction；
- deterministic pre-assembly checks；
- final answer、technical、commentary 和 reasoning 路由测试。

### Slice 3 — Resume 与跨 Session 交互

- interaction receipt；
- incomplete/CONTINUE 与精确 session 恢复；
- crash recovery、late-result rejection 和 exactly-once delivery 测试。

### Slice 4 — 安全写入

- Git worktree/snapshot manager；
- write-set conflict 和 base revision validation；
- patch/commit integration；
- side-effect idempotency 和 approval binding。

### Slice 5 — Live Canary

- 2、4、10 worker 并发；
- simple bypass、DAG、worker timeout、provider failure、stop、resume；
- fixed/flex、Telegram/Workbench、全部 channel toggle 组合；
- 资源、token、延迟、清理和 packaged HER provenance。

## 13. 完成标准

Ultra 只有同时满足以下条件才算完成：

1. `/effort ultra` 在 HER 可选并持久化，其他 Backend 不出现；
2. HASHI 观察到一条 turn 和一个 final；
3. 并发峰值不超过 10，依赖顺序正确；
4. Primary session 不被 worker 污染；
5. `/stop` 终止整棵 Ultra 执行树且拒绝迟到结果；
6. 所有 required claims 有 evidence，malformed output fail closed；
7. retry 不重复未知副作用；
8. commentary、verbose、think、final/control ownership 不交叉；
9. scheduler/Ultra/Primary 的 A/B/C 与 CONTINUE 不串线；
10. focused、full tests 和 live canary 均记录精确代码及 HER package provenance。
