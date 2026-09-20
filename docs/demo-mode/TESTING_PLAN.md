# HASHI Shared Demo — Detailed Testing Plan

日期：2026-09-20
分支：`feature/demo-mode-20260920`
源代码基线：`637422c88692680d35592a46acef5c4e22f33c3f`
状态：**测试设计，未实现测试、未运行产品测试、未验证 200 人容量。**

参考：[实施计划](IMPLEMENTATION_PLAN.md)、[共同契约](CONTRACT.md)、[现行 Testing Policy](../TESTING_POLICY.md)。本次 docs-only 交付只需要相关静态检查；下列步骤用于未来实现，不构成执行实机操作的授权。

## 1. 测试层次和安全范围

| 层次 | 内容 | 外部模型/日常实例 |
|---|---|---|
| D 文档静态 | 链接、版本/阈值一致、diff、无 secret/运行文件 | 不使用 |
| U focused | 真正的 lease/store/admission/policy owner，临时数据库，fake clock/provider | 不使用 |
| C component/contract | 实际 HTTP app + owning services + shared fixtures | 不使用 |
| P isolated product | 专用临时 instance、真实 Worker 进程、受控 fake provider | 不访问日常实例 |
| L capacity/soak | 专用测试实例、200 客户端、受控时延、资源计量 | 默认 fake provider |
| R real canary | 已通过前述门槛的独立 VM 与有限真实 API | 单独明确预算/操作授权 |

每个重要新测试记录 red/green 依据：真实缺陷/pre-fix 或可逆临时 mutation；不得把 mutation 提交到实现分支。mock 用于安排模型结果/故障，不是测试主体。不要运行仅验证源码词语或文档段落存在的所谓功能测试。

## 2. 建议测试文件

这些是拟新增名称，不代表仓库当前存在：

```text
tests/test_demo_profile.py
tests/test_demo_leases.py
tests/test_demo_api.py
tests/test_demo_admission.py
tests/test_demo_worker_lifecycle.py
tests/test_demo_contract.py
tests/test_demo_runtime_policy.py
tests/test_demo_cleanup.py
tests/test_demo_budget.py
tests/test_demo_normal_regression.py
```

真实进程和 deliberate wall-clock 探测放在明确标记的 integration/probe 范围，不能被 focused 测试偷偷启动。使用 testpaths 和现有 marker 规则；不为了方便把 live probe 纳入 bare pytest。

## 3. 行为矩阵

### H-T01 — Profile、路由装配和空启动

- Demo profile 解析真实配置，缺预算、无安全模型/计量能力、非法上限、tools/promotion/background 冲突均拒绝 ready。
- small/standard/large 参数分别为 W/G=12/8、32/24、64/48；访客上限仍200；显式配置而非按物理内存自动扩大。
- Demo 无 Agent/Worker 可以服务 health/config；普通模式空 Agent 拒绝保持原状。
- 页面读/health/config 连续请求不产生 lease、workspace、Worker 或模型调用。
- Demo 启动/恢复不打开 Remote、外部消息 connector、discovery、browser worker 或后台 Agent jobs。
- 关闭 Demo 后普通路由和认证不变；未知/损坏 profile 不能回退 personal owner。
- red 示例：移除 Demo 路由早期分支或 permit 校验，应被额外路由/空启动测试击中。

### H-T02 — 匿名身份与对象级授权

用两个真正随机 token、相同 IP、两个 Agent、至少各两个 Sessions；通过实际 API 建立。

| 操作 | 期望 |
|---|---|
| A 使用 B 的 session_id 取 snapshot、events 或发消息 | 统一404，无 B 数据、无新 Run |
| A 使用 B 的 run_id 取消，或把 B run 放进 A session URL | 统一404，B 继续原状态 |
| A 使用 B/另一 Session 的 cursor、before、epoch | 拒绝/要求本 Session snapshot，不泄漏 |
| 同 IP A/B | 不合并身份 |
| A 改 IP 但持有效 token | 仍是 A，限流另计算 |
| 缺 token/伪造/错摘要/已撤销/过期 | 无个人 owner 回退 |
| 浏览器伪造 owner/agent/service 头或 payload | 拒绝未知字段；不能取得新权限 |
| 第一次匿名 bootstrap、重复有效 Cookie bootstrap | 新建一次/恢复原绑定 |

所有读取路径，包括错误、恢复、history pages、长轮询和取消都覆盖。测试 inspect 出站 PCM 和响应 JSON：不能含其他访客唯一 marker、private paths、credentials 或全局 Agent 清单。red：暂时省略 owner 条件或 epoch 校验，攻击式测试必须失败。

### H-T03 — Transport 和最小暴露面

- demo service key 只通过 Demo Connector；对普通 `/api/agents`、admin、v1 Sessions、Remote、MCP 请求无访问权。
- 在 Demo listener 枚举既有敏感路由，必须未装配或被安全拒绝；不能仅测试客户端未调用。
- Wrong method、encoded path/traversal、双斜线、query 注入、非法 JSON、未知字段、超大 chunked body 均有确定边界。
- 缺/错 Origin、CSRF、客户端头、cross-site JSON/form 请求无副作用；内部服务认证不替代浏览器 CSRF。
- 所有动态响应/错误 no-store；内部 token 只到受限 server，公共 DTO 永不包含它。
- 错误日志和栈不带正文/header/服务凭证。检查真实日志 sink，不仅新增 logger spy。

### H-T04 — 槽位、provisioning 和幂等

- 小容量（如2）并发100次 bootstrap：计数含 provisioning/cleanup_pending，总资源不超过限制；不是先创建后检查。
- 模拟配置创建前/后、Session 写入前/后、ready 前崩溃；重启可恢复或完整清理，无共享路径误删，无重复 Agent。
- DB 与文件不原子时 journal 的每个步骤都测；模拟 revision conflict 和 committed durability warning，禁止盲目重试覆盖较新配置。
- 新建 Session 上限3，同 Agent 但独立上下文；新 Session 不延长 lease。
- 同 key 同内容并发重试只得到同一 Message/Run/预算预留；同 key 不同内容409；同 key 不同访客/Session 各自作用域正确。
- 已接纳 Run 的幂等重放在系统满载时仍能查询原结果，不被作为新任务收费。
- 首次 Set-Cookie 丢失/双标签初领产生的孤儿按 CONTRACT 的有限回收处理；不虚构 exactly-once 保证。

### H-T05 — 真实 Worker 生命周期与恢复

先用受控 fake provider 做真实子进程 integration，不直接 mock 掉整个 lifecycle。

- 同 Agent 同时唤醒只有一个 Worker；starting/running/退出未确认都计位。
- W 限制不因 race、恢复、启动失败或 pending 任务超出；max_starting_workers 单独生效。
- Worker 空闲60秒释放；资源不足时可提前 LRU 驱逐已 quiescent 者；忙碌者不被杀死抢位。
- cancel queued Run 不启动 Worker；启动中取消、启动失败、provider 异常都释放正确 reservations。
- 用 A-session1/B-session1/A-session2 独特上下文，停止后恢复：当前原 Session 连续，不继承任何其他 Session。
- 原生 Session/Engine binding 保持；换进程 PID 不是新会话。检查全部子进程实际退出，不仅 registry count 减少。
- stop/quiesce 失败保留占位和 degraded 状态，不把旧进程再分给新人。
- shared Functions/进程异常恢复不自动启动200个闲置 Agent。
- red：移除 startup reservation 或恢复时使用错误 Session，必须被并发/上下文测试击中。

### H-T06 — 契约、投影和增量读取

- schemas/fixtures 来自 CONTRACT 的单一 owner，并验证真实 HTTP，不做 fixture 自我比较。
- JSON long-poll wait<=20秒，page<=100；慢读、断线、空轮询都不泄露内存/任务/数据库句柄。
- snapshot 与 events 交界同时生成事件，无丢消息；重复 event_id 可去重；旧 cursor 安全恢复。
- lease 过期时等待中的请求及时退出；200 等待连接不启动200 Worker。
- 当无真实 delta 通路，config.text_deltas=false，不能生成伪 delta；有真实通路时序列和 final 一致。
- 原生详细事件中人为加入 private markers，公开 DTO 剥离；只留答案/安全状态。
- 版本缺失/未知或安全能力矛盾时 fail-closed，不回退普通 backend。

### H-T07 — PCM/HER 无副作用

- 走真实 Direct admission/engine 路径验证有效 effort=zero，不仅检查输入配置。
- public seed 经 canonical parser 解析；运行 prompt 不含个人 EXP、memory、global topology、真实机器路径或另一 Session marker。
- 模型返回 tool_calls（shell/files/HChat/spawn/browser 等）或要求切换 engine：执行拦截点拒绝，零工具执行。
- 用户正文/slash `/reboot`、`/backend`、`/hchat` 作为普通文本或受限提示，不触发控制 dispatcher。
- 正常完成、失败、恢复、compact、收尾各路径均没有 memory promotion、habit/meditation、后台模型调用或外部 connector。
- 未注册/未公开工具不能经备用 API 被执行。用惰性 sentinel/spies 证明无副作用，不真正执行危险命令。
- 最大 context/output/总执行时间实际生效；隐藏 provider 调用被并发/预算统计，不能绕限。

### H-T08 — 24小时/idle、清理和迟到结果

使用 fake clock，在 created+24h 前后边界测试，禁止等待真实一天。

- GET/me/snapshot/events、重连、后台 heartbeat、不停接收 chunk 不续期；new Session 不改 absolute deadline。
- idle=1800 在无人有效操作时撤销；idle=0 只按 absolute 撤销；实际公告与 config 一致。
- 排队、启动、生成、写 final 之前、长轮询时分别到期；身份立即不可访问。
- 人工释放迟到模型 callback：fencing 禁止复活 Run/Session/文件，即使入口此前通过鉴权。
- 在每个 cleanup_step crash/restart；先撤销后清理，不提前释放 slot，不误删另一个访客或 normal Agent。
- 注入文件锁、DB foreign-key failure、Worker 无法退出、symlink/越界 root；保留 cleanup_pending，重试有限且可观测。
- 检查所有实际持久 stores、HER state、legacy transcript、cache、日志和备份入口，不能只判断 workspace 不存在。
- 无 normal 七天 quarantine/归档副本；正常 Agent 的 quarantine 仍存在。
- 健康条件下拟目标 purge<=5分钟；超时留失败状态/报警，不伪装清除成功。
- 区分应用不可再访问、应用持久数据 purge、SSD取证及外部模型留存；后两者不在本地测试承诺中。

### H-T09 — 预算守恒和负载保护

- 多个访客并发竞争最后一笔额度，原子 reservation 防止超额接纳。
- 清 Cookie/new Session/lease purge 不重置全局预算；服务重启保持 spent+reserved。
- 接纳失败、queued 取消、执行成功、明确上游拒绝、结果未知超时分别结算；未知不无限退款重试。
- Retry 最多配置次数，所有实际模型请求和必要 compact 计量；不能把逻辑 Run 的幂等当远程 API exactly-once。
- G执行位、W Worker位、Qpending位、每访客一个未结束 Run均有界；新 Session 不绕过。
- 429/预算耗尽/队列超时使用正确 code和Retry-After，无无限后台积压；取消/过期后继续能释放。
- 预算窗口切换和时钟异常不会重复重置、丢失未结算 reservation；聚合记录不携带已清除对话。

### H-T10 — 正常模式与工程边界

保持原来的 normal active创建->启动 Worker、无 Agent 拒绝、normal删除归档/quarantine、原 Session owner、认证和原有效功能；新增 Demo 分支测试而非删掉原断言。

运行受影响的 configuration、Session、creation/deletion、lifecycle及共享 Functions owning tests。保护Core不变，实际 Function manifest包括新模块且正常资格校验；Agent-only replacement不假冒 shared升级。不通过安装新依赖到日常 Core interpreter 来让测试绿。

## 4. 容量与 soak 设计

### L0 资源基线

先记录硬件/VM分配/OS/Python/Node/双方提交、Function generations、profile和日志模式。Linux记录进程树PSS/USS及VM总占用；其他平台采用可比指标并注明，不把所有RSS直接相加当独占内存。

测空服务、1、8、12 Worker；之后按授权测试32和64。记录cold/warm启动p50/p95/p99、整轮时长、峰值内存、Worker退出时间、残留子进程、DB锁等待、事件循环延迟、磁盘增长。0.5GB/Worker与10秒一轮只是待替换的估算值。

### L1 200 个在线连接

200个真实匿名lease/客户端，事件读取覆盖同IP场景；保持至少30分钟，混合刷新、断线重连、多标签（每lease最多3个流）。不发消息时不得使所有Worker常驻。证明连接/缓冲有界和安全，不将其报告为200模型并发。

### L2 稳态消息

受控fake provider平均10秒服务时间，初始无真实费用。采用不同到达分布，而非所有客户端固定同相位发包：

| 场景 | 在线人数 | 每人平均间隔 | 平均在途需求（不含排队） |
|---|---:|---:|---:|
| light-16 | 100 | 180秒 | 约5.6 |
| target-32 | 200 | 180秒 | 约11.1 |
| sustained-64 | 200 | 60秒 | 约33.3 |

分别使用small/standard/large。每组至少20分钟稳定阶段，另有60分钟soak；样本量不足不声称可靠p99。再用20秒和30秒服务时间重复，验证容量下降的报告，不硬套10秒的结果。

拟定目标：在目标稳态流量下，无OOM/持续swap、无串话、无未计量执行；worker/queue不超限，排队p95<=5秒、p99<=15秒且未超配置30秒timeout；VM峰值内存保留约20%余量。目标可在正式容量验收前调整，但必须在看结果之前记录，不为了报告变绿事后降低。

### L3 突发与故障

200人在同一瞬间发第一条消息，冻结fake provider完成点，记录被接纳/排队/拒绝数量。只能接纳受实际执行位和有界队列允许的数量；拒绝是预期保护，不是“200人同时成功”。重试使用jitter且有最大次数，不自动换key。

覆盖API429/超时/坏JSON/慢读/连接断开、Worker启动失败、SQLite写锁、清理暂时失败、共享Functions退出。后台可恢复，不无界扩容、费用不失控。观测配置重载和broadcast_topology成本，不能认为只有模型是瓶颈。

### L4 访客流转

200个有效lease即使浏览器断开也占槽位，201号得到明确满员提示；idle回收完成后才能新入。单独证明24小时保留上限与在线人数不是同一指标。持续入场/离场/丢Cookie，Agent/config/state记录及磁盘增长受期限、清理和总槽位约束。

### R1 真实API canary

仅在授权的独立VM和额度内先1/2/5个访客验证真实Engine、delta/最终回答、provider限制与实测费用，再逐步放量。不要直接用200个真实付费请求压测，也不要连接日常instance。模拟容量通过不能替代真实供应商额度和网络验证。

## 5. 未来执行命令

以下文件在实现后才会存在；不存在时应记录未实现，而非跳过后宣称通过。

```bash
python scripts/check_protected_core_changes.py --base 637422c88692680d35592a46acef5c4e22f33c3f
git diff --check
python -m pytest -q tests/test_demo_profile.py tests/test_demo_leases.py tests/test_demo_api.py --basetemp=/tmp/hashi-demo-focused
python -m pytest -q tests/test_demo_admission.py tests/test_demo_contract.py tests/test_demo_runtime_policy.py tests/test_demo_cleanup.py tests/test_demo_budget.py
```

使用支持的隔离Python环境和不同临时目录；Windows采用同等可写临时路径。不要并行启动多个pytest进程共享basetemp。变化触及共享配置/Session/生命周期时，按Testing Policy跑owning tests与curated core gate；release candidate再显式执行offline product suite。真实Worker/live测试必须命名并单独执行，不能借bare pytest偷偷跑外部API。

## 6. 两端发布门槛

| Gate | 需要的证据 | 当前 |
|---|---|---|
| G0 文档/契约 | 同版本接口、分支基线、静态diff | 文档交付时核验 |
| G1 后台受限运行 | H-T01至H-T07、原生Worker恢复 | 未执行 |
| G2 数据与费用 | H-T08/H-T09、完整store清单、失败重试 | 未执行 |
| G3 正常回归 | H-T10和相关owning/core gate | 未执行 |
| G4 客户端联调 | 同IP双浏览器、跨ID攻击、过期/取消/恢复、真实HTTP | 未执行 |
| G5 目标机器容量 | L0-L4、具体W/G/Q、实测表 | 未执行 |
| G6 公开发布 | 独立授权R1、HTTPS/代理/secret/no-store验收 | 未执行 |

任何跨访客泄漏、普通admin可达、工具实际执行、预算可重置、过期数据复活或清理未知残留均为发布阻断，不用免责声明绕过。

## 7. 证据记录模板

```text
case/gate:
HASHI commit / client commit / protocol:
source vs qualified vs running generation:
profile / OS / VM RAM / CPU / model or fake provider:
exact command and scope:
red reason / red result / green result:
passed / failed / skipped / deselected:
latency percentiles / samples / memory / queue / spend:
retention stores checked / artifacts absent:
remaining risks / untested scope:
operator authorization for live actions:
```

只把脱敏结果和安全指标放入后续报告；真实token、正文、工作目录和provider key不入Git。通过测试的结论必须对应实际范围，不能把docs-only提交或mock结果描述为线上就绪。
