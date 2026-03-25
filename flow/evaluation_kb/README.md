# HASHI Flow — Evaluation Knowledge Base

评估知识库。由 Evaluator Agent 维护，记录工作流执行模式、质量规律和改进建议。

## 目录结构

```
evaluation_kb/
├── README.md              本文件
├── patterns/              执行模式库（哪些做法有效）
│   ├── successful.yaml    成功模式集合
│   └── failure.yaml       失败模式集合
├── model_performance/     各 model 在不同任务类型的表现
│   └── benchmarks.yaml
├── improvements/          待实施的改进建议
│   ├── pending.yaml       待处理（新提案）
│   ├── accepted.yaml      已接受（等待实施）
│   └── implemented.yaml   已实施（归档）
└── workflow_scores/       各工作流的历史评分
    └── scores.jsonl       追加写入的评分记录
```

## 使用方式

Evaluator Agent 在每次工作流运行完成后：
1. 读取 `flow/runs/{run_id}/evaluation_events.jsonl`
2. 分析事件序列，提炼模式
3. 更新本目录下的 YAML 文件
4. 生成并归档改进建议到 `improvements/pending.yaml`

## 评分维度

- **效率分** (0-10)：耗时 vs 任务复杂度
- **质量分** (0-10)：输出质量（由人工或后续 agent 反馈）
- **稳定分** (0-10)：成功率，debug 次数
- **介入分** (0-10)：实际人工介入 vs 预期（越少越好）
