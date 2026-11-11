# L2 本地关键链原生验收：2026-11-05

主责 PAO；协作 HERV3。工程层为 Functions、独立 Windows 服务与受限运行包。
用户批准保留 L1，先完成 HERV3 的本地硬 Workzone，并分配独立 Intel Windows
测试机。唯一开发源沿用 HASHI3 既有 main；没有 Core major migration、生产发布
或扩大到独立 backend／Compute 执行端的授权。

## 结论与版本

本地关键链的开发及本轮组合实测通过；这是可审阅的非生产里程碑，**不是
全部 WAC、外部前端人工验收或生产上线结论**。

受测完整源码为 `ce3d63b0eb51aae44b347f7a08ece043ef0bdac7`，包含代码、测试
与当时文档。Intel 上部署完整 sealed capsule 和同提交的完整源码；可信
Core/shared/Worker 使用单独的同版运行环境，避免其正常 bytecode 缓存改变
sealed 执行包。没有向运行中的 Core 安装依赖，也没有削弱运行包完整性检查。
后续证据文档提交不能冒充同一次原生受测提交；产品代码变化必须另验。

只有模型推理使用确定性合成 transport；SCM、独立非管理员账户、LPAC token、
Job、私有管道、文件、HTTP、HERV3 引擎及普通 Core/shared/Worker 都实际运行。
通过条件读取操作记录、实际文件、进程状态、输出摘要和接收端命中，绝不根据
合成模型的“完成”文字判断。

## 同版证据

原始回执保存于本轮忽略的资格证据目录；机器路径和私有安装配置不进仓库。
下表用稳定的回执名称索引，独立汇总为 `ce3-final-native-observed-summary.json`。

| 验证 | 实际结果 | 回执 |
| --- | --- | --- |
| 完整权限及直接消费者 | 728 passed，无 skip | `windows-ce3-scope.xml` |
| 主要运行回归 | 881 passed；1 项 POSIX 文件模式在 Windows 跳过，Windows DACL 另验 | `windows-ce3-core.xml` |
| 普通 Worker 真实路径 | 公开 Session API，经真实 Core/shared/Worker 到 L2，区内文件写入、原 owner 审计和资源回收成功 | `combo-r55-ordinary-worker-proof.json` |
| 正式批准与原任务续跑 | API 凭据单独拒绝；实际测试人类账户登录后经公开命令批准，同一命令重放复用结果；原 Run/Task/operation 保留，新 epoch 仅执行一次；1 决定、1 成功 claim，结束撤权及资源停止 | `combo-r57-lifecycle-proof.json` |
| OS 与路径边界 | 11 场景；36 项实际越权拒绝，服务控制、进程访问、私有文件／管道／桌面、环境、额外子进程和直连网络均受限；声明输入输出正向成功；3 种路径别名拒绝 | `combo-r59-observed-perimeter-proof.json` |
| 模型与网络通路 | 未隔离的本机 CLI 在 Driver 创建前拒绝；声明 HTTP 成功，未批目标、自动跳转和 POST 上传没有到达禁止的接收端 | 同上及 `combo-r59-network-proof.json` |
| 真实 TTL | 原批准续跑的 12 秒授权到期后约 0.575 秒，host 和脚本均停止；lease revoked、资源 stopped、Task cancelled/settled；中断操作保留 unknown，不重发 | `combo-r61-ttl-proof.json` |
| Safety／Runner 故障 | 分别故意终止精确自有测试服务；host／脚本约 0.03 秒停止；同一注册恢复新 PID，后续新任务成功；原任务取消并收尾 | `combo-r63-lifecycle-safety-crash.json`、`combo-r63-lifecycle-runner-crash.json` |
| Workzone 变更 | 停用后再启用同一路径，revision 递增，旧 epoch／脚本不复活；后续新任务成功 | `combo-r65-lifecycle-workzone-revision.json` |
| 删除用户 profile 前的任务资源 | 47 份 Runner 账本、122 条 retired 记录，无 active、无遗留 scratch、无测试服务／执行进程；先证明任务回收，不能靠删除整个用户目录掩盖泄漏 | `intel-pre-maintenance-audit-r58.json` |
| 安装身份的延迟回收 | 原两账户及 SID/Classes hive 的占用独立保留；正常 Windows 维护后调用现有 recover 接口，账户、文件、注册和双 hive 均确认不存在 | `intel-profile-post-maintenance-r66.json` |
| 操作者收尾 | 一次性维护任务及两份私有传输凭据移除；按精确测试程序处理防火墙记录；测试服务为空，原服务运行、Remote ready，原源码及三份未跟踪成果保留 | `intel-standalone-cleanup-final-r71.json` |

R57 的原观察脚本在异步收尾落盘前检查 Task 状态，原报告仍为失败。随后只读
对账和独立最终条件验证确认完成，没有重新执行业务。早先暴露配置读取权、
Provider 恢复参数及审计接口问题的失败也全部保留；不是删除失败后宣称全绿。

## 本轮修复与保证

实体路径补齐了服务态 LPAC 的最小私有桌面、容器管道映射和原生 token 解码；
固定离线异步运行无需网络 capability。脚本心跳遵循原事件合同；host 审计按
执行 epoch 回原 canonical owner，owner 失败则停止，不改用本地 spool。

普通 Worker 启动和根配置发布为已安装 Safety SID 保留只读权，候选文件在写入
内容前即具正确权限；秘密文件不共享，冲突和已提交错误不重试。L2 呈现模型
按真实 Provider 合同清空私有恢复上下文，恢复快照仍交给受限 host；审计回调
使用真实 DurableAuditLog 的具名字段接口，保留 canonical 先写、脱敏和去重。
上述缺口都有聚焦 red/green 回归；L1 对照及原行为保留。

硬 Workzone 继续按 PAO 的精确根收窄基础与可申请范围；普通审批不能扩根，
文件工具、脚本输入和保存共用边界，不支持的 shell／内部 CLI 不转回宽权限执行。
完整保证仅属于实际经过验证的 HERV3 执行组合。

## 维护和发布边界

两名服务用户的 profile 被 Windows 持有时，禁用账户不是卸载成功。原组件
`pass=false` 不修改。独立 Intel 的正常 OS 维护及延迟 recovery 另取回执；没有
强卸载 hive、关闭未知系统句柄、重启 Profile Service 或把服务改成 SYSTEM。
SYSTEM 只执行固定的一次性操作者清理任务。原 Intel 源码和原未跟踪成果保留，
原自动服务的恢复情况另核实；A9_MAX 上历史 profile 阻碍不因此自动消失。

该维护结果不能改写为“服务停机后即时卸载通过”。常规任务的 Job、桌面、
AppContainer 和 scratch 回收在 OS 维护前已独立验证，二者须分别报告。

发布门仍依 [Windows WAC](L2_WINDOWS.md) 和 [ACCEPTANCE](ACCEPTANCE.md)；本轮
没有逐项覆盖升级回滚、并发跨任务攻击、全部时钟／备份恢复及外部前端条件，
不能将对应表项全部改为通过。原生组合中的合成模型 transport 也不代表真实
供应商、长期运行或所有模型工具都已验收。独立 backend、WSL／其它系统及
Compute 上实际执行资源的服务需各自管制和联调。Compute 仅推理、工具仍在
本地时，继续由本地边界约束，不需要据此新建远端授权权威。

Core 来源只取 `CORE_SOURCE_PATHS`；本轮最终摘要确认与开工的九份文件摘要
相同，保护检查包含当前差异及相对 origin/main 的完整差异。源码提交、完整
原生运行包、正常 HASHI3 Worker 的实际采用、GitHub、生产和用户手测分别记录。
本轮源码仅本地提交，没有 push／PR／合并／生产采用。
