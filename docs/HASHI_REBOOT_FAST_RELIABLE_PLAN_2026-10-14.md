# HASHI 热重启提速、可靠性与回执修复方案

状态：实现、HASHI3 标准 FC 现场验收及离线恢复复核完成；HASHI3 当前在线（现场拓扑为 11 Workers）
日期：2026-10-14
开发分支：`debug-reboot`（仅 HASHI3）
功能所有者：PAO；用户消息由 Frontend Connector Functions 所有
工程层：Functions
不在范围：Core、Remote、`/restart`、HASHI4 运行实例

## 1. 决策摘要

本修复不靠增加超时、增加轮询、盲目提高并发或再加一层健康检查。它把
`/reboot` 统一为一条简单的事务流程：

> 接受请求并通知用户 → 在旧服务继续工作的同时准备候选版本 →
> 只在最后切换时短暂封锁目标 → 用已有身份回执确认结果 → 给出明确结论

同一条流程覆盖单 Agent 的 `min|same`、编号和分组重启，以及
`max` 全 Functions 热重启。差别只在目标范围和切换所有者，不再维护两套
互相漂移的判断方法。

本方案作为一个完整修复包实施和验收。可以分提交形成安全检查点，但在所有
正确性、消息和性能门槛通过前，不把其中一半发布到生产。

## 2. 已确认的问题

### 2.1 替换其实成功，却被 PID 判断成失败

当前最终确认把“新 PID 必须与旧 PID 不同”当作硬条件。Windows 可以很快
复用 PID，因此旧 Worker 已退出、新 Worker 已启动并完全就绪时，仍可能因
数字相同被判为失败。

现有 Worker 回执已经包含更可靠的事实：

- Agent 身份；
- 每次启动唯一的 `worker_nonce`；
- generation ID；
- Worker 启动和采用时间；
- 生命周期阶段；
- accepting、backend ready 和 startup success；
- 由 Supervisor 持有的真实进程对象。

不需要增加新的服务、数据库或反复查询。问题是现有判断选择了错误的主事实。

### 2.2 用户消息把已知事实压成一句“无法确认”

当前消息层只擅长表达全成功或全失败。即使系统已经知道 25 个成功、1 个失败，
也可能只显示“切换已完成，但暂时无法确认代理是否在线”。这句话没有回答：

- 整体是成功、部分成功还是失败；
- 哪个 Agent 有问题；
- 失败的是哪项检查；
- 其余 Agent 当前是否可用；
- 原状态是否已经恢复。

### 2.3 `max` 和单 Agent 重启都在重复做昂贵工作

2026-10-14 的 HASHI4 只读诊断显示，一次 `/reboot max` 耗时约
143.3 秒，其中：

| 阶段 | 约耗时 | 结论 |
|---|---:|---|
| 接受、通知和轻量预检 | 0.4 秒 | 不是瓶颈，应保留 |
| generation 资格检查和制品准备 | 69.7 秒 | 存在重复扫描、重复验证 |
| 共享 Functions 与 26 个 Worker 启动 | 59.2 秒 | 每个 Worker 重复扫描和导入完整闭包 |
| 最终确认和回执 | 0.2 秒 | 不是瓶颈 |

同一份约 538 模块的制品在一次操作中被多次完整散列；每个 Worker 又在受保护
bootstrap 已验证制品后，重复两次完整字节检查，并导入全部模块验证一次已经由
隔离 probe 验证过的公共契约。26 个 Worker 会把同一工作放大 26 倍。

单 Agent `min|same` 也走相同的 generation 准备和 Worker 启动路径，因此
Lumi 的 76.1 秒并不是合理的“单 Agent 成本”，而是共享的重复工作落在了一个
Agent 身上。

### 2.4 准备阶段过早封锁，扩大了用户不可用时间

`max` 当前在发布共享替换请求前就封锁所有 Agent 路由。随后约 70 秒的资格
检查和制品准备期间，旧 Functions 明明仍可工作，用户、定时任务和 heartbeat
却已经看到“Agent 正在重启”。

Core 的共享 Functions handoff 本身已经在真正切换时执行 quiesce、drain、
候选就绪确认、提交或恢复。Functions 层提前封锁整个准备阶段既重复，又把准备
时间错误地变成停机时间。

已确认 heartbeat 定义没有被删除，但在长封锁期间到期的执行会失败或产生噪音。
修复目标是让准备阶段正常运行，只在短暂切换窗口延期一次，而不是丢失、重复或
报成 Agent 故障。

### 2.5 已经正确的行为必须保留

下列行为不是问题，不能为了提速而删除：

- 在 Workbench 断开或路由封锁前，先持久化请求并通知“开始热重启”；
- 精确解析目标，绝不扩大范围；
- 对新代码做一次真实的隔离资格检查；
- 只采用已提交且符合运行时契约的代码；
- 由受保护 bootstrap 在进程边界验证实际制品；
- 候选 Worker 必须完成真实 backend 初始化；
- 切换前失败时保留或恢复旧 Worker；
- 最终结果先持久化，再尝试发送通知；
- `min|same` 不替换共享服务，`max` 不替换 Core 或 Remote。

## 3. 范围和边界

### 3.1 `/reboot` 与 `/restart` 明确分开

`/reboot` 是热采用：

- Core 进程和实例锁保持不变；
- `min|same` 只替换所选 Agent Worker；
- group 只替换明确选择的 Workers；
- `max` 通过现有 Core handoff 替换共享 Functions 和当前运行的 Workers；
- Remote 保持独立。

`/restart` 是实例级冷生命周期，不在本方案内。不得因为热重启慢而偷偷改成
冷重启，也不得用冷重启作为测试捷径。

### 3.2 不修改受保护 Core

本方案不得修改以下受保护路径，包括：

- `orchestrator/kernel_process.py`；
- `orchestrator/kernel_artifact.py`；
- `orchestrator/function_worker_bootstrap.py`；
- `orchestrator/function_worker_protocol.py`；
- 其他 `CORE_SOURCE_PATHS` 项。

受保护 bootstrap 的一次制品验证保留，作为每个新进程的安全边界。所有重复工作
清理、状态判断、回执和消息调整都在 Functions 所有者中完成。若实现中发现必须
改变 Core 协议，应停止该项并单独提出 Core 大版本迁移，不能借本方案绕过。

### 3.3 部署按 Function generation，而不是按提交号

- 已提交的 Function 字节发生变化时，只能通过标准 Frontend Connector
  `/reboot` 路径采用，并选择最窄的正确范围；
- 只有文档、测试或发布说明变化时，Function generation 没有变化，不执行
  `min`、`max` 或任何其他运行采用命令；
- source commit、Function generation 和当前运行状态分别报告，不为追求“提交号
  看起来一致”而制造一次无意义热重启；
- 长期 Core 不得由临时测试、验证或前台命令执行器托管。验收只连接已经运行的
  HASHI3；Core 不可用时报告独立运行故障，不得以 `/restart` 代替 Function
  部署或故障恢复。

## 4. 目标流程

### 4.1 阶段 A：接受与开始通知

1. 精确解析并持久化目标集。
2. 持久化 accepted 回执。
3. 向发起界面投递“🔄 开始热重启…”。
4. Workbench 的开始响应已经提交后，才允许进入任何可能断开连接的步骤。
5. 外部消息发送仍采用现有两秒 head-start：记录发送失败，但不无限阻塞重启。

这里保留现有正确行为。通知成功不是安全条件；“请求已持久化且界面有机会看到
开始状态”才是进入准备阶段的条件。

### 4.2 阶段 B：在旧服务在线时准备

旧路由保持开放，不设置 reboot fence。

1. 对精确目标做一次并发活动预检。目标本来就忙时立即拒绝，避免浪费准备成本。
2. 计算轻量资格键：
   - 当前 Git commit；
   - qualified manifest 路径是否干净；
   - runtime fingerprint；
   - Function API、Worker protocol 和 generation schema；
   - 会影响资格的 observer/config 摘要。
3. 资格键命中现有合格 generation 时，复用其 probe receipt 和制品，不重建
   AST 依赖图、不重新导入完整闭包、不重新复制制品。
4. 未命中时，只运行一次完整隔离 probe，只物化一次内容寻址制品。
5. 为选定目标准备候选 Worker；`max` 由候选共享 Functions 准备全部当前
   Workers。旧共享 Functions 在此期间继续服务。
6. 候选服务在提交前不得绑定活动入口、领取 Telegram 更新或执行任务。

资格键不能只依赖 mtime。Git 的精确路径清洁证明、commit、runtime 和制品
generation 必须全部匹配。Windows 上制品目录的只读位不是充分安全证明，因此
每个新进程中现有受保护 bootstrap 的实际字节验证继续保留。

### 4.3 阶段 C：短暂、事务性的切换

准备完成后才进入短切换窗口。

1. 再做一次目标活动检查。这不是重复预检：
   - 第一次避免在目标已经忙时做昂贵准备；
   - 第二次封住准备期间新任务进入的竞态。
2. 只封锁精确目标；`max` 只在 Core handoff 的切换窗口封锁全部目标。
3. 等待已经接受的调用完成，并 quiesce 旧 Worker。
4. 用候选已经返回的 READY/ACTIVE 回执验证身份和健康。
5. 原子替换路由指针；不得在多目标指针替换中间 await。
6. 立即开放新路由。
7. 提交后异步退出旧 Worker；退出异常不得重新阻断已经健康的新路由。

所有等待由现有 lifecycle event、进程 handle 或 IPC 回执驱动，不新增固定 sleep，
也不对 26 个 Worker 多轮轮询。

### 4.4 阶段 D：一次确认并发送有结论的消息

切换时已经取得的 activation 回执是主要证据。提交后只读取一次权威拓扑快照，
不再对所有 Worker 做多轮相同检查。

结果先持久化，再由 Frontend Connector 渲染和投递。通知失败只影响 delivery
状态，不改变重启事实，也不重新执行重启。

## 5. 正确的 Worker 替换判据

### 5.1 权威身份

新 Worker 的权威身份是：

`Agent identity + expected worker_nonce + target generation_id`

成功还必须同时满足：

- Supervisor 持有的候选进程对象仍存活；
- phase 为 ACTIVE；
- accepting 为 true；
- backend_ready 为 true；
- startup_success 为 true；
- 路由当前指向该候选。

`worker_nonce` 在准备候选时已经生成，READY/ACTIVE 回执中已经返回，所以此
判断不增加 RPC、轮询或等待。

### 5.2 PID、启动时间和旧进程退出各自的作用

- PID 仅用于诊断，不是替换身份。
- `worker_started_at` 和 `worker_adopted_at` 用于时间线、排序和诊断，
  不单独决定成功。
- 旧进程退出由 Supervisor 持有的旧 `Process` handle 和 shutdown receipt
  判断，不用“PID 数字是否不同”代替。
- 新旧 PID 相同是 Windows 合法复用，应正常成功，不能显示警告。
- 若新路由已确认健康、旧路由已隔离，但旧进程还在后台退出，结果为
  “成功并有清理警告”，而不是把可用的新 Agent 报成失败。
- 若旧进程仍可能接收流量，不能降级为警告；切换前应回滚，提交后应明确报告
  路由隔离失败。

这套判据使用现有事实，反而比“PID + 再查几次”更简单、更快。

## 6. 每项检查只由一个所有者负责

| 检查 | 权威所有者 | 次数 | 删除的重复工作 |
|---|---|---:|---|
| 目标解析 | PAO | 每请求 1 次 | 后续只用不可变目标集 |
| 活动状态 | PAO | 预检 1 次、提交门 1 次 | 不在准备期间反复轮询 |
| Git/运行时资格键 | generation owner | 每操作 1 次 | 不为每个 Worker 重做 |
| 完整依赖图、全闭包导入、公共契约 | isolated probe | 每个新 generation 1 次；有效缓存为 0 | 不在每个 Worker 重复 |
| 制品物化 | generation owner | 每个新 generation 1 次 | 不重复复制 |
| 实际制品字节 | 受保护 bootstrap | 每个新进程 1 次 | 删除 Function Host 前后两次重复散列 |
| Agent 配置与 backend 初始化 | 候选 Worker | 每个候选 1 次 | 不能删除，这是实际可用性 |
| 新 Worker 身份/健康 | Supervisor | 使用 READY/ACTIVE 回执 1 次 | 不按 PID 反复查询 |
| 旧 Worker 退出 | Supervisor process handle | 1 次事件 | 不用 PID 差异推测 |
| 提交后拓扑 | PAO | 每操作 1 次 | 不多轮扫描全部 Workers |

两个活动检查有不同目的，必须保留。隔离 probe 与受保护 bootstrap 也处于两个
不同信任边界：前者证明一代代码合格，后者证明某个实际进程加载的制品未损坏。
除此之外，相同事实不得重复计算。

## 7. 单 Agent `min|same` 的快速路径

单 Agent 重启不得承担 26 个 Worker 或共享服务的成本。

1. 开始通知保持现状。
2. 只预检请求目标，不枚举、封锁或验证无关 Agent。
3. 有效 generation 直接复用；代码未变时不重新 probe 或物化。
4. 只启动一个候选 Worker。
5. 受保护 bootstrap 验证制品一次。
6. Function Worker Host 不再：
   - 在 bootstrap 验证后再完整散列两遍同一制品；
   - 再导入全部约 538 模块验证已由 probe 验证的公共契约。
7. Worker 仍加载其自然运行闭包、读取最新 Agent 配置并完成真实 backend 初始化。
8. 候选 READY 后才进入几秒级目标路由切换。
9. 其他 Agent、共享 API、Scheduler 和 Telegram ingress 保持运行。

若代码确实变化，隔离资格检查可能仍需要时间，但它发生在旧 Worker 可用期间；
不能再把整段准备时间算作 Agent 离线时间。

## 8. `/reboot max` 的快速路径

1. 先持久化并投递开始通知，确保 Workbench 在断开前知道原因。
2. 对 26 个 Workers 的活动状态做一次并发快照，而不是串行多轮检查。
3. 旧共享 Functions 继续服务，候选 generation 只资格检查一次、只物化一次。
4. 候选共享 Functions 使用同一个 generation seal 准备所有 Workers。
5. 保持当前 Worker 启动并发上限作为第一版基线；先删除重复 I/O 和导入。
   只有实测仍受 CPU/I/O 并发限制时，才另行调整固定上限，不能用更大并发掩盖
   重复工作。
6. 所有候选 READY 后，再由现有 Core handoff 执行短暂 quiesce、drain 和提交。
7. API、Telegram poller 等有唯一所有者的入口只在提交时移交，候选不得抢先绑定。
8. Core handoff 返回后，PAO 使用同一批 Worker 回执生成一个最终结论。

### Scheduler 和 heartbeat

- 准备阶段旧 Scheduler 正常运行，heartbeat 不应看到“Agent 正在重启”。
- 切换窗口恰好到期的执行保留其持久化定义和幂等键，并标记为
  `deferred_by_reboot`，由新 Functions 在提交后领取一次。
- 回滚时由旧 Functions 恢复领取。
- 不创建第二个 job、不静默跳过、不把短暂切换渲染成 Agent 故障。
- 该行为必须用竞争测试证明“最多执行一次”。

## 9. 回执：同时表达结论和认知边界

不要再把“结果”和“是否完全观察到”塞进一个含糊状态。回执采用两轴模型。

### 9.1 操作结论

- `succeeded`：全部目标已在新 generation 在线，必要清理完成。
- `succeeded_with_warning`：全部目标可用，但存在不影响使用的清理或采用警告。
- `partial`：提交后只有部分目标可用；必须列出成功数和失败目标。
- `rejected`：切换前拒绝，旧状态未改变。
- `rolled_back`：切换失败，已确认恢复旧状态。
- `failed`：切换或恢复失败，至少一个目标当前不可用。

### 9.2 观察置信度

- `confirmed`：权威所有者已返回所需证据。
- `unconfirmed`：权威状态读取中断或证据互相矛盾。

`unconfirmed` 不再冒充“失败原因”。例如切换已提交、25 个确认在线、1 个状态
读取失败时，结论应是“结果未完全确认”，同时保留已知的 25 个成功事实。

### 9.3 每目标事实

每个目标保存：

- `online_new`、`online_old`、`unavailable` 或 `unconfirmed`；
- expected/observed Agent、nonce 和 generation；
- 用户可理解的失败检查码；
- PID 和时间仅作为诊断字段。

建议失败检查码包括：

- `identity_mismatch`
- `generation_mismatch`
- `candidate_exited`
- `backend_not_ready`
- `route_not_accepting`
- `old_route_not_retired`
- `status_read_failed`

现有兼容字段可以继续投影，但所有界面必须从同一个规范结论派生，不能各自猜测。

## 10. 用户消息

开始消息保持不变：

- `🔄 开始热重启…`

最终消息必须在第一行给结论，第二行才给必要原因或下一步。普通消息不显示
PID、generation hash、operation ID 或异常堆栈。

示例：

- 单 Agent 成功：
  `✅ Lumi 热重启成功，耗时 8.4 秒。`
- 完整成功：
  `✅ 完整热重启成功：26/26 个代理已切换并在线，耗时 32.8 秒。`
- 成功但旧进程仍在清理：
  `⚠️ 热重启已成功：26/26 在线；1 个旧 Worker 仍在后台清理，不影响使用。`
- 部分成功：
  `⚠️ 完整热重启部分成功：25/26 已切换并在线；赵君未在线（后端未就绪）。`
- 切换前拒绝：
  `❌ 未执行热重启：Lumi 正在处理任务。当前 Worker 仍在线。`
- 已回滚：
  `❌ 热重启失败，已恢复原状态：候选 Worker 未就绪。`
- 结果未完全确认：
  `⚠️ 切换已提交，结果未完全确认：25/26 已确认在线；赵君状态读取失败。`
- 恢复失败：
  `❌ 热重启未完成：Lumi 当前不可用（候选进程已退出）。`

PID 被复用不是用户问题，因此当前误报案例在新判据下应直接显示 26/26 成功，
不显示无意义警告。

多 Agent 消息第一屏只列失败目标，成功目标用计数；完整 per-Agent 证据留在
`/reboot status` 或诊断视图。中英文 locale 必须具有相同 key 和语义。

## 11. 性能目标和门槛

这些目标在 HASHI3 的同一机器、同一 Agent 集合上用多次运行的 p95 验证。不能
通过提高 timeout 让测试“通过”。

| 场景 | 总耗时目标 | 目标路由封锁目标 |
|---|---:|---:|
| generation 未变的 `min|same` | p95 ≤ 15 秒 | p95 ≤ 5 秒 |
| 新 generation 的 `min|same` | p95 ≤ 45 秒 | p95 ≤ 5 秒 |
| generation 未变、26 Workers 的 `max` | p95 ≤ 45 秒 | Workbench/API 中断 p95 ≤ 15 秒 |
| 最后候选 READY 到最终回执持久化 | p95 ≤ 1 秒 | 不适用 |

新 generation 的 `max` 总耗时受一次真实资格检查影响，因此先以阶段预算验收：

- 完整资格检查只执行一次；
- 制品只物化一次；
- 准备期间旧服务保持可用；
- 26 个 Worker 不再重复完整闭包验证；
- 切换中断仍须满足 15 秒目标。

若首次 HASHI3 基准未达到总耗时目标，必须根据阶段计时继续删除瓶颈，不能先扩大
并发或放宽门槛。每次回执记录以下阶段耗时和计数：

- start_notice
- activity_preflight
- generation_cache_lookup
- qualification
- materialization
- candidate_start
- cutover
- final_observation
- notification_enqueue
- manifest_hash_count
- full_closure_import_count

这些字段用于诊断和性能回归，不进入普通用户消息。

## 12. 失败与回滚规则

- 资格或候选准备失败：旧路由始终开放，结果为 rejected。
- 第二次活动检查发现新任务：丢弃候选，旧路由不变，并明确说明目标已变忙。
- drain、quiesce 或提交前健康检查失败：恢复旧 Worker，确认后报告 rolled_back。
- 多目标切换在提交点前保持全有或全无。
- 提交后某 Worker 立即失效：保留真实 committed 状态，报告 partial 或 failed，
  不伪造“已回滚”。
- 通知投递失败：操作结论不变，可通过状态查询读取。
- 旧 Worker 后台退出延迟：只要旧路由已经不可达，不重新封锁新 Worker。
- 无法证明旧路由隔离时：不能降级成清理警告，必须报告失败或未确认。

## 13. 实施工作包

所有工作包都在 `debug-reboot` 完成并一起通过发布门槛。

### A. 先建立红灯证据和阶段计时

- 固化 PID 复用导致当前误判的回归用例。
- 固化 26 Workers 重复验证和导入次数的计数用例。
- 固化准备阶段路由被过早封锁、heartbeat 被拒绝的用例。
- 固化当前笼统消息丢失部分成功事实的用例。
- 加入阶段计时，但不改变结果。

### B. 修正身份、结果模型和消息

主要 Functions 所有者：

- `orchestrator/reboot_manager.py`
- `orchestrator/reboot_receipts.py`
- `orchestrator/reboot_ui.py`
- 现有 locale catalogs

改为 nonce/generation/Agent/健康判据，加入两轴结果和每目标失败检查；保留旧字段
的兼容投影。

### C. 建立可信 generation seal，删除重复资格工作

主要 Functions 所有者：

- `orchestrator/function_generation.py`
- `orchestrator/function_worker_supervisor.py`
- `orchestrator/function_worker_host.py`

缓存键先做便宜且严格的 Git/运行时判断。缓存未命中时只 probe 和物化一次。
保留受保护 bootstrap 的进程边界验证，删除 Worker Host 的两次重复散列和每个
Worker 的全闭包导入/契约复验。

### D. 缩短 `min|same` 和 `max` 的封锁窗口

- targeted 模式只在候选 READY 后封锁所选路由；
- broad 模式删除 PAO 在资格准备前的早期全路由 fence；
- 使用现有 Core handoff 的 quiesce/commit/rollback，不改 Core；
- Scheduler 在短切换窗口延期一次到期执行；
- 不改变目标解析、实际 backend 初始化或通知先后顺序。

### E. 完整验证后才允许推广

每个工作包可形成自包含提交，但 HASHI4 不采用中间提交。最终由同一个已验证
HASHI3 提交整体推广，禁止 cherry-pick 部分修复。

实现改变 READY、回执或切换行为时，同一变更必须更新：

- `docs/HASHI_REBOOT_RECEIPTS.md` 的权威回执和消息契约；
- `docs/HASHI_PYTHON_RUNTIME_COMPATIBILITY.md` 的 generation/READY 契约；
- 对应架构 FYI 和实现验证记录。

用户批准、源码实现、离线验证和现场采用继续分别记录，不能互相代替。

## 14. 验证计划

实施检查点 `6021be54` 已在 `debug-reboot` 完成。先建立的错误断言与红灯覆盖
PID 复用、身份错配、部分成功、提交后未确认、过早封锁、重复散列/导入及
heartbeat 恢复；随后实现转绿。干净提交上的相关聚焦回归为 219/219 通过，
curated Core gate 为 833 passed、1 skipped，Ruff、Python 编译、locale JSON 和
Protected Core 检查通过。现场采用结果见 14.4，并与离线事实分别记录。

### 14.1 必须先红后绿的回归用例

1. 新旧 PID 相同、nonce 不同、generation/Agent/健康正确：最终成功。
2. PID 不同但 nonce 或 Agent 不匹配：最终失败。
3. 全部目标可用但旧进程退出回执延迟：成功并有清理警告。
4. 25/26 在线、1 个 backend 未就绪：partial，列出失败 Agent 和检查项。
5. 切换已提交但一个状态读取失败：保留 25 个已知成功并标记 unconfirmed。
6. 开始通知发生在任何 route fence 或 Workbench 断开之前。
7. 准备期间旧路由继续接受工作；切换门只封锁精确目标。
8. 准备期间 heartbeat 正常执行；切换窗口到期的 heartbeat 只延期一次。
9. generation cache 命中时 probe、物化和全闭包导入计数为零。
10. 新 generation 对 26 Workers 只进行一次隔离全闭包资格检查。
11. 每个 Worker 只保留受保护 bootstrap 的制品验证和真实 backend 初始化。
12. 候选失败、drain 失败、提交失败分别保留或恢复旧路由。
13. `min|same` 不替换共享服务；`max` 不替换 Core 或 Remote。
14. 中英文消息 key、HTML 转义、计数和失败目标完全一致。

### 14.2 聚焦测试

至少覆盖：

- `tests/test_runtime_contract.py`
- `tests/test_function_generation.py`
- `tests/test_function_worker_protocol.py`
- `tests/test_function_worker_supervisor.py`
- `tests/test_reboot_manager.py`
- `tests/test_reboot_receipt_persistence.py`
- `tests/test_reboot_command_projection.py`
- `tests/test_reboot_origin_delivery_and_cache.py`
- `tests/test_kernel_process.py`
- `tests/test_minimal_core.py`
- `tests/test_runtime_handoff.py`

还要运行 protected-Core 检查、Functions 最小集合、curated Core gate 和相关产品
测试。计时只在专用 HASHI3 集成基准中作为性能门槛，不把操作系统抖动写进普通
单元测试。

### 14.3 实际采用验证

实际 `/reboot` 是运维动作，不由本方案文件授权。后续得到明确授权后，HASHI3
现场验收顺序为：

1. 同 generation 连续执行多次单 Agent `min|same`；
2. 注入候选失败，证明旧 Worker 保持可用；
3. 执行一次 26 Workers `max`；
4. 在准备和切换边界各安排一个 heartbeat；
5. 从 Workbench 验证开始通知先于断开；
6. 核对 Core PID、Remote 生命周期和无关 Agent 未被错误替换；
7. 核对阶段计时、检查次数和最终用户消息；
8. 最终再次核对源代码、制品和运行 Worker 是三个分别确认的事实。

### 14.4 HASHI3 现场验收结果

2026-10-14 获得仅限 HASHI3 的明确授权后完成：

证据更正：此前列出的五次 4.6–5.5 秒 `min` 和两次 20.6–20.7 秒
`max` 是由旧 Workbench 管理端点发起。该端点直接调用 PAO，虽经过同一个
RebootManager，却没有经过标准 Frontend Connector slash-command RPC。因此这些
数据只能证明进程替换阶段性能，不能证明 Workbench/Telegram 命令入口一致，
原“端到端 p95 约 5.4 秒”结论撤回。真实 Telegram 现场随后出现 44.8、37.7 秒
的 `min` 和 49.5 秒的 `max`，暴露出同一 generation 在新 Git 提交与旧
artifact 创建提交之间来回切换、反复触发完整资格检查的问题。

跟进修复把 Workbench 管理动作改为提交同一个 `/reboot min` 到 FC→Worker
命令链，并删除入口处重复的活动检查；PAO 仍保留唯一权威 preflight。缓存身份
改为“精确 Function 内容 + runtime”，提交号只作为当前资格来源：HEAD 改变但
内容相同时只精确复核一次字节并刷新回执，不再跑隔离 probe；任何 Function
字节变化仍 fail closed。artifact 的创建来源保持不可变，不再反向覆盖当前
资格回执。发布构建也改为从临时 Git `HEAD` 导出中完成，不再改写运行目录里的
Python 分发元数据。上述修复已通过标准 Workbench FC 命令链采用并复验：首次
新 generation 的 `max` 为 108.0 秒，其中一次隔离资格检查为 49.9 秒；随后同
generation 的 `min → max → min` 分别为 5.4、21.1、5.2 秒，全部为
`succeeded` + `confirmed`。

- 第一次 `max` 由旧 Functions 发起，仅作为采用运行；62.8 秒后加载提交
  `731592ad` 对应的新 generation；
- 旧管理端点连续五次 `min` 为 5.5、5.2、4.6、4.9、5.0 秒，五次均为
  `succeeded` + `confirmed`，无警告；仅保留为 RebootManager/Worker
  过程证据，不作为 FC 或用户端耗时结论；
- 旧管理端点两次 `max` 为 20.6 和 20.7 秒，11/11 在线；一次精细观测到
  Workbench/API 中断约 0.83 秒；同样不作为命令入口一致性的证明；
- `min` 的 qualification/full-closure import 均为零；稳定缓存后仅有 1 次
  必要清单封印检查。`max` 对 11 个候选只保留每个进程一次 bootstrap 制品
  验证，没有 11 次完整闭包导入；
- Workbench 每次先显示开始通知，最终消息明确显示成功、耗时及在线数量；
- Core PID 与 Remote 进程身份始终不变；Functions/Workers 按预期替换；
- 临时 heartbeat 在准备阶段正常执行，切换边界到期的一次在恢复后恰好执行
  一次，没有重复补跑或待处理恢复批次；验收后已禁用；
- 候选拒绝、drain 失败、组回滚和第二活动门采用确定性注入测试复核为 4/4。
  没有为了现场造故障而给运行代码添加测试开关或失败后门。
- 最终标准 FC 序列由 Workbench 提交 `/reboot max → min → max → min`。首次
  `max` 加载提交 `7aedf642` 对应 generation
  `sha256:22774b7e5c4852a8f9a262a41787258393df340f1ed132e39dc3b18df33b3458`，
  11/11 在线；稳定序列的两个 `min` 只启动一个候选 Worker，完整资格和完整
  闭包导入均为零；稳定 `max` 启动 11 个候选，每个仅保留一次受保护 bootstrap
  制品验证，完整资格和完整闭包导入均为零；
- 两次 `min` 的新旧 Worker nonce、启动时间、generation、Agent 身份、路由、
  backend readiness 和旧进程退出均由权威回执确认，PID 只保留作诊断；
- 标准 FC 验收结束时 Core PID 仍为 12824，Remote child PID 仍为 20204；
  HASHI4 未修改、未重启，全程没有使用 `/restart`。

验收文档提交后曾错误地再触发一次 `max`，尽管该提交没有改变任何 Function
字节、generation 仍为
`sha256:22774b7e5c4852a8f9a262a41787258393df340f1ed132e39dc3b18df33b3458`。
新共享 Functions 已启动并达到 11/11 Worker 后，Core 非正常终止，操作没有
最终回执。后续诊断确认原因不是 Function 热采用需要冷重启，而是 HASHI3 仍由
旧任务启动脚本托管：它在 `ErrorActionPreference=Stop` 下用 `*>>` 接收
原生 Python 的 stderr；一个后台预热线程写入 stderr 后被 PowerShell 转成
`NativeCommandError`，终止了启动脚本，随后的 Windows Job 清理带走 Core
及其 Functions 进程树。

HASHI3 用户级计划任务已改用仓库中的标准启动脚本。该脚本分离 stdout/stderr，
并以 Python 子进程退出码为准，不再把后台诊断输出当作 Core 退出。恢复使用完全
相同的 Python 3.12.13 解释器和 63 个包，没有安装、删除或升级依赖，也没有使用
`/restart`。Core 重新在线后，所有 Function 源码采用仍只经过标准 FC
`/reboot`。

这次恢复还暴露出一个回执正确性缺陷：普通冷启动曾把上次中断的 broad receipt
按“当前 generation 和 Workers 都在线”错误提升为成功。先建立红灯后，提交
`5fbbcbca` 规定只有同一个 live Core handoff 准备出的 successor shared
process 才能确认继承回执；普通冷/恢复启动必须记为 interrupted +
unconfirmed。真实采用随后发现临时候选标记在 commit 时已被清除，提交
`2af77d58` 将这一连续性证据保存为 successor process 的只读事实。两项都是
PAO/Functions 修复，没有修改 Protected Core。

提交 `2af77d58` 通过 Workbench 标准 FC `/reboot max` 现场采用，耗时
47.0 秒并确认 11/11 在线。随后同 generation 的最终
`min → max → min` 分别为 4.6、18.5、4.7 秒，全部为
`succeeded + confirmed`。两次 `min` 都只以 `agent1` 为目标，未请求
共享替换；`max` 精确包含 11 个目标，共享 Functions PID 从 14664 换为
29432。整个采用与最终序列中 Core PID 始终为 13944，Remote child PID 始终为
20204，API 与 Gateway 健康、11/11 Workers 在线。Python 版本、解释器字节
指纹、63 包清单指纹均与恢复前基线相同。

最后相关回归为 236/236，通过 curated Core gate 839 passed、1 skipped，
Windows 启动器回归为 12/12；工作树与相对 `main` 的 Protected Core 检查
均通过。HASHI3 当前在线；HASHI4 未修改、未重启。

HASHI3 当前只有 11 个 Worker，因此上述实机结果不能冒充 26-Worker p95。
26-Worker 的重复资格工作已由计数回归证明不会随 Worker 数量倍增；生产规模
实测必须在后续明确授权的非生产同等拓扑完成，不能先拿 HASHI4 冒险。HASHI4
在本次施工和验收中未修改、未重启。

## 15. 风险控制

| 风险 | 控制 |
|---|---|
| 快速缓存误用旧代码 | Git 精确路径清洁证明 + commit/runtime/config seal；bootstrap 仍验证实际字节 |
| 删除检查降低安全 | 只删除同一信任边界内的重复检查；probe 和 bootstrap 两个边界都保留 |
| 准备期间出现新工作 | 切换前第二次活动门检查，不强停用户任务 |
| 多目标只切了一半 | 保留无 await 的原子指针替换和提交前全体 READY |
| heartbeat 重复 | 持久化 lease/幂等键，切换期间只延期一次 |
| 旧进程未退出 | 路由隔离与进程清理分开；有影响才失败，无影响显示清理警告 |
| 消息过长 | 第一行结论、第二行失败目标；完整证据进入 status/诊断视图 |
| 误改 Core | 每次选文件和结束前运行 protected-Core 检查 |
| 为追求速度扩大并发 | 第一版不改变并发上限；先删除重复工作再用基准决定 |

## 16. 完成定义

只有同时满足以下条件，本修复才算完成：

- PID 复用不再制造失败，身份验证没有降低；
- 用户永远能看出成功、部分成功、拒绝、回滚、失败或未完全确认；
- 失败消息包含失败 Agent、失败检查和其余目标的当前状态；
- `min|same` 不再重复完整 generation 资格工作；
- `max` 对 26 Workers 不再重复 26 次全闭包契约验证；
- 准备阶段不封锁旧路由，切换窗口满足性能门槛；
- heartbeat 在准备阶段不受影响，切换窗口最多延期一次；
- 开始通知仍在 Workbench 断开前出现；
- source、artifact、running Workers 和通知 delivery 分别报告；
- 所有聚焦测试、Core gate 和 protected-Core 检查通过；
- 没有受保护 Core 改动；
- 没有 HASHI4 修改或重启；
- HASHI3 实际热重启验证在另行授权后通过。

## 17. 为什么这是最佳方案

1. **它删除无效工作，而不是把无效工作并行化。**
   先消除重复散列、重复全闭包导入和过早 fence，性能收益稳定，也减少失败面。

2. **它复用已有事实，而不是创造新的确认协议。**
   nonce、generation、Agent 身份、启动时间、健康回执和进程 handle 已经存在；
   新判据不会增加网络往返或等待。

3. **它把安全检查留在正确的信任边界。**
   每代代码由 isolated probe 验证一次，每个实际进程由受保护 bootstrap 验证
   一次；既不牺牲安全，也不让每个 Worker 重做相同业务契约。

4. **它把准备时间与停机时间分开。**
   慢资格检查可以继续优化，但用户不应为离线准备买单。真正不可用窗口只包含
   drain、原子切换和开放新路由。

5. **它让 `min` 和 `max` 共用同一套真相。**
   单 Agent 不再承担全局成本，完整重启也不再另写一套模糊判断，维护面更小。

6. **它诚实表达现实。**
   成功、警告、部分成功和未确认各有清楚含义；系统不再把已知的 25 个成功藏在
   一句“无法确认”后面。

7. **它不要求 Core 迁移，也不改变 `/restart`。**
   修复位于 PAO、Worker 和 Frontend Connector Functions，符合当前工程边界，
   能在 HASHI3 独立完成、验证后再整体推广。
