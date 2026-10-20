-- A note the system writes on the person's behalf (a takeover ended, a purchase was
-- chosen, the phones changed, an action was checked): the agent reads it like theirs,
-- but it is not the person's message to withdraw or edit.
ALTER TABLE task_inputs DROP CONSTRAINT task_inputs_kind_check;
ALTER TABLE task_inputs ADD CONSTRAINT task_inputs_kind_check
  CHECK (kind IN ('goal', 'constraint', 'note', 'agent', 'system'));
