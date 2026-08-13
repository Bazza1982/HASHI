# HER 后端多媒体多模态通路

- 原始方案：Feiyan，2026-08-13
- 审阅与修订：HASHI1 整合，2026-08-13
- 状态：已实施，等待线上灰度重载
- 目标版本：HER `0.1.0-hashi.10`

## 1. 审阅结论

原始方案正确找到了“Telegram 媒体只以本地路径进入 HER”的现象，但不能只在
HASHI Python 网关增加 MCP `image` 块。HER `0.1.0-hashi.9` 会把整个 MCP
结果序列化为字符串，随后又把工具结果压成单个文本块；模型仍然看不到图片。

实施必须同时覆盖三层：

1. HASHI 提供受限的 `media_read`，产生 MCP text/image 内容块；
2. HER Rust 运行时保存并解析结构化 MCP 图片结果；
3. provider translator 把图片转换为供应商实际接受的视觉消息形状。

原始方案还有以下需要修正的地方：

- `her_gateway_context.json` 是启动时生成的状态文件，不能手工持久修改；
- Telegram `media_dir` 可能位于工作区之外，必须成为显式授权根目录；
- `PyMuPDF` 虽在开发机安装，但此前未声明为项目依赖；
- 音频失败后再次调用同一个转写器不构成回退，必须先用 ffmpeg 归一化；
- 将 `media_read` 加入全局 schema 后，通配符配置会意外影响非 HER 后端；
- PDF 不能只抽文本，混合 PDF 的无文本页仍需渲染；
- 必须限制解码像素、页数、帧数、时长、单图和总请求体大小；
- base64 不得进入通用工具审计或持久会话；
- 并行工具结果必须保持连续，不能让图片用户消息插在两个 tool 回执之间。

## 2. 已实施架构

```text
Telegram media path
  -> HER-only generated gateway context
     (access_root + agent media_root + media_read)
  -> media_read
     (resolve + type/signature + resource limits)
  -> MCP content[text | image]
  -> HER private process cache
     (session keeps metadata/cache reference, not base64)
  -> provider request
     Anthropic: tool_result image/source/base64
     OpenAI-compatible: paired tool text, then user image_url data URL
```

OpenAI-compatible 的图片位置遵循官方 vision 输入形式：图片以 URL、base64 data
URL 或 file ID 进入消息内容。HER 采用 data URL，并保留原有 tool 文字回执。
参考：<https://developers.openai.com/api/docs/guides/images-vision>

## 3. `media_read` 行为

### 图片

- 支持 PNG、JPEG、WebP、BMP、GIF、TIFF；
- 扩展名与固定签名同时校验；
- EXIF 方向校正、首帧确定性选择、透明背景合成到白色；
- 最大输入 15 MiB、最大解码 6400 万像素；
- 输出不超过 4000 像素边长及 1600 万像素，并归一为有界 JPEG。

### PDF

- 声明并使用 PyMuPDF；
- 最多 30 页、100000 字符；
- 提取文本层，同时在 `auto` 模式渲染无有效文本页；
- 最多渲染 12 页，并按总图片预算停止。

### 视频

- 使用 ffprobe 检查真实 stream、格式与时长；
- 最长 10 分钟、最大输入 100 MiB；
- 默认在 10%、50%、90% 位置确定性抽取 3 帧，可请求 1–6 帧；
- 可选转写音轨。

### 音频/语音

- 先用 ffmpeg 转为 16 kHz、单声道 PCM WAV；
- 再调用本地 `voice_transcriber`；
- Telegram 直接转写失败时，提示 HER 明确调用 `media_read`，不再把路径冒充音频能力。

## 4. 权限、资源与持久化

- 路径经 `resolve(strict=True)` 后，必须位于 `access_root` 或当前 Agent 的
  `media_root`；符号链接逃逸会被拒绝；
- 网关 context schema 为 v2，并显式携带 `media_roots`；
- `media_read` 不由普通 `allowed:["*"]` 自动启用，而是在 HER 网关生成时加入；
- 单张模型图片最大 2.5 MB，总图片原始字节最大 4 MB，为 6 MiB 供应商请求上限
  预留 base64 与其他消息空间；
- HASHI 工具审计只写安全元数据，不写 image content/base64；
- HER 把当前回合图片写入进程私有临时目录，文件在关闭 MCP 时删除；
- 进入下一个用户回合后，历史图片降级为文字占位，避免重复发送；恢复旧会话时若
  临时缓存已不存在，也会安全降级。

## 5. Provider 协议

- Anthropic：保留标准 `tool_result.content[].type=image` 与
  `source={type:base64,media_type,data}`；
- OpenAI-compatible：先发配对的 `role=tool` 文字回执，再发
  `role=user` 的 `image_url` data URL；
- 并行工具调用：先连续发完全部 tool 回执，再发相应图片消息；
- MCP `isError:true`：转换为真正的 HER tool error，不再伪装成成功字符串。

## 6. 不变项与兼容性

- `file_read` 行为不变；
- `/long`、`/end` 状态机与批处理格式不变；
- 非 HER 后端不会因 wildcard 自动得到 `media_read`；
- 文本/代码文档继续走原有读取方式；
- 不加入 OCR，扫描 PDF 页由视觉模型识别。

## 7. 验证门槛

- Python：图片、PDF、真实 ffmpeg 音频归一化、真实视频抽帧、越界与符号链接、
  类型伪装、网关透传、审计无 base64、语音失败路由；
- Rust：Anthropic/OpenAI wire shape、并行 tool 顺序、当前/历史媒体、全 workspace
  tests、全 workspace clippy `-D warnings`；
- 打包：manifest、二进制 SHA-256、source commit、certification baseline 一致；
- 端到端：打包后的 `hashi-her` 调用 HASHI MCP `media_read`，假 provider 捕获到
  `data:image/jpeg;base64,...`，而不是本地路径或 base64 文本占位。

## 8. 发布与回滚

发布需要新进程加载 Python 网关代码与 HER `0.1.0-hashi.10`。未获授权前不执行
线上 `/reboot`。灰度顺序为 `/reboot min`、真实图片/混合 PDF/语音冒烟，再决定
是否 `/reboot max`。

回滚只需把 manifest 与 adapter version 回退到上一认证版本并重启；旧版不会读取
新 context schema，但 HER 初始化会重新生成匹配的 context 文件。
