# HASHI Flow — Evaluation Knowledge Base

评估知识库。公开仓库只携带空白、无私人运行数据的模板；模式和模型建议只有在其
证据与复现实验可一同审查时才应提交。

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

HASHI Evaluator 在每次工作流运行完成后：
1. 读取 `flow/runs/{run_id}/evaluation_events.jsonl`
2. 分析事件序列并计算有证据支持的指标
3. 将运行分数追加到 `workflow_scores/scores.jsonl`
4. 将规则生成的改进建议写入本地 `improvements/pending.yaml`

`improvements/`、`workflow_scores/` 和 `workflow_versions/` 是被发布边界排除的本地
运行数据。模式、benchmark 和工作流不会被被动 Evaluator 自动修改；本地阈值只会
产生人工复核提示。

## 评分维度

- **效率分**：仅在存在任务复杂度基准时可用；当前被动评估为 `null`
- **质量分**：仅在存在 Validator 或下游反馈时可用；当前被动评估为 `null`
- **稳定分** (0-10)：成功率，debug 次数
- **介入分** (0-10)：实际人工介入 vs 预期（越少越好）
