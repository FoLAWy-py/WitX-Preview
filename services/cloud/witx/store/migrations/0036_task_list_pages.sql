-- A task list pages from where the last task shown sorts: its updated_at, then its id
-- (ties). Both indexes end in id, so the next page starts in the index without a sort:
-- one for a group's or the ungrouped tasks, one for the lists across groups (search,
-- tasks that wait for someone).
DROP INDEX tasks_group_recent;
CREATE INDEX tasks_group_recent ON tasks (tenant_id, group_id, updated_at DESC, id DESC)
  WHERE deleted_at IS NULL;
CREATE INDEX tasks_recent ON tasks (tenant_id, updated_at DESC, id DESC)
  WHERE deleted_at IS NULL;
