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


class Space:
    def __init__(self, store):
        self.s = store
        with self.s.transaction():
            for sql in DDL:
                self.s.db.execute(sql)
            self.s.db.execute("INSERT OR IGNORE INTO acs_meta VALUES('space_version','1')")
            self.s.db.execute("INSERT OR IGNORE INTO acs_meta VALUES('revision','0')")

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
        r=self.s.db.execute('SELECT * FROM acs_jobs WHERE id=?',(jid,)).fetchone()
        if not r:raise ACError(4,'job_not_found')
        d=dict(r)
        for k in ('input_json','output_json','capabilities_json'):d[k.removesuffix('_json')]=json.loads(d.pop(k))
        d['depends_on']=[x[0] for x in self.s.db.execute('SELECT depends_on FROM acs_deps WHERE job_id=?',(jid,))]
        return d

    def claim(self,jid,agent,seconds):
        self.s.require_agent(agent)
        with self.s.transaction():
            j=self.s.db.execute('SELECT * FROM acs_jobs WHERE id=?',(jid,)).fetchone()
            if j is None:raise ACError(4,'job_not_found')
            if j['status']!='open':raise ACError(6,'job_not_open')
            token=uuid.uuid4().hex
            self.s.db.execute("UPDATE acs_jobs SET status='claimed',owner=?,claim_token=?,lease_until=?,updated_at=? WHERE id=? AND status='open'",
                (agent,token,utc(seconds),utc(),jid))
            self.s.event('space_job_claimed',agent,jid,lease_seconds=seconds)
        return token

    def submit(self,jid,agent,token,artifact=None):
        self.s.require_agent(agent)
        sha=None;artifact_ref=None
        if artifact:
            p=Path(artifact)
            if p.is_absolute() or '..' in p.parts:raise ACError(4,'artifact_must_be_relative')
            resolved=(self.s.path.parent / p).resolve()
            if not resolved.is_relative_to(self.s.path.parent.resolve()) or not resolved.is_file():raise ACError(4,'artifact_not_found_or_outside')
            sha=hashlib.sha256(resolved.read_bytes()).hexdigest();artifact_ref=str(p)
        with self.s.transaction():
            row=self.s.db.execute('SELECT * FROM acs_jobs WHERE id=?',(jid,)).fetchone()
            if not row or row['owner']!=agent or row['claim_token']!=token or row['status']!='claimed':raise ACError(6,'stale_or_foreign_claim')
            if row['lease_until'] <= utc():raise ACError(6,'claim_expired_requires_recovery')
            self.s.db.execute("UPDATE acs_jobs SET status='submitted',artifact=?,sha256=?,updated_at=? WHERE id=?",(artifact_ref,sha,utc(),jid))
            self.s.event('space_job_submitted',agent,jid,artifact=artifact_ref,sha256=sha)
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
                print('CLAIMED',args.id,space.claim(args.id,args.agent,args.lease))
            elif args.command=='submit':print('SUBMITTED',args.id,space.submit(args.id,args.agent,args.token,args.artifact))
            elif args.command=='accept':print('ACCEPTED',args.id,'OPENED',','.join(space.accept(args.id,args.agent)) or 'none')
            elif args.command=='discover':
                print(encoded({'space_version':1,'record_kinds':KINDS,'authorities':AUTHORITIES,'tables':[r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'acs_%' AND type='table' ORDER BY name")], 'protocol':['record','context','post','board','wait-open','job','claim','submit','accept']}))
        finally:store.close()
        return 0
    except KeyboardInterrupt:
        print('STOPPED');return 3
    except (ACError,sqlite3.Error,ValueError,OSError,KeyError,TypeError,json.JSONDecodeError) as e:
        print('ERROR',getattr(e,'reason',str(e)));return getattr(e,'code',4)

if __name__=='__main__':sys.exit(cli())
