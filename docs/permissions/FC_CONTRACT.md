# 权限系统 FC 交互与可信批准施工契约

- **状态：实施中。** 当前有未接线的 DecisionReceipt、签名校验和账本基础；权限命令、可信签发与端到端服务尚未实现，本文不是上线证明。
- **日期：2026-10-23。**
- **源码基线：`d6b6513973bfc8f9f22c40ddd8f72802f907baa0`。**
- **功能所有者：PAO。** 权限请求、正式决定、租约、任务归属与执行恢复由 PAO 负责；共享 Frontend Connector（FC）Functions 负责认证入口、交互投影与真实回执。
- **工程位置：Functions。** L2 的受保护服务及 OS 边界另见 [L2_WINDOWS.md](L2_WINDOWS.md)；本文件不增加 Core 职责。
- **上位设计：** [PRD.md](PRD.md)、[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)；验收汇总见 [ACCEPTANCE.md](ACCEPTANCE.md)。
- **实施阶段：** M03 负责标准 FC、可信身份与决定收据；M05 负责 HERV3 等待、续跑与生命周期。普通 FC 文本或提问均不自动授权。

本文中的 **FC 指 Frontend Connector**。模型工具的 function-calling JSON schema 与 FC 是两类接口，不混用术语。本文以固定 SHA 源码为事实依据；“必须”“应”描述待实现契约，“基线已有”描述当前代码。已有架构将权限、Session、Run、Workzone 的权威状态交给 PAO，外部客户端只负责呈现；历史名 `workbench_api.py` 实际是 HASHI Backend API。[架构与所有权][S-ARCH] [兼容命名][S-NAMES]

**统一类型名：** `AccessRequest` 对应旧稿的 `PermissionRequest`，`PermissionLease` 对应旧稿的 `Grant / Lease`；服务端私有类型统一为 `VerifiedPrincipal` 与 `DecisionReceipt`（旧稿分别简称 Principal、称 PermissionDecisionReceipt / Receipt）。这些是同义名称，不是不同对象，不得据此另建一套表、服务或状态机。

## 1. 范围与不可突破的约束

### 1.1 本次允许的工作

1. 在 HASHI 中新增共享权限领域模块，以及基于已有 `RuntimeCommand` / `RuntimeCallback` 注册机制的 **新 `/safety` 命令**。
2. 在 HASHI 公共 FC Functions / Backend API 中做权限写动作必需的最小认证与接线修补，包括历史文件名 `orchestrator/workbench_api.py`。
3. 复用现有命令、文本、状态、菜单 action、回执和持久会话投影；由同一个权限服务提供各通道事实。
4. 复用 HERV3 已有无模型流量等待思路，增加权限决定之后的执行重验与恢复。

### 1.2 本次不变的边界

- 不修改外部 Workbench 或其他外部客户端；不新增其控件、路由、消息字段或权限业务逻辑。
- 不修改 Telegram 专用 connector / 问答 adapter；新增共享命令通过既有注册机制接入，不能另写 Telegram 专属权限实现。
- 不修改公开 FC schema，不新增 `permission_card`、`approval_action` 等组件类型，不要求前端识别新的业务 payload 字段。
- 不修改 protected Core，不新增 Core RPC。保护集合只从 `orchestrator.runtime_contract.CORE_SOURCE_PATHS` 获取，不按目录名猜测，也不在本文件复制另一份保护名单。[Core 权威清单][S-CORE]
- 不改变普通 `ask_user` 的 purpose、答案语义或 `grants_permissions=False`。
- 不把“当前通道无法可信批准”解释成停止整个客户任务；独立工作、既有普通命令、权限申请与可读状态继续。
- 不把无凭据连接、无法控制的后端或尚未隔离的执行环境标成 L2 `enforced`。

**一致体验的含义：** 各前端看到相同请求、同一范围与期限、相同决定结果；可操作方式服从其既有能力。不能据此承诺所有前端都具有相同按钮或布局。

## 2. 基线事实与复用边界

| 基线能力 | 已有事实 | 本次复用方式 |
|---|---|---|
| 动态命令注册 | 扫描共享命令模块及 private commands，注册 RuntimeCommand / RuntimeCallback；既有 runtime 自动绑定。[注册源码][S-REGISTRY] | 新增共享 `/safety` 业务模块；不复制命令列表或通道 handler |
| Generic command interaction | 支持 catalogue/open/act/close，使用服务发行的 menu/button ID、revision 与绑定。[操作校验][S-CMD-SCHEMA] | 状态、请求详情、明确决定、撤销及导航使用现有 operation |
| 菜单投影 | live MenuStore 默认 900 秒；绑定 instance、agent、actor、session、generation、client、connection。[菜单绑定与寿命][S-MENU] | 菜单只是短期交互表示，不拥有 AccessRequest 或 PermissionLease |
| 命令耐久去重 | 共享 bridge 有持久 invocation reservation 与重放路径，重放不重新发行旧 action。[持久命令路径][S-CMD-DURABLE] | 复用命令传输去重；权限服务另做决定/租约事务 |
| 普通问答 | ask_user 仅 clarification/preference；最多 8 个选项，TTL 30–3600 秒；答案不授权。[工具 schema][S-Q-SCHEMA] | 只做范围澄清、偏好选择；正式决定走 /safety |
| 问答来源校验 | answer 路由有 strict owner；答案记录 author=authenticated_user、grants_permissions=false。[HTTP 问答][S-Q-HTTP] [答案事务][S-Q-ANSWER] | 保留现有语义，不能把 answered/consumed 当 PermissionLease |
| 同 Run 等待 | HERV3 terminal guard 保持原 Run，轮询问题而不产生额外 provider 流量。[等待 guard][S-WAIT] | 权限等待复用此模式，正式授权后再恢复操作 |
| FC 问答投影 | run.question.created 映成既有 display/command 与标准 action；不是 approval。[投影][S-Q-PROJECTION] [投影测试][S-Q-TEST] | 不改该映射来伪装权限审批 |
| 通用 Session command | 已有 command/arguments/client_id/request_id/context_generation；TUI relay 走同一命令入口。[Session command][S-SESSION-CMD] | 用已有 arguments 表达 /safety 子命令，无新公开字段 |
| 当前 personal 身份 | _v1_owner_id 在 personal 取配置 owner；它本身不验证请求凭据。[身份解析][S-OWNER] | 权限写不能沿用“有 owner 字符串即已认证”的假设 |

两份 FC 文档确认统一消息、命令、显示、action、媒体与回执的架构方向；历史三前端演练不是本次权限审批已实现或所有按钮已可用的证明。[FC 标准计划][S-FC-PLAN] [历史三前端演练][S-FC-LIVE]

## 3. 唯一事实所有者与标识

### 3.1 领域事实与投影分开

| 对象 | 所有者 | 必须表达的含义 |
|---|---|---|
| PermissionTask | PAO | 一个用户目标的权限归属，可包含多个 PAO Run；有自己的完成、取消与恢复状态 |
| PAO Run | PAO | 接受一条用户 Message 的执行；不能把任意 Worker 重启命名为新 Run |
| ExecutionEpoch | 执行/权限协调层 | 某次执行宿主或后端实例的代次，用于撤销旧执行能力；与 Run ID 独立 |
| AccessRequest | PAO 权限服务 | 待用户决定的具体权限范围、目的、寿命选择、请求 revision 与有效期 |
| ApprovalDecision | PAO 权限服务 | 经可信批准来源验证后提交的正式决定 |
| PermissionLease | PAO 权限服务；L2 由受保护服务执行 | 可使用的权限及其范围、到期、撤销和单次消耗事实 |
| Menu / card / question | 共享 FC 或现有 RunQuestions | 可抛弃表示；不能承担权限账本或扩大范围 |
| VerifiedPrincipal / DecisionReceipt | 可信 ingress 与权限服务 | 服务端私有的批准者证明与一次具体决定的凭据；不是客户端自报 JSON |

Run/Turn/Session 的定义以现有架构为准；PermissionTask、ExecutionEpoch 与权限对象是本次设计，基线没有本文件所述完整权限实现。[现有术语][S-ARCH]

### 3.2 决定必须绑定用户实际看到的版本

正式决定必须绑定：

- instance、owner、agent；
- PermissionTask 与 AccessRequest；
- request revision、规范化 scope digest；
- 操作、资源身份、参数限制、所选寿命、一次使用的 operation 绑定；
- 决定动作及幂等标识；
- 当前交互的 Session / context generation 和可信来源；
- 必要的执行归属，不把旧 ExecutionEpoch 的能力直接继承给新宿主。

按钮通过**服务发行且服务端登记的 action mapping**隐含绑定这些事实。纯文本批准指令必须携带服务生成的 `--revision N`，或等价的不可变带版本 shortref；本期示例统一使用 `--revision N`。用户复制现成指令即可，不要求手填 digest。

服务必须保留 revision 对应的范围快照。**不能只收到 request ID 就取“最新范围”批准。** 修改范围、风险影响或批准寿命含义时，增加 revision；旧按钮和旧指令必须失效。只刷新展示、翻页或换通道，不应无理由改变业务 revision。

## 4. 对用户的命令契约

下列是**拟新增命令语义**；基线存在的是注册与执行机制，不存在完整 `/safety` 模块。[共享注册与分发][S-REGISTRY] [generic bridge][S-BRIDGE]

| 指令 | 效果 | 是否形成正式授权 |
|---|---|---|
| `/safety` / `/safety status` | 显示所选等级、实际覆盖、待批数量、临时授权摘要 | 否 |
| `/safety requests` | 列出当前 owner/agent 可见请求 | 否 |
| `/safety request <id>` | 展示该请求的当前有效版本和决定方式 | 否 |
| `/safety approve <id> once --revision N` | 批准已展示的具体一次操作 | 是 |
| `/safety approve <id> run --revision N` | 批准指定 Run 边界内的该范围 | 是 |
| `/safety approve <id> task --revision N` | 批准指定 PermissionTask 内的该范围，完成即收回 | 是 |
| `/safety approve <id> ttl 2h --revision N` | 批准该范围最多 2 小时，仍受任务收尾等较早终止条件约束 | 是 |
| `/safety narrow <id> … --revision N` | 生成可验证的更窄提案与新 revision，随后展示准确范围 | 否；按钮若明确标为“批准更窄范围”可一次完成 |
| `/safety deny <id> --revision N` | 拒绝该版本请求，不取消整个任务 | 否 |
| `/safety withdraw <id> --revision N` | 撤回待批请求，解除其等待；不倒推撤销已生效 PermissionLease | 否 |
| `/safety revoke <lease-id>` | 撤销指定现行授权 | 不增加权限 |
| `/safety revoke-task <task-id>` | 撤销该任务的直接及派生临时授权，保留成果并追踪资源清理 | 不增加权限 |
| `/safety set-level <L0\|L1\|L2>` | 可信用户选择等级；服务按当前配置 revision CAS 和迁移规则生效 | 管理动作，必须经过可信写门 |

等级与基础策略菜单绑定服务器登记的 policy revision；配置冲突须返回当前状态，不能静默重试覆盖。文本端的精确等级选择同样执行 CAS。上述管理动作不要求新的公开 FC operation 或字段。`revoke-task` 不把任务成果删除，也不把资源仍在清理写成完成。

`narrow` 的精确参数 grammar 应从权限资源类型派生；不得让自然语言“看起来更窄”就直接写 PermissionLease。对目录、对象、工具操作与参数限制进行规范化后的 subset 验证。扩大范围形成新待批版本或新请求，并明确显示变化。

所有会增加权限、放宽策略或改变强制控制状态的动作都进入同一可信写门。Agent 可以通过独立工具主动释放自身 PermissionLease；这种减权行为不能获得人类 VerifiedPrincipal 或作为改等级入口。

### 4.1 卡片内容

请求正文按现有命令显示风格排列：

1. “权限请求”及简短任务目的；
2. 代理与任务、具体资源；
3. 将执行什么操作、可见的影响；
4. 可选临时范围与收回条件；
5. 一次、当前 Run、当前任务、指定时长、收窄、拒绝等可用动作；
6. 无按钮通道的复制指令及请求详情入口。

不自动选中批准，不把“关闭卡片”写成拒绝，不用默认选项代表用户决定。自定义时长必须是明确正时长，拒绝 0、负值、无限值及超出策略上限的输入；不得通过异常值获得永久授权。常规交互不自动授予长期权限。

文案和 locale 复用共享 `command_ui.py` 及既有语言规则，显示具体效果；内部 receipt、进程代次和 digest 只在必要诊断中出现。现有风格约定本来就要求共享帮助、导航和状态派生，不能新建平行 UI 规范。[命令显示规范][S-STYLE]

## 5. 使用现有 schema 的负载示例

以下 JSON 为**既有结构的示例**，业务命令是拟新增功能。示例 ID 符合结构形状但不是可使用的凭据；menu ID、button ID 必须由服务发行，客户端只回传已发行值。不得由模型或客户端自行造 ID 取得动作权。

### 5.1 既有 Session command body

发送到既有 `POST /api/v1/sessions/{session_id}/commands` 的 body：

```json
{
  "command": "safety",
  "arguments": [
    "approve",
    "prq_K7M2P9V4",
    "ttl",
    "2h",
    "--revision",
    "4"
  ],
  "client_id": "client_q7W9e2R4t6Y8u0I2",
  "request_id": "request_p3A5s7D9f1G3h5J7",
  "ui_locale": "zh-CN",
  "context_generation": 7
}
```

字段取自现有 allowlist；不增加 `actor_id`、`human_verified`、`permission_scope`、`principal`、`receipt` 或新的 `request_metadata` 内容。认证使用连接已有的受信凭据；client_id/request_id 只标识客户端与重试，不证明真人。[当前 body 校验][S-SESSION-CMD]

### 5.2 既有 generic command open / act

用户打开请求菜单的 operation：

```json
{
  "version": 1,
  "op": "open",
  "client_id": "client_q7W9e2R4t6Y8u0I2",
  "request_id": "request_b4N6m8Q0w2E4r6T8",
  "ui_locale": "zh-CN",
  "command": "/safety request prq_K7M2P9V4"
}
```

用户选择一个已发行动作的 operation：

```json
{
  "version": 1,
  "op": "act",
  "client_id": "client_q7W9e2R4t6Y8u0I2",
  "request_id": "request_c5V7b9N1m3Q5w7E9",
  "ui_locale": "zh-CN",
  "menu_id": "menu_rQ6K8S0v2T4y6U8i",
  "revision": 2,
  "button_id": "act_F4q6W8e0R2t4Y6u8"
}
```

这里的 `revision:2` 是**菜单 revision**；它与正文所展示的 permission request revision 4 不同。服务端 action mapping 必须同时绑定两者及 scope digest。不能把菜单 revision 当资源范围版本。[operation schema][S-CMD-SCHEMA] [执行前校验][S-ACTION]

既有 opaque transport 使用自己的字段 allowlist 与 connection binding；外部请求不得附加“可信身份”字段，HASHI 不应放宽公开 allowlist 来传 VerifiedPrincipal。[现有 transport][S-TRANSPORT]

### 5.3 既有 response card 的 command_ui 片段

```json
{
  "version": 1,
  "menu_id": "menu_rQ6K8S0v2T4y6U8i",
  "revision": 2,
  "expires_at": 1792766700000,
  "closed": false,
  "rows": [
    [
      {
        "text": "允许一次",
        "button_id": "act_F4q6W8e0R2t4Y6u8",
        "disabled": false
      },
      {
        "text": "拒绝请求",
        "button_id": "act_J8k0L2z4X6c8V0b2",
        "disabled": false
      }
    ]
  ]
}
```

这是现有 command_ui 子对象，不是新卡片类型；它由现有 response 的 message 承载。正文仍使用现有 text/meta/presentation 形式；新授权结果用现有 text/status 投影，不要求前端解析额外权限状态字段。已有 renderer 会把合法 callback 映成 opaque button ID，raw callback_data 保留在服务器端。[render/message][S-MENU-RENDER] [callback 捕获][S-CAPTURE]

### 5.4 普通澄清问题保持原样

可为请求补充需求信息，但不批准权限：

```json
{
  "question": "这个任务需要读取原始数据，还是仅处理已有汇总？这次回答只用于澄清范围。",
  "purpose": "clarification",
  "options": [
    {
      "id": "summary",
      "label": "仅已有汇总",
      "description": "只读取 reports 目录中的汇总文件"
    },
    {
      "id": "raw",
      "label": "需要原始数据",
      "description": "先准备准确的权限请求供批准"
    }
  ],
  "allow_free_text": true,
  "expires_seconds": 900,
  "idempotency_key": "permission-scope-clarification-K7M2P9V4"
}
```

实际答案仍为：

```json
{
  "option_id": "summary",
  "text": "",
  "author": "authenticated_user",
  "grants_permissions": false
}
```

权限服务可把 question_id 与请求关联作为澄清记录；不得据此生成 ApprovalDecision。普通问题的 pending/answered/consumed 状态不改变这一规则。[schema][S-Q-SCHEMA] [保存答案][S-Q-ANSWER]

## 6. 菜单、请求、授权和 Run 的独立生命周期

### 6.1 明确四个时钟

| 对象 | 本契约的寿命 | 到期效果 |
|---|---|---|
| 通用菜单 | 基线默认 15 分钟 | 老按钮失效；重新打开 /safety 生成新投影 |
| 普通 RunQuestion | 基线默认 15 分钟，30 秒至 1 小时 | 结束该澄清问题；不产生决定或授权 |
| AccessRequest | **本次设计默认 24 小时，从请求创建时开始** | 尚未决定的请求过期，不放行操作；原任务仍需要该权限时，Agent 可按既有用户任务重新申请，无需先问是否允许提问 |
| PermissionLease | 用户本次选择的一次/Run/任务/时长，受策略和较早收尾条件约束 | 终止后续受控使用并按执行层契约收回；不以请求 TTL 延长 |

前两项是当前源码事实，第三项是新增产品默认值。菜单刷新、镜像重投、重复申请和 HTTP 重试都不得延长同一 AccessRequest 的 24 小时；请求到期后按原任务重新申请会创建关联的新请求，不复活旧请求或旧批准，也不得靠自动周期续期反复催促用户。[当前菜单寿命][S-MENU] [当前 question 寿命][S-Q-CREATE]

PermissionLease 的时长以正式决定事务的 **`ApprovalDecision.committed_at`** 为唯一计时起点；不从最初申请或 UI 展示时偷扣，也不因排队、暂停、通知迟到、Worker 重启、恢复或执行真正开始而重新计时。用户选择绝对截止时刻时，该时刻同样不顺延。一次、Run、任务结束及策略上限等较早终止条件仍适用。

过期后重新申请保留原 PermissionTask、已完成成果及仍未完成的 `PendingOperation / operation_id`，只更新请求关联和展示；不要求用户先同意重新提问，不创建新的业务操作，不重新执行已完成步骤。原操作结果未知时先对账，不能把重新申请用作重复执行的授权。

### 6.2 跨 Run 与 ExecutionEpoch

- PermissionTask 可跨多个 Run；AccessRequest 可以在自己的有效期内继续等待。
- Run-scoped 选择绑定具体 Run；该 Run 已终结时，不能把这项批准静默转给后继 Run。展示其他仍有效的寿命选择，必要时生成新的请求版本。
- task-scoped 授权可以按服务规则用于同一 PermissionTask 的后续 Run，不能扩展到同 Session 的其他任务。
- ExecutionEpoch 变化要重新检查执行能力与恢复计划；不能据此自动新建 Run，也不能把已消耗的一次操作重新变成未使用。
- 旧 context generation 的菜单或问题不得继续批准。用户在新的有效上下文重新打开同一仍有效请求时，创建新的交互绑定；是否还可批准由请求事实和服务决定。

现有 RunQuestions 强制原 Run 为 running、Session active 且 generation 一致，失配会取消 pending/answered 问题。因此权限请求跨 Run 不能靠长期保留一个普通 question 实现；权限等待也不能通过改变 PAO Run state 而破坏原问题约束。[question refresh/scope][S-Q-LIFECYCLE]

### 6.3 重开、镜像和换通道

1. 请求只有一份。每个有能力的通道从同一 request revision/scope snapshot 派生表示。
2. 各通道拥有独立 menu/action ID；禁止把一个 Workbench button ID 当成另一连接或 Telegram 的有效动作。
3. 同一请求被多个通道批准时，服务以 request revision、决定幂等键和事务 CAS 只提交一次；后续有效重复返回同一结果，不追加 PermissionLease。
4. 一个通道变更或拒绝请求后，其他通道的旧表示展示已关闭/已变更结果。更新投递失败不会撤销已提交决定，也不会允许旧卡再次执行。
5. 点 generic `close` 只关闭表示。撤回请求、拒绝请求、撤销 PermissionLease 必须是明确业务动作；关闭不等于任何一种决定。
6. 窗口重开、页面刷新、菜单过期均从服务重读；不能拿客户端缓存决定权限。

现有 MenuStore binding、revision 检查与 generic close 的投影语义可复用；业务跨通道去重必须另外由权限账本保证。[菜单绑定][S-MENU] [close 与 bridge][S-BRIDGE-CLOSE] [动作校验][S-ACTION]

## 7. 可信 VerifiedPrincipal / DecisionReceipt：仅存在于服务端

### 7.1 普通认证与正式批准分开

现有 `_strict_authenticated_owner` 能验证 personal admin token，或从 enterprise session 取得真实用户。现有 generic session command 的 `_v1_owner_id` 本身则不保证请求经过严格认证。[strict auth][S-STRICT] [认证解析][S-AUTH] [owner][S-OWNER]

本次实现必须在**权限写动作入口**补足认证与来源判断，而不是：

- 从请求 body 的 owner/actor/client_id 推导“真人”；
- 仅检查 source_channel 字符串是不是 Workbench；
- 将 `command_invocation.authorization.allowed` 或 actor_digest 当批准证明；
- 将有效 loopback 请求或实例 HMAC 当人类意愿；
- 把模型返回的 question answer、日志文字或 tool output 转成审批凭据。

个人部署可复用原有受信用户连接的认证 wire，但必须先证明该批准能力不被 agent 工具、shell、管理脚本共享。持有同一个可批准 secret 的两个调用者无法靠后端猜测其身份。

### 7.2 私有对象与作用域

`VerifiedPrincipal` 与 `DecisionReceipt` 为**提议的服务端内部类型**，不是基线已有类，也不是新增 FC JSON。

| 内部事实 | 必须来自 |
|---|---|
| authenticated subject、owner、认证方式与可信等级 | 验证过的用户 session 或隔离的可信 FC ingress |
| instance / agent / task / request | 权限服务的权威记录与已验证目标 |
| request revision / scope digest / decision / duration | 用户看到的快照及服务端 issued action mapping，或含 revision 的明确命令 |
| Session / generation / existing invocation identity | 公共 admission 的当前有效绑定 |
| nonce、有效期、issuer | 可信服务生成 |
| consumed/committed state | 权限决定事务与耐久去重记录 |

VerifiedPrincipal 只说明谁通过何种入口被验证；DecisionReceipt 只允许**一次已明确绑定的决定**。它不自动成为可执行工具的 capability。用户可见的“已授权”回执是提交结果的普通文本投影，不泄露 receipt 密钥或可重用 bearer token。

DecisionReceipt 必须短时有效、单次消费、绑定确切动作；重复同一已提交决定返回原结果。receipt 的单次消费与“允许一次”的 PermissionLease 使用计数是两件事：前者防止重复批准，后者防止操作重复执行。

### 7.3 服务内部传递

- 同进程可使用不可从公开 payload 构造的私有上下文。
- 跨 Functions 进程可用既有 invocation ID 关联受保护服务端登记记录，或通过权限服务自身受 ACL 保护的 IPC 传递。
- 不向 Core 新增产品字段，不把 VerifiedPrincipal 塞进公开 FC operation，不允许请求自行填写“内部”字段被透传。
- Worker 只能引用已登记且与当前动作严格匹配的证明；不能签发、修改、延长或把证明转用到另一个请求。
- L2 必须保护批准 ingress、签发材料、范围快照、动作映射和权限账本。只有数据文件移出项目目录、而 Worker 仍能改渲染或调用 signer，不构成可信批准边界。

### 7.4 凭据隔离是实现条件

在 **L1/L2 受管执行域与新的可信批准通路**中，agent 不得持有能冒充用户批准的 admin/shared secret，不得通过受限管理代理请求服务代调批准入口；L1 的同进程边界限制须如实报告，L2 则必须在受保护域内强制实施。现有 scoped run-question token 的“仅 create/get、不能 answer/admin”边界可作为能力分离参考。[工具 token][S-Q-TOKEN] [gateway scoped secrets][S-GATEWAY] [负向测试][S-Q-REVIEW]

当前 Remote HMAC 验证只认证共享实例来源，现有 remote rescue 工具仍有直接加载 shared secret 的路径。因此“复用已有 header”不等于“现有 secret 已经满足隔离”；需要在上述受管域内使用受限服务代办或隔离原始凭据后，才可以把对应通道列为可信批准来源。[Remote auth][S-REMOTE-AUTH] [现有工具凭据路径][S-REMOTE-TOOL]

**这不授权全局改造 L0。** L0 原有工具、凭据、环境、访问范围和普通命令保持原行为，不得为了满足新的批准模型而静默换 secret、删 header、改 ACL 或套入代理。混合部署中若 L0 仍能取得同一批准能力、操作批准会话或改写其可信入口，应隔离新的批准控制平面，或如实把 L2 及受影响的可信批准覆盖报告为 `partial`；不能通过暗改 L0 或伪报 `enforced` 消除冲突。

## 8. 最小公共入口修补与当前通道能力

### 8.1 修补位置与规则

| 位置 | 最小职责 | 禁止引入的变化 |
|---|---|---|
| `workbench_api.py` 的现有 Session / generic command ingress | 对权限写动作调用严格认证；保存原始已认证请求上下文，派生私有 VerifiedPrincipal；旧无凭据 owner 不可作为批准者 | 不全面改变 L0 普通命令；不增加公开字段 |
| 共享 `frontend_command_admission.py` / command bridge | 从真实 ingress 传入的可信证据建立当前动作绑定；所有 /safety 写路径都到同一权限写门 | 不把任意 Worker metadata 当已验证证据 |
| 新 /safety handler 与权限 bridge | 解析命令、渲染共享状态、核对 action mapping、提交正式决定 | 不直接改 files/JSON 制造 PermissionLease；不实现通道专属审批 |
| 权限服务 | 最终验证 VerifiedPrincipal/DecisionReceipt、revision、scope、时限和 CAS；提供执行恢复通知 | 不因上游说“已认证”就跳过写门 |
| HERV3 等待/执行边界 | 等待正式决定、重验 PermissionLease、恢复对应操作 | 不依赖模型自行理解批准，不在等待时刷模型 |

现有 admin command 已检查 admin token，而简化 agent command 与 Session command 的 personal owner 路径不完全相同；必须覆盖所有能到达同一权限 handler 的入口。最终服务写门是防绕过的唯一共同保证。[admin/agent command][S-ADMIN-CMD] [Session command][S-SESSION-CMD]

opaque `act` 没有明文 `/safety`，需从服务登记 action 解析；不能仅对字符串前缀加鉴权。具有合法管理 token 但没有用户批准来源的请求只具有管理/请求权限，不自动取得人类 VerifiedPrincipal。

### 8.2 当前凭据矩阵：不能假定全部已就绪

| 现有通路 | 源码证据 | 本次处理 |
|---|---|---|
| 外部 Workbench 经 HASHI Remote gateway | gateway 验证原请求 HMAC，并在到 Backend API 的 hop 注入已有 X-Workbench-Token。[gateway][S-REMOTE-GW] [token 注入][S-REMOTE-INJECT] | wire 可复用。完成凭据隔离与可信连接归属验证后使用；HMAC本身只认证实例 |
| governed HTTP 用户 session | identity_service 获取用户并映射 owner。[认证][S-AUTH] [owner][S-OWNER] | 复用有效用户 session，另核对请求批准资格；不能伪装为配置 personal actor |
| 本地 TUI direct | 直接 session.request 无认证 header；命令 body 发往既有 Session command。[TUI direct][S-TUI-DIRECT] [TUI command][S-TUI-CMD] | **当前缺少可复用的可信批准凭据**；保留工作/申请/查看，不能生成批准证明 |
| TUI → 本机 proxy → 远端 | TUI POST proxy 无凭据；本机 proxy 仅判断 loopback；目标转发只有 Content-Type。[TUI proxy][S-TUI-PROXY] [loopback入口][S-TUI-LOOPBACK] [目标转发][S-TUI-FORWARD] | 实例源证明不补足用户认证；不能因 shared HMAC 就准许批准 |
| 已连接 Telegram | 既有用户授权与问答 scope 校验、标准 callback 机制可用。[现有问答入口][S-TG-Q] [注册][S-REGISTRY] | 新共享命令复用入口；在公共授权层证明真实来源，L2还需隔离平台凭据/ingress，不改专用adapter |

### 8.3 缺少可信批准能力时的正常交互

向当前通道明确说明：

> 请求已保存，独立工作会继续。当前连接不能验证批准者，因此尚未授予权限。可在其他已连接、具备可信批准能力的 HASHI 通道打开同一请求并批准。

必须同时显示 request shortref、范围摘要、待批状态与可用查看指令。若已有可信连接，指向其现有入口；不新建外部 UI，不要求所有客户因当前通道限制改变工作流程。

若没有可信连接，保持待批至到期或用户撤回，继续独立工作；如用户主动选择不受 HASHI 完整管控的 backend，按全局兼容策略如实说明实际覆盖并继续，不伪造 PermissionLease，不自动降级用户选定等级。缺失凭据不能用“默认为本机 owner”修补，也不能写“L2 已强制生效”。

## 9. 完整交互场景

### 9.1 正常申请 → 一次批准 → 自动续跑

1. HERV3 准备读取某目录，执行门发现当前范围未覆盖；服务合并等价待批请求并创建 request revision 1，默认待批有效期 24 小时。
2. 共享 FC 发出准确范围与“允许一次 / 当前任务 / 指定时长 / 收窄 / 拒绝”。有按钮的既有客户端显示 action；其他客户端显示可复制、含 revision 的命令。
3. Agent 继续不依赖该权限的工作；没有独立工作时进入无模型流量等待。
4. 用户在可信连接选择“允许一次”。服务验证 VerifiedPrincipal、issued mapping、request revision/scope、操作身份及任务当前状态，在事务内写决定和单次 PermissionLease；期限从该事务的 committed_at 起算。
5. FC 先显示已提交的准确范围与收回条件；只有确认任务仍可运行且恢复已排入当前执行绑定时，才显示“任务正在恢复”。决定提交本身不证明执行已经开始。
6. 执行门重验任务可运行、未停止、原操作仍待执行，以及 PermissionLease、backend、ExecutionEpoch 和当前执行绑定，以原 operation ID 恢复；成功、失败或结果未知均按执行契约结算一次使用，不靠重新发一个批准按钮解决重试。

以下是晚到批准的明确例外，不增加正常路径的确认步骤：任务暂停时可以提交仍合法的批准，但不自动恢复，显示“已批准，任务仍暂停；有效期按原批准时间计算”；任务已停止、取消、完成或终结失败时不再发放可用权限，也不复活原工作；后端或 ExecutionEpoch 已切换时不唤醒旧执行实例。仍有效的任务级批准能否用于新绑定，由恢复流程重验；需要改变范围时展示新的请求版本，不能把旧批准转给不同范围。

现有 HERV3 guard 已证明等待可不占用模型轮询；正式权限恢复是本期新接线，不应写成现有问答本身会授予权限。[等待实现][S-WAIT] [等待测试][S-WAIT-TEST]

### 9.2 收窄范围

- 请求原为读取整个项目。用户选择“仅 reports/”且按钮明确表示“批准仅 reports/”，服务核对该预定义 subset 后可直接提交一次决定，不再弹第二次相同确认。
- 用户输入自定义路径或操作集合时，先生成规范化后新 revision 的范围预览；只在用户明确选择其批准动作时授予。旧 revision 的卡和复制命令失效。
- 如果输入实际扩大范围，显示准确的新请求；不得继续用“收窄”描述。

### 9.3 自定义时长

请求详情提供常用时长以及复制指令，例如：

```text
/safety approve prq_K7M2P9V4 ttl 2h --revision 4
```

用户在已认证通道发送该明确指令即为决定；无需再问“是否确定 2 小时”。服务验证正时长和策略上限，回执给出明确到期时间及“任务提前完成时即收回”。超限输入返回允许范围与当前有效请求，不扩大时长也不产生 PermissionLease。

### 9.4 关闭、撤回、拒绝与停止

- 关闭窗口或 generic close：请求继续待批，菜单表示关闭。
- withdraw：待批请求终止；解除该等待，独立工作继续。
- deny：不给该版本范围，Agent 可使用用户允许的替代方案；不得循环提交相同请求骚扰用户。
- 用户明确停止任务：由 PAO 关闭对应任务/等待并收回任务权限；不能以“后台还有 cleanup”永久保留交互。
- 已批准后点击旧“撤回请求”：显示已决定，提供明确 revoke PermissionLease 操作；不能把无效 withdraw 写成权限已撤销。

### 9.5 重复点击、旧卡与未知结果

| 情况 | 必须行为 |
|---|---|
| 同一 action 因网络重试重复提交 | 返回原决定，不增加 PermissionLease、不重复恢复副作用 |
| 同 request 不同通道近同时批准 | 一个决定事务胜出，其余返回当前权威结果 |
| 旧菜单 revision | 拒绝该动作，显示当前请求/决定；不把旧选择套到新范围 |
| 请求范围 revision 已变 | 明确需要查看新范围，旧命令不能批准最新版本 |
| 菜单 15 分钟过期、请求仍有效 | 重开新菜单，同一请求、同一业务 revision，不重置 24 小时 |
| 批准落盘但 UI 更新失败 | PermissionLease事实不回滚；重查展示已生效，不要求再次批准 |
| 提交结果未知 | 用原 request/invocation identity 对账；不自动另发批准或重放执行 |
| Worker 重启 | 从权限账本重建等待/恢复；旧内存 action不可复活，Run身份按PAO记录恢复 |
| request 24 小时过期 | 保持无授权；Agent 可基于原任务创建关联新请求，保留 PendingOperation / operation_id 和成果；不先问是否可提问、不复活旧批准、不自动周期续期 |
| 决定与撤销/任务结束并发 | 由服务事务与执行门决定最终有效性；恢复前再次读取，不只信任先前UI回执 |

现有 command action 会预先消耗 revision，持久 replay 会移除旧 action；这些保障可复用，但不替代权限领域的幂等和执行消耗。[action处理][S-ACTION] [耐久重放][S-CMD-DURABLE]

## 10. 状态与回执的显示约束

下表是**显示语义**，不是新增 FC enum 或 JSON schema：

| 权威事实 | 用户可见文案要点 |
|---|---|
| 请求登记成功 | 等待批准；说明操作与范围；不是已授权 |
| 普通答案已保存 | 已收到澄清；不是权限批准 |
| 决定已提交，任务可运行且恢复已排入当前绑定 | 已授权具体范围/期限；任务正在恢复；不宣称操作已开始 |
| 决定已提交，任务仍暂停 | 已批准，任务仍暂停；有效期按原批准时间计算 |
| 晚到决定遇到任务停止、取消或其他终态 | 不再发放可用权限，不复活任务；显示当前终态 |
| 决定已提交但后端或 ExecutionEpoch 已变化 | 旧执行不恢复；显示当前绑定与重验结果，不承诺旧步骤继续 |
| 操作已完成、临时权限收回 | 已完成并收回；不宣称可继续使用 |
| 来源不能可信验证 | 当前连接不能批准；请求保留；可在其他已连接可信FC处理 |
| Backend 不支持全部控制 | 显示所选等级与实际覆盖、未覆盖部分；按兼容策略继续 |
| L2 服务/认证边界未就绪 | 显示未强制生效或实际覆盖；不得仅依据配置值标enforced |
| 明确拒绝/撤回/过期 | 该请求未授予权限；独立工作状态单独说明 |
| 展示失败或投递未知 | 展示/投递状态未知；不把它解释为未批准或已执行 |

所有回执只陈述已完成的事实：`saved`、投递成功、决定提交、PermissionLease 生效、操作完成是不同事件。业务审计记录完整内部标识，默认正文不暴露 bearer token、secret、签名材料或多余内部拓扑。

## 11. 验收边界与证据要求

文档阶段只做链接、schema 示例、diff 和 protected Core 静态检查。以下是**实现阶段必需的行为验收**，不表示已经通过；遵守现有测试政策，测试真实权限/恢复边界，不做文档字符串镜像测试。[测试政策][S-TESTING]

| 场景 | 必须看到的结果 |
|---|---|
| FC-C01 普通问答 | 已回答/consumed 且 grants_permissions=false，权限账本没有新增决定或PermissionLease |
| FC-C02 可信批准 | 现有FC负载不加字段；私有VerifiedPrincipal匹配；可运行任务一次提交后自动恢复；无第二次确认 |
| FC-C03 一次授权重试 | 同operation重复交付/结果未知对账不多执行、不多消耗、不重新弹批准 |
| FC-C04 范围收窄 | subset验证、新revision、旧卡失效；新范围与回执准确一致 |
| FC-C05 自定义时长 | 正值与上限校验；时长唯一从ApprovalDecision.committed_at计时；排队/暂停/恢复不延期；任务先结束先收回 |
| FC-C06 菜单与请求过期 | 15分钟菜单失效但24小时请求仍存在；重开不重置请求TTL；请求到期后Agent可在原任务重提，保留operation_id，无额外提问许可 |
| FC-C07 跨Run/重启 | PermissionTask保留；Run-scoped不转移；ExecutionEpoch变化不等于新Run；旧action不能恢复 |
| FC-C08 镜像并发 | 两通道同请求只一份决定/PermissionLease；UI刷新失败不重复批准 |
| FC-C09 纯文本 | 服务复制指令含revision；可信slash调用同写门；改revision/范围不能批准 |
| FC-C10 无认证TUI | 不生成human VerifiedPrincipal/PermissionLease；继续普通命令和独立工作；准确提示已有可信批准入口 |
| FC-C11 伪造身份 | actor/client/source/allowed字段、scoped tool token、agent管理凭据均不能铸造批准proof |
| FC-C12 绕行入口 | Session command、admin command、简化agent command、opaque act最终都受同一权限写门保护 |
| FC-C13 凭据隔离 | 受管执行不能读取批准secret、调用signer或借管理代理转调批准；L2按受保护IPC验证；L0保持原行为，混合域隔离不足不虚报enforced |
| FC-C14 暂停/取消/拒绝 | close不当deny；withdraw不冒充revoke；任务stop收回；晚批准不恢复暂停/停止/终态或旧backend；不循环重问 |
| FC-C15 无模型等待 | pending期间模型调用计数不增长；可运行任务批准后恢复一次；权限pending不计cognitive失败 |
| FC-C16 兼容事实 | 无法管理的后端仍按策略工作；状态不虚报L2 enforced；不要求新增客户端功能 |

已有测试可以作为行为起点：RunQuestions 与 connector 测试覆盖 scope、幂等、ordinary no-permission；command interaction 测试覆盖当前 binding、revision、重放与未知结果；HERV3 guard 测试覆盖等待。它们不证明新权限系统已通过。[问答测试][S-Q-BASE-TEST] [问答负向测试][S-Q-REVIEW] [connector测试][S-Q-TEST] [命令测试][S-CMD-TEST] [adapter测试][S-CMD-ADAPTER-TEST] [transport测试][S-CMD-TRANSPORT-TEST] [guard测试][S-WAIT-TEST]

## 12. 实施文件与交付标准

### 12.1 拟新增模块，均不声称基线已存在

| 提议位置 | 内容 |
|---|---|
| `orchestrator/commands/safety.py` | 共享 RuntimeCommand/RuntimeCallback；状态/请求/决定命令及现有卡片投影 |
| `orchestrator/permissions/fc_bridge.py` | 现有FC操作到权限领域操作的转换、server-side action mapping、私有receipt关联 |
| `orchestrator/permissions/principals.py` | 私有VerifiedPrincipal/receipt接口与验证；具体命名可随实现收敛 |
| 权限服务已有计划目录 | durable request/decision/lease、原子幂等、任务/执行恢复；不由FC重复存一份权威状态 |
| 现有 locale catalog 的新键 | 用户可见状态、动作、错误；不新增前端翻译逻辑 |

以上路径是施工建议，不是运行时代码事实。实现应与主计划唯一状态所有者统一，避免为了本文目录建议复制已有合适模块。

### 12.2 完成定义

1. 正式批准全部通过同一权限写门；普通问答不授予权限。
2. 外部客户端、Telegram 专用 adapter、公开 FC schema 与 protected Core 均无修改。
3. HASHI 公共入口的最小修改有明确目的、来源认证证据和针对性负向测试。
4. 菜单、请求、任务、Run、ExecutionEpoch、PermissionLease 生命周期分别验证，跨通道只有一份授权事实。
5. TUI 等未认证连接的限制如实呈现；只报告实证已可用的可信批准通道。
6. 不以新UI、不明来源的actor字段或虚假的L2覆盖消除阻碍。
7. 源码、离线验证、真实前端验收与运行时采用分开记录；未完成事项写入 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。

---

## 固定版本源码依据

以下链接全部固定到本契约基线 SHA，不随 main 漂移。

[S-ARCH]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/ARCHITECTURE.md
[S-NAMES]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/docs/WORKBENCH_NOTES.md#L29-L38
[S-CORE]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/runtime_contract.py#L28-L38
[S-REGISTRY]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_registry.py#L50-L265
[S-CMD-SCHEMA]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interactions.py#L38-L62
[S-MENU]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interactions.py#L100-L259
[S-MENU-RENDER]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interactions.py#L256-L313
[S-CAPTURE]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interactions.py#L345-L389
[S-ACTION]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interactions.py#L511-L542
[S-CMD-DURABLE]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interaction_bridge.py#L394-L486
[S-BRIDGE]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interaction_bridge.py#L99-L140
[S-BRIDGE-CLOSE]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interaction_bridge.py#L301-L316
[S-TRANSPORT]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/command_interaction_transport.py#L22-L120
[S-Q-SCHEMA]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tools/schemas.py#L6-L45
[S-Q-HTTP]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/workbench_api.py#L1034-L1139
[S-Q-ANSWER]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/run_questions.py#L254-L293
[S-Q-CREATE]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/run_questions.py#L180-L233
[S-Q-LIFECYCLE]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/run_questions.py#L166-L178
[S-Q-TOKEN]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/run_questions.py#L26-L50
[S-Q-PROJECTION]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/frontend_projection.py#L193-L197
[S-Q-TEST]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tests/test_run_question_connectors.py
[S-Q-BASE-TEST]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tests/test_run_questions.py
[S-Q-REVIEW]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tests/test_run_questions_review.py
[S-WAIT]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/adapters/her_v2_provider.py#L1935-L2009
[S-WAIT-TEST]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tests/test_her_v2_cognitive_control.py#L200-L264
[S-SESSION-CMD]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/workbench_api.py#L8419-L8715
[S-OWNER]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/workbench_api.py#L5817-L5821
[S-STRICT]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/workbench_api.py#L1053-L1059
[S-AUTH]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/workbench_api.py#L1600-L1629
[S-ADMIN-CMD]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/workbench_api.py#L12666-L12754
[S-GATEWAY]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tools/gateway/context.py#L115-L141
[S-REMOTE-AUTH]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/remote/security/auth.py#L126-L140
[S-REMOTE-GW]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/remote/api/server.py#L2899-L2927
[S-REMOTE-INJECT]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/remote/api/server.py#L2223-L2251
[S-REMOTE-TOOL]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tools/remote_rescue.py#L510-L519
[S-TUI-DIRECT]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tui/api_client.py#L90-L128
[S-TUI-CMD]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tui/api_client.py#L506-L550
[S-TUI-PROXY]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tui/api_client.py#L274-L277
[S-TUI-LOOPBACK]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/remote/api/server.py#L3083-L3104
[S-TUI-FORWARD]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/remote/api/server.py#L1442-L1455
[S-TG-Q]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/orchestrator/runtime_run_questions.py
[S-FC-PLAN]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/docs/HASHI_FC_STANDARD_INTERFACE_EXECUTION_PLAN_2026-09-25.md
[S-FC-LIVE]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/docs/HASHI1_FC_THREE_FRONTEND_LIVE_TEST_2026-09-26.md
[S-STYLE]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/docs/HASHI_COMMAND_UI_STYLE_GUIDE.md
[S-TESTING]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/docs/TESTING_POLICY.md
[S-CMD-TEST]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tests/test_command_interactions.py
[S-CMD-ADAPTER-TEST]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tests/test_command_interaction_adapters.py
[S-CMD-TRANSPORT-TEST]: https://github.com/Bazza1982/HASHI/blob/d6b6513973bfc8f9f22c40ddd8f72802f907baa0/tests/test_command_interaction_transport.py
