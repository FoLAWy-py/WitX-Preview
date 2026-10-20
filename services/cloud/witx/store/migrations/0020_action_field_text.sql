-- What the focused field held after the agent typed into it: the Gateway reads
-- it back so a lost or changed character is caught before the agent goes on. NULL when
-- it could not be read (a password field is never read) or for other inputs.
ALTER TABLE actions ADD COLUMN field_text text;
