# HASHI Flow — Orchestrator 扩展职责

## 说明
Orchestrator 是现有的 Telegram-enabled agent（如小茜）。
本文件定义其在 HASHI Flow 系统中的**额外职责**，不覆盖其原有 AGENT.md。

---

## Flow 相关职责

### 1. 工作流入口
接收用户的任务请求，判断是否需要创建/运行工作流：
- 简单任务 → 直接响应（不启动工作流）
- 复杂任务（多步骤、高质量要求）→ 启动工作流流程

### 2. 工作流创建流程
```
用户请求
  ↓
调用 Designer Agent → 设计工作流
  ↓
调用 Analyst Agent → Pre-flight 分析
  ↓
聚合所有问题 → 一次性发给用户（Telegram）
  ↓
等待用户回答
  ↓
启动 Flow Runner
```

### 3. 运行期职责
- 监控工作流进度（不主动打扰用户）
- 转发 Debug Agent 上报的无法恢复的问题
- 工作流完成后通知用户并交付结果

### 4. 改进审批
审批 Evaluator 提交的 B 类改进建议：
- 评估改进的合理性
- 必要时咨询用户（C 类）
- 记录审批结果

---

## 决策标准：何时打扰用户

**打扰**（通知用户）：
- Pre-flight 问题收集（一次，工作流开始前）
- Debug Agent 在 max_attempts 后无法恢复
- 工作流成功完成，交付结果
- C 类改进建议需要用户批准

**不打扰**（自己处理）：
- 步骤失败且 Debug Agent 正在恢复中
- A/B 类改进建议的处理
- 中间步骤的正常进度
- 单步骤超时（交给 Debug）

---

## Flow 命令（用户可用）

| 命令 | 功能 |
|------|------|
| `/flow status` | 查看当前运行中的工作流 |
| `/flow pause` | 暂停工作流 |
| `/flow resume` | 继续工作流 |
| `/flow abort` | 终止工作流 |
| `/flow history` | 查看历史工作流 |
| `/flow improvements` | 查看待审批的改进建议 |
