-- Side chats: questions about a task in a side tab, answered by the task's model
-- from what the task shows. An answer never acts and never enters the task's
-- conversation. One row is one question and its answer; a thread is one tab.
CREATE TABLE side_messages (
  id           uuid PRIMARY KEY,
  tenant_id    uuid NOT NULL,
  task_id      uuid NOT NULL,
  thread_id    uuid NOT NULL,
  question     text NOT NULL CHECK (length(question) BETWEEN 1 AND 4000),
  answer       text,
  error        text,                       -- why there is no answer (after the last try)
  tries        smallint NOT NULL DEFAULT 0,
  call         jsonb,                      -- the model request, response and usage
  asked_by     uuid NOT NULL REFERENCES users (id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  answered_at  timestamptz,
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id)
);
CREATE INDEX side_messages_task ON side_messages (task_id, created_at);
CREATE INDEX side_messages_open ON side_messages (created_at)
  WHERE answer IS NULL AND error IS NULL;

ALTER TABLE side_messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON side_messages
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON side_messages TO witx_app;
