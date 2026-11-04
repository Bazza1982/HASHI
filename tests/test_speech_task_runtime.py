"""Exercise the actual Functions entry point, including typed IPC-safe errors."""
import asyncio
from types import SimpleNamespace
import pytest
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.voice_synthesizer import VoiceAsset

@pytest.mark.asyncio
async def test_runtime_accepts_once_and_returns_typed_missing_conflict_split(tmp_path, monkeypatch):
    from orchestrator import runtime_session
    monkeypatch.setattr(runtime_session, "publish_owner_mirror_media_notification", lambda *a, **k: None)
    calls=[]
    gate=asyncio.Event()
    audio=tmp_path/"reply.ogg"
    audio.write_bytes(b"OggS"+b"\0"*32)
    class Manager:
        def _load(self): return {"provider":"test","voice_name":"v","performance":False}
        def get_voice_profile_id(self): return "p"
        async def synthesize_reply(self,*args,**kwargs):
            calls.append(kwargs)
            await gate.wait()
            return VoiceAsset(provider="test",text="hello",spoken_text="hello",wav_path=None,ogg_path=audio)
    runtime=FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name="test";runtime.workspace_dir=tmp_path;runtime.voice_manager=Manager()
    runtime.logger=SimpleNamespace(warning=lambda *a:None)
    request=lambda op,text="hello",ident="task": runtime.synthesize_tui_speech(text,ident,"en",operation=op)
    assert (await request("status"))["task_error"]=="speech_task_not_found"
    assert (await request("start_generate"))["state"]=="queued"
    await request("start_generate")
    assert (await request("start_generate",text="other"))["task_error"]=="speech_request_conflict"
    assert (await request("start_generate",text="长"*1000,ident="huge"))["task_error"]=="speech_split_required"
    gate.set()
    for _ in range(200):
        result=await request("status")
        if result["state"]=="completed":break
        await asyncio.sleep(.005)
    assert result["state"]=="completed"
    assert len(calls)==1 and calls[0]["max_retries"]==0
    assert result["result"]["content_b64"].startswith("T2dn")
    await runtime._speech_tasks.close()
