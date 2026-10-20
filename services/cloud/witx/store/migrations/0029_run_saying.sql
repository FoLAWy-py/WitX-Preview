-- What the agent is saying right now: its words as the model produces them, kept while a
-- step's model call streams and cleared once the step is applied. The reasoning
-- summary that this column held for a day is not shown any more: it came in one burst at
-- the end of the model's thinking, a second before the step moved on.
ALTER TABLE runs RENAME COLUMN thinking TO saying;
