# 权限系统实施与证据状态

更新日期：**2026-10-29**；设计版本：**v1.1**。原始设计核查日期为 2026-10-23。

**当前交付为设计文档包。没有实现权限功能，没有安装 Windows 安全服务，没有进行运行中采用或生产重启。** 本文件必须在后续开发中更新，不能把计划中的要求自动勾成完成。

## 2026-10-29 接手准备

- 用户明确批准本次权限设计工作的分支和工作区；已在 HASHI3 建立本次工作区。这个批准不自动授权未来的新分支、额外工作区或同时安排多个写入者。
- 权限分支已完整合入当时的 GitHub main `d2a6f36d4597181800c2eb671381a1079a872bae`，保留原来的九份设计文档；本次文件修订仍只涉及文档。原始设计和固定源码证据仍指向下表的 2026-10-23 基线。
- 对新基线的权限相关入口做了有限复核，发现多个入口文件相对原始基线已有变化；复核结果和未完成事项见 [CODE_MAP](CODE_MAP.md)。M00 的完整入口盘点、可执行 L0 行为比较和实施门仍未完成。
- 草稿 PR #118 只用于审阅设计文档。文档提交、GitHub 合入、功能实现、运行采用和真实验收分别记录，不因建立工作区而视为完成。

## 1. 当前事实

| 项目 | 状态 | 证据 / 说明 |
| --- | --- | --- |
| 用户设计边界 | 已确定 | 仅 HASHI，FC 复用，外部 Workbench / Telegram 专属改动为零，Core 不变 |
| 2026-10-23 设计基线源码核查 | 已完成 | `d6b6513973bfc8f9f22c40ddd8f72802f907baa0`；新基线的 M00 复核仍待完成 |
| GitHub 功能分支 | 已创建 | `feature-permission-sys`，从上述基线建立 |
| 更新 PRD 与施工包 | 已完成并交叉审阅 | 本目录 9 份文档；交付版本以包含本文件的分支提交为准 |
| 文档静态验证 | 已通过 | 相对链接、固定源码路径/行范围、编号、JSON与既有FC操作校验、whitespace、Core guard；不等同功能测试 |
| 应用源码实现 | 未开始 | 本提交不改变应用源码 |
| L0 行为比较 fixture | 待实现 | 已定位基线，不声称完成代码行为比较 |
| L1 行为 / 并发 / 恢复测试 | 未执行 | 验收用例均为计划 |
| Windows L2 服务 / runner | 未实现、未安装 | W01–W12 是施工要求 |
| Windows OS 边界 / WAC | 未执行 | 不以 Linux/mock 替代 Windows 实证 |
| 外部前端实时验收 | 未执行 | 没有用户浏览器/客户端操作或消息发送 |
| Functions 构建 / 运行采用 | 未执行 | 没有 reboot、服务切换或部署 |
| 生产验证 / Core重启 | 未执行 | 本任务没有进行这些操作 |

## 2. M00–M10 状态

| 阶段 | 状态 | 已有准备 | 尚需完成 |
| --- | --- | --- | --- |
| M00 | 部分研究完成，实施门未通过 | 基线、代码/FC/平台边界已核查 | 本地现场确认、入口盘点和可执行 L0 fixture |
| M01 | 待实现 | 类型、配置与 L0 约束已设计 | 类型/API、配置CAS、真实旁路及证据 |
| M02 | 待实现 | schema、事务、恢复算法已设计 | 账本/迁移/outbox、并发与崩溃验证 |
| M03 | 待实现 | FC契约和认证缺口已核查 | `/safety`、可信决定、公共入口接线 |
| M04 | 待实现 | Registry排队风险和工具范围已定位 | 描述器、start门、取消及各通道覆盖 |
| M05 | 待实现 | HERV3真实路径、quiesce/代际问题已定位 | 等待/续跑/park、任务与生命周期接线 |
| M06 | 待实现 | L1行为、迁移、后端兼容已规定 | 完整闭环、覆盖报告和验收 |
| M07 | 待实现 | Windows服务/身份/broker方案已规定 | 安装产物、OS隔离、可信IPC |
| M08 | 待实现 | L2 host/runner和代理合同已规定 | 真实受限HERV3、资源代理与正负向测试 |
| M09 | 待实现 | 故障矩阵和WAC已列出 | 撤权、监督、恢复和Windows证据 |
| M10 | 待实现 | 发布/采用/回退门已规定 | 合格产物、对应授权范围内采用和移交 |

## 3. 已知实现难点与发布边界

| 事项 | 对实施的要求 | 不应声称 |
| --- | --- | --- |
| 现有 generic command 的 personal owner 解析 | 新权限写需可信 Principal/Receipt 和最终服务写门 | 配置 owner 即人类认证 |
| TUI direct/proxy 缺可信批准凭据 | 普通工作继续，通过另一已连接可信 FC 批准 | 所有旧客户端天然都能可信批准 |
| Remote/bot/admin secret 可能被工具取得 | 对受管理执行域隔离批准凭据，提供窄业务代理 | 有 HMAC 就证明是人类 |
| L1 无约束本机 shell | 明确宽权限例外及 partial；必要时提供受限 runner | 普通 shell cwd 等于磁盘围栏 |
| 现有 Functions worker 为普通 spawn | 新受限 HERV3 host/窄 broker | 仅 env flag 就成为 L2 |
| 宽 Functions RPC 可请求管理能力 | 不向 sandbox 交出；每个代理动作重新验主体和scope | IPC在localhost便可信 |
| 同桌面或混合L0可取得审批能力 | 隔离L2控制平面；否则actual_controls同步partial | 为保持L2标签可以改变L0 |
| 完整同机快照回滚 | 明确L2可证明范围；恢复缺证据则失效活动授权 | 一份同机数据库可证明自己从未被回滚 |

这些是具体实现和环境限制，不是阻止客户工作的全局门槛。受控动作仍须有有效授权；其他独立工作与用户选择的兼容后端继续。

## 4. 后续更新模板

每个里程碑追加一条记录，保留旧证据，不覆盖失败历史：

```text
Milestone:
Date / developer:
Starting HEAD / ending HEAD:
Functional owner / engineering layer:
Changed paths:
Observable behavior changed:
Requirements / CASE / WAC covered:
Commands and actual results:
OS / runtime / service / runner versions:
Artifacts and evidence paths:
Core guard / FC compatibility / L0 comparison:
Adoption scope and authorization already in session:
Actual running generation (if adopted):
Unverified / failed / blocked items and impact:
Next concrete action:
```

不以“测试代码存在”“本地打桩成功”“源码已提交”替代执行记录。设计批准、源码合入、产物构建、运行采用和真实前端操作是不同事实。

## 5. 本次文档交付的验证记录

- 9 份 Markdown；104 条有编号的 PRD 需求；57 个统一 CASE；16 个 FC 交互场景；12 个 Windows 施工包与 34 个 WAC。行为用例全部保持待执行。
- 114 个相对链接和 101 个固定 SHA 源码链接的目标及行范围已核查；拟新增路径明确是 proposed，不伪装现有文件。
- 7 个 JSON 示例解析通过；其中 2 个 generic operation 通过现有 `command_interactions.validate_operation`，没有增加公开 FC 字段或版本。
- 33 个现有测试文件及 4 个完整 pytest node 已核查存在；没有运行这些行为测试。候选新增模块与测试路径在计划/映射/验收间统一。
- `git diff --cached --check` 通过；protected Core 检查在 cached 和固定基线比较下通过；暂存变化仅位于 `docs/permissions/`。
- 已对 Task/Run/ExecutionEpoch、批准计时、过期重提、暂停/停止后的晚批准、排队后验权、Windows准备与start、混合L0凭据边界做交叉审阅并修正文字差异。

本节只证明文档和源码引用一致，不证明尚未开发的权限功能满足以上 CASE/WAC。

## 6. 最终发布前必填

- [ ] 对外宣称的每项控制都有 CASE/WAC 证据。
- [ ] L0行为与冻结基线等价；原有privacy/enterprise行为保留。
- [ ] FC公开schema、外部前端和Telegram专属适配无改动。
- [ ] Protected Core guard通过；Core依赖和运行实例未被功能施工改变。
- [ ] 后端能力矩阵、已知partial/unknown和具体原因准确。
- [ ] 到期/撤销/停止/切换/恢复均能追到operation与资源结算。
- [ ] 发布与回退流程在合适的隔离环境验证。
- [ ] 源码/构建/采用/实时验收分别报告，未授权或未做的操作明确未做。
