"""FC rendering of PAO questions. Answer authority remains in RunQuestions."""
from __future__ import annotations


def question_blocks(question):
    qid = question['question_id']
    blocks = [{'type': 'text', 'format': 'plain', 'text': question['question']}]
    for index, choice in enumerate(question.get('options', [])):
        if choice.get('description'):
            blocks.append({'type': 'text', 'format': 'plain',
                           'text': f"{choice['label']}: {choice['description']}"})
        blocks.append({'type': 'action', 'action_id': f'{qid}:{index}', 'label': choice['label'],
                       'style': 'secondary', 'payload': {'kind': 'run_question_answer',
                       'question_id': qid, 'option_id': choice['id'], 'index': index}})
    if question.get('allow_free_text'):
        blocks.append({'type': 'action', 'action_id': f'{qid}:text', 'label': 'Write an answer',
                       'style': 'secondary', 'payload': {'kind': 'run_question_text', 'question_id': qid}})
    return blocks


_TEXT = {
    'en': {'write': 'Write an answer', 'reply': 'Reply to this question message with your answer.',
           'saved': 'Answer received', 'closed': 'This question is no longer accepting answers.',
           'expired': 'This question has expired. No answer was received.',
           'cancelled': 'This question was cancelled.',
           'failed': 'Answer not confirmed. Retry the same answer.'},
    'zh-CN': {'write': '自行填写', 'reply': '请回复这条问题消息，填写您的答案。',
              'saved': '已收到答复', 'closed': '这条问题已结束，无法再提交答复。',
              'expired': '问题已过期，未收到答复。', 'cancelled': '问题已取消。', 'failed': '答复尚未确认，请重交同一答复。'},
    'zh-TW': {'write': '自行填寫', 'reply': '請回覆這則問題訊息，填寫您的答案。',
              'saved': '已收到答覆', 'closed': '這則問題已結束，無法再提交答覆。',
              'expired': '問題已過期，未收到答覆。', 'cancelled': '問題已取消。', 'failed': '答覆尚未確認，請重交同一答覆。'},
    'ja': {'write': '自由に回答', 'reply': 'この質問メッセージに返信して回答してください。',
           'saved': '回答を受け取りました', 'closed': 'この質問の回答受付は終了しました。',
           'expired': '質問の有効期限が切れました。回答はありません。', 'cancelled': '質問は取り消されました。', 'failed': '回答を確認できません。同じ回答を再送してください。'},
}


def question_text(locale):
    return _TEXT.get(locale, _TEXT['en'])


def telegram_button(block, locale):
    from telegram import InlineKeyboardButton
    payload = block.get('payload') or {}
    qid = payload['question_id']
    free = payload['kind'] == 'run_question_text'
    return InlineKeyboardButton(question_text(locale)['write'] if free else block['label'],
                                callback_data=f"rq:{qid}:{'text' if free else payload['index']}")


class QuestionRenditions:
    """FC transport identities, derived from PAO questions; never answer state."""

    def __init__(self, store):
        from orchestrator.run_questions import RunQuestions
        RunQuestions(store)
        self.store = store
        with store._lock, store._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS frontend_question_renditions (
                question_id TEXT NOT NULL, endpoint_id TEXT NOT NULL,
                chat_id TEXT NOT NULL, transport_message_id TEXT NOT NULL,
                presentation_state TEXT NOT NULL,
                PRIMARY KEY(question_id,endpoint_id,transport_message_id),
                UNIQUE(endpoint_id,transport_message_id),
                FOREIGN KEY(question_id) REFERENCES run_questions(question_id))""")

    def record(self, *, question_id, endpoint_id, chat_id, transport_message_id,
               owner_id, agent_id, request_id):
        from orchestrator.run_questions import RunQuestions, QuestionError
        question = RunQuestions(self.store).telegram_question(
            question_id=question_id, owner_id=owner_id, agent_id=agent_id,
            chat_id=str(chat_id))
        if question["request_id"] != request_id:
            raise QuestionError("question_transport_scope_invalid")
        with self.store._lock, self.store._connection() as connection:
            connection.execute(
                "INSERT INTO frontend_question_renditions VALUES(?,?,?,?,?) "
                "ON CONFLICT(question_id,endpoint_id,transport_message_id) DO NOTHING",
                (question_id, endpoint_id, str(chat_id), str(transport_message_id), "pending"))

    def resolve(self, *, agent_id, chat_id, transport_message_id):
        with self.store._lock, self.store._connection() as connection:
            row = connection.execute(
                "SELECT q.question_id FROM frontend_question_renditions f "
                "JOIN run_questions q ON q.question_id=f.question_id "
                "WHERE q.instance_id=? AND q.agent_id=? AND f.chat_id=? "
                "AND f.transport_message_id=?",
                (self.store.instance_id, str(agent_id).lower(), str(chat_id),
                 str(transport_message_id))).fetchone()
            return row["question_id"] if row else None

    def pending_updates(self, *, agent_id, limit=40):
        from orchestrator.run_questions import RunQuestions
        service = RunQuestions(self.store)
        with self.store._lock, self.store._connection() as connection:
            rows = connection.execute(
                "SELECT q.*,f.endpoint_id,f.chat_id,f.transport_message_id "
                "FROM frontend_question_renditions f "
                "JOIN run_questions q ON q.question_id=f.question_id "
                "WHERE q.instance_id=? AND q.agent_id=? AND q.state!='pending' "
                "AND f.presentation_state!=q.state AND f.presentation_state!='unavailable' "
                "ORDER BY q.created_at LIMIT ?",
                (self.store.instance_id, str(agent_id).lower(), limit)).fetchall()
            return [{**service._public(row), **{key: row[key] for key in
                     ("endpoint_id", "chat_id", "transport_message_id")}} for row in rows]

    def mark_rendered(self, question, *, unavailable=False):
        with self.store._lock, self.store._connection() as connection:
            connection.execute(
                "UPDATE frontend_question_renditions SET presentation_state=? "
                "WHERE question_id=? AND endpoint_id=? AND transport_message_id=?",
                ("unavailable" if unavailable else question["state"],
                 question["question_id"], question["endpoint_id"],
                 question["transport_message_id"]))


def question_identity(event):
    identities = {(block.get("payload") or {}).get("question_id")
                  for block in event.get("content_blocks", ())
                  if (block.get("payload") or {}).get("kind")
                  in {"run_question_answer", "run_question_text"}}
    if len(identities) > 1 or (identities and not next(iter(identities))):
        raise ValueError("question transport identity is ambiguous")
    return next(iter(identities), None)


async def refresh_telegram_question_renditions(runtime, store):
    """Edit the original card in place; retrying this rendition is idempotent."""
    import html
    from orchestrator import ui_language
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.error import BadRequest, Forbidden

    renditions = QuestionRenditions(store)
    ui = question_text(ui_language.preferred_locale(runtime))
    for question in renditions.pending_updates(agent_id=runtime.name):
        state = question["state"]
        label = ui["saved"] if state in {"answered", "consumed"} else ui.get(state, ui["closed"])
        text = html.escape(label) + "\n\n" + html.escape(question["question"])
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            label, callback_data=f"rq:{question['question_id']}:text")]])
        try:
            await runtime.app.bot.edit_message_text(
                chat_id=int(question["chat_id"]), message_id=int(question["transport_message_id"]),
                text=text, parse_mode="HTML", reply_markup=markup)
        except Forbidden as exc:
            renditions.mark_rendered(question, unavailable=True)
            runtime.logger.warning("Question card is no longer accessible (%s)", type(exc).__name__)
            continue
        except BadRequest as exc:
            detail = str(exc).lower()
            if "message is not modified" not in detail:
                if "message to edit not found" in detail or "message can't be edited" in detail:
                    renditions.mark_rendered(question, unavailable=True)
                runtime.logger.warning("Question card update failed (%s)", type(exc).__name__)
                continue
        except Exception as exc:
            runtime.logger.warning("Question card update unconfirmed (%s)", type(exc).__name__)
            continue
        renditions.mark_rendered(question)
