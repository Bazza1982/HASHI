# HASHI 权限系统：设计与实施包

**版本 1.1 · 2026-10-23 · 文档设计完成，功能待实现。**

目标是在保留自主工作体验的前提下，为 HASHI 原生 HERV3 增加可申请、可撤销、随任务回收的权限。L1 在 HASHI Functions 中管理；L2 使用同机独立安全服务和受限 HERV3 执行环境。L0 继续现有执行路径。第三方后端按真实控制能力说明并继续用户选择的工作。

本包按用户最新决定更新：**仅 HASHI 改动、复用现有 Frontend Connector（FC）标准、外部 Workbench 与 Telegram 专属适配不改、protected Core 不改。** 当前分支只提交文档，未实现权限功能、安装服务或重启实例。

| 项目 | 内容 |
| --- | --- |
| 仓库 / 分支 | `Bazza1982/HASHI` / `feature-permission-sys` |
| 源码参考基线 | `d6b6513973bfc8f9f22c40ddd8f72802f907baa0`，2026-10-23 再次核对 main |
| 功能 owner | PAO；HERV3 负责等待/续跑，FC 负责标准投影与可信入口，PCM 只投影 |
| 工程层 | Functions、HASHI 内的平台安装单元与独立服务 |
| 首个完整 L2 平台 | Windows 原生环境，不以 WSL 为前提 |
| 当前事实 | 见 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md)，不能将设计要求当作已上线能力 |

## 1. 文档导航与阅读顺序

| 文件 | 用途 |
| --- | --- |
| [PRD.md](PRD.md) | 完整产品需求，替代 v1.0；定义 L0/L1/L2、任务/授权、UX、生命周期和成功标准 |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | M00–M10 施工顺序；模块、数据表、事务、算法、调用位置、失败恢复与退出条件 |
| [CODE_MAP.md](CODE_MAP.md) | 固定提交的真实源码落点；已有能力、缺口、Functions/PAO 归属与现有测试 |
| [FC_CONTRACT.md](FC_CONTRACT.md) | 现有命令/action/文本如何承载批准；可信身份、服务器 receipt、实际 JSON 和通道差异 |
| [L2_WINDOWS.md](L2_WINDOWS.md) | 独立服务、ACL/身份、受限 host、窄 broker、启动/撤权、W01–W12 平台施工及 WAC 验收 |
| [ACCEPTANCE.md](ACCEPTANCE.md) | L0/L1/FC/生命周期/L2/运维的可观察用例、失败条件和证据模板 |
| [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) | 文档、实现、测试、运行采用分别记账；开发阶段持续更新 |
| [LOCAL_AGENT_PROMPT.md](LOCAL_AGENT_PROMPT.md) | 可直接给本地开发 agent 的一次性交接指令 |

产品行为由 PRD 定义；代码事实以固定 SHA 和 CODE_MAP 为准；详细施工需同时读 FC 与 Windows 文档。若出现文字冲突，先遵守用户边界及 PRD，依据代码证据修正文档，不能悄悄修改产品承诺或删除验收来迎合实现。

## 2. 本次确定的设计

### 2.1 L0：没有接入新执行链

缺少安全配置或用户选择 L0 时，保留原 prompt、工具 schema、执行路径、现有规则、超时/重试与生命周期。新权限服务、数据库、票据与 runner 不参与；这需要和冻结基线做行为比较，不能用一个永远返回 allowed 的新服务替代。

用户主动 `/safety` 可以查看和选择等级，但普通 L0 任务不被插入新安全提示。L2 安装不能改变 L0 的全局文件权限、环境或网络设置；混合部署存在审批信任冲突时隔离 L2 控制平面或诚实报告 partial。

### 2.2 L1：一次准确批准，原步骤继续

审批在服务端是结构化 AccessRequest / ApprovalDecision / PermissionLease；用户仍通过现有 FC 消息、按钮或完整文本命令操作。请求说明代理、任务、用途、资源、动作、期限和边界。有效批准内直接执行；新增范围再询问；任务提前结束即撤权。

普通 `ask_user` 只做澄清，回答“好”不会变成授权。正式批准走可信 `/safety` 命令/action；有能力的客户端显示已有按钮，无按钮时显示可复制命令，不新增公开 FC schema。

### 2.3 L2：增加独立执行边界

现有可信 PAO / FC Functions 宿主继续持有对话、Run 和 Task。受限 HERV3 host 执行模型循环与工具请求；独立 Windows 服务持有权限账本、凭据与窄 broker。受限 host 不取得旧的宽 Functions RPC pipe，也不能操作人类审批凭据或服务本身。

仅将账本搬出目录、仅增加 daemon，或仅把 shell 放进 sandbox 均不满足完整 L2。实际发布需验证模型 host、文件、网络、进程、凭证、IPC、审批来源和资源回收；未验证的通道如实标明。

### 2.4 用户后端选择继续生效

Codex 等后端自己的原生规则由其自身决定。HASHI 管理它确实拥有的工具路径，说明其他路径的实际覆盖。用户选择或既定 fallback 的后端可以继续；权限拒绝本身不触发自动换后端逃避。隐私和数据去向规则保持各自原有语义。

## 3. 相比 v1.0 的关键修订

| 修订 | 影响 |
| --- | --- |
| 移除专用 Workbench 权限面板要求 | 只生成现有标准 FC 命令、菜单、消息与结果 |
| 固定 protected Core 零改动 | 新共享状态在可替换 Functions；L2 使用新 host/sidecar，原 Core envelope 不变 |
| L0 真旁路与默认无迁移 | 明确没有新服务依赖、工具/prompt污染或隐式 ACL/环境改变 |
| Run 与 ExecutionEpoch 分离 | 一个 Message 的 Run 不等于进程；重启不自动结束 Task 或延长批准 |
| 排队之后再 claim/start | 消除 admission 后排队期间撤权/过期被忽略的问题 |
| 等待可 park 并热采用 | 批准不重发消息、不重做已完成动作，Functions quiesce 不被永久等待卡住 |
| 可信决定与实际入口能力 | `owner_id`/localhost/实例 HMAC 不自动证明人类；TUI 等缺口如实列出 |
| L2 明确完整主拓扑 | 可信控制平面、受限 HERV3 host、独立服务与窄 broker，补 OS 实施包 |
| 撤权与停止完成分离 | 先禁止新执行，再清理已开资源；unknown 不假装成功 |

## 4. 术语、接口与安全边界

- **FC**：Frontend Connector；与模型 function-calling schema 不同。
- **AccessRequest**：文档中 PermissionRequest 的同义名称；实现只建立一个请求对象。
- **PermissionLease**：有效 Grant/Lease；ApprovalDecision 是不可静默改写的决定依据。
- **PermissionTask**：可跨 Run 的用户目标及授权归属；不是每 Turn 创建的 HERTaskState。
- **ExecutionEpoch**：可执行实例绑定代次；与 run_id、worker_generation、context_generation 独立。
- **DecisionReceipt**：服务端私有的一次决定证明；不是客户端公开字段，也不是执行票据。
- **权限撤销**：新动作不再可启动；**资源停止完成**是另一个需要确认的事实。

普通授权只有 Once、Run、Task、明确时长。用户主动维护基础工作范围是独立长期设置，不允许把临时批准悄悄保存成永久权限。

## 5. 研发与交付路径

先实施 M00–M06 形成可用 L1，再完成 M07–M09 的独立 L2 边界，按实际发布级别执行 M10。交互、执行拦截和平台 spike 可依据接口并行开展只读研究；实际源码、测试和文档修改保持单一写入者，不能用桩实现跳过最终闭环。

本地 agent 应先读取交接指令与整个设计包，按里程碑持续实现、测试、记录证据；不把“写了计划”当成本地开发任务的完成。当前分支只为下一阶段提供设计基线，尚无权限功能的行为测试通过记录。

## 6. 源码与来源

代码事实均有固定提交引用，集中在 CODE_MAP、FC_CONTRACT 和 L2_WINDOWS。Windows 隔离机制引用 Microsoft 官方文档。数字时限和性能阈值是本项目设计目标；发布前必须实际测量，不属于当前性能结论。

仓库 AGENTS、分层边界与测试政策继续适用。受保护路径只从 `orchestrator.runtime_contract.CORE_SOURCE_PATHS` 推导；本目录不维护第二份名单。
