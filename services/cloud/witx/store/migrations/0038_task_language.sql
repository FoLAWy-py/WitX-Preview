-- The language the task's first message is written in, named in English by the model
-- that titles the task: the agent is told it, so it does not have to guess.
ALTER TABLE tasks ADD COLUMN language text;
