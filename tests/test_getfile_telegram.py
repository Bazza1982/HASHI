"""Controlled Bot boundary, actual FC renderer/outbox; not external Telegram acceptance."""
from types import SimpleNamespace as NS
import pytest
from telegram.error import TimedOut
from tests.test_getfile_commands import runtime
from tests.test_frontend_attachment_tool import _ToolBot
from orchestrator import runtime_session
from orchestrator.getfile_service import GetFileService

def update_for(runtime):
 session=runtime_session.ensure_store(runtime).resolve_primary_session(owner_id="user:7",agent_id="alice",establish=True)
 return NS(effective_user=NS(id=7),effective_chat=NS(id=7),update_id=91,
  effective_message=NS(message_id=91),_hashi_session_surface="telegram",_hashi_session_channel_key="7",
  _hashi_session_owner_id="user:7",_hashi_session_id=session["session_id"],
  _hashi_session_context_generation=1)

@pytest.mark.asyncio
async def test_actual_telegram_fc_sends_lossless_document_once(runtime):
 bot=_ToolBot();runtime.app=NS(bot=bot);runtime.telegram_connected=True
 update=update_for(runtime);path=runtime.workspace_dir/"transport.bin";path.write_bytes(b"\x00\xfftransport")
 a=await GetFileService(runtime).transfer(update,raw_path=str(path))
 b=await GetFileService(runtime).transfer(update,raw_path=str(path))
 assert a["getfile"]["message_id"]==b["getfile"]["message_id"]
 assert a["getfile"]["delivery_state"]=="delivered"
 assert len(bot.media)==1
 assert bot.media[0]["payload"]==path.read_bytes() and bot.media[0]["chat_id"]==7

@pytest.mark.asyncio
async def test_lost_telegram_ack_is_durable_unknown_and_never_replayed(runtime):
 class LostAck(_ToolBot):
  async def send_document(self,**kwargs):
   await super().send_document(**kwargs)
   raise TimedOut("synthetic lost acknowledgement")
 bot=LostAck();runtime.app=NS(bot=bot);runtime.telegram_connected=True
 update=update_for(runtime);path=runtime.workspace_dir/"unknown.bin";path.write_bytes(b"accepted-before-ack-loss")
 with pytest.raises(TimedOut):
  await GetFileService(runtime).transfer(update,raw_path=str(path))
 result=await GetFileService(runtime).transfer(update,raw_path=str(path))
 assert result["getfile"]["delivery_state"]=="unknown"
 assert len(bot.media)==1
