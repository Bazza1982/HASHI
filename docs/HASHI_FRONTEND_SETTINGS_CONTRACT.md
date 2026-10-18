# HASHI Frontend Connector 设置契约

状态：feature-settings 分支已接通 HASHI 权威设置操作（活动 Scheduler、Agent 隐私与文件访问范围）；其他功能按下文范围继续扩展。

## 目的与边界

Workbench 负责界面、已连接实例选择和收集用户意图。HASHI 仍拥有设置含义、权限策略、校验、持久化及回执。Workbench 不建立平行的隐私、活动或权限状态，也不直接写 HASHI 文件。

目标由“当前 Workbench 连接”与 typed envelope 中的 instance/Agent 共同限定。界面分组及显示顺序不赋予权限。FC 只转发明确允许的操作，不接受任意 URL、路径或函数名。

## 能力发现

`GET /api/v1/capabilities` 在既有能力结果中声明：

- 契约类型 `hashi.frontend-settings`，版本 1。
- 目标字段 `instance_id`、`agent_id`，作用域为经认证的 owner 与目标 Agent。
- 操作 envelope 类型 `hashi.frontend-settings-operation`，版本 1。
- 当前支持的操作：
  - `privacy.read`、`privacy.set_level`
  - `activity.scheduler.list`、`activity.scheduler.create`、`activity.scheduler.update`、`activity.scheduler.delete`
  - `permissions.read`、`permissions.set_access_scope`
- 隐私等级的外层范围为 0、1、2；实际可选值由每个目标 Agent 的当前 backend 返回。HASHI 不声明 L1–L4 自主风险梯度。

Workbench 读取 `/settings/context`，从被选连接的 HASHI 取得 instance ID、Agent 列表和设置能力，再展示能力状态。连接切换会清除旧目标的数据，避免短暂显示另一个实例或 Agent 的状态。

## Typed 操作信封

写操作使用如下结构：

```json
{
  "type": "hashi.frontend-settings-operation",
  "version": 1,
  "request_id": "unique-request-id",
  "target": {
    "instance_id": "HASHI3",
    "agent_id": "momo"
  },
  "operation": "privacy.set_level",
  "payload": {}
}
```

HASHI 必须验证信封类型和版本、操作 allowlist、认证身份、接收实例 ID 与目标 Agent。实例 ID 不是任意跨实例路由地址；Workbench 的 FC/Remote 连接决定实际接收方，信封中的 ID 只用于绑定和核验。

读取也显式带目标实例：

- `GET /api/v1/agents/{agent_id}/settings/privacy?instance_id=…`
- `GET /api/v1/agents/{agent_id}/settings/activity?instance_id=…`

- `GET /api/v1/agents/{agent_id}/settings/permissions?instance_id=…`
Workbench v2 的简单模式只通过 allowlist 路由和现有 Remote/HMAC 连接调用上述接口。它不走聊天 Session 来改变设置，不维护另一份权威副本，也不直接访问目标文件。

## 隐私操作

隐私由 HASHI 当前 privacy policy 和对应 Agent 的运行时/backend manager 管理，不是 Workbench 本地偏好，也不由界面独立定义风险梯度。

读取回传当前等级、active backend 及该 backend 实际支持的等级。写入 `privacy.set_level` 的 payload 包含：

- `level`：HASHI 当前支持的 0、1 或 2。
- `expected_current_level`：用户读取时见到的当前值，作为并发比较依据。
- `confirm_downgrade`：降低保护时必须显式确认。
- `accept_level_two_risk`：首次启用 Level 2 时必须显式接受。

HASHI 再次验证当前值、backend 兼容性、Level 2 隔离过滤器就绪状态和持久化结果。旧值已变化时返回冲突而不静默覆盖。Workbench 仅显示 HASHI 回执。

## 权限操作

权限设置沿用 HASHI 已有的 Agent/backend `access_scope`，不另建 Workbench 风险刻度：

- 读取当前 Agent 的 active backend 及实际文件访问范围。
- 只允许 HASHI 已定义的 `workspace`、`project`、`drive` 三种值。
- 写操作使用 `permissions.set_access_scope`，携带 backend、`expected_current_scope`、新范围和 `confirm_scope_expansion`。
- HASHI 核对当前实例、Agent、backend 与旧值；扩大范围必须显式确认。范围更新会重建该 Agent 的当前 backend，再由 HASHI 原子保存配置。
- Workbench 只展示并更改这一项 HASHI 原生权限。工具/动作的具体授权和审批仍由 HASHI 原有机制控制，不因页面名称或 FC 能力而扩大。

读取接口为 `GET /api/v1/agents/{agent_id}/settings/permissions?instance_id=…`，写入为 `PATCH /api/v1/agents/{agent_id}/settings/permissions`。FC 只转发该 allowlist 操作。

## 活动操作

活动设置直接使用 HASHI Scheduler，不创建 Workbench 专属的“响应式/主动式”总开关、巡检间隔、风险级别或权限系统。

- 作业所有者为目标 Agent。
- 支持 HASHI 现有的 cron、heartbeat、nudge 作业，动作仍为 `enqueue_prompt`。
- Cron 的 IANA 时区由 Workbench 明确传入；当前界面默认使用并显示本机时区，由 HASHI 校验后持久化。
- 创建和更新复用 HASHI 的 Scheduler 校验及 Agent 所有权检查。
- 删除必须带 `explicit_user_confirmation: true`，再由 HASHI 校验作业归属并执行。
- 作业开关只更新该作业的 `enabled` 状态；不改变全局权限或其他作业。

“可用操作”列表说明 FC 可提供哪些调用，不代表已经授予 Agent 任意行动权限。HASHI 对具体操作的 Remote 身份、owner、Scheduler、隐私及确认约束仍然有效。

## FC 与 HASHI 的责任

- Workbench：选择实例/Agent、展示 HASHI 能力、构造 typed 信封、呈现回执；不保存 HASHI 权威状态。
- FC/Remote：认证和固定当前连接、限定 allowlist、传递信封和错误/回执；不拥有隐私、权限或 Scheduler 策略。
- HASHI：核对实例与 Agent、执行既有 privacy、access_scope 和 Scheduler 所有者逻辑，验证确认及兼容性、持久化并返回结果。

错误应返回稳定的公开错误码；隐私响应不返回 provider 凭据。活动读取只应返回目标 Agent 的 Scheduler 记录。

## 当前实现范围与后续边界

本次已完成菜单信息架构、HERV3 默认说明、实例/Agent 目标选择，以及上述 privacy、文件访问范围和 Scheduler 操作。以下仍是后续独立工作，不得伪装为已支持：

- Agent persona/agent.md 字段级读取与局部更新。
- Telegram/WhatsApp 的 Agent 级凭据和通道设置。
- HASHI 权威 Primary Agent 绑定。
- HERV3 provider/model/reasoning 的运行时写入与校验。
- 本地 AI 安装、完整性验证与运行时回执。
- 字幕消息资格的 FC 消息来源过滤。

扩展每项功能前，应先确认 HASHI 的设置所有者与可调用能力，再增加对应 typed operation 和测试；不得为了填满界面而添加无后端依据的开关。
