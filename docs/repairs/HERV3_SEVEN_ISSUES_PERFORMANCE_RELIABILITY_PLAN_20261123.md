# HERV3 七项性能与可靠性缺陷：施工、验证与交接计划

- 日期：2026-11-23（Australia/Sydney）
- 仓库：Bazza1982/HASHI
- 工作分支：plan/herv3-performance-reliability-20261123（本分支既是计划交接基线，也可由本地 Agent 在此继续开发；如拆多个 PR，应从此基线建立有明确依赖的工作分支）
- 创建时 main：bd5444240f6fdd27a5c08a6a5e4832590e25fb36
- 文件性质：PLAN ONLY；本次 GitHub 提交没有修改业务代码、测试或部署
- 工程 Owner：HERV3 Adapter / HERV3 Tool Execution / HER Session & Audit；跨层只做必要接口兼容
- 核心规则：绝不修改 Protected Core；不接触运行中的 HASHI4 生产实例；不对用户的真实数据进行破坏性试验
- 审查要求：Source、Test、Merge、Deployment、Live Verification 五种状态分别记录，禁止混为一谈
- 任务目标：以最小改动消除明显的低效工作、恢复黑洞和长 Turn 故障；维持现有工具/权限/审计/恢复协议；不扩展新产品功能

## 0. 先读：执行授权与严格范围

本文件授权本地 Agent 在隔离的非生产工作区开展分析、修复、测试、提交并创建供复审的 OPEN PR；不授权合并 main、不授权部署生产 HASHI4、不授权修改 Core，也不授权停止或重启生产 Agent。

执行前须读：
1. AGENTS.md；
2. ARCHITECTURE.md；
3. docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md；
4. docs/TESTING_POLICY.md 和 .github/CI_POLICY.md；
5. docs/PURE_IDIOT_TEST.md；
6. 受影响函数的领域契约、现有测试、近期已合并修复；
7. docs/herv3-search-root-causes-20261117.md（避免重新引入搜索超时/游标缺陷）。

执行须在当前可用的非生产 HASHI checkout 或独立 worktree 进行，确保与其他 Agent 互不干扰；确认 branch、HEAD、是否有未提交改动以及产物输出目录。若 main 之后移动，先列出冲突/并行变更，再选择以不覆盖其他工作为前提的 rebase/merge 策略。不要在生产 HASHI4 目录修改、热更新、重启、部署、清理任何文件；不要触碰生产 logs、state、sessions 和真实运行 PID。运行 python scripts/check_protected_core_changes.py，结果须能证明没有 Protected Core 更改。

禁止将本计划误解成必须修复所有相关软件：Workbench、Compute、SOK、HLI、HAL 与 xAI/HASHI API/Ollama 的同源问题只能报告兼容性风险。若修复共用模块必然影响其他 Adapter，必须做回归，不得悄悄扩大范围。禁止新增全局“优化开关”、额外守护服务、统一锁、固定等待轮询或重量级中间层。技术上确需配置时，默认使用符合普通用户期待的行为，解释为何存在配置和如何回退。

## 1. 成功的定义与优先级

业务目标：长时间、多工具、复杂任务仍能持续推进；常见文件/命令调用不因 Harness 无谓膨胀与阻塞；临时传输问题不抹掉已完成工作；用户可看见 Shell 超时前最后的可用输出；审计、取消与恢复仍然正确。

P0（先施工）：A1 SSE 二次方快照；A2 Shell 超时输出丢失；A3 短暂 DNS 失败与有限原调用重试。
P1：B1 混合批次只读段并发；B2 上下文增量优化/精确恢复边界；C1 file_list 非递归快路径；C2 SQLite 过大审计事件的空间放大。
不能为追逐速度导致新 P0：数据损坏、重复工具执行、断点恢复失败、错误的成功回执、丢失必要审计证据、权限越界、无法停止任务。

注意优先级与证据等级不同：代码级机制已查明，但报告中的 18.06 s/9.68 s、70+ GB、392K/552K、67–79 s、156–248 MB、2.4–2.9x 均来自当地观察日志，未由此 GitHub 分支复测。要在非生产测量确认具体原因，不要把它们写成我们的现场复现结论。

## 2. 源码导航与已知事实

A1:
- adapters/deepseek_api.py ~991–1004：每 SSE 工具碎片向 assembly_snapshots 追加累计参数 JSON round-trip；
- adapters/openrouter_api.py ~4455–4477：同类逻辑；
- adapters/openrouter_api.py ~4775–4810：request_prepared 在调用计时开始后构造完整 payload + hash；
- adapters/openrouter_api.py ~5119–5142：结束后 record response_received；
- adapters/openrouter_api.py ~2240–2360：_record_provider_wire_evidence；
- orchestrator/permissions/provider_forensics.py：受限 Host 同步 IPC forensic sink + canonical 持久性确认；
- orchestrator/canonical_audit.py：CanonicalAuditStore / BufferedCanonicalAuditWriter 两条不同写入路径，保留原始证据链；
- 同源观察（非默认施工范围）：adapters/xai_api.py、adapters/hashi_api.py、adapters/ollama_native.py。

A2:
- adapters/openrouter_api.py ~1881–1940：compact_intra_turn_tool_messages 只覆盖 role=tool；字符估算未计 reasoning_content；
- adapters/openrouter_api.py ~5480–5515：工具批次完成后调用压缩再记录 Provider context；
- adapters/deepseek_api.py ~307–335：DeepSeek thinking/tool 历史消息处理；
- orchestrator/her_v2/session_store.py ~1110–1200、~1276–1480：精确 Provider continuation 元素和 checksum，已实现某种前缀增量，禁止误删；
- 必须先核实当前官方 DeepSeek 模型/接口关于 tool-call reasoning_content 的精确规则；不能不经验证就裁剪活跃工具循环中的 assistant reasoning / tool_calls。

A3:
- tools/builtins.py ~349–445：超时清理后 _communicate_partial_tail；
- tools/tool_activity.py ~210–232：bounded_communicate 的 head/tail 存在协程局部变量；
- tools/builtins.py ~190–326：_cleanup_bash_process，Windows tree-termination；
- orchestrator/process_execution.py ~323–327：Windows CREATE_NEW_PROCESS_GROUP；这不等于保证所有后台子进程关闭 stdout/stderr 句柄。

A4:
- adapters/deepseek_api.py ~308：3 次本地传输重试上限；
- adapters/openrouter_api.py ~1786–1803、~2046–2049、~4884–5095：传输异常分类、退避、恢复预算；
- orchestrator/her_v2/runtime_invocation.py ~890–1005：外层 side_effects_seen 与 replay_safe，不能改为盲目整段重跑。

B1:
- adapters/openrouter_api.py ~1806–1829：_PARALLEL_SAFE_READONLY_TOOLS；
- adapters/openrouter_api.py ~3410–3667：全批必须只读，否则整体进入后续串行分支；
- 现有测试：tests/test_herv3_harness_improvements.py 的并发、失败/续跑用例；
- orchestrator/her_v2/v3_prompt.py 的 MAIN_CONTRACT / DEVELOPER_TOOLKIT_CONTRACT 已鼓励合并独立读取，不要先堆 prompt。

C1:
- tools/builtins.py ~885–930：file_list 转调 execute_file_search(expanded)；
- tools/file_search.py ~38–96：file_list 非递归工程默认预算 15s，递归预算 45s；
- tools/file_search.py ~215–330：每调用启动 search_worker 子进程并等待其输出/清理；
- docs/herv3-search-root-causes-20261117.md：已修复 worker deadline/游标竞态；快速路径不能回归其行为。

C2:
- adapters/her_v2_provider.py ~1849–1870：task_state_delta 审计包含 task_state.snapshot()；
- orchestrator/her_v2/audit.py：DurableAuditLog 的 canonical-before-projection、去重与 fallback；
- orchestrator/her_v2/session_store.py ~2721–2820：runtime event 完整 facts 进 payload_json；
- orchestrator/her_v2/session_store.py ~1138–1190：provider context 已通过 digest 做追加/分歧处理；不能声称全部记录都是全量 INSERT；
- orchestrator/her_v2/backend_session.py：fixed_sessions.sqlite3 归属。

源码行号会变。所有实施和复审引用必须用实际最终 commit 固定链接，不能只复制本计划的近似行号。

## 3. Phase 0 — 建立可信、可重复的非生产基线（先于修改）

0.1 固定版本：记录 git status、HEAD、main 合并基准、Python/Windows 版本、运行用户、执行 Shell（PowerShell 5.1/7）、临时根目录、所选测试 Adapter、工具注册表，以及若有的已有 open PR 与待合并冲突。确保没有其他 Agent 在同一 worktree 写入。

0.2 建立唯一非生产工作根目录、测试 state、audit、sessions 和日志目录。不得指向 HASHI4 生产路径；目录中使用合成文件和合成账号；HTTP 测试用 mock transport / 本地测试服务，不使用真实生产 key、真实 paid provider 数据或个人机密。需要真实 Provider smoke test 时，仅使用已获授权的非生产凭据并记录成本。

0.3 制作一份轻量基线记录：每个案例最少运行 3 次；波动大则重复更多次；记录样本数和环境负载。墙钟用 perf_counter，CPU 时间、peak RSS、进程数、bytes written、文件大小、tool_calls/min、provider wait 与审计耗时分离。不可把 API 输出速度/任务难度差异误认为 Harness 性能提升。

0.4 采集 provider call 分段时间：pre-request 组装、请求前 payload hash/审计、transport send/首 token/最后 token、response_received 审计、tool intent 派发、tool start/end、checkpoint、SQLite append。记录 p50/p95/max 和总量。注意现有 provider_call_latency_ms 开始于 request_prepared 前、结束于 response_received 审计前；不能直接称其为纯 API 延迟。

0.5 为 7 项写红灯最小复现：长 file_write 工具参数 SSE（至少 64KB、256KB、1MB 梯度；碎片数可控）；连续正常 shell 输出+超时；Windows 后台孙进程继承 stdout/stderr；DNS 解析抖动 15 秒；[file_read,file_read,shell,file_read] 混批；7 条目目录非递归 list；重复 task_state_delta 及 10MB+ 事件。所有测试必须有明确 expected failure 和修复后 expected pass，不依赖生产日志。

0.6 记录现存测试覆盖和缺口；先跑相关 targeted tests 确认 baseline 可运行。不要把一次全仓昂贵 CI 作为开发起点。

0.7 交付 Phase 0 证据表：case_id、baseline SHA、test command、samples、baseline outcome、关键环境说明、已复现/未复现、原始证据存放路径及可复核的简短摘要。涉及本机私密路径/令牌时只留脱敏摘要。

## 4. Wave A — 先消灭无谓浪费和恢复黑洞（一个可独立复审的功能范围）

### A1. SSE 二次方装配快照 / 审计热路径

步骤 A1.1：检查全部 assembly_snapshots、tool_call_fragments、wire_evidence 的生产者/消费者、Forensic 回放、invalid tool 修复与协议取证测试，确定每项原始审计字段的法定/恢复用途。先证明保存到第 N 段的完整前缀并非后续恢复/授权必需数据；若有消费者需要它，设计确定性重建函数，而非切断证据。

步骤 A1.2：保留每个 SSE 原始 arguments_fragment、index、arrival、id/name fragments、结束后的最终 tool_calls 及 transport 完整性元数据；删除每个碎片对增长中 acc 的 JSON 深拷贝。确保串流异常、JSON 断裂、多个交错 tool_call id、同一 id 重复碎片、零长度 delta 不错配；没有伪造 Provider 最终成功。

步骤 A1.3：评估 request_prepared 的 payload 二次序列化。优先复用一次 canonical bytes 用于 sha256；如大型完整请求必须在 Canonical Audit 持久保存，则使用原有大对象去重/内容寻址能力，不能以原文完全不存代替。不要在没有测量和确认 durability contract 前重新设计全局异步 Writer。

步骤 A1.4：针对受限 Host 同步 forensic RPC 与 canonical.flush，分别测 IPC/序列化/flush 的真实耗时与责任边界。仅当 A1.2/A1.3 后仍存在显著热路径阻塞时，再提出不牺牲持久确认的隔离线程/专门异步队列方案；不能让回执在必需证据未落盘前报成功，也不能丢失故障/取消时已承诺的 audit。

A1 测试：
- A1-T01：同一 SSE 输入与原版比对最终 tool calls 的 id/name/arguments 字节、顺序、语义及 raw fragment 可无损重建性；
- A1-T02：64KB/256KB/1MB、固定碎片大小，记录峰值内存、CPU、总 forensic bytes，检查增长由二次方趋向线性；不要硬编码某个环境的绝对毫秒阈值；
- A1-T03：1、2、4 个交错 tool calls，乱序 index、空 delta、invalid JSON、无 [DONE]、中断/cancel、timeout，错误分类和证据齐全；
- A1-T04：模拟 canonical sink 失败和 flush 失败，验证错误回执真实、不可把已执行工具重跑、审计链没有无效的伪成功；
- A1-T05：无受限 Host / 受限 Host、DeepSeek 与 OpenRouter 路径均绿；若共享字段变化影响 xAI/HASHI API/Ollama，应跑对应 focused contract test。
验收：同输入最终工具调用完全一致；能从持久化数据重建协议事实；大 payload 的内存/审计体积显著改善且有样本数据；没有审计丢失和虚假成功；按实际测试报告收益，不夸张宣称“70GB 已消失”。

### A2. Shell 超时保留部分输出与 Windows 后台进程

步骤 A2.1：在 bounded_communicate 中把有界 stdout/stderr head/tail 设为由执行器可访问的状态，保持原有 64KB 上界/双管同时读取、UTF-8 容错。超时、取消、子进程退出但管道不 EOF、清理失败时应能读取最后 4KB；不建立无界内存缓冲，也不磁盘无限 spool。

步骤 A2.2：把 foreground process exit 与 pipe EOF 分成不同状态；对 Windows 的 Start-Process/pwsh/cmd/.ps1，使用可复制的合成脚本模拟“父进程结束但孙进程继承输出句柄”。确定何时使用明确的后台管理工具，何时仅等待前台完成。不要对任意命令以丢弃 stdout/stderr 的方式假装修复；同步脚本仍需完整观察实际退出码与必要输出。

步骤 A2.3：有界收尾：超时只清理本次拥有的进程树，不影响 HASHI、无关 Agent 或系统进程；cleanup 错误仍可见。用户显式 /stop 必须及时取消，不得因等待管道永远阻塞。保持 output/exit_code/cleanup/partial_output 的既有调用契约，必要时只增补有区分度的字段。

A2 测试：
- A2-T01：子进程每 100ms 打一行并在 1–2s 超时；确保输出含预期末尾且标识 partial，不需要等待多分钟；
- A2-T02：仅 stderr、仅 stdout、两路交错输出、超过缓冲上限、非 UTF-8 字节与空输出；
- A2-T03：Windows Start-Process 继承管道、解绑管道、后台长任务三场景，验证不误判完成/不无限等 EOF/不杀无关 PID；
- A2-T04：硬超时、进程提前退出、取消、重复取消、树清理失败、1000 行输出、非零退出码；
- A2-T05：Python/Linux 非 Windows 路径回归，以及 tool_activity 的进度/终态事件契约。
验收：超时有可用最后日志，且可区分什么情况下确实无输出；受管树按期清理；停止信号正常；没有新增长期阻塞或输出膨胀。

### A3. DNS/连接前失败的有限重试

步骤 A3.1：分辨真正的 DNS getaddrinfo / 尚未发出 HTTP 请求，与无法证明是否被服务端接纳的其他 ConnectError、TLS、socket reset、SSE 部分输出。只给“可证实请求前失败”更长的等待窗口，其他走当前可靠性逻辑。

步骤 A3.2：建立局部、可取消的退避预算（目标覆盖约 60–90 秒 DNS 抖动，可按 5–6 次有界指数退避设计）；清楚定义次数、deadline、累计 provider cost、call ceiling、stop/cancel、error metadata。尽量不要和 INVALID_TOOL_CALL_REPAIR_LIMIT 的无关“坏工具调用修复次数”错误耦合；但也不要引入一大套可配置重试框架。

步骤 A3.3：只重发当前尚未成功的 HTTP Provider 调用。对于已经完成并有回执的 Tool 严格复用，不允许重跑；外层 replay_safe/side_effects_seen 仍有效。明确长期异常终局应返回可诊断、可继续的事实，而非静默“成功”。

A3 测试：
- A3-T01：DNS 连续失败 15s 后恢复，一个长任务在原 context、原已完成工具收据上继续；
- A3-T02：DNS 持续失败至有限预算耗尽后诚实失败，等待时间符合上限，用户 /stop 可中途结束；
- A3-T03：server 已接收后断流、连接重置、TLS 失败、SSE 部分响应不得冒充 DNS-before-send；
- A3-T04：先执行一次有副作用的合成工具，再 DNS 失败并恢复，assert 工具确切执行次数仍为 1，审计/会话按序续跑；
- A3-T05：rate limit/Retry-After、provider-call limit、permission denial、取消、外层阶段 fallback 原行为回归。
验收：不回放已完成工具、不破坏重试预算，15s 短 DNS 故障可以自动恢复且不会强制整体重新冷启动；不存在无限重试或无提示盲等。

Wave A 结束点：提交独立 commit、focused 红绿测试及非生产关键用例、PIT Quick 记录；无阻断回归才推进 Wave B。可在一个 OPEN PR 中提交 Wave A 供独立复审；未获批准不得合并。

## 5. Wave B — 工具吞吐与长上下文（独立于 Wave A 的复杂优化）

### B1. 混合批次中的相邻只读段并发

步骤 B1.1：分析 _try_run_parallel_readonly_tool_calls / 串行 fallback 的绑定、completed_bindings、事件顺序、permission_durable_wait、在途锁/资源、工具策略及增量审计。现有“全有或全无”行为确实可从源码确认。

步骤 B1.2：只对原有白名单且独立的相邻只读工具做有界并发（沿用现有并发上限 4）；任意 shell、file_write、text_replace、其他 mutation、无法解析/权限未决/副作用不明工具均为严格顺序边界。不要让后续读取跨越一个可能修改同一资源的工具提前执行。

步骤 B1.3：Provider tool_call_id 与最终结果顺序必须一一对应；已完成 sibling 在另一个失败、取消、恢复后不应再次执行。保留 permission durable wait、policy denial、异常收敛、审计/进度和回执所有权。GET/HEAD http_request 并不天然无副作用，不直接扩大白名单。

步骤 B1.4：v3_prompt.py 原来已鼓励批量独立读取；先量化模型多工具批次分布，除非证明提示词不足，否则不更改大段主 prompt。不为了凑“每轮 4 tools”硬性诱导有依赖的操作并行。

B1 测试：
- B1-T01：[readA,readB,writeC,readD,readE]，A/B 与 D/E 各自并发，C 严格处于两组之间；
- B1-T02：[shell,readA,readB] 和 [readA,shell,readB]，shell 前后结果顺序/资源隔离符合语义；
- B1-T03：readA 先完成、readB 失败，后续恢复不重复 A；测试取消、permission wait、重复 tool_call_id、policy denied；
- B1-T04：并发与串行输出逻辑、tool_result 配对、checkpoint 和 audit 事件可稳定追踪；
- B1-T05：测试最大 4 并发上限、不同资源锁、跨 Agent 不相互阻断。
验收：混合批次可兑现实测并发收益，但不牺牲执行顺序、安全边界或 recoverability；没有大面积 refactor。

### B2. 长 Turn 上下文增长与续跑冷启动

步骤 B2.1：在合成 100/200/400 个工具循环中分别测量：system、user、assistant.content、assistant.tool_calls.arguments、assistant.reasoning_content、tool.content、附件元数据占用；对比模型 input tokens/字符数/真实序列化 bytes。当前 _estimate_messages_chars 漏算 reasoning_content，应先改进观测/预算依据。

步骤 B2.2：检查 file_write 大参数是否必须在所有后续 API 请求中重复发送；确认精确恢复所需的 original tool call 参数、result pairing 和 provider-specific reasoning 状态。优先优化“新产生的重复非必要内容”和已完成、可验证的大数据，不要在活动 DeepSeek thinking-tool loop 任意删除 reasoning_content、assistant.tool_calls 或 tool pairing；不能将“超过 200K”作为自动丢上下文理由。

步骤 B2.3：构建经过协议验证的“完整工具批次/恢复边界 -> 精简上下文 -> 新调用”设计草图；判断是否需要留在单个 Run 内，如何保证用户原目标、任务状态、已执行工具凭据、幂等性和 model-specific continuation。必须从官方 Provider 当前协议、既有测试和真实非生产调用证明有效后，才落代码。需要新阶段控制器/大改恢复格式时应拆独立设计/PR，不要强行塞进本轮修复。

步骤 B2.4：崩溃/重启续跑时检查 provider_context_generation、resume ticket、digest、cache-hit、cache-miss 与调用大小。不得通过截断原始恢复证据“解决磁盘/上下文问题”，也不能将“冷启动 392K”宣称全部来自 compact 函数。

B2 测试：
- B2-T01：含 200KB file_write 参数、tool output、reasoning 的 100/300 工具轮次，比较输入 tokens 变化与每部分贡献；
- B2-T02：已完成工具不得被重新执行；中断/恢复前后会话、文件效果和结果一致；
- B2-T03：DeepSeek thinking+tool_calls 真实协议 smoke test，错误响应 400 / 信息丢失则立即停止该子方案；
- B2-T04：OpenRouter / 其他 provider 不得因 DeepSeek 专项处理发生回归，检查本地缓存前缀复用；
- B2-T05：模拟 crash、resume、无副作用恢复、有副作用恢复、partial SSE、工具结果丢失、无效配对。
验收：先提供可验证的 token/bytes 贡献与低风险改善；若完整上下文跨边界重建未经证实，则标记 DEFERRED/NOT VERIFIED 而不是冒险上线；不要宣称一定稳定低于 200K。

Wave B 结束点：单独 commit / OPEN PR，特别披露所有模型协议限制和实际未测平台。

## 6. Wave C — file_list 与 SQLite 局部优化（只动最贵的一段）

### C1. 非递归 file_list 快路径

步骤 C1.1：在可控 7 文件目录、50 文件目录、受限目录、网络盘/杀毒负载下分段量测：路径解析、access_root 校验、worker 启动、scan、进度事件、等待进程结束、cleanup 与 JSON 格式化。因 15s 默认搜索预算仍见 67–79s，不能未经分段计时就认为 scan 本身耗时 79s。

步骤 C1.2：非递归、无需复杂游标复用的普通 file_list 采用进程内 os.scandir 快路径；保持 _resolve_path/access_root 边界与 symlink/junction 不越界；保持 pattern(glob)、include_hidden、max_results、dir/file 标记、size_bytes、权限失败、相对路径、合法路径的 Windows/Linux 差异。不可越过 protected_read_paths 或其他 Function 安全范围。

步骤 C1.3：对于需要延续游标或超出简单快路径语义的查询，沿用原搜索执行逻辑而非重新写一个平行索引系统。不可把列出前 50 个结果冒充 coverage_complete=true；如果不能保证完整枚举，保持 partial 与 continuation 能力。恢复 2026-11-17 已修复的 deadline race 不得被新逻辑绕坏。

C1 测试：
- C1-T01：7 条目、小目录、隐藏文件、文件夹、unicode、glob/大小写、默认参数；
- C1-T02：数百/数千条目、max_results/partial/cursor、递归与非递归差异、权限错误、符号链接/Windows junction；
- C1-T03：15s 预算、强制子进程卡顿、取消、进程 cleanup，验证快路径不虚构“完整覆盖”；
- C1-T04：针对 7 条目普通目录，展示 p50/p95/进程创建次数，目标是可复核的显著下降，不能硬性保证在任何机器都是 <10ms。
验收：普通单目录浏览不再无理由启动子进程；复杂/递归搜索仍保持覆盖真相和资源边界。

### C2. fixed_sessions.sqlite3 空间放大

步骤 C2.1：在隔离样本 DB 只读统计 page_count/page_size/freelist_count、WAL 与 journal 文件、各表行数、sum(length(payload_json))、p95、最大单行；分事件 kind（尤其 runtime_task_state_delta、runtime_stage_attempt_failed、runtime_request_received），做 total byte contribution 排序。不能直接打开或 VACUUM 生产 DB。

步骤 C2.2：区分三个不同层次：immutable canonical event、供精确恢复的 provider context、可派生的 projection。优先只针对重复巨大 task_state.snapshot / failure payload 降冗余。事件名叫 delta 却写全量确实存在，但部分完整状态可能是恢复必需；没有读取契约与回放测试，不删字段。

步骤 C2.3：若改为 diff 或 content-addressed referenced payload，必须有明确 schema/version、引用原始内容的完整性校验、事务提交一致性、审计顺序、崩溃后恢复、旧 DB 兼容读取；不要因为压缩省空间就把只读检查变成网络或额外服务依赖。默认先考虑重用现有 blob/artifact 存储，而不是创建新的持久化系统。

步骤 C2.4：只有与现有契约兼容且证据证明节省显著才提交 C2 实现。不要在此 PR 触发历史生产 SQLite 迁移/删除/VACUUM；如果需要迁移，另写备份、演练、rollback 且单独待批。

C2 测试：
- C2-T01：大量重复 task_state_delta（含 100KB+ snapshot）前后 DB/WAL 增长、单行大小与 I/O 时延；
- C2-T02：单个 10–20MB request/failed payload，合法恢复读取与引用校验，不能丢诊断必要事实；
- C2-T03：crash between event/commit、exact idempotent retry、digest mismatch、缺失 artifact、旧版 DB read、会话恢复；
- C2-T04：并发 2+ Agent、断点续跑、权限边界、无生产数据库触及；
- C2-T05：保持 SQLite 事务时序与 durable acknowledgement，禁止用异步虚假成功替代正确回执。
验收：有按事件 kind 的归因证据，节省的是重复数据而非必需状态；恢复/审计 100% 可验证。

Wave C 结束点：独立 commit / OPEN PR 与明确的“是否值得做 C2”结论。若空间归因不支持改动，可不写实现，提交测试+证据即可，拒绝为了凑数开发功能。

## 7. 横向回归矩阵与 PIT

至少覆盖以下正常路径：
- HERV3 单 Agent 1/100/300 次工具循环：file_read、file_write、shell、file_list、repo_discover、git_inspect、text_replace；
- 模型返回单工具、多工具、非法工具参数、无工具直接答复、thinking/非 thinking、streaming/non-streaming；
- provider 正常、DNS 前失败、HTTP 已接收后失败、SSE 中断、API 慢响应、取消；
- 2+ Agent 并行时没有跨 Agent 锁占用或互相无理由阻塞；
- 单 Agent 完整已执行工具效果和 resume 后效果一致；没有重复文件写入/命令执行；
- 非生产 Windows native 优先，兼顾 Linux；PowerShell 5.1/7 分开报告可用性；
- 非生产测试的审计 events、checkpoint、ToolReceipt、session store 在失败后可读取，不产生虚假 Completed/Ready；
- 现有 HERV3 兼容 Adapter、Workbench 相关事件/显示契约只能按代码引用及有限聚焦回归验证，不启动生产 Workbench/HASHI4；WorkBench 离线不得改变独立本地功能的可用性。

PIT Quick：对每一子修复执行 PIT-04 傻逼逻辑、PIT-06 傻逼修复、PIT-07 傻逼回执、PIT-08 傻逼性能、PIT-10 傻逼恢复、PIT-11 傻逼复杂度；同时检查 PIT-02 测试是否只用 mock 通过却违背默认用户路径，PIT-12 交付是否把 merge 当部署。明确查“Stupid Dependencies / 傻逼依赖”：无实质技术依赖的软件/服务离线不应禁用此功能；新修复不得把 file_list、Shell、Session 绑定到无关组件 readiness。不要把 PIT 自身变成新的发布阻断层。

每一项对照五问：正常用户期望、实际可见结果、是否有更简单方案、在保护用户还是保护代码、删掉后是否真的更差。异常与恢复用例至少各有一次；测试 assertions 不能只检查日志字符串或者 mock “success”。报告 PIT-S/PIT-R 已做与 PIT-U 非生产真实行为是否已做；没做要标 NOT RUN。

## 8. 测量与验收输出格式

每条性能用例报告：case id / OS / hardware / branch HEAD / Python / test input bytes / SSE fragment count / tool_count / effort / provider / run_count / p50/p95/max wall / CPU / peak memory / disk delta / canonical audit delta / SQLite+WAL delta / API wait / overhead / outcome / note。对真实模型测量必须分离模型 thinking effort、任务内容、cache hit、token 数、provider API 耗时与 Harness。测量失败时也如实展示，不允许只报告最好的一次。

用户报告的 Codex mimi ~12.7 tools/min 与 HERV3 zelda ~5.3 / lin_yueru ~5.4 tools/min 是不同模型和任务上的现场观察，不可直接作为同条件加速验收。关注同样输入、同一工具、同一版本基线的 before/after。若用本地真实模型回归，不擅改任何 Agent 的全局默认 effort；medium/high/max 比较是可选后续性能实验，不与 bug 修复混在一起。

所有 benchmark 的原始样本只存脱敏、小体量结果；不要把 MB 级 SSE 原始内容或生产隐私日志 commit 进仓库。大型合成测试产物保存在非生产忽略目录并记录重现实验命令，避免使 Git 仓库和 CI 本身膨胀。

## 9. 提交与评审策略

建议至多三个修复 PR：
- PR A：SSE + Shell + DNS（彼此独立的小 commits，测试各自聚焦）；
- PR B：混合只读并发 + 低风险上下文观测/优化；若涉及 DeepSeek reasoning 协议或跨边界重建，应单独 PR 或列为 deferred；
- PR C：file_list + SQLite（若 SQLite 需要 schema 迁移，则另案，不在本轮强行合入）。

本协调分支可作为施工起点；本地 Agent 在不互相覆盖的独立工作分支/工作树上依次施工，并保证 PR 的 base 和 diff 清晰，避免每条 PR 包含前一条尚未合并的无关代码。先确认当前 main 最新 SHA/是否已有相同修复已合并，避免重复 PR。

每个 PR 描述必须列明：
1. 问题及用户影响、源码机制、涉及哪些文件；
2. 最小改动、为何不需要额外层/配置/默认关闭开关；
3. 红灯/绿灯测试的精确命令与结果；
4. Benchmark 环境、样本和前后对比；
5. PIT-S/PIT-R/PIT-U 范围及未验证部分；
6. Protected Core check；
7. 兼容风险（Provider protocol、审计、checkpoint、Windows 后台进程、并发顺序、SQLite schema）；
8. OPEN 状态、最新 HEAD、已部署状态（默认 NOT DEPLOYED）、人工审查待办、回滚说明。

Agent 可以在非生产开发环境反复修复。创建 PR 后全部保持 OPEN，不启用 auto-merge、不修改生产、不开自动部署。需要 ChatGPT/用户复审后，只有在用户另行明确批准合并/部署时才执行。批准后先确认 exact HEAD、必要 CI、权限、Core check、基线变更及冲突；按依赖合并 main；再**仅**在指定非生产环境采用最新 main 实测，报告运行 service、版本、时间、验收记录。任何生产 HASHI4 的部署、重启或升级均需再次独立授权，不随主分支合并默认发生。

## 10. 失败停止条件、回滚与最终交账

立即停止当前危险改动并保留可复现记录的条件：
- DeepSeek tool+reasoning 请求因裁剪上下文返回 400、丢消息、回放错误；
- 审计/canonical/hash 链不匹配、Provider 收据在持久化前宣布成功；
- 已完成 side effect 再次执行，或者工具调用次序/结果映射错乱；
- Windows 清理误杀非本任务进程、Shell /stop 无法及时生效；
- file_list 快路径越过访问根目录、跟随链接越界或误报完整覆盖；
- SQLite 恢复失败、旧 DB 不可读取、缺证据 artifact；
- 修改涉及 Protected Core 或接触生产 HASHI4；
- 发现同一任务被其他 Agent 修改，可能覆盖正在进行的工作。

回滚按 PR/commit 粒度撤销，保留能验证原行为的测试；不做跨项目全局状态回滚；所有迁移/空间清理须在独立批准、备份和真实恢复演练后再讨论。

最终交账模板：
- 当前项目、仓库/分支/提交 HEAD；
- 每项 issue：状态（source-confirmed / reproduced / fixed-in-branch / tested-nonprod / review-pending / merged / deployed-nonprod / production-unverified）；
- 改动及当前未做事项；
- 红绿测试列表和精确 command、通过/失败/未运行；
- 性能前后对照、运行环境与证据；
- 受影响的 Core/Adapter/Tool/SQLite/Workbench 协议；
- PIT 发现/修复结果；
- 每个 PR URL + exact HEAD + CI 状态；
- 未合并未部署证明、独立审批要求与后续优先级；
- 一段可复制的下一轮 agent handover，避免重新从头排查。

## 11. 给接手 Agent 的启动指令

项目：Bazza1982/HASHI；工作分支 plan/herv3-performance-reliability-20261123；施工文档 docs/repairs/HERV3_SEVEN_ISSUES_PERFORMANCE_RELIABILITY_PLAN_20261123.md。

先读本文件以及仓库 AGENTS.md、架构/测试/PIT 约束，确认非生产隔离环境与最新 Git SHA，然后立刻实施 Phase 0 红灯基线。随后按 Wave A -> Wave B -> Wave C 施工；每个 wave 完成独立小提交、聚焦 red/green、真实非生产测试和 PIT Quick，形成 OPEN PR 供复审。以最小根因修复替代额外架构；不可更改 Protected Core，不可接触生产 HASHI4，不得合并 main、自动部署或宣称生产修复完成。遇到协议不确定的 B2/C2 子任务，先出证据和明确 deferred 结论，继续推进独立可修部分。优先交付可信、可用、可回滚的修复，不追求所有条目都必须改代码。
