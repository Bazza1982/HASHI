"""PAO-owned durable questions for an active Run; answers confer no permissions."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from uuid import uuid4
from contextlib import contextmanager

from orchestrator.session_store import SessionNotFound

class QuestionError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status

def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def _stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')

def issue_tool_token(secret, *, instance_id, agent_id, request_id, now=None):
    if not secret or not instance_id or not agent_id or not request_id:
        raise QuestionError('question_tool_scope_unavailable', 503)
    claims = {'v': 1, 'instance_id': instance_id, 'agent_id': agent_id,
              'request_id': request_id, 'expires': int((time.time() if now is None else now) + 3600)}
    body = base64.urlsafe_b64encode(_json(claims).encode()).decode().rstrip('=')
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + '.' + sig

def verify_tool_token(secret, token, *, instance_id, now=None):
    try:
        body, supplied = token.split('.', 1)
        if not secret or len(token) > 4096:
            raise ValueError()
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if (claims.get('v') != 1 or claims.get('instance_id') != instance_id
            or claims.get('expires', 0) <= (time.time() if now is None else now)
            or not claims.get('agent_id') or not claims.get('request_id')):
            raise ValueError()
        return claims
    except (ValueError, TypeError, KeyError, AttributeError):
        raise QuestionError('question_tool_not_authenticated', 401) from None

class RunQuestions:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock
        with store._lock, store._connection() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS run_questions (
                question_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
                owner_id TEXT NOT NULL, agent_id TEXT NOT NULL, session_id TEXT NOT NULL,
                run_id TEXT NOT NULL, request_id TEXT NOT NULL, context_generation INTEGER NOT NULL,
                create_key TEXT NOT NULL, create_digest TEXT NOT NULL, question_json TEXT NOT NULL,
                state TEXT NOT NULL, answer_json TEXT, answer_key TEXT,
                created_at TEXT NOT NULL, expires_epoch REAL NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(run_id,create_key), FOREIGN KEY(run_id) REFERENCES runs(run_id))''')
            c.execute('CREATE INDEX IF NOT EXISTS run_questions_session ON run_questions(session_id,context_generation,created_at)')

    @contextmanager
    def _transaction(self):
        # A Python RLock does not fence the Agent Worker's independent SQLite
        # connection. Reserve the write transaction before *any* scope read so
        # cancellation, context changes and concurrent answers are linearized.
        with self.store._lock, self.store._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                yield c
            except QuestionError:
                # A refused answer may still have legitimately refreshed an
                # expired/cancelled question. Persist that fact, not the answer.
                c.commit()
                raise

    def _session(self, c, session_id, *, owner_id=None):
        clauses=['session_id=?', 'instance_id=?', "status!='deleted'"]
        args=[session_id,self.store.instance_id]
        if owner_id is not None:
            clauses.append('owner_id=?'); args.append(owner_id)
        row=c.execute('SELECT * FROM sessions WHERE '+' AND '.join(clauses),args).fetchone()
        if row is None:
            raise SessionNotFound(str(session_id))
        return row

    def _run(self, c, request_id, agent_id):
        row=c.execute('''SELECT r.* FROM runs r JOIN sessions s ON s.session_id=r.session_id
            WHERE r.request_id=? AND r.agent_id=? AND s.instance_id=?''',
            (request_id,str(agent_id).lower(),self.store.instance_id)).fetchone()
        if row is None:
            raise SessionNotFound(str(request_id))
        return row

    @staticmethod
    def _text(value, limit, code):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise QuestionError(code, 400)
        return value.strip()

    def _public(self, row):
        q = json.loads(row['question_json'])
        return {**q, **{k:row[k] for k in ('question_id','instance_id','agent_id','session_id',
                'run_id','request_id','context_generation','state','created_at','updated_at')},
                'expires_at':_stamp(row['expires_epoch']),
                'answer':json.loads(row['answer_json']) if row['answer_json'] else None}

    def _event(self, c, row, kind):
        self.store._append_event(c, session_id=row['session_id'], run_id=row['run_id'],
            kind='run.question.' + kind, status=row['state'], summary='', detail=self._public(row),
            outbox=kind == 'created')

    def cancel(self, *, request_id, agent_id, question_id):
        """Withdraw a native request resolved/interrupted by its owning Engine."""
        with self._transaction() as c:
            run = self._run(c, request_id, agent_id)
            row = c.execute('SELECT * FROM run_questions WHERE question_id=? AND run_id=?',
                            (question_id, run['run_id'])).fetchone()
            if row is None:
                raise QuestionError('question_not_found', 404)
            if row['state'] in ('pending', 'answered'):
                c.execute("UPDATE run_questions SET state='cancelled',updated_at=? WHERE question_id=?",
                          (_stamp(self.clock()), question_id))
                row = c.execute('SELECT * FROM run_questions WHERE question_id=?', (question_id,)).fetchone()
                self._event(c, row, 'cancelled')
            return self._public(row)

    def telegram_question(self, *, question_id, owner_id, agent_id, chat_id):
        """Bind an authenticated Telegram reply to the original frozen Run route."""
        from orchestrator.frontend_delivery import route_destination
        with self._transaction() as c:
            row = c.execute('SELECT * FROM run_questions WHERE question_id=? AND owner_id=? AND agent_id=? AND instance_id=?',
                            (question_id, owner_id, str(agent_id).lower(), self.store.instance_id)).fetchone()
            if row is None:
                raise QuestionError('question_not_found', 404)
            row = self._scoped(c, question_id, session_id=row['session_id'], owner_id=owner_id)
            run = self._run(c, row['request_id'], agent_id)
            destination = route_destination(json.loads(run['delivery_route_json']), 'telegram')
            if destination is None or str(destination['channel_key']) != str(chat_id):
                raise QuestionError('question_not_found', 404)
            return self._public(row)

    def pending_telegram_deliveries(self, *, agent_id, limit=20):
        """Derived view of existing FC tasks; question creation/outbox is atomic."""
        with self._transaction() as c:
            rows = c.execute('''SELECT q.*, e.event_id FROM run_questions q
                JOIN run_events e ON e.run_id=q.run_id AND e.kind='run.question.created'
                    AND json_extract(e.detail_json,'$.question_id')=q.question_id
                JOIN connector_delivery_tasks d ON d.event_id=e.event_id
                WHERE q.instance_id=? AND q.agent_id=? AND q.state='pending'
                    AND d.connector_id='telegram' AND (d.state IN ('pending','retry')
                        OR (d.state='claimed' AND d.lease_expires_at<=?))
                ORDER BY q.created_at LIMIT ?''', (self.store.instance_id, str(agent_id).lower(),
                    datetime.now(timezone.utc).isoformat(), max(1, min(100, limit)))).fetchall()
            result = []
            for row in rows:
                refreshed = self._refresh(c, row)
                if refreshed['state'] == 'pending':
                    result.append({**self._public(refreshed), 'owner_id': row['owner_id'], 'event_id': row['event_id']})
            return result

    def _refresh(self, c, row):
        run = c.execute('''SELECT r.state,s.context_generation AS current_generation,s.status AS session_status
            FROM runs r JOIN sessions s ON s.session_id=r.session_id WHERE r.run_id=?''',
            (row['run_id'],)).fetchone()
        invalid = (not run or run['state'] != 'running' or run['session_status'] != 'active'
                   or run['current_generation'] != row['context_generation'])
        new_state = 'cancelled' if invalid else ('expired' if self.clock() >= row['expires_epoch'] else None)
        if new_state and row['state'] in ('pending','answered'):
            c.execute('UPDATE run_questions SET state=?,updated_at=? WHERE question_id=?',
                      (new_state,_stamp(self.clock()),row['question_id']))
            row = c.execute('SELECT * FROM run_questions WHERE question_id=?',(row['question_id'],)).fetchone()
            self._event(c,row,new_state)
        return row

    def create(self, *, request_id, agent_id, payload):
        question = self._text(payload.get('question'),2000,'question_text_invalid')
        key = self._text(payload.get('idempotency_key'),160,'question_idempotency_required')
        purpose = payload.get('purpose','clarification')
        if purpose not in ('clarification','preference'):
            raise QuestionError('question_purpose_invalid',400)
        free = payload.get('allow_free_text',True)
        options = payload.get('options',[])
        if not isinstance(free,bool) or not isinstance(options,list) or len(options)>8:
            raise QuestionError('question_options_invalid',400)
        seen, cleaned = set(), []
        for option in options:
            if not isinstance(option,dict) or set(option)-{'id','label','description'}:
                raise QuestionError('question_options_invalid',400)
            oid = self._text(option.get('id'),80,'question_option_invalid')
            if not re.fullmatch(r'[A-Za-z0-9_.:-]+',oid) or oid in seen:
                raise QuestionError('question_option_invalid',400)
            seen.add(oid)
            label=self._text(option.get('label'),160,'question_option_invalid')
            desc=option.get('description','')
            if not isinstance(desc,str) or len(desc)>400:
                raise QuestionError('question_option_invalid',400)
            cleaned.append({'id':oid,'label':label,'description':desc})
        if not cleaned and not free:
            raise QuestionError('question_options_invalid',400)
        ttl=payload.get('expires_seconds',900)
        if isinstance(ttl,bool) or not isinstance(ttl,int) or not 30<=ttl<=3600:
            raise QuestionError('question_expiry_invalid',400)
        data={'question':question,'options':cleaned,'allow_free_text':free,'purpose':purpose}
        digest=hashlib.sha256(_json({**data,'expires_seconds':ttl}).encode()).hexdigest()
        with self._transaction() as c:
            run=self._run(c,request_id,agent_id)
            session=self._session(c,run['session_id'])
            prior=c.execute('SELECT * FROM run_questions WHERE run_id=? AND create_key=?',
                            (run['run_id'],key)).fetchone()
            if prior:
                if prior['create_digest']!=digest:
                    raise QuestionError('question_idempotency_conflict')
                return self._public(self._refresh(c,prior))
            generation=session['context_generation']
            if run['state']!='running' or run['context_generation']!=generation or session['status']!='active':
                raise QuestionError('question_run_not_active')
            for old in c.execute("SELECT * FROM run_questions WHERE run_id=? AND state IN ('pending','answered')",(run['run_id'],)).fetchall():
                self._refresh(c,old)
            if c.execute("SELECT COUNT(*) FROM run_questions WHERE run_id=? AND state IN ('pending','answered')",(run['run_id'],)).fetchone()[0]>=8:
                raise QuestionError('question_limit_reached',429)
            now=self.clock(); qid='que_'+uuid4().hex
            c.execute('''INSERT INTO run_questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (qid,self.store.instance_id,session['owner_id'],run['agent_id'],run['session_id'],run['run_id'],
                       request_id,generation,key,digest,_json(data),'pending',None,None,
                       _stamp(now),now+ttl,_stamp(now)))
            row=c.execute('SELECT * FROM run_questions WHERE question_id=?',(qid,)).fetchone()
            self._event(c,row,'created')
            return self._public(row)

    def _scoped(self,c,question_id,*,session_id,owner_id):
        session=self._session(c,session_id,owner_id=owner_id)
        row=c.execute('SELECT * FROM run_questions WHERE question_id=? AND session_id=? AND owner_id=? AND instance_id=?',
                      (question_id,session_id,owner_id,self.store.instance_id)).fetchone()
        if not row or row['agent_id']!=session['agent_id']:
            raise QuestionError('question_not_found',404)
        row=self._refresh(c,row)
        if row['context_generation']!=session['context_generation']:
            raise QuestionError('question_not_found',404)
        return row

    def list(self,*,session_id,owner_id):
        with self._transaction() as c:
            session=self._session(c,session_id,owner_id=owner_id)
            rows=c.execute('''SELECT * FROM run_questions WHERE session_id=? AND owner_id=? AND instance_id=?
                AND context_generation=? ORDER BY created_at DESC LIMIT 50''',
                (session_id,owner_id,self.store.instance_id,session['context_generation'])).fetchall()
            return [self._public(self._refresh(c,r)) for r in reversed(rows)]

    def answer(self,*,session_id,owner_id,question_id,payload):
        key=self._text(payload.get('idempotency_key'),160,'question_idempotency_required')
        with self._transaction() as c:
            row=self._scoped(c,question_id,session_id=session_id,owner_id=owner_id)
            spec=json.loads(row['question_json'])
            option=payload.get('option_id'); text=payload.get('text','')
            if option is not None and (not isinstance(option,str) or option not in {o['id'] for o in spec['options']}):
                raise QuestionError('question_answer_invalid',400)
            if not isinstance(text,str) or len(text)>4000 or (text and not spec['allow_free_text']):
                raise QuestionError('question_answer_invalid',400)
            text=text.strip()
            if option is None and not text:
                raise QuestionError('question_answer_required',400)
            answer={'option_id':option,'text':text,'author': 'authenticated_user','grants_permissions':False}
            if row['state'] in ('answered','consumed'):
                if row['answer_key']==key and json.loads(row['answer_json'])==answer:
                    return self._public(row)
                raise QuestionError('question_already_answered')
            if row['state']!='pending':
                raise QuestionError('question_'+row['state'])
            c.execute("UPDATE run_questions SET state='answered',answer_json=?,answer_key=?,updated_at=? WHERE question_id=?",
                      (_json(answer),key,_stamp(self.clock()),question_id))
            row=c.execute('SELECT * FROM run_questions WHERE question_id=?',(question_id,)).fetchone()
            self._event(c,row,'answered')
            return self._public(row)

    def get_answer(self,*,request_id,agent_id,question_id):
        with self._transaction() as c:
            run=self._run(c,request_id,agent_id)
            session=self._session(c,run['session_id'])
            row=self._scoped(c,question_id,session_id=run['session_id'],owner_id=session['owner_id'])
            if row['run_id']!=run['run_id']:
                raise QuestionError('question_not_found',404)
            if run['state']!='running' or session['status']!='active' or run['context_generation']!=session['context_generation']:
                raise QuestionError('question_run_not_active')
            if row['state']=='answered':
                c.execute("UPDATE run_questions SET state='consumed',updated_at=? WHERE question_id=?",(_stamp(self.clock()),question_id))
                row=c.execute('SELECT * FROM run_questions WHERE question_id=?',(question_id,)).fetchone()
                self._event(c,row,'consumed')
            return self._public(row)
