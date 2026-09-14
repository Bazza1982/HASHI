# HCC 实施验证与本地交接

验证日期：2026-09-15（Australia/Sydney）。分支：`feature-hcc`。
实现基线：`79a204a8d5df30bb80511e96ed84df323796acaf`。
功能提交：`7d65e934b7542a1a10b11f496d66bdc6154aff60`。

## 结论与范围

HCC 功能代码、相关组件回归及 Linux / Windows 原生验证已通过，可以拉取分支进行本地验收。**这不是全仓测试全绿、生产部署完成或真实信息源验证完成的声明。** 没有合并或推送到 `main`，没有修改受保护 Core，没有重启运行中的 HASHI，没有创建真实抓取任务或写入个人 Agent 的配置。

实现说明、逐文件变更、Cron Skill/CLI 参数、旧断言处置及回滚步骤见 [HASHI_HCC_IMPLEMENTATION.md](HASHI_HCC_IMPLEMENTATION.md)。

## 已完成的测试

所有环境使用隔离 CPython **3.12.13**，依赖来自 `constraints/standard-py312.lock` 和 `.[test]`。

| 环境 / 检查 | 实际结果 |
|---|---|
| Linux 沙盒：HCC 专项（含真实管线、迁移包节点） | 57 passed |
| Linux 沙盒：18 个相关测试文件 | 446 passed |
| GitHub Ubuntu 22.04 原生组件回归 | 446 passed，0 failed，0 skipped |
| GitHub Windows 原生组件回归 | 435 passed，0 failed，11 skipped |
| 两个 CI 平台的 HCC 专项子集 | 各 57 passed，0 skipped |
| 受保护 Core、执行代码 Ruff、Git whitespace 检查 | 沙盒及两平台 CI 均通过 |

CI 证据：[验证与发布运行 34909306402](https://github.com/Bazza1982/HASHI/actions/runs/34909306402)。
Ubuntu job：`104193012789`；Windows job：`104193012512`；成功发布 job：`104193427273`。
两份 JUnit XML 已下载并逐项核对，而非仅根据 job 状态计算数量。Actions 测试附件保留 7 天。

Windows 的 11 个跳过均来自已有测试的平台条件：POSIX 权限位/符号链接、大小写不敏感文件系统上无法同时构造 `agent.md` 与 `AGENT.md` 等。没有为 HCC 新增 skip、xfail 或删除这些测试。HCC 并发写入测试在 Windows 原生执行并通过。

覆盖包括：可选/空 HCC；其他 PCM 区块的严格校验；每轮完整读取；Memory 与增量模式不屏蔽 HCC；无关键词筛选；开关及重建后的持久化；真实命令注册；实际 PCM → HER durable coordinator 更新/撤销；定时管线；跨进程并发；同条目旧结果拒写；失败保留旧内容；CRLF/其他区块字节保留；CLI Agent 绑定；导入导出；旧注释式条目兼容。

还进行了 7 项可恢复故障注入：禁止空 HCC、用 Memory 开关屏蔽 HCC、丢失 HER 撤销列表、删除版本冲突检查、删除跨进程锁、忽略开关保存、将权限操作放到发布之后。对应测试全部检出了故障。所有临时修改已恢复，20 个交付源文件的 SHA-256 与最终通过测试的版本逐一一致。

交付代码通过哈希校验的补丁在两个 runner 上应用、测试，只有两个验证 job 都成功才允许发布到 `feature-hcc`。临时补丁文件已经从分支当前树删除；本交接提交删除一次性交付 workflow，保留只读的 `HCC contract` 持续回归 workflow。旧 CI 尝试曾因 Windows Python 构建供应和补丁换行格式失败，已修正测试交付环境；没有通过降低测试要求解决。

## 全仓验证的已知边界

曾运行更广的 core gate，**不能宣称其全部通过**：

1. `tests/test_her_v2_adapter.py::test_adapter_reconciles_old_inflight_ledger_without_resuming_it`：预期 ERROR、实际 EXECUTING。同样的失败已在未修改的 `79a204a` 基线工作树独立复现，不由 HCC 引入。
2. 扩大运行在 `tests/test_function_generation.py::test_default_hot_probe_does_not_seed_from_core_loaded_modules` 附近达到沙盒时间上限。基线单独探测在较短的时间界限内也停留在 source-manifest 构建；这只能证明本次未完成该验证，不能证明该测试必然失败。

这两项测试没有删除、放宽、重分类或加入跳过。它们不包含在上述 446 项组件回归中。本地验收应分别复核，不要把已知基线问题伪装成 HCC 的测试通过。

没有调用真实模型、天气/新闻/交通 API；没有测量生产延迟，也没有验证所有 provider 的远端历史擦除。HCC OFF 撤销的是活动 PCM 区块，不承诺擦除历史回答、原始审计或 provider 保留的旧对话。

## 本地验收

先保存未提交的本地工作；不要使用强制 reset 覆盖其他改动。在已有 HASHI 仓库执行：

```bash
git fetch origin
git switch feature-hcc
git pull --ff-only origin feature-hcc
```

使用符合仓库要求的隔离 Python 环境运行：

```bash
python -m pytest -q tests/test_hcc.py tests/test_hcc_integration.py tests/test_runtime_pipeline.py::test_scheduled_turn_carries_hcc_through_real_pcm_and_fixed_transport tests/test_agent_move_package.py::test_package_preserves_hcc_and_injection_preference
python scripts/check_protected_core_changes.py
```

完整 446 项命令见实施说明。先通过现有 Functions 资格验证和受控热更新流程加载新版，再向测试 Agent 的规范 `agent.md` 加入 HCC；不要冷重启 Core。先验证 `/hcc` 默认 OFF，再 `/hcc on`：写入 V1 后提问，刷新为 V2 后下一轮提问，最后 `/hcc off` 检查活动上下文撤销及开关持久化。

定时刷新使用 `skill:hcc-refresh`，来源、地区、长度和频率由用户配置。每次先 `inspect` 获取条目版本，再获取新信息，最后 `replace --expected <digest-or-absent>`。失败不覆盖；`/hcc off` 仅关闭注入，不停止 Cron。上线前检查这一区分。

分支中较早的 `23aebe28` HCC 预览提交保留在祖先历史中。旧 `[hcc]`、开关、`/hcc status` 和注释式条目可兼容；曾按预览版手工设置的刷新命令需改用当前必填版本参数，具体差异见实施说明末节。
