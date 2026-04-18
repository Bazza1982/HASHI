#!/bin/bash
# Wiki organise cron wrapper
# Step 1: Run data prep (daily tags + memory dump) — no LLM, no external API
# Step 2: Send HChat task to Lily — Lily does the actual wiki generation herself

LOG=/home/lily/projects/hashi/workspaces/lily/wiki_organise.log
HASHI_DIR=/home/lily/projects/hashi
DUMP_DIR=/home/lily/projects/hashi/workspaces/lily/wiki_dump

cd "$HASHI_DIR"

# Run data prep and capture output
OUTPUT=$(python3 scripts/wiki_organise.py 2>&1)
EXIT_CODE=$?

# Write to log
echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"
echo "$OUTPUT" >> "$LOG"
echo "" >> "$LOG"

# Extract basic stats
DAILY=$(echo "$OUTPUT" | grep "Updated.*daily" | grep -oP '\d+')
ERRORS=$(echo "$OUTPUT" | grep -i "error\|exception\|traceback" | head -3)

if [ $EXIT_CODE -ne 0 ]; then
  # Data prep failed — tell Lily immediately
  python3 tools/hchat_send.py --to lily --from lily --text "❌ Wiki 数据准备脚本失败 (exit $EXIT_CODE)，请检查错误并告知爸爸。错误：${ERRORS:-见 wiki_organise.log}"
  exit $EXIT_CODE
fi

# Send Lily the full wiki generation task
# Lily IS Claude — she reads the memory dumps and writes the wiki pages herself
TASK_MSG="【Wiki 整理任务】数据准备已完成，请你现在执行 Wiki 生成。

数据位置：
- 记忆 dump 目录：$DUMP_DIR
- manifest 文件：$DUMP_DIR/manifest.json
- Vault 根目录：/mnt/c/Users/thene/Documents/lily_hashi_wiki

你需要做的事：
1. 读取 manifest.json，了解本次要处理哪些 Topics 和 Projects
2. 对每个 Topic：读取对应的 topic_*.json，用你自己的理解生成 wiki 页面，写入 Vault/Topics/<TopicId>.md
3. 对每个 Project：读取 project_*.json，生成项目页面，写入 Vault/Projects/<ProjId>.md
4. 读取 weekly_*.json，生成本周 Weekly digest，写入 Vault/Weekly/<week>.md
5. 完成后做质量检查（内容是否连贯、有无截断、数量是否正常）
6. 向爸爸汇报完整结果和你的质量判断

格式参考：直接看 Vault/Topics/ 和 Vault/Projects/ 下已有的文件，沿用相同的 frontmatter + 章节结构。

Daily 标签：已由脚本自动更新 ${DAILY:-0} 个页面，无需你处理。

注意：不要调用任何外部 API，你自己就是 Claude，直接生成内容即可。"

python3 tools/hchat_send.py --to lily --from lily --text "$TASK_MSG"
