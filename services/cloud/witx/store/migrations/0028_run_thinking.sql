-- What the agent is thinking right now, as the model's own summary of its reasoning
-- so far, kept while a step's model call streams and cleared when it ends. The
-- API's event stream sends it to whoever watches the task; it is never an event.
ALTER TABLE runs ADD COLUMN thinking text;
