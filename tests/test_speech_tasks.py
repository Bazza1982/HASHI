import asyncio
import json
import sqlite3
import pytest
from orchestrator.speech_tasks import SpeechTasks, SpeechTaskError

async def settled(owner, ident):
    for _ in range(200):
        value = owner.status(ident)
        if value["state"] not in {"queued","running","retry_wait","cancelling"}:
            return value
        await asyncio.sleep(.005)
    pytest.fail("speech task did not settle")

@pytest.mark.asyncio
async def test_accepted_work_survives_lost_delivery_and_reopens_without_duplicate(tmp_path):
    calls = []
    gate = asyncio.Event()
    async def execute(deadline):
        calls.append(deadline)
        await gate.wait()
        return {"request_id":"one","content_b64":"T2dnUw==","media_type":"audio/ogg"}
    owner = SpeechTasks(tmp_path)
    value = owner.start("one", "private utterance", "generate", {"voice":"v"}, execute)
    assert value["state"] == "queued"
    # Same accepted identity must not call the executor a second time.
    owner.start("one", "private utterance", "generate", {"voice":"v"}, execute)
    gate.set()
    result = await settled(owner,"one")
    assert result["state"] == "completed" and len(calls) == 1
    await owner.close()
    other = SpeechTasks(tmp_path)
    assert other.status("one")["result"] == result["result"]
    other.start("one", "private utterance", "generate", {"voice":"v"}, execute)
    assert len(calls) == 1
    with pytest.raises(SpeechTaskError, match="conflict"):
        other.start("one", "different text", "generate", {"voice":"v"}, execute)
    assert b"private utterance" not in (tmp_path/"speech.sqlite3").read_bytes()
    await other.close()

@pytest.mark.asyncio
async def test_cancellation_waits_for_executor_and_cannot_publish_late_audio(tmp_path):
    entered = asyncio.Event()
    drained = asyncio.Event()
    async def execute(_deadline):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await drained.wait()
            return {"content_b64":"late"}
    owner=SpeechTasks(tmp_path)
    owner.start("cancel","text","generate",{},execute)
    await entered.wait()
    owner.cancel("cancel")
    assert owner.status("cancel")["state"] == "cancelling"
    await asyncio.sleep(.01)
    assert owner.status("cancel")["state"] == "cancelling"
    drained.set()
    result=await settled(owner,"cancel")
    assert result["state"] == "cancelled" and "result" not in result
    await owner.close()

@pytest.mark.asyncio
async def test_retry_only_confirmed_unaccepted_failure_and_one_budget(tmp_path):
    calls=[]
    async def execute(_):
        calls.append(1)
        if len(calls)==1:
            raise SpeechTaskError("tts_rate_limited",retryable=True,accepted=False)
        return {"request_id":"retry"}
    owner=SpeechTasks(tmp_path,retry_delay=.001)
    owner.start("retry","retry text","generate",{},execute)
    value=await settled(owner,"retry")
    assert value["state"]=="completed" and value["attempts"]==2
    async def unknown(_):
        raise SpeechTaskError("tts_timeout",retryable=True,accepted=None)
    owner.start("unknown","other","generate",{},unknown)
    value=await settled(owner,"unknown")
    assert value["state"]=="uncertain" and value["attempts"]==1 and not value["can_retry"]
    await owner.close()

@pytest.mark.asyncio
async def test_owner_restart_does_not_replay_unfinished_native_work(tmp_path):
    owner=SpeechTasks(tmp_path)
    owner.db.execute("INSERT INTO speech_tasks (id,fingerprint,phase,state,created,seen,expires,budget,attempts) VALUES ('lost','fp','generate','running',0,0,999999999999,'{}',1)")
    owner.db.commit()
    await owner.close()
    other=SpeechTasks(tmp_path)
    assert other.status("lost")["state"]=="uncertain"
    await other.close()

def test_budgets_follow_text_and_language_and_never_silently_clip(tmp_path):
    owner=SpeechTasks(tmp_path)
    small=owner.estimator.estimate("您好。")
    large=owner.estimator.estimate("这是较长的一段中文。"*10)
    assert large.native_seconds > small.native_seconds
    assert owner.estimator.estimate("one two three").units != owner.estimator.estimate("中华人民共和国").units
    with pytest.raises(ValueError,match="split_required"):
        owner.estimator.estimate("长"*1000)
    owner.db.close()

@pytest.mark.asyncio
async def test_deadline_cannot_publish_a_result_from_executor_suppressing_cancel(tmp_path):
    from dataclasses import replace
    owner=SpeechTasks(tmp_path)
    original=owner.estimator.estimate
    owner.estimator.estimate=lambda *a,**kw: replace(original(*a,**kw),native_seconds=.005,attempt_seconds=.01,total_seconds=.02)
    async def late(_):
        try: await asyncio.sleep(1)
        except asyncio.CancelledError: return {"content_b64":"late"}
    owner.start("deadline","text","generate",{},late)
    value=await settled(owner,"deadline")
    assert value["state"]=="uncertain" and "result" not in value
    await owner.close()

def test_short_cold_sample_does_not_make_normal_segments_impossible(tmp_path):
    owner=SpeechTasks(tmp_path)
    owner.estimator.observe("default","generate","爱",30.)
    budget=owner.estimator.estimate("这是正常长度的爱情故事。"*9)
    assert budget.native_seconds <= 120
    owner.db.close()

def test_short_planning_sample_does_not_reject_a_long_logical_reading(tmp_path):
    owner=SpeechTasks(tmp_path)
    owner.estimator.observe("default","prepare","爱",8.)
    budget=owner.estimator.estimate("爱情故事"*1250,phase="prepare")
    assert 40 <= budget.native_seconds <= 90
    owner.db.close()
