-- Long-term memory : what a person chose to have remembered for their
-- future tasks. The agent may propose an entry; only the person keeps it. Each entry
-- belongs to one user in one tenant and is never shown to anyone else's tasks.
CREATE TABLE user_memories (
  id              uuid PRIMARY KEY,
  tenant_id       uuid NOT NULL,
  user_id         uuid NOT NULL REFERENCES users (id),
  text            text NOT NULL CHECK (length(text) BETWEEN 1 AND 300),
  status          text NOT NULL CHECK (status IN ('proposed', 'kept', 'declined')),
  source_task_id  uuid,                    -- the task whose agent proposed it, if any
  created_at      timestamptz NOT NULL DEFAULT now(),
  decided_at      timestamptz
);
CREATE INDEX user_memories_user ON user_memories (user_id, created_at);
CREATE INDEX user_memories_task ON user_memories (source_task_id) WHERE source_task_id IS NOT NULL;

ALTER TABLE user_memories ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON user_memories
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON user_memories TO witx_app;
