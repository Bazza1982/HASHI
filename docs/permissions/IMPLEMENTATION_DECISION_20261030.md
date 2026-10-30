# 权限系统实施决定（2026-10-30）

本决定记录 feature-permission-sys 的实际实现选择，不授予部署、Core 迁移或降低验收门槛的权限。功能 owner 为 PAO；HERV3 保留真实循环和计费；公共 FC 负责原始身份与显示。采用和用户实测另记。

## 固定执行与失败语义

正常文件、搜索、补丁和 HTTP 使用固定 helper，先建立实际对象/目标，再完成单一 authority 的最终 start 栅栏。脚本 helper 本身不执行用户代码；它只制作输入快照并调用已安装的 Runner。Python 用户代码进入独立 LPAC profile，原子加入 Safety/helper/脚本 Job；只允许一个脚本进程，使用实际读取核对的内存限制。

Windows Python profile 只包含固定的 `registryRead` capability，满足 SxS 运行库的只读系统注册表依赖。执行前核对真实 token 中 capability 的完整 SID 集合；未知、多余、缺失或仅有 count 的观察均拒绝。它不包含网络或注册表写 capability，不修改系统注册表 ACL。环境中的用户目录来自当前系统 token；不继承调用者提供的凭据、代理、导入路径或用户目录别名。

run_script 的正式接口是源码、明确输入及临时输出名称。实际输入文件不在脚本进程内开放；输出回传后保存正式成果仍走文件代理。当前 profile 不支持任意 shell、进程树、隐含联网、原始凭证或安装依赖。目录大小监测是软限制，不称硬配额。

已确认尚未执行的文件绑定失败，必须在所有初始资源关闭后记录 authority-owned prestart failure；不创建假 claim，不消费 once。其摘要与加密结果可核对恢复。已经创建 claim、可能 ResumeThread、部分写入或无法确认清理的错误仍保持 unknown/待对账。权限数据库版本 8 的迁移是一个事务，原有决定和 lease 不改写。

HTTP 的域名、方法、DNS 固定与连接验证不构成禁止外传证明；调用者可在 URL 路径携带信息，因此 GET/HEAD 也需要 send。具名凭证由安装配置限制精确 HTTPS origin、方法和允许响应字段；秘密不进模型、日志或返回体。

## 原 owner 与实际状态

受限 HERV3 通过闭合的 Run 回调保留原 Persona、草稿、TurnServices、习惯、Meditation、压缩和 HChat 包装；不开放动态函数或路径加载。附件只来自本轮原始接收清单，大小/摘要核对后分块传输。Safe Voice 确认仍留在实际 Worker；TTS 只接收本轮权威请求。模型音频成果交给原资产 owner，绑定原用户和会话，避免随 host scratch 退役而丢失。

服务在每个实际 HTTP 请求前保存未知 intent；响应后保存观测。host 和 Worker 的计费投影都先保存固定字段再交给原 observer，使用稳定物理 ID。跨 epoch 不重发旧请求推测结果，不将未知费用写成零。

实际控制由当前绑定、实际工具库存、native host 观测、服务代次和期限推导。第三方后端不被全局禁用；明确选择时先停止旧 PAO 执行，再显示 unmanaged。旧资源未确认时保留记录，其他独立任务仍可工作。审批环境与未受管 Agent 的隔离没有实体证据时，L2 的审批及依赖控制保持 partial。

## 认证与安装

Backend API 使用独立人类登录；共享应用凭据不能代替人类。原生命令通过既有共享解析器识别原始 transport Update，且必须命中安装时明确关联的外部 actor/人类用户/PAO owner；合成兼容/API context 不能自动登录。短期原生命令会话只在私有 FC 面签发，结束后撤销，并有固定短 TTL。没有修改 Telegram 专用 connector 或外部客户端，也没有新增公开 FC schema/widget。

L1 的服务代码属于可替换 Functions，按明确安装描述接入；不要求独立 Runner，普通未提升用户可用，不宣称能抵抗 HASHI 自身被篡改。L2 则要求独立 Safety/Runner/Worker 身份、封闭代码与受限 host，且必须另有平台资格证据。所有入口显式启动，import 和 L0 不启动服务。

批准、传输、监督和结果密钥必须使用不同路径及不同内容；Runner 的脚本结果密钥也与传输密钥分开。密钥、身份库、私有安装和机器路径仍属于忽略的实例配置，不能进入共享源码。

## 基础权限与申请上限（PR #118 P1 修复）

安装策略将 `baseline_scopes` 与 `requestable_scopes` 分别传递到实际工具注册及执行门。两者按资源的单个 action 核对执行资格，但只有实时 authority 的基础授权或有效 lease 才能准许执行；静态资格不是授权。新增批准始终仅针对申请上限内缺少的 action，不把基础范围并入申请上限。主动 `request_access` 对仍有效的基础权限返回 `granted_existing`。工具名单、受保护资源、当前策略摘要和最终 start 栅栏继续独立拒绝不合规动作。

基础范围及申请上限均显式进入策略摘要。排队、prepare 后的策略改变，以及最后 start 前撤权，都必须在实际文件写入前停止。18 项组合用例通过真实安装策略、factory、registry 和 authority，仅注入原生 launcher/IPC 与后端配置输入：同组测试在原 HEAD `6756cf43` 上为 8 failed / 10 passed，修复后为 18 passed；覆盖基础独有、申请独有、重叠、分 action、禁止范围及策略/撤权变化。此结果不替代 Windows 隔离或完整服务采用验证。

## 验证与采用

以单一完整提交交付代码、测试、文档和资格配方。源代码、原生实体资格、服务采用和用户前端验证分别报告。Windows 原生脚本资格已在独立普通测试账户通过：原文件读取/写入、子进程和 TCP/UDP IPv4/IPv6 均被阻止；输入快照、临时输出、低完整性目录、原子 Job 与回收通过。此为实际脚本入口/launcher 的组件资格，不能代替完整安装的 WAC 或用户前端验收。完整 WAC 和发布门未通过前，不宣称整体 L2 已验证或运行中采用。当前事实及历史失败见 [状态](IMPLEMENTATION_STATUS.md)。
