#!/usr/bin/env python3
"""Optional thin project layer for Agent Chat: shared memory, task board and catalogs.

Uses the same SQLite file as ac.py but owns only acs_* tables. No agent runtime,
no orchestration, no model or external service calls. Python standard library only.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

from ac import Store, ACError, encoded, utc

DDL = (
    "CREATE TABLE IF NOT EXISTS acs_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS acs_records(
       revision INTEGER PRIMARY KEY,kind TEXT NOT NULL,key TEXT NOT NULL,scope TEXT NOT NULL,
       value_json TEXT NOT NULL,authority TEXT NOT NULL,source_agent TEXT NOT NULL,
       evidence TEXT,created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS acs_record_lookup ON acs_records(kind,key,scope,revision DESC)",
    """CREATE TABLE IF NOT EXISTS acs_jobs(
       id TEXT PRIMARY KEY,title TEXT NOT NULL,requester TEXT NOT NULL,objective TEXT NOT NULL,
       input_json TEXT NOT NULL,output_json TEXT NOT NULL,capabilities_json TEXT NOT NULL,
       parent_id TEXT REFERENCES acs_jobs(id),status TEXT NOT NULL,
       owner TEXT,claim_token TEXT,lease_until TEXT,artifact TEXT,sha256 TEXT,
       created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS acs_deps(
       job_id TEXT NOT NULL REFERENCES acs_jobs(id),depends_on TEXT NOT NULL REFERENCES acs_jobs(id),
       PRIMARY KEY(job_id,depends_on),CHECK(job_id<>depends_on))""",
    "CREATE INDEX IF NOT EXISTS acs_jobs_state ON acs_jobs(status,id)",
)
KINDS = ('goal','setting','role','decision','fact','resource','skill','tool','schema','note')
AUTHORITIES = ('agent_proposal','reference','verified','user_confirmed')


def load_json(path):
    raw = sys.stdin.buffer.read().decode('utf-8-sig') if path == '-' else Path(path).read_text(encoding='utf-8-sig')
    return json.loads(raw)


def require_id(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,95}', value):
        raise ACError(4,'invalid_identifier')
    return value


def execute_sql_script(db, script):
    statement = ''
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                db.execute(statement)
            statement = ''
    if statement.strip():
        raise ACError(4, 'incomplete_space_migration')


class Space:
    def __init__(self, store):
        self.s = store
        with self.s.transaction():
            for sql in DDL:
                self.s.db.execute(sql)
            self.s.db.execute("INSERT OR IGNORE INTO acs_meta VALUES('space_version','1')")
            self.s.db.execute("INSERT OR IGNORE INTO acs_meta VALUES('revision','0')")
        version = int(self.s.db.execute("SELECT value FROM acs_meta WHERE key='space_version'").fetchone()[0])
        if version == 1:
            migration = Path(__file__).resolve().parent / 'migrations' / '003_job_execution_lifecycle.sql'
            with self.s.transaction():
                current = int(self.s.db.execute("SELECT value FROM acs_meta WHERE key='space_version'").fetchone()[0])
                if current == 1:
                    execute_sql_script(self.s.db, migration.read_text(encoding='utf-8'))
                    self.s.db.execute("UPDATE acs_meta SET value='2' WHERE key='space_version'")
                    self.s.event('space_schema_migrated', 'agent-chat', from_version=1, to_version=2)
                elif current != 2:
                    raise ACError(4, 'unsupported_space_schema_version')
        elif version != 2:
            raise ACError(4, 'unsupported_space_schema_version')

    def _get_job(self, jid):
        row = self.s.db.execute('SELECT * FROM acs_jobs WHERE id=?', (jid,)).fetchone()
        if row is None:
            raise ACError(4, 'job_not_found')
        return row

    def _active_attempt(self, jid):
        return self.s.db.execute(
            """SELECT * FROM acs_job_attempts WHERE job_id=?
               AND status IN ('claimed','starting','running','cancel_requested','recovery_required')
               ORDER BY attempt_no DESC LIMIT 1""", (jid,)
        ).fetchone()

    def _attempt_for_claim(self, row, agent, token):
        if row['status'] != 'claimed' or row['owner'] != agent or row['claim_token'] != token:
            raise ACError(6, 'stale_or_foreign_claim')
        attempt = self._active_attempt(row['id'])
        if attempt is None:
            # Adopt claims created by the v0.1 task board before execution tracking existed.
            attempt_no = self.s.db.execute(
                'SELECT COALESCE(MAX(attempt_no),0)+1 FROM acs_job_attempts WHERE job_id=?',
                (row['id'],)
            ).fetchone()[0]
            attempt_id = uuid.uuid4().hex
            lease_until = row['lease_until'] or utc()
            self.s.db.execute(
                """INSERT INTO acs_job_attempts(
                   attempt_id,job_id,attempt_no,agent_id,claim_token_hash,status,
                   executor_id,runtime,created_at,updated_at,heartbeat_at,lease_until,details_json
                   ) VALUES(?,?,?,?,?,'claimed','legacy','legacy',?,?,?,?,?)""",
                (attempt_id, row['id'], attempt_no, agent,
                 hashlib.sha256(token.encode('utf-8')).hexdigest(),
                 row['updated_at'], utc(), utc(), lease_until, encoded({'adopted_from':'space_v1'}))
            )
            self.s.event('space_attempt_adopted', agent, row['id'], attempt_id=attempt_id, legacy=True)
            attempt = self._active_attempt(row['id'])
        token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
        if attempt is None or attempt['agent_id'] != agent or attempt['claim_token_hash'] != token_hash:
            raise ACError(6, 'stale_or_foreign_attempt')
        return attempt

    def _artifact(self, artifact):
        if not artifact:
            return None, None
        path = Path(artifact)
        if path.is_absolute() or '..' in path.parts:
            raise ACError(4, 'artifact_must_be_relative')
        base = self.s.path.parent.resolve()
        resolved = (base / path).resolve()
        if not resolved.is_relative_to(base) or not resolved.is_file():
            raise ACError(4, 'artifact_not_found_or_outside')
        return str(path), hashlib.sha256(resolved.read_bytes()).hexdigest()

    def next_rev(self):
        self.s.db.execute("UPDATE acs_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        return int(self.s.db.execute("SELECT value FROM acs_meta WHERE key='revision'").fetchone()[0])

    def record(self, kind, key, scope, value, agent, authority, evidence):
        if kind not in KINDS or authority not in AUTHORITIES: raise ACError(4,'invalid_kind_or_authority')
        if not isinstance(value,(dict,list,str,int,float,bool)) and value is not None: raise ACError(4,'invalid_value')
        require_id(key);require_id(scope)
        self.s.require_agent(agent)
        # A human-confirmed record needs an explicit source reference, not merely agent confidence.
        if authority == 'user_confirmed' and not evidence:
            raise ACError(4,'user_confirmed_requires_source_evidence')
        with self.s.transaction():
            rev=self.next_rev()
            self.s.db.execute("INSERT INTO acs_records VALUES(?,?,?,?,?,?,?,?,?)",
                (rev,kind,key,scope,encoded(value),authority,agent,evidence,utc()))
            self.s.event('space_recorded',agent,revision=rev,record_kind=kind,record_key=key,scope=scope,authority=authority,evidence=evidence)
        return rev

    def context(self, since=None, kind=None, limit=50):
        # User-confirmed decisions are never shadowed by an agent's later proposal.
        # Changes/since is an event feed; current context is an effective projection.
        if since is not None:
            sql = 'SELECT * FROM acs_records WHERE revision>? '
            params = [since]
            if kind:
                sql += 'AND kind=? '
                params.append(kind)
            rows = self.s.db.execute(sql+'ORDER BY revision LIMIT ?', (*params,limit)).fetchall()
        else:
            rank = lambda a: ("CASE " + a + ".authority WHEN 'user_confirmed' THEN 4 "
                  "WHEN 'verified' THEN 3 WHEN 'reference' THEN 2 ELSE 1 END")
            sql = f"""SELECT r.* FROM acs_records r WHERE NOT EXISTS(
                SELECT 1 FROM acs_records n WHERE n.kind=r.kind AND n.key=r.key
                AND n.scope=r.scope AND (({rank('n')})>({rank('r')}) OR
                (({rank('n')})=({rank('r')}) AND n.revision>r.revision)))"""
            params=[]
            if kind:
                sql += ' AND r.kind=?'
                params.append(kind)
            rows=self.s.db.execute(sql+' ORDER BY r.revision DESC LIMIT ?', (*params,limit)).fetchall()
        result=[]
        for row in rows:
            d=dict(row);d['value']=json.loads(d.pop('value_json'));result.append(d)
        return result

    def job_post(self, spec):
        if not isinstance(spec,dict):raise ACError(4,'job_spec_must_be_object')
        allowed={'id','title','requester','objective','input','output','capabilities','parent_id','depends_on'}
        if set(spec)-allowed:raise ACError(4,'unknown_job_fields')
        jobid=require_id(spec['id']);requester=spec['requester'];self.s.require_agent(requester)
        parent=spec.get('parent_id'); deps=spec.get('depends_on',[])
        if not isinstance(deps,list) or len(deps)!=len(set(deps)) or jobid in deps:raise ACError(4,'bad_dependencies')
        if not isinstance(spec.get('capabilities',[]),list):raise ACError(4,'bad_capabilities')
        if not isinstance(spec.get('input',{}),dict) or not isinstance(spec.get('output',{}),dict):raise ACError(4,'contracts_must_be_objects')
        if not str(spec.get('title','')).strip():raise ACError(4,'title_required')
        with self.s.transaction():
            if self.s.db.execute('SELECT 1 FROM acs_jobs WHERE id=?',(jobid,)).fetchone():raise ACError(4,'job_already_exists')
            for dep in deps+([parent] if parent else []):
                if not self.s.db.execute('SELECT 1 FROM acs_jobs WHERE id=?',(dep,)).fetchone():raise ACError(4,'unknown_parent_or_dependency')
            blocked=any(self.s.db.execute('SELECT status FROM acs_jobs WHERE id=?',(dep,)).fetchone()[0]!='done' for dep in deps)
            state='blocked' if blocked else 'open'
            self.s.db.execute('''INSERT INTO acs_jobs(id,title,requester,objective,input_json,output_json,capabilities_json,parent_id,status,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(jobid,spec['title'],requester,spec.get('objective',''),encoded(spec.get('input',{})),encoded(spec.get('output',{})),encoded(spec.get('capabilities',[])),parent,state,utc(),utc()))
            for dep in deps:self.s.db.execute('INSERT INTO acs_deps VALUES(?,?)',(jobid,dep))
            self.s.event('space_job_posted',requester,jobid,status=state,depends_on=deps,parent_id=parent)
        return state

    def board(self, state=None):
        rows=self.s.db.execute('SELECT id,title,status,requester,owner,capabilities_json,parent_id FROM acs_jobs '+('WHERE status=? ' if state else '')+'ORDER BY id', (state,) if state else ()).fetchall()
        return [dict(id=r['id'],title=r['title'],status=r['status'],requester=r['requester'],owner=r['owner'],capabilities=json.loads(r['capabilities_json']),parent_id=r['parent_id']) for r in rows]

    def open_for(self, agent):
        self.s.require_agent(agent)
        profile=self.s.db.execute('SELECT capabilities FROM agents WHERE agent_id=?',(agent,)).fetchone()
        caps=set(json.loads(profile[0]))
        rows=self.s.db.execute("SELECT id,capabilities_json FROM acs_jobs WHERE status='open' ORDER BY id").fetchall()
        for row in rows:
            if set(json.loads(row['capabilities_json'])).issubset(caps):
                return row['id']
        return None

    def job(self, jid):
        r=self._get_job(jid)
        d=dict(r)
        for k in ('input_json','output_json','capabilities_json'):
            d[k.removesuffix('_json')]=json.loads(d.pop(k))
        d['depends_on']=[x[0] for x in self.s.db.execute('SELECT depends_on FROM acs_deps WHERE job_id=?',(jid,))]
        attempts=[]
        for row in self.s.db.execute('SELECT * FROM acs_job_attempts WHERE job_id=? ORDER BY attempt_no',(jid,)):
            item=dict(row)
            item.pop('claim_token_hash',None)
            item['details']=json.loads(item.pop('details_json'))
            attempts.append(item)
        active=next((x for x in reversed(attempts) if x['status'] in
                     ('claimed','starting','running','cancel_requested','recovery_required')),None)
        d['attempts']=attempts
        d['active_attempt']=active
        d['lease_expired']=bool(active and active.get('lease_until') and active['lease_until']<=utc())
        return d

    def recover(self,jid,actor,action,seconds=3600,confirmed_stopped=False):
        self.s.require_agent(actor)
        if action not in ('mark','resume','requeue','cancel'):
            raise ACError(4,'invalid_recovery_action')
        if seconds < 1 or seconds > 86400:
            raise ACError(4,'invalid_lease')
        if action in ('requeue','cancel') and not confirmed_stopped:
            raise ACError(4,'recovery_requires_confirmed_stopped')
        with self.s.transaction():
            row=self._get_job(jid)
            if row['status']!='claimed':
                raise ACError(6,'job_not_claimed')
            if actor not in (row['requester'],row['owner']):
                raise ACError(6,'recovery_requires_requester_or_owner')
            attempt=self._attempt_for_claim(row,row['owner'],row['claim_token'])
            if attempt['status'] not in ('claimed','starting','running','cancel_requested','recovery_required'):
                raise ACError(6,'attempt_not_recoverable')
            lease=attempt['lease_until'] or row['lease_until']
            if not lease or lease>utc():
                raise ACError(6,'lease_not_expired')
            now=utc()
            if attempt['status']!='recovery_required':
                self.s.db.execute(
                    "UPDATE acs_job_attempts SET status='recovery_required',updated_at=? WHERE attempt_id=?",
                    (now,attempt['attempt_id'])
                )
                self.s.event('space_attempt_recovery_required',actor,jid,attempt_id=attempt['attempt_id'],
                             expired_lease=lease)
            if action=='mark':
                return 'recovery_required'
            if action=='resume':
                if attempt['status']=='cancel_requested':
                    raise ACError(6,'cancel_requested_attempt_cannot_resume')
                new_lease=utc(seconds)
                self.s.db.execute(
                    """UPDATE acs_job_attempts SET status='running',started_at=COALESCE(started_at,?),
                       heartbeat_at=?,lease_until=?,updated_at=? WHERE attempt_id=?""",
                    (now,now,new_lease,now,attempt['attempt_id'])
                )
                self.s.db.execute('UPDATE acs_jobs SET lease_until=?,updated_at=? WHERE id=?',(new_lease,now,jid))
                self.s.event('space_attempt_recovered',actor,jid,attempt_id=attempt['attempt_id'],
                             action='resume',lease_seconds=seconds)
                return 'running'
            final_state='interrupted' if action=='requeue' else 'cancelled'
            self.s.db.execute(
                "UPDATE acs_job_attempts SET status=?,finished_at=?,updated_at=?,error=? WHERE attempt_id=?",
                (final_state,now,now,'recovered_after_expired_lease',attempt['attempt_id'])
            )
            job_state='open' if action=='requeue' else 'cancelled'
            self.s.db.execute(
                "UPDATE acs_jobs SET status=?,owner=NULL,claim_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (job_state,now,jid)
            )
            self.s.event('space_attempt_recovered',actor,jid,attempt_id=attempt['attempt_id'],
                         action=action,confirmed_stopped=True)
        return job_state

    def claim(self,jid,agent,seconds):
        self.s.require_agent(agent)
        if seconds < 1 or seconds > 86400:
            raise ACError(4, 'invalid_lease')
        with self.s.transaction():
            j=self._get_job(jid)
            if j['status']!='open':
                raise ACError(6,'job_not_open')
            token=uuid.uuid4().hex
            now=utc()
            lease_until=utc(seconds)
            self.s.db.execute(
                "UPDATE acs_jobs SET status='claimed',owner=?,claim_token=?,lease_until=?,updated_at=? WHERE id=? AND status='open'",
                (agent,token,lease_until,now,jid)
            )
            attempt_no=self.s.db.execute(
                'SELECT COALESCE(MAX(attempt_no),0)+1 FROM acs_job_attempts WHERE job_id=?',(jid,)
            ).fetchone()[0]
            attempt_id=uuid.uuid4().hex
            self.s.db.execute(
                """INSERT INTO acs_job_attempts(
                   attempt_id,job_id,attempt_no,agent_id,claim_token_hash,status,
                   created_at,updated_at,heartbeat_at,lease_until,details_json
                   ) VALUES(?,?,?,?,?,'claimed',?,?,?,?,?)""",
                (attempt_id,jid,attempt_no,agent,hashlib.sha256(token.encode('utf-8')).hexdigest(),
                 now,now,now,lease_until,encoded({}))
            )
            self.s.event('space_job_claimed',agent,jid,attempt_id=attempt_id,attempt_no=attempt_no,lease_seconds=seconds)
        return token

    def start(self,jid,agent,token,executor_id='native',runtime='native',session_id=None,pid=None):
        self.s.require_agent(agent)
        if not executor_id.strip() or not runtime.strip():
            raise ACError(4,'executor_and_runtime_required')
        if pid is not None and pid < 1:
            raise ACError(4,'invalid_pid')
        with self.s.transaction():
            row=self._get_job(jid)
            attempt=self._attempt_for_claim(row,agent,token)
            if attempt['lease_until'] and attempt['lease_until'] <= utc():
                raise ACError(6,'lease_expired_requires_recovery')
            if attempt['status']=='starting' and attempt['executor_id']==executor_id and attempt['runtime']==runtime:
                return attempt['attempt_id']
            if attempt['status']!='claimed':
                raise ACError(6,'attempt_not_claimed')
            now=utc()
            self.s.db.execute(
                """UPDATE acs_job_attempts SET status='starting',executor_id=?,runtime=?,
                   session_id=?,pid=?,heartbeat_at=?,updated_at=? WHERE attempt_id=?""",
                (executor_id,runtime,session_id,pid,now,now,attempt['attempt_id'])
            )
            self.s.event('space_attempt_starting',agent,jid,attempt_id=attempt['attempt_id'],
                         executor_id=executor_id,runtime=runtime,session_id=session_id,pid=pid)
        return attempt['attempt_id']

    def running(self,jid,agent,token,seconds):
        self.s.require_agent(agent)
        if seconds < 1 or seconds > 86400:
            raise ACError(4,'invalid_lease')
        with self.s.transaction():
            row=self._get_job(jid)
            attempt=self._attempt_for_claim(row,agent,token)
            if attempt['status'] not in ('starting','running'):
                raise ACError(6,'attempt_not_starting')
            if attempt['lease_until'] and attempt['lease_until'] <= utc():
                raise ACError(6,'lease_expired_requires_recovery')
            now=utc()
            lease_until=utc(seconds)
            self.s.db.execute(
                """UPDATE acs_job_attempts SET status='running',started_at=COALESCE(started_at,?),
                   heartbeat_at=?,lease_until=?,updated_at=? WHERE attempt_id=?""",
                (now,now,lease_until,now,attempt['attempt_id'])
            )
            self.s.db.execute('UPDATE acs_jobs SET lease_until=?,updated_at=? WHERE id=?',(lease_until,now,jid))
            self.s.event('space_attempt_running',agent,jid,attempt_id=attempt['attempt_id'],lease_seconds=seconds)
        return attempt['attempt_id']

    def heartbeat(self,jid,agent,token,seconds):
        self.s.require_agent(agent)
        if seconds < 1 or seconds > 86400:
            raise ACError(4,'invalid_lease')
        with self.s.transaction():
            row=self._get_job(jid)
            attempt=self._attempt_for_claim(row,agent,token)
            if attempt['status'] not in ('starting','running','cancel_requested'):
                raise ACError(6,'attempt_not_active')
            if attempt['lease_until'] and attempt['lease_until'] <= utc():
                raise ACError(6,'lease_expired_requires_recovery')
            now=utc()
            lease_until=utc(seconds)
            self.s.db.execute(
                'UPDATE acs_job_attempts SET heartbeat_at=?,lease_until=?,updated_at=? WHERE attempt_id=?',
                (now,lease_until,now,attempt['attempt_id'])
            )
            self.s.db.execute('UPDATE acs_jobs SET lease_until=?,updated_at=? WHERE id=?',(lease_until,now,jid))
            self.s.event('space_attempt_heartbeat',agent,jid,attempt_id=attempt['attempt_id'],lease_seconds=seconds)
        return lease_until

    def cancel(self,jid,actor):
        self.s.require_agent(actor)
        with self.s.transaction():
            row=self._get_job(jid)
            if row['status']=='cancelled':
                return 'cancelled'
            if row['status'] in ('open','blocked'):
                if actor!=row['requester']:
                    raise ACError(6,'cancel_requires_requester')
                self.s.db.execute("UPDATE acs_jobs SET status='cancelled',updated_at=? WHERE id=?",(utc(),jid))
                self.s.event('space_job_cancelled',actor,jid,immediate=True)
                return 'cancelled'
            if row['status']!='claimed':
                raise ACError(6,'job_not_cancellable')
            if actor not in (row['requester'],row['owner']):
                raise ACError(6,'cancel_requires_requester_or_owner')
            attempt=self._attempt_for_claim(row,row['owner'],row['claim_token'])
            if attempt['status']=='cancel_requested':
                return 'cancel_requested'
            if attempt['status'] not in ('claimed','starting','running','recovery_required'):
                raise ACError(6,'attempt_not_active')
            now=utc()
            self.s.db.execute(
                "UPDATE acs_job_attempts SET status='cancel_requested',updated_at=? WHERE attempt_id=?",
                (now,attempt['attempt_id'])
            )
            self.s.event('space_job_cancel_requested',actor,jid,attempt_id=attempt['attempt_id'],owner=row['owner'])
        return 'cancel_requested'

    def finish(self,jid,agent,token,result,artifact=None,exit_code=None,error=None):
        self.s.require_agent(agent)
        if result not in ('succeeded','failed','interrupted','start_failed','cancelled'):
            raise ACError(4,'invalid_attempt_result')
        artifact_ref,sha=self._artifact(artifact)
        with self.s.transaction():
            row=self._get_job(jid)
            attempt=self._attempt_for_claim(row,agent,token)
            state=attempt['status']
            if state=='cancel_requested' and result!='cancelled':
                raise ACError(6,'cancel_requested_attempt_must_finish_cancelled')
            if result=='succeeded' and state not in ('starting','running'):
                raise ACError(6,'attempt_cannot_succeed_from_state')
            if result=='start_failed' and state not in ('claimed','starting'):
                raise ACError(6,'attempt_cannot_start_fail_from_state')
            if result=='cancelled' and state!='cancel_requested':
                raise ACError(6,'cancellation_was_not_requested')
            if result in ('failed','interrupted') and state not in ('starting','running','cancel_requested'):
                raise ACError(6,'attempt_not_active')
            now=utc()
            self.s.db.execute(
                """UPDATE acs_job_attempts SET status=?,finished_at=?,updated_at=?,exit_code=?,
                   artifact=?,sha256=?,error=? WHERE attempt_id=?""",
                (result,now,now,exit_code,artifact_ref,sha,error,attempt['attempt_id'])
            )
            if result=='succeeded':
                self.s.db.execute(
                    """UPDATE acs_jobs SET status='submitted',artifact=?,sha256=?,claim_token=NULL,
                       lease_until=NULL,updated_at=? WHERE id=?""",(artifact_ref,sha,now,jid)
                )
            elif result=='cancelled':
                self.s.db.execute(
                    """UPDATE acs_jobs SET status='cancelled',owner=NULL,claim_token=NULL,
                       lease_until=NULL,updated_at=? WHERE id=?""",(now,jid)
                )
            else:
                # A confirmed terminal failure releases the job for another voluntary claimant.
                self.s.db.execute(
                    """UPDATE acs_jobs SET status='open',owner=NULL,claim_token=NULL,
                       lease_until=NULL,updated_at=? WHERE id=?""",(now,jid)
                )
            self.s.event('space_attempt_finished',agent,jid,attempt_id=attempt['attempt_id'],
                         result=result,exit_code=exit_code,artifact=artifact_ref,sha256=sha,error=error)
            if result=='succeeded':
                self.s.event('space_job_submitted',agent,jid,attempt_id=attempt['attempt_id'],
                             artifact=artifact_ref,sha256=sha)
        return 'submitted' if result=='succeeded' else ('cancelled' if result=='cancelled' else 'open')

    def submit(self,jid,agent,token,artifact=None):
        # Backward-compatible shorthand: claim -> submit is treated as a successful
        # one-shot execution. New integrations should use start/running/finish.
        self.s.require_agent(agent)
        artifact_ref,sha=self._artifact(artifact)
        with self.s.transaction():
            row=self._get_job(jid)
            attempt=self._attempt_for_claim(row,agent,token)
            if row['lease_until'] <= utc():
                raise ACError(6,'claim_expired_requires_recovery')
            if attempt['status'] not in ('claimed','starting','running'):
                raise ACError(6,'attempt_not_submittable')
            now=utc()
            self.s.db.execute(
                """UPDATE acs_job_attempts SET status='succeeded',executor_id=COALESCE(executor_id,'legacy'),
                   runtime=COALESCE(runtime,'legacy'),started_at=COALESCE(started_at,?),
                   finished_at=?,heartbeat_at=?,updated_at=?,artifact=?,sha256=? WHERE attempt_id=?""",
                (now,now,now,now,artifact_ref,sha,attempt['attempt_id'])
            )
            self.s.db.execute(
                """UPDATE acs_jobs SET status='submitted',artifact=?,sha256=?,claim_token=NULL,
                   lease_until=NULL,updated_at=? WHERE id=?""",(artifact_ref,sha,now,jid)
            )
            self.s.event('space_attempt_finished',agent,jid,attempt_id=attempt['attempt_id'],
                         result='succeeded',compat_submit=True,artifact=artifact_ref,sha256=sha)
            self.s.event('space_job_submitted',agent,jid,attempt_id=attempt['attempt_id'],
                         artifact=artifact_ref,sha256=sha)
        return 'submitted'

    def accept(self,jid,actor):
        self.s.require_agent(actor)
        with self.s.transaction():
            row=self.s.db.execute('SELECT * FROM acs_jobs WHERE id=?',(jid,)).fetchone()
            if not row or row['status']!='submitted':raise ACError(4,'job_not_submitted')
            # V0: requester decides acceptance; user should communicate via its registered project participant.
            if actor!=row['requester']:raise ACError(4,'acceptance_requires_requester')
            self.s.db.execute("UPDATE acs_jobs SET status='done',updated_at=? WHERE id=?",(utc(),jid))
            # Unlock DAG successors without AI scheduling.
            candidates=[x[0] for x in self.s.db.execute("SELECT job_id FROM acs_deps WHERE depends_on=?",(jid,))]
            opened=[]
            for child in candidates:
                blocked=self.s.db.execute('''SELECT count(*) FROM acs_deps d JOIN acs_jobs p ON p.id=d.depends_on
                      WHERE d.job_id=? AND p.status<>'done' ''',(child,)).fetchone()[0]
                if blocked==0:
                    result=self.s.db.execute("UPDATE acs_jobs SET status='open',updated_at=? WHERE id=? AND status='blocked'",(utc(),child))
                    if result.rowcount:opened.append(child)
            self.s.event('space_job_accepted',actor,jid,opened=opened)
        return opened

def cli():
    p=argparse.ArgumentParser(description='Agent Chat project memory and voluntary task board; no orchestration.')
    p.add_argument('--db',default=str(Path(__file__).resolve().with_name('ac.db')))
    s=p.add_subparsers(dest='command',required=True)
    rec=s.add_parser('record');rec.add_argument('--kind',choices=KINDS,required=True);rec.add_argument('--key',required=True)
    rec.add_argument('--scope',default='project');rec.add_argument('--file',required=True);rec.add_argument('--agent',required=True)
    rec.add_argument('--authority',choices=AUTHORITIES,default='agent_proposal');rec.add_argument('--evidence')
    ctx=s.add_parser('context');ctx.add_argument('--kind',choices=KINDS);ctx.add_argument('--since',type=int);ctx.add_argument('--limit',type=int,default=50)
    post=s.add_parser('post');post.add_argument('--file',required=True)
    board=s.add_parser('board');board.add_argument('--status',choices=('open','blocked','claimed','submitted','done','failed','cancelled'))
    job=s.add_parser('job');job.add_argument('id')
    w=s.add_parser('wait-open');w.add_argument('--agent',required=True);w.add_argument('--timeout',type=float,default=180);w.add_argument('--interval',type=float,default=0.5)
    claim=s.add_parser('claim');claim.add_argument('id');claim.add_argument('--agent',required=True);claim.add_argument('--lease',type=int,default=3600)
    submit=s.add_parser('submit');submit.add_argument('id');submit.add_argument('--agent',required=True);submit.add_argument('--token',required=True);submit.add_argument('--artifact')
    accept=s.add_parser('accept');accept.add_argument('id');accept.add_argument('--agent',required=True)
    start=s.add_parser('start');start.add_argument('id');start.add_argument('--agent',required=True);start.add_argument('--token',required=True)
    start.add_argument('--executor',default='native');start.add_argument('--runtime',default='native');start.add_argument('--session');start.add_argument('--pid',type=int)
    running=s.add_parser('running');running.add_argument('id');running.add_argument('--agent',required=True);running.add_argument('--token',required=True);running.add_argument('--lease',type=int,default=3600)
    hb=s.add_parser('heartbeat');hb.add_argument('id');hb.add_argument('--agent',required=True);hb.add_argument('--token',required=True);hb.add_argument('--lease',type=int,default=3600)
    cancel=s.add_parser('cancel');cancel.add_argument('id');cancel.add_argument('--agent',required=True)
    finish=s.add_parser('finish');finish.add_argument('id');finish.add_argument('--agent',required=True);finish.add_argument('--token',required=True)
    finish.add_argument('--result',choices=('succeeded','failed','interrupted','start_failed','cancelled'),required=True)
    finish.add_argument('--artifact');finish.add_argument('--exit-code',type=int);finish.add_argument('--error')
    recover=s.add_parser('recover');recover.add_argument('id');recover.add_argument('--agent',required=True)
    recover.add_argument('--action',choices=('mark','resume','requeue','cancel'),required=True);recover.add_argument('--lease',type=int,default=3600)
    recover.add_argument('--confirmed-stopped',action='store_true')
    s.add_parser('discover')
    args=p.parse_args()
    try:
        store=Store(args.db)
        try:
            space=Space(store)
            if args.command=='record':print('REV',space.record(args.kind,args.key,args.scope,load_json(args.file),args.agent,args.authority,args.evidence))
            elif args.command=='context':
                if args.limit<1 or args.limit>500:raise ACError(4,'invalid_limit')
                print(encoded(space.context(args.since,args.kind,args.limit)))
            elif args.command=='post':
                spec=load_json(args.file);print('POSTED',spec.get('id','?'),space.job_post(spec))
            elif args.command=='board':print(encoded(space.board(args.status)))
            elif args.command=='job':print(encoded(space.job(args.id)))
            elif args.command=='wait-open':
                if not (0 < args.timeout <= 86400 and 0.1 <= args.interval <= 60):raise ACError(4,'invalid_wait_bounds')
                deadline=time.monotonic()+args.timeout
                while True:
                    if (store.path.parent/'STOP').exists():print('STOPPED');return 3
                    available=space.open_for(args.agent)
                    if available:print('AVAILABLE',available);return 0
                    if time.monotonic() >= deadline:print('IDLE');return 2
                    time.sleep(min(args.interval,max(0,deadline-time.monotonic())))
            elif args.command=='claim':
                if args.lease<1 or args.lease>86400:raise ACError(4,'invalid_lease')
                token=space.claim(args.id,args.agent,args.lease)
                attempt=space.job(args.id)['active_attempt']
                print('CLAIMED',args.id,token,'ATTEMPT',attempt['attempt_id'])
            elif args.command=='submit':print('SUBMITTED',args.id,space.submit(args.id,args.agent,args.token,args.artifact))
            elif args.command=='accept':print('ACCEPTED',args.id,'OPENED',','.join(space.accept(args.id,args.agent)) or 'none')
            elif args.command=='start':print('STARTING',args.id,space.start(args.id,args.agent,args.token,args.executor,args.runtime,args.session,args.pid))
            elif args.command=='running':print('RUNNING',args.id,space.running(args.id,args.agent,args.token,args.lease))
            elif args.command=='heartbeat':print('HEARTBEAT',args.id,space.heartbeat(args.id,args.agent,args.token,args.lease))
            elif args.command=='cancel':print('CANCEL',args.id,space.cancel(args.id,args.agent))
            elif args.command=='finish':print('FINISHED',args.id,space.finish(args.id,args.agent,args.token,args.result,args.artifact,args.exit_code,args.error))
            elif args.command=='recover':print('RECOVERED',args.id,args.action,space.recover(args.id,args.agent,args.action,args.lease,args.confirmed_stopped))
            elif args.command=='discover':
                print(encoded({'space_version':2,'record_kinds':KINDS,'authorities':AUTHORITIES,
                    'execution_states':['claimed','starting','running','cancel_requested','recovery_required','succeeded','failed','interrupted','start_failed','cancelled'],
                    'tables':[r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'acs_%' AND type='table' ORDER BY name")],
                    'protocol':['record','context','post','board','wait-open','job','claim','start','running','heartbeat','cancel','finish','recover','submit','accept']}))
        finally:store.close()
        return 0
    except KeyboardInterrupt:
        print('STOPPED');return 3
    except (ACError,sqlite3.Error,ValueError,OSError,KeyError,TypeError,json.JSONDecodeError) as e:
        print('ERROR',getattr(e,'reason',str(e)));return getattr(e,'code',4)

if __name__=='__main__':sys.exit(cli())
