-- What the person asked for, their first message, apart from the goal: most tasks are
-- a plain request and have no goal; a goal is for complex or long work (set by the
-- user with /goal, or once by the agent), tracked until it is met. Tasks made before
-- this kept their first message as the goal; both stay as they were.
ALTER TABLE tasks ADD COLUMN request text NOT NULL DEFAULT '';
UPDATE tasks SET request = goal;
