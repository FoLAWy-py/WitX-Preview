-- What the agent says an input with effects changes, in a few words ("plant cherries",
-- "reminder: Mon 9 am"). The step's effect label in the timeline names it; the
-- full expected effect stays in expected_effect. NULL when the agent did not say.
ALTER TABLE actions ADD COLUMN changes text
  CHECK (changes IS NULL OR char_length(changes) BETWEEN 1 AND 40);
