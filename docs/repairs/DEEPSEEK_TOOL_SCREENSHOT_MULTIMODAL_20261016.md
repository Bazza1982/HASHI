# DeepSeek Tool 截图多模态修复（2026-10-16）

## 范围与授权

- 所有者：HERV3；层级：可替换 Function 的 DeepSeek provider adapter。
- 施工位置：HASHI3 `debug-deepseek`；用户授权修复、测试及 HASHI3 热重启。
- HASHI4 不在本次变更和重启范围内；Protected Core 不变。

## 故障

DeepSeek Vision 原本能够接收正常的用户图片，但 HERV3 直接执行截图 Tool 后，
OpenRouter 共用循环只读取 `ToolResult.output`，忽略结构化媒体内容，并把兼容
返回里的完整 Base64 当成 `role=tool` 文字发送。一次 2,821,918 字节 PNG 因
Base64 扩张为 3,762,560 个字符，最终被 DeepSeek 当成约 268 万文字 token，
超过上下文后返回 HTTP 400。图片本身并未超过 DeepSeek 的单图限制。

## 决定

1. 共用 Tool 循环增加 provider 可覆盖的结果翻译边界，默认文本行为不变。
2. DeepSeek 从结构化 Tool 内容或旧截图字符串中提取图片；向 provider 发送连续
   tool 文字回执，随后发送 `role=user` 的 `image_url`，不把 Base64 放进文字。
3. 出站前拒绝文字块中的图片 data URL，并校验实际序列化 JSON。
4. 采用 DeepSeek 官方 Vision 限制，不复用本地 `media_read` 的 2.5/4 MiB 预算：
   最多 600 张、单张 32 MiB、无 file ID 时图片合计 64 MiB、请求体 48 MiB；
   格式为 JPEG、PNG、GIF、WebP。

官方来源：<https://api-docs.deepseek.com/guides/vision/>（2026-10-16 核对）。

## 验证记录

- 红灯：新增回归首先证明截图执行后只有一个含 Base64 的 tool 文字消息。
- 绿灯：截图改为 `tool` + `user/image_url`，文字不含 Base64。
- 并行批次：所有 tool 回执先连续完成，图片 follow-up 后置。
- 限制：能力注册表记录 600 / 32 MiB / 64 MiB / 48 MiB，适配器按实际请求验证。
- 相关 DeepSeek、多模态和 OpenRouter 回归均需通过后才可提交。

## 交付状态

实现、提交、HASHI3 Worker 采用和用户通话实测分别记录；其中任何一项未验证，
都不能替代下一项。HASHI4 生产采用需要另行明确授权。
