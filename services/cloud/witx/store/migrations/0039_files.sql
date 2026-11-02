-- Files the user gives a task, kept in object storage at <tenant>/files/<id>. A file
-- is uploaded first, on its own, and then attached to a task with the message it came with
-- (input_seq; NULL for the task's first message). One not attached within a day is purged.
CREATE TABLE files (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL REFERENCES tenants (id),
  task_id       uuid,
  input_seq     integer,
  name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 255),
  size          bigint NOT NULL CHECK (size >= 0),
  sha256        text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  content_type  text NOT NULL,
  created_by    uuid NOT NULL REFERENCES users (id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id),
  CHECK (task_id IS NOT NULL OR input_seq IS NULL)
);
CREATE INDEX files_task ON files (task_id);
CREATE INDEX files_unattached ON files (created_at) WHERE task_id IS NULL;

ALTER TABLE files ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON files
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE (task_id, input_seq), DELETE ON files TO witx_app;

-- Signs the links a phone downloads a file by. A link names its tenant, so it is
-- checked with that tenant's key under that tenant's row-level security; no setting to
-- configure, and every API process has the same key. gen_random_uuid() is a strong
-- random source; two give 244 random bits.
ALTER TABLE tenants ADD COLUMN link_key bytea NOT NULL
  DEFAULT (uuid_send(gen_random_uuid()) || uuid_send(gen_random_uuid()));
