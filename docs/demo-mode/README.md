# Shared Demo Mode — 计划入口

日期：2026-09-20
分支：`feature/demo-mode-20260920`
源代码基线：`637422c88692680d35592a46acef5c4e22f33c3f`
状态：**仅实施与测试计划；尚未实现、部署或完成容量验证。**

## 本次交付的边界

本分支从上述 main 快照建立。本次只增加 `docs/demo-mode/` 下的文档，不修改 runtime、配置、依赖、Core、现有架构正文或 main，不启动任何实例，不开通公网路由，也不合并分支。后续实现、实机采用和公网发布分别记录，不能把这些计划当成现成开关。

## 阅读顺序

1. [共同接口契约](CONTRACT.md)：客户端与 HASHI 的唯一协议基线，标识为 `hashi.shared-demo` / version `1`（设计草案）。
2. [HASHI 实施计划](IMPLEMENTATION_PLAN.md)：文件级责任、访客生命周期、受限执行、资源管理和施工顺序。
3. [HASHI 测试计划](TESTING_PLAN.md)：隔离、恢复、到期清理、容量和正常模式回归。

外部客户端应引用本契约，而不是另建一份独立演进的后台协议。实现开始时固定本目录所在的提交 SHA，并在联调记录中同时记录客户端和 HASHI 提交；本文中的分支名只是施工入口，不是不可变发布版本。

## 已确定的产品范围

- 一个隔离 VM、一个 HASHI instance、一个兼容的受限网页客户端；不依赖托管控制平台，不为每位访客创建 VM/container，不引入独立网关产品。
- 无账户。使用一个匿名随机凭证；浏览器以一枚 HttpOnly Cookie 持有。IP 只用于辅助限流，不能作为聊天归属。
- 每位访客一个真实 Agent，最多三个原生 Conversation Sessions；只能看自己的数据。
- 最多 200 个有效访客槽位。连接数、有效租约数、存在的 Worker 数、模型并发数是不同限额。
- 对话最长 24 小时；默认建议闲置 30 分钟提前回收，可用配置关闭提前回收。界面必须披露实际策略。
- HER Direct / `zero`，配置决定模型；无工具、slash 控制、上传、语音、长期记忆晋升和后台自主工作。
- 使用现有逐 Agent Function Worker 生命周期，按需启动并在空闲时释放；不跨访客复用可变 Worker，不改造 Core 为多租户调度器。

## 容量参数不是容量证明

| 测试配置 | 主机内存标签 | Worker 上限 | 执行并发上限 | 有效访客上限 |
|---|---:|---:|---:|---:|
| small | 16GB | 12 | 8 | 200 |
| standard | 32GB | 32 | 24 | 200 |
| large | 64GB | 64 | 48 | 200 |

这些是显式选择的初始参数，不自动探测硬件、不承诺能承载相应人数。默认采用 small；standard 是 200 人轻量体验的目标验证配置。此前每 Worker 0.5GB、每轮 10 秒和每人 3 分钟一条的数字均为估算假设，不能写成验收结果。200 人突发同时发送与 200 个在线连接分开测试。

## 架构归属

PAO owns：访客与 Agent 绑定、Conversation Sessions、接纳控制、Worker 使用、回收协调。PCM owns：公开 Persona/Context 的组装。HER owns：Engine Session、受限执行和模型计量。Frontend Connector owns：受限协议及公开投影。工程放置为 Functions、平台配置、实例配置；**不授权 Core major-version migration**。

以 [ARCHITECTURE](../../ARCHITECTURE.md)、[Layered Runtime Boundaries](../HASHI_LAYERED_RUNTIME_BOUNDARIES.md) 和 [Testing Policy](../TESTING_POLICY.md) 为上位约束。现有兼容文件名 `workbench_api.py` 仅指 Backend API，不表示在 HASHI 仓库中新增外部 UI 产品。

## 交付与发布状态

| 阶段 | 状态 |
|---|---|
| 产品方向及文档分支 | 已批准 |
| 实施计划与测试设计 | 本目录 |
| 代码实现、focused/core gate | 未执行 |
| 真实 Worker 恢复与内存测试 | 未执行 |
| 200 连接压力与费用验证 | 未执行 |
| 隔离 VM、真实模型、公网 canary | 未执行；需单独操作授权 |

后续不覆盖 main、不重置他人分支、不动日常实例。文档内的测试命令和配置均是将来实施步骤，不是本次运行记录。
