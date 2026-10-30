"""Run-scoped ordinary questions, using PAO's original owner and event schema."""
from __future__ import annotations

import asyncio
import json

from orchestrator.run_questions import RunQuestions, QuestionError


class ScopedRunQuestions:
    def __init__(self, store, binding, guard):
        self.questions, self.binding, self.guard = RunQuestions(store), binding, guard

    async def execute(self, tool_name, arguments, tool_call_id):
        if not isinstance(arguments, dict) or "operation" in arguments:
            return "Error: question_reserved_field"
        payload = dict(arguments)
        if tool_name == "ask_user":
            payload.setdefault("idempotency_key", tool_call_id)
            wait = 0
        elif tool_name == "get_user_answer":
            wait = payload.pop("wait_seconds", 0)
            if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= 30:
                return "Error: question_wait_invalid"
        else:
            return "Error: unknown_question_tool"
        deadline = asyncio.get_running_loop().time() + wait
        try:
            while True:
                # RunQuestions independently validates the canonical active Run
                # and session. No user answer is sent to the approval service.
                with self.guard(self.binding.context):
                    pass
                if tool_name == "ask_user":
                    result = self.questions.create(
                        request_id=self.binding.request_id, agent_id=self.binding.context.agent_id, payload=payload,
                    )
                else:
                    result = self.questions.get_answer(
                        request_id=self.binding.request_id, agent_id=self.binding.context.agent_id,
                        question_id=str(payload.get("question_id") or ""),
                    )
                with self.guard(self.binding.context):
                    pass
                if tool_name == "ask_user" or result.get("state") not in {"pending", "stopping"}:
                    return json.dumps({"ok": True, "question": result}, ensure_ascii=False)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return json.dumps({"ok": True, "question": result}, ensure_ascii=False)
                await asyncio.sleep(min(.5, remaining))
        except QuestionError as error:
            return "Error: " + error.code
