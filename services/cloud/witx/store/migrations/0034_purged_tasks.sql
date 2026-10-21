-- Tasks purged before the purge cleared titles and pins still had them on their
-- tombstones; the purge now clears both.
UPDATE tasks SET title = NULL WHERE purged_at IS NOT NULL;
DELETE FROM task_pins p USING tasks t WHERE t.id = p.task_id AND t.purged_at IS NOT NULL;
