# Windows 权限服务：SCM 入口与组件资格

功能 owner：PAO；工程层：可替换 Functions／Windows 平台 sidecar。Core、权限账本与原审批协议不变。2026-11-03 用户明确批准在既有 HASHI3 接手 L1 收尾及 L2 开发；这不是 Core major migration 或生产采用授权。

## 本轮边界

原有 Safety 和 Runner 安装读取器负责身份、完整 runtime capsule、私有数据及单写锁核验，本轮继续复用它们。此前的前台入口依赖 Python 主线程信号，不能直接用作 Windows SCM 回调。现加入：

- orchestrator.permissions.windows_service：固定 safety／runner 两角色、显式私有安装路径；只有显式 SCM dispatch 才启动。
- 两个原 serve 接受外部停止事件和就绪回调。前台调用继续处理并恢复原信号；SCM 调用不修改进程信号。
- SCM 在端点启动及 Runner 原记录恢复之后才报告 RUNNING。启动／关闭期间有独立 checkpoint；停止先进入 STOP_PENDING，清理确认后才 STOPPED。
- 启动失败、状态报告失败、异常退出及清理不明均报告非零服务退出码。停止重入或启动中的迟到就绪不能重新报告 RUNNING。
- 不在 import、L0、Core 或 Agent 工具调用中隐式安装／启动服务；不开放任意服务名对应的动态产品模块。

原生组件配方使用完整提交构建的 capsule，由管理员显式启动，创建两名新的普通测试账户及独立的 HashiSafetyTest_<id>／HashiRunnerTest_<id> 服务。账号密码只在内存和 SCM 原生创建 API 中传递，不进命令行、日志或回执。

## 验证层次

聚焦回归直接调用实际服务入口，并在非主线程运行事件循环。旧入口同组 10 项失败；增加外部生命周期合同后通过。SCM 适配层另测状态顺序、停止竞争、长同步初始化时的 checkpoint 和失败退出。注入的 SCM／应用端点是测试边界，不能据此宣称 Windows 安装通过。

原生配方：

- 构建：既有 scripts/permissions/build_runtime_capsule.py；必须是完整已提交源码及匹配的既有 Python／依赖目录，禁止在运行的 Core 安装依赖。
- 运行：python -B -m scripts.permissions.windows_service_probe probe --capsule-manifest <absolute-capsule.json> --capsule-sha256 <pinned-sha256> --output <new-absolute-directory>。
- capsule 需位于服务账户可读的封闭目录，所有内容及目录须通过原 qualifier；包括持久所有者和父目录控制。不得通过放宽现有用户目录／生产目录 ACL 来准备探针。
- 实体观察：SCM 真实 PID、账户 SID、实际 token 非管理员、service SID、禁止该服务账号修改／启动／停止／删除自身 SCM 注册，以及正常与失败退出的原生状态。
- 清理：先确认服务停止及注册删除，后撤销本次唯一新增的 service-logon right，回收实际新建 profile 和账户。回执保留所有权及失败；配置冲突或已提交耐久性错误先对账，不自动重放。

注册辅助器限于新测试命名空间。已存在的服务不得接管；停止期间发现身份或命令改变，不得删除。任何服务仍未确认清理时，不删除其账户来制造“干净”。

## 不代表完整 L2

该原生配方的 payload 专门验证 SCM 生命周期及 OS 身份控制，并非整个 Safety／Runner 安装、HERV3、文件／网络代理及可信审批组合。因此其通过只能记为组件资格。

独立完整安装器、账户及目录／进程控制边界、普通 Worker 的实际 L2 闭环、审批环境隔离、撤权与故障恢复等仍按 [Windows L2](L2_WINDOWS.md) 和 [ACCEPTANCE](ACCEPTANCE.md) 完成。完整 WAC 未通过时，展示继续保持 partial／unknown。生产采用与用户实际前端验收另记。
