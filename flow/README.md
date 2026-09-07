# HASHI Flow — 元工作流平台

> **状态**: 当前实现参考（发布前审查版）

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
 ├── DEBUG                   ← 故障诊断与有针对性的持续恢复
 └── WORKERS (local-only)    ← 执行具体任务的专业化 agent
```

---

## 目录结构

```
flow/
├── README.md                   # 本文件
├── flow_cli.py                 # ✅ CLI 入口（run/status/list/eval）
├── engine/
│   └── ...                     # nagare/ 核心的 HASHI 兼容导入层
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
│   ├── schema/
│   │   └── workflow_schema.yaml         # 当前工作流契约
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

1. **优先任务前确认**：通常在工作流开始前收集输入；声明 `wait_for_human`
   的步骤也可以在运行中显式暂停并等待回答
2. **角色独立**：每个 worker 是独立的 local-only agent，防止记忆和人格污染
3. **可控模型**：每个 worker 明确声明受支持的 backend 与 model；需要不同配置时
   建立独立 worker
4. **自动恢复**：失败由 Debug Agent 诊断并改变恢复策略；次数本身不触发终止，
   只有明确不可恢复、基础设施故障或外部停止信号才结束该路径
5. **可审查改进**：被动 Evaluator 只记录可测量指标并生成建议；只有明确的
   candidate-authoring 步骤才可写候选，候选仍须通过一次完整运行才会晋升

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
CREATED → pre-flight 校验／可选 CLI 确认 → RUNNING → COMPLETED
                                            ├── PAUSED（当前进程内可恢复）
                                            └── FAILED / ABORTED
```

步骤失败时，顺序和并行执行路径都会调用 Debug Agent 进行有针对性的恢复；工作流也可通过
`wait_for_human` 暂停等待明确的人类输入。
