"""FC rendering of PAO questions. Answer authority remains in RunQuestions."""
from __future__ import annotations

import threading

from orchestrator.frontend_database import run_database_work

_SCHEMA_LOCK = threading.RLock()


def question_blocks(question):
    qid = question['question_id']
    blocks = [{'type': 'text', 'format': 'plain', 'text': question['question']}]
    choices = list(question.get('options', []))
    if not any(choice.get('system_action') for choice in choices):
        from orchestrator.run_questions import system_question_options
        choices.extend(system_question_options(choices))
    for index, choice in enumerate(choices):
        if choice.get('description'):
            blocks.append({'type': 'text', 'format': 'plain',
                           'text': f"{choice['label']}: {choice['description']}"})
        blocks.append({'type': 'action', 'action_id': f'{qid}:{index}', 'label': choice['label'],
                       'style': 'secondary', 'payload': {'kind': 'run_question_answer',
                       'question_id': qid, 'option_id': choice['id'], 'index': index,
                       'system_action': choice.get('system_action')}})
    if question.get('allow_free_text'):
        blocks.append({'type': 'action', 'action_id': f'{qid}:text', 'label': 'Write an answer',
                       'style': 'secondary', 'payload': {'kind': 'run_question_text', 'question_id': qid}})
    return blocks


_TEXT = {
    'en': {'write': 'Write an answer', 'reply': 'Reply to this question message with your answer.',
           'saved': 'Answer received', 'closed': 'This question is no longer accepting answers.',
           'expired': 'This question has expired. No answer was received.',
           'cancelled': 'This question was cancelled.',
           'invalid': 'Invalid answer. Choose a current option or reply with 1–4000 characters.',
           'unavailable': 'This question is unavailable in this conversation. Refresh the question list.',
           'changed': 'The question state changed. Refresh it and check whether your answer was received.',
           'already_answered': 'This question already has an answer. Refresh it to check the saved answer.',
           'failed': 'Answer not confirmed. Refresh the question and check whether it was received.'},
    'zh-CN': {'write': '自行填写', 'reply': '请回复这条问题消息，填写您的答案。',
              'saved': '已收到答复', 'closed': '这条问题已结束，无法再提交答复。',
              'expired': '问题已过期，未收到答复。', 'cancelled': '问题已取消。',
              'invalid': '答复无效，请选择当前选项，或回复1至4000字的答案。',
              'unavailable': '当前会话无法读取这条问题，请刷新问题列表。',
              'changed': '问题状态已变化，请刷新后检查答复是否已收到。',
              'already_answered': '这条问题已有答复，请刷新后检查已保存的答案。',
              'failed': '答复尚未确认，请刷新问题，核对是否已收到。'},
    'zh-TW': {'write': '自行填寫', 'reply': '請回覆這則問題訊息，填寫您的答案。',
              'saved': '已收到答覆', 'closed': '這則問題已結束，無法再提交答覆。',
              'expired': '問題已過期，未收到答覆。', 'cancelled': '問題已取消。',
              'invalid': '答覆無效，請選擇目前選項，或回覆1至4000字的答案。',
              'unavailable': '目前對話無法讀取這則問題，請重新整理問題清單。',
              'changed': '問題狀態已變更，請重新整理後檢查答覆是否已收到。',
              'already_answered': '這則問題已有答覆，請重新整理後檢查已儲存的答案。',
              'failed': '答覆尚未確認，請重新整理問題，核對是否已收到。'},
    'ja': {'write': '自由に回答', 'reply': 'この質問メッセージに返信して回答してください。',
           'saved': '回答を受け取りました', 'closed': 'この質問の回答受付は終了しました。',
           'expired': '質問の有効期限が切れました。回答はありません。', 'cancelled': '質問は取り消されました。',
           'invalid': '無効な回答です。現在の選択肢を選ぶか、1〜4000文字で返信してください。',
           'unavailable': 'この会話では質問を読み取れません。質問一覧を更新してください。',
           'changed': '質問の状態が変わりました。更新して回答が届いたか確認してください。',
           'already_answered': 'この質問には回答があります。更新して保存された回答を確認してください。',
           'failed': '回答を確認できません。質問を更新して回答が届いたか確認してください。'},
}


def question_text(locale):
    return _TEXT.get(locale, _TEXT['en'])


def question_error_text(code, locale):
    """Render only known PAO codes, never exception text or an answer."""
    key = {
        'question_answer_invalid': 'invalid',
        'question_answer_required': 'invalid',
        'question_already_answered': 'already_answered',
        'question_expired': 'expired',
        'question_cancelled': 'cancelled',
        'question_not_found': 'unavailable',
        'question_transport_scope_invalid': 'unavailable',
        'question_state_changed': 'changed',
        'question_run_not_active': 'changed',
    }.get(code, 'failed')
    return question_text(locale)[key]


def telegram_button(block, locale):
    from telegram import InlineKeyboardButton
    payload = block.get('payload') or {}
    qid = payload['question_id']
    free = payload['kind'] == 'run_question_text'
    from orchestrator import ui_language
    system_keys = {'continue': 'askme.continue', 'stop': 'askme.stop'}
    label = (question_text(locale)['write'] if free else
             ui_language.tr(system_keys[payload['system_action']], locale=locale)
             if payload.get('system_action') in system_keys else block['label'])
    return InlineKeyboardButton(label, callback_data=f"rq:{qid}:{'text' if free else payload['index']}")


class QuestionRenditions:
    """FC transport identities, derived from PAO questions; never answer state."""

    def __init__(self, store):
        from orchestrator.run_questions import RunQuestions
        RunQuestions(store)
        self.store = store
        if getattr(store, '_question_renditions_schema_ready', False):
            return
        with _SCHEMA_LOCK:
            if getattr(store, '_question_renditions_schema_ready', False):
                return
            with store._connection() as connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS frontend_question_renditions (
                    question_id TEXT NOT NULL, endpoint_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL, transport_message_id TEXT NOT NULL,
                    presentation_state TEXT NOT NULL,
                    PRIMARY KEY(question_id,endpoint_id,transport_message_id),
                    UNIQUE(endpoint_id,transport_message_id),
                    FOREIGN KEY(question_id) REFERENCES run_questions(question_id))""")
            store._question_renditions_schema_ready = True

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
                (question_id, endpoint_id, str(chat_id), str(transport_message_id), "pending_v2"))

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
                "WHERE q.instance_id=? AND q.agent_id=? AND f.presentation_state!='unavailable' "
                "AND ((q.state='pending' AND f.presentation_state='pending') OR "
                "(q.state!='pending' AND f.presentation_state!=q.state)) "
                "ORDER BY q.created_at LIMIT ?",
                (self.store.instance_id, str(agent_id).lower(), limit)).fetchall()
            return [{**service._public(row), **{key: row[key] for key in
                     ("endpoint_id", "chat_id", "transport_message_id")}} for row in rows]

    def mark_rendered(self, question, *, unavailable=False):
        with self.store._lock, self.store._connection() as connection:
            connection.execute(
                "UPDATE frontend_question_renditions SET presentation_state=? "
                "WHERE question_id=? AND endpoint_id=? AND transport_message_id=?",
                ("unavailable" if unavailable else
                 "pending_v2" if question["state"] == "pending" else question["state"],
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

    def pending():
        renditions = QuestionRenditions(store)
        return renditions, renditions.pending_updates(agent_id=runtime.name)
    renditions, questions = await run_database_work(pending, _db_role='worker')
    locale = ui_language.preferred_locale(runtime)
    ui = question_text(locale)
    for question in questions:
        state = question["state"]
        stop_unconfirmed = (state == "answered" and
                            (question.get("answer") or {}).get("system_action") == "stop")
        if state == "pending":
            blocks = question_blocks(question)
            text = "\n".join(html.escape(block["text"]) for block in blocks if block["type"] == "text")
            markup = InlineKeyboardMarkup([[telegram_button(block, locale)]
                                           for block in blocks if block["type"] == "action"])
        elif stop_unconfirmed:
            blocks = question_blocks(question)
            stop = next(block for block in blocks if block.get("type") == "action"
                        and (block.get("payload") or {}).get("system_action") == "stop")
            text = html.escape(ui_language.tr("askme.stop_unconfirmed", locale=locale))
            markup = InlineKeyboardMarkup([[telegram_button(stop, locale)]])
        else:
            label = ui["saved"] if state in {"answered", "consumed"} else ui.get(state, ui["closed"])
            text = html.escape(label) + "\n\n" + html.escape(question["question"])
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                label, callback_data=f"rq:{question['question_id']}:text")]])
        try:
            await runtime.app.bot.edit_message_text(
                chat_id=int(question["chat_id"]), message_id=int(question["transport_message_id"]),
                text=text, parse_mode="HTML", reply_markup=markup)
        except Forbidden as exc:
            await run_database_work(renditions.mark_rendered, question, unavailable=True)
            runtime.logger.warning("Question card is no longer accessible (%s)", type(exc).__name__)
            continue
        except BadRequest as exc:
            detail = str(exc).lower()
            if "message is not modified" not in detail:
                if "message to edit not found" in detail or "message can't be edited" in detail:
                    await run_database_work(renditions.mark_rendered, question, unavailable=True)
                runtime.logger.warning("Question card update failed (%s)", type(exc).__name__)
                continue
        except Exception as exc:
            runtime.logger.warning("Question card update unconfirmed (%s)", type(exc).__name__)
            continue
        await run_database_work(renditions.mark_rendered, question)
# HASHI-Fingerprint: hfp_be43b1e442e34cca9dd04065780788f4
