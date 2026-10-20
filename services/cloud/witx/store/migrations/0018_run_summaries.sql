-- Context compression for long runs: a summary of a run's earlier steps, written
-- by a separate model call. Requests then carry the summary instead of those steps; the
-- steps themselves stay stored. ``summary`` is NULL when the call failed (nothing is
-- compressed; the next try waits). The call is kept for audit and counted in usage.
CREATE TABLE run_summaries (
  id           uuid PRIMARY KEY,
  tenant_id    uuid NOT NULL,
  run_id       uuid NOT NULL,
  through_seq  integer NOT NULL,          -- covers the steps before it and its input
  summary      text,
  call         jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id)
);
CREATE INDEX run_summaries_run ON run_summaries (run_id, created_at);

ALTER TABLE run_summaries ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON run_summaries
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON run_summaries TO witx_app;
