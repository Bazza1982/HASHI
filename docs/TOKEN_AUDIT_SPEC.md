# HASHI Token Audit Spec

## 目标

这份 spec 的目标很直接：

1. 不只看总 token，而是看每次请求的上下文由什么组成
2. 找出可优化的浪费点，比如重复 prompt、memory 过肥、tool schema 过大、非增量请求反复塞全文

## 核心原则

- 以单次请求为事件单位
- 同时覆盖 `flex` 和 `fixed` agent
- 同时记录 prompt 组成、token、工具元数据、请求来源
- API backend 用真实 usage
- CLI backend 用估算值，但必须按 `final_prompt` 估算

## 文件

每个 agent workspace 新增：

- `token_audit.jsonl`

原有文件保留：

- `token_usage.jsonl`

分工：

- `token_usage.jsonl` 做账单和总量统计
- `token_audit.jsonl` 做结构化根因分析

## 记录时机

每次 backend 请求完成后记录一条事件：

- 成功请求
- 失败请求
- background detach 后的最终完成请求

## 事件字段

请求字段：

- `request_id`
- `agent`
- `runtime`
- `backend`
- `model`
- `source`
- `summary`
- `silent`
- `is_retry`
- `success`
- `incremental_mode`
- `detached`
- `background_completion`

prompt 字段：

- `raw_prompt_chars`
- `effective_prompt_chars`
- `final_prompt_chars`
- `context_chars_before_budget`
- `time_fyi_chars`
- `budget_applied`
- `budget_limit_chars`
- `context_expansion_ratio`
- `context_fingerprint`
- `request_fingerprint`

section 字段：

- `section_chars`
- `section_tokens_est`
- `section_counts`

当前 section key：

- `additional_system_context`
- `system_identity`
- `active_skills`
- `relevant_long_term_memory`
- `recent_context`
- `extra:*`

token 字段：

- `token_source`
- `input_tokens`
- `output_tokens`
- `thinking_tokens`

工具字段：

- `tool_catalog_count`
- `tool_schema_chars`
- `tool_schema_tokens_est`
- `tool_max_loops`
- `tool_call_count`
- `tool_loop_count`

时延字段：

- `queue_wait_s`
- `backend_elapsed_s`

## 关键分析口径

### 1. Context Expansion Ratio

公式：

`final_prompt_chars / raw_prompt_chars`

意义：

- 越高，说明桥层注入越重
- 高比值请求优先查 skills、memory、recent context

### 2. Section Cost

看每个 section 的总 token 和平均 token：

- `active_skills`
- `relevant_long_term_memory`
- `recent_context`
- `system_identity`
- `extra:*`

这个口径直接回答“token 花在哪”。

### 3. Repeated Context

按 `context_fingerprint` 聚合，找反复出现的相同上下文。

这个口径用来定位：

- 重复 handoff
- 重复 FYI/skills/memory 注入
- 本来该 incremental，却仍反复塞全文

### 4. Tool Schema Bloat

重点看：

- `tool_schema_tokens_est`
- `tool_catalog_count`
- `tool_call_count`

重点找两类：

- schema 很大，但本次根本没调用工具
- schema 很大，但只调用了很轻的工具

### 5. Token Source Split

区分：

- `api`
- `estimated`

这样可以避免把 CLI 估算和 API 真实 usage 混在一起误读。

## 报表脚本

命令：

`python scripts/token_audit.py report`

可选参数：

- `--since-days`
- `--limit`
- `--json-out <path>`
- `--md-out <path>`

输出维度：

- 全局 summary
- 按 agent 汇总
- 按 backend 汇总
- 按 model 汇总
- 按 source 汇总
- section summary
- highest input requests
- repeated contexts
- tool schema bloat
- context expansion hotspots

## 第一批优化动作

审计跑起来后，优先从这些动作收口：

- 把 memory recall 默认上限从 6 降到 3，再看召回质量
- 把 toggle skill 从整段注入改成短 policy + 按需展开
- 把 `/handoff` 默认恢复模式改成摘要优先
- 对 `recent_context` 加去重和 section budget
- 对工具做更小的 per-agent 白名单
- 对支持 session 的路径优先走 incremental，避免反复塞整份 bridge context
