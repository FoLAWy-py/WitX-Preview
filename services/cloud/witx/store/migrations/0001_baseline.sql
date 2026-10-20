-- WitX baseline schema.
--
-- Until the first production deployment this file may be edited in place.
-- Afterwards, add a new numbered file instead (see docs/development.md).
--
-- Every tenant-owned table carries tenant_id, is protected by row-level security
-- keyed on the transaction setting `witx.tenant_id`, and references its parents
-- through (tenant_id, id) so rows can never point across tenants.
-- The application must connect with a role that does NOT own these tables
-- (it is granted witx_app); table owners bypass row-level security.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'witx_app') THEN
    CREATE ROLE witx_app NOLOGIN;
  END IF;
END
$$;

-- ------------------------------------------------------------------ identity

CREATE TABLE tenants (
  id          uuid PRIMARY KEY,
  name        text NOT NULL UNIQUE,
  -- Owners may let members pick "full access". Off by default.
  allow_full_access  boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id            uuid PRIMARY KEY,
  subject       text NOT NULL UNIQUE,          -- OIDC `sub` (or a dev subject)
  display_name  text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memberships (
  tenant_id  uuid NOT NULL REFERENCES tenants (id),
  user_id    uuid NOT NULL REFERENCES users (id),
  role       text NOT NULL CHECK (role IN ('owner', 'member')),
  PRIMARY KEY (tenant_id, user_id)
);

-- ------------------------------------------------------------------ devices

-- A Gateway's machine identity . It enrolls once with a one-time code,
-- then holds a credential it rotates itself: a rotation issues the next credential,
-- which replaces the current one the first time it is used. Revoked is for good;
-- disabled can be undone. Only SHA-256 hashes are stored.
CREATE TABLE gateways (
  id                 uuid PRIMARY KEY,
  tenant_id          uuid NOT NULL REFERENCES tenants (id),
  name               text NOT NULL,
  token_hash         bytea UNIQUE,              -- current credential; NULL until enrolled
  next_token_hash    bytea UNIQUE,              -- issued by a rotation, not used yet
  enroll_code_hash   bytea UNIQUE,              -- one-time enrollment code, until used
  enroll_expires_at  timestamptz,
  host               text,                      -- host name given when enrolling
  enrolled_at        timestamptz,
  rotated_at         timestamptz,
  disabled_at        timestamptz,
  revoked_at         timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  last_seen_at       timestamptz,
  UNIQUE (tenant_id, id)
);

CREATE TABLE devices (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  gateway_id    uuid NOT NULL,
  native_id     text NOT NULL,                 -- adb serial / iOS UDID
  platform      text NOT NULL CHECK (platform IN ('android', 'ios', 'fake')),
  name          text NOT NULL,
  capabilities  text[] NOT NULL DEFAULT '{}',
  sensitive_apps text[] NOT NULL DEFAULT '{}', -- every input in these apps counts as sensitive
  problems      text[] NOT NULL DEFAULT '{}', -- why it cannot take tasks now; empty: ready
  enabled_at    timestamptz,                   -- NULL: waits for an owner to enable it
  enabled_by    uuid REFERENCES users (id),
  online        boolean NOT NULL DEFAULT false,
  epoch         bigint NOT NULL DEFAULT 0,     -- only ever increases
  last_seen_at  timestamptz,
  UNIQUE (gateway_id, native_id),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, gateway_id) REFERENCES gateways (tenant_id, id)
);

-- Who enabled or disabled a device, and when (device pool, #33).
CREATE TABLE device_changes (
  id          uuid PRIMARY KEY,
  tenant_id   uuid NOT NULL,
  device_id   uuid NOT NULL,
  user_id     uuid NOT NULL REFERENCES users (id),
  change      text NOT NULL CHECK (change IN ('enabled', 'disabled')),
  name        text NOT NULL,                 -- the device's name after the change
  created_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, id)
);

-- ------------------------------------------------------------------ tasks

CREATE TABLE tasks (
  id                uuid PRIMARY KEY,
  tenant_id         uuid NOT NULL REFERENCES tenants (id),
  created_by        uuid NOT NULL REFERENCES users (id),
  goal              text NOT NULL,
  constraints       text[] NOT NULL DEFAULT '{}',
  status            text NOT NULL
                    CHECK (status IN ('active', 'pausing', 'paused', 'stopping', 'stopped',
                                      'completed', 'failed')),
  revision          integer NOT NULL DEFAULT 1,   -- bumped by goal/constraint changes
  control_revision  integer NOT NULL DEFAULT 1,   -- bumped by stop/resume; invalidates queued input
  model             text NOT NULL,
  permission_mode   text NOT NULL DEFAULT 'auto' CHECK (permission_mode IN ('ask', 'auto', 'full')),
  device_ids        uuid[] NOT NULL DEFAULT '{}',
  result            jsonb,
  event_seq         bigint NOT NULL DEFAULT 0,    -- stream head for `events`
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  -- Deletion : the tombstone hides the task at once; the purge then removes
  -- its content and screenshots, keeping only `audit` until the audit retention ends.
  deleted_at        timestamptz,
  deleted_by        uuid REFERENCES users (id),
  audit             jsonb,                        -- minimal summary kept after deletion
  purged_at         timestamptz,
  UNIQUE (tenant_id, id)
);

CREATE TABLE task_inputs (
  tenant_id   uuid NOT NULL,
  task_id     uuid NOT NULL,
  seq         integer NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('goal', 'constraint', 'note')),
  text        text NOT NULL,
  revision    integer NOT NULL,
  created_by  uuid NOT NULL REFERENCES users (id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, seq),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id)
);

CREATE TABLE runs (
  id             uuid PRIMARY KEY,
  tenant_id      uuid NOT NULL,
  task_id        uuid NOT NULL,
  parent_run_id  uuid REFERENCES runs (id),
  spawn_key      text,                         -- tool call that created a child run
  goal           text NOT NULL,
  depth          integer NOT NULL DEFAULT 0,
  device_ids     uuid[] NOT NULL DEFAULT '{}',
  status         text NOT NULL CHECK (status IN
                   ('runnable', 'running', 'waiting', 'completed', 'failed', 'cancelled')),
  wait_reason    text CHECK (wait_reason IN ('observation', 'action', 'approval',
                   'user_input', 'children', 'device', 'model_retry', 'paused')),
  wait_detail    text,
  next_wake_at   timestamptz,                  -- a waiting run is re-checked at this time
  epoch          bigint NOT NULL DEFAULT 0,    -- bumped on every claim; fences stale workers
  owner          text,
  lease_until    timestamptz,
  result         jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (parent_run_id, spawn_key),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id)
);
CREATE INDEX runs_claimable ON runs (updated_at) WHERE status IN ('runnable', 'waiting', 'running');
CREATE INDEX runs_task ON runs (task_id);

-- One model call. The complete request and response are kept for audit.
CREATE TABLE steps (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  run_id        uuid NOT NULL,
  seq           integer NOT NULL,
  status        text NOT NULL CHECK (status IN ('sent', 'done', 'error', 'abandoned')),
  model         text NOT NULL,
  input_seq     integer NOT NULL,              -- last task_inputs.seq included so far
  input_items   jsonb NOT NULL,                -- items new in this step
  request       jsonb NOT NULL,                -- full provider request (images elided)
  response      jsonb,
  output_items  jsonb,
  usage         jsonb,
  risk_call     jsonb,                         -- the independent risk check of this step's
                                               -- input: request, response, usage, timing
  error         text,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  UNIQUE (run_id, seq),
  FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id)
);

CREATE TABLE tool_calls (
  id              uuid PRIMARY KEY,
  tenant_id       uuid NOT NULL,
  run_id          uuid NOT NULL,
  step_id         uuid NOT NULL REFERENCES steps (id),
  call_id         text NOT NULL,               -- provider call id
  name            text NOT NULL,
  arguments       jsonb NOT NULL,
  output          jsonb,                       -- set when the call is resolved
  observation_id  uuid,
  action_id       uuid,
  child_run_id    uuid,
  created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),  -- orders calls of one step
  resolved_at     timestamptz,
  UNIQUE (step_id, call_id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id)
);
CREATE INDEX tool_calls_open ON tool_calls (run_id) WHERE output IS NULL;

-- ------------------------------------------------------------------ execution

CREATE TABLE device_leases (
  device_id          uuid PRIMARY KEY,
  tenant_id          uuid NOT NULL,
  task_id            uuid NOT NULL,
  run_id             uuid NOT NULL,            -- the single writer
  epoch              bigint NOT NULL,
  stop_id            uuid,                     -- set while a stop barrier is pending
  stop_requested_at  timestamptz,
  -- A person controlling the device  under this lease's epoch, until the
  -- expiry, which each of their inputs extends. A new epoch ends the takeover.
  takeover_id          uuid,
  takeover_user        uuid REFERENCES users (id),
  takeover_expires_at  timestamptz,
  acquired_at        timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id)
);

CREATE TABLE observations (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  device_id     uuid NOT NULL,
  task_id       uuid,
  epoch         bigint,                        -- lease epoch at request time
  status        text NOT NULL CHECK (status IN ('requested', 'sent', 'complete', 'failed')),
  requested_at  timestamptz NOT NULL DEFAULT now(),
  sent_at       timestamptz,
  captured_at   timestamptz,
  screen        jsonb,
  sha256        text,
  object_key    text,                          -- NULL on a complete one: purged (retention)
  error         text,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, id)
);
CREATE INDEX observations_latest ON observations (device_id, captured_at DESC)
  WHERE status = 'complete';
CREATE INDEX observations_pending ON observations (device_id) WHERE status IN ('requested', 'sent');

CREATE TABLE actions (
  id                   uuid PRIMARY KEY,
  tenant_id            uuid NOT NULL,
  task_id              uuid NOT NULL,
  run_id               uuid NOT NULL,
  device_id            uuid NOT NULL,
  tool_call_id         uuid NOT NULL UNIQUE REFERENCES tool_calls (id),
  epoch                bigint NOT NULL,
  control_revision     integer NOT NULL,
  observation_id       uuid REFERENCES observations (id),
  kind                 text NOT NULL,
  input                jsonb NOT NULL,
  target               text NOT NULL,
  expected_effect      text NOT NULL,
  effects              text[] NOT NULL DEFAULT '{}',
  policy               jsonb,                  -- mode, signals and decision (domain.Policy)
  digest               text NOT NULL,
  status               text NOT NULL CHECK (status IN ('awaiting_approval', 'queued', 'sent',
                         'done', 'rejected', 'denied', 'cancelled')),
  effect               text NOT NULL CHECK (effect IN
                         ('not_attempted', 'unverified', 'unknown', 'success', 'no_effect')),
  reason               text,
  post_observation_id  uuid NOT NULL UNIQUE,
  found_apps           jsonb,                  -- find_app: the installed apps that match, best first
  intent               jsonb,                  -- a message's or payment's terms, bound to its approval
  reconciled_by        uuid REFERENCES users (id),
  reconciled_with      uuid REFERENCES observations (id),
  retry_of             uuid REFERENCES actions (id), -- the action a person confirmed had no effect,
                                                    -- which this one tries again 
  created_at           timestamptz NOT NULL DEFAULT now(),
  sent_at              timestamptz,
  finished_at          timestamptz,
  FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id),
  FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, id)
);
CREATE INDEX actions_task ON actions (task_id);
CREATE INDEX actions_device_open ON actions (device_id) WHERE status IN ('queued', 'sent');
CREATE INDEX actions_device_unknown ON actions (device_id) WHERE effect = 'unknown';

CREATE TABLE approvals (
  action_id     uuid PRIMARY KEY REFERENCES actions (id),
  tenant_id     uuid NOT NULL,
  digest        text NOT NULL,                 -- must equal actions.digest at use time
  requested_at  timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,
  decision      text CHECK (decision IN ('approve', 'deny')),
  decided_by    uuid REFERENCES users (id),
  decided_at    timestamptz,
  decided_via   text CHECK (decided_via IN ('web', 'mcp')),  -- where the person decided
  decided_client text,                          -- the MCP client's name, if any
  used_at       timestamptz,
  CHECK (used_at IS NULL OR decision = 'approve')
);

CREATE TABLE events (
  tenant_id   uuid NOT NULL,
  task_id     uuid NOT NULL,
  seq         bigint NOT NULL,
  type        text NOT NULL,
  data        jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, seq),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id)
);

-- A browser's sign-in . The cookie holds a random token; only its hash is
-- stored. Not tenant-scoped: the tenant is chosen per request, and membership is
-- checked on every request, so removing a member takes effect at once.
CREATE TABLE sessions (
  token_hash  bytea PRIMARY KEY,               -- SHA-256 of the cookie value
  user_id     uuid NOT NULL REFERENCES users (id),
  subject     text NOT NULL,                   -- issuer subject (or dev subject)
  csrf        text NOT NULL,                   -- sent back in X-CSRF-Token on writes
  created_at  timestamptz NOT NULL DEFAULT now(),
  seen_at     timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,            -- absolute limit
  revoked_at  timestamptz
);

-- A request's Idempotency-Key and the response it got. A retry with the same key and
-- request gets the same response instead of acting again; the same key with another
-- request is a conflict. Keys expire after a day.
CREATE TABLE idempotency_keys (
  tenant_id    uuid NOT NULL REFERENCES tenants (id),
  key          text NOT NULL,
  user_id      uuid NOT NULL REFERENCES users (id),
  request      text NOT NULL,                  -- method and path, e.g. "POST /api/v1/tasks"
  digest       text NOT NULL,                  -- SHA-256 of the request body
  response     jsonb NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, key)
);

-- ------------------------------------------------------------------ row-level security

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['gateways', 'devices', 'device_changes', 'tasks', 'task_inputs', 'runs', 'steps',
                           'tool_calls', 'device_leases', 'observations', 'actions',
                           'approvals', 'events', 'idempotency_keys']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I '
      'USING (tenant_id = NULLIF(current_setting(''witx.tenant_id'', true), '''')::uuid) '
      'WITH CHECK (tenant_id = NULLIF(current_setting(''witx.tenant_id'', true), '''')::uuid)',
      t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO witx_app', t);
  END LOOP;
END
$$;

GRANT SELECT ON tenants, users, memberships TO witx_app;
GRANT SELECT, INSERT, UPDATE ON sessions TO witx_app;
-- The only tenant column the application may change; services check the caller is owner.
GRANT UPDATE (allow_full_access) ON tenants TO witx_app;

-- ------------------------------------------------------------------ cross-tenant entry points
-- The only two places that look across tenants. Both return the tenant so the caller
-- can continue inside that tenant's row-level security.

-- Claim runs that are ready: runnable, waiting past their wake time, or abandoned by a
-- worker whose lease expired. Each claim bumps the run epoch.
CREATE FUNCTION witx_claim_runs(p_owner text, p_lease_seconds integer, p_limit integer)
RETURNS TABLE (run_id uuid, tenant_id uuid, epoch bigint)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  WITH ready AS (
    SELECT id FROM runs
    WHERE status = 'runnable'
       OR (status = 'waiting' AND next_wake_at <= now())
       OR (status = 'running' AND lease_until < now())
    ORDER BY updated_at
    LIMIT p_limit
    FOR UPDATE SKIP LOCKED
  )
  UPDATE runs r
     SET status = 'running', owner = p_owner, epoch = r.epoch + 1,
         lease_until = now() + make_interval(secs => p_lease_seconds), updated_at = now()
    FROM ready
   WHERE r.id = ready.id
  RETURNING r.id, r.tenant_id, r.epoch;
$$;

-- Authenticates a Gateway credential. The first use of a rotated credential makes it
-- the current one and retires the previous one. Revoked or disabled Gateways get nothing.
CREATE FUNCTION witx_gateway_by_token(p_token_hash bytea)
RETURNS TABLE (gateway_id uuid, tenant_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public AS $$
BEGIN
  UPDATE gateways g
     SET token_hash = g.next_token_hash, next_token_hash = NULL, rotated_at = now()
   WHERE g.next_token_hash = p_token_hash AND g.revoked_at IS NULL;
  RETURN QUERY
    SELECT g.id, g.tenant_id FROM gateways g
     WHERE g.token_hash = p_token_hash AND g.revoked_at IS NULL AND g.disabled_at IS NULL;
END
$$;

REVOKE ALL ON FUNCTION witx_claim_runs(text, integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION witx_gateway_by_token(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION witx_claim_runs(text, integer, integer) TO witx_app;
GRANT EXECUTE ON FUNCTION witx_gateway_by_token(bytea) TO witx_app;
