"""Telegram answers enter PAO directly, never the ordinary new-Run queue."""
from __future__ import annotations

import re
from orchestrator import runtime_session, ui_language
from orchestrator.frontend_database import run_database_work
from orchestrator.frontend_run_questions import question_error_text, question_text
from orchestrator.run_questions import QuestionError, RunQuestions
from orchestrator.session_store import SessionConflict, SessionNotFound

CALLBACK_PATTERN = r'^rq:(que_[a-f0-9]{32}):(text|[0-9])$'


def _service(runtime):
    return RunQuestions(runtime_session.ensure_store(runtime))


def _database(runtime, operation, **scope):
    return getattr(_service(runtime), operation)(**scope)


async def _question(runtime, update, qid):
    return await run_database_work(_database, runtime, 'telegram_question', question_id=qid,
        owner_id=runtime_session.owner_id(runtime), agent_id=runtime.name,
        chat_id=str(update.effective_chat.id))


def _answer_error_text(error, locale):
    code = (error.code if isinstance(error, QuestionError) else
            'question_not_found' if isinstance(error, SessionNotFound) else
            'question_state_changed')
    return question_error_text(code, locale)


async def handle_callback(runtime, update, context):
    query = update.callback_query
    if not runtime._is_authorized_user(update.effective_user.id):
        await query.answer()
        return
    if not await runtime._telegram_channel_allowed(update, source_channel='telegram_callback'):
        return
    locale = ui_language.preferred_locale(runtime, update)
    ui = question_text(locale)
    match = re.fullmatch(CALLBACK_PATTERN, str(query.data or ''))
    if not match:
        await query.answer(ui['invalid'], show_alert=True)
        return
    qid, choice = match.groups()
    try:
        q = await _question(runtime, update, qid)
        if q['state'] != 'pending':
            stop_unconfirmed = (q['state'] == 'answered' and
                                (q.get('answer') or {}).get('system_action') == 'stop')
            retry_stop = (stop_unconfirmed and choice != 'text' and
                          int(choice) < len(q['options']) and
                          q['options'][int(choice)].get('system_action') == 'stop')
            if not retry_stop:
                label = (ui['failed'] if stop_unconfirmed else
                         ui['saved'] if q['state'] in ('answered', 'consumed') else
                         question_error_text('question_' + q['state'], locale))
                await query.answer(label, show_alert=True)
                return
        if choice == 'text':
            if not q['allow_free_text']:
                raise QuestionError('question_answer_invalid')
            await query.answer(ui['reply'], show_alert=True)
            return
        index = int(choice)
        if index >= len(q['options']):
            raise QuestionError('question_answer_invalid')
        saved = await run_database_work(_database, runtime, 'answer', session_id=q['session_id'], owner_id=runtime_session.owner_id(runtime),
            question_id=qid, payload={'option_id': q['options'][index]['id'],
                                     'idempotency_key': f'telegram:callback:{query.id}'})
        if (saved.get('answer') or {}).get('system_action') == 'stop':
            from types import SimpleNamespace
            from orchestrator import runtime_control
            message = getattr(update, 'effective_message', None) or getattr(query, 'message', None)
            if message is None:
                await query.answer(ui['failed'], show_alert=True)
                return
            stop_update = SimpleNamespace(
                effective_user=update.effective_user, effective_chat=update.effective_chat,
                message=message, effective_message=message,
                update_id=getattr(update, 'update_id', None),
            )
            result = await runtime_control.cmd_stop(
                runtime, stop_update, SimpleNamespace(_hashi_question_stop_id=qid),
            )
            if not result or not result.get('ok'):
                await query.answer(ui['failed'], show_alert=True)
                return
    except (QuestionError, SessionConflict, SessionNotFound) as exc:
        await query.answer(_answer_error_text(exc, locale), show_alert=True)
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
    if not ids:
        # Some Telegram clients omit reply_markup on the quoted message.
        from orchestrator.frontend_run_questions import QuestionRenditions
        def resolve(**scope):
            return QuestionRenditions(runtime_session.ensure_store(runtime)).resolve(**scope)
        qid = await run_database_work(resolve,
            agent_id=runtime.name, chat_id=update.effective_chat.id,
            transport_message_id=getattr(original, "message_id", ""))
        if qid:
            ids.add(qid)
    if len(ids) != 1:
        return False
    # The caller has already checked actor authorization and the enterprise gate.
    locale = ui_language.preferred_locale(runtime, update)
    ui = question_text(locale)
    try:
        qid = ids.pop()
        q = await _question(runtime, update, qid)
        await run_database_work(_database, runtime, 'answer', session_id=q['session_id'], owner_id=runtime_session.owner_id(runtime),
            question_id=qid, payload={'text': message.text,
                'idempotency_key': f'telegram:reply:{update.effective_chat.id}:{message.message_id}'})
        text = ui['saved']
    except (QuestionError, SessionConflict, SessionNotFound) as exc:
        text = _answer_error_text(exc, locale)
    except Exception as exc:
        runtime.logger.warning('Question reply outcome unconfirmed (%s)', type(exc).__name__)
        text = ui['failed']
    await runtime._reply_text(update, text)
    return True
# HASHI-Fingerprint: hfp_cba45ce716ce4075940f37b8214daf33
