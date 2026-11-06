-- Add execution attempts without changing the public job-board contract.
-- Jobs remain shared work offers; attempts record each concrete execution try.
CREATE TABLE IF NOT EXISTS acs_job_attempts(
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES acs_jobs(id),
    attempt_no INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    claim_token_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'claimed','starting','running','cancel_requested','recovery_required',
        'succeeded','failed','interrupted','start_failed','cancelled'
    )),
    executor_id TEXT,
    runtime TEXT,
    session_id TEXT,
    pid INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    heartbeat_at TEXT,
    lease_until TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    artifact TEXT,
    sha256 TEXT,
    error TEXT,
    updated_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(job_id,attempt_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS acs_attempt_one_active_per_job
    ON acs_job_attempts(job_id)
    WHERE status IN ('claimed','starting','running','cancel_requested','recovery_required');
CREATE INDEX IF NOT EXISTS acs_attempts_by_job
    ON acs_job_attempts(job_id,attempt_no DESC);
CREATE INDEX IF NOT EXISTS acs_attempts_by_lease
    ON acs_job_attempts(status,lease_until);
