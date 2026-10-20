-- Plan mode: the user wants a plan before any action. While it is on, the agent can
-- look but its inputs are refused at admission; the user's approval turns it off.
ALTER TABLE tasks ADD COLUMN plan_mode boolean NOT NULL DEFAULT false;
