# HASHI Provider 无关多模态输入升级与测试计划

## 文档控制

| 字段 | 内容 |
|---|---|
| 状态 | 已合并并通过离线验证；通用热重载已验收，真实多图专项验收仍待执行 |
| 批准日期 | 2026-08-23 |
| 适用范围 | HASHI Agent Runtime、HER v2、API Gateway、Provider Adapters、Telegram /long |
| 首要验收对象 | Momo 多图读取 |
| 代表性原生 Provider | OpenRouter Gemini、HASHI API 所服务的 Codex/GPT 模型 |
| 兼容对象 | 不具备原生多模态能力的现有 backend/model |
| 发布要求 | 在宣称具体 Provider 多模态生产可用前，须完成真实媒体专项 canary |

## 1. 批准结论

HASHI 的多模态能力必须采用 Provider 无关、Model 精确、逐模态判定的架构。

1. 当前阶段实际选中的 provider/model 原生支持某种媒体时，原始媒体必须以该
   Provider 接受的结构化内容形状直接进入模型。
2. 当前模型不支持该媒体时，必须保留并使用现有本地解释链路，包括
   media_read、vision_inspect、OCR、音频规范化与转录、PDF 解析以及视频抽帧。
3. HASHI API 所服务的 Codex/GPT 模型只是一个适配实例；OpenRouter Gemini、
   Claude 或未来任何具备多模态能力的模型均应遵守同一内部契约。
4. HER v2 的 Immediate Response、Triage 及后续前台阶段必须按各自实际选择的
   provider/model 独立解析能力，不得依赖 HER 外层 supports_files 布尔值。
5. 图片路径、文件名及 transport receipt 只能证明媒体已接收，不能证明模型已
   看到或理解媒体内容。

本升级不废除现有媒体回退系统，也不把所有媒体无条件发往远端 Provider。

## 2. 问题陈述

当前系统已经能够可靠接收 Telegram 多媒体并保存附件 receipt，但在部分链路中：

- 多媒体被表示为 prompt 中的本地路径；
- API Gateway 的无工具路径会把 OpenAI multipart content 扁平化为文字；
- 扁平化时只保留 text part，image part 被丢弃；
- Codex app-server 转换层已有图片 part 支持，但目前主要由外部工具协议路径使用；
- OpenRouterAdapter 与 HashiApiAdapter 的普通 generate_response 接口只接受
  prompt 字符串；
- HER v2 StageRequest 当前只包含 goal 和 context，没有一等多模态输入；
- supports_files 与 supports_native_vision 无法表达逐模型、逐媒体类型及传输形状。

因此，Triage 可以在逻辑上正确判定 DIRECT_RESPONSE，但 Immediate Response
实际上可能只收到路径文字。该问题属于能力声明与实际 wire payload 不一致，而
不是 Triage 分类错误。

## 3. 目标与非目标

### 3.1 目标

- 多张图片零丢失、顺序稳定、身份稳定；
- 原生多模态模型直接接收原始媒体；
- 非原生模型继续使用已工作的本地媒体解释链路；
- 同一 Provider 下不同模型可以具有不同能力；
- 同一模型可以支持图片而不支持音频、视频或 PDF；
- HER 每个前台阶段都使用实际路由目标进行能力判断；
- tools 缺省、tools=[]、有工具三种路径均支持结构化多模态；
- 同步与流式行为一致；
- 原始媒体字节不进入持久化 metadata、ledger、日志或 transcript；
- Provider 能力漂移时失败关闭，并只对明确的不支持错误执行一次安全回退。

### 3.2 非目标

- 不移除或重写已工作的 media_read 安全与规范化实现；
- 不把 HASHI API 设为多模态架构中的特殊分支；
- 不把 supports_files=True 等同于支持全部媒体；
- 不从文件名、caption 或 receipt 推断媒体内容；
- 实现过程中不擅自重载服务或执行线上验收；重启与测试由用户明确负责；
- 不默认把当前回合原始媒体复制给 Meditation 或 Dream 后台学习任务。

## 4. 统一内部输入契约

### 4.1 Canonical Request Content

文本 prompt 与附件必须分离。建议引入版本化的 canonical request content，
其逻辑形状如下：

~~~json
{
  "version": 1,
  "parts": [
    {
      "type": "text",
      "item_index": 1,
      "text": "Compare all images and report once."
    },
    {
      "type": "media",
      "item_index": 2,
      "attachment_id": "attachment-...",
      "modality": "image",
      "kind": "photo",
      "mime_type": "image/jpeg",
      "filename": "photo.jpg",
      "caption": "",
      "local_ref": "...",
      "size_bytes": 12345,
      "sha256": "...",
      "transport": {
        "media_group_id": "album-1",
        "message_id": 51
      }
    }
  ]
}
~~~

约束：

- item_index 必须反映用户原始顺序；
- attachment_id 在同一请求、重试和各 HER 阶段中保持稳定；
- 同名文件仍须具有不同 attachment_id、local_ref 和完整性信息；
- local_ref 只能在授权边界内解析；
- data URL、Base64 或原始 bytes 不得写入该持久化结构；
- Provider Adapter 只能在最后一跳临时物化远端需要的媒体格式。

### 4.2 能力契约

能力必须至少表达以下维度：

~~~text
provider
model
input_modalities: text | image | audio | video | document
input_transports by modality: local_path | data_url | remote_url | inline | provider_file
limits: item_count | item_bytes | total_bytes | dimensions | duration | page_count
privacy eligibility
capability source: registry | explicit config | verified probe
~~~

能力解析不得只查看 engine，也不得以 supports_files 作为最终判据。

推荐优先级：

1. 经验证的 model-specific registry；
2. 明确且经过 schema 校验的 Agent/provider 配置；
3. 可选的运行时 capability probe；
4. 未知模型或未知模态默认不具备原生能力，进入本地回退或显式失败。

### 4.3 逐附件路由算法

对每个需要语义理解的附件执行：

1. 校验 attachment_id、授权根、真实路径、MIME、签名、大小和 SHA-256；
2. 解析当前 HER 阶段实际选中的 provider/model；
3. 查询该 model 对当前 modality、transport、limits 和 privacy 的能力；
4. 若原生支持，在 Adapter 边界临时转换为 Provider wire shape；
5. 若不支持，进入现有本地媒体解释链路；
6. 混合模态请求逐附件独立决策；
7. 按原始 item_index 重组 Provider 输入或回退证据；
8. 记录安全的 routing decision，不记录媒体字节。

同一个附件不得在同一阶段既原生直传又调用回退解释，除非原生请求明确返回可识别
且无副作用的 modality unsupported 错误。

## 5. HER v2 阶段传播契约

### 5.1 前台阶段

完整 attachment manifest 必须随 TurnState 与 StageRequest 传播到：

- Immediate Response
- Triage
- Planning
- Execution
- Replanning
- Review
- Verification
- Finalisation

每个阶段按自己的实际 profile 独立决定原生直传或回退。阶段的 allow_tools=False
只禁止工具调用，不得禁止原生多模态输入。

### 5.2 DIRECT_RESPONSE

对于内容可由 Immediate Response 直接回答的多媒体请求：

- Immediate Response 与 Triage 必须各自收到相同且有序的原始媒体；
- Triage 可以返回 DIRECT_RESPONSE；
- Immediate Response 的结果成为唯一最终答案；
- Planning 和 Execution 不应启动；
- 原有并发竞态、provisional resolution 和单次交付契约保持不变。

若 Triage 返回 DIRECT_RESPONSE，但 Immediate Response 无法消费任务所必需的
媒体，则 runtime 不得交付一个假装看过媒体的答案。它必须进入已批准的本地回退
工作路径，或以明确的 typed failure 失败。

### 5.3 工作路径

当原生直答不可行时：

- Triage 可根据用户文字和媒体依赖将请求归为工作任务；
- Execution 继续使用 media_read 或 vision_inspect；
- 比较多图时，每个相关附件必须有独立 evidence/provenance；
- Review 与 Verification 必须能区分哪些附件已检查、哪些失败；
- Finalisation 不得声称检查了未成功读取的附件。

### 5.4 重试与 Sub-agent

- Provider retry 与 structured repair 必须使用相同 attachment manifest；
- 不得在重试中重新下载、重新编号或丢弃附件；
- retry invariant hash 应覆盖附件身份与完整性摘要；
- Sub-agent 只继承 assignment 明确授权的附件；
- 要求比较全部图片的 assignment 必须继承全部相关附件；
- Sub-agent 的结果必须携带 attachment_id 对应关系。

### 5.5 后台维护阶段

Meditation 与 Dream 默认只接收经过最小化的文字记录，不自动继承原始媒体。未来如
需媒体学习，必须另行定义显式授权、保留期、删除和审计契约。

## 6. Provider 与 Gateway 契约

### 6.1 OpenRouter

对于已声明支持图片的 Gemini 或其他模型：

- messages.content 必须保持 OpenAI-compatible multipart 结构；
- 图片按顺序转换为 image_url 或 Provider 明确支持的等价 part；
- detail、MIME 和输入限制必须被保留或显式规范化；
- 无工具请求不得退回纯字符串 generate_response；
- sync 与 stream 必须使用相同的内容构造函数；
- 原生成功路径不得调用 media_read 或 vision_inspect。

同一 OpenRouter engine 下的文本模型不得继承 Gemini 的图片能力。

### 6.2 HASHI API

HashiApiAdapter 必须：

- 保留结构化 messages；
- 保持 reasoning_effort 请求级传播；
- 不添加 OpenRouter 专用 reasoning 对象或认证头；
- 在 tools 缺省、空数组及存在工具时均不丢失媒体 parts。

### 6.3 HASHI API Gateway

Gateway 必须把下列两个判断分离：

1. 是否使用 caller-owned external tool protocol；
2. 是否需要 structured conversation protocol。

tools=[] 仍然不应进入 external tool mode，但只要 messages 含媒体，就必须进入
structured conversation path，不能调用只提取 text part 的 prompt flattener。

### 6.4 Codex app-server

Codex 转换层必须覆盖：

- 最终 user message 的多图 turn/start.input；
- 历史 user image 的 thread history；
- tool result 中的结构化图片；
- detail 变体与 data URL；
- 无工具请求；
- 有工具请求及 tool result 续接；
- sync/stream Gateway 响应一致性。

## 7. 强制 Assertions

| ID | Assertion |
|---|---|
| MM-A01 | 能力由 provider + model + modality 共同解析 |
| MM-A02 | 未知 model/modality 默认失败关闭 |
| MM-A03 | 原始 item 数量、顺序与 attachment_id 在全链路稳定 |
| MM-A04 | prompt 中路径或 receipt 不算作媒体已传送 |
| MM-A05 | 原生成功路径不调用本地媒体解释工具 |
| MM-A06 | 非原生路径不向 Provider 发送不支持的原始媒体 |
| MM-A07 | 非原生路径完整保留现有本地媒体解释能力 |
| MM-A08 | 混合模态逐附件路由并按原顺序重组 |
| MM-A09 | HER 每个前台阶段按自己的实际 model 决策 |
| MM-A10 | allow_tools=False 不阻止原生媒体输入 |
| MM-A11 | 有效的多模态 DIRECT_RESPONSE 只交付一次 |
| MM-A12 | 不可兑现的 DIRECT_RESPONSE 不得假装成功 |
| MM-A13 | tools 缺省、tools=[]、有工具三种路径均不丢媒体 |
| MM-A14 | sync 与 stream 的媒体输入等价 |
| MM-A15 | 重试、Replan 和结构修复不改变附件身份 |
| MM-A16 | Sub-agent 只接收授权附件且不丢任务必需附件 |
| MM-A17 | 只有 typed modality unsupported 可触发一次自动回退 |
| MM-A18 | 损坏、越界、哈希变化或超限附件不得静默忽略 |
| MM-A19 | Base64/bytes 不得进入持久化 metadata、ledger、日志或 transcript |
| MM-A20 | 每个已解释媒体结论均可追溯到 attachment_id |

## 8. 新增测试计划

### 8.1 统一契约与能力解析

建议新增 tests/test_multimodal_contract.py：

- test_capability_is_resolved_by_provider_model_and_modality
- test_unknown_model_fails_closed_to_local_fallback
- test_native_image_support_does_not_imply_audio_video_or_pdf
- test_mixed_modalities_are_routed_per_attachment
- test_privacy_policy_can_force_local_processing
- test_canonical_envelope_preserves_order_identity_and_integrity
- test_persistent_metadata_never_contains_inline_media_bytes
- test_capability_limits_are_checked_before_provider_submission

### 8.2 Telegram 与 /long Intake

扩展 tests/test_runtime_long.py、tests/test_runtime_media.py 和
tests/test_runtime_pipeline.py：

- 五张相册在 /end 先到时仍只形成一个请求；
- message_id、item_index、attachment_id 与图片顺序完全一致；
- 同名文件具有唯一 local_ref 与 attachment_id；
- media-only batch 仍生成有效 canonical request content；
- QueuedRequest 与 current_request_meta 深拷贝 attachment manifest；
- metadata 中没有 Base64 或原始 bytes；
- timeout、SafeVoice、chat scope 和 quiet-window 行为不回归。

### 8.3 OpenRouter 原生多模态

建议新增 tests/test_openrouter_multimodal.py：

- test_gemini_receives_ordered_native_images_without_tools_sync
- test_gemini_receives_ordered_native_images_without_tools_stream
- test_openrouter_native_image_payload_preserves_detail_and_mime
- test_openrouter_native_success_does_not_call_media_fallback
- test_openrouter_text_model_uses_local_fallback
- test_same_engine_models_do_not_share_image_capability
- test_openrouter_mixed_image_audio_routes_per_modality

### 8.4 HASHI API 与 Gateway

扩展 tests/test_hashi_api.py，并建议新增
tests/test_api_gateway_multimodal.py：

- test_hashi_api_preserves_multipart_messages_and_reasoning_effort
- test_gateway_multimodal_without_tools_uses_structured_conversation
- test_gateway_multimodal_with_empty_tools_uses_structured_conversation
- test_gateway_multimodal_with_external_tools_preserves_all_parts
- test_gateway_multimodal_sync_and_stream_are_equivalent
- test_gateway_does_not_flatten_image_parts_into_prompt_paths
- test_gateway_rejects_invalid_or_unsupported_media_without_silent_drop

### 8.5 Codex app-server

扩展 tests/test_codex_api_tool_bridge.py：

- test_final_user_multiple_images_preserve_turn_input_order
- test_historical_user_images_preserve_history_shape
- test_assistant_images_do_not_become_user_input
- test_structured_tool_image_result_preserves_function_output
- test_no_tools_codex_request_still_accepts_images
- test_codex_image_detail_variants_round_trip
- test_codex_multimodal_tool_continuation_preserves_public_tool_names

### 8.6 HER v2

建议新增 tests/test_her_v2_multimodal.py：

- test_direct_response_and_triage_receive_same_ordered_images
- test_multimodal_direct_response_delivers_once_without_execution
- test_allow_tools_false_still_allows_native_media_input
- test_each_foreground_stage_resolves_its_selected_model_capability
- test_text_only_stage_uses_existing_media_fallback
- test_unfulfillable_direct_response_is_not_delivered
- test_stage_retry_preserves_attachment_manifest
- test_replan_review_and_verification_preserve_attachment_provenance
- test_subagent_receives_only_authorised_attachment_subset
- test_compare_all_assignment_receives_every_image
- test_finalisation_does_not_duplicate_direct_response

### 8.7 故障、安全与端到端

- Provider 返回明确 modality unsupported 时，本地回退恰好执行一次；
- 鉴权、限流和连接错误不得触发媒体回退；
- 单张损坏时明确指出 attachment_id，不能声称全部已检查；
- 路径逃逸、symlink、签名不匹配及哈希变化继续失败关闭；
- 请求超过模型单项或总大小限制时不得静默截断；
- Momo + HASHI API/Codex 真实多图验收；
- HER + OpenRouter Gemini 真实多图验收；
- 文本 backend 的既有本地识图端到端回归。

真实验收 fixture 应包含至少三张明显不同且需要跨图比较的图片，避免只验证模型能
读取第一张。答案应能指出每张图片的独立属性及跨图关系。

## 9. 现有测试处置

### 9.1 保留

以下测试或测试组的契约仍然正确：

- /long 的竞态、超时、chat scope、SafeVoice 和单请求提交；
- media_read 的图片、OCR、PDF、音频、视频、路径、symlink、MIME 与资源限制；
- test_direct_response_race_delivers_exactly_one_answer；
- test_direct_response_still_requires_valid_immediate_content；
- caller-owned external tool passthrough、工具名映射、并行工具和 usage；
- test_empty_tools_do_not_force_legacy_clients_into_external_mode；
- 非 HER wildcard 不自动授权 media_read 或 vision_inspect。

test_empty_tools_do_not_force_legacy_clients_into_external_mode 必须保留，因为 structured
multimodal routing 与 external tool routing 是两个独立概念。

### 9.2 修正

#### test_multimodal_batch_preserves_item_order_and_requests_one_response

保留顺序与单响应断言。删除“路径存在于 prompt 即表示媒体已传送”的语义，改为
检查 canonical parts、attachment_id、item_index 和完整性字段。

#### test_cmd_end_enqueues_media_batch_once

保留单次 enqueue，新增 request content 与 attachment manifest 断言。

#### test_document_is_collected_without_enqueue_during_long_batch

保留收集行为；从 prompt 路径断言改为附件结构与 receipt 状态断言。

#### test_multiple_documents_and_task_enqueue_as_one_request_on_end

保留文件顺序与单报告要求；新增多个 attachment part 的顺序和身份断言。

#### test_end_before_five_photo_album_still_enqueues_one_multimodal_request

保留全部 album race 覆盖；增加五个结构化 media parts 及 HER 阶段传播断言。

#### test_begin_queue_item_preserves_multimodal_request_metadata

增加 deep-copy、manifest 版本、无媒体字节持久化与附件身份稳定断言。

#### test_triage_receives_complete_policy_and_minimal_turn_prompt

继续要求最小文字 prompt；新增独立断言，证明图片通过 structured input 进入
Provider，而没有被拼入 prompt 或内部控制字段。

#### test_backend_declares_configured_native_vision

从 supports_native_vision 单一布尔断言升级为逐模态、逐 transport 能力断言。

#### test_gateway_strips_image_blocks_for_text_only_backend

保留行为，但重命名并限定为非原生回退 Adapter/Gateway Context 行为。通用 API
Gateway 不得无条件剥离图片。

#### Runtime 测试 fixtures

删除硬编码 supports_files=True/False 所表达的全媒体假设，改为显式
input_modalities 与 fallback capabilities。

### 9.3 删除并替换

删除旧契约测试：

- test_backend_manager_exposes_vision_only_in_tool_mode

该测试假定 native 与 fallback tool 必须互斥。新契约允许：

- 原生支持时优先直传；
- 已配置 fallback 保留，用于其他不支持的模态或明确 capability drift；
- 同一附件在正常路径不得被原生与 fallback 重复处理。

替代测试：

- test_native_media_is_preferred_without_invoking_fallback
- test_fallback_remains_available_for_unsupported_modalities
- test_one_attachment_is_not_processed_by_both_paths

除该旧互斥契约外，不应删除现有媒体回退测试覆盖。

## 10. 测试执行层级

### Level 1：纯单元测试

- canonical schema；
- capability resolution；
- per-part routing；
- HER stage propagation；
- Provider payload builder；
- typed failure classification。

不得依赖网络或真实 Provider。

### Level 2：Fake HTTP / Fake IPC 集成

- 捕获 OpenRouter request JSON；
- 捕获 HASHI API Gateway request；
- 捕获 Codex app-server turn/start 与 history injection；
- 验证 sync、stream、tools 与 tool results；
- 验证调用计数，确保原生与 fallback 不重复执行。

### Level 3：现有核心回归

- runtime media 与 /long；
- HER v2；
- API Gateway；
- HashiApiAdapter；
- Codex bridge；
- media_read 与 vision_inspect；
- 核心门禁测试。

### Level 4：真实 Provider 验收

在代码合入并获得明确重载授权后执行：

1. Momo + HASHI API/Codex 多图 DIRECT_RESPONSE；
2. HER + OpenRouter Gemini 多图 DIRECT_RESPONSE；
3. 文本模型多图本地回退；
4. mixed modality；
5. sync/stream；
6. tools 缺省、tools=[] 与存在工具；
7. 日志、ledger 与 transcript 无媒体 bytes。

## 11. 上线验收门禁

必须全部满足：

- 多图数量、顺序和 attachment_id 零丢失；
- HASHI API/Codex 原生多图通过；
- OpenRouter Gemini 原生多图通过；
- 非多模态 backend 的现有本地识别链全部回归通过；
- DIRECT_RESPONSE 只交付一次；
- 原生路径不调用 fallback；
- 非原生路径不发送不支持的原始媒体；
- tools 三态与 sync/stream 均保持多模态；
- 未知能力默认失败关闭；
- 损坏或超限附件无静默忽略；
- 持久化日志、metadata、ledger 与 transcript 中无 Base64/bytes；
- 工作树相关变更经过审查并通过既定核心门禁。

## 12. 推荐实施顺序

1. 先建立 canonical content 与 capability resolver 的失败测试；
2. 扩展 /long 与 QueuedRequest 的结构化附件契约；
3. 为 backend 增加 structured conversation invocation；
4. 实现 OpenRouter 原生多模态；
5. 实现 HASHI API Gateway 无工具多模态路径；
6. 完成 Codex app-server 无工具图片桥接；
7. 将 attachment manifest 传播到 HER TurnState 与 StageRequest；
8. 添加 DIRECT_RESPONSE 能力兑现保护；
9. 接入现有本地回退并覆盖 mixed modality；
10. 运行聚焦测试、核心门禁和真实 Provider 验收；
11. 取得明确授权后执行 /reboot min；
12. 完成真实 Momo Telegram 多图验收后再决定扩大重载范围。

测试应先红后绿。不得先修改实现再补形式化覆盖。

## 13. 风险与控制

| 风险 | 控制 |
|---|---|
| Provider/model 能力发生变化 | model-specific registry、显式配置、可选 probe、未知失败关闭 |
| 图片在无工具 Gateway 路径丢失 | structured conversation routing 与 external tools routing 分离 |
| 原生与 fallback 重复处理 | 每阶段每附件唯一 routing decision 与调用计数断言 |
| 多图只读取第一张 | 固定多图比较 fixture、逐附件 provenance、数量与顺序断言 |
| 远端 payload 过大 | Adapter 物化前检查单项与总量限制 |
| Base64 泄漏到审计 | 只在最后一跳临时物化，持久化 schema 禁止 bytes/data URL |
| Triage 返回无法兑现的 DIRECT_RESPONSE | Immediate capability gate 与 typed fallback |
| Sub-agent 丢失附件 | assignment-scoped manifest 与 attachment_id 验证 |
| 现有文本模型链路回归 | 保留全部 media_read/vision_inspect 回归与真实 fallback E2E |

## 14. 与既有媒体方案的关系

docs/her_multimedia_multimodal_plan.md 记录的是现有 HER 媒体工具结果、MCP 内容块、
本地规范化、安全边界和 Provider translator 回退方案。

本文件不取代该方案。两者关系如下：

- 既有文件：定义本地媒体解释与结构化 tool result 如何安全工作；
- 本文件：定义何时原生直传、何时调用既有回退，以及所有 Provider、Gateway 和
  HER 阶段必须遵守的统一输入与测试契约。

实施时必须确保新原生路径不会破坏既有回退路径。

## 15. 当前状态

截至 2026-08-24：

- 设计已获批准；
- Assertions、测试矩阵及旧测试处置已完成；
- Provider 无关 canonical content、精确能力解析、逐附件路由、Gateway、Adapters、
  HER v2、重试与 Sub-agent 传播实现已完成；
- 实现已合并至 `main` 的
  `cc010d11d69b4eb24c62c134dc57ac62ea42c277`，相关自动化覆盖已纳入通过的
  2,663 项离线产品测试和 232 项核心发布门禁；
- 集成主线已通过 `/reboot min` 与 `/reboot max` 通用代码加载验收；
- 尚未完成真实 Momo Telegram 多图和各代表性 Provider 的专项 canary，因此不得把
  通用热重载结果写成所有 Provider 多模态生产验收；
- 下一步按第 10、11 节执行真实媒体专项验收，再决定是否扩大能力声明或部署范围。
