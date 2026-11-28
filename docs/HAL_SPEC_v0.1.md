# HAL — Hashi Automation Language

**语言规范（初版） | HAL Language Specification v0.1**

| 项目 | 内容 |
|---|---|
| 名称 | HAL — Hashi Automation Language |
| 版本 | v0.1（设计规范草案） |
| 语言设计 | HASHI 项目作者原创设计 |
| 规范日期 | 2026-11-23 |
| 状态 | 已确认的基础语法与语义；尚非实现或部署声明 |
| 执行归属 | HASHI HAL Runtime（规划） |
| 主要编辑入口 | Workbench Canvas 的 HAL 工作流构件（规划） |

## 1. 目标与原则

HAL 是一门由 HASHI 项目作者设计的**极简、事件驱动自动化语言**，借鉴 MUD 客户端自动练功、自动战斗脚本的易读风格，主要用于 HASHI Agent 与系统事件的触发、命令执行和连续自动化工作流。

设计原则：

1. **少量符号、少量关键词**。优先保持语法好学、直观，不提前扩展为通用编程语言。
2. **作者主导语法设计**。新增词汇与句式须由语言作者确认；实现方不得自行添加语法。
3. **事件与行为清晰区分**。以 `@`、`#`、`$` 和 HASHI 原生命令 `/` 区分不同元素。
4. **支持通配与运行时绑定**。不用为每个 Agent 重复配置相同的自动化规则。
5. **支持独立与连续工作流**。利用 `.` 和 `;`，不增加额外的流程控制关键词。
6. **语言驱动，UI 辅助**。HAL 是 Canvas 的按需工作流构件，不新建固定占屏的独立自动化面板。
7. **职责解耦**。HASHI 负责 HAL 解析与执行；Workbench 只提供编辑、可视化及状态查看。Canvas 基础功能不得依赖无关 Agent 的在线状态。

## 2. 已确认的符号与词汇

### 2.1 符号约定

| 符号 | 类别 | 含义 |
|---|---|---|
| `@` | Agent 标识及变量 | 指定 Agent、匹配范围或引用触发者 |
| `#` | 事件 | Agent 事件或系统事件 |
| `$` | HAL 行为 | HAL 定义的动作 |
| `/` | HASHI 原生命令 | 复用 HASHI 已支持的命令及参数 |
| `"..."` | 字符串 | 包裹 Prompt 文本；允许引用已定义变量 |
| `if` | 条件语句 | 声明触发条件 |
| `then` | 执行语句 | 声明条件满足后的操作 |
| `else` | 备用分支 | 条件不满足或显式失败时的分支；详见未决事项 |
| `.` | 规则链终止符 | 结束一个独立条件工作流 |
| `;` | 连续符 | 继续同一工作流，下一条规则依赖此前执行进度 |

> `.` 和 `;` 属于 HAL 语句结构，不是 Prompt 引号内的普通标点；在引号内仍按文本处理。

### 2.2 Agent 标识

| 词汇 / 形式 | 含义 |
|---|---|
| `@agent_a` | Agent A（示意标识） |
| `@agent_b` | Agent B（示意标识） |
| `@agent_any` | Agent 通配匹配：任意符合范围的 Agent 可触发 |
| `@agent_trigger` | 本次触发工作流的具体 Agent，运行时绑定 |
| `@rika@hashi4` | 具体 Agent 和所属 HASHI 实例的组合标识（示例） |

**重要区别：**`@agent_any` 是事件匹配用的通配符；`@agent_trigger` 是通配或具体 Agent 触发后绑定的**实际 Agent 身份**，不是第二个通配符。

例如事件源为 `@lily@hashi4` 时，本次运行中的 `@agent_trigger` 应解析为 `@lily@hashi4`。并发触发应各自保存自己的绑定，不能通过全局变量相互覆盖。

### 2.3 已提出的事件名称

| 事件 | 预期语义 | 状态 |
|---|---|---|
| `#turn_finish` | Agent 完成一轮工作 | 基础词汇已确定 |
| `#turn_error` | Agent 一轮工作出错 | 基础词汇已确定 |
| `#turn_commentary` | Agent 产生 commentary 事件 | 基础词汇已确定，具体触发粒度待明确 |
| `#system_idle` | 系统从有工作进入无 Agent 工作的空闲状态 | 基础词汇已确定，实例范围待明确 |
| `#hashideploy_finish` | 某次 HASHI 部署完成 | 用户给出的工作流示例名称；事件接口和成功语义待核实 |
| `#reboot_ok` | 某次重启成功 | 用户给出的工作流示例名称；事件接口和失败分支待核实 |
| `#whiteboard_stage` | 白板卡片提交了一次阶段跃迁 | 已实现（v2）：白板跃迁事件源，无 Agent 主语 |

`#system_idle` 应是**进入空闲的事件**，而不是只要持续空闲就反复触发的轮询条件。具体作用域（单个 HASHI 实例或其他范围）尚待作者决定。

### 2.4 HAL 行为

| 行为 | 含义 | 状态 |
|---|---|---|
| `$prompt` | 向指定 Agent 发送 Prompt | 基础词汇已确定 |
| `$stop` | 停止指定目标或相关执行 | 基础词汇已确定，目标与停止范围待明确 |

### 2.5 HASHI 原生命令

HAL 允许在 `then` 后复用 HASHI 已存在的 `/` 命令，不为这些命令另造 HAL 行为词。

讨论中的命令示例：

- `/reboot max`
- `/hashideploy`

这些是**预期调用形式**，并非本文已经验证其当前 HASHI 版本中真实存在、参数有效或可由自动化执行。

## 3. 基础句式

### 3.1 指定 Agent 的事件触发

```hal
if @rika@hashi4 #turn_finish then /reboot max.
```

解释：指定的 Agent 完成一轮工作后执行 HASHI 命令。

### 3.2 任意 Agent 触发 + 运行时变量

```hal
if @agent_any #turn_finish then @rika@hashi4 $prompt "@agent_trigger has finished work; please go review the work".
```

解释：任意符合监听范围的 Agent 完成一轮工作，就由 `@rika@hashi4` 接收提示词。`@agent_trigger` 在**当前工作流运行实例**内替换成具体触发者。

### 3.3 无 Agent 主语的系统事件

```hal
if #system_idle then /reboot max.
```

解释：系统级事件可直接位于 `if` 之后，不必虚构一个 Agent 主语。

### 3.4 连续工作流

```hal
if #system_idle then /hashideploy;
if #hashideploy_finish then /reboot max;
if #reboot_ok then @lily $prompt "重启成功，看看新代码加载是否完整".
```

解释：第一条是工作流入口；第二、三条依赖前面的执行进度，**不能独立监听并启动新的工作流**。第三条的事件还必须对应此工作流实例中的重启，不应被其他工作流或人工操作产生的同名事件误触发。

### 3.5 `else` 的讨论示例

```hal
if #reboot_ok then @lily $prompt "重启成功，看看新代码加载是否完整" else @lily $prompt "重启失败，请检查报告原因".
```

`else` 已由作者提出，但**准确触发语义尚未最终确定**。特别是「尚未收到 `#reboot_ok`」不等于「重启已失败」。实现前必须确定失败事件或失败状态如何对应 `else`，不能擅自把超时或未知状态等同于失败。

### 3.6 白板阶段跃迁触发（v2）

```hal
if #whiteboard_stage where [project="Hashi", to_stage="in_review"] then
  @{reviewer} $prompt "【评审请求】{card_id} HEAD {head_sha} 第 {round}/{max_rounds} 轮：{next_step}";
if @agent_any #turn_finish then @{implementer} $prompt "【整改】{card_id}：{next_step}".
```

- **条件**：`where [key="value", ...]`，可用键为 `project`、`from_stage`、`to_stage`、`category`，取值来自事件本身；阶段名是作者数据，代码里不写死，白板新增自定义阶段无需改 HAL。
- **变量**：`{card_id}`、`{work_key}`、`{project}`、`{from_stage}`、`{to_stage}`、`{revision}`、`{round}`、`{max_rounds}`、`{implementer}`、`{reviewer}`、`{tester}`、`{pr_url}`、`{head_sha}`、`{last_verdict}`、`{test_env}`、`{next_step}`。
- **角色路由**：`@{reviewer}` / `@{implementer}` / `@{tester}` 取卡片上该字段的值；写 `yun` 即本实例，写 `yun@hashi6` 走既有点对点通道。字段为空时如实失败，不猜测接收人。
- **幂等**：同一张卡的同一 `revision` 只派发一次（键为 `<card_id>:rev:<revision>`）。
- **停转**：阶段被熔断（`fused`）、进入 `halted` 类阶段、或白板判定这次跃迁超出返工预算时，不派发并在卡片上留言通知；进入人类门禁阶段同样停转，但只有本来会触发规则或在跑执行时才通知。熔断理由（含预算用尽、阶段表不可读）一律落到执行记录里，执行不会「通知了却没停」。
- **停转以卡片为准**：每次派发（含重试）前都读一次卡片的当前状态。因此 Agent Run 完成事件先于白板事件被处理时，也不会把整改派进一张已经被熔断的卡；白板读不到时暂缓该卡链路，等能读到时再继续。
- **游标**：启用规则时只记下当前白板位置，不重放历史；与 Agent Run 事件游标互相独立。出现缺口（事件已被清理）时按白板契约改读卡片当前状态，缺口之前的跃迁无法重放也不假装重放。
- **阶段表**：门禁与返工预算的判定每次都读白板的当前定义，不使用缓存——白板主人随时可以改定义，旧表会让自动化穿过刚加上的门禁。
- **配置**：`HASHI_HAL_WHITEBOARD_URL`（默认 `http://127.0.0.1:4417`，设为空字符串即停用该事件源）、`HASHI_HAL_WHITEBOARD_TOKEN`（`key` 模式下的 Bearer 令牌）。

## 4. `.` 与 `;`：最重要的执行规则

### `.` — 独立条件工作流的结束

以 `.` 结束一条规则链。其后的新 `if` 可以成为另一条独立的事件监听工作流。

```hal
if @agent_any #turn_error then @lily $prompt "有 Agent 发生错误".
if #system_idle then /reboot max.
```

上面是两个独立入口，彼此不依赖。

### `;` — 当前工作流继续

正式连续符为 `;`。以 `;` 结束当前阶段意味着下一条 `if` 属于同一工作流，必须等待此前阶段推进到该处才会成为有效等待条件。

```hal
if #system_idle then /hashideploy;
if #hashideploy_finish then /reboot max.
```

当其他无关部署产生 `#hashideploy_finish` 时，第二条不应自行启动。每次工作流执行具有自身上下文，用于关联该次命令及后续事件。

## 5. 字符串与变量替换

- Prompt 使用双引号 `"..."` 包裹。
- 引号内允许使用**已定义的** `@`、`#`、`$` 变量或引用；目前唯一已明确定义替换含义的变量是 `@agent_trigger`。
- 引号中的普通 `@`、`#`、`$` 字符不得一律强制解释为变量，例如邮箱地址或 `$100` 应能作为普通文字保留。
- 尚未定义具体的转义规则、未知变量的错误处理及未来事件变量的命名方式；这些不属于 v0.1 已确定内容。

## 6. 运行模型与组件边界（已确定的架构方向）

1. **HASHI 作为 HAL Runtime 的归属方**，负责语法解析、验证、事件匹配、变量绑定、连续工作流状态及命令或动作派发。
2. **Workbench Canvas 提供 HAL 工作流构件**，按需加入 Canvas；不建立常驻的独立 IF–THEN 编辑模块。
3. **HAL 编辑器复用 Canvas 已有 Inspector**。仅在选择 HAL 构件时显示，不额外挤压或固定占领 Canvas 主工作区。
4. **运行不依赖 Workbench 页面是否打开**；单个 Agent 离线不能无理由禁用整个 Canvas。
5. **复用现有事件、会话和命令能力**；HAL 不应再引入第二套同类事件或 Agent Run 执行机制。
6. **HAL 与 HASHI Protected Core 解耦**：优先以独立模块集成现有接口，不把脚本解释器硬塞进 Agent 核心执行循环。
7. 现有 Workbench HEA 的持久化、事件监听、幂等及恢复能力值得复用，但具体迁移方案须经代码审查确认，不能假定文件可以直接搬迁。

## 7. 正确性约束（不是新增语法）

- `@agent_any` 必须支持真正的通配匹配，不要求每个 Agent 单独创建同一规则。
- 每次触发创建独立的运行上下文；并发触发的 `@agent_trigger` 不得串值。
- `;` 后的事件等待必须属于此前触发的工作流实例，不能由无关事件越级启动。
- 事件重复投递不得无条件重复执行同一次副作用动作。
- 编辑器关闭、Workbench 离线或某个无关 Agent 离线，不应无理由阻断 HASHI 的 HAL 执行。
- 对部署、重启等影响运行服务的命令，需要确定目标实例、执行结果及恢复语义；不能把「命令已发送」当作「命令成功完成」。
- `#system_idle` 不得在持续空闲期间高频重复触发；工作流自身引起的状态变化也不应造成无限重启循环。

## 8. 尚待语言作者明确的事项（不得自行扩展）

1. `@agent_any` 与 `#system_idle` 的默认监听范围，特别是多 HASHI 实例时的边界。
2. `@agent_a`、`@agent_b` 是正式可配置别名，还是仅为示意命名。
3. `$stop` 的目标写法、停止范围及其完成反馈。
4. `#turn_commentary` 的事件粒度和何时触发。
5. `else` 究竟对应哪个判定及失败状态，如何匹配到当前执行阶段。
6. `#hashideploy_finish`、`#reboot_ok` 是否为最终事件名称及其来源契约。
7. 连续工作流在执行失败、事件永不到达、HASHI 服务重启后如何处理等待及恢复。
8. 引号中的转义、未知变量处理、大小写与换行的正式词法规则。
9. `/` 原生命令的目标实例选择、允许调用范围及具体参数契约。
10. HAL 脚本如何与 Canvas 其他节点连接，以及是否需要同步可视化的具体格式。

以上项目为**待定事项，不是新功能需求**。HAL v0.1 不引入 `WAIT`、`EVERY`、`REPEAT`、变量赋值、循环、通用表达式或其他此前未由作者确认的关键词。

---

**规范原则：由语言作者定义语言；实现方只负责忠实执行、验证与提供简洁 UI。**