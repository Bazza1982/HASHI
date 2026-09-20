# Shared Demo Contract v1

日期：2026-09-20  
协议标识：`hashi.shared-demo`，版本：`1`  
状态：**拟实现的共同契约，不代表当前 Backend API 已支持。**  
源代码基线及授权范围见 [README](README.md)。

## 1. 范围和事实所有者

一个独立 HASHI Demo instance 服务多个匿名访客。每个访客对应一个真实 Agent，可有最多三个 Conversation Sessions。客户端只呈现数据，不运行模型，不写 HASHI 配置或数据库。所有 DTO 是 PAO/HER 权威状态的受限投影，不建立第二份聊天历史、Run 队列或永久事件存储。

| 对象 | 唯一所有者 |
|---|---|
| 访客凭证摘要、lease、Agent 绑定、到期时间 | HASHI PAO Functions |
| Conversation Session、Message、Run、Event | 原有 PAO 所有者 |
| Engine Session、Turn、模型调用与实际计量 | HER v2 |
| 公开产品材料与当前 Session 上下文 | PCM |
| HTTP Cookie、Origin 校验、浏览器展示 | 兼容网页客户端及其受限 server |
| 模型选择、服务 secret、端口、根路径 | 忽略跟踪的实例配置 |

IP 不是 owner；Agent、Session、Run 的随机 ID 不是授权凭证。进程和目录分开也不等于每访客 OS/container 隔离。

## 2. 两段链路

```text
浏览器 -- HTTPS + 单枚 Cookie --> 受限网页 server
       -- loopback + 专用服务凭证 + 匿名凭证 --> HASHI Demo Connector
```

浏览器只访问本源 `/api/demo/*`。网页 server 对固定 HASHI origin 的固定 `/api/demo/*` 路由逐一映射，不提供 URL、path 或连接选择器驱动的通配代理。

拟定义内部头：

- `X-Hashi-Demo-Service-Token`：HASHI 签发/配置的 demo-only 服务凭证，只由服务器持有。
- `X-Hashi-Demo-Visitor`：由浏览器 Cookie 取出的 opaque token，只走内部请求头。
- `X-Hashi-Demo-CSRF`：当前访客的非持久 CSRF 值。

网页 server 必须丢弃浏览器传来的 service/visitor/owner/agent/forwarded 内部身份头，再自行构造。绝不能使用普通管理员 token 兜底。HASHI 的 demo-only 凭证不得通过普通 Backend、admin、Remote、MCP 或其他凭证认证器。

Demo instance 默认只装配 Demo 路由及无敏感数据的本地 health。普通模式的路由、HMAC 和认证语义保持原样；本契约不为正常客户端增加角色、配对或账户系统。

## 3. 匿名 lease 和 Cookie

HASHI 生成密码学随机的 32 字节 token；数据库只保存摘要及必要 lease 信息。浏览器只保留一枚身份 Cookie：

```http
Set-Cookie: __Host-hashi-demo=<opaque-token>; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=86400
```

无 Domain。token 不得进入 URL、JSON 公共响应、localStorage、sessionStorage、模型上下文、日志、分析系统或错误栈。HASHI 内部 bootstrap 响应可以一次返回 `visitor_token` 供网页 server 设置 Cookie，server 必须在转发 JSON 前删除它；此响应及 header 不能被内容日志采集。

恢复不能重置绝对有效期。重发 Cookie 使用服务器剩余秒数。丢弃 Cookie 就无法找回；不承诺跨设备身份。无 Cookie 的页面加载、health、config 读取、爬虫请求不创建 Agent/lease，不启动 Worker，也不调用模型。

CSRF：所有浏览器写请求要求精确配置的 Origin、JSON 内容类型及自定义客户端头 `X-Hashi-Demo-Client: browser-v1`；不得开放跨源 CORS。已有 lease 的写请求还要求内存中的 `X-Hashi-Demo-CSRF`，可由 HASHI 按访客 token/epoch 派生并在 bootstrap/me 返回。CSRF 值不是第二枚 Cookie。缺少 Origin 的普通浏览器写请求默认拒绝；本地测试必须显式提供测试 Origin，不增加生产绕过参数。

无凭证的首次 bootstrap 以 Origin+JSON+自定义头防护。`GET /me` 可以重新取得 CSRF 值，但不能改变 lease 寿命。仅同源页面能读取该响应。

## 4. 时间、状态和身份绑定

- `expires_at = created_at + 86400s`，UTC；任何新 Session、重连、重试都不延长。
- `idle_ttl_seconds = 1800` 为建议默认；`0` 表示仅使用绝对期限。
- idle 只由成功接纳的用户操作更新，例如发送消息或新建 Session。config、me、snapshot、events、后台心跳、chunk 和轮询不更新。幂等重放不反复续期。
- lease 状态：`provisioning -> ready -> expiring -> purged`；失败可进入 `cleanup_pending`，但不能重新成为可访问的 ready。
- `lease_epoch` 为不可复用的实例化标识。删除后的旧请求、事件及模型回调都失效。
- 完成清理前保留资源槽位，失败不能通过先释放槽位来绕开上限。

一个凭证的所有访问都先导出内部 `owner_id` 和 `agent_id`，再调用带显式 owner 的原生存储操作。不得调用 `SessionStore.owner_id_for()` 的个人用户回退。

## 5. HTTP 操作表

以下路径在浏览器入口和内部 Connector 保持一致；内部每条路由仍需要 demo 服务认证。未知路由、方法或字段默认拒绝。

| 方法/路径 | 作用 | 正常响应 |
|---|---|---|
| `GET /api/demo/config` | 公共能力、实际限制、协议版本 | 200；不创建身份 |
| `POST /api/demo/bootstrap` | 第一次发送时自动领取，或恢复已有有效 lease | 201 新建 / 200 恢复 |
| `GET /api/demo/me` | 自己的 Agent、lease 截止、CSRF、个人额度 | 200 / 401 / 410 |
| `GET /api/demo/sessions` | 仅自己的 Sessions | 200 |
| `POST /api/demo/sessions` | 为自己的同一 Agent 新建独立上下文 | 201 / 幂等重放 200 |
| `GET /api/demo/sessions/{sid}/snapshot` | 自己的有界快照及恢复 cursor | 200 |
| `GET /api/demo/sessions/{sid}/events` | 当前 Session 的有界增量读取 | 200 |
| `POST /api/demo/sessions/{sid}/runs` | 接纳纯文本用户消息 | 202；不表示已完成 |
| `POST /api/demo/sessions/{sid}/runs/{rid}/cancel` | 取消自己的 queued/active Run | 200；报告实际状态 |
| `DELETE /api/demo/me` | 撤销体验并调度清理 | 202；清 Cookie |

bootstrap 只接受可选 `locale`，不得接受 agent/provider/model/owner/workspace。新 Session 只接受 `idempotency_key` 和可选受限 `title`。标题默认为本地化 New conversation，不额外调用模型命名。

Bootstrap 不声称无凭证首次网络丢包时的 exactly-once：同一有效 Cookie 的重试复用原 lease；首次 Set-Cookie 丢失或跨标签同时首次领取可能留下未被浏览器持有的租约。服务端槽位原子上限和 idle 清理限制其影响，不能按 IP 合并。客户端合并本标签的重复请求；无须引入永久设备指纹。

## 6. 最小公共 DTO

示例字段描述的是拟实现结构；所有新增字段需先更新此契约和两侧契约测试。

### Config

```json
{
  "ok": true,
  "protocol": "hashi.shared-demo",
  "version": 1,
  "mode": "demo",
  "ready": true,
  "capabilities": {
    "new_session": true,
    "cancel": true,
    "text_deltas": false,
    "uploads": false,
    "voice": false,
    "tools": false,
    "agent_switch": false,
    "events_transport": "long-poll-json-v1"
  },
  "limits": {
    "absolute_ttl_seconds": 86400,
    "idle_ttl_seconds": 1800,
    "max_sessions": 3,
    "max_input_chars": 4000,
    "max_request_bytes": 32768
  }
}
```

public config 不返回其他访客数量、全局配额余额、Worker PID、模型凭证、内部 URL、真实机器名、全部配置或 Agent 清单。server 私下健康检查可以取得更详细状态，但不透传。

### Bootstrap / me

```json
{
  "ok": true,
  "lease_epoch": "opaque-epoch",
  "expires_at": "2026-09-21T00:00:00Z",
  "idle_expires_at": "2026-09-20T00:30:00Z",
  "csrf_token": "nonpersistent-csrf-value",
  "agent": {"id": "opaque-agent-id", "display_name": "HASHI Guide"},
  "sessions": [{"id": "opaque-session-id", "title": "New conversation"}]
}
```

示例时间仅说明格式，不用于部署。`agent.id` 只作为受限显示身份，不接受它控制后端路由。浏览器不取得 owner_id。内部 bootstrap 才有一次性的 visitor_token；公共 JSON 绝无此字段。

### Run 接纳

```json
{
  "idempotency_key": "client-generated-random-id",
  "text": "请介绍一下 HASHI。"
}
```

响应包括 `ok`、`session_id`、`run_id`、`message_id`、公开 `state` 和 `replayed`。禁止接受自由 metadata、system message、role、tool list、model、effort 或附件。文本按 Unicode code points 计数，两侧一致；同时限制 UTF-8 请求总字节数。空白消息拒绝。

幂等作用域为访客+Session+操作。同 key 同有效载荷返回同一个已接纳 Run；同 key 不同内容为 409。额度和并发检查不能抢在已接纳幂等重放前导致重复计费或无法恢复。网络失败不得换 key 重发原消息。

每位访客最多一个未结束 Run，包括排队、启动、生成；切换 Session 不绕过此限制。cancel 也验证 `(owner, agent, session, run)` 全链。Provider 请求在网络异常后可能结果未知；不宣称远程模型 exactly-once，按实施计划保守结算与处理。

### Snapshot / Events

snapshot 返回当前 Session 的有界 messages、公开活动状态、`lease_epoch`、`session_id`、`cursor`、`has_more`；需要更早页时只能使用该 Session 的不透明 `before` cursor。单页最多 100 条，按实际消息限制裁剪。

Events 首版统一为 JSON 长轮询投影：`cursor` + `wait_seconds`，上限 20 秒；每次最多 100 条。无事件返回空数组和当前 cursor，不为等待创建 Agent Worker。cursor 必须绑定 lease_epoch+Session；不能利用外部 cursor 跨 Session 读取，也不能触发无界回放。到期立即关闭等待，而不是等下一次查询。

允许的公开事件类别：`message.accepted`、`run.status`、`message.delta`、`message.final`、`run.error`、`session.expired`。这是协议投影名称，**不是添加新的 PAO 持久 Run 状态**。`run.status` 可显示 queued/starting/generating/completed/failed/stopped；以原生状态和实时生命周期事实映射，不复制权威状态机。

只有真实 Engine/PAO delta 通路经过验证时，才发布 `text_deltas=true` 和 `message.delta`；否则返回真实状态和最终消息。不得把完整答案分字假装模型流，也不得声称当前源码已经具备 token SSE。传输只观察原生事件，不新增持久事件总线或 WebSocket 服务。

每个事件带稳定 `event_id`/sequence、session_id、lease_epoch；重连可能重复，客户端去重并在需要时重新 snapshot。事件只包含公开答案和安全状态，剥离内部推理、系统 prompt、调用参数、路径、日志和其他 Agent 拓扑。慢读有缓冲上限，溢出要求重新 snapshot，不无限保留内存。

## 7. 错误、限流与缓存

统一错误形状：

```json
{"ok":false,"error":{"code":"demo_busy","message_key":"demo.busy","retry_after_seconds":5}}
```

| HTTP | 公开 code 示例 | 要求 |
|---|---|---|
| 400 | `demo_invalid_request` | 无原始 payload/栈 |
| 401 | `demo_identity_required` | 不凭 IP 自动恢复 |
| 403 | `demo_origin_denied` / `demo_csrf_denied` | 无跨源重试 |
| 404 | `demo_object_not_found` | 未知与他人对象同形 |
| 409 | `demo_idempotency_conflict` / `demo_run_active` | 不自动换 key |
| 410 | `demo_expired` | 清理 UI，需用户开始新体验 |
| 413 | `demo_message_too_large` | 不调用模型 |
| 429 | `demo_rate_limited` / `demo_busy` / `demo_budget_exhausted` | 合适的 Retry-After；不无限自动重试 |
| 503 | `demo_unavailable` / `demo_protocol_mismatch` | 不回退普通 API |

预算耗尽与瞬时忙碌通过 code 区分；日预算耗尽不使用几秒自动重试。所有动态响应，包括错误、bootstrap、me、snapshot、events，设置 `Cache-Control: no-store`；CDN 和 service worker 同样排除。静态带版本资源可以缓存。

## 8. 运行与清理不变量

1. 工具 catalog 为空且调用入口拒绝；关闭 slash、文件、语音、跨 Agent 通信、外部 Connector、模型自主后台工作和记忆晋升。不靠 prompt 代替授权。
2. PCM 只含公开材料和当前 Session 上下文。新 Session 不自动继承同访客其他会话，更不继承其他访客状态。
3. Worker、starting reservation、执行位和 pending Run 均有上限；在线 GET 不使 Worker 常驻。空闲 Worker 可被安全驱逐，永不强杀其他访客忙碌 Worker来满足新消息。
4. 绝对/idle 到期先撤销与 fencing，再取消、停 Worker、按 owner purge。清理失败只保持不可访问和重试，不回收旧身份给新人。
5. 不保留 Demo 对话备份、常规七天 quarantine、正文诊断或客户端永久草稿。全局预算可保留不含正文、不可重新关联已清理访客的聚合数，不能随 lease 清理被重置。
6. 本地服务内的数据清除不代表 SSD 取证擦除或第三方模型供应商副本删除；文案不得扩大承诺。

## 9. 协议联调与冻结

实现者应在 HASHI 增加机器可验证的 schema/fixtures，由本契约派生（建议未来路径 `contracts/shared_demo/v1/`）。客户端固定采用对应提交与版本并用实际 HTTP 响应检验，不维护另一份独立规范。缺失/未知版本、关键限制为 true、默认 profile 不符或 backend 未 ready，客户端均不得连接普通管理员接口兜底。

联调至少包含：同 IP 双访客、跨 Session/Run/cursor 越权、重复发送、过期长轮询、无工具副作用、预算跨重启、Worker 真正释放、正常模式不回归。详见 [测试计划](TESTING_PLAN.md)。此文档中的 DTO 示例只是设计，schema 通过不能替代行为验证。
