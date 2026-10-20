-- Which field the typed text went into: its name as the app gives it and where it
-- is on the screen ({"name": ..., "box": [x, y, width, height]}). NULL when it could not
-- be read. The agent is told; later the box can be checked against where it tapped.
ALTER TABLE actions ADD COLUMN field_info jsonb;
