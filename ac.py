#!/usr/bin/env python3
"""Agent Chat 0.1: local transport and durable receipts, never a model host."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

VERSION = '0.1.2'
AGENTS_SQL = """CREATE TABLE agents(agent_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,
       provider TEXT NOT NULL,capabilities TEXT NOT NULL,wake_mode TEXT NOT NULL,
       wake_level TEXT NOT NULL,status TEXT NOT NULL,last_seen_at TEXT NOT NULL,
       registered_at TEXT NOT NULL,metadata TEXT NOT NULL)"""
SCHEMA = (
    """CREATE TABLE tasks(task_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,
       assigned_agent TEXT NOT NULL,task_type TEXT NOT NULL,status TEXT NOT NULL,
       current_step TEXT NOT NULL,artifact_ref TEXT,retry_count INTEGER NOT NULL DEFAULT 0,
       created_at TEXT NOT NULL,updated_at TEXT NOT NULL,lease_until TEXT,claim_token TEXT,
       claimed_by TEXT,lease_message_id INTEGER,recovery_required INTEGER NOT NULL DEFAULT 0,
       recovery_count INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,sender TEXT NOT NULL,
       receiver TEXT NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL,
       status TEXT NOT NULL CHECK(status IN ('queued','processing','done','failed')),
       correlation_id TEXT,idempotency_key TEXT NOT NULL,content_hash TEXT NOT NULL,
       created_at TEXT NOT NULL,claimed_at TEXT,finished_at TEXT,lease_until TEXT,
       claim_token TEXT,delivery_count INTEGER NOT NULL DEFAULT 0,
       task_id TEXT REFERENCES tasks(task_id),UNIQUE(sender,idempotency_key))""",
    """CREATE TABLE events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,
       agent_id TEXT NOT NULL,event_type TEXT NOT NULL,event_time TEXT NOT NULL,
       details TEXT NOT NULL,message_id INTEGER REFERENCES messages(id))""",
    'CREATE INDEX inbox ON messages(receiver,status,id)',
    'CREATE INDEX event_message ON events(message_id,event_id)',
    AGENTS_SQL,
    'CREATE INDEX task_leases ON tasks(status,lease_until)',
    "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END",
    "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END",
)


class ACError(Exception):
    def __init__(self, code, reason):
        self.code, self.reason = code, reason
        super().__init__(reason)


def utc(after=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=after)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def wal_fixed(version):
    return version >= (3, 51, 3) or (version[:2] == (3, 50) and version[2] >= 7) or (version[:2] == (3, 44) and version[2] >= 6)


class Store:
    def __init__(self, path, lock_timeout=5):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=lock_timeout, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=' + str(max(1, int(lock_timeout * 1000))))
        # Use WAL only on SQLite builds with the relevant WAL fix.
        # Older system Python builds fall back to rollback journaling, not unsafe WAL.
        mode = self.db.execute('PRAGMA journal_mode').fetchone()[0].lower()
        if not wal_fixed(sqlite3.sqlite_version_info) and mode == 'wal':
            raise ACError(4, 'existing_wal_database_requires_patched_sqlite')
        self.db.execute('PRAGMA journal_mode=' + ('WAL' if wal_fixed(sqlite3.sqlite_version_info) else 'DELETE'))
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version == 0:
            with self.transaction():
                # Another process may have initialized while BEGIN was waiting.
                if self.db.execute('PRAGMA user_version').fetchone()[0] == 0:
                    for sql in SCHEMA:
                        self.db.execute(sql)
                    self.db.execute('PRAGMA user_version=2')
        elif version == 1:
            self.migrate()
        elif version != 2:
            raise ACError(4, 'unsupported_schema_version')

    def migrate(self):
        folder = self.path.parent / 'backups'
        folder.mkdir(exist_ok=True)
        backup = folder / ('schema1-' + uuid.uuid4().hex + '.db')
        target = sqlite3.connect(backup)
        try:
            self.db.backup(target)
        finally:
            target.close()
        migration = Path(__file__).resolve().parent / 'migrations' / '002_agents_and_task_leases.sql'
        sql = migration.read_text(encoding='utf-8')
        with self.transaction():
            if self.db.execute('PRAGMA user_version').fetchone()[0] != 1:
                return
            statement = ''
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    self.db.execute(statement)
                    statement = ''
            if statement.strip():
                raise ACError(4, 'incomplete_migration')
            self.db.execute('PRAGMA user_version=2')
            self.event('schema_migrated', 'agent-chat', from_version=1, to_version=2, backup_ref=str(backup))

    def close(self):        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def event(self, kind, agent, task=None, message=None, **details):
        self.db.execute('INSERT INTO events(task_id,agent_id,event_type,event_time,details,message_id) VALUES(?,?,?,?,?,?)',
                        (task, agent, kind, utc(), encoded(details), message))

    def registered(self, agent):
        return self.db.execute('SELECT 1 FROM agents WHERE agent_id=?', (agent,)).fetchone() is not None

    def register(self, agent, profile=None, update=False, evidence=None):
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,95}', agent):
            raise ACError(4, 'invalid_agent_id')
        if profile is not None and not isinstance(profile, dict):
            raise ACError(4, 'profile_must_be_object')
        allowed = {'display_name', 'provider', 'capabilities', 'wake_mode', 'wake_level', 'status', 'metadata'}
        if profile and set(profile) - allowed:
            raise ACError(4, 'unknown_profile_fields')
        with self.transaction():
            old = self.db.execute('SELECT * FROM agents WHERE agent_id=?', (agent,)).fetchone()
            current = {k: old[k] for k in allowed} if old else dict(display_name=agent, provider='unknown', capabilities='[]', wake_mode='unknown', wake_level='L1', status='unknown', metadata='{}')
            current['capabilities'] = json.loads(current['capabilities'])
            current['metadata'] = json.loads(current['metadata'])
            candidate = dict(current)
            candidate.update(profile or {})
            if profile and isinstance(profile.get('metadata'), dict):
                if any(k.startswith('wake_evidence_') for k in profile['metadata']):
                    raise ACError(4, 'wake_evidence_is_managed_by_verification')
                candidate['metadata'] = dict(current['metadata'], **profile['metadata'])
            caps = candidate['capabilities']
            if not isinstance(caps, list) or any(not isinstance(x, str) or not x.strip() for x in caps):
                raise ACError(4, 'capabilities_must_be_string_array')
            candidate['capabilities'] = sorted(set(caps))
            if not isinstance(candidate['metadata'], dict) or any(not isinstance(candidate[k], str) or not candidate[k].strip() for k in ('display_name', 'provider', 'status')):
                raise ACError(4, 'invalid_profile_values')
            if candidate['wake_mode'] not in ('manual', 'native_background', 'scheduled', 'external_adapter', 'none', 'unknown') or candidate['wake_level'] not in ('L1', 'L2', 'L3', 'L4'):
                raise ACError(4, 'invalid_wake_mode_or_level')
            if old and candidate == current:
                return False
            if old and not update:
                raise ACError(4, 'registration_change_requires_update')
            old_level = old['wake_level'] if old else 'L1'
            if candidate['wake_level'] > old_level or (old and candidate['wake_level'] in ('L3', 'L4') and candidate['wake_mode'] != current['wake_mode']):
                self.verify_level(agent, candidate['wake_level'], evidence, candidate)
            now = utc()
            if old:
                self.db.execute('UPDATE agents SET display_name=?,provider=?,capabilities=?,wake_mode=?,wake_level=?,status=?,metadata=?,last_seen_at=? WHERE agent_id=?',
                                (candidate['display_name'], candidate['provider'], encoded(candidate['capabilities']), candidate['wake_mode'], candidate['wake_level'], candidate['status'], encoded(candidate['metadata']), now, agent))
                self.event('agent_updated', agent, profile=candidate, evidence_ref=evidence)
            else:
                self.db.execute('INSERT INTO agents VALUES(?,?,?,?,?,?,?,?,?,?)',
                                (agent, candidate['display_name'], candidate['provider'], encoded(candidate['capabilities']), candidate['wake_mode'], candidate['wake_level'], candidate['status'], now, now, encoded(candidate['metadata'])))
                self.event('agent_registered', agent, profile=candidate)
            return True

    def verify_level(self, agent, level, evidence, candidate):
        kinds = {r[0] for r in self.db.execute("SELECT DISTINCT event_type FROM events WHERE agent_id=? AND event_type IN ('message_sent','message_claimed','message_done')", (agent,))}
        if not {'message_sent', 'message_claimed', 'message_done'} <= kinds:
            raise ACError(4, 'messaging_not_yet_verified')
        if level in ('L3', 'L4'):
            if not evidence:
                raise ACError(4, 'wake_level_requires_test_evidence')
            path = Path(evidence)
            if not path.is_absolute():
                path = self.path.parent / path
            record = json.loads(path.read_text(encoding='utf-8-sig'))
            if record.get('agent_id') != agent or record.get('transport') != 'sqlite' or record.get('level') != level or record.get('passed') is not True:
                raise ACError(4, 'invalid_wake_evidence')
            if level == 'L4' and not all(record.get(k) is True for k in ('long_running', 'recovery', 'cross_session')):
                raise ACError(4, 'L4_lifecycle_tests_missing')
            candidate['metadata'] = dict(candidate['metadata'], wake_evidence_ref=str(path), wake_evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), wake_evidence_scope=record.get('scope', 'unknown'))

    def touch(self, agent):
        self.db.execute('UPDATE agents SET last_seen_at=? WHERE agent_id=?', (utc(), agent))

    def heartbeat(self, agent, status=None):
        with self.transaction():
            self.require_agent(agent)
            if status:
                self.db.execute('UPDATE agents SET status=?,last_seen_at=? WHERE agent_id=?', (status, utc(), agent))
            else:
                self.touch(agent)
            self.event('heartbeat', agent, reported_status=status or 'unchanged', source='cpu_heartbeat_not_model_proof')

    def info(self, agent, activity_window=60):
        row = self.db.execute('SELECT * FROM agents WHERE agent_id=?', (agent,)).fetchone()
        if row is None:
            raise ACError(4, 'agent_not_registered')
        data = dict(row)
        data['capabilities'] = json.loads(data['capabilities'])
        data['metadata'] = json.loads(data['metadata'])
        recent = data['last_seen_at'] >= utc(-activity_window)
        executing = self.db.execute("SELECT count(*) FROM tasks WHERE claimed_by=? AND lease_until>? AND status IN ('claimed','running')", (agent, utc())).fetchone()[0]
        data.update(registered=True, recent_activity=recent, executing_tasks=executing,
                    wake_verified=data['wake_level'] in ('L3', 'L4'), wake_available_now='unknown',
                    availability='offline' if data['status'] == 'offline' or not recent else ('executing' if executing else 'unknown'))
        return data

    def require_agent(self, agent):
        if not self.registered(agent):
            raise ACError(4, 'agent_not_registered')

    def send(self, sender, receiver, payload, key=None, kind='message', task_id=None,
             project_id='local', task_type='generic', step=None, artifact=None,
             retry=0, max_retries=2, correlation_id=None, reply_to=None,
             claim_token=None, complete=False):
        if not payload.strip() or not receiver.strip():
            raise ACError(4, 'empty_payload_or_receiver')
        if task_id and correlation_id and task_id != correlation_id:
            raise ACError(4, 'task_and_correlation_mismatch')
        if (complete or claim_token) and reply_to is None:
            raise ACError(4, 'reply_requires_source_message')
        correlation = task_id or correlation_id
        key = key or uuid.uuid4().hex
        signature = hashlib.sha256(encoded([receiver, kind, payload, correlation, project_id,
                                           task_type, step, artifact, retry, reply_to, complete]).encode('utf-8')).hexdigest()
        with self.transaction():
            self.require_agent(sender)
            self.require_agent(receiver)
            artifact_hash = None
            if artifact:
                path = Path(artifact)
                if not path.is_absolute():
                    path = self.path.parent / path
                if not path.is_file():
                    raise ACError(4, 'artifact_not_found')
                artifact_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            existing = self.db.execute('SELECT id,content_hash,task_id FROM messages WHERE sender=? AND idempotency_key=?', (sender, key)).fetchone()
            if existing:
                if existing['content_hash'] != signature or existing['task_id'] != task_id:
                    raise ACError(4, 'idempotency_payload_conflict')
                if artifact:
                    event = self.db.execute("SELECT details FROM events WHERE message_id=? AND event_type='message_sent' ORDER BY event_id LIMIT 1", (existing['id'],)).fetchone()
                    snapshot = json.loads(event['details']).get('artifact_sha256') if event else None
                    if snapshot is None:
                        raise ACError(4, 'legacy_artifact_snapshot_unavailable_use_new_version_key')
                    if snapshot != artifact_hash:
                        raise ACError(4, 'idempotency_artifact_conflict')
                return existing['id'], True
            source = None
            if reply_to is not None:
                source = self.db.execute('SELECT * FROM messages WHERE id=?', (reply_to,)).fetchone()
                if (source is None or source['receiver'] != sender or source['claim_token'] != claim_token
                        or source['status'] != 'processing' or source['lease_until'] <= utc()):
                    raise ACError(6, 'stale_or_foreign_reply_claim')
                if source['task_id']:
                    source_task = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (source['task_id'],)).fetchone()
                    if source_task['claim_token'] != claim_token or source_task['lease_message_id'] != reply_to:
                        raise ACError(6, 'stale_task_lease')
                    if source_task['recovery_required']:
                        raise ACError(4, 'inspect_delivery_before_recovery')
            if task_id:
                task = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                if task is None:
                    if retry:
                        raise ACError(4, 'cannot_retry_missing_task')
                    self.db.execute('INSERT INTO tasks(task_id,project_id,assigned_agent,task_type,status,current_step,artifact_ref,retry_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                    (task_id, project_id, receiver, task_type, 'queued', step or 'assigned', artifact, 0, utc(), utc()))
                    self.event('task_created', sender, task_id, assigned_agent=receiver, artifact_ref=artifact, artifact_sha256=artifact_hash)
                else:
                    if task['status'] in ('completed', 'failed', 'cancelled'):
                        raise ACError(4, 'task_already_closed')
                    pending = self.db.execute("SELECT id FROM messages WHERE task_id=? AND status IN ('queued','processing')", (task_id,)).fetchall()
                    if any(not (source and complete and r['id'] == reply_to) for r in pending):
                        raise ACError(6, 'task_already_has_pending_work')
                    if task['lease_until'] and task['lease_until'] > utc() and not (source and source['task_id'] == task_id and task['claim_token'] == claim_token):
                        raise ACError(6, 'task_has_another_live_lease')
                    if task['retry_count'] + retry > max_retries:
                        raise ACError(4, 'task_retry_budget_exhausted')
                    self.db.execute("UPDATE tasks SET status='queued',lease_until=NULL,claim_token=NULL,claimed_by=NULL,lease_message_id=NULL,assigned_agent=?,current_step=?,artifact_ref=?,retry_count=retry_count+?,updated_at=? WHERE task_id=?",
                                    (receiver, step or task['current_step'], artifact or task['artifact_ref'], retry, utc(), task_id))
                    self.event('task_updated', sender, task_id, assigned_agent=receiver, step=step, retry=retry, artifact_ref=artifact, artifact_sha256=artifact_hash)
            row = self.db.execute("INSERT INTO messages(sender,receiver,type,payload,status,correlation_id,idempotency_key,content_hash,created_at,task_id) VALUES(?,?,?,?,'queued',?,?,?,?,?)",
                                  (sender, receiver, kind, payload, correlation, key, signature, utc(), task_id))
            mid = row.lastrowid
            self.event('message_sent', sender, correlation, mid, receiver=receiver, type=kind, idempotency_key=key, reply_to=reply_to, artifact_ref=artifact, artifact_sha256=artifact_hash)
            if complete:
                if source['task_id'] and source['task_id'] != task_id:
                    self.db.execute("UPDATE tasks SET status='retry_pending',lease_until=NULL,claim_token=NULL,claimed_by=NULL,lease_message_id=NULL,updated_at=? WHERE task_id=?", (utc(), source['task_id']))
                    self.event('task_updated', sender, source['task_id'], reply_to, status='retry_pending', reason='receipt_without_work_handoff')
                self.db.execute("UPDATE messages SET status='done',finished_at=?,lease_until=NULL WHERE id=?", (utc(), reply_to))
                self.event('message_done', sender, source['correlation_id'], reply_to, reply_id=mid)
            self.touch(sender)
            return mid, False

    def claim(self, agent, lease_seconds=600, max_deliveries=5):
        self.require_agent(agent)
        now = utc()
        # No write transaction or event is generated for an empty inbox poll.
        if not self.db.execute("SELECT 1 FROM messages WHERE receiver=? AND (status='queued' OR (status='processing' AND lease_until<=?)) LIMIT 1", (agent, now)).fetchone():
            return None
        with self.transaction():
            expired = self.db.execute("SELECT * FROM messages WHERE receiver=? AND status='processing' AND lease_until<=?", (agent, utc())).fetchall()
            for row in expired:
                self.db.execute("UPDATE messages SET status='queued',claim_token=NULL,lease_until=NULL WHERE id=?", (row['id'],))
                self.event('lease_expired', agent, row['correlation_id'], row['id'], delivery_count=row['delivery_count'])
                if row['task_id']:
                    self.db.execute("UPDATE tasks SET status='retry_pending',lease_until=NULL,claim_token=NULL,claimed_by=NULL,lease_message_id=NULL,recovery_required=1,recovery_count=recovery_count+1,updated_at=? WHERE task_id=? AND lease_message_id=?", (utc(), row['task_id'], row['id']))
                    self.event('task_recovery_pending', agent, row['task_id'], row['id'], delivery_check_required=True)
            queued = self.db.execute("SELECT * FROM messages WHERE receiver=? AND status='queued' ORDER BY id", (agent,)).fetchall()
            for row in queued:
                task = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (row['task_id'],)).fetchone() if row['task_id'] else None
                if task and task['status'] in ('completed', 'failed', 'cancelled'):
                    self.db.execute("UPDATE messages SET status='failed',finished_at=? WHERE id=?", (utc(), row['id']))
                    self.event('message_failed', agent, row['correlation_id'], row['id'], reason='persistent_task_closed')
                    continue
                if task and task['lease_until'] and task['lease_until'] > utc():
                    continue
                if row['delivery_count'] >= max_deliveries:
                    self.db.execute("UPDATE messages SET status='failed',finished_at=? WHERE id=?", (utc(), row['id']))
                    self.event('delivery_exhausted', agent, row['correlation_id'], row['id'])
                    if task:
                        self.db.execute("UPDATE tasks SET status='failed',lease_until=NULL,claim_token=NULL,claimed_by=NULL,lease_message_id=NULL,updated_at=? WHERE task_id=?", (utc(), task['task_id']))
                        self.event('task_failed', agent, task['task_id'], row['id'], reason='delivery_exhausted')
                    continue
                token = uuid.uuid4().hex
                lease_until = utc(lease_seconds)
                self.db.execute("UPDATE messages SET status='processing',claimed_at=?,lease_until=?,claim_token=?,delivery_count=delivery_count+1 WHERE id=?",
                                (utc(), lease_until, token, row['id']))
                if task:
                    self.db.execute("UPDATE tasks SET status='claimed',claimed_by=?,claim_token=?,lease_until=?,lease_message_id=?,updated_at=? WHERE task_id=?", (agent, token, lease_until, row['id'], utc(), task['task_id']))
                    self.event('task_claimed', agent, task['task_id'], row['id'], recovery_required=bool(task['recovery_required']))
                self.event('message_claimed', agent, row['correlation_id'], row['id'], delivery_count=row['delivery_count'] + 1, lease_seconds=lease_seconds)
                self.touch(agent)
                return dict(self.db.execute('SELECT * FROM messages WHERE id=?', (row['id'],)).fetchone())
            return None

    def ack(self, mid, agent, token, failed=False, renew=None, task_status=None, step=None, artifact=None, start=False, resume_after_check=False):
        with self.transaction():
            row = self.db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
            if row is None:
                raise ACError(4, 'message_not_found')
            if row['receiver'] != agent or row['claim_token'] != token:
                raise ACError(6, 'stale_or_foreign_claim')
            target = 'failed' if failed else 'done'
            if row['status'] == target and renew is None and not start:
                return target.upper()
            if row['status'] != 'processing' or row['lease_until'] <= utc():
                raise ACError(6, 'lease_expired_or_not_processing')
            task = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (row['task_id'],)).fetchone() if row['task_id'] else None
            if task and (task['claim_token'] != token or task['lease_message_id'] != mid):
                raise ACError(6, 'stale_task_lease')
            if start:
                if not task or failed or renew is not None or task_status:
                    raise ACError(4, 'invalid_task_start')
                if task['recovery_required'] and not resume_after_check:
                    raise ACError(4, 'inspect_delivery_before_recovery')
                if task['recovery_required']:
                    ref = Path(task['artifact_ref']) if task['artifact_ref'] else None
                    if ref is not None and not ref.is_absolute():
                        ref = self.path.parent / ref
                    self.event('task_recovery_checked', agent, task['task_id'], mid, artifact_ref=task['artifact_ref'], artifact_exists=bool(ref and ref.is_file()), artifact_sha256=hashlib.sha256(ref.read_bytes()).hexdigest() if ref and ref.is_file() else None, operator_confirmed=True)
                self.db.execute("UPDATE tasks SET status='running',recovery_required=0,current_step=?,updated_at=? WHERE task_id=?", (step or task['current_step'], utc(), task['task_id']))
                self.event('task_running', agent, task['task_id'], mid)
                self.touch(agent)
                return 'RUNNING'
            if renew is not None:
                if failed or task_status or step or artifact:
                    raise ACError(4, 'renew_cannot_complete_task')
                self.db.execute('UPDATE messages SET lease_until=? WHERE id=?', (utc(renew), mid))
                if task:
                    self.db.execute('UPDATE tasks SET lease_until=?,updated_at=? WHERE task_id=?', (utc(renew), utc(), task['task_id']))
                self.event('lease_renewed', agent, row['correlation_id'], mid, lease_seconds=renew)
                return 'RENEWED'
            if task_status == 'running':
                raise ACError(4, 'use_start_to_keep_running_task_lease')
            if task and task['recovery_required'] and not failed and task_status not in ('failed', 'cancelled'):
                raise ACError(4, 'inspect_delivery_before_recovery')
            artifact_hash = None
            if artifact:
                path = Path(artifact)
                if not path.is_absolute():
                    path = self.path.parent / path
                if not path.is_file():
                    raise ACError(4, 'artifact_not_found')
                artifact_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if task or task_status or step or artifact:
                if task is None:
                    raise ACError(4, 'no_persistent_task_for_update')
                task_status = {'done': 'completed', 'paused': 'retry_pending'}.get(task_status, task_status)
                task_status = task_status or ('failed' if failed else 'retry_pending')
                self.db.execute('UPDATE tasks SET status=?,current_step=?,artifact_ref=?,updated_at=?,lease_until=NULL,claim_token=NULL,claimed_by=NULL,lease_message_id=NULL WHERE task_id=?',
                                (task_status, step or task['current_step'], artifact or task['artifact_ref'], utc(), task['task_id']))
                self.event('task_updated', agent, task['task_id'], mid, status=task_status, step=step, artifact_ref=artifact, artifact_sha256=artifact_hash)
            self.db.execute('UPDATE messages SET status=?,finished_at=?,lease_until=NULL WHERE id=?', (target, utc(), mid))
            self.event('message_' + target, agent, row['correlation_id'], mid)
            self.touch(agent)
            return target.upper()

    def get(self, mid):
        row = self.db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
        if row is None:
            raise ACError(4, 'message_not_found')
        task = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (row['correlation_id'],)).fetchone()
        events = [dict(r) for r in self.db.execute('SELECT * FROM events WHERE message_id=? ORDER BY event_id', (mid,))]
        return {'message': dict(row), 'task': dict(task) if task else None, 'events': events}


def positive(value):
    number = float(value)
    if not 0 < number <= 86400:
        raise argparse.ArgumentTypeError('requires 0 < seconds <= 86400')
    return number


def parser():
    p = argparse.ArgumentParser(description='Agent Chat 0.1, local SQLite transport; no model calls.')
    p.add_argument('--db', default=str(Path(__file__).resolve().with_name('ac.db')))
    p.add_argument('--lock-timeout', type=positive, default=5)
    p.add_argument('--version', action='version', version=VERSION)
    sub = p.add_subparsers(dest='command', required=True)
    send = sub.add_parser('send')
    send.add_argument('--sender', required=True)
    send.add_argument('--receiver', required=True)
    send.add_argument('--type', dest='kind', default='message')
    send.add_argument('--file', required=True, help='UTF-8 payload file, or - for stdin')
    send.add_argument('--idempotency-key')
    send.add_argument('--correlation-id')
    send.add_argument('--reply-to', type=int)
    send.add_argument('--claim-token')
    send.add_argument('--complete', action='store_true', help='atomically publish reply and finish its claimed source')
    send.add_argument('--task-id')
    send.add_argument('--project-id', default='local')
    send.add_argument('--task-type', default='generic')
    send.add_argument('--step')
    send.add_argument('--artifact')
    send.add_argument('--retry', type=int, choices=(0, 1), default=0)
    send.add_argument('--max-retries', type=int, default=2)
    wait = sub.add_parser('wait')
    wait.add_argument('--agent', required=True)
    wait.add_argument('--timeout', type=positive, default=180)
    wait.add_argument('--interval', type=positive, default=0.5)
    wait.add_argument('--lease-seconds', type=positive, default=600)
    wait.add_argument('--max-deliveries', type=int, default=5)
    wait.add_argument('--stop-file')
    get = sub.add_parser('get')
    get.add_argument('--id', type=int, required=True)
    get.add_argument('--out', help='write selected full message to a UTF-8 file')
    ack = sub.add_parser('ack')
    ack.add_argument('--agent', required=True)
    ack.add_argument('--id', required=True, type=int)    ack.add_argument('--token', required=True)
    ack.add_argument('--failed', action='store_true')
    ack.add_argument('--renew', type=positive)
    ack.add_argument('--task-status', choices=('running', 'completed', 'failed', 'cancelled', 'retry_pending', 'done', 'paused'))
    ack.add_argument('--start', action='store_true', help='start claimed persistent work without completing its message')
    ack.add_argument('--resume-after-check', action='store_true')
    ack.add_argument('--step')
    ack.add_argument('--artifact')
    status = sub.add_parser('status')
    status.add_argument('--agent')
    status.add_argument('--register', action='store_true')
    status.add_argument('--task-id')
    status.add_argument('--json', action='store_true')
    register = sub.add_parser('register')
    register.add_argument('agent_id')
    register.add_argument('--file', help='UTF-8 partial profile JSON, or - for stdin')
    register.add_argument('--update', action='store_true')
    register.add_argument('--evidence', help='explicit test-owner evidence JSON for L3/L4')
    listing = sub.add_parser('list-agents')
    listing.add_argument('--activity-window', type=positive, default=60)
    find = sub.add_parser('find')
    find.add_argument('capability')
    find.add_argument('--activity-window', type=positive, default=60)
    info = sub.add_parser('info')
    info.add_argument('agent_id')
    info.add_argument('--activity-window', type=positive, default=60)
    heartbeat = sub.add_parser('heartbeat')
    heartbeat.add_argument('--agent', required=True)
    heartbeat.add_argument('--status', help='self-reported state, never model-liveness proof')
    heartbeat.add_argument('--duration', type=float, default=0)
    heartbeat.add_argument('--interval', type=positive, default=15)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        store = Store(args.db, args.lock_timeout)
        if args.command == 'register':
            profile = None
            if args.file:
                raw = sys.stdin.buffer.read().decode('utf-8-sig') if args.file == '-' else Path(args.file).read_text(encoding='utf-8-sig')
                profile = json.loads(raw)
            changed = store.register(args.agent_id, profile, args.update, args.evidence)
            print('REGISTERED' if changed else 'EXISTS', args.agent_id)
        elif args.command == 'info':
            print(encoded(store.info(args.agent_id, args.activity_window)))
        elif args.command in ('list-agents', 'find'):
            if args.command == 'find':
                rows = store.db.execute('SELECT DISTINCT a.agent_id FROM agents a,json_each(a.capabilities) c WHERE c.value=? ORDER BY a.agent_id', (args.capability,)).fetchall()
            else:
                rows = store.db.execute('SELECT agent_id FROM agents ORDER BY agent_id').fetchall()
            for row in rows:
                item = store.info(row['agent_id'], args.activity_window)
                print(item['agent_id'], item['display_name'], item['wake_level'], item['availability'], sep='\t')
            if not rows:
                print('NONE')
        elif args.command == 'heartbeat':
            if args.duration < 0 or args.duration > 86400:
                raise ACError(4, 'invalid_heartbeat_duration')
            deadline = time.monotonic() + args.duration
            while True:
                if (store.path.parent / 'STOP').exists():
                    print('STOPPED')
                    return 3
                store.heartbeat(args.agent, args.status)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(args.interval, remaining))
            print('HEARTBEAT', args.agent)
        elif args.command == 'send':
            payload = sys.stdin.buffer.read().decode('utf-8-sig') if args.file == '-' else Path(args.file).read_text(encoding='utf-8-sig')
            keys = ('sender', 'receiver', 'kind', 'task_id', 'project_id', 'task_type', 'step', 'artifact', 'retry', 'max_retries', 'correlation_id', 'reply_to', 'claim_token', 'complete')
            mid, duplicate = store.send(payload=payload, key=args.idempotency_key, **{k: getattr(args, k) for k in keys})
            print(('EXISTS' if duplicate else 'SENT'), mid)
        elif args.command == 'wait':
            if args.interval > 60 or args.max_deliveries < 1:
                raise ACError(4, 'invalid_wait_interval_or_delivery_budget')
            stop = Path(args.stop_file) if args.stop_file else store.path.parent / 'STOP'
            deadline = time.monotonic() + args.timeout
            while True:
                if stop.exists():
                    print('STOPPED')
                    return 3
                row = store.claim(args.agent, args.lease_seconds, args.max_deliveries)
                if row:
                    print('READY', row['id'], row['claim_token'])
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print('IDLE')
                    return 2
                time.sleep(min(args.interval, remaining))
        elif args.command == 'get':
            output = encoded(store.get(args.id))
            if args.out:
                Path(args.out).write_text(output, encoding='utf-8')
                print('WRITTEN', args.id)
            else:
                print(output)
        elif args.command == 'ack':
            keys = ('failed', 'renew', 'task_status', 'step', 'artifact', 'start', 'resume_after_check')
            print(store.ack(args.id, args.agent, args.token, **{k: getattr(args, k) for k in keys}), args.id)
        else:
            if args.register:
                if not args.agent:
                    raise ACError(4, 'registration_requires_agent')
                store.register(args.agent)
            counts = dict(store.db.execute('SELECT status,count(*) FROM messages WHERE receiver=? GROUP BY status', (args.agent,)).fetchall()) if args.agent else dict(store.db.execute('SELECT status,count(*) FROM messages GROUP BY status').fetchall())
            if args.task_id:
                task = store.db.execute('SELECT * FROM tasks WHERE task_id=?', (args.task_id,)).fetchone()
                data = {'task': dict(task) if task else None}
            else:
                data = {'agent': args.agent, 'registered': store.registered(args.agent) if args.agent else None,
                        'messages': counts, 'sqlite': sqlite3.sqlite_version, 'version': VERSION}
                if args.agent and store.registered(args.agent):
                    data['activity'] = store.info(args.agent)
            if args.json:
                print(encoded(data))
            elif args.register:
                print('REGISTERED', args.agent)
            else:
                print('STATUS', 'queued=' + str(counts.get('queued', 0)), 'processing=' + str(counts.get('processing', 0)))
        return 0
    except KeyboardInterrupt:
        print('STOPPED')
        return 3
    except ACError as exc:
        print('ERROR', 100 + exc.code, exc.reason)
        return exc.code
    except sqlite3.OperationalError as exc:
        locked = 'locked' in str(exc).lower() or 'busy' in str(exc).lower()
        print('ERROR', 105 if locked else 104, 'database_locked' if locked else str(exc).replace('\n', ' '))
        return 5 if locked else 4
    except (OSError, UnicodeError, sqlite3.DatabaseError, ValueError, TypeError) as exc:
        print('ERROR', 104, str(exc).replace('\n', ' '))
        return 4
    finally:
        if store:
            store.close()


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    sys.exit(main())
