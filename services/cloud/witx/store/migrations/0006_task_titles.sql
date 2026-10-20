-- A short title for each task, summarised from its first message by the worker.
-- Until it is there (or if summarising fails) the goal is shown.
ALTER TABLE tasks ADD COLUMN title text CHECK (length(title) BETWEEN 1 AND 80);
ALTER TABLE tasks ADD COLUMN title_tries smallint NOT NULL DEFAULT 0;
CREATE INDEX tasks_untitled ON tasks (created_at) WHERE title IS NULL AND deleted_at IS NULL;
