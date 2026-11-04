# 权限系统实施与证据状态

## 2026-11-05：本地关键链原生实测完成，可审阅

完整受测提交 `ce3d63b0eb51aae44b347f7a08ece043ef0bdac7` 的权限及直接消费者728通过，主要运行回归881通过、1项POSIX文件模式在Windows跳过。Intel同版真实SCM/LPAC/Job/IPC/HERV3、普通Core/shared/Worker公开入口、正式人类登录批准及原任务一次续跑、36项系统级拒绝、网络正反对照、真实TTL、Workzone变更和两服务故障恢复均有独立效果及权威记录证据。仅模型推理合成，不用模型结语判通过。

删除用户profile前已核对47份Runner账本、122条retired记录，无active、scratch或测试服务/进程。两份安装身份的profile经独立Intel正常Windows维护后由原recover接口完整回收，原组件失败回执保留；这不等于即时卸载资格，也不消除共享A9_MAX上的历史profile事项。详细范围、回执与发布门见[本轮原生验收](L2_INTEL_LOCAL_QUALIFICATION_20261105.md)。

本地关键链里程碑不等于全部WAC、外部前端人工验收、Compute执行端或生产上线。以下是逐轮故障与修复历史；其“待复验”按该轮时间解释，最终同版结果以上述报告为准。源码本地提交、正常HASHI3运行采用、GitHub及生产交付继续分别核实。

## 2026-11-05：普通 Worker 配置发布缺口与原生推进

本轮在既有 HASHI3 main 修复，生产与原正常 Worker 代次未采用。完整 c7b8200b 包在 Intel 的真实服务、LPAC、IPC 与 HERV3 固定入口完成区内文件、区外拒绝、shell 拒绝和离线脚本；脚本输入/输出摘要相符，额外子进程和直连网络拒绝。随后真实原任务/Turn/operation 的一次批准续跑完成并回收；原测试报告的证据写入参数错误另以只读对账绑定，不重放业务。36 项实际 OS 越权拒绝、目录别名/硬链接、CLI 传输阻断及声明网络 origin 的正向与拒绝对照通过。真实授权到期、工作区版本变更、Safety/Runner 故障与恢复后的新任务有独立进程及权威记录证据；不以模型结语判断通过。

补验普通 Core/shared/Worker 的公开 Session API 时，启动配置原子发布丢失独立 Safety 身份的读取权，原生准入失败，任务未完成；该失败保留。现由 PAO 根配置 publication owner 按已安装 descriptor 显式加入只读 SID，各根配置写入者共用，候选空文件即设权限。原文件 ACL 不作盲目继承，实例 secrets 不因此共享；读取 revision、冲突与 committed durability 语义不变。真实 Windows 配置身份/管理两项 RED，修复及配置/迁移/分组/转移等直接消费者144通过、3项平台/条件跳过；新完整提交及普通 Worker 原生复验仍待执行。

可信 Worker 的 bytecode 缓存曾使 sealed capsule 完整性检查拒绝启动；153 个新增缓存已逐项保全，9141 原文件摘要均未变化，并重新通过原 qualifier。测试操作者启动环境禁止缓存写入，没有削弱 qualifier。原两份服务账户 profile 仍待受支持回收，全部适用 WAC、生产采用、独立 backend、Compute 服务器端与用户实际前端均不得据此冒称完成。


完整 bb8b4f80 包随后在 Intel 的独立普通 Core/shared/Worker 通过公开 Session API 到达实际 L2 模型接线，配置读取缺口消失；但原 Worker 清理模型私有上下文时遗漏必填 recovery 参数，Run 明确 failed。原生失败保留。现按原 StageProvider 合同同时清空 observer/recovery，真正恢复数据仍单独交给受限 host；L1 不走该清空分支。实际 StageProvider 两项 RED、L1对照原先通过，修复后接线/采用/真实服务回环/恢复组合22通过。另有期限测试误把系统时钟与 authority 的防回拨时钟混比，改用已有 observed_time 严格界定120秒；授权实现未改，相关55通过。完整新包及普通 Worker 后续仍须实际核实。

d46afa5d 的完整权限/直接消费者723通过，curated关键运行881通过、1项POSIX平台跳过。Intel普通Worker继续暴露审计回调把整条mapping传给具名字段接口的TypeError，固定host正确以AUDIT_PERSISTENCE_FAILURE停止，原失败保留。现只在Worker回调适配原DurableAuditLog接口，保留其canonical先写、脱敏、去重与失败传播，不碰共享审计实现或允许host本地替代。真实原owner及canonical store的两项RED、修复及host恢复/故障/实际Provider合同13通过；新完整版本普通Worker仍待实体复验。

## 2026-11-04：Intel 服务态宿主与私密诊断修复（完整包实测待完成）

本轮在既有 HASHI3 main 开发，Intel 使用独立完整 sealed capsule；未推送、合并或生产采用。原始服务组件报告和 profile 回收失败保持原状，延迟回收写独立 recovery receipt，不改写原失败。两份自有测试账户在每轮测试后禁用并撤销服务登录权，不继续创建同类账户。

实体对照发现三处离线替身未揭示的边界：Session 0 LPAC 无法初始化 USER32 依赖，LOCAL 管道名字在容器端发生映射，以及 identification token 下临时加载 UTF-16 codec 被系统拒绝。21 个真实原生进程组合测出 station 0x20020、任务私有 desktop 0x81 足够；对照中的 PID／SID／AppContainer／LPAC 认证已实际通过。对照入口有诊断注入，不能冒称正常完整包通过。

源码补上任务私有桌面与 station ACL 租约、正确的服务端管道命名及双重 DACL／low integrity、无文件加载的原生 token 解码。新增聚焦回收与并发测试覆盖其它任务仍存活、未知 ACL 变化、创建／关闭失败及 Job 未清空时保留资源。受限 Adapter 的 wire／坏工具诊断另经原 canonical owner 记录并等待 flush；新端口不能选择文件或任意记录类型，故障没有本地 fallback。L1 无 sink 的普通 Adapter 沿用原行为。

聚焦 red/green 回执保存在本轮忽略的资格证据中。完整权限／共享合同回归、新提交的完整包资格、正常 HERV3 四路及故障／撤权／全回收仍须逐项实测；当前不能标整体 L2 完成。

正常完整包随后在 Intel 实际完成区内写入、区外读取拒绝及 shell 拒绝；第四项脚本暴露进度事件缺少必填 summary 的 TypeError。原生失败与同组两项红测保留；Runner 与 Safety 转发均补齐无正文的心跳字段，22 项脚本／结果持久化／清理回归通过。脚本活动是存活信号，不代表进展或成功；通道失败仍清理并传播错误。完整包原生重测及整体验收仍待完成。

完整 f4c182e0 包在 Intel 正常固定入口的四项随后通过，实际脚本 exit0、正向输入／输出摘要相符，区外读写、额外子进程（1816）和直接网络（10013）拒绝；四个 host 桌面关闭且原 station ACL 恢复。该提交权限／直接消费者 80 文件709通过。下一项原生批准续跑确认伪造会话拒绝、重复决定复用同一 lease，但新 host 未按执行代次区分 runtime audit，原操作在审计失败前停止。现由 Runner 从自有 binding 注入 epoch，host 复用既有 audit scoping，不允许改用本地 spool 掩盖原 owner 失败。两种上下文续跑及 owner 故障 RED3／GREEN15；同 epoch 改不可变事实仍拒绝。新增修复后的完整包／原生续跑尚待验证，原 profile 阻碍及完整 WAC 不因此通过。

更新日期：2026-11-04。功能主责 PAO；执行协作 HERV3，认证及显示协作公共 Frontend Connector。后续 L1 修复在既有 HASHI3 分支进行；原 feature-permission-sys 阶段作为历史保留。Core 不变。用户本轮明确要求补齐 Workbench 自动弹窗、一键审批与时长选择，个人 L1 的按钮点击即视为用户决定；不要求反复身份验证。

本期源码已完成权限服务、真实 HERV3 接入、公共认证/审批、受控工具、媒体/原服务、计费与恢复接线。Windows 实体脚本的启动问题已经解决并通过组件资格。本轮在既有 HASHI3 main 接手；已合并基线为 HASHI PR #127 与 Workbench PR #26。本轮新增源码提交、运行版本、页面证据与生产交付分别核实；旧提交的绿色检查不能代替当前版本。

完整安装的 Windows WAC、实际前端验收及生产采用仍是独立交付事实。未完成这些组合验收的环境继续显示 partial/unknown，不显示整体 L2 已验证。此前分批结果与失败记录见 [历史](IMPLEMENTATION_HISTORY_20261030.md)；原始产品要求仍以 [PRD](PRD.md)、[ACCEPTANCE](ACCEPTANCE.md)、[Windows L2](L2_WINDOWS.md) 为准。

2026-10-31 HASHI3 L1 实测发现并修复模型工厂写入 Adapter 只读超时属性的问题，改走既有配置超时合同。真实 Adapter 回归同组测试旧代码 2 failed / 2 passed，修复后 4 passed；直接模型/超时/L0 消费组合 18 passed。本项离线修复不代表完整 L1 前端实测通过，实际采用与后续问题记入当轮证据及共享白板。

L1 后续真实审批测试证实：应用令牌不能代替人类会话，测试人类账户能批准并撤销指定 lease；但续跑时重新装配的上下文触发旧事件编号冲突。运行日志现按真实 ExecutionEpoch 区分新事件，原 Run/Turn/operation 与物理用量身份保留，同 epoch 的不可变事实检查继续拒绝冲突。接入 canonical observer 的两项私有 IPC 组合回归从 2 failed 变为 2 passed；完整权限/直接合同组合 616 passed，HERV3/最小 Core/Function 合同 24 passed；此为历史组合回归；本轮实体续跑与实际页面结果见下节。

## 2026-11-04：稳定 L1、补 L2 硬 Workzone

用户本轮批准保留 L1 行为并写清保证边界；L2 先完成本地部分，主要保证已验收的 HERV3 受控执行链。L1 的实际权限仍由基础策略和租约决定，Workzone 不自动成为硬围栏；Codex 等独立 Engine Provider 及未验收的远端执行不继承完整 L2 保证。

本地源码现按 PAO 原 Workzone writer 的精确根，分别收窄 L2 基础范围及申请上限；无有效根不回退到 home/access_root，附加根不扩成共同父目录，普通批准不能扩根。根 revision、精确根和边界协议版本进入 L2 policy digest；停用再启用不能复活旧 epoch。路径被重定向拒绝，脚本输入与文件工具共用同一范围；原生绑定/受限 host/Runner 仍承担实际隔离。L1 不采用这次硬上限或根 revision 栅栏。

聚焦集成回归通过真实安装策略、factory、registry、权威账本及临时文件效果，原生 launcher/IPC 注入。改动前两份确定源码的同组回放为 11 failed／24 passed，修复后 35 passed；覆盖 L1 对照、宽权限越界、主动请求、缺根/不可用根、附加根、多根/窄动作、脚本输入、无约束执行、实际目录重定向及 admission/queued/prepared 后根版本变化。切级检查还发现 review 使用切换前等级的范围，现改为按目标等级预览；缺根在操作前拒绝，根版本变化使审阅 revision 失效。同组该缺口 RED 3 failed，修复后切级及原聚焦组合 40 passed。最终完整权限/FYI 回归为 70 文件、647 passed（无过滤、无 skip）。关键运行加载检查要求完整已提交源码；最终提交后的回执另存当轮证据，不将提交前正确拒绝未提交源码的两项检查当成产品回归。不能把这些组合当成完整平台资格。

另补模型传输检查：HERV3 内部选用的原生 CLI 也不自动受 L2 控制。L2 执行器现要求受代理传输，在创建/初始化 Driver 前按原 registry 的 CLI 分类拒绝；L1 兼容行为和 API 模型路径保留。该接线 RED 1 failed／6 passed → 聚焦与原边界组合 47 passed。前述完整权限647与关键运行881 passed／1项POSIX平台跳过属于前一完整提交；最终补堵后的结果见当轮交付回执，不混作同一树的测试。

Compute 仅推理时，返回的工具调用继续走本地 broker；若在服务器实际执行资源操作，必须补服务器权限服务、根映射、受限执行、撤权/期限/幂等回执与独立资格，才纳入端到端 L2。本轮只写清对接合同，不部署或假定远端能力。完整 Windows 服务/WAC 仍受 profile 回收失败阻碍；不继续新增同类账户或强改系统服务。本轮运行 L1 保留既有已验收代次，生产未采用。详见 [专门决定](L2_HARD_WORKZONE.md)。

## 2026-11-04：发现状态修复与 NP 页面验收

真实 H3 测试已生成有效待批申请（期限未到、0 claim、无文件），但未加载临时安装描述的共享 API 返回正常空列表。现修复为：已配置但 frontend controller 未加载，或配置不可读时，返回明确不可用；未安装的实例保留原空视图。复用原配置读取者，身份／Session／generation 先核验，正常已绑定查询和审批权威不改变。有效用例 RED 3 failed／6 passed → 发现及精确点击 GREEN 14 passed。

当前功能提交 `cd0f9cad` 的完整权限与 FYI 回归为 **69 文件、627 passed**。先前采用因其它 Worker 忙碌被拒绝，未强制切换；空闲后通过标准 `/reboot max` 完整采用同一提交，12 个 Worker 与 shared 的源码版本一致，Core 进程和源码保持不变。

通过实际 NP Workbench（5185，配套版本 `b98778be`）对自有临时 Agent／Session 执行四种原生弹窗按钮，审批通过浏览器点击，未使用私有决定 RPC 或补造人类登录：

| 按钮 | 权威与真实效果 | 本轮结果 |
| --- | --- | --- |
| 批准一次 | 批准前无 claim／文件；原 operation 一次成功写入，once 被消费；任务终态撤权 | 通过 |
| 拒绝访问 | request=denied，无 lease、无 claim、无目标文件；原 Run 正常完成 | 通过 |
| 允许 10 分钟 | 从服务端批准提交起精确 600 秒；原 operation 一次写入，任务提前结束即撤权 | 通过 |
| 批准本任务使用 | lifetime=task，原 operation 一次写入，Task 完成即撤权 | 通过 |

页面刷新后重新打开同一 Session，未决请求仍能重新发现。本轮没有等待 600 秒自然到期，也没有逐一点击 1 小时／4 小时预设；这些期限边界的回归与本轮按钮实体结果分开。四个 Run 均 completed，无错误码；三项获批操作各一条成功 claim，拒绝项为零，全部 Task 已完成、lease 均已撤销。

有限普通服务已经 exit0／Job stopped，临时 Worker 已停止，原配置按 revision 完整恢复，两处目录 ACL 已恢复。再次等到空闲后，经标准 shared handoff 清除旧 controller 缓存；11 个正常 Worker 采用 `cd0f9cad`，未安装状态查询恢复 200 空视图，Core 保持原进程和源码。原生弹窗截图、浏览器步骤、authority／claim／lease、文件效果、两次采用及收尾回执保留在本轮忽略的证据目录。此为 Agent 通过真实 NP 页面的验收，不等于用户手工验收、全部 L1 CASE、生产采用或完整 L2 WAC。

## 2026-11-03：接手与 SCM 生命周期

HASHI PR #127、Workbench PR #26 已合并；本轮开始时 HASHI3 和 Workbench NP 都是既有 main 的干净检出。开始核对时 NP5185 已连接 HASHI3，两个运行版本与各自已合并的基线一致。正常连接范围内的原自有 L1 Session 待批查询返回 200，Workbench 发现／交互回归 7 通过；空列表不代表完成新的点击验收，生产正式验收仍单独保留。

L2 原安装入口只有前台信号循环，本轮新增 Windows SCM host，复用原 Safety／Runner 安装核验，增加明确外部停止／实际就绪合同。同组入口 RED 10 失败；入口、SCM 状态及测试注册所有权聚焦 GREEN 30 通过。原生 SCM 组件配方已执行：两名独立普通账户的四种场景按预期，实际 service SID 与禁止自管均通过；全部服务注册／进程及新增 service-logon right 已清理。但 Windows User Profile Service 仍持有两名测试用户的 hive；正常卸载／删除接口未能释放，整轮组件资格保持未通过。两账户已按 manifest／SID 核对后禁用，配置及所有权证据保留；不重启或强行操作同机系统服务。原生 SCM 试验使用独立 capsule；其源码已随本轮验证版本被 HASHI3 Functions 采用，但这不等于独立 SCM 安装已合格。2026-11-04 收尾只读复核确认四项服务注册仍不存在、两账户仍禁用且无新增 rights，两个 profile 仍 loaded。新增源码未推送或发布。完整 L2 继续未验收。详见 [SCM 组件资格](WINDOWS_SERVICE_QUALIFICATION.md)。

## 个人 L1 按钮交互修复

独立公开 HTTP 曾通过，随后真实 Workbench 页面验收失败：用户看不到审批按钮，输入正式批准命令仍被额外登录门拦截。PR #123 后台修复已合并，不能视为此页面缺口已完成。

本轮源码实现个人 L1 免重复登录的精确 action 入口、Session 待批只读发现、简短按钮优先视图和策略内短时长。governed/L2、普通问题无授权、版本/范围/期限/幂等保护仍保留。外部 Workbench 配套在独立非生产完整源包实现并交付补丁，不修改正在运行的生产目录。具体取舍见[实施决定](IMPLEMENTATION_DECISION_20261030.md#个人-l1-一键审批2026-10-31-用户修订)。

新增回归先复现“没有按钮”和缺少个人 L1 入口，再验证不登录即可准确批准一次、重复投递不增权、当前 owner/Session/L1 校验及只读发现。实际采用、页面点击和生产发布的结果另记；此段源码完成不自动表示页面通过。

## 当前实现

| 部分 | 实际行为 |
| --- | --- |
| L0 | 缺省及显式 L0 在最外层保留原执行路径，不创建权限服务、账本、租约或新工具；既有隐私与其他防护保留。 |
| 权限权威 | PAO 单一 Task/Run/Turn/epoch；服务持有决定、范围、期限、once、派生、撤回与撤权事务。旧执行先失效，未知副作用保持原 operation ID。 |
| 基础权限与申请上限 | 安装策略分别保留两种权威；基础已授权操作直接执行，缺少的 action 只在申请上限内批准。主动申请返回 granted_existing，当前策略与最终 start 仍重新校验。 |
| 人类批准 | 个人 L1 在现有认证前端点击即可决定，私有 FC 自动处理短期上下文；governed/L2 保留独立身份规则。公共 /safety 与受签 action 复用同一权威，普通回答不授权。 |
| 原运行续跑 | 现行 HERV3 singleloop 与精确 provider checkpoint；原 Run park，批准后新 epoch 恢复原待执行动作。服务故障不切到无约束执行。 |
| 文件与 HTTP | 实际 Windows 文件对象绑定，固定 helper 与最后 start 栅栏；目录、搜索、严格补丁。HTTP 固定 DNS/目标，无自动重定向、环境代理或 cookie；具名凭证独立代理，GET URL 同样声明 send。 |
| 结构化脚本 | 固定输入快照、离线 Python、临时输出。用户代码在独立 LPAC，原子纳入 Safety/helper/脚本 Job；实际进程/内存限制、独立加密结果及 profile 收尾。 |
| Python 原生环境 | 仅固定 registryRead，满足 Windows SxS 只读注册表依赖；真实 capability SID 集合必须完全匹配，没有网络 capability。系统用户目录从当前 token 获取，排除调用者秘密、代理及导入路径。 |
| 原服务与媒体 | 原 Persona、草稿、习惯、Meditation、TurnServices、压缩及 HChat 包装继续由原 owner 处理。附件只取本轮原始接受内容并核对摘要；语音确认及音频成果回到原 owner/资产 store。 |
| 物理用量 | 每次实际 HTTP 前持久化 intent，响应后记录观测；host 与 Worker 物理 ID 稳定，计费投影先持久化再调用原 observer。恢复不重发请求推测旧用量。 |
| 兼容与实际控制 | 明确选择兼容后端时先停止旧 PAO 执行，再继续 unmanaged；失联清理保留待确认记录。当前绑定、实际工具、原生观察、服务代次及期限生成控制状态，并传播依赖不足。 |
| 安装入口 | 显式独立 Functions/sidecar、封闭完整源码/依赖、分离密钥与单写锁；import/L0 不启动服务。L1 不要求独立 Runner，L2 保持独立普通账户和受限 host。 |

## 必要行为验证与范围

- 完整服务、真实私有 pipe、SDK 与现行 HERV3 的组合闭环通过：原 Run 缺权暂停，可信人类批准，新 epoch 继续同一操作，真实临时文件只写一次。确定性模型 transport 和原生文件 launcher 在此用例注入，不能据此宣称完整安装 L2。
- Windows 实体脚本组件通过：实际普通账户、LPAC、固定 registryRead、低完整性任务目录、原子 Job 和 128 MiB 可观测内存限制。原文件读/改及子进程被拒绝；TCP/UDP IPv4/IPv6 四项均被系统拒绝。输入快照、临时结果与原文件不变通过。所有 Job、profile、账户、用户配置及 staging 清理已确认。
- 实体失败路径保留证据：最初 SxS 运行库加载缺少注册表读取，固定诊断捕捉到加载错误；系统用户环境与低完整性标签的缺口也已修复。扩大只读代码 ACL 的试探无效，未保留。正式通过使用无调试器进程，不能把调试诊断当资格通过。
- 文件启动前失败已做 red/green：没有 claim、不消耗 once；初始资源关闭后保存 authority-owned 失败结果，可恢复且不伪造 started。已准备、已开始或清理未知不能走该分支。
- 附件变动/任意路径拒绝、原语音确认、TTS 权威文本、原压缩/草稿包装、真实音频资产 owner 均有直接用例。
- 共享原生命令通过真实私有服务批准；缺少身份关联、合成 API actor、PAO 面伪造登录及旧 Session 均拒绝。
- Worker 用量先持久化、跨 epoch 保留原阶段/ID；脚本原结果在 profile 清理失败时保留；旧物理行失败不跳过其他行；已完成旧快照不依赖已删除 plan。
- 新增固定 capability 后，额外网络 SID、被替换 SID、旧零 capability profile 和 bool 伪装 count 均在执行前拒绝。
- PR #118 P1 基础权限回归：同一组 18 项安装策略→factory→registry→authority 集成用例在 6756cf43 为 8 failed / 10 passed，修复后 18 passed。覆盖基础独有、申请独有、重叠、分 action、禁止范围；入场/排队/prepare 后改策略及最终 start 撤权均无文件副作用。原生 launcher/IPC 与后端配置输入为注入边界，不宣称 OS 组件资格。
- Windows CI 捕获旧恢复用例在结果核对阶段超过 1 秒 watchdog；同一提交的另一套 Windows 检查及本地组合通过。该恢复 helper 的 watchdog 调为 5 秒，仍保留授权、结果摘要、epoch 栅栏及只执行一次的全部断言；产品期限和超时不变。
- CI 范围来自 permissions workflow 的完整 permission/直接旧合同组合；共享运行接线另跑仓库规范 curated core gate。Core 保护和 whitespace 必须通过。不默认跑全库、不用 mock 宣称 OS 通过，不将重叠分组数量相加。

## 当前 profile 与采用边界

当前 profile 提供文件、结构化 HTTP/具名凭证、离线 Python 和受限 HERV3。任意 shell、依赖安装、未评定设备/浏览器/桌面、自定义远程工具不能通过服务静默逃逸；缺少的通道显示 unavailable。脚本仅收到文件输入副本，正式保存仍经文件代理。临时目录监测是软限制，不是磁盘硬配额。

混合部署中，审批桌面/凭据与未受管 Agent 的隔离没有实体证据时，审批及依赖控制保持 partial。固定 registryRead 允许平台共同注册表读取，不授予 registry write、网络或其他账户的受保护对象；业务/审批密钥仍由不同服务身份及私有文件/通道隔离。

| 交付事实 | 核对位置 / 状态 |
| --- | --- |
| 本地源码与测试 | HASHI3 既有 main；SCM/L1 收尾与本轮 L2 硬 Workzone、目标等级审阅、回归和说明按完整提交交付，见 Git 日志及当轮回执 |
| GitHub 收到与 PR 可审阅 | 已合并基线 HASHI #127／Workbench #26；本轮新增提交未推送，无新增 PR |
| Windows 脚本入口/launcher 组件 | 已通过；证据保留实际 SID、OS、Python 版本、逐文件摘要和清理结果 |
| 完整独立安装/WAC | SCM 原生四场景行为按预期，但 profile 回收未过；完整 L2 仍保持发布门 |
| 生产源码同步 | 未进行 |
| 运行服务/Worker/页面采用 | HASHI3 Functions 保留已验证 cd0f9cad；NP 四按钮实测及恢复证据保留。本轮 L2 硬根源码尚未被运行服务采用，不能以本地提交或离线检查冒充部署 |
| 用户手工前端验收 | 未进行；本轮实际浏览器操作由 Agent 完成 |

本轮只在指定 HASHI3 范围实际采用 Functions，没有冷重启 Core、安装生产持久服务或修改生产源码。一次性资格进程和测试 IPC 不等于完整 L2 安装合格。详细取舍见 [当前实施决定](IMPLEMENTATION_DECISION_20261030.md)，源码关系见 [CODE_MAP](CODE_MAP.md#map-11--当前实现与边界2026-10-30)。
