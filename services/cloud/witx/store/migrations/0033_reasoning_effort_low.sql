-- A model may let a person choose low effort too; medium stays the default.
ALTER TABLE tasks DROP CONSTRAINT tasks_reasoning_effort_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_reasoning_effort_check
  CHECK (reasoning_effort IN ('low', 'medium', 'high', 'xhigh', 'max'));
ALTER TABLE schedules DROP CONSTRAINT schedules_reasoning_effort_check;
ALTER TABLE schedules ADD CONSTRAINT schedules_reasoning_effort_check
  CHECK (reasoning_effort IN ('low', 'medium', 'high', 'xhigh', 'max'));
