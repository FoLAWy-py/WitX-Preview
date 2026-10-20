-- The independent check of a run's completion: a separate model call that compares
-- the agent's summary with the screenshots it cites. Kept with the step that completed,
-- like the risk check of an input, and counted in the task's usage.
ALTER TABLE steps ADD COLUMN verify_call jsonb;
