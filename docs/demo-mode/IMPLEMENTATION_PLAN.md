# HASHI Shared Demo — Detailed Implementation Plan

日期：2026-09-20
基线：`637422c88692680d35592a46acef5c4e22f33c3f`
分支：`feature/demo-mode-20260920`
范围：后台 Functions 和实例/平台配置。**本次仅编写计划，以下代码、配置和命令尚未实施。**

先读 [README](README.md)、[共同契约](CONTRACT.md)、[测试计划](TESTING_PLAN.md)、根目录 AGENTS.md、ARCHITECTURE.md 及现行 Testing Policy。上位架构优先；不为本功能变更 Core major version。

## 1. 目标和明确排除

实现无需安装/账户的临时纯文字体验：一台隔离 VM、一个 HASHI instance、最多 200 个有效匿名访客、每人一个真实 Agent、最多三个原生 Conversation Sessions。外部受限网页客户端只使用 Demo Connector。托管控制平台、独立 Gateway 产品、每访客 VM/container、Redis、账户服务和跨 Agent 可变 Worker 池均不在范围。

保留 PAO、PCM 和 HER 的实际运行链；禁止直接在 Connector 调模型来冒充 HASHI。默认 HER Direct/zero，工具及长期自主行为硬禁用。单个访客的不同 Session 也不共享可变对话上下文。

## 2. 基线事实和需要落实的边界

所有现有路径以本页基线为准；实现前重新比较 main 后续变化。源码、qualified Function artifact 和实机 running generation 分别报告。

| 基线位置 | 已知情况 | 实施含义 |
|---|---|---|
| `orchestrator/agent_creation.py` | AgentCreationService 经权威配置 owner 创建 workspace/config；默认 HER effort 不是 Demo 保证 | 添加内部受限 preset，不能由调用方写 raw agents.json |
| `orchestrator/agent_lifecycle.py` | start/stop 管理独立逐 Agent Function Worker | 复用按需生命周期，不假定已有共享执行池 |
| `orchestrator/startup_manager.py` | 通常无 active Agent 会退出；有 `_allow_empty_start` 受控分支 | 从 Functions 的已验证 Demo profile 接入空启动，normal 不变 |
| `orchestrator/workbench_api.py` | v1 Session routes 有 `persistent_session_v1` 发布门槛，普通路由面很广 | 专用 Demo 组装；缺能力拒绝 ready，不回退普通接口 |
| `orchestrator/session_store.py` | owner/agent/session/run/event 已有；owner 存在 personal fallback；默认 memory_policy=promote | 每次显式 owner；明确 no-promotion；复用存储而非再造聊天库 |
| `orchestrator/agent_deletion.py` | 常规删除归档历史并保留七天 quarantine | 新增 Demo-only purge；普通删除语义不变 |
| `orchestrator/runtime_app.py` / `manager_registry.py` | 共享 Functions 管理器及启动装配 | lifecycle/admission/cleanup 留在 Functions，不搬进 Core |

源代码复核入口：[基线目录](https://github.com/Bazza1982/HASHI/tree/637422c88692680d35592a46acef5c4e22f33c3f/orchestrator)、[架构](../../ARCHITECTURE.md)、[Minimal Core](../HASHI_SLIM_CORE_ARCHITECTURE.md)。本方案未确认的深层调用点，必须在 H0 形成调用链清单，不凭文件名猜测实现。

## 3. 文件级变更地图

`新增` 表示建议路径，当前尚不存在。可合并真正过小的模块，但不能破坏 ownership 或把规则散布成大量 flags。

| 文件/目录 | 类型 | 责任与变更 | 验收组 |
|---|---|---|---|
| `orchestrator/demo/__init__.py` | 新增 | 小型包边界，无 import 时启动服务 | H-T01 |
| `orchestrator/demo/profile.py` | 新增 | typed 配置、不可放宽的 Demo 能力、ready 校验 | H-T01/07 |
| `orchestrator/demo/api.py` | 新增 | demo-only 认证/路由、显式 owner、DTO、错误、事件读取 | H-T02/03/06 |
| `orchestrator/demo/leases.py` | 新增 | 随机凭证、访客槽位、期限与生命周期服务 | H-T02/04/08 |
| `orchestrator/demo/admission.py` | 新增 | 复用 PAO Run 的有界接纳与 Worker 使用；不是第二队列 owner | H-T04/05/09 |
| `orchestrator/demo/cleanup.py` | 新增 | 撤销、fencing、owner-driven purge、恢复 | H-T08 |
| `orchestrator/session_store.py` | 扩展 | Demo lease/预算/清理记录事务 API；显式 ownership；原生关联清除 | H-T02/04/08/09 |
| `orchestrator/agent_creation.py` | 扩展 | 内部 Demo preset：安全 seed、独有 workspace、固定 engine/profile | H-T04/07 |
| `orchestrator/agent_deletion.py` 及关联状态 owner | 窄扩展 | Demo 所有权标记及 purge 能力；不复制一套任意路径删除器 | H-T08/10 |
| `orchestrator/agent_lifecycle.py` | 复用，必要时窄扩展 | 安全启动、quiesce、停止、resume；保持正常 active/running 语义 | H-T05/10 |
| `orchestrator/startup_manager.py` / `service_manager.py` | 扩展 | Demo 零 Worker ready；不启动不必要通道/作业 | H-T01/05/10 |
| `orchestrator/workbench_api.py` | 扩展 | Demo 早期专用路由装配及认证；普通路由不挂入 Demo listener | H-T03/10 |
| `orchestrator/config.py` 及既有配置 owner | 待 guard 确认后的窄扩展 | 一个 typed Demo 配置入口；不能仅加未被消费者使用的字段 | H-T01/10 |
| PCM/HER 的现有策略与存储消费者 | H0 定位后修改 | 强制 no-tools、no-promotion、no-background、公开上下文、日志策略 | H-T07/08 |
| `contracts/shared_demo/v1/` | 拟新增 | machine schema/共享 fixtures，派生自 CONTRACT.md | H-T06 |
| `tests/test_demo_*.py`、必要的原有 owning tests | 拟新增/调整 | 行为、失败边界、契约与压力 probe | 全部 |
| `docs/` 所属架构/FYI、部署样例 | 实现时更新 | 发布状态和 profile 边界，不在文档阶段改架构事实 | H-T10 |

任何候选源文件先运行现有 protected-Core guard；保护清单仅来自 `CORE_SOURCE_PATHS`，不要另抄一份。若某修改需要 protected Core 变化，停止该技术方向并报告；本次没有授权此迁移。

## 4. H0 — 先证明原生路径与资源假设

先做 disposable instance 的技术验证，不接公网、不使用日常资料。默认使用 fake provider；真实模型探测另获预算/实例操作授权。

必须形成一份短调用链/数据位置记录：

1. Demo 文本如何进入 PAO Session/Run，再绑定 HER Engine Session；哪里触发 slash 分发，如何确保 Demo 不进入该通道。
2. Tool catalogue 与实际调用的最后授权点；HER Direct 是否仍可能触发 compact、observers、memory promotion、habit 或后台 provider 调用。
3. PCM 包含哪些全局配置、Agent 名称、EXP/memory 来源；哪些可以对 Demo 显式排除。
4. Session、HER state、provider context、legacy transcript、日志与缓存的实际持久位置和写入 owner。
5. 原生 event/delta 通路是否存在；可公开什么，如何等待事件而不为 200 个客户端每次扫描整个数据库。
6. Worker cold start、restore、quiesce 和 exit 的实际行为，是否有遗留子进程或重新加载配置开销。

先跑一个 Agent、两个 Sessions；停止 Worker 后重启同一 Agent，证明两段上下文各自连续。再做两位访客不可见测试。测空实例、1/8/12 个 Worker 的整树内存和启动耗时。不能因为方法存在就声称高频按需启动已经可用。

若冷启动不满足延迟目标，先调整保温/LRU 回收和并发；不得未经决定改成共享 Agent、绕开 PAO 或跨访客 Worker 池。H0 结果决定第一轮容量，不直接采用 0.5GB/Worker 的估算作为实测。

## 5. H1 — Profile、安全入口和空启动

接入既有配置解析 owner。建议逻辑结构如下，**不是当前可直接识别的配置文件**：

```yaml
demo:
  enabled: true
  capacity_profile: small
  max_live_visitors: 200
  absolute_ttl_seconds: 86400
  idle_ttl_seconds: 1800
  max_sessions_per_visitor: 3
  runtime:
    engine: her-v2
    orchestration_effort: zero
    max_running_agent_workers: 12
    max_concurrent_generations: 8
    max_starting_workers: 2
    worker_idle_seconds: 60
    max_pending_runs: 32
    max_queue_wait_seconds: 30
    max_inflight_runs_per_visitor: 1
  conversation:
    max_request_bytes: 32768
    max_input_chars: 4000
    max_context_tokens: 8192
    max_output_tokens: 800
    max_messages_per_visitor: 80
    max_provider_retries: 1
    max_run_seconds: 120
  retention:
    cleanup_interval_seconds: 60
    content_logging: false
    transcript_backups: false
  budget:
    daily_limit_required: true
```

模型/provider、定价、secret、端口及 root 从实例配置取得，不硬编码。公网 ready 前必须配置实际总预算并验证执行链能强制它；仅 `daily_limit_required=true` 不能算完成。

Demo 不可通过请求或普通配置误开启 tools/slash/upload/voice/memory promotion/自主后台行为。启动验证发现矛盾时拒绝 ready，不静默忽略。`enabled=false` 时现有 normal 行为逐项不变；不得把未知的 deployment_profile 字符串意外落入 personal owner 路径。

空启动只由 Functions 的已验证 Demo profile 触发。重启后先扫描过期和 provisioning/cleanup-pending 记录，不能自动启动所有尚未过期 Agent。不启用 Remote/HChat/Exchange、Telegram/WhatsApp、browser/computer sidecar、自动 discovery 或访客自主 jobs；内部确定性清理与调度不是 Agent cron。

Demo API 在独立实例内采用专用 allowlist 组装，service key 只认证这些路由。若复用 Backend API listener，Demo 分支必须在普通路由注册之前明确选择；不能“先挂全部接口，再靠 UI 不调用”。模型凭证、管理凭证和 Demo 访问凭证不能互换。

## 6. H2 — Lease、Agent 和原生 Session 绑定

建议把小型 Demo 表加入现有 SessionStore 所管理的数据库，通过公开事务方法访问；不要把 SQL 散布到 Connector，也不要让网页 server 直接读写。

必要记录：

```text
demo_leases:
  lease_id, owner_id, credential_digest, lease_epoch, agent_id
  created_at, expires_at, last_user_activity_at, state, revision
  provisioning_step, cleanup_step, last_safe_error_code

demo_budget_windows / reservations:
  window, aggregate spent/reserved amounts, run reference, status
  no prompt/response bodies, no raw credentials
```

约束：凭证摘要/owner/Agent 绑定唯一；计数包括 provisioning、ready、expiring、cleanup_pending 中仍占资源者。数据库槽位检查与预留必须原子，201 号不能在竞争下越限。SQLite migration 可重复、已有数据库保持正常行为；不能以整个数据库重建实现升级。

文件配置、SQLite 和进程创建不是一个跨资源原子事务。用现有 revisioned config writer 加小型 provisioning journal：预留 lease -> 创建独有 Agent/workspace -> 验证受限 preset -> 建立原生 Session -> ready。失败按已完成步骤回滚/重试；重启读取 journal，不盲目重复创建或误删已有路径。

Agent 使用唯一不可复用名称和 incarnation，workspace 限于专门 Demo 根。先创建 inactive/configured 状态并在请求执行时唤醒；必要的 eligibility 通过 Demo lease 管理，不能把普通 `is_active` 与瞬时进程存在混为一谈。现有 public creation API 的 `is_active=true` 会即时启动，所以内部 preset 通路不能无意预热 200 个 Worker。

Session creation 强制 explicit owner、正确 Agent、禁晋升策略。首个和后续 Session 都通过原生 SessionStore；最多三个；不共享 primary personal conversation/default authorized_id。追加 Session 不复制上一个 Session 的对话或 memory，不调用额外模型取标题。

Bootstrap 丢失首个 Cookie 或多标签首次竞态不保证 exactly-once；按 CONTRACT 处理孤儿 lease 的有限回收。已持有效凭证的 bootstrap 恢复必须幂等。

## 7. H3 — 接纳、幂等、Worker 和预算

### 7.1 接纳顺序

```text
验证 service/visitor/epoch + owner/session
  -> 查同作用域幂等记录
  -> 验证新请求字段、大小、访客额度
  -> 原子预留 pending/visitor/global-budget 名额
  -> 通过原有 PAO owner 记录 Message + Run
  -> 唤醒有界调度观察者
  -> 返回已接纳 IDs
```

必须把幂等、额度预留与 Run admission 的一致性落实到现有事务边界；若不能一事务完成，用可恢复 reservation 状态机和 reconciliation，不允许永久“收费但无 Run”或“Run 存在却无预算”。执行队列仍以 PAO Run 为权威，Demo 只保留有界待唤醒索引/通知。网页 server 不排另一套消息队列。

每访客包括所有 Sessions 共一个未终结 Run，重复的同幂等 key 不再占位。等待队列满/超时返回明确结果；不能无限后台排队。取消队列里的 Run 不启动 Worker。

### 7.2 Worker 使用

- 先保留 worker-start reservation；starting+running+尚未确认退出的进程都计入上限。
- 调用现有生命周期；每 Agent 的并发唤醒只发生一次。模型执行位与 Worker 位分开。
- G 个执行位限制整个活动回复处理；Direct 路径的所有实际 provider 调用（包括必要 compact/重试）都纳入对应预算和并发控制，不能从隐藏路径超限。
- 完成后短暂保温；当排队任务需要位置时，优先安全停止最久空闲、已 quiescent 的 Worker，不必等满 60 秒。
- 忙碌 Worker 不得为了新访客被抢占杀死。停止失败保留占位并报告 degraded；不能先减计数再假装退出。
- Worker 停止不删除 lease/Session，不迁移身份。再次唤醒恢复原生 Engine binding。
- 浏览器轮询和心跳不延长 Worker 寿命。重新启动共享 Functions 后不恢复所有闲置 Worker。

### 7.3 费用和不确定结果

以 UTC 预算窗口（或一个明确配置的统一时区）计算，跨服务重启保留全局 aggregate 和未结算 reservations。每请求按受限输入/输出及实际 adapter 计量做保守预留；缺价格/用量能力时必须有可强制的 token/request 总兜底，不得宣传严格金额上限。

上游明确拒绝且未执行的请求可有限重试；网络超时/断连导致结果未知时，不自动重放模型工作。客户端重试只查询/复用原 Run。未知费用不能立即全部退回后无限再试。重试和必要 compact 同样计量，关闭自主后台模型调用。

如果配置的模型暴露 hidden reasoning 或可变计费，必须限制/计量相关资源并保守估算；不要仅凭输出文本长度做预算。全局额度耗尽关闭新 Run 接纳但保留安全读取和清理。删除 Cookie、Session 或 lease 不清零日预算。

## 8. H4 — PCM/HER 的真实受限链路

H0 确定精确调用点后，只在各自 owner 加一个可验证的 policy 输入，不散布模型名/工具黑名单。

- 使用公共 seed，经既有 canonical agent.md parser 校验；不复制个人 Agent、EXP、Memory+、宿主路径、secret 或真实任务。
- PCM 仅组装固定公共说明及当前 Session 的许可上下文；禁止全局 Agent topology、其他访客和其他 Session 内容进入 prompt。
- engine 固定 HER；Direct=zero；provider/model 来自兼容的实例选择，不让用户通过正文/slash/请求 JSON 改。
- tool catalogue 为空且 PAO/HER 最后执行点拒绝；验证模型返回伪造 tool_calls 仍无副作用。仅依赖 prompt 的测试不通过。
- 默认的 memory_policy=promote 必须被受限策略替换，并验证观察者、meditation/habit、恢复/收尾和异常路径不会晋升。
- 普通文本 `/reboot` 等不进入控制解释器；可能出现代码样式文字不等于有权执行，禁止用简单关键词检测代替执行授权。
- 允许短上下文与输出上限，明确上下文收缩规则；不能把全局资料用来“补上下文”。

## 9. H5 — 公开投影和长轮询

按 CONTRACT 实现固定 DTO。优先复用原生事件等待/通知；如果只能分页查询，在一个 Functions 观察者中协调有界唤醒，不让每个访客无界扫描数据库。200 个等待连接不应创建 200 个 Worker。

`message.delta` 只来自真实 upstream delta；能力不足时诚实发布 false，状态与最终答案依然真实。client 当前 Session 只开一个长轮询；单 lease 的多标签连接应有上限，建议 3 个。wait 上限 20 秒、每页最多 100 条、缓冲有界；到期/revocation 主动唤醒结束等待。

cursor 与 lease_epoch/session 绑定，snapshot 与事件交接不能漏掉中间消息。重复事件可去重，cursor 过旧要求重新 snapshot。API 不直通原始 event.detail、内部推理、栈、路径或其他 Agent 数据。所有动态路径及错误 no-store。

## 10. H6 — 到期、取消与真正清除

采用确定性 Functions 清理循环，建议每 60 秒，绝对期限逐请求检查，不依赖循环按时运行。建议健康状态下 purge 延迟不超过 5 分钟；这是拟验收目标，逾期报警并停止继续挤满系统，而非宣称按时清除。

```text
撤销 lease + epoch fence
 -> 停止新的读写与长轮询
 -> 取消 pending/active Run，禁止恢复重试
 -> quiesce/停止 Worker，确认退出及旧回调失效
 -> 各 owner 清除受访客拥有的数据
 -> 记录无正文的清理完成结果并释放槽位
```

旧模型结果在最终写入点也检查有效 epoch/Run fencing，不能仅入口检查。取消发生在网络模型请求发出之后不保证对方停止计费；本地禁止提交过期结果并按预算规则处理。

清理清单必须在 H0 补齐实际表/文件：原生 messages/runs/events/projections/consumers/bindings/idempotency/outbox、HER Engine state/provider cache、legacy transcript、workspace 与任何正文副本。存在非级联外键，按 owner 顺序事务删除；不能凭单条 DELETE 或 `rm -rf workspace` 判完成。

Demo-only purge 不沿用常规历史归档/七天 quarantine，也不能全局修改普通 Agent 的恢复政策。路径只来自已记录的 demo ownership，校验 resolved root、symlink 和 incarnation，拒绝共享/未知/越界路径。清理失败保持 revoked+cleanup_pending，重启可恢复；旧 Agent ID/workspace 永不分给下一访客。

日志默认仅记录安全错误码、时延、计数、用量、请求相关 ID；禁正文、prompt、凭证和内部 header。检查既有共享日志和 Worker 日志，而非仅新增日志模块。Demo 不参与对话备份；SQLite WAL/自由页清理策略须明确，不能承诺 SSD 取证级擦除。聚合预算记录可按窗口保留，但 purge 后不得保留对话内容和访客可恢复关联。

## 11. H7 — 部署、回归与交接

部署包只含脱敏样例和固定代码版本。建立独立 VM/user/bridge_home，模型凭证受限；不挂宿主资料、日常 HASHI state、共享 secrets 或 Docker socket。拒绝访问日常 LAN 服务，模型出站按部署需要授权。

后台只绑定 loopback，由兼容网页 server 访问；公网只到受限网页入口。未通过版本/安全能力校验拒绝 ready。正常客户端的 Remote/HMAC/管理入口不因 Demo 改动。

先 small 配置验证，之后在目标 32GB 机器上测 standard：W=32、G=24；64GB 的 W=64/G=48 只是后续配置。访客槽位仍为 200，修改硬件标签不自动放大用户数。CPU、磁盘、数据库锁、Worker 启停、上游限额都须实测。

采用新隔离实例时按支持的安装流程。已运行 Demo 的源更新必须走既有 qualified Functions 采用机制；共享服务修改需明确 shared replacement 操作授权，不能用 Agent-only reboot 假装更新全体。不得为普通 Demo 功能建议 Core 冷重启迁移。所有实际启动/停止/网络/模型操作都在后续单独授权范围内。

回滚先关闭公网入口和接纳，撤销 Demo 服务凭证/leases，处理在途工作并完成回收，然后回退匹配的两端版本。数据库不兼容时不得直接把旧代码套到新 schema；只清理专用 Demo 环境或使用已验证迁移回滚，绝不触碰日常实例。

## 12. 施工顺序和客户端依赖

| 工作包 | 前置 | 完成标准 |
|---|---|---|
| H0 原生路径/资源 spike | docs/基线 | Session 恢复、策略与存储调用链、实测待填表 |
| H1 profile/安全路由 | H0 | 空启动、normal 不变、专用认证 fail-closed |
| H2 lease/provisioning/session | H1 | 双访客隔离、原子槽位、故障恢复 |
| H3 admission/Worker/预算 | H2 | 有界 Run、真实资源释放、费用守恒 |
| H4 PCM/HER 硬限制 | H0-H3 | 无工具/记忆/背景副作用，真实 HER |
| H5 契约/投影 | H2-H4 | CONTRACT HTTP fixtures 与实际接口一致 |
| H6 purge/恢复 | H2-H5 | 到期 fence、完整删除、失败可恢复 |
| H7 release candidate | 全部 | 测试计划各发布门槛、客户端真实联调 |

客户端可以在 H1-H4 期间用契约 fixtures 开发 UI/入口，但不得把 mock server 当 HASHI 联调通过。真实端到端必须固定两端 SHA、protocol version、config 和运行 generation。

## 13. 过时断言和必须保留的回归

实现时调整而非删除安全门槛：normal 无 active Agent 的拒绝、normal active 创建启动 Worker、normal 删除 quarantine、normal Session 默认 memory policy、normal owner 与认证行为都继续有效。新增针对 demo profile 的分支断言，不把正常断言整体改成宽松默认。

不保留“文档存在所以功能存在”、源码包含关键字、fixture 自己等于自己、虚假身份切换等测试。队列/隔离/清理以真正存储、HTTP、生命周期和可逆故障注入证明。具体证据格式见 [TESTING_PLAN](TESTING_PLAN.md)。

## 14. 本次状态

本文件只规划实施，不创建运行配置、真实 lease、Agent 或数据库，不执行任何测试命令。未实现与未验证项不得打勾；文档分支成功不等于 Demo 可上线。
