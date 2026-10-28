"""Native Codex user-input RPCs backed by the same PAO question owner as HER."""
from __future__ import annotations

import asyncio
import hashlib

from orchestrator.run_questions import QuestionError, UnansweredQuestion


class NativeRunQuestions:
    def __init__(self, service, *, request_id, agent_id, activity=lambda: None, interval=0.5):
        self.service = service
        self.request_id, self.agent_id = request_id, agent_id
        self.activity, self.interval = activity, interval
        self.created = {}

    async def answer(self, rpc_id, params):
        questions = params.get('questions')
        if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
            raise ValueError('native question batch must contain one to three questions')
        if any(q.get('isSecret') for q in questions):
            raise ValueError('secret input is not supported by shared question cards')
        if len({q.get('id') for q in questions}) != len(questions):
            raise ValueError('native question ids must be unique')
        key = hashlib.sha256(str(rpc_id).encode()).hexdigest()[:24]
        batch = self.created.setdefault(str(rpc_id), [])
        answers = {}
        try:
            for index, raw in enumerate(questions):
                options = [{'id': f'option_{i}', 'label': o['label'], 'description': o.get('description', '')}
                           for i, o in enumerate(raw.get('options') or [])]
                try:
                    q = self.service.create(request_id=self.request_id, agent_id=self.agent_id, payload={
                        'idempotency_key': f'codex-native:{params["itemId"]}:{key}:{index}',
                        'question': raw['question'], 'options': options,
                        'allow_free_text': True,
                    })
                except QuestionError as exc:
                    if exc.code == 'question_cards_disabled':
                        raise UnansweredQuestion(
                            [{'native_question_id': item['id'], 'question': item['question']}
                             for item in questions],
                            'question_cards_disabled', partial_answers=answers,
                        ) from exc
                    raise
                batch.append((raw['id'], q))
            while len(answers) < len(batch):
                self.activity()
                terminal_questions = []
                terminal_reason = None
                cancelled_without_reason = False
                for original_id, q in batch:
                    if original_id in answers:
                        continue
                    current = self.service.get_answer(request_id=self.request_id,
                        agent_id=self.agent_id, question_id=q['question_id'])
                    if current['state'] == 'expired':
                        terminal_questions.append(
                            {**current, "native_question_id": original_id}
                        )
                        terminal_reason = terminal_reason or "expired"
                        continue
                    if current['state'] == 'cancelled':
                        if current.get("resolution_reason"):
                            terminal_questions.append(
                                {**current, "native_question_id": original_id}
                            )
                            terminal_reason = (
                                terminal_reason or current["resolution_reason"]
                            )
                        else:
                            cancelled_without_reason = True
                        continue
                    if current['state'] == 'consumed':
                        answer = current['answer']
                        if answer.get('system_action') == 'continue':
                            raise UnansweredQuestion(
                                [{**current, 'native_question_id': original_id}],
                                'user_continue', partial_answers=answers,
                            )
                        values = [o['label'] for o in q['options'] if o['id'] == answer['option_id']]
                        if answer['text']:
                            values.append(answer['text'])
                        answers[original_id] = {'answers': values}
                if terminal_questions:
                    raise UnansweredQuestion(
                        terminal_questions,
                        terminal_reason or "expired",
                        partial_answers=answers,
                    )
                if cancelled_without_reason:
                    raise QuestionError('question_cancelled')
                if len(answers) < len(batch):
                    await asyncio.sleep(self.interval)
            return {'answers': answers}
        finally:
            self.cancel(rpc_id)

    def cancel(self, rpc_id):
        for _, q in self.created.pop(str(rpc_id), []):
            self.service.cancel(request_id=self.request_id, agent_id=self.agent_id, question_id=q['question_id'])
