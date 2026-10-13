---
name: antigravity
description: Use when delegating a self-contained task to Antigravity CLI (agy), monitoring progress, and verifying its result.
---

Delegate the specified task to Antigravity CLI, using the configured executable,
working directory and model. This replaces the retired Gemini CLI skill.

Write a self-contained prompt with the requested result, exact file scope,
constraints and acceptance checks. The delegated process has no access to this
conversation. Use the current `agy models` output or the instance catalogue for
model selection; old Gemini CLI model names are not interchangeable with agy
model IDs.

Prefer the existing HASHI Antigravity adapter, which owns prompt transport,
session state and process cleanup. For a standalone invocation, inspect the
installed `agy --help` and use its documented headless mode. For long or
multiline requests, pass the complete prompt over stdin with
`--input-format stream-json --output-format stream-json`: send one JSON line
with `{"event":"user","message":{"content":"TASK"}}`,
then close stdin. Do not truncate the prompt to fit an argv limit.

Run from the authorized task directory. Extra directories and permission
flags must follow the task's existing scope. On Windows service deployments,
use the configured HASHI `agy_launch_mode` launcher rather than executing agy
under a different account or copying login credentials.

Keep a distinct log for the invocation and monitor the actual process and
structured events. A version check or exit code alone does not prove success:
require a successful terminal result, a nonempty response, and meaningful
validation of the requested output. If a run fails after possible effects,
inspect its result before considering any retry. Report the outcome and any
specific remaining action without inferring that a client rejection means the
whole Google account or all Gemini models are unavailable.
