from contextlib import closing
import concurrent.futures
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
RUNS = ROOT / 'dev' / 'tests' / 'runs'


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=RUNS)
        self.work = Path(self.temp.name)
        self.db = self.work / 'ac.db'

    def tearDown(self):        self.temp.cleanup()

    def cli(self, *args, code=0, input=None):
        p = subprocess.run([sys.executable, '-B', str(ROOT / 'ac.py'), '--db', str(self.db), *map(str, args)],
                           cwd=self.work, input=input, text=True, encoding='utf-8', capture_output=True,
                           timeout=10, env=dict(os.environ, PYTHONUTF8='1'))
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p.stdout.strip()

    def profile(self, name, value):
        path = self.work / (name + '.json')
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def reg(self, name, **profile):
        flags = ['--file', str(self.profile(name, profile))] if profile else []
        return self.cli('register', name, *flags)

    def info(self, name):
        return json.loads(self.cli('info', name))

    def message(self, sender='codex', receiver='antigravity', key='one', task=None):
        flags = ['--task-id', task] if task else []
        return self.cli('send', '--sender', sender, '--receiver', receiver, '--file', '-',
                        '--idempotency-key', key, *flags, input='{"request":"test"}')

    def claim(self, agent='antigravity', lease='30'):
        out = self.cli('wait', '--agent', agent, '--timeout', '.1', '--interval', '.05', '--lease-seconds', lease).split()
        self.assertEqual(out[0], 'READY')
        return out[1], out[2]

    def test_register_four_identities_find_and_isolate_mailboxes(self):
        self.reg('codex', display_name='主编', provider='OpenAI', capabilities=['editing'])
        self.reg('antigravity', capabilities=['writing'])
        self.reg('codex.editor.01', capabilities=['coding', 'review'])
        self.reg('qwen.mock', provider='simulated', capabilities=['coding'])
        found = self.cli('find', 'coding')
        self.assertIn('codex.editor.01', found)
        self.assertIn('qwen.mock', found)
        self.assertNotIn('antigravity', found)
        self.assertEqual(len(self.cli('list-agents').splitlines()), 4)
        self.message(receiver='antigravity')
        self.message(receiver='qwen.mock', key='two')
        mid, token = self.claim('qwen.mock')
        self.assertEqual(mid, '2')
        self.cli('ack', '--agent', 'qwen.mock', '--id', mid, '--token', token)
        self.assertEqual(self.claim('antigravity')[0], '1')
        self.assertEqual(self.info('qwen.mock')['wake_level'], 'L1')

    def test_duplicate_registration_needs_explicit_update_and_is_audited(self):
        self.reg('codex', capabilities=['editing'])
        self.reg('codex', capabilities=['editing'])
        changed = self.profile('changed', {'capabilities': ['coding']})
        self.cli('register', 'codex', '--file', changed, code=4)
        self.cli('register', 'codex', '--file', changed, '--update')
        self.assertEqual(self.info('codex')['capabilities'], ['coding'])
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM agents').fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE event_type='agent_registered'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE event_type='agent_updated'").fetchone()[0], 1)

    def test_invalid_or_unregistered_receiver_is_explicit(self):
        self.reg('codex')
        self.cli('send', '--sender', 'codex', '--receiver', 'missing', '--file', '-', input='hello', code=4)
        self.cli('register', 'bad agent', code=4)
        self.cli('info', 'missing', code=4)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_heartbeat_is_not_online_or_reactive_proof(self):
        self.reg('mock')
        self.cli('heartbeat', '--agent', 'mock', '--status', 'waiting')
        info = self.info('mock')
        self.assertTrue(info['recent_activity'])
        self.assertEqual(info['availability'], 'unknown')
        self.assertFalse(info['wake_verified'])
        self.assertEqual(info['wake_available_now'], 'unknown')
        self.assertEqual(info['wake_level'], 'L1')
        self.cli('heartbeat', '--agent', 'mock', '--status', 'offline')
        self.assertEqual(self.info('mock')['availability'], 'offline')

    def test_levels_require_actual_database_activity_and_explicit_evidence(self):
        self.reg('codex')
        self.reg('mock')
        l3 = self.profile('l3', {'wake_level': 'L3', 'wake_mode': 'native_background'})
        self.cli('register', 'mock', '--update', '--file', l3, code=4)
        self.message(receiver='mock')
        mid, token = self.claim('mock')
        self.cli('ack', '--agent', 'mock', '--id', mid, '--token', token)
        self.message(sender='mock', receiver='codex', key='return')
        self.assertEqual(self.info('mock')['wake_level'], 'L1')
        l2 = self.profile('l2', {'wake_level': 'L2'})
        self.cli('register', 'mock', '--update', '--file', l2)
        self.assertEqual(self.info('mock')['wake_level'], 'L2')
        self.assertFalse(self.info('mock')['wake_verified'])

    def test_migration_keeps_schema1_records_and_makes_backup(self):
        legacy_path = ROOT / 'dev' / 'fixtures' / 'schema1' / 'ac.py'
        spec = importlib.util.spec_from_file_location('ac_legacy_for_test', legacy_path)
        legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(legacy)
        s = legacy.Store(self.db)
        s.register('codex')
        s.register('antigravity')
        mid, _ = s.send('codex', 'antigravity', 'old-message', key='legacy', task_id='old-task')
        before_message = dict(s.db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone())
        before_events = [dict(r) for r in s.db.execute('SELECT * FROM events ORDER BY event_id')]
        s.close()
        self.cli('status')
        with closing(sqlite3.connect(self.db)) as c:
            c.row_factory = sqlite3.Row
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 2)
            after = dict(c.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone())
            for key, value in before_message.items():
                self.assertEqual(after[key], value)
            self.assertEqual([dict(r) for r in c.execute('SELECT * FROM events ORDER BY event_id LIMIT ?', (len(before_events),))], before_events)
            self.assertEqual(tuple(c.execute('SELECT status,recovery_required FROM tasks WHERE task_id=?', ('old-task',)).fetchone()), ('retry_pending', 1))
            self.assertEqual(c.execute('SELECT count(*) FROM agents').fetchone()[0], 2)
        self.assertTrue(list((self.work / 'backups').glob('schema1-*.db')))
        self.assertEqual(self.info('antigravity')['wake_level'], 'L1')

    def test_failed_migration_rolls_back_without_losing_history(self):
        spec = importlib.util.spec_from_file_location('legacy_rollback', ROOT / 'dev' / 'fixtures' / 'schema1' / 'ac.py')
        legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(legacy)
        s = legacy.Store(self.db)
        s.register('codex')
        s.close()
        package = self.work / 'broken_upgrade'
        (package / 'migrations').mkdir(parents=True)
        shutil.copy2(ROOT / 'ac.py', package / 'ac.py')
        (package / 'migrations' / '002_agents_and_task_leases.sql').write_text('CREATE TABLE partial_change(id INTEGER);\nINVALID SQL;\n', encoding='utf-8')
        p = subprocess.run([sys.executable, '-B', str(package / 'ac.py'), '--db', str(self.db), 'status'],
                           cwd=self.work, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='partial_change'").fetchone())
            self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        self.assertTrue(list((self.work / 'backups').glob('schema1-*.db')))

    def test_concurrent_submitters_cannot_duplicate_one_task(self):
        self.reg('codex')
        self.reg('antigravity')
        def submit(key):
            return subprocess.run([sys.executable, '-B', str(ROOT / 'ac.py'), '--db', str(self.db),
                                   'send', '--sender', 'codex', '--receiver', 'antigravity', '--task-id', 'same-job',
                                   '--idempotency-key', key, '--file', '-'], input='work', text=True, capture_output=True,
                                  cwd=self.work, timeout=10).returncode
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(submit, ('one', 'two'))), [0, 6])
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0], 1)

    def test_task_claim_running_expiry_requires_recovery_check(self):
        self.reg('codex')
        self.reg('antigravity')
        self.message(task='job')
        self.assertEqual(json.loads(self.cli('status', '--task-id', 'job', '--json'))['task']['status'], 'queued')
        mid, token = self.claim()
        task = json.loads(self.cli('get', '--id', mid))['task']
        self.assertEqual(task['status'], 'claimed')
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token, '--start')
        self.assertEqual(json.loads(self.cli('get', '--id', mid))['task']['status'], 'running')
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token, '--renew', '.05')
        time.sleep(.15)
        mid2, token2 = self.claim()
        self.assertEqual(mid, mid2)
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token2, '--start', code=4)
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token2, '--start', '--resume-after-check')
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token2, '--task-status', 'completed')
        task = json.loads(self.cli('status', '--task-id', 'job', '--json'))['task']
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['recovery_count'], 1)

    def test_task_has_one_pending_work_item_and_handoff_is_atomic(self):
        self.reg('codex')
        self.reg('antigravity')
        self.message(task='job')
        self.cli('send', '--sender', 'codex', '--receiver', 'antigravity', '--file', '-',
                 '--task-id', 'job', '--idempotency-key', 'duplicate-work', input='second', code=6)
        mid, token = self.claim()
        self.cli('send', '--sender', 'antigravity', '--receiver', 'codex', '--file', '-',
                 '--task-id', 'job', '--reply-to', mid, '--claim-token', token, input='handoff', code=6)
        self.cli('send', '--sender', 'antigravity', '--receiver', 'codex', '--file', '-',
                 '--task-id', 'job', '--reply-to', mid, '--claim-token', token, '--complete', input='handoff')
        next_id, _ = self.claim('codex')
        task = json.loads(self.cli('get', '--id', next_id))['task']
        self.assertEqual(task['claimed_by'], 'codex')
        self.assertEqual(json.loads(self.cli('get', '--id', mid))['message']['status'], 'done')

    def test_notification_completion_releases_source_task_lease(self):
        self.reg('codex')
        self.reg('antigravity')
        self.message(task='job')
        mid, token = self.claim()
        self.cli('send', '--sender', 'antigravity', '--receiver', 'codex', '--file', '-',
                 '--correlation-id', 'job', '--reply-to', mid, '--claim-token', token, '--complete', input='notice')
        task = json.loads(self.cli('status', '--task-id', 'job', '--json'))['task']
        self.assertEqual(task['status'], 'retry_pending')
        self.assertIsNone(task['claim_token'])

    def test_idempotency_distinguishes_notice_from_work(self):
        self.reg('codex')
        self.reg('antigravity')
        args = ('send', '--sender', 'codex', '--receiver', 'antigravity', '--file', '-', '--idempotency-key', 'same')
        self.cli(*args, '--correlation-id', 'job', input='payload')
        self.cli(*args, '--task-id', 'job', input='payload', code=4)

    def test_recovered_task_cannot_skip_delivery_check(self):
        self.reg('codex')
        self.reg('antigravity')
        self.message(task='job')
        mid, _ = self.claim(lease='.05')
        time.sleep(.1)
        _, token = self.claim()
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token, '--task-status', 'completed', code=4)
        self.cli('send', '--sender', 'antigravity', '--receiver', 'codex', '--file', '-',
                 '--task-id', 'job', '--reply-to', mid, '--claim-token', token, '--complete', input='unchecked', code=4)

    def test_verified_level_retains_evidence_and_mode_changes_need_retest(self):
        self.reg('codex')
        self.reg('mock')
        self.message(receiver='mock')
        mid, token = self.claim('mock')
        self.cli('ack', '--agent', 'mock', '--id', mid, '--token', token)
        self.message(sender='mock', receiver='codex', key='return')
        evidence = self.profile('evidence', {'agent_id': 'mock', 'transport': 'sqlite', 'level': 'L3',
                                            'passed': True, 'scope': 'test fixture only'})
        upgrade = self.profile('upgrade', {'wake_level': 'L3', 'wake_mode': 'native_background'})
        self.cli('register', 'mock', '--update', '--file', upgrade, '--evidence', evidence)
        update = self.profile('metadata', {'metadata': {'note': 'kept'}})
        self.cli('register', 'mock', '--update', '--file', update)
        self.assertIn('wake_evidence_sha256', self.info('mock')['metadata'])
        update = self.profile('mode', {'wake_mode': 'manual'})
        self.cli('register', 'mock', '--update', '--file', update, code=4)
        evidence.write_text(json.dumps({'agent_id': 'mock', 'transport': 'sqlite', 'level': 'L4', 'passed': True}), encoding='utf-8')
        update = self.profile('l4', {'wake_level': 'L4'})
        self.cli('register', 'mock', '--update', '--file', update, '--evidence', evidence, code=4)

    def test_stale_activity_is_distinct_from_registration(self):
        self.reg('mock')
        with closing(sqlite3.connect(self.db)) as c:
            c.execute("UPDATE agents SET last_seen_at='2000-01-01T00:00:00.000000Z'")
            c.commit()
        info = self.info('mock')
        self.assertTrue(info['registered'])
        self.assertFalse(info['recent_activity'])
        self.assertEqual(info['availability'], 'offline')

    def test_first_artifact_is_audited_and_changed_file_cannot_reuse_key(self):
        self.reg('codex')
        self.reg('antigravity')
        artifact = self.work / 'draft.md'
        artifact.write_text('version one', encoding='utf-8')
        args = ('send', '--sender', 'codex', '--receiver', 'antigravity', '--file', '-',
                '--task-id', 'artifact-job', '--artifact', artifact, '--idempotency-key', 'artifact-v1')
        self.cli(*args, input='review')
        self.assertEqual(self.cli(*args, input='review'), 'EXISTS 1')
        with closing(sqlite3.connect(self.db)) as c:
            created = json.loads(c.execute("SELECT details FROM events WHERE event_type='task_created'").fetchone()[0])
            self.assertEqual(created['artifact_ref'], str(artifact))
            self.assertEqual(len(created['artifact_sha256']), 64)
        artifact.write_text('version two', encoding='utf-8')
        self.cli(*args, input='review', code=4)

    def test_legacy_artifact_without_snapshot_is_not_falsely_verified(self):
        spec = importlib.util.spec_from_file_location('legacy_artifact', ROOT / 'dev' / 'fixtures' / 'schema1' / 'ac.py')
        legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(legacy)
        artifact = self.work / 'old.md'
        artifact.write_text('old', encoding='utf-8')
        s = legacy.Store(self.db)
        s.register('codex')
        s.register('antigravity')
        s.send('codex', 'antigravity', 'review', key='old', task_id='old-task', artifact=str(artifact))
        s.close()
        output = self.cli('send', '--sender', 'codex', '--receiver', 'antigravity', '--file', '-',
                          '--task-id', 'old-task', '--artifact', artifact, '--idempotency-key', 'old', input='review', code=4)
        self.assertIn('artifact_snapshot_unavailable', output)

    def test_message_receipt_does_not_finish_persistent_task(self):
        self.reg('codex')
        self.reg('antigravity')
        self.message(task='job')
        mid, token = self.claim()
        self.cli('ack', '--agent', 'antigravity', '--id', mid, '--token', token)
        task = json.loads(self.cli('status', '--task-id', 'job', '--json'))['task']
        self.assertEqual(task['status'], 'retry_pending')


if __name__ == '__main__':
    unittest.main(verbosity=2)
