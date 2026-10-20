-- Auto memory (D59, after Claude Code): the agent keeps what it learns for the person's
-- future tasks itself; the person sees each entry and can undo it, delete it, or turn
-- this off. Three kinds: the person's preferences ('user'), how they corrected or
-- confirmed the agent ('feedback'), and how an app works ('app', with its package).
ALTER TABLE user_memories ADD COLUMN kind text NOT NULL DEFAULT 'user'
  CHECK (kind IN ('user', 'feedback', 'app'));
ALTER TABLE user_memories ADD COLUMN app text
  CHECK (app IS NULL OR char_length(app) BETWEEN 1 AND 200);
ALTER TABLE user_memories ADD CONSTRAINT user_memories_app_kind_check
  CHECK ((kind = 'app') = (app IS NOT NULL));
-- Kept by an agent on its own (with auto memory off, only the person's own are used;
-- the task an entry came from may be deleted).
ALTER TABLE user_memories ADD COLUMN by_agent boolean NOT NULL DEFAULT false;
CREATE INDEX user_memories_app ON user_memories (user_id, app) WHERE app IS NOT NULL;
-- Proposals from before were never kept: they stay out.
UPDATE user_memories SET status = 'declined', decided_at = now() WHERE status = 'proposed';

-- Each person decides, in each organisation, whether the agent remembers on its own.
-- Like the tenant settings, only this column may be updated by the application role;
-- the API sets it for the signed-in person only.
ALTER TABLE memberships ADD COLUMN auto_memory boolean NOT NULL DEFAULT true;
GRANT UPDATE (auto_memory) ON memberships TO witx_app;
