-- The user may pause a task's goal (/goal pause) and resume it: while paused the agent
-- does what the messages ask and the goal's time does not run.
ALTER TABLE tasks ADD COLUMN goal_paused boolean NOT NULL DEFAULT false;
