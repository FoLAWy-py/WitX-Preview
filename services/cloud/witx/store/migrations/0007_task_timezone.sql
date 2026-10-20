-- The time zone of the person who started the task (their browser's), for "tomorrow"
-- and the schedules the agent proposes. Tasks a schedule starts take the schedule's.
ALTER TABLE tasks ADD COLUMN timezone text NOT NULL DEFAULT 'UTC';
