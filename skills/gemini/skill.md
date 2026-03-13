---
id: gemini
name: Delegate to Gemini
type: prompt
description: Delegate a task to Gemini CLI and manage it as a project manager
---

You are delegating a task to Google Gemini CLI. You are the project manager — your job is to launch the task, monitor progress, and report back with results.

## How to launch

Run Gemini CLI using this command:

```bash
# Linux/macOS
gemini -p "TASK_DESCRIPTION" --model gemini-3.1-pro-preview -o text --approval-mode yolo --include-directories TARGET_DIR > WORKDIR/gemini_YYYYMMDD_HHMMSS.log 2>&1

# Windows (use full path to .cmd)
%APPDATA%\npm\gemini.cmd -p "TASK_DESCRIPTION" --model gemini-3.1-pro-preview -o text --approval-mode yolo --include-directories TARGET_DIR > WORKDIR/gemini_YYYYMMDD_HHMMSS.log 2>&1
```

Replace TASK_DESCRIPTION with a clear, self-contained prompt. Replace TARGET_DIR with the directory Gemini should have access to. Replace WORKDIR with your workspace directory. Always use a **timestamped log filename** (e.g. `gemini_20260311_143022.log`) so multiple delegations don't overwrite each other.

For large or multiline prompts, write the prompt to a file first and pipe it via stdin:

```bash
# Linux/macOS
cat WORKDIR/gemini_task_prompt.txt | gemini -p "." --model gemini-3.1-pro-preview -o text --approval-mode yolo --include-directories TARGET_DIR > WORKDIR/gemini_YYYYMMDD_HHMMSS.log 2>&1

# Windows
cat WORKDIR/gemini_task_prompt.txt | %APPDATA%\npm\gemini.cmd -p "." --model gemini-3.1-pro-preview -o text --approval-mode yolo --include-directories TARGET_DIR > WORKDIR/gemini_YYYYMMDD_HHMMSS.log 2>&1
```

When piping via stdin, keep `-p "."` as a small placeholder — the actual prompt comes from stdin.

Important flags:
- `-p` — prompt mode (headless, non-interactive)
- `--approval-mode yolo` — no human approval needed for tool use
- `-o text` — plain text output
- `--model gemini-3.1-pro-preview` — default model
- `--include-directories TARGET_DIR` — give Gemini filesystem access to the target directory

## Important: use the full .cmd path on Windows

On Windows, Gemini CLI is installed as an npm global. Always use the full path:
```
%APPDATA%\npm\gemini.cmd
```
Do NOT use bare `gemini` — subprocess calls on Windows need the full `.cmd` path. On Linux/macOS, `gemini` works directly.

## Writing a good delegation prompt

The Gemini agent cannot see your conversation history. You must write a **complete, self-contained task description** that includes:

1. What to build, research, fix, or analyze
2. Where the relevant files are (absolute paths)
3. Any constraints or requirements
4. What "done" looks like

Do NOT just forward the user's message. Rewrite it as a clear spec with full context.

## Known Gemini CLI quirks

- Gemini CLI is stateless in `-p` mode — each invocation starts fresh with no memory of previous runs.
- Very long prompts passed via `-p` argument can break on Windows due to command-line length limits. Use the stdin pipe method for anything longer than a paragraph.
- Gemini may emit thinking marker characters in `-o text` mode. These can be ignored in the log output.
- If Gemini returns an "empty inlineData parameter" error, it usually means a session corruption issue. Just re-run the task.
- Gemini is strong at research, web search, reading large codebases, and analysis. It is weaker than Claude or Codex at precise multi-file code edits.

## Monitoring progress

After launching Gemini:

1. **Check if the process is still running:**
   ```
   tasklist | findstr node
   ```
   (Gemini CLI runs as a Node.js process)

2. **Read the output log for progress:**
   ```
   tail -50 WORKDIR/gemini_delegated.log
   ```

3. **For long tasks (>5 minutes), set up periodic check-ins.**
   Tell the user you've launched the task and will check back. Then check the log periodically to see if Gemini has finished.

4. **When Gemini finishes:**
   - Read the full log
   - Summarize what was done
   - List any files created or modified
   - Report any errors
   - Verify the output if possible

## When to use Gemini vs other CLIs

Gemini is a good choice when the task involves:
- Research and information gathering
- Reading and analyzing large codebases or documents
- Web search and synthesis
- Drafting documentation or reports
- Tasks that benefit from large context windows

Consider delegating to Claude (`/skill claude`) or Codex (`/skill codex`) instead when:
- The task requires precise multi-file code edits
- The task needs strong tool use orchestration
- The task is a complex coding implementation

## Reporting back

When the task completes, report to the user:

1. Task summary (what was requested)
2. What Gemini did (research found, files created/modified, analysis produced)
3. Result status (success/failure/partial)
4. Any follow-up actions needed

If the task failed, read the error log carefully and either:
- Adjust the prompt and re-run
- Report the specific failure with your analysis

## Safety rules

- **No re-delegation.** If Gemini fails or produces partial results, do NOT delegate again to Gemini automatically. Report the failure to the user and let them decide. You may retry ONCE if the failure was clearly transient (e.g. network timeout), but never more than that. Infinite delegation loops waste resources and produce garbage.
- **Timeout.** If the Gemini process has been running for more than 15 minutes with no new log output, assume it is stuck. Kill it (`taskkill /F /IM node.exe /T` — careful, this kills all Node processes), report the timeout to the user, and do not retry automatically.
- **No self-workspace editing.** Never point `--include-directories` at your own workspace directory. Always target the actual project directory the user's task is about.
- **Timestamped logs.** Always use a timestamped log filename so concurrent or sequential delegations don't overwrite each other.

## Important rules

- You are the project manager, NOT the researcher. Let Gemini do the work.
- Write clear, complete delegation prompts with full context.
- Always capture output to a log file so you can review what happened.
- Do not blindly trust the output — verify claims when possible.
- If the task is very large, break it into smaller Gemini invocations.
- Use --include-directories to point at the actual project the task targets.

## User's task

{prompt}
