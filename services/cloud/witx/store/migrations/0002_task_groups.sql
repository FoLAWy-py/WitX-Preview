-- Task groups: a person's folders for tasks in the web app's sidebar. Deleting a group
-- keeps its tasks; they become ungrouped.

CREATE TABLE task_groups (
  id          uuid PRIMARY KEY,
  tenant_id   uuid NOT NULL REFERENCES tenants (id),
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  created_by  uuid NOT NULL REFERENCES users (id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id)
);

ALTER TABLE tasks ADD COLUMN group_id uuid;
ALTER TABLE tasks ADD CONSTRAINT tasks_group_fk FOREIGN KEY (tenant_id, group_id)
  REFERENCES task_groups (tenant_id, id) ON DELETE SET NULL (group_id);
CREATE INDEX tasks_group_recent ON tasks (tenant_id, group_id, updated_at DESC)
  WHERE deleted_at IS NULL;

ALTER TABLE task_groups ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON task_groups
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON task_groups TO witx_app;
