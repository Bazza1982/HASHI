# 本地开发 agent 一次性交接指令

将以下整个代码块交给位于 HASHI checkout 的本地 agent。它会从本分支的设计包开始实施；本文件本身不表示功能已经开发完成。

```text
请接手 Bazza1982/HASHI 的权限系统开发。目标分支 feature-permission-sys；设计包在 docs/permissions/。本次任务是按设计完成实现与验证，不是再写一份计划或做一个演示。

先检查当前 checkout、HEAD、分支及已有修改，保留其他工作；仅在指定的 HASHI3 及获准分支施工，新分支或额外 worktree 须有当前用户明确授权，不得通过脱离分支提交绕过限制；同一仓库保持单一写入者。读取根 AGENTS.md、ARCHITECTURE.md、分层边界、TESTING_POLICY、COMMAND_UI_STYLE_GUIDE，以及 docs/permissions 下的 README、PRD、IMPLEMENTATION_PLAN、CODE_MAP、FC_CONTRACT、L2_WINDOWS、ACCEPTANCE、IMPLEMENTATION_STATUS。设计参考基线是 d6b6513973bfc8f9f22c40ddd8f72802f907baa0；若当前分支已有新提交，先核对差异，不 reset 回旧基线。

按 IMPLEMENTATION_PLAN 的 M00–M10 持续完成工作；每批写清 owner、工程层、行为变化和针对性验证，分阶段提交，并更新 IMPLEMENTATION_STATUS。独立的只读核对可以并行；实际源码、测试和文档修改必须保持单一写入者。不要在完成骨架、文档或单个 happy path 后就结束；遇到平台验证缺口，继续完成其余可实施部分并准确记录待验证项。

不可变约束：
1. 改动只在 HASHI。外部 Workbench、Telegram 专用 connector/adapter、其他外部客户端均不改；FC 指 Frontend Connector。审批使用已有 command/action/text/event/snapshot，不新增公开 FC schema、字段、action kind 或专用审批控件。历史命名 workbench_api.py 属 HASHI Backend API Functions，允许必要的公共认证接线。
2. Protected Core 不改。唯一保护集合来自 orchestrator.runtime_contract.CORE_SOURCE_PATHS。选路径、每批结束和最终都运行 scripts/check_protected_core_changes.py；不得使用 --authorized、授权环境变量、修改名单或改 Core 依赖绕过。共享状态归可替换 Functions，Windows native 依赖归独立环境。
3. PAO 是任务/生命周期 owner，权限服务是唯一授权账本。HERV3 只在实际 DIRECT 连续工具循环处理等待/续跑；PCM和FC只投影。L2 使用独立服务、受限HERV3 host和窄broker，sandbox不得取得原wide Functions pipe或批准凭据。
4. L0 必须真旁路：无安全字段和明确L0保留旧prompt、工具目录/schema、执行、重试/超时、生命周期和旧privacy/enterprise规则；不得初始化新权限DB/service/runner，不采用allow-all新服务。L2安装不能静默改变L0的ACL、环境或网络。
5. 临时批准仅Once、Run、Task、明确时长；任务提前结束即收回，无普通永久批准。PermissionTask可跨Run；Run是PAO接纳一条Message的执行，不能等同进程。execution_epoch、worker_generation、context_generation及业务revision分开。
6. 缺权可正常申请；获批恢复同一operation，保留成果、不重发用户Message、不重复执行副作用。排队后、真正dispatch前再次原子验权；unknown先对账。等待不空转模型、不算普通工具失败/fallback，且可park以完成Functions热采用。
7. 正式批准须绑定request revision/scope和可信服务器DecisionReceipt。普通ask_user的“同意”不授权；owner字符串、localhost、instance HMAC或agent共享secret不证明人类。无可信批准能力的现有通道继续普通工作和申请，通过另一已连接可信FC处理；不能伪造身份。
8. 暂停/停止/切后端后的晚批准不复活旧工作；旧worker退出只清旧epoch。先阻止新用权，再清理handles/connections/jobs；“权限已撤销”和“资源已停止”分别报告。不能靠删除账本行宣称完成回收。
9. 用户切换到无法完整管理的后端时，具体说明actual_controls并继续，不新增兼容硬阻断。拒绝、撤销或等待权限不能自动触发更宽后端规避。既有privacy和数据接收方限制保持。
10. L2必须用真实Windows证据验证身份/ACL/IPC/文件/网络/进程/凭证/审批隔离和独立监督。仅另起service、仅shell沙箱、mock通过均不足以宣称完整L2。同机L0或桌面若破坏审批信任，隔离L2控制平面或报告partial，不能改变L0承诺。

测试以ACCEPTANCE和L2_WINDOWS为依据，优先相关既有测试及必要新行为/并发/恢复测试。对L0比较冻结基线输入与调用路径；对排队撤权、一次授权争用、晚批准、unknown副作用、hot adoption和旧epoch退出使用可控barrier。平台测试记录实际OS/service/runner版本、正向正常工作、负向拒绝和清理证据；不要把测试文件存在当通过。

完成开发、构建、离线验证及可执行的隔离Windows测试服务验证。生产安装/升级或正在运行实例的采用只在本次会话已有明确对应实例/代理/范围授权时执行，沿用既有hot reboot范围；不自行冷启Core、扩大到全实例重启或停止客户工作。某项采用未授权不妨碍先完成其余实现与验证。没有运行采用就明确报告未采用，不冒称已生效。

遇到具体设计与源码冲突，给出文件/符号证据，在这些边界内修正实现和对应文档；不要为赶进度放宽需求、删除验收、增加客户端改造或修改Core。遇到确实无法完成的环境条件，明确列出已完成、未验证、原因和可执行的后续步骤，不以占位实现伪装完成。

最终交付：有意义的分阶段提交、源码与必要测试、更新后的设计/状态/拥有决策的文档、CASE/WAC实际证据、准确后端能力矩阵、安装升级回退说明。汇报源码实现、构建、运行采用和实时验收四类事实，以及仍未覆盖的能力。默认不merge、不擅自发布或改外部前端。现在从M00开始，持续推进。
```
