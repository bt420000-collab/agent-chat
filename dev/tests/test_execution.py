"""Execution lifecycle tests for the optional Agent Chat project layer."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ExecutionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.db = self.work / "ac.db"
        for agent in ("codex", "antigravity", "qwen"):
            self.run_cmd("ac.py", "register", agent)

    def run_cmd(self, script, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(ROOT / script), "--db", str(self.db), *map(str, args)],
            cwd=self.work, text=True, capture_output=True, encoding="utf-8", timeout=10
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout.strip()

    def post(self, job_id="JOB-1"):
        spec = {
            "id": job_id, "title": "执行生命周期测试", "requester": "codex",
            "objective": "验证领取、执行、交付和验收", "input": {}, "output": {},
            "capabilities": []
        }
        path = self.work / (job_id + ".json")
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        self.run_cmd("ac_space.py", "post", "--file", path)

    def claim(self, job_id="JOB-1", agent="antigravity", lease=30):
        parts = self.run_cmd("ac_space.py", "claim", job_id, "--agent", agent, "--lease", lease).split()
        self.assertEqual(parts[:2], ["CLAIMED", job_id])
        return parts[2]

    def job(self, job_id="JOB-1"):
        return json.loads(self.run_cmd("ac_space.py", "job", job_id))

    def test_full_execution_lifecycle_and_acceptance(self):
        self.post()
        token = self.claim()
        started = self.run_cmd(
            "ac_space.py", "start", "JOB-1", "--agent", "antigravity", "--token", token,
            "--executor", "codex-dispatch", "--runtime", "codex-cli", "--session", "session-1"
        )
        attempt_id = started.split()[-1]
        self.run_cmd("ac_space.py", "running", "JOB-1", "--agent", "antigravity", "--token", token, "--lease", 30)
        self.run_cmd("ac_space.py", "heartbeat", "JOB-1", "--agent", "antigravity", "--token", token, "--lease", 30)
        artifact = self.work / "result.md"
        artifact.write_text("完成的结果", encoding="utf-8")
        self.run_cmd(
            "ac_space.py", "finish", "JOB-1", "--agent", "antigravity", "--token", token,
            "--result", "succeeded", "--artifact", "result.md", "--exit-code", 0
        )
        job = self.job()
        self.assertEqual(job["status"], "submitted")
        self.assertEqual(len(job["attempts"]), 1)
        attempt = job["attempts"][0]
        self.assertEqual(attempt["attempt_id"], attempt_id)
        self.assertEqual(attempt["status"], "succeeded")
        self.assertEqual(attempt["executor_id"], "codex-dispatch")
        self.assertEqual(attempt["runtime"], "codex-cli")
        self.assertEqual(attempt["artifact"], "result.md")
        self.run_cmd("ac_space.py", "accept", "JOB-1", "--agent", "codex")
        self.assertEqual(self.job()["status"], "done")

    def test_failed_attempt_reopens_job_and_fences_old_token(self):
        self.post()
        old_token = self.claim()
        self.run_cmd("ac_space.py", "start", "JOB-1", "--agent", "antigravity", "--token", old_token)
        self.run_cmd("ac_space.py", "running", "JOB-1", "--agent", "antigravity", "--token", old_token)
        self.run_cmd(
            "ac_space.py", "finish", "JOB-1", "--agent", "antigravity", "--token", old_token,
            "--result", "failed", "--exit-code", 17, "--error", "runtime crashed"
        )
        self.assertEqual(self.job()["status"], "open")
        new_token = self.claim(agent="qwen")
        self.assertNotEqual(old_token, new_token)
        self.run_cmd("ac_space.py", "heartbeat", "JOB-1", "--agent", "antigravity",
                     "--token", old_token, expected=6)
        self.run_cmd("ac_space.py", "start", "JOB-1", "--agent", "qwen", "--token", new_token)
        self.run_cmd("ac_space.py", "running", "JOB-1", "--agent", "qwen", "--token", new_token)
        self.run_cmd("ac_space.py", "finish", "JOB-1", "--agent", "qwen", "--token", new_token,
                     "--result", "succeeded")
        attempts = self.job()["attempts"]
        self.assertEqual([x["attempt_no"] for x in attempts], [1, 2])
        self.assertEqual([x["status"] for x in attempts], ["failed", "succeeded"])

    def test_cancel_is_request_then_confirmed_terminal_state(self):
        self.post()
        token = self.claim()
        self.run_cmd("ac_space.py", "start", "JOB-1", "--agent", "antigravity", "--token", token)
        self.run_cmd("ac_space.py", "running", "JOB-1", "--agent", "antigravity", "--token", token)
        self.assertEqual(
            self.run_cmd("ac_space.py", "cancel", "JOB-1", "--agent", "codex"),
            "CANCEL JOB-1 cancel_requested"
        )
        self.assertEqual(self.job()["active_attempt"]["status"], "cancel_requested")
        self.run_cmd("ac_space.py", "finish", "JOB-1", "--agent", "antigravity", "--token", token,
                     "--result", "cancelled")
        self.assertEqual(self.job()["status"], "cancelled")
        self.assertEqual(self.job()["attempts"][0]["status"], "cancelled")

    def test_expired_lease_requires_explicit_recovery_and_stop_confirmation(self):
        self.post()
        token = self.claim(lease=30)
        self.run_cmd("ac_space.py", "start", "JOB-1", "--agent", "antigravity", "--token", token)
        self.run_cmd("ac_space.py", "running", "JOB-1", "--agent", "antigravity", "--token", token)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE acs_jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id='JOB-1'")
            db.execute("UPDATE acs_job_attempts SET lease_until='2000-01-01T00:00:00Z' WHERE job_id='JOB-1'")
        self.run_cmd("ac_space.py", "recover", "JOB-1", "--agent", "codex", "--action", "requeue", expected=4)
        self.assertEqual(self.job()["status"], "claimed")
        self.run_cmd("ac_space.py", "recover", "JOB-1", "--agent", "codex", "--action", "requeue",
                     "--confirmed-stopped")
        self.assertEqual(self.job()["status"], "open")
        self.assertEqual(self.job()["attempts"][0]["status"], "interrupted")
        self.run_cmd("ac_space.py", "heartbeat", "JOB-1", "--agent", "antigravity",
                     "--token", token, expected=6)

    def test_legacy_claim_submit_remains_compatible(self):
        self.post()
        token = self.claim()
        artifact = self.work / "legacy.md"
        artifact.write_text("legacy flow", encoding="utf-8")
        self.run_cmd("ac_space.py", "submit", "JOB-1", "--agent", "antigravity",
                     "--token", token, "--artifact", "legacy.md")
        job = self.job()
        self.assertEqual(job["status"], "submitted")
        self.assertEqual(job["attempts"][0]["status"], "succeeded")
        self.assertEqual(job["attempts"][0]["details"], {})
        self.assertEqual(job["attempts"][0]["runtime"], "legacy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
