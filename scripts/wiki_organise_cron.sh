#!/bin/bash
# Wiki organise cron wrapper — runs organiser and reports full results via HChat

LOG=/home/lily/projects/hashi/workspaces/lily/wiki_organise.log
HASHI_DIR=/home/lily/projects/hashi

cd "$HASHI_DIR"

# Run and capture output
OUTPUT=$(python3 scripts/wiki_organise.py 2>&1)
EXIT_CODE=$?

# Write to log
echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"
echo "$OUTPUT" >> "$LOG"
echo "" >> "$LOG"

# Extract key stats
TOPICS=$(echo "$OUTPUT" | grep "Topics" | grep -oP '\d+' | head -1)
PROJECTS=$(echo "$OUTPUT" | grep "Projects" | grep -oP '\d+' | head -1)
WEEKLY=$(echo "$OUTPUT" | grep "Written:.*Weekly" | grep -oP '20\d\d-W\d+')
DAILY=$(echo "$OUTPUT" | grep "Updated.*daily" | grep -oP '\d+')
ERRORS=$(echo "$OUTPUT" | grep -i "error\|exception\|traceback" | head -5)

# Build report message
if [ $EXIT_CODE -eq 0 ]; then
  STATUS="✅ Wiki 整理完成"
else
  STATUS="❌ Wiki 整理失败 (exit $EXIT_CODE)"
fi

MSG="${STATUS}

📊 Topics 处理：${TOPICS:-未知} 个
📁 Projects 处理：${PROJECTS:-未知} 个
📅 Weekly Digest：${WEEKLY:-未生成}
🏷️ Daily 页面打标签：${DAILY:-0} 个
$(if [ -n "$ERRORS" ]; then echo "⚠️ 错误：$ERRORS"; fi)

--- 完整输出 ---
$OUTPUT"

# Send trigger to Lily for AI quality review (not just number relay)
TRIGGER_MSG="Wiki 整理脚本已运行 (exit: $EXIT_CODE)。请你主动做质量审查：
1. 读取本周生成的 Weekly digest 文件，判断内容质量是否正常
2. 检查 Topics/Projects 数量是否合理，有无异常消失或为零的条目
3. 如果一切正常，简短告知爸爸；如果发现问题，详细说明并建议处理方式
4. 不要只转发这里的数字，要给出你自己的判断

基本运行数据供参考：
- Topics: ${TOPICS:-未知}, Projects: ${PROJECTS:-未知}
- Weekly: ${WEEKLY:-未生成}, Daily标签: ${DAILY:-0}
$(if [ -n "$ERRORS" ]; then echo "⚠️ 脚本报错：$ERRORS"; fi)
$(if [ $EXIT_CODE -ne 0 ]; then echo "❌ 脚本异常退出，请优先检查错误"; fi)"

python3 tools/hchat_send.py --to lily --from lily --text "$TRIGGER_MSG"
