#!/bin/bash
# Wiki organise cron wrapper
# Step 1: Run data prep (daily tags + memory dump)
# Step 2: Run local backend generation + review directly inside this cron job

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
  exit $EXIT_CODE
fi

REVIEW_OUTPUT=$(python3 scripts/wiki_generate_review.py --daily-tags "${DAILY:-0}" 2>&1)
REVIEW_EXIT=$?

echo "$REVIEW_OUTPUT" >> "$LOG"
echo "" >> "$LOG"

exit $REVIEW_EXIT
