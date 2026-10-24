BEGIN;

ALTER TABLE core.shadow_orders
    ADD COLUMN IF NOT EXISTS origin_strategy_id varchar(80) NULL,
    ADD COLUMN IF NOT EXISTS legacy_mode varchar(32) NULL,
    ADD COLUMN IF NOT EXISTS shadow_profile_id varchar(80) NULL;

CREATE INDEX IF NOT EXISTS idx_shadow_orders_origin_strategy
    ON core.shadow_orders USING btree (origin_strategy_id);

CREATE INDEX IF NOT EXISTS idx_shadow_orders_legacy_mode
    ON core.shadow_orders USING btree (legacy_mode);

COMMIT;
