"""Offline reference tests with explicitly fake ports; not repository/live qualification."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

from orchestrator.frontend_live_voice.protocol import CallBinding, Fragment, LiveVoiceError, normalize_transcript
from orchestrator.frontend_live_voice.delegation import build_proposal
from orchestrator.frontend_live_voice.service import LiveVoiceEventService
from orchestrator.frontend_live_voice.openai_live import session_request, append_update, safe_sideband_event

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
        f=normalize_transcript({'type':'session.input_transcript.delta','event_id':'event1','delta':'go go ','start_ms':0,'end_ms':10})
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
    def test_client_delegation_and_recording_off(self):
        req=session_request('v=0\r\n','Voice style only.', voice='willow')
        self.assertEqual(req['session']['delegation'],{'type':'client'});self.assertFalse(req['session']['store'])
        self.assertEqual(req['session']['audio']['output']['voice'],'willow')
        self.assertEqual(req['transport']['type'],'webrtc')
    def test_unqualified_model_not_silently_selected(self):
        with self.assertRaises(LiveVoiceError):session_request('v=0','style','other')
    def test_unqualified_voice_not_silently_selected(self):
        with self.assertRaises(LiveVoiceError):session_request('v=0','style',voice='other')
    def test_cjk_instructions_use_character_projection_not_utf8_byte_count(self):
        req=session_request('v=0','温柔地说话。'*1200)
        self.assertEqual(req['session']['model'],'gpt-live-1')
    def test_token_budget_not_character_length(self):
        with self.assertRaises(LiveVoiceError):append_update('commentary','short','d1','e1',lambda _text:501)
    def test_public_update_identity(self):
        e=append_update('commentary','Done.','d1','e1',lambda _text:2)
        self.assertEqual(e['delegation_id'],'d1');self.assertEqual(e['type'],'session.commentary.append')
    def test_audio_copies_dropped_before_projection(self):
        self.assertIsNone(safe_sideband_event(json.dumps({'type':'session.output_audio.delta','delta':'raw'})))
    def test_unknown_sideband_type_ignored(self):
        self.assertIsNone(safe_sideband_event(json.dumps({'type':'response.function_call','arguments':'danger'})))
    def test_frame_cap(self):
        with self.assertRaises(LiveVoiceError):safe_sideband_event('x'*262145)

class FakeDurable:
    """TEST ONLY. Not a persistence implementation or authorization system."""
    def __init__(self):self.fragments={};self.delegations=set();self.scheduled=[];self.proposal=proposal()
    async def append_fragment_once(self,binding,item):self.fragments.setdefault(item.provider_event_id,item);return{}
    async def register_delegation_once(self,binding,delegation_id,offset):
        if delegation_id in self.delegations:return False
        self.delegations.add(delegation_id);return True
    async def schedule_proposal(self,binding,delegation_id,offset):self.scheduled.append(delegation_id)
    async def read_proposal(self,binding,delegation_id):return self.proposal
class FakeAdmission:
    def __init__(self):self.receipts={};self.count=0
    async def find_decision(self,binding,*,idempotency_key,request_digest):
        prior=self.receipts.get(idempotency_key)
        if prior and prior[0]!=request_digest:raise LiveVoiceError('live_idempotency_conflict',409)
        return prior[1] if prior else None
    async def decide_and_admit(self,binding,proposal,*,decision,idempotency_key,request_digest):
        # A real implementation uses the PAO transaction; this tests coordinator wiring only.
        prior=await self.find_decision(binding,idempotency_key=idempotency_key,request_digest=request_digest)
        if prior:return prior
        if decision=='confirm':self.count+=1
        result={'ok':True,'run_id':'run-1' if decision=='confirm' else None}
        self.receipts[idempotency_key]=(request_digest,result);return result

def run_async(fn):
    def wrapper(self, *args, **kwargs):
        return asyncio.run(fn(self, *args, **kwargs))
    return wrapper


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeDurable()
        self.admission = FakeAdmission()
        self.service = LiveVoiceEventService(self.store, self.admission)

    def body(self, **changes):
        return dict({'delegation_id': 'delegation-1', 'proposal_version': 1, 'proposal_digest': self.store.proposal.digest, 'decision': 'confirm', 'idempotency_key': 'decision-1'}, **changes)

    @run_async
    async def test_delegation_duplicate_has_one_outbox_wakeup(self):
        e = {'type': 'session.delegation.created', 'offset_ms': 600, 'delegation': {'id': 'delegation-1', 'target': 'client'}}
        await self.service.on_provider_event(BINDING, e)
        await self.service.on_provider_event(BINDING, e)
        self.assertEqual(self.store.scheduled, ['delegation-1'])
        self.assertEqual(self.admission.count, 0)

    @run_async
    async def test_caption_does_not_admit_task(self):
        await self.service.on_provider_event(BINDING, {'type': 'session.input_transcript.delta', 'event_id': 'e1', 'delta': 'do it', 'start_ms': 0, 'end_ms': 50})
        self.assertEqual(self.admission.count, 0)

    @run_async
    async def test_confirmation_replay_returns_same_receipt(self):
        a = await self.service.decide(BINDING, self.body())
        b = await self.service.decide(BINDING, self.body())
        self.assertEqual(a, b)
        self.assertEqual(self.admission.count, 1)

    @run_async
    async def test_same_key_different_payload_conflicts(self):
        await self.service.decide(BINDING, self.body())
        with self.assertRaises(LiveVoiceError):
            await self.service.decide(BINDING, self.body(decision='discard'))

    @run_async
    async def test_changed_digest_rejected_before_admission(self):
        with self.assertRaises(LiveVoiceError):
            await self.service.decide(BINDING, self.body(proposal_digest='0'*64))
        self.assertEqual(self.admission.count, 0)

    @run_async
    async def test_discard_creates_no_run(self):
        await self.service.decide(BINDING, self.body(decision='discard'))
        self.assertEqual(self.admission.count, 0)

    @run_async
    async def test_boolean_proposal_version_rejected(self):
        with self.assertRaises(LiveVoiceError):
            await self.service.decide(BINDING, self.body(proposal_version=True))

    @run_async
    async def test_unknown_browser_fields_rejected(self):
        with self.assertRaises(LiveVoiceError):
            await self.service.decide(BINDING, self.body(owner_id='admin'))

    @run_async
    async def test_replay_resolved_before_new_proposal_gate(self):
        body = self.body()
        expected = await self.service.decide(BINDING, body)
        self.store.proposal = replace(self.store.proposal, version=2, digest='1'*64)
        self.assertEqual(await self.service.decide(BINDING, body), expected)

if __name__=='__main__':unittest.main()
