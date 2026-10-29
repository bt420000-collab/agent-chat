#!/usr/bin/env python3
"""Agent Chat 0.1: local transport and durable receipts, never a model host."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

VERSION = '0.1.0'
SCHEMA = (
    """CREATE TABLE tasks(task_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,
       assigned_agent TEXT NOT NULL,task_type TEXT NOT NULL,status TEXT NOT NULL,
       current_step TEXT NOT NULL,artifact_ref TEXT,retry_count INTEGER NOT NULL DEFAULT 0,
       created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,sender TEXT NOT NULL,
       receiver TEXT NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL,
       status TEXT NOT NULL CHECK(status IN ('queued','processing','done','failed')),
       correlation_id TEXT,idempotency_key TEXT NOT NULL,content_hash TEXT NOT NULL,
       created_at TEXT NOT NULL,claimed_at TEXT,finished_at TEXT,lease_until TEXT,
       claim_token TEXT,delivery_count INTEGER NOT NULL DEFAULT 0,
       UNIQUE(sender,idempotency_key))""",
    """CREATE TABLE events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,
       agent_id TEXT NOT NULL,event_type TEXT NOT NULL,event_time TEXT NOT NULL,
       details TEXT NOT NULL,message_id INTEGER REFERENCES messages(id))""",
    'CREATE INDEX inbox ON messages(receiver,status,id)',
    'CREATE INDEX event_message ON events(message_id,event_id)',
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
                    self.db.execute('PRAGMA user_version=1')
        elif version != 1:
            raise ACError(4, 'unsupported_schema_version')

    def close(self):
        self.db.close()

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
        return self.db.execute("SELECT 1 FROM events WHERE agent_id=? AND event_type='agent_registered' LIMIT 1", (agent,)).fetchone() is not None

    def register(self, agent):
        if not agent.strip():
            raise ACError(4, 'empty_agent')
        with self.transaction():
            if not self.registered(agent):
                self.event('agent_registered', agent)

    def require_agent(self, agent):
        if not self.registered(agent):
            raise ACError(4, 'register_agent_with_status_first')

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
            existing = self.db.execute('SELECT id,content_hash FROM messages WHERE sender=? AND idempotency_key=?', (sender, key)).fetchone()
            if existing:
                if existing['content_hash'] != signature:
                    raise ACError(4, 'idempotency_payload_conflict')
                return existing['id'], True
            source = None
            if reply_to is not None:
                source = self.db.execute('SELECT * FROM messages WHERE id=?', (reply_to,)).fetchone()
                if (source is None or source['receiver'] != sender or source['claim_token'] != claim_token
                        or source['status'] != 'processing' or source['lease_until'] <= utc()):
                    raise ACError(6, 'stale_or_foreign_reply_claim')
            if task_id:
                task = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                if task is None:
                    if retry:
                        raise ACError(4, 'cannot_retry_missing_task')
                    self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                                    (task_id, project_id, receiver, task_type, 'running', step or 'assigned', artifact, 0, utc(), utc()))
                    self.event('task_created', sender, task_id, assigned_agent=receiver)
                elif step is not None or retry or artifact is not None:
                    if task['status'] in ('done', 'failed'):
                        raise ACError(4, 'task_already_closed')
                    if task['retry_count'] + retry > max_retries:
                        raise ACError(4, 'task_retry_budget_exhausted')
                    self.db.execute('UPDATE tasks SET assigned_agent=?,current_step=?,artifact_ref=?,retry_count=retry_count+?,updated_at=? WHERE task_id=?',
                                    (receiver, step or task['current_step'], artifact or task['artifact_ref'], retry, utc(), task_id))
                    self.event('task_updated', sender, task_id, assigned_agent=receiver, step=step, retry=retry)
            row = self.db.execute("INSERT INTO messages(sender,receiver,type,payload,status,correlation_id,idempotency_key,content_hash,created_at) VALUES(?,?,?,?,'queued',?,?,?,?)",
                                  (sender, receiver, kind, payload, correlation, key, signature, utc()))
            mid = row.lastrowid
            self.event('message_sent', sender, correlation, mid, receiver=receiver, type=kind, idempotency_key=key, reply_to=reply_to)
            if complete:
                self.db.execute("UPDATE messages SET status='done',finished_at=?,lease_until=NULL WHERE id=?", (utc(), reply_to))
                self.event('message_done', sender, source['correlation_id'], reply_to, reply_id=mid)
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
            while True:
                row = self.db.execute("SELECT * FROM messages WHERE receiver=? AND status='queued' ORDER BY id LIMIT 1", (agent,)).fetchone()
                if row is None:
                    return None
                if row['delivery_count'] >= max_deliveries:
                    self.db.execute("UPDATE messages SET status='failed',finished_at=? WHERE id=?", (utc(), row['id']))
                    self.event('delivery_exhausted', agent, row['correlation_id'], row['id'])
                    continue
                token = uuid.uuid4().hex
                self.db.execute("UPDATE messages SET status='processing',claimed_at=?,lease_until=?,claim_token=?,delivery_count=delivery_count+1 WHERE id=?",
                                (utc(), utc(lease_seconds), token, row['id']))
                self.event('message_claimed', agent, row['correlation_id'], row['id'], delivery_count=row['delivery_count'] + 1, lease_seconds=lease_seconds)
                return dict(self.db.execute('SELECT * FROM messages WHERE id=?', (row['id'],)).fetchone())

    def ack(self, mid, agent, token, failed=False, renew=None, task_status=None, step=None, artifact=None):
        with self.transaction():
            row = self.db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
            if row is None:
                raise ACError(4, 'message_not_found')
            if row['receiver'] != agent or row['claim_token'] != token:
                raise ACError(6, 'stale_or_foreign_claim')
            target = 'failed' if failed else 'done'
            if row['status'] == target and renew is None:
                return target.upper()
            if row['status'] != 'processing' or row['lease_until'] <= utc():
                raise ACError(6, 'lease_expired_or_not_processing')
            if renew is not None:
                if failed or task_status or step or artifact:
                    raise ACError(4, 'renew_cannot_complete_task')
                self.db.execute('UPDATE messages SET lease_until=? WHERE id=?', (utc(renew), mid))
                self.event('lease_renewed', agent, row['correlation_id'], mid, lease_seconds=renew)
                return 'RENEWED'
            artifact_hash = None
            if artifact:
                path = Path(artifact)
                if not path.is_absolute():
                    path = self.path.parent / path
                if not path.is_file():
                    raise ACError(4, 'artifact_not_found')
                artifact_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if task_status or step or artifact:
                task = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (row['correlation_id'],)).fetchone()
                if task is None:
                    raise ACError(4, 'no_persistent_task_for_update')
                self.db.execute('UPDATE tasks SET status=?,current_step=?,artifact_ref=?,updated_at=? WHERE task_id=?',
                                (task_status or task['status'], step or task['current_step'], artifact or task['artifact_ref'], utc(), task['task_id']))
                self.event('task_updated', agent, task['task_id'], mid, status=task_status, step=step, artifact_ref=artifact, artifact_sha256=artifact_hash)
            self.db.execute('UPDATE messages SET status=?,finished_at=?,lease_until=NULL WHERE id=?', (target, utc(), mid))
            self.event('message_' + target, agent, row['correlation_id'], mid)
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
    ack.add_argument('--id', required=True, type=int)
    ack.add_argument('--token', required=True)
    ack.add_argument('--failed', action='store_true')
    ack.add_argument('--renew', type=positive)
    ack.add_argument('--task-status', choices=('running', 'done', 'failed', 'paused'))
    ack.add_argument('--step')    ack.add_argument('--artifact')
    status = sub.add_parser('status')
    status.add_argument('--agent')
    status.add_argument('--register', action='store_true')
    status.add_argument('--task-id')
    status.add_argument('--json', action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        store = Store(args.db, args.lock_timeout)
        if args.command == 'send':
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
            keys = ('failed', 'renew', 'task_status', 'step', 'artifact')
            print(store.ack(args.id, args.agent, args.token, **{k: getattr(args, k) for k in keys}), args.id)
        else:
            if args.register:
                if not args.agent:
                    raise ACError(4, 'registration_requires_agent')
                store.register(args.agent)
            counts = dict(store.db.execute('SELECT status,count(*) FROM messages GROUP BY status').fetchall())
            if args.task_id:
                task = store.db.execute('SELECT * FROM tasks WHERE task_id=?', (args.task_id,)).fetchone()
                data = {'task': dict(task) if task else None}
            else:
                data = {'agent': args.agent, 'registered': store.registered(args.agent) if args.agent else None,
                        'messages': counts, 'sqlite': sqlite3.sqlite_version, 'version': VERSION}
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
    except (OSError, UnicodeError, sqlite3.DatabaseError) as exc:
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
