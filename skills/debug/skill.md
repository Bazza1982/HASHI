---
id: debug
name: Debug
type: prompt
description: Run a task in strict debugging and verification mode
---

Treat this task as a debugging assignment that is not complete until you have pushed verification as far as the backend and tools allow.

Debugging operating rules:
- maximize your own reasoning depth and verification effort before asking the human to check
- reproduce the issue when possible before changing anything
- inspect the real code, config, logs, and runtime state instead of guessing
- prefer direct evidence over assumptions
- after making a fix, verify it with the strongest practical checks available
- use browser tools, local tests, log inspection, API checks, screenshots, or other available tools when they help verify the result
- if a check cannot be run, state exactly what could not be verified
- do not stop at "I changed it"; continue until you have evidence the fix works or a precise blocker

Reporting rules:
- findings and root cause first
- then the fix
- then the verification you performed
- only ask the human to test when you have already exhausted the agent's own verification options
