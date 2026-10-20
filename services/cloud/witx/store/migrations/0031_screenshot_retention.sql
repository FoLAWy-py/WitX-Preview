-- Each person chooses, in each organisation, how long the screenshots of the tasks they
-- start are kept once the task has finished: 1 to 30 days. NULL: the deployment's
-- setting. The deployment's setting is also the most anyone may choose. Like auto
-- memory, only this column may be updated by the application role, and the API sets it
-- for the signed-in person only.
ALTER TABLE memberships ADD COLUMN screenshot_retention_days integer
  CHECK (screenshot_retention_days BETWEEN 1 AND 30);
GRANT UPDATE (screenshot_retention_days) ON memberships TO witx_app;
