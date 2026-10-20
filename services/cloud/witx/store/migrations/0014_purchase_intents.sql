-- Purchase intents : in a task that uses several phones, the agent
-- proposes where to buy (one option per phone it compared) and the person chooses one.
-- Only the chosen phone may then order or pay, and only on the chosen terms, so one
-- purchase goal is never ordered on two platforms. Tasks with one phone do not need one.
CREATE TABLE purchase_intents (
  id           uuid PRIMARY KEY,
  tenant_id    uuid NOT NULL,
  task_id      uuid NOT NULL,
  run_id       uuid NOT NULL,                -- the run that proposed it
  goal         text NOT NULL CHECK (length(goal) BETWEEN 1 AND 2000),
  options      jsonb NOT NULL,               -- [{device_id, payee, amount, currency, summary, observation_id}]
  status       text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'chosen', 'declined')),
  chosen       smallint,                     -- index into options
  decided_by   uuid REFERENCES users (id),
  decided_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id),
  CHECK ((status = 'chosen') = (chosen IS NOT NULL))
);
CREATE INDEX purchase_intents_task ON purchase_intents (task_id, created_at);

ALTER TABLE purchase_intents ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON purchase_intents
  USING (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('witx.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON purchase_intents TO witx_app;

-- The purchase an order or payment input submits for.
ALTER TABLE actions ADD COLUMN purchase_intent_id uuid REFERENCES purchase_intents (id);
