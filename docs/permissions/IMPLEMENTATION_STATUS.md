# 权限系统实施与证据状态

更新日期：2026-10-30。功能主责 PAO；执行协作 HERV3，认证及显示协作公共 Frontend Connector。施工仅在既有 HASHI3 权限工作区及 feature-permission-sys 分支。Core 与外部客户端边界不变。

本期源码已完成权限服务、真实 HERV3 接入、公共认证/审批、受控工具、媒体/原服务、计费与恢复接线。Windows 实体脚本的启动问题已经解决并通过组件资格。当前提交的最终回归、GitHub 检查及审阅状态以 PR #118 的最新提交为准；旧提交的绿色检查不能代替新版本。

完整安装的 Windows WAC、实际前端验收及生产采用仍是独立交付事实。未完成这些组合验收的环境继续显示 partial/unknown，不显示整体 L2 已验证。此前分批结果与失败记录见 [历史](IMPLEMENTATION_HISTORY_20261030.md)；原始产品要求仍以 [PRD](PRD.md)、[ACCEPTANCE](ACCEPTANCE.md)、[Windows L2](L2_WINDOWS.md) 为准。

## 当前实现

| 部分 | 实际行为 |
| --- | --- |
| L0 | 缺省及显式 L0 在最外层保留原执行路径，不创建权限服务、账本、租约或新工具；既有隐私与其他防护保留。 |
| 权限权威 | PAO 单一 Task/Run/Turn/epoch；服务持有决定、范围、期限、once、派生、撤回与撤权事务。旧执行先失效，未知副作用保持原 operation ID。 |
| 基础权限与申请上限 | 安装策略分别保留两种权威；基础已授权操作直接执行，缺少的 action 只在申请上限内批准。主动申请返回 granted_existing，当前策略与最终 start 仍重新校验。 |
| 人类批准 | 独立人类登录与明确用户/PAO owner 关联；公共 /safety 及受签 action。原生命令必须关联原始外部身份，合成 API actor 与普通回答不授权。 |
| 原运行续跑 | 现行 HERV3 singleloop 与精确 provider checkpoint；原 Run park，批准后新 epoch 恢复原待执行动作。服务故障不切到无约束执行。 |
| 文件与 HTTP | 实际 Windows 文件对象绑定，固定 helper 与最后 start 栅栏；目录、搜索、严格补丁。HTTP 固定 DNS/目标，无自动重定向、环境代理或 cookie；具名凭证独立代理，GET URL 同样声明 send。 |
| 结构化脚本 | 固定输入快照、离线 Python、临时输出。用户代码在独立 LPAC，原子纳入 Safety/helper/脚本 Job；实际进程/内存限制、独立加密结果及 profile 收尾。 |
| Python 原生环境 | 仅固定 registryRead，满足 Windows SxS 只读注册表依赖；真实 capability SID 集合必须完全匹配，没有网络 capability。系统用户目录从当前 token 获取，排除调用者秘密、代理及导入路径。 |
| 原服务与媒体 | 原 Persona、草稿、习惯、Meditation、TurnServices、压缩及 HChat 包装继续由原 owner 处理。附件只取本轮原始接受内容并核对摘要；语音确认及音频成果回到原 owner/资产 store。 |
| 物理用量 | 每次实际 HTTP 前持久化 intent，响应后记录观测；host 与 Worker 物理 ID 稳定，计费投影先持久化再调用原 observer。恢复不重发请求推测旧用量。 |
| 兼容与实际控制 | 明确选择兼容后端时先停止旧 PAO 执行，再继续 unmanaged；失联清理保留待确认记录。当前绑定、实际工具、原生观察、服务代次及期限生成控制状态，并传播依赖不足。 |
| 安装入口 | 显式独立 Functions/sidecar、封闭完整源码/依赖、分离密钥与单写锁；import/L0 不启动服务。L1 不要求独立 Runner，L2 保持独立普通账户和受限 host。 |

## 必要行为验证与范围

- 完整服务、真实私有 pipe、SDK 与现行 HERV3 的组合闭环通过：原 Run 缺权暂停，可信人类批准，新 epoch 继续同一操作，真实临时文件只写一次。确定性模型 transport 和原生文件 launcher 在此用例注入，不能据此宣称完整安装 L2。
- Windows 实体脚本组件通过：实际普通账户、LPAC、固定 registryRead、低完整性任务目录、原子 Job 和 128 MiB 可观测内存限制。原文件读/改及子进程被拒绝；TCP/UDP IPv4/IPv6 四项均被系统拒绝。输入快照、临时结果与原文件不变通过。所有 Job、profile、账户、用户配置及 staging 清理已确认。
- 实体失败路径保留证据：最初 SxS 运行库加载缺少注册表读取，固定诊断捕捉到加载错误；系统用户环境与低完整性标签的缺口也已修复。扩大只读代码 ACL 的试探无效，未保留。正式通过使用无调试器进程，不能把调试诊断当资格通过。
- 文件启动前失败已做 red/green：没有 claim、不消耗 once；初始资源关闭后保存 authority-owned 失败结果，可恢复且不伪造 started。已准备、已开始或清理未知不能走该分支。
- 附件变动/任意路径拒绝、原语音确认、TTS 权威文本、原压缩/草稿包装、真实音频资产 owner 均有直接用例。
- 共享原生命令通过真实私有服务批准；缺少身份关联、合成 API actor、PAO 面伪造登录及旧 Session 均拒绝。
- Worker 用量先持久化、跨 epoch 保留原阶段/ID；脚本原结果在 profile 清理失败时保留；旧物理行失败不跳过其他行；已完成旧快照不依赖已删除 plan。
- 新增固定 capability 后，额外网络 SID、被替换 SID、旧零 capability profile 和 bool 伪装 count 均在执行前拒绝。
- PR #118 P1 基础权限回归：同一组 18 项安装策略→factory→registry→authority 集成用例在 6756cf43 为 8 failed / 10 passed，修复后 18 passed。覆盖基础独有、申请独有、重叠、分 action、禁止范围；入场/排队/prepare 后改策略及最终 start 撤权均无文件副作用。原生 launcher/IPC 与后端配置输入为注入边界，不宣称 OS 组件资格。
- Windows CI 捕获旧恢复用例在结果核对阶段超过 1 秒 watchdog；同一提交的另一套 Windows 检查及本地组合通过。该恢复 helper 的 watchdog 调为 5 秒，仍保留授权、结果摘要、epoch 栅栏及只执行一次的全部断言；产品期限和超时不变。
- CI 范围来自 permissions workflow 的完整 permission/直接旧合同组合；共享运行接线另跑仓库规范 curated core gate。Core 保护和 whitespace 必须通过。不默认跑全库、不用 mock 宣称 OS 通过，不将重叠分组数量相加。

## 当前 profile 与采用边界

当前 profile 提供文件、结构化 HTTP/具名凭证、离线 Python 和受限 HERV3。任意 shell、依赖安装、未评定设备/浏览器/桌面、自定义远程工具不能通过服务静默逃逸；缺少的通道显示 unavailable。脚本仅收到文件输入副本，正式保存仍经文件代理。临时目录监测是软限制，不是磁盘硬配额。

混合部署中，审批桌面/凭据与未受管 Agent 的隔离没有实体证据时，审批及依赖控制保持 partial。固定 registryRead 允许平台共同注册表读取，不授予 registry write、网络或其他账户的受保护对象；业务/审批密钥仍由不同服务身份及私有文件/通道隔离。

| 交付事实 | 核对位置 / 状态 |
| --- | --- |
| 本地完整提交 | 原 feature-permission-sys 分支，代码、测试、文档整体交付；以最终 HEAD 为准 |
| GitHub 收到与 PR 可审阅 | 以 PR #118 当前 head、检查及 draft/ready 状态为准 |
| Windows 脚本入口/launcher 组件 | 已通过；证据保留实际 SID、OS、Python 版本、逐文件摘要和清理结果 |
| 完整独立安装/WAC 及前端组合 | 尚未在当前实例执行，保持发布门 |
| 生产源码同步 | 未进行 |
| 运行服务/Worker/页面采用 | 未进行 |
| 用户实际前端验收 | 未进行 |

没有启动或重启 HASHI 实例，没有安装持久服务，没有修改生产源码。一次性资格进程和测试 IPC 不等于运行中采用。详细取舍见 [当前实施决定](IMPLEMENTATION_DECISION_20261030.md)，源码关系见 [CODE_MAP](CODE_MAP.md#map-11--当前实现与边界2026-10-30)。
