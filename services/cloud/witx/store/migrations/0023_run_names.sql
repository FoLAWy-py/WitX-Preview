-- A sub-agent's name, given by the agent that delegates to it ("iOS Agent", "Reviewer"),
-- so the person can tell several apart. The main agent and older runs have none.
ALTER TABLE runs ADD COLUMN name text CHECK (name IS NULL OR char_length(name) BETWEEN 1 AND 40);
