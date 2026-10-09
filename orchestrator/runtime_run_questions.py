"""Telegram answers enter PAO directly, never the ordinary new-Run queue."""
from __future__ import annotations

import re
from orchestrator import runtime_session, ui_language
from orchestrator.frontend_run_questions import question_text
from orchestrator.run_questions import QuestionError, RunQuestions
from orchestrator.session_store import SessionConflict, SessionNotFound

CALLBACK_PATTERN = r'^rq:(que_[a-f0-9]{32}):(text|[0-7])$'


def _service(runtime):
    return RunQuestions(runtime_session.ensure_store(runtime))


def _question(runtime, update, qid):
    return _service(runtime).telegram_question(question_id=qid,
        owner_id=runtime_session.owner_id(runtime), agent_id=runtime.name,
        chat_id=str(update.effective_chat.id))


async def handle_callback(runtime, update, context):
    query = update.callback_query
    if not runtime._is_authorized_user(update.effective_user.id):
        await query.answer()
        return
    if not await runtime._telegram_channel_allowed(update, source_channel='telegram_callback'):
        return
    ui = question_text(ui_language.preferred_locale(runtime, update))
    match = re.fullmatch(CALLBACK_PATTERN, str(query.data or ''))
    if not match:
        await query.answer(ui['closed'], show_alert=True)
        return
    qid, choice = match.groups()
    try:
        q = _question(runtime, update, qid)
        if q['state'] != 'pending':
            await query.answer(ui['saved'] if q['state'] in ('answered', 'consumed') else ui['closed'], show_alert=True)
            return
        if choice == 'text':
            if not q['allow_free_text']:
                raise QuestionError('question_answer_invalid')
            await query.answer(ui['reply'], show_alert=True)
            return
        index = int(choice)
        if index >= len(q['options']):
            raise QuestionError('question_answer_invalid')
        _service(runtime).answer(session_id=q['session_id'], owner_id=runtime_session.owner_id(runtime),
            question_id=qid, payload={'option_id': q['options'][index]['id'],
                                     'idempotency_key': f'telegram:callback:{query.id}'})
    except (QuestionError, SessionConflict, SessionNotFound):
        await query.answer(ui['closed'], show_alert=True)
        return
    except Exception as exc:
        runtime.logger.warning('Question answer outcome unconfirmed (%s)', type(exc).__name__)
        await query.answer(ui['failed'], show_alert=True)
        return
    # Receipt first. Failure to update a Telegram rendition cannot undo the answer.
    await query.answer(ui['saved'])
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        # Retain the durable question identity on replies to an already answered
        # card, so late text cannot accidentally enter the ordinary Run queue.
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(ui['saved'], callback_data=f'rq:{qid}:text')]]))
    except Exception as exc:
        runtime.logger.warning('Question answer saved; Telegram card refresh failed (%s)', type(exc).__name__)


async def handle_text_reply(runtime, update):
    """Only an explicit reply to this bot's question card is an answer."""
    message = update.message
    original = getattr(message, 'reply_to_message', None)
    if original is None or getattr(getattr(original, 'from_user', None), 'id', None) != runtime.app.bot.id:
        return False
    rows = getattr(getattr(original, 'reply_markup', None), 'inline_keyboard', ())
    ids = set()
    for row in rows:
        for button in row:
            match = re.fullmatch(CALLBACK_PATTERN, str(getattr(button, 'callback_data', '') or ''))
            if match:
                ids.add(match.group(1))
    if len(ids) != 1:
        return False
    # The caller has already checked actor authorization and the enterprise gate.
    ui = question_text(ui_language.preferred_locale(runtime, update))
    try:
        qid = ids.pop()
        q = _question(runtime, update, qid)
        _service(runtime).answer(session_id=q['session_id'], owner_id=runtime_session.owner_id(runtime),
            question_id=qid, payload={'text': message.text,
                'idempotency_key': f'telegram:reply:{update.effective_chat.id}:{message.message_id}'})
        text = ui['saved']
    except (QuestionError, SessionConflict, SessionNotFound):
        text = ui['closed']
    except Exception as exc:
        runtime.logger.warning('Question reply outcome unconfirmed (%s)', type(exc).__name__)
        text = ui['failed']
    await runtime._reply_text(update, text)
    return True
