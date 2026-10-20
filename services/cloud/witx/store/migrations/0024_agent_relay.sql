-- Questions and messages between agents. A message goes to one run: the user's
-- answer to a sub-agent's question goes to that sub-agent only, and the main agent's
-- word to a sub-agent ('agent', written by no person) too. Without a run it is the
-- conversation, which the main agent reads.
ALTER TABLE task_inputs ADD COLUMN run_id uuid;
ALTER TABLE task_inputs ADD FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id);
ALTER TABLE task_inputs DROP CONSTRAINT task_inputs_kind_check;
ALTER TABLE task_inputs ADD CONSTRAINT task_inputs_kind_check
  CHECK (kind IN ('goal', 'constraint', 'note', 'agent'));
ALTER TABLE task_inputs ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE task_inputs ADD CONSTRAINT task_inputs_agent_check
  CHECK ((kind = 'agent') = (created_by IS NULL) AND (kind <> 'agent' OR run_id IS NOT NULL));

-- A sub-agent that asked the main agent waits for its answer.
ALTER TABLE runs DROP CONSTRAINT runs_wait_reason_check;
ALTER TABLE runs ADD CONSTRAINT runs_wait_reason_check CHECK (wait_reason IN ('observation',
  'action', 'approval', 'user_input', 'children', 'device', 'model_retry', 'paused', 'agent'));

-- The main agent's side turn while its sub-agents work: one model call on its history,
-- for a sub-agent's question or a message from the user. It answers, passes the word on,
-- or stops a sub-agent; it does not move the main agent's own run on, so it is no step.
CREATE TABLE side_turns (
  id           uuid PRIMARY KEY,
  tenant_id    uuid NOT NULL,
  task_id      uuid NOT NULL,
  run_id       uuid NOT NULL,              -- the main agent's run
  about_run_id uuid,                       -- the sub-agent that asked (a question)
  input_seq    integer,                    -- the user's message (a message)
  text         text NOT NULL,              -- the question or the message
  done_at      timestamptz,
  error        text,                       -- why it ended without the model (last try)
  tries        smallint NOT NULL DEFAULT 0,
  call         jsonb,                      -- the model request, response and usage
  created_at   timestamptz NOT NULL DEFAULT now(),
  CHECK ((about_run_id IS NULL) <> (input_seq IS NULL)),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id),
  FOREIGN KEY (tenant_id, about_run_id) REFERENCES runs (tenant_id, id)
);
CREATE INDEX side_turns_open ON side_turns (created_at) WHERE done_at IS NULL;

ALTER TABLE side_turns ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON side_turns
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON side_turns TO witx_app;
