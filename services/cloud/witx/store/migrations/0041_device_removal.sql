-- An owner removes a device that is gone: its row stays, as the screenshots,
-- inputs and history of the tasks that used it refer to it, but it is disabled and no
-- longer listed. If its Gateway reports it again, it comes back, waiting to be enabled.
ALTER TABLE devices ADD COLUMN removed_at timestamptz;

ALTER TABLE device_changes DROP CONSTRAINT device_changes_change_check;
ALTER TABLE device_changes ADD CONSTRAINT device_changes_change_check
  CHECK (change IN ('enabled', 'disabled', 'taken_over', 'removed'));
