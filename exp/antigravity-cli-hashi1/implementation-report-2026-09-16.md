# Antigravity CLI (agy) 接入 HASHI backend — 实现与测试报告

- 日期：2026-09-16
- 执行：里香（rika）· HASHI PAO
- 分支/worktree：`feature/antigravity-cli-hashi1` @ 8e6b41b3
  （WSL 仓库 /home/lily/projects/hashi；worktree 位于
  C:\home\lily\projects\hashi\antigravity-cli-hashi1）
- 结论：**实现完成，聚焦测试 22/22 通过，真实 agy 冒烟 PASS；未部署、未改运行配置。**

---

## 1. 阶段1 取证补测结果（本次会话新证据）

| # | 实验 | 结果 | 证据 |
|---|---|---|---|
| F1 | `agy --version` | 1.2.3，exit=0 | 本报告 §8；feasibility 报告 §2 |
| F2 | `--output-format stream-json`（U1） | **可用**：NDJSON 每行一个 JSON；事件 `init` / `step_update` / `result`；`step_update.step_type="agent_response"` 携带 `text_delta` 文本增量；`result.status/response/usage` 为完成信号；`conversation_id` 全程回传 | `exp/antigravity-cli-hashi1/f2-stream-json.txt` |
| F3 | `--input-format text` + stdin（U2） | **不可用（负向结论）**：`-p ""` + 管道输入 → `error: empty prompt`；且 **exit=0 但 status=ERROR**（adaptor 必须认 payload status） | `exp/antigravity-cli-hashi1/f3-stdin.txt` |
| F4 | `agy models` | 14 个模型，确切 ID 已落盘（gemini-3.8/3.7/3.6-flash-high/medium/low、gemini-3.1-pro-high/low、claude-sonnet-4-6、claude-opus-4-6-thinking、gpt-oss-120b-medium） | `exp/antigravity-cli-hashi1/agy-models-2026-09-16.txt` |
| F5 | `agy --help` 关键参数复核 | `-p/--print`、`--output-format text\|json\|stream-json`、`--conversation <id>`、`-c/--continue`、`--print-timeout`（默认 5m0s，Go duration）、`--add-dir`（可重复）、`--dangerously-skip-permissions` | `exp/antigravity-cli-hashi1/agy-help-2026-09-16.txt` |

**决策**：主传输采用 stream-json（schema 已实测稳定）；保留 `--output-format json`
整段模式作为解析器后备（`extra.output_format="json"` 可切换）。stdin 传输维持
未验证状态：超过 24000 字符的提示返回明确错误而不是冒丢失风险。

## 2. 改动清单（相对 main 8e6b41b3）

| 文件 | 改动 |
|---|---|
| `adapters/antigravity_cli.py` | **新增**。AntigravityCLIAdapter：stream-json/json 双解析；conversation_id 生命周期（`--conversation` 恢复 + 每工作区 `.hashi-antigravity-session.json` 持久化 + `/new` 清除 + `set_session_mode`）；`--print-timeout` 对齐 idle 超时；idle 超时与取消走进程树 kill；启动即 touch activity（从启动起计沉默）；supports_sessions=True |
| `adapters/registry.py` | 注册 `antigravity-cli → AntigravityCLIAdapter` |
| `orchestrator/flexible_backend_registry.py` | CLI_ENGINES 增加 `antigravity-cli`；BACKEND_REGISTRY 新增条目（label=antigravity，14 模型，default_model=gemini-3.8-flash-high，secret_keys=[]——keyring 认证）；**gemini-cli 条目一字未动** |
| `orchestrator/pathing.py` | 新增 `resolve_agy_executable`：显式配置路径 → `%LOCALAPPDATA%\agy\bin\agy.exe` → PATH `agy` |
| `orchestrator/config.py` | 新增 `agy_cmd: str = "agy"` 字段 + 载入时自动发现（`resolve_agy_executable` + `resolve_command_value`） |
| `orchestrator/api_gateway_preflight.py` | `antigravity-cli` 分支：`_cli_command` 映射 + 自动发现解析 |
| `orchestrator/backend_preflight.py` | cli_map 增加 `antigravity-cli`（自动发现解析） |
| `tests/mocks/bin/agy` | **新增** mock（复刻 1.2.3 契约：stream-json 事件流、json 整段、exit-0-但-ERROR、crash、slow；AGY_MOCK_LOG/AGY_MOCK_NOISE_STDOUT 测试钩子） |
| `tests/test_antigravity_cli.py` | **新增** 22 个聚焦测试（注册表/常量/发现/初始化/往返/会话恢复跨实例/json 降级/错误语义/超时/取消/preflight） |
| `exp/antigravity-cli-hashi1/` | 证据目录：取证原始输出、编辑脚本（apply_edits.py/fix_*.py/red_green.py）、测试日志、冒烟脚本与结果 JSON、本报告 |

未改动：`pyproject.toml`（未把新测试加入 bare-pytest gate，待拍板）、agents.json、
任何运行实例配置、受保护 Core 文件（runtime_*/function_*/kernel_*）。

## 3. 测试证据

- **聚焦测试**：`tests/test_antigravity_cli.py` → **22 passed**（`focused-tests-green.log`）
- **Red/Green 对照**：临时回退 registry 条目与 `resolve_agy_executable` →
  测试 collection 失败（`ImportError: cannot import name 'resolve_agy_executable'`）；
  恢复后 22 passed（`red_green.py` + 日志）。
- **回归对比**：同一组 6 个既有测试文件（api_gateway_preflight/backend_timeout/
  model_catalog/config/flexible_backend_state/architecture_boundaries）
  - main 基线：**88 passed, 1 failed**
  - worktree 修改后：**88 passed, 1 failed**（同一个既有失败
    `test_save_state_recovers_from_invalid_existing_json`，与本次改动无关）
  - → 无新增失败，gemini 双轨安全（`regression-main.log` / `regression-worktree.log`）
- **真实 agy 冒烟**（Windows Python + 真实 agy.exe，2 次配额调用）：
  `smoke-real-2026-09-16.json` → **SMOKE_VERDICT: PASS**，exit=0：
  - turn1："PONG"（精确匹配），conversation_id `1cf716d0-…`，num_turns=1，9.5s；
  - 新进程实例 turn2：追问上一轮秘密词 → 答 "PONG"（上下文保留），
    同一 conversation_id，num_turns=2，9.0s —— 跨进程会话恢复成立；
  - 会话文件落于工作区 `.hashi-antigravity-session.json`。

## 4. 账号配额/Token 消耗披露（本次会话）

- F2 stream-json 取证：约 14,950 tokens（含系统提示 14,921）
- F3 stdin 负向实验：0 tokens（本地报错，未发起模型调用）
- 冒烟 turn1：input 15,974 / output 131；turn2：input 32,301 / output 200
- **合计约 63.6k tokens**，4 次真实调用，均为无害提示。
- 会话残留：1 个新测试会话（`1cf716d0-…`，内容为 PONG 往返）。

## 5. 风险与未确证项

- **U2 stdin 传输**：未验证（F3 负向）；>24k 字符提示会被 adaptor 明确拒绝。
- **U3/U4/U5/U6/U7/U8**：沿用可行性报告状态（`--continue` 作用域、配额/限速、
  会话删除手段、GUI/CLI 并发、`--json-schema`、Unicode 大输出）均未补测。
- keyring 失效形态未在本机破坏性验证（不应做）；若登录态丢失，agy 回退浏览器
  登录，无人值守时表现为超时/报错而非假死（adaptor 有 idle 超时兜底）。
- agy 输出 schema 依赖 1.2.3；升级后若事件 schema 变化需回归 `f2-stream-json.txt`。

## 6. 回滚方式

- 彻底回滚：删除本 worktree（`git worktree remove antigravity-cli-hashi1`）
  并删除分支 `feature/antigravity-cli-hashi1`；main 与 gemini-cli 实现全程未被触碰。
- 双轨期回滚（若已切换）：把 agent 的 backend/model 值改回 gemini-cli 即可，
  antigravity 条目与代码保留、不影响 gemini。

## 7. 需要爸爸拍板的点

1. **是否进入切换窗口**：HASHI4 运行实例的 agents.json（62 处 gemini 引用）
   不在本会话作用域，逐 agent 迁移 backend/model 需要另行授权。
2. **是否把 tests/test_antigravity_cli.py 纳入 bare-pytest gate**（pyproject testpaths）。
3. **模型名映射/别名**（报告 §5 建议改）与 `skills/gemini` 退役节奏。
4. **流式 UI 是否要开 antigravity 事件流**（当前 supports_answer_stream/tool_stream=True，
   但 stream-json 工具事件 schema 只验证了文本增量，未实测工具调用事件形态）。
