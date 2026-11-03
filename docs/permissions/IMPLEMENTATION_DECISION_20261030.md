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

## 个人 L1 一键审批（2026-10-31 用户修订）

用户明确要求：在个人 L1 中，将现有前端的按钮点击视为用户决定，不增加重复登录、身份验证或手打命令的步骤。本次修订覆盖此前“外部客户端不改”和个人 L1 必须另行人类登录的限制；governed 与 L2 的身份及隔离规则保持原合同。

功能主责 PAO，交互协作 Frontend Connector，工程层为 Functions 与外部前端。现有连接仍走原认证；私有 FC 根据当前精确 Session、owner 与 L1 策略取得短期操作上下文，结束即撤销，不把会话凭据交给模型或浏览器。L1 是协作式权限控制，不据此宣称 OS 隔离或 L2 资格。

Workbench 只读查询当前 Session 的待批申请，自动弹出简短范围和已有通用命令按钮。批准一次、本任务、策略上限内的时长及拒绝均调用既有服务器登记的 action；不把普通问题答案当批准，不复制租约状态。稍后决定只收起窗口，待批入口继续保留。未知结果先刷新，禁止自动重放决定。申请改版、终结、过期、跨 Session、跨连接和重复点击仍由原权威核验。

只读发现接口是现有 Session 的权限投影，不改 Run schema 或权限账本；具体决定仍走通用 command interaction。短时长按钮由现行策略上限派生，不增加可申请范围。实际页面验收与生产发布分别记录。

## 认证与安装

Backend API 使用独立人类登录；共享应用凭据不能代替人类。原生命令通过既有共享解析器识别原始 transport Update，且必须命中安装时明确关联的外部 actor/人类用户/PAO owner；合成兼容/API context 不能自动登录。短期原生命令会话只在私有 FC 面签发，结束后撤销，并有固定短 TTL。没有修改 Telegram 专用 connector 或外部客户端，也没有新增公开 FC schema/widget。

L1 的服务代码属于可替换 Functions，按明确安装描述接入；不要求独立 Runner，普通未提升用户可用，不宣称能抵抗 HASHI 自身被篡改。L2 则要求独立 Safety/Runner/Worker 身份、封闭代码与受限 host，且必须另有平台资格证据。所有入口显式启动，import 和 L0 不启动服务。

批准、传输、监督和结果密钥必须使用不同路径及不同内容；Runner 的脚本结果密钥也与传输密钥分开。密钥、身份库、私有安装和机器路径仍属于忽略的实例配置，不能进入共享源码。

## 基础权限与申请上限（PR #118 P1 修复）

安装策略将 `baseline_scopes` 与 `requestable_scopes` 分别传递到实际工具注册及执行门。两者按资源的单个 action 核对执行资格，但只有实时 authority 的基础授权或有效 lease 才能准许执行；静态资格不是授权。新增批准始终仅针对申请上限内缺少的 action，不把基础范围并入申请上限。主动 `request_access` 对仍有效的基础权限返回 `granted_existing`。工具名单、受保护资源、当前策略摘要和最终 start 栅栏继续独立拒绝不合规动作。

基础范围及申请上限均显式进入策略摘要。排队、prepare 后的策略改变，以及最后 start 前撤权，都必须在实际文件写入前停止。18 项组合用例通过真实安装策略、factory、registry 和 authority，仅注入原生 launcher/IPC 与后端配置输入：同组测试在原 HEAD `6756cf43` 上为 8 failed / 10 passed，修复后为 18 passed；覆盖基础独有、申请独有、重叠、分 action、禁止范围及策略/撤权变化。此结果不替代 Windows 隔离或完整服务采用验证。

## L1 模型初始化（2026-10-31）

普通权限服务的真实采用暴露了模型工厂与 Adapter 合同不一致：`IDLE_TIMEOUT_SEC` 是只读配置视图，不能直接赋值。工厂通过既有 `apply_timeout_layers` 将当前 HERV3 空闲期限写入 Adapter 配置，保留其余私有传输选项，并在配置或初始化失败时关闭已经创建的 Driver。权限范围、审批权威和 L0 路径不改变。

回归使用真实的 BackendManager、DeepSeek Adapter 及客户端生命周期，不用可任意写属性的替身证明初始化；覆盖有凭据时的初始化/关闭，以及缺少凭据时的失败关闭。这是 Functions 修复；完整 L1 前端组合验收仍独立记录。

## 续跑时的运行日志（2026-10-31）

L1 续跑保留原 Run、Turn、工具调用及 operation 身份，但新的 ExecutionEpoch 会重新装配 PCM，因而产生新的运行事实。运行日志在权限接线处绑定实际 epoch；运行及 Provider 的日志共同使用同一个带 epoch 的视图，继续复用原持久写入者、去重集合与锁。审计事件明确携带 epoch，而物理 Provider 请求和工具 operation ID 不变。

原 SQLite 不可变事实检查保持不变：同一 epoch 的同一事件编号若对应不同事实仍然拒绝。组合回归接入真实 canonical observer，覆盖未变化及已刷新的上下文、可信批准后原 pending batch 续跑、一次真实临时文件效果及重复事实冲突；模型 transport 和原生 launcher 仍是该回归的注入边界，实体 L1 结果另记。

## 验证与采用

以单一完整提交交付代码、测试、文档和资格配方。源代码、原生实体资格、服务采用和用户前端验证分别报告。Windows 原生脚本资格已在独立普通测试账户通过：原文件读取/写入、子进程和 TCP/UDP IPv4/IPv6 均被阻止；输入快照、临时输出、低完整性目录、原子 Job 与回收通过。此为实际脚本入口/launcher 的组件资格，不能代替完整安装的 WAC 或用户前端验收。完整 WAC 和发布门未通过前，不宣称整体 L2 已验证或运行中采用。当前事实及历史失败见 [状态](IMPLEMENTATION_STATUS.md)。

## Windows SCM 生命周期（2026-11-03）

用户已明确批准在既有 HASHI3 接手 L1 收尾和 L2 继续开发，配套 NP Workbench 连接 HASHI3。归属 PAO Functions／Windows sidecar；Core、生产及原权限权威不变。原 Safety／Runner 安装读取器继续是身份、封闭 runtime 和私有数据的唯一装配入口，新 SCM host 只管理其真实就绪与停止，不复制策略、端点或权限状态。

前台信号与 SCM 生命周期分开：SCM 回调线程使用明确停止事件；只有实际端点就绪后报告 RUNNING。checkpoint 不依赖应用事件循环；停止或状态报告失败不能被迟到就绪覆盖。未确认关闭资源时保留失败语义，SCM 非零退出，不能声称已干净回收。安装和运行失败必须分别取证。

新测试注册辅助器只操作 manifest-owned 独立服务和新普通账户，不接管既有注册；身份变化、配置冲突和已提交耐久性错误先保全对账。配方、注入边界及完整 L2 尚需条件见 [SCM 组件资格](WINDOWS_SERVICE_QUALIFICATION.md)。组件与完整安装／用户前端验收分别记录。

实体 SCM 探针确认四场景预期及服务自管拒绝，但主机 User Profile Service 仍持有两个新账户 hive，正常配置回收未成功；整轮组件资格不通过。新失败策略仅禁用 manifest／SID／marker 仍匹配的测试账户并核实结果，保留 profile、所有权和失败，不对替换账户操作，不将禁用冒充卸载成功。原生服务、进程和新增 service-logon right 已清理。不得为重跑而继续在同一共享主机制造此类 profile；完整安装 WAC 推荐独立可维护 Windows 测试环境，不擅自重启或改动共享系统服务。详见组件资格的实体结果。


## 发现接口的未采用状态（2026-11-04）

实际 HASHI3 复验中，权限服务已保存有效待批申请，期限未到、0 claim、无文件效果；共享 API 在未配置服务时启动，permission_control 尚未绑定，因此此前返回了正常空列表。安装描述保存与 shared owner 的实际采用必须分别处理；不在只读发现请求中启动服务或替换 shared。

Frontend Connector 的 Functions 投影现在在控制器为空时，使用原配置读取者检查当前持久配置。已保存 permission_service 但尚未加载，或配置无法可靠读取时，返回原有 503／permission_requests_unavailable；未安装的实例保持原只读空视图。身份与 Session／generation 检查仍在配置读取之前，已绑定控制器仍走原权威。不改变批准、范围、期限、重放或 Run 续跑规则，也不把普通问题答案当授权。

同组实际 API／配置／权威回归：旧代码 3 failed／6 passed，修复后发现与精确点击相关 14 passed。无 controller 的失败状态保留有效 pending 和 0 decision，不创建假空权威。H3 首次正常 /reboot max 被忙碌保护拒绝，未强制切换；随后空闲窗口实际采用与页面结果另见本轮状态记录。

## NP 实测与收尾边界（2026-11-04）

个人 L1 的决定仍走已登记前端按钮与原权威。当前 HASHI3 的真实 NP 页面验证批准一次、本任务、10 分钟及拒绝四条路径，核对同一 operation 的真实文件效果、成功 claim 与 Task 终态撤权；页面刷新后重开原 Session 仍发现 pending。10 分钟从批准提交精确计时 600 秒，本轮只验证任务提前结束的回收，不把它写成自然到期实体测试。原 Run 续跑成功，没有为测试改写运行、授权或物理用量状态。

临时服务、测试 Worker、配置和目录 ACL 均单独确认关闭／恢复。shared API 曾保留启动时 controller，因此配置撤回后仍需同一标准 Functions 采用路径刷新；等待空闲，遵守忙碌保护，不以只读发现、任意属性注入或 Core 冷重启代替采用。恢复后未安装状态返回原 200 空视图。

这是指定 HASHI3 与 NP Workbench 的证据，不能传播成生产、其它实例或完整 L2 的授权与验收。SCM 的遗留 Windows profile 继续保持失败与所有权记录，两个账户已禁用、rights 已撤除；完整安装推荐可维护的独立 Windows 测试环境，不能在同一共享主机反复新增账户或强行操作系统服务。
