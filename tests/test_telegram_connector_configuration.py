from types import SimpleNamespace
import pytest
from orchestrator.function_worker_supervisor import FunctionWorkerSupervisor
from tests.test_telegram_ingress import _Bot


@pytest.mark.asyncio
async def test_shared_polling_reads_trusted_endpoint_not_partial_worker_descriptor(tmp_path,monkeypatch):
    calls=[]
    def bot(token,**kwargs):
        calls.append(kwargs)
        return _Bot(token)
    monkeypatch.setattr("telegram.Bot",bot)
    handle=SimpleNamespace(config=SimpleNamespace(extra={"agent_lifecycle_id":"x"}))
    kernel=SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        global_cfg=SimpleNamespace(instance_id="test"),
        _handoff_draining=True,_shared_committed=False,
        _runtime_map=lambda:{"alpha":handle},
        _load_raw_config=lambda:{"agents":[{"name":"alpha","extra":{
            "telegram_api_url":"http://127.0.0.1:18990"}}]})
    supervisor=FunctionWorkerSupervisor(kernel)
    assert await supervisor.start_telegram_ingress("alpha","123456:abcdef",drop_pending_updates=False)
    assert calls==[{"base_url":"http://127.0.0.1:18990/bot",
                   "base_file_url":"http://127.0.0.1:18990/file/bot"}]
    assert supervisor._telegram_ingress["alpha"].api_root=="http://127.0.0.1:18990"
# HASHI-Fingerprint: hfp_9ee2ac23ae664675abfa7c5ee12818e4
