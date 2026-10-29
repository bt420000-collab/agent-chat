import concurrent.futures
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'ac.py'
RUNS = ROOT / 'dev' / 'tests' / 'runs'
RUNS.mkdir(exist_ok=True)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=RUNS)
        self.work = Path(self.temp.name)
        self.db = self.work / 'ac.db'

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, code=0, input=None, script=SCRIPT):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        p = subprocess.run([sys.executable, str(script), '--db', str(self.db), *map(str, args)],
                           input=input, text=True, encoding='utf-8', capture_output=True,
                           cwd=self.work, env=env, timeout=10)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p.stdout.strip()

    def register(self):
        self.cli('status', '--agent', 'codex', '--register')
        self.cli('status', '--agent', 'antigravity', '--register')

    def send(self, key='one', **extra):
        flags = []
        for k, v in extra.items():
            flags.extend(['--' + k.replace('_', '-'), str(v)])
        return int(self.cli('send', '--sender', 'codex', '--receiver', 'antigravity',
                            '--idempotency-key', key, '--file', '-', *flags,
                            input='{"内容":"中文正文只走文件或标准输入"}').split()[1])

    def claim(self, lease=30):
        out = self.cli('wait', '--agent', 'antigravity', '--timeout', '0.2',
                       '--interval', '0.05', '--lease-seconds', lease)
        parts = out.split()
        self.assertEqual(parts[0], 'READY')
        return int(parts[1]), parts[2]

    def _wal_ok(self):
        v=sqlite3.sqlite_version_info
        return v >= (3,51,3) or (v[:2] == (3,50) and v[2] >= 7) or (v[:2] == (3,44) and v[2] >= 6)

    def test_register_is_idempotent_and_schema_uses_four_tables(self):
        self.register()
        self.cli('status', '--agent', 'codex', '--register')
        with closing(sqlite3.connect(self.db)) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            self.assertEqual(tables, {'messages', 'tasks', 'events', 'agents'})
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0], 'wal' if self._wal_ok() else 'delete')
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE event_type='agent_registered'").fetchone()[0], 2)

    def test_unicode_roundtrip_and_duplicate_payload_conflict(self):
        self.register()
        first = self.send()
        self.assertEqual(self.send(), first)
        self.cli('send', '--sender', 'codex', '--receiver', 'antigravity', '--idempotency-key', 'one',
                 '--file', '-', input='different', code=4)
        m = json.loads(self.cli('get', '--id', first))['message']
        self.assertIn('中文正文', m['payload'])
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0], 1)

    def test_transactional_claim_is_unique_between_processes(self):
        self.register()
        for i in range(8):
            self.send(str(i))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.claim(), range(8)))
        self.assertEqual(len({mid for mid, token in claims}), 8)
        self.assertEqual(len({token for mid, token in claims}), 8)

    def test_ack_owner_fencing_and_idempotence(self):
        self.register()
        self.send()
        mid, token = self.claim()
        self.cli('ack', '--agent', 'codex', '--id', mid, '--token', token, code=6)
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', 'stale', code=6)
        self.assertEqual(self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token), f'DONE {mid}')
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE event_type='message_done'").fetchone()[0], 1)

    def test_expired_lease_recovers_and_old_token_cannot_ack(self):
        self.register()
        self.send()
        mid, old = self.claim(0.15)
        time.sleep(0.2)
        mid2, new = self.claim()
        self.assertEqual(mid, mid2)
        self.assertNotEqual(old, new)
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', old, code=6)
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', new)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT delivery_count FROM messages').fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE event_type='lease_expired'").fetchone()[0], 1)

    def test_abrupt_process_exit_recovers_committed_claim(self):
        self.register()
        self.send()
        code = "import sys,os;sys.path.insert(0,sys.argv[1]);import ac;q=ac.Store(sys.argv[2]);r=q.claim('antigravity',0.15,5);print(r['id'],r['claim_token'],flush=True);os._exit(99)"
        p = subprocess.run([sys.executable, '-c', code, str(ROOT), str(self.db)], capture_output=True,
                           text=True, cwd=self.work, timeout=10, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(p.returncode, 99, p.stderr)
        old_id, old_token = p.stdout.split()
        time.sleep(0.2)
        mid, token = self.claim()
        self.assertEqual(mid, int(old_id))
        self.assertNotEqual(token, old_token)

    def test_renewal_and_failed_ack_are_explicit(self):
        self.register()
        self.send()
        mid, token = self.claim(8)
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token, '--renew', '20')
        time.sleep(0.25)
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token, '--failed')
        msg = json.loads(self.cli('get', '--id', mid))['message']
        self.assertEqual(msg['status'], 'failed')

    def test_timeout_stop_and_empty_payload_do_not_consume(self):
        self.register()
        self.assertEqual(self.cli('wait', '--agent', 'antigravity', '--timeout', '0.1', '--interval', '0.05', code=2), 'IDLE')
        self.cli('send', '--sender', 'codex', '--receiver', 'antigravity', '--file', '-', input='  ', code=4)
        self.send()
        stop = self.work / 'STOP'
        stop.write_text('stop', encoding='utf-8')
        self.assertEqual(self.cli('wait', '--agent', 'antigravity', '--stop-file', stop, code=3), 'STOPPED')
        m = json.loads(self.cli('get', '--id', 1))['message']
        self.assertEqual(m['status'], 'queued')

    def test_locked_database_returns_bounded_error_and_then_recovers(self):
        self.register()
        with closing(sqlite3.connect(self.db)) as locker:
            locker.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            self.cli('--lock-timeout', '0.15', 'send', '--sender', 'codex', '--receiver', 'antigravity',
                     '--file', '-', input='locked', code=5)
            self.assertLess(time.monotonic() - started, 3)
            locker.rollback()
        self.send()

    def test_events_cannot_be_updated_or_deleted(self):
        self.register()
        with closing(sqlite3.connect(self.db)) as c:
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute("UPDATE events SET details='erased'")
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute('DELETE FROM events')

    def test_task_artifact_and_revision_budget_are_transactional(self):
        self.register()
        self.send('initial', task_id='chapter', project_id='smoke', task_type='chapter', step='writing')
        artifact = self.work / 'chapter.md'
        artifact.write_text('测试正文', encoding='utf-8')
        mid, token = self.claim()
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token,
                 '--task-status', 'retry_pending', '--step', 'review', '--artifact', artifact)
        self.send('rev1', task_id='chapter', step='revision', retry='1', max_retries='2')
        mid, token = self.claim()
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token)
        self.send('rev2', task_id='chapter', step='revision', retry='1', max_retries='2')
        mid, token = self.claim()
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token)
        self.cli('send', '--sender', 'codex', '--receiver', 'antigravity', '--task-id', 'chapter',
                 '--retry', '1', '--max-retries', '2', '--file', '-', input='third revision', code=4)
        task = json.loads(self.cli('status', '--task-id', 'chapter', '--json'))['task']
        self.assertEqual(task['retry_count'], 2)
        self.assertEqual(Path(task['artifact_ref']), artifact)

    def test_delivery_budget_prevents_infinite_crash_loop(self):
        self.register()
        self.send()
        self.claim(0.1)
        time.sleep(0.15)
        self.cli('wait', '--agent', 'antigravity', '--timeout', '0.1', '--interval', '0.05', '--max-deliveries', '1', code=2)
        self.assertEqual(json.loads(self.cli('get', '--id', 1))['message']['status'], 'failed')

    def test_default_database_is_beside_copied_script(self):
        copied = self.work / 'portable'
        copied.mkdir()
        shutil.copy2(SCRIPT, copied / 'ac.py')
        p = subprocess.run([sys.executable, str(copied / 'ac.py'), 'status', '--agent', 'copy', '--register'],
                           cwd=self.work, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((copied / 'ac.db').exists())

    def test_reply_and_receipt_commit_together_and_are_idempotent(self):
        self.register()
        self.send()
        mid, token = self.claim()
        args = ('send', '--sender', 'antigravity', '--receiver', 'codex', '--file', '-',
                '--idempotency-key', 'reply-1', '--reply-to', mid, '--claim-token', token, '--complete')
        reply = self.cli(*args, input='reply')
        self.assertEqual(reply, 'SENT 2')
        self.assertEqual(self.cli(*args, input='reply'), 'EXISTS 2')
        self.assertEqual(json.loads(self.cli('get', '--id', mid))['message']['status'], 'done')
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE event_type='message_done'").fetchone()[0], 1)

    def test_expired_worker_cannot_publish_reply(self):
        self.register()
        self.send()
        mid, old = self.claim(0.1)
        time.sleep(0.15)
        self.claim()
        self.cli('send', '--sender', 'antigravity', '--receiver', 'codex', '--file', '-',
                 '--reply-to', mid, '--claim-token', old, '--complete', input='stale result', code=6)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
