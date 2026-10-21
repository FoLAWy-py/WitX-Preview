-- The last screenshot sweep of each tenant: what it found and whether it got
-- through. /metrics reads it, like every other number; the worker has no port.
CREATE TABLE screenshot_sweeps (
  tenant_id             uuid PRIMARY KEY REFERENCES tenants (id),
  finished_at           timestamptz NOT NULL,
  duration_ms           integer NOT NULL,
  scanned               integer NOT NULL,  -- objects listed under <tenant>/observations/
  deleted               integer NOT NULL,  -- held by no observation, deleted
  skipped_held          integer NOT NULL,  -- an observation holds it
  skipped_unrecognized  integer NOT NULL,  -- not named like a screenshot: left alone
  failed                integer NOT NULL,  -- could not be deleted: tried again next sweep
  error                 text               -- why the pass stopped early (an error class)
);

ALTER TABLE screenshot_sweeps ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON screenshot_sweeps
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON screenshot_sweeps TO witx_app;
