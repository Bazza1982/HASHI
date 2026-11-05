# FYI v1 implementation and release notes

Status: historical

These are dated implementation, packaging or instance observations carried from the pre-v2 FYI at source ea37129b. They preserve review evidence, not current global readiness, authorization, feature enablement or deployment. Consult the relevant domain guide and current contract before acting.

### FYI v1 005 Standby implementation qualification at migration

- Standby控制有独立停止容量；租约仅存储、到期非停采，未接Call/Phone/设备；Judge/ARMED未合格。[阶段](../../HASHI_STANDBY_IMPLEMENTATION_PLAN.md)。

### FYI v1 009 Personal L1 implementation observation at migration

- 个人L1：认证前端决定、自动弹窗、免重复登录；范围/版本/时长/幂等/原操作续跑不变，governed/L2除外。
  Adapter设超时，只读视图禁写，保留epoch日志/不可变检查；实测独立验收。[权限](../../permissions/IMPLEMENTATION_STATUS.md)。

### FYI v1 055 Dated portable speech packaging observation

Deploys add Edge/Kokoro/SAPI without changing settings; the 957 MB portable keeps
Edge/SAPI/Piper, omits Kokoro/Torch. See
[deployment](../../repairs/FREE_TTS_DEPLOYMENT_DECISION_20261015.md).

### FYI v1 056 Historical records are reference only

[Records](../../AGENT_FYI_HISTORY_MEDIA_20261009.md) grant no authority.

### FYI v1 057 Instance Wiki consolidation evidence at migration

Lily Wiki needs new candidates plus consolidation/embed evidence; a Scheduler
timestamp is insufficient.
