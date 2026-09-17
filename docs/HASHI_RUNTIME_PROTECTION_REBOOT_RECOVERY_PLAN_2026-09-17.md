# HASHI 运行时保护、`/reboot` 可靠性与恢复治理计划

状态：**已确认的规划边界；实现进行中**

决策日期：2026-09-17

主要负责人：PAO（发布、运行代际、恢复与工程治理）

协同负责人：Frontend Connector（命令和回执展示）、PCM（Agent FYI）、平台配置（ACL 与服务配置）

工程层：文档、Functions、平台配置和实例配置；**不修改受保护 Core 源码**

## 1. 本文的权威范围

本文固定本轮重大修复的目标、禁止事项、实施顺序和验收边界，防止后续执行时范围漂移。

这里的“重大修复”描述问题的重要性和工作规模，**不等于**授权 Core 主版本迁移。受保护 Core
路径仍只由 `orchestrator.runtime_contract.CORE_SOURCE_PATHS` 定义；当前没有修改、移动、重命名
或删除其中任何文件的授权。如果某一步只能通过修改受保护 Core 完成，必须停止该步，另行申请明确的
Core 主版本迁移授权，不能把本文当作授权替代品。

本文记录的是已确认的目标设计，不代表当前运行实例已经采用。源码实现、测试合格、制品生成、GitHub
发布和生产实例采用必须分别记录。

若后续建议与本文冲突，先保持本文边界并请用户作产品决定；不得由实现者自行扩大保护、增加 HER v2
限制、增加 `/restart` 人工证明，或降低 Agent 的正常开发能力。

## 2. 要解决的问题

当前问题不是“保护越多越好”，而是保护位置不够精确：

- Git 规则可以阻止误提交，却不能阻止运行中的 Agent 或工具修改 live Core、live Core 解释器、服务配置或密钥。
- 过宽的依赖摘要、dirty-tree 判断和运行保护会把无关包、无关文档或实例状态误判为 `/reboot` 风险，导致正常 Function 更新经常失败。
- `/reboot` 的“命令已受理”“候选合格”“路由已切换”和“新 Worker 最终在线”没有始终清楚分开，容易产生错误诊断。
- HASHI1、HASHI2、HASHI3、HASHI4 和 GitHub 已经出现不同的已提交与未提交工作；继续盲目同步会覆盖证据或混合不相关功能。
- 旧计划曾建议给 HER v2 增加 token、时间、工具循环限制和自动压缩，也曾建议给 `/restart` 增加真人一次性证明与双重确认；这些建议已被用户明确否决。

本计划的核心原则是：**精确保护 live runtime，同时保持 Agent 在自己的 workspace/workzone 中正常工作。**

## 3. 已确认且不可自行改变的决定

### 3.1 必须做

1. 先保存四个实例的独立证据，再考虑同步：
   - HASHI1：`/reboot` 补丁；
   - HASHI2：缓存补丁；
   - HASHI3：Agent 创建功能；
   - HASHI4：未提交的 Telegram 功能。
2. 每项工作建立独立分支和独立 GitHub Issue；不得把 H3 Agent 创建或 H4 Telegram 混入 `/reboot` 修复。
3. 从执行时最新的 GitHub `main` 建修复分支，先恢复可信基线。
4. 精简 Agent FYI，使其回到现有预算内；不得用简单提高上限掩盖问题。
5. 清理 source-publication 中的实验文件、本机路径和不应公开的运行证据。
6. 精确保护 live Core、live Core 解释器／虚拟环境、服务配置和重启密钥。
7. `/reboot` 只采用已提交、经过聚焦验证、内容寻址且不可变的 Function 制品。
8. 标准锁定依赖发生变化必须阻断；无关额外包不得阻断 `/reboot`。
9. `/restart` 保留现有可信网络、Remote/WatchTower 与 `L3_RESTART` 能力边界，并在完成后验证新 PID、健康状态和版本／代际。
10. 可记录文件写入、后台任务、配置／服务变化及 Provider request/response ID，供用户查询。

### 3.2 明确不做

- 不强制重置、覆盖或清理任何实例的现有工作树。
- 在合格候选出现前，不在生产实例反复试 `/reboot`。
- 不修改受保护 Core 源码，不以“保护 Core”为由申请或暗示 Core 主版本迁移。
- 不把所有 Agent Worker 一刀切迁移到会破坏凭据、CLI 或开发能力的新受限账户。
- 不禁止 workspace/workzone、开发分支、开发虚拟环境或 Function sidecar 内的正常写入和 `pip`／`uv`。
- 不允许 Agent 直接修改 live Core 环境、向 live Core 解释器安装包、改服务定义或读取重启密钥。
- 不给 HER v2 增加 token、时间、工具数、上下文或工具循环上限。
- 不给 HER v2 增加自动 checkpoint、自动压缩或新的确定性失败重试政策。
- 不改变 HER v2 当前 shell 只读／副作用判断、执行循环或普通失败答复。
- 不大改 HER v2；本轮仅允许低风险、fail-open 的诊断日志投影。
- 不改当前已正常工作的 Antigravity（AGY）路径。
- 不给 `/restart` 增加真人一次性证明、nonce、防重放证明或本机／远端双重确认。
- 不要求每次普通 Function PR 都跑全量 Linux、Windows、Nagare 和发布资格套件。
- 不把每个 dirty 文件都作为 `/reboot` 阻断条件。
- 不把 Function 更新升级成 Core 冷重启。

## 4. 负责人和工程边界

| 工作 | 功能负责人 | 工程位置 | 不得放入 |
|---|---|---|---|
| 证据冻结、候选资格、制品、回滚、回执 | PAO | Functions、工程脚本、文档 | Core |
| `/reboot`、`/restart` 命令和用户可见状态 | Frontend Connector | Functions、语言目录 | Core |
| live 路径 ACL、服务配置、部署身份 | PAO／平台 | 平台配置、安装与部署脚本 | Agent 业务逻辑 |
| Agent FYI 精简 | PCM | PCM Functions／配置 | Core、HER 执行循环 |
| 副作用与 Provider ID 的可查询日志 | HER v2 诊断投影／工具审计 | 既有审计或旁路投影 | HER v2 关键执行路径 |
| Gemini CLI 退役 | PAO／Backend 配置 | Functions、平台／实例配置、UI | Core |

任何实现前都要重新声明 owner、工程层和聚焦验证。正常功能必须放入最窄的现有 owner，不复制模型、命令、端口、状态写入或配置事实源。

## 5. 阶段计划

### 阶段 1：冻结证据，不再盲目同步

目标：把四个实例的工作变成可复核、可恢复、互不覆盖的证据。

对每个实例分别记录：

- 当前 HEAD、上游、工作树状态和相关文件清单；
- 已提交范围和未提交 diff；
- 未跟踪文件的私有归档及摘要；
- 当前运行代际与源码状态的区别；
- 已执行的验证、失败和未验证项；
- 是否含密钥、本机路径、聊天内容或其他不得上传的材料。

保存方式：

- 已提交工作建立精确分支引用；
- 未提交工作先生成私有、带摘要的证据包，再在独立干净分支中审查性重建；
- GitHub Issue 只放安全摘要和验收条件，不上传秘密或原始运行状态；
- 分支建议分别使用 H1 reboot、H2 cache、H3 agent-create、H4 telegram 的独立名称；
- 不切换或重置正在承载未提交工作的生产 checkout。

退出条件：四项均有独立分支、Issue、来源记录和恢复方式；任何一项都没有被另一个实例或 `main` 覆盖。

### 阶段 2：恢复可信 GitHub 基线

目标：从最新 `main` 建立最小、可审查、无本机污染的修复线。

工作内容：

1. 在隔离 worktree 中从最新 `origin/main` 建修复分支。
2. 修复当前 baseline 的失败，但不借机合入 H3 Agent 创建或 H4 Telegram。
3. 精简 Agent FYI：保留权限、当前请求、必要上下文和来源信息；删除重复、过期或可按需读取的内容；保持现有预算，不提高上限。
4. 清理 source-publication：实验文件和运行证据转入忽略的私有证据区；文档中的本机路径改成通用示例；扫描精确待发布 commit 范围。
5. 只启用最小 GitHub 门禁：
   - `main` 只通过 PR 合并，禁止日常直接推送；
   - 必需检查只保留快速 architecture/boundary 检查和本次 owner 的聚焦测试；
   - 只有改动触及 `CORE_SOURCE_PATHS` 时，才要求 Core 主版本、`core-change-approved` 标签和独立 Core review；
   - 普通 Functions PR 不进入 Core 审查流程；
   - 全量资格套件只在发布候选阶段运行一次，不在每个小提交重复运行。

这些 GitHub 门禁不进入 HASHI 消息、工具或模型运行路径，因此不影响生产运行性能。成本只是在 PR 合并时多等待必要检查；路径过滤和条件门禁用于避免无关检查拖慢开发。

退出条件：最新 `main` 的最小必需检查为绿；发布范围没有实验文件、本机路径或秘密；H3/H4 功能仍在各自分支。

### 阶段 3：精确保护 live runtime，并修复 `/reboot`

#### 3A. 区分开发区和 live runtime

目标布局：

- Agent 自己的 workspace/workzone：可读写，可使用 Git、测试、开发虚拟环境和被允许的开发工具；
- 开发源码 checkout：按任务授权读写，但不是运行制品；
- live Core 源码／制品：运行时只读；
- live Core 解释器／虚拟环境：运行时只读，禁止 Agent 安装或升级依赖；
- live 服务配置和重启密钥：只由受信部署／服务边界管理，Agent 工具不可直接读取或修改；
- Function 的可选或原生依赖：放在隔离 sidecar／独立环境，不进入 Core 解释器。

ACL 必须只覆盖明确的 live 目标，不能覆盖整个仓库、用户目录、workspace、workzone 或所有 Python 环境。Windows 与 Linux/WSL 分别使用本机 ACL；先只读盘点实际运行账户和路径，再生成显式目标清单，不根据文件夹名称猜身份。

不把所有 Worker 直接改成新受限账户。AGY 曾证明服务身份可能读不到交互用户凭据。优先方案是：让 live 部署目标与 Agent 开发目标分离，并让工具层对精确 live 目标 fail-closed；若某个平台上服务与工具实际共享同一 OS 身份，不能虚假宣称 ACL 已完成。该平台必须先设计不会破坏 Agent 凭据和工作能力的身份／工具执行边界，并在可销毁实例验证后才能采用。

工具层规则只拒绝：

- 对权威 protected Core 路径和明确 live runtime 路径的写、移动、删除和覆盖；
- 指向 live Core 解释器／虚拟环境的 `pip`、`uv` 或等效安装／同步；
- 直接修改服务定义、启动环境和受保护密钥；
- 绕过受支持控制接口的原始服务控制命令。

以下必须继续允许：

- 在 Agent 自己 workspace/workzone 中编辑、构建和测试；
- 在独立开发虚拟环境、构建环境或 Function sidecar 中使用 `pip`／`uv`；
- 通过受支持、已鉴权的 `/restart` 路径请求 WatchTower／Remote 执行重启；
- 只读诊断 live 状态和受控日志。

#### 3B. 精确依赖门禁

`/reboot` 资格检查只比较标准 lock 所声明的运行依赖投影：包名、锁定版本、平台条件和必要 ABI。规则如下：

| 情况 | `/reboot` 结果 |
|---|---|
| 标准 lock 中的包缺失 | 阻断 |
| 标准 lock 中的版本／平台条件不符 | 阻断 |
| Python、Core API、Function API、Worker protocol 或 ABI 不符 | 阻断 |
| protected Core 摘要不符 | 阻断 |
| 仅存在标准 lock 未声明、且不进入 live Function 闭包的额外包 | 记录诊断，不阻断 |
| Function 的可选依赖位于合格 sidecar | 按该 sidecar 合同验证，不污染 Core |

不能再用整个 `pip freeze` 或“所有已安装包集合”的摘要差异作为普通 `/reboot` 阻断理由。若现有 protected Core 合同无法在不改 Core 的前提下表达该投影，先在 PAO Function 资格层建立兼容检查；如果仍做不到，停止并另行报告，不得偷改 Core。

#### 3C. 不可变 Function 制品

“已提交、已验真的不可变 Function 制品”具体指：

1. Function 变更属于一个连贯的本地 Git commit；该 commit 可以尚未推送或合并 GitHub。
2. 根据权威 Function generation manifest 只收集该代际实际需要的文件。
3. manifest 范围内存在 staged、unstaged 或未跟踪差异时拒绝构建；范围外的无关文档、配置或其他项目 dirty 状态不阻断。
4. 对收集内容生成内容摘要，并保存构建来源、commit、manifest、运行合同和聚焦测试结果。
5. 在隔离进程完成现有真实 import/READY probe；失败则丢弃候选，live Worker 不受影响。
6. 候选 Worker 只从固定快照启动，不从继续变化的工作树导入代码。
7. 已启动代际不可被后续文件编辑改变；下一次修改必须形成新的 commit 和新摘要。

这不是要求每次 `/reboot` 都先开 PR、等待全量 CI 或发布正式 Release。它只要求本地候选自身是连贯、可复现、经过聚焦验证的固定快照。

#### 3D. `/reboot` 回执

回执至少明确区分：

| 状态 | 含义 |
|---|---|
| 已受理 `accepted` | 请求和目标已持久化；尚未证明候选合格或切换成功 |
| 候选拒绝 `candidate_rejected` | 资格、依赖、dirty scope、probe 或 READY 失败；live 路由未切换 |
| 已提交 `committed` | 原子路由切换已发生；仍需最终健康验证，不能提前显示“在线” |
| 已回滚 `rolled_back` | 新候选未能完成，旧 Worker 已恢复并通过健康检查 |
| 最终在线 `online` | 新 PID、目标 Agent、Function 代际／版本、ACTIVE、accepting 和健康检查均匹配 |
| 未确认 `unconfirmed` | 观察被中断，不能把未知结果写成成功或失败 |

通知发送失败不得重跑 `/reboot`，也不得改写已经持久化的生命周期结果。

退出条件：可销毁实例证明 live Core 写入和 live 依赖安装会被拒绝；workspace 开发仍正常；额外无关包不阻断；锁定依赖变化会阻断；manifest 内 dirty 会阻断、manifest 外无关 dirty 不阻断；成功和回滚回执均包含真实 PID、健康与代际证据。

### 阶段 4：保持 `/restart` 简单可用

本阶段修正旧设计中已被否决的复杂授权，不引入新的人工流程：

- Agent 可以通过受支持的 `/restart` 命令请求重启。
- 保留现有 Remote/WatchTower 网络认证、可信 LAN/Tailscale 边界、目标实例绑定和 `L3_RESTART` 能力检查。
- 共享机器凭据只用于已定义的控制协议，不能变成任意 shell、服务配置或密钥读取权限。
- 不新增真人 HMAC proof、nonce、一次性证明或重放数据库。
- 不要求本机和远端分别二次确认；不得叠加新的确认层。若当前前端已有一次危险操作确认，维持现状，另行评审后再决定是否简化。
- 每次请求记录发起 Agent、目标实例、原因、开始时间、旧 PID、结果和失败阶段。
- 成功回执必须证明旧 PID 已退出、新 PID 不同、Backend API 健康、实例身份正确、版本／代际符合预期；“启动命令已返回”不等于成功。

本文在以上范围内取代 `HASHI_REMOTE_RESCUE_PROTOCOL.md` 中关于强制 `human_restart_proof` 和跨入口双重确认的未来目标；其余固定端点、`L3_RESTART`、审计和健康检查边界继续保留。代码和旧文档在实现 PR 合并前仍代表当前实现事实，不能提前宣称已经采用新规则。

退出条件：授权 Agent 可稳定使用支持的命令路径；未增加人工证明或第二确认；错误目标、错误实例身份和不健康的新进程均不能得到成功回执。

实现检查点（2026-09-17）：Functions 源码已移除额外真人 proof／nonce 与“仅真人”硬编码，保留 Remote 网络鉴权、目标绑定和 `L3_RESTART`；已增加持久 restart record、旧／新 PID、Backend API、实例身份、运行时版本与 Function 代际的终态验证。通知为 fail-open 旁路，不改变回执事实。聚焦 Windows 测试已通过；HASHI2／HASHI3 的实际受控重启仍须在候选同步后分别验收，因此尚未宣称 live adoption 完成。

Windows 服务型实例的补充边界：Remote 只读取本地 live-runtime policy 中与受控实例 ID 精确匹配的服务名，并只通过窄范围服务脚本执行停止／启动；部署工具只给 Remote 运行身份授予该服务的 start/stop 权限。没有明确服务目标的开发型实例继续使用非服务 launcher。服务命令返回或服务显示 `Running` 均不构成成功，仍以新 Core PID、健康、身份、版本和 Function 代际的终态回执为准。

H3 实测补充（2026-09-18）：Windows PowerShell 服务脚本若使用 `DETACHED_PROCESS`，可能只留下 launcher PID 而未执行服务控制；服务型路径必须改用无窗口、非 detached 子进程，并给正常的服务停止、启动和产品 readiness 留出独立验证窗口。该窗口只约束一次 restart 的健康确认，不属于 HER v2 的 turn、token、工具或上下文预算。

H3 的连续现场样本从请求到最终 ready 约需 133–135 秒，120 秒会把随后正常上线的实例过早记为失败。终态验证窗口因此设为 240 秒，客户端请求为 270 秒并在 ready 后立即返回；不得以放宽 PID、身份、版本、Function 代际或健康条件来换取较快的成功回执。

H3 最终验收又暴露了一个独立活性问题：Remote 在 `/restart` 请求内同步轮询 Workbench 时会占用自身请求循环，导致新 Core 启动阶段无法查询同一个 Remote 的 `/health`，进而把健康的独立 Remote 误记为不可用。修复要求把阻塞健康探测移出 Remote 请求循环，保持 `/health` 可并发响应；终态 PID、身份、运行时、Function 代际和 Backend 健康标准保持不变。失败回执不得因新 PID 已出现而改写为成功。

H2 最终验收同时确认 Linux Remote systemd unit 的默认 `KillMode=control-group` 会在重载 Remote 时误杀由其救援入口启动、仍处于同一 cgroup 的 HASHI Core。Remote supervisor 必须只管理 Remote 主进程；Core 的停止与重启继续只由显式 HASHI 控制端点及终态回执负责。该边界不得让 Remote reload 隐式变成 Core restart。

### 阶段 5：只增强诊断证据，不大改 HER v2

允许的唯一新增范围是可查询日志：

- 文件写入和写入目标；
- 后台任务启动、完成和失败；
- 配置和服务状态变化；
- Provider request ID、response ID 和已有 wire evidence 引用；
- 最终任务状态、已知副作用和是否存在安全重试证据。

实现优先复用现有 tool ledger、audit event 和 BackgroundJob receipt，建立只读诊断投影；不在 HER v2 主执行循环复制状态机。日志写入不得成为任务成功的同步前置条件；投影失败时保留原执行结果并记录本地警告，避免“为了报告失败而让正常任务失败”。

本批次不改变普通用户失败答复。待日志证据稳定后，再单独评审是否给失败界面增加三项简短摘要：任务是否完成、已知副作用、能否安全重试。没有完整证据时必须显示“未知”，不能猜测。

以下提案取消，不进入 backlog：HER token／时间／工具预算、自动 checkpoint、自动压缩、shell 重新分类、工具循环改写和新的自动重试禁令。

实现检查点（2026-09-17）：已增加按 request ID 查询的只读诊断投影，复用现有 Tool audit／Smart Tool ledger 与 BackgroundJob receipt；普通 Tool 审计补齐 request ID，后台任务保留状态变化历史，终态旁路投影记录 Provider request/response ID、wire evidence、已知副作用和“安全重试证据存在／缺失／未知”。投影异步且 fail-open，不改变 HER v2 主循环、预算、压缩、shell 判定、普通失败答复或重试行为。

### 阶段 6：剩余功能分开处理

在首批重大修复稳定后，剩余项目按 owner 分支和 Issue 处理，不打包进保护／reboot PR：

1. HER Turn 所有权、WIP 和 `/stop`；
2. 流中断与副作用对账；
3. 入站和出站媒体；
4. Workbench 状态、路径与时长；
5. Remote 发现及 Move 历史最终验收；
6. Gemini CLI 退役。

Gemini CLI 退役采用渐进方式：先从新配置和新选择界面隐藏；已有配置返回明确“已退役”提示，不静默切换到别的 backend；完成实例配置审计后再删除适配器和依赖。AGY 当前可用，本计划不修改 AGY；只在独立验收中确认其已提交来源和实际采用代际。

实现检查点（2026-09-18）：新建配置、Onboarding、TUI／Telegram／Workbench 选择、Gateway 模型目录和 Wrapper／Audit 模型选择均不再提供 Gemini CLI；旧配置仍可读取，但启动、直接命令、回调和 Adapter 初始化都会返回明确“已退役”，且不会自动降级到其他后端。实例只读审计确认 H1／H2／H4 分别仍有 15／8／20 条允许列表引用，H3 为 0，四个实例均没有把 Gemini CLI 设为当前 active backend。因此本阶段保留 Adapter、技能和打包入口，待所有引用迁移并留下验收证据后再删除。AGY 注册表、模型和启动路径未修改。

### 阶段 7：按风险执行资格验证与同步

验证不是越多越安全。每个检查只在其能证明当前风险时启用：

| 检查 | 何时运行 | 不运行的情况 |
|---|---|---|
| owner 聚焦测试、Core 保护检查、`git diff --check` | 每个相关 PR | 无 |
| 快速 architecture/boundary | 每个 PR 的必需检查 | 不扩成全量产品套件 |
| Linux UTC canonical suite 零失败 | 完整发布候选 | 每个小提交不重复 |
| Windows 合同测试 | Windows、ACL、服务、restart 或跨平台路径有变化 | 纯 Linux/文档且无相关代码 |
| Nagare 套件 | 改动 Nagare 或其 HASHI adapter | 未触及 Nagare |
| source-publication 全检查 | 发布候选和精确外发范围 | 普通本地开发提交 |
| live Core 写入／安装拒绝 | 安全边界实现后，在可销毁实例 | 普通 UI/文档变化 |
| `/reboot` 额外包与锁变化矩阵 | reboot／依赖资格变化 | 无关功能 |
| `/restart` 新 PID、健康和版本 | restart／WatchTower 变化 | 普通 Function 变化 |
| 可销毁实例实测 | reboot、restart、ACL、安全边界 | 每个普通 Function PR |

明确删除“一次性 restart proof 重放拒绝”测试，因为该证明机制不再属于设计。

推广顺序：先在可销毁实例验收，再同步 HASHI1、HASHI2、HASHI3，HASHI4 最后。每一步保留上一代不可变制品和回滚路径；前一实例没有稳定观察结果时不继续。Function 采用只使用 `/reboot`，不把 Core 冷重启当作正常采用或失败恢复手段。

## 6. 首个正式修复批次

第一批只包含以下五项：

1. 冻结并保存 H1 reboot、H2 cache、H3 Agent 创建、H4 Telegram 四项证据；
2. 让 GitHub baseline 转绿，并启用最小、条件式 PR 门禁；
3. 修复 `/reboot` 的标准锁依赖投影，使无关额外包不再误阻断；
4. 精确保护 live Core／live 解释器，同时保持 workspace/workzone 和开发环境可写；
5. 采用连贯本地 commit 的不可变 Function 快照，并提供真实终态回执。

不在首批中加入 H3/H4 功能评审、HER 行为变化、AGY 改动、Gemini 删除、媒体、Workbench、Remote/Move 收尾或新的 restart 人工授权。

## 7. 性能与新失败模式控制

- PR 门禁只影响合并等待，不进入生产请求路径。
- ACL 使用操作系统原生权限，不做每条消息的全仓扫描。
- manifest 摘要、依赖投影和 isolated probe 只在构建／`/reboot` 时运行，不在普通 Agent turn 运行。
- 回执复用现有持久化边界，不让通知发送决定生命周期结果。
- 诊断日志走既有审计／旁路投影并 fail-open，不成为 HER 成功条件。
- 路径和 dirty 检查只看权威 manifest 与明确 live 目标，避免全仓误伤。
- 条件测试按触及范围启动，避免把无关平台、Nagare 或发布检查变成日常开发故障源。
- 任何“安全增强”若使 Agent 无法在自己的 workzone 写文件、运行测试、使用已批准 backend 或调用支持的 restart，视为回归，不能推广。

## 8. 完成定义

只有同时满足以下条件，才能称本重大修复完成：

- 四个实例差异均已保存、分支化、Issue 化，没有证据丢失；
- 最新 GitHub baseline 及最小必需检查为绿；
- source-publication 无实验文件、秘密或本机路径；
- 受保护 Core 文件零改动；
- Agent 对精确 live Core 和 live 解释器的写入／安装在可销毁实例中被真实拒绝；
- Agent 在自己的 workspace/workzone 中仍可正常开发、测试并使用已批准 backend；
- 无关额外包不阻断 `/reboot`，标准锁变化会阻断；
- manifest 范围内 dirty 候选被拒绝，范围外无关 dirty 不被误伤；
- `/reboot` 只启动内容寻址的固定 Function 快照；
- 回执能区分受理、候选拒绝、提交、回滚、未确认和最终在线；
- `/restart` 不要求新增真人 proof 或双重确认，并以新 PID、健康和版本作为成功证据；
- Linux UTC 候选、相关 Windows 合同、Architecture 及所有被触及 owner 的检查通过；
- 可销毁实例验收后按 H1、H2、H3、H4 顺序采用，全程不以 Core 冷重启替代 Function `/reboot`。

## 9. 参考与冲突说明

- [HASHI System Architecture](../ARCHITECTURE.md)
- [Layered Runtime Boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md)
- [Testing Policy](TESTING_POLICY.md)
- [Core Protection Hardening](HASHI_CORE_PROTECTION_HARDENING_2026-09-13.md)
- [Core Runtime and Function Worker Contract](HASHI_PYTHON_RUNTIME_COMPATIBILITY.md)
- [Agent Reboot Receipts](HASHI_REBOOT_RECEIPTS.md)
- [Remote Rescue Protocol](HASHI_REMOTE_RESCUE_PROTOCOL.md)
- [Command/UI Style Guide](HASHI_COMMAND_UI_STYLE_GUIDE.md)
- [Release Checklist](RELEASE_CHECKLIST.md)

这些文档中未与本文明确冲突的架构、所有权、认证、审计、回滚和测试规则继续有效。本文只冻结本次讨论已改变的范围；在实现合并前，旧文档和当前代码仍需如实标注为“当前实现”，本文则是“已批准目标”。
