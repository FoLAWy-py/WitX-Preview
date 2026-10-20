-- Grants : a person approves one held input "and the same kind". The
-- grant then approves later inputs of the task on the same phone and app, with the same
-- effects (and, for a message, the same recipient and sender), for a limited number of
-- uses and time, until revoked or the task is paused, stopped or resumed. Each input it
-- covers still gets its own approval row, bound to that input's digest and used once.
CREATE TABLE grants (
  id                uuid PRIMARY KEY,
  tenant_id         uuid NOT NULL,
  task_id           uuid NOT NULL,
  created_from      uuid NOT NULL,             -- the action the person approved
  device_id         uuid NOT NULL,
  app               text NOT NULL,             -- foreground app of that action's screenshot
  effects           text[] NOT NULL,
  terms             jsonb NOT NULL,            -- e.g. a message's recipient and sender
  control_revision  integer NOT NULL,          -- the task's; any pause, stop or resume ends it
  max_uses          integer NOT NULL CHECK (max_uses > 0),
  uses              integer NOT NULL DEFAULT 0 CHECK (uses BETWEEN 0 AND max_uses),
  created_by        uuid NOT NULL REFERENCES users (id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz NOT NULL,
  revoked_at        timestamptz,
  revoked_by        uuid REFERENCES users (id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id),
  FOREIGN KEY (created_from) REFERENCES actions (id)
);
CREATE INDEX grants_task ON grants (task_id, created_at);

ALTER TABLE grants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON grants
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON grants TO witx_app;

-- An approval a grant gave: decided "via grant", on behalf of the grant's creator.
ALTER TABLE approvals ADD COLUMN grant_id uuid REFERENCES grants (id);
ALTER TABLE approvals DROP CONSTRAINT approvals_decided_via_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_decided_via_check
  CHECK (decided_via IN ('web', 'mcp', 'grant'));
ALTER TABLE approvals ADD CHECK ((grant_id IS NULL) = (decided_via IS DISTINCT FROM 'grant'));
