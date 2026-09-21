# HASHI Shared Demo Mode

日期：2026-09-20
分支：`feature/demo-mode-20260920`
当前 main 基线：`b3649c9c1336aa81facb44bb8ef3f60567d42780`
协议：`hashi.shared-demo` / version `1`

## 当前状态

**HASHI 侧 Demo Connector 源码施工已完成，离线 CI 已通过。**

本分支实现匿名 Demo lease、一个访客一个真实 HASHI Agent、最多三个原生 Conversation Sessions、HER Direct/zero、按需 Agent Worker、纯文字 Run、取消、事件长轮询、每日请求预算和到期/结束清理。没有修改 Core major version，也没有合并到 main。

尚未由本次施工声明完成的部分：真实 Workbench↔HASHI 联调、真实模型、Windows/目标 VM、200 用户压力、Cloudflare/公网 canary。它们按用户要求留给本地验证。

## 阅读顺序

1. [共同接口契约](CONTRACT.md)
2. [实现状态](IMPLEMENTATION_STATUS.md)
3. [本地联调与测试](LOCAL_TESTING.md)
4. [原实施计划](IMPLEMENTATION_PLAN.md)
5. [原测试计划](TESTING_PLAN.md)

## 实际实现边界

- Demo 路由：`/api/demo/*`，通过独立 `X-Hashi-Demo-Service-Token` 认证。
- 匿名访客 token 由 HASHI 生成；数据库只保存摘要。
- 每个访客绑定唯一 `owner_id`、`agent_id`、`lease_epoch`。
- Agent 使用正常 HASHI AgentCreation/配置 owner 创建，但保持 inactive，首次 Run 时才启动 Function Worker。
- 每个 Session 显式使用 Demo owner；`memory_policy=disabled`，promotion schedule 关闭。
- Run 仍进入原生 HASHI Session/Run/HER 路径；Connector 不直接调用模型。
- Demo Agent 固定 HER v2、`zero` effort，并写入空 Tool allowlist。
- 每访客最多一个未完成 Run；全局 generation semaphore 与 Worker 上限独立限制。
- 每日请求预算保存在独立 Demo lease DB 中，清除访客不会重置当日全局预算。
- 结束/过期先 revoke，再 cancel active Runs、停止 Worker、清 Session owner 数据、删除 Agent config/workspace、最后删除 lease。
- 长轮询只投影安全公开事件，不暴露内部 event detail、推理、路径或其他 Agent 数据。

## 初始容量参数

| profile 用途 | Worker 上限 | generation 并发 | visitor slots |
|---|---:|---:|---:|
| 16GB 起步 | 12 | 8 | 200 |
| 32GB 目标验证 | 32 | 24 | 200 |
| 64GB 后续 | 64 | 48 | 200 |

实际参数通过环境变量配置；这些仍是容量测试起点，不是已证明承载量。

## 施工证据

当前 Demo Connector CI 运行：
- protected Core guard
- Ruff lint
- `tests/test_demo_connector.py`
- `tests/test_session_api.py`

真实联调与容量测试见 [LOCAL_TESTING.md](LOCAL_TESTING.md)。

## 分支规则

本分支不得覆盖 main；本地验证后再决定是否创建 PR。普通 HASHI 的 owner、Agent lifecycle、Session、删除/quarantine、Remote/HChat 和其他 Frontend Connector 行为保持原有语义。
