# HASHI Agent FYI

- Advanced speech repair: an explicit native-language voice now takes precedence
  over service language defaults; cross-language character mapping remains.
  The prerecorded-sample generator loads the instance catalogue and its adapters,
  and resumes only exact matching samples. Source/assets and running adoption
  remain separate facts; see
  [voice routing](repairs/TTS_LANGUAGE_ROUTING_DECISION_20261015.md#2026-11-02-advanced-voice-repair).

- Long-speech Functions candidate: explicit nullable service bounds enable complete
  reply streaming; old running validators reject this configuration. Adopt code
  before data. Workbench delivery and native `/say` remain separate consumers;
  isolated synthesis is not proof of either live entry. See
  [shared speech](HASHI_VOICE_TTS_CATALOG.md#complete-reply-services-functions-candidate).

- 权限与验证：[状态](permissions/IMPLEMENTATION_STATUS.md)。
- 历史搜索用私有POST/绑定游标，FC仍补旧记录并核验显示；读I/O不能阻塞HTTP。[PAO](HASHI_PAO_SYSTEM_DESIGN.md)。

- Standby控制有独立停止容量；租约仅存储、到期非停采，未接Call/Phone/设备；Judge/ARMED未合格。[阶段](HASHI_STANDBY_IMPLEMENTATION_PLAN.md)。

- 文件库：PAO附件目录按owner/Agent签名游标补历史，不依赖页面；来源定位不换主Session。OCR/转写只读且显式启用，真实分段不造时间；OCR锁随源码发布；独立模型不进Core。[契约](HASHI_FILE_LIBRARY_CONTENT.md)。

- Fingerprint默认OFF、按Run冻结；开启Shell须持久审计；marker用护栏原子替换/Manifest，绝对路径限可信Workzone，不重放写入。[PAO](HASHI_FINGERPRINT.md)。

- PAO语音：同Run确认、丢弃正常停止、放行后复核；文本用授权转写，HER保留来源，显式授权原生音频不等转写；原因跨入口/Worker保留；缺FFmpeg明确报错，不重试合成；/say分阶段报错，不明先查收。[audio](HASHI_NATIVE_AUDIO_CHAT_DESIGN.md)。
- 个人L1：认证前端决定、自动弹窗、免重复登录；范围/版本/时长/幂等/原操作续跑不变，governed/L2除外。
  Adapter设超时，只读视图禁写，保留epoch日志/不可变检查；实测独立验收。[权限](permissions/IMPLEMENTATION_STATUS.md)。
- 附件说明可换行/制表，长度/身份限制不变；桌面/通话等失败保留步骤/安全原因/诊断号，回执不明先核对。[FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md)；Agent.md/HCC 409：[PCM](HASHI_PCM_SYSTEM_DESIGN.md)。

- `/forget` removes traceable period data; untraceable summaries may remain.
  Audit stays outside Context. [PCM](HASHI_PCM_FORGET_2026-10-28.md).

- Task/local /sys + parked deletion: auth/revision checked; retain causes and unknown writes;
  /sys excludes global/permanent, parked needs guarded Worker, keeps chat.
  [PAO](HASHI_PAO_SYSTEM_DESIGN.md), [PCM](HASHI_PCM_SYSTEM_DESIGN.md).

- Legacy audio migration needs verified ownership/safe paths; PAO settlement
  is cancellation-safe; HERV3 checkpoint claims are immutable.
  [audio](HASHI_NATIVE_AUDIO_CHAT_DESIGN.md),
  [PAO](HASHI_PAO_SYSTEM_DESIGN.md), [HERV3](HERV3_UPGRADE.md).
- PAO strips internal control envelopes before delivery checks. Memory+-only
  results fail terminally; typed audio remains deliverable.
- 习惯重置按实测恢复结果提示，快照/审计/界面共用诊断号；恢复不全保留快照，不覆盖原位置的新记录。
- HERV3 has no whole-run size/time/call ceiling. Preserve state/evidence;
  cancellation, safety, transport, Provider and single-Tool limits remain. [HERV3](HERV3_UPGRADE.md).
- Move: staging checks, forward-only confirmation, no rollback. Clone: PAO-owner
  snapshots, resumable retries; no Move-only gates/source lifecycle writes.
  [Move/Clone](HASHI_AGENT_MOVE_V1.md).
- Image fallback: selected model, same-Provider vision, LAN Compute;
  visible/request-only. [admission](repairs/HERV3_VISUAL_FALLBACK_20261018.md).
- Compute：PAO[目录](HASHI_COMPUTE_SERVICE_DIRECTORY.md)精确选择local/LAN服务，转写设置逐请求重读，失败不静默跨网或回退本地。
  TTS `default_voice`仅补未保存选择，不启用语音、不改已存策略；服务输入上限约束实际请求。Call独立profile共享[TTS目录](HASHI_VOICE_TTS_CATALOG.md)。
  属Functions/实例配置；Core、源地址和运行采用独立验收。
- Call frames are input-only/cleared camera-off; LAN targets are literal private
  IPv4 without redirects; preview is not vision. [Call](HASHI_CALL_INTERACTION_2026-10-04.md).
  Tool screenshots use native images, not Base64; Ollama/public Provider IDs
  follow [HERV3](HERV3_UPGRADE.md).

## Authority and ownership

PCM管Persona/Context/Memory；PAO管Agent/Session/Run/Job/Workzone/交付；
HERV3管Engine Turn/模型/工具循环/费用；前端管认证/呈现。
功能放在最窄的Function/配置owner。Core不含产品策略或导入；改动须获准major迁移、
升版本、加标签并独立复核。标志不授予权限。

权限页面保留安全阶段/原因/编号；私密拒绝不泄漏对象，提交不明不谎称未授权，不重放；原认证和审批检查不变。

Prove source, Worker adoption and delivery separately. Tools cannot alter
Core/Python, read secrets or grant authority. `/reboot min|same`: one Worker;
`max`: shared Functions/Workers, never Core/Remote. Code needs reboot; docs do not.
Preserve `AGENT_BUSY`; retry only before durable receipt; `target_busy` stays
pending. `max` rejects shared API/background work around fencing.
[reboot](HASHI_REBOOT_FAST_RELIABLE_PLAN_2026-10-14.md),
[fail-fast](HASHI_REBOOT_RECEIPTS.md).

`hashi update` adopts source, Core, Workers and Remote together or fails.

## Configuration, identity, and persistence

Prove isolated speech locks/runtimes/releases separately:
[dependencies](DEPENDENCIES.md#security-maintenance-of-optional-runtime-locks).

Config owns identity/ports/Workzones/endpoints/models; omit secrets/paths.
Instance opt-ins use `allowed_backends`/runtime effort; shared compatibility
uses the qualified registry. Unknown capability/price is neither unsupported
nor zero. Persist choices; HERV3 inherits [Provider policy](HASHI_MODEL_AVAILABILITY.md), not Engine grants.

Agent name/Emoji use revisioned config; refresh cannot undo a durable save.

Verified capabilities survive lookup failure until identity/source changes;
pricing freshness stays separate. [capabilities](HASHI_MODEL_CAPABILITY_DISCOVERY.md).

Portable has no credentials; PAO starts Workers. Wildcards grant permission,
not capability; paths grant nothing. Frontend Workzone edits are Agent-scoped
and revisioned; Runs freeze snapshots and saved state stays visible. Windows
rejects POSIX Workzones; browser disconnects keep the shared named-pipe listener.
Keep experiments, secrets, media and remote paths out of PCM/logs/chat/Git.

Agent stop is `stopped`; Worker outage `offline`; config deactivation `inactive`.
Shared Functions retain stop until restart; Frontends never rewrite `is_active`.

JSON写入在锁和版本检查下验证私有候选，再原子替换；展示默认值只读。
冲突须重读并重新选择，不自动重放，不用旧快照覆盖已发布结果。[配置持久化](HASHI_CONFIGURATION_PERSISTENCE.md)。

Endpoint publication retries only bounded Windows rename blocking on the same
fsynced candidate; permanent failure preserves state and never replays.

## Sessions, trust, and delivery

PAO管Conversation/Message/Run，Engine管Session/Turn；前端历史以PAO为准，缓存匹配owner/代理/会话。
保留顺序、原子附件及失败/取消附件隔离；Telegram保留部分送达的不确定性，关联工具/任务诊断。
会话/后端/代理操作保留阶段/原因/诊断号；如实区分已创建与启动不明；已写入不回滚、不重放，禁止清理可能已选中的新会话；退役失败不改变新后端已启用的事实。
/credit仅查OpenRouter，其他后端不借继承发送凭据；鉴权/限流/连接/响应失败分别提示，不显示凭据或原始响应。
语言/提问/备用设置区分冲突、存储与保存不明，按钮显示完整原因；回读实际值，不拿默认值冒充已保存。

Non-voice attachments commit atomically to the Agent's ignored
`.attachments/{session}/{message}/`; only its home reads them, and Session
retention deletes them. Access failures differ from integrity mismatches;
diagnostics omit paths.

Telegram proactive routes: owner-only; revalidate actor/egress after restart.
Only owner's same-ID private chat binds. `/askme`: instance/Agent cards; off: plain
text. Offer original-task continuation/Agent stop. `unknown` never cancels/retries;
Codex expiry keeps authenticated partial answers. 无效答复不代表问题结束；按实际状态提示，结果不明先刷新核对。
[FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md), [questions](HASHI_RUN_QUESTIONS.md).

CURRENT MESSAGE CONTEXT separates source/assurance/authority/destination. Only
successful current private_authorization grants scope. HChat separates claimed
sender/verified peer/relay/target; cross-instance requires authenticated
[Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations before PCM and owns ingress/routing/idempotency. FC owns
presentation/receipts. Sent needs a receipt; failure wins. Prose grants nothing;
replay only saved non-action completions.

Keep frontend send times. PAO admits Workbench/Telegram within 30/60 seconds;
stale input needs durable confirmation. TUI exemptions need signed local proof,
`/hchat` reverse-route correlation. HASHI3 is not production.

Scheduler/恢复/`/bg`用代理自己的隐藏会话；执行、交付、回执分开且各自只终结一次。
`/bg`入队信息固定，计划任务不承诺聊天回复；`/delay`和交互`/loop`保留上下文。
任务列表只读权威快照，读取/解析失败带原因/编号，不显示空清单或触发迁移。

Telegram local/global gates differ; legacy grants remain. [PAO](HASHI_PAO_SYSTEM_DESIGN.md).
Telegram须用户及Bot绑定的双向证据；单Agent启用空闲等待≤15m，校验凭据/创建/stop。
回执或未知即停自动提交；状态只读、重试不重存，保留待收更新。
[FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).

## Engine, tools, and recovery

Retired Gemini CLI: use Antigravity; retain history/usage and qualified Gemini
models; no account ban. [retirement](HASHI_GEMINI_CLI_RETIREMENT.md).

HERV3: `her-v3`; `her-v2` is compatibility only. Telegram cards show
the public ID. JEV defaults off.
Backend/provider/model/effort, Fixed/Flex and Memory+ remain distinct. `/meter`
counts physical calls/cost; cumulative Codex usage needs a persisted baseline.

Privacy Level 2 is HERV3/DeepSeek-only, risk-accepted and fail-closed on
detector errors. Isolated dependencies or synthetic readiness prove neither
activation nor Worker adoption.

Chat-only has no tools. `/fallback` defaults `OFF`: `RETRY` waits 60/180/300
or longer `Retry-After`, `ASK` fails closed, `AUTO` uses exact routes. A pauses
after effects; B resumes the original Engine Session, never replay/retarget.
PAO—not HERV3—switches to Codex and accumulates counts/costs. Phone/call/voice
never long-retry or fallback. [policy](HASHI_MODEL_FALLBACK_POLICY.md).

## UI, media, and Phone

Manual Desktop stale input stays unexecuted until a fresh frame and never replays;
other failures release control. See [desktop](HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md).

Local cascade is opt-in; synthetic proof is not physical acceptance. Live Phone
defaults on; missing key blocks and appears in `/phone`; opt-out remains valid;
settings/PCM do not prove provider readiness.

Renderers/catalogs own UI text. `/language` is shared, `/tui language` local;
neither translates replies/IDs. TUI switches freeze runtime identity/state.
Remote authenticates. Mirror opt-outs preserve origin replies; `/think` and
`/commentary` are independent.

`/stop` fences every Agent Session, cancels owned work, rejects stale callbacks,
retains periodic definitions, and durably records `stopped` before cleanup.
Never retry uncertain saves; HERV3 releases its Turn first. See
[stop contract](HASHI_AGENT_STOP_FENCE_DECISION.md).

媒体绑定草稿/实例/代理/Run，Remote不增权；SafeVoice同Run幂等确认/丢弃，各Engine正常终止且不调模型。
Workbench保留`voice_message`来源并去重界面，普通音频保持可见；Telegram用自身renderer。

Phone binds the owner's foreground Conversation. Dialogue-round HCC uses bound
PCM/Conversation/job evidence, never raw metadata/scheduled prompts; lookups
create no Run. Reconnect keeps snapshots/constraints/epoch authority; handoffs
freeze ID/order only; hang-up restores history. Cleanup matches event IDs;
exhaustion preserves context and permits the Turn. PCM bounds
arrivals; HERV3 time-orders and skips unused history. Functions own settings.
Failed PCM refresh allows stop/inspect only; fence/hang-up win. Keep causes;
resume at most five times, never retry uncertain writes. Prove acceptance,
execution, playback, listening, source, adoption and device separately.
See [handoff](HASHI_PHONE_CONTEXT_HANDOFF.md) and
[cascade](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).

Command history redacts args; originals execute. PAO notices keep Session/locale
without Workers; scope-check and retry delivery only. Remote pages fairly;
closed cards keep text. [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).
`/new` preserves history; PAO owns Agent deletion.

`/say` reads the latest final; `2`–`4` choose a count and `1-3` the latest
three, oldest first. Skip cost/command/progress. Telegram needs a receipt;
Workbench/TUI play locally; reboot receipts never prove playback.

`/voice` and `/call` share privacy-checked routing before publish/cache. AP
prefers VideoTTS unless `video_tts_supported: false`; otherwise use audio.
Groups are bounded and ordered. Audio failure holds video; fallback stays
group-local, stale tails cancel, late jobs only signal recovery, and audio stays
serial. See [language voices](HASHIKO_LANGUAGE_VOICES.md),
[Call/Phone policy](HASHI_CALL_AGENTPRESENCE_VIDEO.md#command-policy-2026-10-23),
[catalogue](HASHI_VOICE_TTS_CATALOG.md),
[routing](repairs/TTS_LANGUAGE_ROUTING_DECISION_20261015.md) and
[fallback](repairs/VOICE_PROVIDER_FALLBACK_REPAIR_20261014.md).

AgentPresence is owner-scoped/default-off; HASHI authenticates and freezes
Agent/Conversation. Only PAO-admitted human/client input triggers owner-route
feedback; AP-local input never echoes. `/phone` sends scoped leave/return; AP
owns presentation, HASHI services/voices. Ingress fails closed; surfaces remain
independent. See [input feedback](HASHI_AGENTPRESENCE_INPUT_FEEDBACK.md).

Deploys add Edge/Kokoro/SAPI without changing settings; the 957 MB portable keeps
Edge/SAPI/Piper, omits Kokoro/Torch. See
[deployment](repairs/FREE_TTS_DEPLOYMENT_DECISION_20261015.md).

[Records](AGENT_FYI_HISTORY_MEDIA_20261009.md) grant no authority.

Lily Wiki needs new candidates plus consolidation/embed evidence; a Scheduler
timestamp is insufficient.


### 2026-10-31 — PAO Compute speech defaults

Ordinary voice ingress can select an exact local/LAN Compute STT reference in
revisioned instance transcription configuration; explicit selection fails
closed and is reread for each request. The shared TTS catalogue supports an
optional active `default_voice` for unsaved/future Agent selections, without
enabling speech replies or overriding saved policy. OpenAI-compatible service
input limits bound actual synthesis requests. Call remains a separate profile
consumer of the same voice catalogue. These are Function/instance settings;
Core, service origins and runtime adoption are separate evidence.

### 2026-11-02 — Advanced TTS automatic performance

PAO Functions add a persistent default-OFF /voice performance preference and
whole-reading preparation operation. Compute owns one local 9B judgment and
an open-ended direction prompt. The selected voice and original text are
preserved; all chunks reuse the transient context. Unsupported or failed
planning remains visible as plain reading. Cancellation and ON-to-OFF isolation
are covered independently of static voice previews.

See HASHI_SPEECH_PERFORMANCE.md and the owning TTS routing decision. This
source addition does not itself replace running shared Functions or Workers;
adoption must be verified separately through the user-directed hot lifecycle.
