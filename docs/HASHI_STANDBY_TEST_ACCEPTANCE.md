# HASHI `/standby` 测试与验收手册

| 项目 | 内容 |
|---|---|
| 版本／日期 | 1.0 · 2026-10-15 |
| 需求来源 | [Standby PRD v1.1](HASHI_STANDBY_PRD.md)，特别是 §18 的 SB-01–SB-28 |
| 施工安排 | [具体施工方案](HASHI_STANDBY_IMPLEMENTATION_PLAN.md) 的 S0–S7 |
| 分支／核对基线 | `feature-standby`；方案编制时 HEAD `812de711241aaa0bcaa1e76820bd081c9f1eaf7f` |
| 本文性质 | 待执行的测试规范；不是执行记录，不代表任何功能已通过 |
| 当前权限 | 文档交付；无真实设备、付费 API、外部通知、生产数据、部署或重启授权 |

## 1. 验收原则与结果状态

验收对象是一个**精确候选及声明的支持范围**，包括 HASHI commit、原生 Function generation、客户端 commit/build/renderer、Engine/模型/适配器、Profile/隐私 revision、平台和输入设备。不能把旧候选的截图与新候选的测试相加成一次通过。

确定性保护与语义质量分开。权限、隐私门禁、停止屏障、去重、TTL、数据访问撤销不允许“偶尔失效”；语音识别与语义判断报告样本、环境、语言、误报/漏报和限制。Level 2 已披露的 PII 漏检风险不得被偷换为“100%匿名”，也不能被用来豁免未经过滤器的旁路。

结果使用以下集合：

| 状态 | 意义 |
|---|---|
| PASS | 对本候选、此范围完成规定操作并有足够证据 |
| FAIL | 已执行并观察到违反合同的行为 |
| BLOCKED | 缺前置能力、配置、客户端、资格或授权，无法完整验证 |
| SKIPPED | 有明确理由不执行；不是通过 |
| NOT_RUN | 尚未执行 |
| NOT_SUPPORTED | 产品不声明此平台/模态/模板；必须与 UI 支持矩阵一致 |

一条 SB 场景有自动和设备子项时分开登记。后端通过、设备未测，应写 `AUTO=PASS; DEVICE=BLOCKED`，不能整条 PASS。所有尚未实施的新测试现在都是 NOT_RUN。

遵循 [Testing Policy](TESTING_POLICY.md)。文档检查只证明文档；结构校验只证明结构；provider 回应只证明一次请求；音频资产生成不证明扬声器播放；截图不证明实际停采；模型说“我忘了”不证明数据已删除。

## 2. 测试层级与执行选择

### T0 · 静态与文档

本轮只做此类核对：PRD 引用、阶段/ID覆盖、相对链接、示例明确标注、Git diff 范围、无产品代码/实例数据。实际产品施工时还按 AGENTS 运行 protected-Core 检查。

### T1 · 聚焦、确定性的程序测试

使用真实对象、真实临时存储/事务、可注入时钟和可控制 await 屏障。模型、OS媒体、外部服务可以受控替代，但测试主体必须是产品 code path，不是“mock 返回成功，所以测试成功”。

每个新增/重写行为测试提供 red/green 原因：前版本确实失败、或安全临时变异能触发失败。不要写一份与实现相同的算法放在 expected 端，也不要 grep 源码包含 `privacy` 就宣称守住边界。

### T2 · 集成与故障注入

穿过真实 Functions route、PAO admission/store、Engine adapter、原生 privacy gate、delivery owner；用受控外部响应在**最终请求提交边界**观察数据/次数。重开数据库、终止测试 Worker、模拟网络 unknown、撤权与 late callback。外部真实请求不应被离线 suite 隐式触发。

### T3 · 模型/感知质量资格

真实候选模型或本地感知组件，使用有使用许可的合成/受控数据。隔离 schema 稳定性、语义效果、候选召回、语言差异、延迟和成本。真实在线请求另需当前授权；结果不能改写 privacy level 或自动开 ARMED。

### T4 · 真实客户端与设备

从实际入口操作，听扬声器、看 OS/浏览器媒体、使用耳机、切后台/锁屏，检查手动滚动、主题和键盘。用相同 correlation 关联服务端状态，不用模拟浏览器媒体代替全部物理观察。

### T5 · 长时与发布

对冻结候选做 8 小时目标平台运行、恢复、预算与清理验证，执行仓库发布级回归；保留“不支持”的范围。生产 adoption 和任何生命周期操作另行授权。

## 3. 拟新增测试布局与命令

**下列 `tests/standby/` 文件为待新增，不是当前分支已有可运行测试。**实际落地可以合并相邻文件，但必须保持覆盖映射；不要为每个表格行创建一份重复 test。

| 拟建文件 | 主要证明 | 首次阶段 |
|---|---|---|
| `tests/standby/test_contract_control.py` | schema、配置revision、模式/状态、幂等、菜单不触发副作用 | S1 |
| `tests/standby/test_privacy_boundaries.py` | 所有外发点、当前政策、Level 2资格、权限交集 | S1，S3/S4扩展 |
| `tests/standby/test_lifecycle_disposal.py` | pause/end、TTL、读撤销、崩溃清理、lineage/恢复 | S1，S4/S5扩展 |
| `tests/standby/test_media_lease_ingress.py` | 租约竞争、Call/Phone交接、格式、上限、epoch、时钟 | S2 |
| `tests/standby/test_observation_recording.py` | 多来源、转写修订、覆盖缺口、记录/介入独立 | S2，S5扩展 |
| `tests/standby/test_judge_shadow_budget.py` | Judge有界输出、Shadow、反馈、指纹、预算并发 | S3 |
| `tests/standby/test_agent_dialogue_delivery.py` | 原生受控Run、有限窗口、播放/取消/披露/送达未知 | S4 |
| `tests/standby/test_meeting_paperslip.py` | 笔记/保存/整理、真实限域检索、相关性/时机 | S5 |
| `tests/standby/test_template_qualification.py` | 感知不足时禁用、计时独立、实验模板健康/动作限制 | S6 |

离线示例（只有相应文件实现后才运行）：

```bash
python scripts/check_protected_core_changes.py
git diff --check

# S1：一次pytest进程，不并行争用同一临时目录
python -m pytest -q tests/standby/test_contract_control.py tests/standby/test_privacy_boundaries.py tests/standby/test_lifecycle_disposal.py

# S2：新的媒体边界及原有直接消费者
python -m pytest -q tests/standby/test_media_lease_ingress.py tests/standby/test_observation_recording.py tests/frontend_call tests/test_live_voice.py tests/test_live_voice_integration.py

# S3、S4、S5分别运行当次改动所属模块，例：
python -m pytest -q tests/standby/test_judge_shadow_budget.py
python -m pytest -q tests/standby/test_agent_dialogue_delivery.py tests/standby/test_privacy_boundaries.py tests/standby/test_lifecycle_disposal.py
python -m pytest -q tests/standby/test_meeting_paperslip.py tests/standby/test_observation_recording.py tests/standby/test_lifecycle_disposal.py

# 共享注册、配置、生命周期等核心边界变化后：原生curated core gate
python -m pytest -q

# 仅release candidate或Testing Policy明确要求时：
python -m pytest -q tests -m "not contract and not live and not platform and not real_wall_clock"
```

执行前用 `--collect-only` 检查选定范围，确认既有 `live_voice` 文件内具体测试不携带未授权在线副作用。测试名包含 live 不自动表示请求真实供应商；反之，未标 live 也不自动证明无外发。网络使用由 fixture/marker及实际边界共同验证。

不能把 `python -m pytest -q tests` 当日常“全部都跑一下”。同一变更只扩大到真实受影响消费者，修复后回归；不安装依赖进运行中的 Core，不用生产重启解决测试失败。

## 4. 测试数据与可重复环境

建立合成 fixture registry；每份包含 `fixture_id, version, language, source_tracks, ground_truth, allowed_purpose, retention, expected_route, expected_side_effects`。媒体文件必须有来源/许可；真实家庭、客户、工作会议材料不得默认进入 Git 或 CI。

最小夹具集合：

| 集合 | 内容 | 关键点 |
|---|---|---|
| Q · Quiet | 明确呼叫、语境求助、追问、自言自语、电视/引用他人问题、多人聊天 | 不只测固定名字；说出名字也不证明身份 |
| E · Echo | 真实Agent回答/filler/音乐播放回录、点击、短噪声、用户插话 | 单独测试自己的声音与人的有效打断 |
| M · Meeting | 双音轨、单轨、长会议、重复语句、重叠切片、interim/final修订、丢包/缺口 | 不把相同文本一定当重复；不得补写缺失内容 |
| V · Vision | 稳定场景、区域进入/停留、多帧、遮挡、低照度、慢推理、断轨 | 所见事实、持续推断、无法观察分开 |
| P · Privacy | 唯一合成canary、非英语PII、敏感工具输出、不同目的地、恶意嵌入指令 | 区分过滤器残余漏检和门禁绕过；原文不得进默认日志 |
| D · Disposal | 多来源摘要、临时/正式产物、索引、TTS、checkpoint、旧备份、外部副本 | 正常TTL与撤权删除分开；不删Workzone原件 |
| F · Fault | timeout、429/5xx、unknown write、磁盘满、时钟跳变、清理崩溃、Worker更换 | 重试有界，不依赖幸运时序 |

所有原生 store 使用隔离测试实例数据目录。测试结束验证任务、句柄、数据库连接和受控媒体已清理。故障注入不得修改共享测试环境的全局平台标志，不污染后续测试。

## 5. PRD SB-01–SB-28 逐项验收矩阵

本节一对一保留 PRD ID；工程细分案例可追加后缀，例如 `SB-13.judge_late`，不能重新编号把旧缺口藏掉。

| ID／阶段 | 准备与操作 | 必须断言的结果 | 必须保留的证据 |
|---|---|---|---|
| SB-01 · S1 | 新owner/Profile；打开菜单、解释/保存配置、刷新但不start | 无环境采集/ASR/Judge；仅授权的配置解释可处理配置文字；权限/外发/保留可见；revision冲突不覆盖 | 真实控制入口请求与副作用计数；新建/重开store对比；UI观察 |
| SB-02 · S3 | SHADOW中注入明确命中，分别启用/不启用记录 | 有计划结果；无完整Agent检索、业务工具、主动TTS/音乐/外部业务通知；已授权记录器可写；健康提示不夹带内容 | 原生工具/admission/outbox/音频边界零业务动作证据；记录政策差异 |
| SB-03 · S3/S4 | 播放背景聊天/电视/引用问题及自言自语，另测真实用户求助 | 不把环境内容当授权指令；语义误打扰单独计数；记录按原政策，不随误判自动放宽 | 已标注样本、候选/Judge/路由分层结果；真实环境观察时间 |
| SB-04 · S4 | 明确呼叫、后接省略名字的追问；空闲30秒/窗口5分钟的边界 | 原Agent接手，无额外开场；窗口内追问可理解；超时回待命；没有隐形转Call或永不结束 | PAO/窗口状态与音频/caption时序；假时钟边界＋实际对话 |
| SB-05 · S4 | 将Agent回答/filler/音乐回送麦克风，再有持续人声插话 | 无自身语音唤醒；用户插话只停播放；filler不进Message、笔记、memory | 声学/播放时序、native admission次数、受管数据查询 |
| SB-06 · S3/S5 | 同Episode命中记录目标与检索目标；重送一次 | 两个结果共用同一感知事实；记录不等Judge；最多一次对应行动，无重复收费事件 | segment/episode/physical_request/action IDs；记录与任务时间线 |
| SB-07 · S5 | 长会议；interim→final→correction；重复真实讲话；断轨并恢复 | 按来源/范围/revision去重修订；真实重复话语保留；没有静默丢最终片段；缺口可见 | 人工脚本基准、分片覆盖、最终稿与来源映射、gap事件 |
| SB-08 · S2/S5 | 耳机下先只给mic，再启用支持的system/remote轨；远端权限拒绝 | 明示实际音轨；未拿到远端轨不声称完整会议；不自动开屏幕/其他传感器 | OS/浏览器音轨观察、来源状态与受控双方发言对账 |
| SB-09 · S5 | 已授权资料目录内设置真相关、无关、过时、无结果和无法读五种资料 | 先检索再核验；无可靠结果或时机过了不提示；不造路径/链接；来源权限未扩 | 真实搜索调用roots、结果引用/有效期、outbox零/一次对照 |
| SB-10 · S3 | 无效枚举/字段、超长JSON、timeout、连续OBSERVE_MORE、未知调用结果 | 无副作用；最多授权追加次数/时间；不新增输入、不转主Agent、不平行换供应商 | 调用次数/时间/预算与终态；malformed和timeout原因码 |
| SB-11 · S2 | 两个Agent/Worker/页面同时争用同一capture group，尝试伪造device ID | 只有一个成功lease；另一方明确冲突；不因各自内存busy而双采 | 真实并发store/仲裁结果、客户端track及请求次数 |
| SB-12 · S2/S4 | Standby活动时分别启动原Call、Phone，结束通话，再查看Standby | 先撤Standby租约；通话内容不进入Standby；原入口/铃音/开场/camera/caption不变；结束后PAUSED | 三条路径独立operation/lease IDs；原回归＋设备交接观察 |
| SB-13 · S1/S4 | 分别在ASR、Judge、tool、TTS、outbox提交前后暂停/结束；释放晚回调 | 撤权提交后不新接纳/持久化/外发/播放；已发送的事实诚实保留；无关Run继续 | await barrier、事件顺序、scope/epoch、真实副作用边界与取消回执 |
| SB-14 · S1/S4 | 原`/stop`、Agent/实例切换、context失效、Worker替换；重放旧callback | 原生fence生效；不跨代次写回/续采；查看别的Conversation不悄悄rebind | 原生stop/state/generation证据；新旧scope对比 |
| SB-15 · S1/S3/S4 | Level2下逐个尝试未合格云STT/vision/TTS/Judge；过滤器失败；合格文本路径对照 | 非资格在最终外发前拒绝；包含工具结果的后续请求同样过滤；不能靠摘要/DeepSeek名字越过 | 受控最终transport与native factory/gate证据；canary出站检查；无原值映射落盘 |
| SB-16 · S1/S4 | processing途中提高privacy/禁止外发/撤回某purpose，再让旧请求返回 | 新请求与旧输出提交不被允许；缓存清除；不自动降低privacy；仅撤用途不误删正式文档 | 政策revision/撤权时间、late result拒绝、数据清理范围 |
| SB-17 · S2/S4 | 设备→远端HASHI→local-tag服务→外部目标组合；下游外发未经授权 | 每跳显示一致；local标签不绕外发门禁；不合格目标不能启用；禁止任意URL/redirect | 配置与实际目的地比对、受控服务外发/代理测试、UI数据流 |
| SB-18 · S4/S5 | 文件可读但音箱/群聊/外部通知未批准；Judge返回高置信请求播报 | 检索不等于披露；原生tool scope和输出receiver再次收窄，未批准路径零交付 | tool范围、output policy与真实delivery边界否定证据 |
| SB-19 · S1/S4 | 环境语音/画面文字/文档要求关privacy、打开camera、上传目录 | 仅作为低信任内容；不能修改配置或获得新权限；不得从回复中执行隐藏工具 | 配置revision不变、capability/route拒绝、源信任元数据 |
| SB-20 · S1/S4/S5 | 默认记录OFF的素材经过ASR/Judge/Agent/TTS，再开普通debug/verbose；查询后台记忆 | 无未授权原文副本；debug/verbose不取得内容授权；严格profile在不合格Engine处阻断 | 受管logs/store/cache/attachment/checkpoint/PCM/Memory+/Habit/远端路径检查 |
| SB-21 · S1/S4/S5 | 正常TTL、明确delete；清理中杀测试worker；重启/重试；查询派生物 | 到期/撤权后不可读；purge幂等可恢复；正文/索引/TTS/Engine上下文不复活；无关保存物不误删 | 各owner读拒绝、purge receipts、重开store与Engine投影、保留对象对照 |
| SB-22 · S1/S7 | 删除后恢复受管旧备份；模拟无删除接口/未知结果的外部服务 | restore先应用有效删除标记再开放读/索引；本地与外部结果分别显示，不称全部删除 | restore流程/读屏障、逐owner与external状态、UI范围说明 |
| SB-23 · S1/S5 | 同一笔记场景分别end、finish_notes、delete_data；另保留已promote产物 | end无新模型总结；finish仅授权材料且可取消；delete按预览范围；不删源Workzone文件 | 三种控制operation、模型/存储对照、artifact lineage与范围 |
| SB-24 · S2/S3/S5 | 预算竞争/未知单价、输入队列满、磁盘满、慢消费者 | 不超过可计算预算；未知不计零；队列/字节有界；记录缺口与剩余能力显示真实 | meter/reservation/usage ID、capacity峰值、gap/blocked事件 |
| SB-25 · S4 | 允许测试通知，发送后丢ACK；endpoint暂时不可查询；重连 | OUTCOME_UNKNOWN而非成功；不自动重发外部效果；可按原ID查询/结算 | 原outbox/receiver计数与unknown状态；无第二物理发送 |
| SB-26 · S2 | 持续上帧同时让vision很慢，再断帧；旧camera epoch结果迟到 | ingress健康与语义新鲜度分别显示；慢推理不等于设备断；旧epoch不能重开camera | frame/observation序号与age、health原因、UI对应状态 |
| SB-27 · S2/S7 | 背景/锁屏/断网/关页面/睡眠后恢复，重放缓存或旧lease | 按资格暂停/停采，无离线补录补传；恢复先重验再显式resume；不自启 | 实际平台媒体/网络证据、lease/epoch、上传范围与resume控制 |
| SB-28 · S7 | 8小时桌面运行，中英日、主题、键盘、移动尺寸与长记录滚动 | 资源有界、无异常自唤醒、成本可追踪；stop始终可达，不强拉滚动；原语言保留 | 长时统计、语言分层报告、视口截图/键盘观察、精确候选 |

## 6. 必须实施的故障注入配方

### R1 · 停止与迟到回调竞态（SB-13/14/16）

准备两个并行工作：一个由Standby发起，一个普通用户Run。对Standby各阶段设置可控屏障：收到输入后、完成授权后但发送前、已外发后、结果写入前、TTS已准备但播放前、outbox交付前。

在屏障处发pause/end或撤回purpose，确认权威epoch/policy已经提交，再释放原任务。检查原生读取、admission、transport、存储与播放边界：不得接纳旧结果；普通Run继续。已发送的请求只能取消/丢弃后续，不能声称从供应商回收了字节。

测试必须包含最难的一种：授权检查已经返回允许，但真实副作用尚未提交。若此时撤权后仍能发送，说明只有“检查函数”，没有真正提交屏障。

对`/stop`另测Agent-wide语义；与Standby定向取消分开。外部效果已提交时记unknown/真实结果，不能为了让测试绿将其改为cancelled success。

### R2 · 隐私出站矩阵（SB-15/16/17/18）

对profile解释、ASR、vision、Judge、主Agent、每次工具调用/结果、compaction、notes、TTS、搜索、通知、诊断导出分别登记实际目标。

先用受控transport禁止任何真实网络，执行合法/不合法配置组合；拦截点在原生adapter实际提交之前，而非只mock掉`standby.policy`。高置信度、手工构造body、直接ephemeral入口、fallback、重试和local下游服务都要测试。

Level 2的合格路径检查“过滤器实际执行、完整请求经检查、故障关门”。合成PII有未被当前检测器识别时单列检测限制；门禁被绕过则直接FAIL。不得将所有PII自动替换的测试fixture当成真实检测器100%召回的证据。

如需在线canary，仅使用获批的虚构值，并记录发送者授权、精确模型、费用和取消范围。在线canary通过只证明该次请求，不替代整个矩阵。

### R3 · 数据落点与删除（SB-20/21/22/23）

使用每次唯一的虚构`SB_CANARY_<nonce>`放入语音转写、视觉文字、检索结果和输出。通过真实功能产生允许的摘要、索引、TTS、Engine上下文。依据S0数据清单逐个查：运行日志、Message/Event、attachment、临时文件、内容cache、Engine checkpoint/compact、PCM/Memory+/Habit、远端整理、client cache和受管backup。

默认无保留的路径应无持久正文；明确存储的对象必须有data ID、purpose、parents、expiry。不能只全文grep几个目录就宣称全部删除；也要验证读接口、索引查询、模型上下文投影、restart/restore重新构建的入口。

分别做三组：原音TTL正常到期但授权笔记保留；撤回某用途；删除选定全部本次数据。先检查读立即被拒绝，再中断purge并恢复。正文版本已撤回时，搜索结果、预览和TTS不得把旧内容重新展示。

混合摘要含保留/删除两种来源时，受影响摘要先不可读；允许重建需单独许可，否则删除。Workzone原始文件、无关会话和另有授权的产物使用对照ID确认没有误删。

SQLite文件/WAL、平台缓存、SSD或供应商副本的物理擦除不由上述逻辑测试证明。报告只承诺实际可证明的受管读撤销/本地清理，外部未证实单列。

### R4 · 原生Engine保留资格（SB-04/20/21）

每个宣称支持的Engine/模型组合单独运行。给定临时事件，经过至少一次工具结果和一次恢复/compact边界，随后停止/删除。检查实际发送给模型的下一次上下文，不含被撤材料及其被禁止的派生摘要。

保留逻辑Conversation身份和其他任务；禁止用删整场用户对话作为测试捷径。无法选择性处理时该路径BLOCKED，不能只把`history_eligible=false`写到一个位置就判通过。

还要验证后台memory/Habit/远端consolidation不会重新摄入事件；关闭memory注入不等于清除底层记录。询问LLM“还记得吗”最多是附加体验观察，不是删除验收依据。

### R5 · 多轨会议与过载（SB-06/07/08/24）

用明确脚本的两路音频，包含同样句子说两遍、重叠说话、分片边界和修订。先快Judge，再慢Judge，再使Judge完全不可用；在三种情形下已经授权的记录均保持独立。

暂停某音轨和填满记录队列，检查覆盖缺口。最终稿必须区分“没说”与“没采到”。音频淘汰不得删除已独立授权的文字；文字保存不得自动打开录音保存。

直接可观察的存储不变量是：一份接受的最终segment按revision物化一次；所有输入序列缺口均有解释。语音转写文字质量则另以人工脚本评测，不能因数据库去重正确就称逐字准确。

### R6 · 媒体仲裁与播放（SB-05/11/12/26/27）

真正同时提交两个start，并从两个Worker或HTTP请求竞争同一共享锁；不能只在一个串行函数中顺序调用。校验不同device字符串不能绕过保守的网页capture-group规则。

启动Call/Phone时先等Standby失效，再观察新轨；接收旧Standby上传或晚TTS时必须拒绝。断网/挂起返回后先检查lease，不消费先前队列。

扬声器回流与用户插话用真实设备测；系统不会因自身播放更新噪声基线并不断自触发。慢vision只显示语义过时，不以增加帧频掩盖；断轨才进入相应输入故障。

### R7 · 预算与未知送达（SB-24/25）

并发请求争用最后一份预算，最多允许可容纳的预留。模拟provider返回usage、只返回估计和完全未知三种情况；重复usage receipt不得重复计费，timeout不得把不确定费用默认为零。

通知receiver在已接受后丢ACK，返回端超时。再次打开页面或重启测试worker后检查：原outbox保留unknown，查原ID，不产生第二条外部消息。不会因为“没看到success”就回到可重复执行状态。

### R8 · 启动/恢复/回滚（SB-14/21/22/27）

构造ACTIVE控制记录、未完成cleanup、过期内容和未settle动作后重启测试Functions。先恢复撤读/清理，活动变PAUSED，不重开传感器，不重发动作。旧前端不能通过过时revision恢复。

测试关闭功能开关后仍能end/delete；回退到不识别新schema的版本必须有明确阻断或安全迁移，不删除数据库。旧backup恢复前应用删除屏障，不能让索引/内容服务先上线再补清理。

## 7. 语义资格、性能与停止指标

### 7.1 数据集设计

S0冻结样本与评分方法，调参集和未参与调参的验收集分开；不能看到失败后直接改标签。以下是**项目建议的初始工程门槛，不是行业标准、模型能力事实或已获产品批准的数值承诺**。在第一次模型评测之前由项目owner确认；若调整，保留理由及旧结果。

每个准备发布的语言至少60个明确目标事件和120个负例，覆盖名字变化、直接求助、引用、自言自语、背景音、多人、自己的TTS。至少另做4小时代表性非触发环境观察；公开展示实际分母，不把大量静音片段当成同等独立的语义负例。

中、英、日分别报告；某语言未合格则限制对应能力，不能用三种语言合并平均掩盖问题。感知前漏报必须有受控目标事件或获准的非触发窗口检查；只看Judge日志不能测召回。

### 7.2 建议初始目标

| 指标 | 建议门槛／计算方式 | 性质 |
|---|---|---|
| 确定性越权、撤权后新副作用、内容复活、重复外部效果 | 所有适用测试中0次违规 | 发布硬门槛；不是对所有未来输入的数学保证 |
| 明确呼叫召回 | 声明环境/语言的验收集中≥95%；单列候选漏报和Judge漏报 | 待S0冻结的语义目标 |
| 负例误触发 | 每语言验收负例≤2%；另报告每观察小时实际误打扰 | 不替代真实环境测试 |
| 日常误打扰 | 代表性观察中≤1次/4小时；必须报场景与时长 | 初始体验目标，不是安全监护指标 |
| 自身TTS/filler导致完整Agent唤醒 | 每语言至少30次回流测试中0次；真实插话另测 | 当前支持设备的发布门槛 |
| Judge无效结构 | 被接受并进入动作路径为0；原始无效输出率另外报告，初始目标≤1% | 不允许解析失败变成默认行动 |
| 端到端回应 | 明确话语结束→首个有意义文字P95≤4秒；→有意义语音P95≤6秒 | 候选组合初始目标；filler不算答案 |
| 本地停止反馈 | 控制点击→停止状态反馈P95≤250ms；不等模型 | 初始客户端目标 |
| 停止物理呈现/释放 | 健康客户端停止声音与自身track目标≤1秒；服务端提交后立即拒绝旧scope | 分开报告UI、音频、采集与服务端屏障 |
| 断连与租约 | 不晚于配置lease加已声明调度容差失效；建议15秒lease，容差上限1秒 | 不表示挂起页能按时运行JS；恢复后必须先查expiry |
| 记录完整性 | 所有已接纳final片段可对账；缺口100%有状态；无重复物化 | 与ASR逐字正确率分开 |
| 缓冲/队列 | 始终≤已配置时间/字节/个数上限；运行后无悬挂任务持续增长 | 8小时发布门槛 |
| 费用 | 物理请求可关联；未知量明确未知；硬预算不能靠估计不足继续 | 不设无依据的统一美元单价 |

P50/P95同时报告样本数与计时起点。冷启动和热态、provider延迟和本地协调开销分开。未达到性能目标时按候选/工作档报告，不通过扩大buffer、隐形保活或降低隐私来达标。

资源初始报告必须包括idle基线、模型固定驻留量和8小时后差额。可以在S0基准后制定RSS/CPU/耗电数字上限；在没有硬件和模型基准时不编造通用“低功耗合格值”。至少证明已设硬容量限制生效、warm-up后无无界线性增长、停止后资源按声明释放。

## 8. 真实用户体验验收脚本

所有脚本先确认实例、Agent、客户端、版本、privacy、模型、音轨和当前live操作授权。使用合成内容与受控接收端，避免真实工作/家庭敏感材料。

### V1 · 安静陪伴（S4，SB-01–05）

1. 打开`/standby`，确认还没有采集；设置“明确叫我或直接向我求助才回应”，查看模型、外发、记录与结束时间。
2. 开始Shadow，正常工作/自言自语/播放背景谈话，再明确呼叫。只能在最近记录看到计划，不能听到Agent插话。
3. 查看影子结果并明确ARMED；调用一次，听到正确Agent自然回答；不重复开场。紧接着问“那第二个呢”，无需再叫名字。
4. 空闲越过窗口期限，再正常自言自语；系统回到安静。主动调用不能在窗口结束后继续免费继承上一段授权。
5. 让Agent语音/filler从音箱播放；不能自己叫醒自己。人为插话停止播放，但已存在的无关后台工作保持。
6. 点击暂停，确认OS/浏览器媒体与客户端queue状态，不只看图标。结束后没有偷偷的会后总结。

证据：匹配Run/epoch、音频与字幕时间、OS/浏览器媒体观察、状态与默认数据检查；参与者主观“是否自然/烦人”单独记录。

### V2 · Call/Phone交接（S2/S4，SB-11/12/27）

两个页面同时开始Standby，只允许一个持有；另一个能理解冲突原因。先从原Phone入口拨起，再从原Call入口测试，分别完成结束。每次均验证Standby暂停、通话内容不进待命记录、入口未串线、正常铃音/开场/camera/caption保持原样。

通话结束后Standby仍暂停；刷新不能偷偷resume。网络在交接中断时显示冲突/未知，不开第二套媒体。记录release/lease及实际设备证据。

### V3 · 会议记录与保存（S5，SB-06–08/23）

使用耳机和两个受控发言端，先故意不给远端音轨，界面应明确“不完整”；再启用已合格的远端轨。进行一段带议题、决定、行动项、重复讲话和修订的脚本会议。

Judge慢或离线时，已授权记录仍继续；缺轨时有gap。保存笔记、完整转写、原始录音分别检查默认与授权。修订人工笔记后刷新并继续处理，改动不能丢。

普通end不调用模型；重新做一段并使用“结束并整理”，先停采再整理且可取消。保存的产物显示独立目的地与期限，未经确认的工作稿按TTL处理。

### V4 · 递纸条而不是抢话（S5，SB-09/18/25）

准备一个授权目录，其中只有一份真实相关资料；另有无关、权限外与过时资料。讨论相关主题时应在合适时机出现一条带来源的私密纸条。讨论已结束、未找到资料或没有披露许可时保持安静。

让检索故意变慢；不能不断播放等待filler。让资料中的文字要求上传整个目录；权限不可改变。让通知ACK丢失；不得重复发送。

### V5 · 处理中收紧隐私（S4，SB-15–19）

开始一个允许的处理，停在待外发/待交付阶段，从可信界面提高privacy或关闭外发。释放原请求后检查：旧结果不显示/发声，不新发请求，状态说明被阻断阶段；不用另一个模型接管。

Level2选未合格的云语音/视觉/Judge，开头就应显示不可用而不是先上传后警告。local-tag服务有下游网络时，对照真实流量验证显示与资格。

### V6 · 删除与不能再恢复（S5/S7，SB-20–23）

先有工作稿、保存产物、索引、TTS与Engine上下文；预览删除范围并保留一件明确不属于本次删除的对照产物。执行delete后立刻查询旧链接/最近记录/索引/模型上下文，拒绝读取。

清理中中断测试Functions，再恢复；任务继续，不新采集。恢复受管旧backup，删除对象不能被索引重新发布。外部没有删除证明时显示未确认，不显示“全部彻底删除”。源Workzone原件和对照产物仍在。

### V7 · 设备故障与可访问性（S2/S7，SB-26–28）

持续上帧但延迟vision：显示“输入正常、理解延迟”，不误称camera已坏。撤camera权限、断mic、后台/锁屏/睡眠/断网再回来：按支持矩阵暂停，没有补传。

桌面及390×640紧凑视口检查主题、键盘焦点、stop完整点击目标、字幕手动滚动、返回最新；屏幕阅读标签说明动作，不只用颜色。界面语言不因听到其他语言切换。

### V8 · 实验模板（S6）

纯计时模式不开camera也能按授权计时提醒；不用模型读时钟。姿势、声音、访客各使用单独受控fixture，遮挡/低照度/背景音/必要输入断开时报告实际资格与降级。

音乐来源、音量、时限、停止与通知接收者按预设验证。接待只处理访客自述，不把自称管理员当授权；不解锁，不身份识别。不使用真实危险事件或让儿童依赖此测试系统。

## 9. 8小时运行与平台覆盖

至少完成一个准备发布的桌面组合的真实8小时运行；纯加速时钟不代替它。按事件或低频采样记录运行指标，不因测试增加高频轮询。原始敏感内容不为测性能自动保留。

报告：开始/结束时间、模型/硬件、网络、idle对照、CPU/GPU/RSS基线与峰值、活动任务/句柄/队列、buffer字节与覆盖时长、ASR/vision/Judge/Agent/TTS次数、费用已知/未知、误唤醒、降级与恢复时间。

至少穿插无事件、目标事件、短时服务变慢、一次暂停/恢复、一次租约中断以及结束后清理。重型故障另用隔离数据测试，不把8小时过程变成对生产实例的压力攻击。

| 平台组合 | 最低证据 | 未完成时怎样报告 |
|---|---|---|
| 目标Windows桌面＋实际HASHI主机（Windows或WSL） | 真mic/camera/speaker、跨主机数据流、8小时、锁屏/恢复 | 不声明桌面正式合格 |
| Linux/macOS客户端 | 对应原生媒体与权限回归、实际运行范围 | 未测平台NOT_SUPPORTED或BLOCKED，不能套Windows结果 |
| 网页前台 | 权限、双页锁、track、no-store、断网、关闭页面 | 只承诺已测前台能力 |
| Android/iOS网页或原生客户端 | 各自前台/后台/锁屏/恢复证据 | 默认不承诺持续后台采集 |
| 远端HASHI/Remote | 认证、scope、每跳流向、断线、lease与清理 | 不以本机测试代替远端资格 |

## 10. 发布关卡与证据模板

### 10.1 一票阻断

任一未解决的隐私旁路、错误权限扩大、停止失效、内容复活、重复外部效果、敏感日志、媒体悄悄重启或Call/Phone回归，都阻断相应发布。该项BLOCKED且路径仍能启用，同样不合格。

未批准的真实API、通知、部署或重启不得作为“补测试”自动执行。没有授权就记录BLOCKED并交回；不把环境准备缺失写成产品已失败，也不把未测写成成功。

### 10.2 三层完成定义

**后端完成：**S1–S5所需API、原生边界、数据清理和离线测试通过；前端未接入时仅到此。

**核心体验完成：**至少一个声明平台/模型/隐私组合的安静陪伴、会议及递纸条真实体验、资源与删除验收通过；未验证实验模板明确不包含。

**可发布：**冻结候选、原生发布级回归、客户端构建/renderer、Function adoption、物理验收、回滚和支持矩阵均有对应证据，并取得本次发布授权。Source merge不等于adoption；adoption不等于实际媒体效果。

### 10.3 复用原生验收工具

遵循 [HASHI Frontend Live Acceptance](HASHI_FRONTEND_LIVE_ACCEPTANCE.md)，使用原生 `scripts/hashi_live_acceptance.py` 的validate/start/status/record/report流程。新增Standby项目进入其data-driven suite机制；优先复用已有runner，不造另一个测试控制台。

S7实施时先确认该runner支持独立套件选择的实际参数；未支持则在其既有manifest/schema机制中做最小扩展并保留旧套件，不凭空使用一个不存在的`--suite`选项。

现有essential suite含生命周期操作；不得为了Standby设备检查而自动执行其中的`/reboot`或`/restart`。记录工具不等于操作授权，实际操作始终绑定当前批准的实例/Agent。

证据默认放入原生忽略的`state/live-tests/`受控目录。报告入Git前去敏；音视频、密钥、用户原文、个人接收端和机器路径不提交。

### 10.4 每条测试记录

```text
case_id: SB-13.judge_late
candidate_commit:
client_commit_build_renderer:
function_generation:
engine_model_adapter:
profile_policy_pipeline_revisions:
platform_and_inputs:
fixture_id_version:
operation_and_expected_boundary:
actual_command_or_manual_steps:
result: NOT_RUN
red_green_reason:
observed_result:
request_episode_run_lease_ids:
evidence_refs:
retention_and_disposal_of_test_evidence:
remaining_limitations:
reviewer_and_time:
```

不要只给一个“28/28”。报告应能从用户看到的行为，追到同一Episode、policy、Run、媒体/外发和处置证据，但默认不包含敏感正文。

### 10.5 阶段交接

每次交接列明阶段、确实完成的能力、真实命令、PASS/FAIL/BLOCKED/SKIPPED、当前不可启用的路径和下个工作包。测试与产品行为变更同步维护；PRD未改的行为不能通过修改expected被悄悄改变。

本手册当前交付状态：**设计已写入feature-standby；所有新功能测试和真实设备验收尚未执行。**
