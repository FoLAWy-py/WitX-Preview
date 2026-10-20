-- Scheduled tasks: a task that starts by itself, once or repeatedly, at a local time.
-- The worker looks for due schedules every few seconds, each tenant under its own RLS.

CREATE TABLE schedules (
  id               uuid PRIMARY KEY,
  tenant_id        uuid NOT NULL REFERENCES tenants (id),
  created_by       uuid NOT NULL REFERENCES users (id),
  goal             text NOT NULL,
  constraints      text[] NOT NULL DEFAULT '{}',
  device_ids       uuid[] NOT NULL DEFAULT '{}',
  permission_mode  text NOT NULL CHECK (permission_mode IN ('ask', 'auto', 'full')),
  model            text NOT NULL,
  repeat           text NOT NULL
                   CHECK (repeat IN ('once', 'daily', 'weekdays', 'weekly', 'monthly')),
  time_of_day      time NOT NULL,
  timezone         text NOT NULL,
  weekdays         smallint[] NOT NULL DEFAULT '{}',
  month_day        smallint CHECK (month_day BETWEEN 1 AND 31),
  run_on           date,
  enabled          boolean NOT NULL DEFAULT true,
  next_run_at      timestamptz,
  last_run_at      timestamptz,
  last_task_id     uuid,
  last_outcome     text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id)
);
CREATE INDEX schedules_due ON schedules (next_run_at) WHERE enabled AND next_run_at IS NOT NULL;

ALTER TABLE schedules ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON schedules
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON schedules TO witx_app;
