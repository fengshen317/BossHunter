CREATE TABLE IF NOT EXISTS assistant_conversation_contexts (
    conversation_id TEXT PRIMARY KEY REFERENCES assistant_conversations(id) ON DELETE CASCADE,
    summary_text TEXT NOT NULL DEFAULT '',
    summary_token_count INTEGER NOT NULL DEFAULT 0,
    summarized_through_message_id TEXT,
    pending_digest_json TEXT NOT NULL DEFAULT '[]',
    pending_intent_json TEXT NOT NULL DEFAULT '{}',
    active_goal_json TEXT NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE assistant_memories ADD COLUMN IF NOT EXISTS memory_key TEXT;
ALTER TABLE assistant_memories ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'profile';
ALTER TABLE assistant_memories ADD COLUMN IF NOT EXISTS value_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE assistant_memories ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'explicit';
ALTER TABLE assistant_memories ADD COLUMN IF NOT EXISTS source_conversation_id TEXT;
ALTER TABLE assistant_memories ADD COLUMN IF NOT EXISTS source_message_id TEXT;
ALTER TABLE assistant_memories ADD COLUMN IF NOT EXISTS source_excerpt TEXT NOT NULL DEFAULT '';
ALTER TABLE assistant_memories ADD COLUMN IF NOT EXISTS supersedes_id TEXT;

UPDATE assistant_memories
SET memory_key = 'legacy.' || id
WHERE memory_key IS NULL OR memory_key = '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_assistant_memories_active_key
ON assistant_memories(user_id, memory_key)
WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_assistant_messages_conversation_cursor
ON assistant_messages(conversation_id, created_at, id);
