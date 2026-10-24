-- System-level flip guard state.
-- This table is the production persistence layer for flat/open history used to
-- block same-bar reversals and immediate reopen loops across process restarts.

CREATE TABLE IF NOT EXISTS core.core_flip_guard (
    root_symbol text NOT NULL,
    action text NOT NULL,
    symbol text NULL,
    side text NULL,
    strategy_id text NULL,
    bar_id text NULL,
    ts_epoch_s double precision NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT core_flip_guard_pkey PRIMARY KEY (root_symbol, action),
    CONSTRAINT core_flip_guard_action_check CHECK (action IN ('entry', 'flat'))
);

CREATE INDEX IF NOT EXISTS core_flip_guard_updated_idx
    ON core.core_flip_guard (updated_at DESC);
