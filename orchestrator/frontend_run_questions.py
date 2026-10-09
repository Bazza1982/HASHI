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
           'failed': 'Answer not confirmed. Retry the same answer.'},
    'zh-CN': {'write': '自行填写', 'reply': '请回复这条问题消息，填写您的答案。',
              'saved': '已收到答复', 'closed': '这条问题已结束，无法再提交答复。', 'failed': '答复尚未确认，请重交同一答复。'},
    'zh-TW': {'write': '自行填寫', 'reply': '請回覆這則問題訊息，填寫您的答案。',
              'saved': '已收到答覆', 'closed': '這則問題已結束，無法再提交答覆。', 'failed': '答覆尚未確認，請重交同一答覆。'},
    'ja': {'write': '自由に回答', 'reply': 'この質問メッセージに返信して回答してください。',
           'saved': '回答を受け取りました', 'closed': 'この質問の回答受付は終了しました。', 'failed': '回答を確認できません。同じ回答を再送してください。'},
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
