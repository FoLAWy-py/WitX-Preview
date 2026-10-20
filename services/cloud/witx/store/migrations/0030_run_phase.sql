-- What the agent's step is at right now, beside its words: talking, planning the
-- next input, assessing its risk, checking its summary, or rejected (the check refused
-- the summary and the words stay up while it works on). NULL between steps.
ALTER TABLE runs ADD COLUMN phase text;
