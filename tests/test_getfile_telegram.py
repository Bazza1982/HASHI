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


@pytest.mark.asyncio
async def test_real_immutable_telegram_update_opens_native_menu_without_workbench_input(runtime):
 from telegram import Update
 from orchestrator.commands.getfile import getfile_command, getfile_callback
 from telegram.error import BadRequest
 class NativeBot(_ToolBot):
  edits=0
  async def answer_callback_query(self, **kwargs): return True
  async def edit_message_text(self, **kwargs):
   self.edits += 1
   if self.edits > 1: raise BadRequest("Message is not modified")
   return NS(message_id=100)
 bot=NativeBot();runtime.app=NS(bot=bot);runtime.telegram_connected=True
 path=runtime.workspace_dir/"native.bin";path.write_bytes(b"actual-native-callback")
 runtime.canonical_audit.record("tool_call",{"tool_name":"file_write","is_error":False,"arguments":{"path":str(path)}},
  provenance={"workspace_dir":str(runtime.workspace_dir),"access_roots":[str(runtime.workspace_dir)],"owner_id":"user:7"})
 update=Update.de_json({"update_id":501,"message":{"message_id":501,"date":1794480000,
  "chat":{"id":7,"type":"private"},"from":{"id":7,"is_bot":False,"first_name":"QA"},"text":"/getfile"}},bot)
 await getfile_command(runtime,update,NS(args=[]))
 assert bot.messages
 markup=bot.messages[-1]["reply_markup"]
 assert all(not str(b.callback_data).startswith("getfile:path:") for row in markup.inline_keyboard for b in row)
 assert "Recent 10 distinct files" in bot.messages[-1]["text"]
 callback=Update.de_json({"update_id":502,"callback_query":{"id":"native-query",
  "from":{"id":7,"is_bot":False,"first_name":"QA"},"chat_instance":"synthetic",
  "message":{"message_id":100,"date":1794480000,"chat":{"id":7,"type":"private"},
   "from":{"id":123456,"is_bot":True,"first_name":"QA Bot"}},
  "data":markup.inline_keyboard[0][0].callback_data}},bot)
 first=await getfile_callback(runtime,callback,NS(args=[]))
 replay=await getfile_callback(runtime,callback,NS(args=[]))
 assert first["getfile"]["message_id"]==replay["getfile"]["message_id"]
 assert len(bot.media)==1 and bot.media[0]["payload"]==path.read_bytes()
 assert runtime.queue.empty()
