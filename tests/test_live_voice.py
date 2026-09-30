"""Offline reference tests with explicitly fake ports; not repository/live qualification."""
import asyncio
from dataclasses import replace
import json
import unittest

from orchestrator.frontend_live_voice.protocol import CallBinding, Fragment, LiveVoiceError, normalize_transcript
from orchestrator.frontend_live_voice.delegation import build_proposal
from orchestrator.frontend_live_voice.service import LiveVoiceEventService
from orchestrator.frontend_live_voice.openai_live import (
    append_update,
    create_provider_session,
    fit_live_session_input,
    safe_sideband_event,
    session_request,
)
from orchestrator.frontend_live_voice.manager import _live_text_chunks

BINDING=CallBinding('person@example.test','hashi4','generation-7','zelda','session-example',8,'call-example',1,'live_example')

def fragment(event='event-1',text='hello ',start=0,end=500):
    return Fragment(event,'user',text,start,end)

def proposal():
    return build_proposal(BINDING,'delegation-1',[fragment()],after_ms=0,cutoff_ms=600,expires_at='2099-01-01T00:00:00Z')

class ProtocolTests(unittest.TestCase):
    def test_public_scope_hides_owner_and_provider(self):
        scope=BINDING.public_scope();self.assertNotIn('owner_id',scope);self.assertNotIn('provider_session_id',scope)
    def test_existing_owner_identity_not_restricted_to_new_id_vocabulary(self):
        self.assertEqual(BINDING.owner_id,'person@example.test')
    def test_scope_generation_fence(self):
        expected=BINDING.public_scope();expected['context_generation']=9
        with self.assertRaises(LiveVoiceError):BINDING.require_scope(expected,authenticated_owner=BINDING.owner_id)
    def test_cross_owner_rejected(self):
        with self.assertRaises(LiveVoiceError):BINDING.require_scope(BINDING.public_scope(),authenticated_owner='other')
    def test_boolean_generation_invalid(self):
        with self.assertRaises(LiveVoiceError):replace(BINDING,context_generation=True)
    def test_exact_caption(self):
        f=normalize_transcript({'type':'conversation.user.delta','event_id':'event1','delta':'go go ','start_ms':0,'end_ms':10})
        self.assertEqual(f.text,'go go ')
    def test_audio_not_transcript(self):
        self.assertIsNone(normalize_transcript({'type':'session.input_audio.append','audio':'secret'}))
    def test_reversed_timestamp_rejected(self):
        with self.assertRaises(LiveVoiceError):fragment(start=500,end=200)
    def test_no_provider_task_text_assumption(self):
        self.assertIsNone(normalize_transcript({'type':'session.delegation.created','delegation':{'id':'d','target':'client'}}))

class ProposalTests(unittest.TestCase):
    def test_fragments_ordered_and_deduplicated(self):
        p=build_proposal(BINDING,'d1',[fragment('e2','world',500,900),fragment(),fragment()],after_ms=0,cutoff_ms=1000,expires_at='2099')
        self.assertEqual(p.text,'hello world');self.assertEqual(len(p.source_event_ids),2)
    def test_cutoff_crossing_requires_clarification(self):
        p=build_proposal(BINDING,'d1',[fragment()],after_ms=0,cutoff_ms=300,expires_at='2099')
        self.assertTrue(p.ambiguous)
    def test_empty_window_is_ambiguous(self):
        p=build_proposal(BINDING,'d1',[],after_ms=0,cutoff_ms=100,expires_at='2099');self.assertTrue(p.ambiguous)
    def test_digest_changes_with_scope(self):
        p=proposal();q=build_proposal(replace(BINDING,call_epoch=2),'delegation-1',[fragment()],after_ms=0,cutoff_ms=600,expires_at='2099-01-01T00:00:00Z')
        self.assertNotEqual(p.digest,q.digest)
    def test_large_proposal_not_silently_truncated(self):
        with self.assertRaises(LiveVoiceError):build_proposal(BINDING,'d1',[fragment(text='x'*40000)],after_ms=0,cutoff_ms=600,expires_at='2099')

class ProviderTests(unittest.TestCase):
    def test_exact_provider_count_drops_only_oldest_complete_history_units(self):
        weights = {
            "MANDATORY": 4_000,
            "OLD-USER": 1_500,
            "OLD-ASSISTANT": 1_500,
            "MIDDLE": 1_800,
            "NEWEST": 1_500,
        }

        class FakeContent:
            def __init__(self, payload):
                self.payload = payload

            async def iter_chunked(self, _size):
                yield json.dumps(self.payload).encode()

        class FakeResponse:
            status = 200
            headers = {"x-request-id": "req-count-1"}

            def __init__(self, payload):
                self.content = FakeContent(payload)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class FakeHttp:
            def post(self, _url, *, json, **_kwargs):
                tokens = sum(
                    weights[part["text"]]
                    for item in json["input"]
                    for part in item["content"]
                )
                return FakeResponse({"input_tokens": tokens})

        def item(role, text):
            content_type = "output_text" if role == "assistant" else "input_text"
            return {
                "type": "message",
                "role": role,
                "content": [{"type": content_type, "text": text}],
            }

        candidate = [
            item("developer", "MANDATORY"),
            item("user", "OLD-USER"),
            item("assistant", "OLD-ASSISTANT"),
            item("user", "MIDDLE"),
            item("assistant", "NEWEST"),
        ]
        fitted, audit = asyncio.run(
            fit_live_session_input(
                FakeHttp(),
                key="test-key",
                model="gpt-live-1",
                input_messages=candidate,
                required_message_count=1,
                history_unit_message_counts=[2, 1, 1],
            )
        )

        texts = [item["content"][0]["text"] for item in fitted]
        self.assertEqual(texts, ["MANDATORY", "MIDDLE", "NEWEST"])
        self.assertEqual(audit["input_tokens_exact"], 7_300)
        self.assertEqual(audit["history_included_units"], 2)
        self.assertEqual(audit["history_omitted_units"], 1)

    def test_exact_provider_count_rejects_mandatory_context_over_limit(self):
        class FakeContent:
            async def iter_chunked(self, _size):
                yield b'{"input_tokens":9000}'

        class FakeResponse:
            status = 200
            headers = {}
            content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class FakeHttp:
            def post(self, *_args, **_kwargs):
                return FakeResponse()

        mandatory = [{
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "FULL-HCC"}],
        }]
        with self.assertRaises(LiveVoiceError) as caught:
            asyncio.run(
                fit_live_session_input(
                    FakeHttp(),
                    key="test-key",
                    model="gpt-live-1",
                    input_messages=mandatory,
                    required_message_count=1,
                    history_unit_message_counts=[],
                )
            )
        self.assertEqual(caught.exception.code, "live_input_mandatory_limit")

    def test_client_delegation_and_recording_off(self):
        history=[{'type':'message','role':'user','content':[{'type':'input_text','text':'Remember this.'}]}]
        req=session_request('v=0\r\n','Voice style only.', voice='willow', input_messages=history)
        self.assertEqual(req['session']['delegation'],{'type':'client'});self.assertFalse(req['session']['store'])
        self.assertEqual(req['session']['audio']['output']['voice'],'willow')
        self.assertEqual(req['session']['input'],history)
        self.assertEqual(req['transport']['type'],'webrtc')
    def test_unqualified_model_not_silently_selected(self):
        with self.assertRaises(LiveVoiceError):session_request('v=0','style','other')
    def test_unqualified_voice_not_silently_selected(self):
        with self.assertRaises(LiveVoiceError):session_request('v=0','style',voice='other')
    def test_cjk_instructions_use_character_projection_not_utf8_byte_count(self):
        req=session_request('v=0','温柔地说话。'*1200)
        self.assertEqual(req['session']['model'],'gpt-live-1')
    def test_instruction_budget_uses_provider_token_limit_not_old_14k_character_cap(self):
        req=session_request('v=0','S'*20000)
        self.assertEqual(req['session']['instructions'],'S'*20000)
    def test_startup_history_message_limit_is_enforced(self):
        history=[{'type':'message','role':'user','content':[{'type':'input_text','text':str(index)}]} for index in range(129)]
        with self.assertRaises(LiveVoiceError):session_request('v=0','style',input_messages=history)
    def test_token_budget_not_character_length(self):
        with self.assertRaises(LiveVoiceError):append_update('commentary','short','d1','e1',lambda _text:501)
    def test_public_update_identity(self):
        e=append_update('commentary','Done.','d1','e1',lambda _text:2)
        self.assertEqual(e['delegation_id'],'d1');self.assertEqual(e['type'],'session.commentary.append')
    def test_audio_copies_dropped_before_projection(self):
        self.assertIsNone(safe_sideband_event(json.dumps({'type':'session.output_audio.delta','delta':'raw'})))
    def test_unknown_sideband_type_ignored(self):
        self.assertIsNone(safe_sideband_event(json.dumps({'type':'response.function_call','arguments':'danger'})))
    def test_provider_update_acknowledgements_are_accepted(self):
        for event_type in ('session.commentary.appended','session.thinking.appended','session.instructions.appended'):
            event=safe_sideband_event(json.dumps({'type':event_type,'event_id':'provider-1','client_event_id':'update-1'}))
            self.assertEqual(event['type'],event_type)
    def test_cjk_result_chunks_keep_a_conservative_provider_margin(self):
        chunks=_live_text_chunks('训练记录。'*400)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(len(chunk.encode('utf-8'))<=512 for chunk in chunks))
    def test_frame_cap(self):
        with self.assertRaises(LiveVoiceError):safe_sideband_event('x'*262145)

    def test_provider_rejection_logs_safe_diagnostics_without_error_message(self):
        class FakeContent:
            async def iter_chunked(self, _size):
                yield json.dumps(
                    {
                        'error': {
                            'type': 'invalid_request_error',
                            'code': 'context_length_exceeded',
                            'param': 'session.input',
                            'message': 'SENSITIVE PROVIDER DETAIL',
                        }
                    }
                ).encode()

        class FakeResponse:
            status = 400
            headers = {'x-request-id': 'req-safe-1'}
            content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class FakeHttp:
            def post(self, *_args, **_kwargs):
                return FakeResponse()

        with self.assertLogs(
            'orchestrator.frontend_live_voice.openai_live', level='WARNING'
        ) as captured:
            with self.assertRaises(LiveVoiceError) as caught:
                asyncio.run(
                    create_provider_session(
                        FakeHttp(), key='test-key', request={'session': {}}
                    )
                )

        logged = '\n'.join(captured.output)
        self.assertEqual(caught.exception.code, 'live_provider_create_failed')
        self.assertIn('context_length_exceeded', logged)
        self.assertIn('session.input', logged)
        self.assertNotIn('SENSITIVE PROVIDER DETAIL', logged)

class FakeDurable:
    """TEST ONLY. Not a persistence implementation or authorization system."""
    def __init__(self):self.fragments={};self.delegations=set();self.scheduled=[];self.proposal=proposal()
    async def stage_fragment_once(self,binding,item):return None
    async def stage_delegation_once(self,binding,event_id,delegation_id,offset):return None
    async def append_fragment_once(self,binding,item):self.fragments.setdefault(item.provider_event_id,item);return{}
    async def register_delegation_once(self,binding,delegation_id,offset):
        if delegation_id in self.delegations:return False
        self.delegations.add(delegation_id);return True
    async def schedule_proposal(self,binding,delegation_id,offset):self.scheduled.append(delegation_id)
    async def read_proposal(self,binding,delegation_id):return self.proposal

def run_async(fn):
    def wrapper(self, *args, **kwargs):
        return asyncio.run(fn(self, *args, **kwargs))
    return wrapper


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeDurable()
        self.service = LiveVoiceEventService(self.store)

    @run_async
    async def test_delegation_duplicate_has_one_outbox_wakeup(self):
        e = {'type': 'action.proposed', 'event_id': 'event-delegation-1', 'offset_ms': 600, 'delegation': {'id': 'delegation-1', 'target': 'client'}}
        await self.service.on_provider_event(BINDING, e)
        await self.service.on_provider_event(BINDING, e)
        self.assertEqual(self.store.scheduled, ['delegation-1'])

    @run_async
    async def test_caption_alone_does_not_schedule_agent_work(self):
        await self.service.on_provider_event(BINDING, {'type': 'conversation.user.delta', 'event_id': 'e1', 'delta': 'do it', 'start_ms': 0, 'end_ms': 50})
        self.assertEqual(self.store.scheduled, [])

if __name__=='__main__':unittest.main()
