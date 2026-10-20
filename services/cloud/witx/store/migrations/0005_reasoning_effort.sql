-- How hard the agent thinks, chosen per task (and per schedule). Never below medium.
ALTER TABLE tasks ADD COLUMN reasoning_effort text NOT NULL DEFAULT 'medium'
  CHECK (reasoning_effort IN ('medium', 'high', 'xhigh', 'max'));
ALTER TABLE schedules ADD COLUMN reasoning_effort text NOT NULL DEFAULT 'medium'
  CHECK (reasoning_effort IN ('medium', 'high', 'xhigh', 'max'));
