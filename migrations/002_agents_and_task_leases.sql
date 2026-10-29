-- Applied transactionally after a schema-1 SQLite backup. No history is deleted.
CREATE TABLE agents(agent_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,provider TEXT NOT NULL,capabilities TEXT NOT NULL,wake_mode TEXT NOT NULL,wake_level TEXT NOT NULL,status TEXT NOT NULL,last_seen_at TEXT NOT NULL,registered_at TEXT NOT NULL,metadata TEXT NOT NULL);
INSERT INTO agents SELECT agent_id,agent_id,'unknown','[]','unknown','L1','unknown',MAX(event_time),MIN(event_time),'{"schema1_registration":true}' FROM events WHERE event_type='agent_registered' GROUP BY agent_id;
ALTER TABLE messages ADD COLUMN task_id TEXT REFERENCES tasks(task_id);
UPDATE messages SET task_id=correlation_id WHERE correlation_id IN (SELECT task_id FROM tasks);
ALTER TABLE tasks ADD COLUMN lease_until TEXT;
ALTER TABLE tasks ADD COLUMN claim_token TEXT;
ALTER TABLE tasks ADD COLUMN claimed_by TEXT;
ALTER TABLE tasks ADD COLUMN lease_message_id INTEGER;
ALTER TABLE tasks ADD COLUMN recovery_required INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0;
-- Schema 1 used running for both unclaimed work and work already delivered.
-- Do not infer that it is safe to repeat a side effect during migration.
UPDATE tasks SET recovery_required=1 WHERE status IN ('running','paused');
UPDATE tasks SET status=CASE status WHEN 'done' THEN 'completed' WHEN 'paused' THEN 'retry_pending' WHEN 'running' THEN 'retry_pending' ELSE status END;
UPDATE tasks SET status='claimed',lease_until=(SELECT lease_until FROM messages WHERE task_id=tasks.task_id AND status='processing' ORDER BY id DESC LIMIT 1),claim_token=(SELECT claim_token FROM messages WHERE task_id=tasks.task_id AND status='processing' ORDER BY id DESC LIMIT 1),claimed_by=(SELECT receiver FROM messages WHERE task_id=tasks.task_id AND status='processing' ORDER BY id DESC LIMIT 1),lease_message_id=(SELECT id FROM messages WHERE task_id=tasks.task_id AND status='processing' ORDER BY id DESC LIMIT 1) WHERE task_id IN (SELECT task_id FROM messages WHERE status='processing');
CREATE INDEX task_leases ON tasks(status,lease_until);
