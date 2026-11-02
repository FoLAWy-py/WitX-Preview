-- find_app is gone: launch_app opens an app by its names. A search only read
-- the installed apps and changed nothing on a phone, so the old searches go with it, and
-- no phone declares it any more.
DELETE FROM actions WHERE kind = 'find_app';
UPDATE devices SET capabilities = array_remove(capabilities, 'find_app');
