-- A person may take over a phone that no task holds: the lease then has no task
-- and no run, only the takeover, and it is deleted when the takeover ends.
ALTER TABLE device_leases ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE device_leases ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE device_leases ADD CHECK ((task_id IS NULL) = (run_id IS NULL));
ALTER TABLE device_leases ADD CHECK (task_id IS NOT NULL OR takeover_id IS NOT NULL);

-- Who took over which phone outside a task, next to who enabled and disabled it.
ALTER TABLE device_changes DROP CONSTRAINT device_changes_change_check;
ALTER TABLE device_changes ADD CHECK (change IN ('enabled', 'disabled', 'taken_over'));
