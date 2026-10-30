"""An ordinary Run answer is information and cannot create permission leases."""
import json

import pytest

from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.questions import ScopedRunQuestions
from orchestrator.permissions.authority import PermissionDenied
from tests.test_permission_pao_fence import setup_run


@pytest.mark.asyncio
async def test_question_uses_original_events_and_never_grants_permissions(tmp_path):
    store, session, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    guard = PaoRunFence(store, context=context, request_id=accepted.request_id,
                        fencing_token=fence, worker_id="INSTANCE:agent")
    scoped = ScopedRunQuestions(store, binding, guard)
    scoped.questions.set_question_policy(scope="agent", agent_id="agent", enabled=True)
    response = json.loads(await scoped.execute("ask_user", {"question": "Which document?"}, "call-1"))
    question = response["question"]
    again = json.loads(await scoped.execute("ask_user", {"question": "Which document?"}, "call-1"))
    assert again["question"]["question_id"] == question["question_id"]
    scoped.questions.answer(session_id=session["session_id"], owner_id="owner", question_id=question["question_id"],
                            payload={"text": "Use the first document", "idempotency_key": "human-answer"})
    answer = json.loads(await scoped.execute("get_user_answer", {"question_id": question["question_id"]}, "call-2"))
    assert answer["question"]["answer"]["grants_permissions"] is False
    assert answer["question"]["state"] == "consumed"
    store.cancel_run(accepted.run_id, owner_id="owner")
    with pytest.raises(PermissionDenied):
        await scoped.execute("ask_user", {"question": "Late question"}, "late-call")
