-- Tasks a person pinned: theirs alone, so one person's pins do not reorder the
-- list anyone else sees. A deleted task's pins go with its row.
CREATE TABLE task_pins (
  tenant_id  uuid NOT NULL,
  user_id    uuid NOT NULL REFERENCES users (id),
  task_id    uuid NOT NULL,
  pinned_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, task_id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX task_pins_task ON task_pins (task_id);

ALTER TABLE task_pins ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON task_pins
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, DELETE ON task_pins TO witx_app;
