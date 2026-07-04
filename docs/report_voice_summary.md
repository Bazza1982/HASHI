# Report Voice Summary Sidecar

Use this for scheduled text reports that should also produce a short OGG voice
summary. The text report remains the source of truth; the voice summary is only
a concise companion.

## Prompt Addition

Append this requirement to report-producing scheduled tasks:

```text
在完整文字报告后，额外增加：

🎙️ 语音摘要稿
（30-60秒中文自然口语，只包含紧急事项、重要邮件/通知、今天需要处理的行动项。
不要包含广告、促销、可忽略邮件、长邮箱地址、长链接、质量门技术细节。
如果没有重要事项，就说“今天没有需要马上处理的事项”，再用一句话概括整体平稳。）

文字报告仍按原格式输出；语音摘要稿是新增内容，不得取代或删减文字版。
```

## OGG Generation

Generate OGG from a full report that contains `🎙️ 语音摘要稿`, and send it to
Barry on Telegram:

```bash
cd /home/lily/projects/hashi
/home/lily/projects/hashi/.venv/bin/python3 tools/report_voice_summary_ogg.py \
  --input /path/to/report.md \
  --send-telegram \
  --telegram-chat-id 7430217666
```

Or synthesize exact text directly:

```bash
cd /home/lily/projects/hashi
/home/lily/projects/hashi/.venv/bin/python3 tools/report_voice_summary_ogg.py \
  --summary-only \
  --text "爸爸，小夏给您快速报一下今天的重点。今天没有需要马上处理的事项。" \
  --send-telegram \
  --telegram-chat-id 7430217666
```

The command prints the generated `.ogg` path and sends it as a Telegram voice
message by default. By default it writes to:

```text
/home/lily/projects/hashi/media/sunny/report_voice_summaries/
```

## Design Rules

- This is a sidecar. It does not modify, shorten, or send the original text report.
- It only speaks the `语音摘要稿` section, so ads and ignored notifications stay out
  of audio unless the report writer mistakenly includes them there.
- It uses Edge TTS voice `zh-CN-XiaoxiaoNeural` and outputs OGG through ffmpeg.
- Sending is opt-in with `--send-telegram`; scheduled report tasks should use
  this flag so Barry receives both the text report and the OGG voice summary.
