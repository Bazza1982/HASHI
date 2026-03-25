# HASHI Flow — 元工作流平台

> **版本**: 0.2.0
> **状态**: Phase 2 完成 — 可运行

---

## 概述

HASHI Flow 是一个通用的、可扩展的工作流管理平台，设计用于协调 10-20 个 AI Agent 协同完成复杂高质量任务。

任何任务——翻译书籍、深度研究、写作——都可以被定义为一个工作流 YAML 文件，插入到平台中自动执行。

---

## 系统架构

```
人类
 │ Telegram（仅在必要时）
 ▼
ORCHESTRATOR（如小茜）        ← 唯一对外接口
 │ HChat
 ├── EVALUATOR               ← 系统级观察者，持续自我改进
 ├── ANALYST                 ← 任务开始前分析，一次性收集人工输入
 ├── DESIGNER                ← 动态生成工作流 YAML
 ├── DEBUG                   ← 故障自动恢复（最多3次，可调）
 └── WORKERS (local-only)    ← 执行具体任务的专业化 agent
```

---

## 目录结构

```
flow/
├── README.md                   # 本文件
├── flow_cli.py                 # ✅ CLI 入口（run/status/list/eval）
├── schema/
│   ├── workflow.schema.yaml    # 工作流 YAML 标准
│   └── agent.schema.yaml       # Worker Agent 配置标准
├── engine/
│   ├── flow_runner.py          # ✅ 工作流执行器（DAG + HChat）
│   ├── worker_dispatcher.py    # ✅ claude CLI 子进程调度器
│   ├── preflight.py            # ✅ Pre-flight 信息收集
│   ├── artifact_store.py       # ✅ 工件管理
│   └── task_state.py           # ✅ 任务状态持久化
├── agents/
│   ├── orchestrator/AGENT.md   # Orchestrator 角色定义
│   ├── analyst/AGENT.md        # Analyst 角色定义
│   ├── designer/AGENT.md       # Designer 角色定义
│   ├── debug/AGENT.md          # ✅ Debug 角色定义（详细策略）
│   └── evaluator/
│       ├── AGENT.md            # Evaluator 角色定义
│       └── evaluator.py        # ✅ Python 评估引擎
├── evaluation_kb/              # ✅ 评估知识库（Evaluator 维护）
│   ├── patterns/               # 成功/失败模式库
│   ├── model_performance/      # Model 性能基准
│   ├── improvements/           # 改进建议（pending/accepted/implemented）
│   └── workflow_scores/        # 历史评分记录
├── workflows/
│   ├── examples/
│   │   └── meta_workflow_creation.yaml   # ✅ 元工作流（工作流创建工作流）
│   └── library/
│       └── book_translation.yaml         # ✅ 书籍翻译工作流
└── runs/
    └── {run_id}/               # 每次运行的工件和日志
        ├── state.json
        ├── evaluation_events.jsonl
        ├── artifacts/
        ├── logs/
        └── workers/{agent_id}/{inbox,outbox,logs}/
```

---

## 核心设计原则

1. **任务前确认**：所有人工输入在工作流开始前一次性收集，运行中不打断
2. **角色独立**：每个 worker 是独立的 local-only agent，防止记忆和人格污染
3. **可控模型**：每步骤可指定不同 model，由 Orchestrator 或人类控制
4. **自动恢复**：失败由 Debug Agent 处理（最多 3 次），超限才上报
5. **持续改进**：Evaluator 观察所有行为，积累知识，自动改进工作流质量

---

## 快速开始

### 运行预定义工作流
```bash
# 交互式（会提问 pre-flight 问题）
python flow/flow_cli.py run flow/workflows/library/book_translation.yaml

# 带预填充答案（自动化模式）
python flow/flow_cli.py run flow/workflows/library/book_translation.yaml \
  --prefill answers.json --yes

# 静默模式（用默认值，适合测试）
python flow/flow_cli.py run flow/workflows/examples/meta_workflow_creation.yaml --silent -y
```

### 查看运行状态
```bash
python flow/flow_cli.py list
python flow/flow_cli.py status run-book-translation-20260326-062329
python flow/flow_cli.py eval run-book-translation-20260326-062329
```

### 用元工作流创建新工作流
```bash
# 准备 prefill（描述你想要的工作流）
echo '{"task_description": "将英文研究报告翻译为中文 PDF"}' > /tmp/answers.json

# 运行元工作流
python flow/flow_cli.py run flow/workflows/examples/meta_workflow_creation.yaml \
  --prefill /tmp/answers.json -y
```

### 通过 Python 使用
```python
from flow.engine import FlowRunner, PreFlightCollector

runner = FlowRunner("flow/workflows/library/book_translation.yaml")
runner.set_pre_flight_data({
    "source_path": "/path/to/book.pdf",
    "output_path": "/path/to/output.pdf",
    "target_language": "简体中文",
})
result = runner.start()
```

---

## 工作流生命周期

```
CREATED → PRE_FLIGHT → CONFIRMED → RUNNING → REVIEWING → COMPLETED
                ↑                      ↓
            人工确认              DEBUG（自动）
```
