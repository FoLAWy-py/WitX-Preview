-- An input whose effect is unknown keeps its tool call open: the run waits for the
-- person's ruling on it, which is the input's result.
ALTER TABLE runs DROP CONSTRAINT runs_wait_reason_check;
ALTER TABLE runs ADD CONSTRAINT runs_wait_reason_check CHECK (wait_reason IN ('observation',
  'action', 'approval', 'user_input', 'children', 'device', 'model_retry', 'paused', 'agent',
  'unknown'));
