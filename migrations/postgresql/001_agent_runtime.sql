CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    goal_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_plan_steps (
    id TEXT NOT NULL,
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    capability TEXT NOT NULL,
    arguments_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    dependencies_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL,
    result_json JSONB,
    error TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, id)
);

CREATE TABLE IF NOT EXISTS agent_observations (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    observation_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS action_proposals (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    capability TEXT NOT NULL,
    arguments_json JSONB NOT NULL,
    approved_arguments_json JSONB,
    status TEXT NOT NULL DEFAULT 'pending',
    expires_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    step_id TEXT,
    storage_key TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    size BIGINT NOT NULL,
    checksum TEXT NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation ON agent_runs(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_plan_steps(run_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_observations_run ON agent_observations(run_id, step_id);
