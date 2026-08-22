# `/meter`（`/metre`）回合成本显示 — 设计与实施记录（v2.1）

> 状态：已实施并完成本地回归；当前运行实例尚未加载，未执行 `/reboot` 或 `/restart`
> 依据：Zelda（皇后娘娘）2026-08-22 审核结论「🟡 有条件通过」——批准修订后方案，不批准原五点直接开工。

## 1. 目标

在 HER v2 backend 上新增 `/meter`（兼容别名 `/metre`）命令：开启后，每轮对话结束**再**追加一条简短的「成本尾巴」，显示本轮 token 与金额。默认关闭。

## 2. 总体结论（Zelda 审核）

| 原五点 | 结论 |
|---|---|
| 1. 注册 `/meter` + `/metre` | ✅ 基本成立，按别名机制细节修订 |
| 2. `cmd_meter`、持久化、`self._meter` | ✅ 基本成立，默认 OFF + 无参只显示状态 |
| 3. 白名单加入 `meter` | ✅ 正确，复用 workspace 级 JSON 偏好 |
| 4. `record_usage` 后追加尾巴 | ⚠️ 需调整：记账返回结构化 receipt，且**确认最终答复送达后**才发送 |
| 5. 冥想成本合并进主报告 | ❌ 必须重新设计：冥想异步、不能阻塞主答复，改为两阶段 |

最大必要改动：**先建立带阶段/模型/来源标记的逐调用成本明细，再做 `/meter` 展示层。**

## 3. 修订后的实现点

### 3.1 命令注册
- `command_specs.py` 注册两个 `CommandSpec`：
  - `meter` 为 canonical；
  - `metre` 隐藏菜单，并设置 `alias_of="meter"`。

### 3.2 命令行为
- `flexible_agent_runtime.py` 加 `cmd_meter`：
  - 默认 **OFF**；
  - 无参数只显示当前状态；
  - 仅 `on` / `off` 改变设置（另提供 `status`）。

### 3.3 持久化
- 白名单：`telegram_stream_policy.py` 的 `DISPLAY_PREFERENCE_NAMES` 加入 `meter`。
- 复用 workspace 级 JSON 偏好持久化开关，**不**新造 `.meter_off` 文件。

### 3.4 逐调用成本明细（新数据契约，核心前置）
在 `HashiStageProvider` 仍知道真实 `profile.engine / profile.model / stage` 时生成逐调用 usage line item，至少含：

- `request_id`、`parent_request_id`
- `phase`：`execution` / `review` / `persona` / `wrapper` / `meditation` 等
- `engine`、实际 `model`
- `input` / `output` / `thinking` tokens
- `token_source`：`provider` / `estimated`
- `cost_usd`
- `cost_source`：`provider` / `pricing_table` / `local_zero` / `unknown`

再据此汇总，避免 HER 外层 `role-configured` 把多 stage/多模型 token 合成一笔后用一个默认价格估算。

### 3.5 成本尾巴渲染（确定性，无模型调用）
- **不使用** `_ConfiguredPersonaPackager`（会为显示成本而产生新成本，且是 adapter 私有实现）。
- 使用无模型调用的**确定性 formatter**；可借鉴 `minimal_persona_fallback` 的语气，但不调用 Persona 模型。

### 3.6 展示与发送规则
- 尾巴示例：`💰 前台回合：≈ US$0.012347 · 8.4K tokens · 价目表估算`
- provider 实报时去掉 `≈` 并标注「Provider 实报」；小额成本用六位小数或 `< US$0.0001`。
- `/meter` 默认关闭，workspace 级持久化。
- 请求开始时冻结 `meter_at_start`，避免长任务中途切换漂移。
- 成本尾巴作为**独立 Telegram 展示消息**，在最终答复**确认送达后**发送。
- 不写入模型记忆 / transcript / wrapper 输入，不朗读，不影响 CoS 判定。
- 覆盖 foreground、background、流式最终提交、HER `final_already_delivered`。
- `silent`、不投递 Telegram、transfer buffering、最终发送失败时**不发送**。
- cron 默认走普通回合，自然覆盖，无需 cron 特判。

### 3.7 Habit Meditation 处理（两阶段）
冥想不阻塞主答复（不破坏「冥想不阻塞 live completion」契约）：

1. 主答复后发送「前台回合成本」；
2. 冥想结束后发送：`🧘 冥想：≈ US$0.001240 · 任务累计 ≈ US$0.013587`

- 已有 Habit 更新通知时可合并；无 Habit 变化但 `/meter` 开启时发独立简短通知，避免漏掉费用。
- 冥想通知依据请求开始的 `meter_at_start`，**不依赖** `/verbose`。

## 4. 语义修正（必须遵守）

- 未知模型**不**悄悄套用高价 `default` 后假装精确 → 显示「成本未知」。
- `0.0` 与 `None` 必须区分：前者可能是真正免费或本地模型，后者才是未知。

## 5. 范围定义

- 只显示主响应时写作「**前台回合成本**」，不称「账单」或「任务总成本」（wrapper / CoS / audit follow-up / observer / meditation / dream 等部分异步、部分无 request correlation）。

## 6. 必补回归测试

- canonical 命令、隐藏别名及菜单绑定。
- 默认关闭、`on/off/status`、跨 runtime 持久化。
- Provider 实报、静态估算、未知模型、本地零成本。
- HER 多 stage、多模型、replan、Persona 与重试聚合。
- 前台、后台、cron、流式、HER 提前投递及失败投递。
- 异步冥想完成、无 Habit 变化、恢复后通知和防重复。
- 尾巴不进入记忆、语音、wrapper 或 HChat 内容。

## 7. 安全边界与备注

- 本方案属于 HASHI 命令层与展示层改动，涉及 `command_specs.py`、`flexible_agent_runtime.py`、`telegram_stream_policy.py`、`adapters/her_v2_provider.py`（HashiStageProvider 明细）与 token_tracker 语义。
- 相关 core runtime 改动已在用户明确要求继续修复 `/meter` 后实施。
- 遵循 HASHI 编码原则：改动前先出 `check` / 计划；实现后先跑回归测试再合入。

## 8. 实施状态（2026-08-22 更新）

### 已完成
- 逐调用成本明细数据契约 `tools/meter_cost.py`（`PerCallUsageLineItem` / `UsageReceipt` / `format_cost_tail` / `format_meditation_cost_tail`）。
- 命令注册 `/meter`（canonical）+ `/metre`（隐藏别名，`alias_of="meter"`），默认 OFF。
- `cmd_meter` on/off/status，workspace 级持久化，白名单加入 `telegram_stream_policy.DISPLAY_PREFERENCE_NAMES`。
- 前台/后台回合成本尾巴（确认送达后发送，`meter_at_start` 冻结），静默/非 Telegram/transfer 缓冲/未送达时跳过。
- **冥想两阶段通知**：冥想结束后按 `meter_at_start` 独立发送 `🧘 冥想：… · 任务累计 ≈ …`，不依赖 `/verbose`，不阻塞主答复，不进入记忆/语音/wrapper/HChat；冥想成本按阶段 line item 持久化到 journal（`meter.line_items`）。
- Zelda 回归矩阵新增测试：流式去重、silent/transfer 排除、不进记忆/语音/wrapper/HChat、冥想按 meter 而非 verbose 门控。
- 并发请求的开关快照和 receipt 均按 `request_id` 隔离，不再使用 runtime 全局临时槽，避免重叠回合串账。
- Provider 的 `completion/output_tokens` 已按“包含 reasoning 子集”处理，不再把 reasoning tokens 重复加入 token 总量或费用。
- OpenRouter 与 DeepSeek 工具循环保留每次真实 HTTP 调用的 usage；按单次 prompt 长度套用阶梯价，不再对整段 stage 聚合后统一定价。
- `token_usage.jsonl` 以结构化 receipt 为金额权威来源；未知成本持久化为 `null`，不再伪装成 `0.0`。
- `/meter` 无参数与 `status` 均为只读；只有 `on` / `off` 会修改 workspace 偏好，非法参数只返回用法。
- 冥想成本建立独立持久化 outbox：逐次调用先落盘，再进入 pending/sending/sent 状态；支持有界重试、进程恢复和已发送状态防重复。

### 未落地 / 待验证（如实说明）
- 真实 Telegram 收发端到端验证未做。
- 当前运行实例尚未加载这些改动；本轮明确未执行 `/reboot` 或 `/restart`。
- 冥想成本为定价表估算（maintenance response 不携带 provider 实报 cost_usd），显示 `≈`；后续如 backend 透出实报成本可再收紧。
- Telegram 不提供应用级幂等键；若进程恰好在 Telegram 已接收消息、但本地 `sent` 标记尚未落盘的极小窗口崩溃，外部“严格恰好一次”仍无法证明。稳定 `delivery_id` 与本地 outbox 可消除其余正常恢复路径的重复发送。

### 本地验证结果

- `/meter` 定向矩阵：命令语义、成本来源、并发隔离、reasoning、OpenRouter/DeepSeek 工具循环、冥想重试与恢复均通过。
- 跨模块矩阵：runtime pipeline、Habit journal、HER v2 adapter、usage/overview 与 sidecar 调用均通过。
- `git diff --check` 通过；仅发现工作树中既有 PowerShell 文件的 LF/CRLF 提示，与本功能无关。

---
_更新时间：2026-08-22 · v2.1 技术修复与回归校准_
