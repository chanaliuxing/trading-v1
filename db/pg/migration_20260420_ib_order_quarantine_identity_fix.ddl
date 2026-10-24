-- Fix active quarantine identity semantics:
-- 1) add missing quarantine columns expected by runtime code
-- 2) drop the old active-per-symbol uniqueness that collapses multiple orphans
-- 3) dedupe active quarantine rows by broker order identity instead

ALTER TABLE core.core_order_quarantine
    ADD COLUMN IF NOT EXISTS ib_perm_id int8 NULL,
    ADD COLUMN IF NOT EXISTS ib_client_id int4 NULL,
    ADD COLUMN IF NOT EXISTS observed_at timestamptz NOT NULL DEFAULT now();

DROP INDEX IF EXISTS core.core_order_quarantine_active_symbol_uk;
DROP INDEX IF EXISTS core_order_quarantine_active_symbol_uk;

CREATE INDEX IF NOT EXISTS core_order_quarantine_symbol_status_idx
    ON core.core_order_quarantine (symbol, status);

CREATE UNIQUE INDEX IF NOT EXISTS core_order_quarantine_active_broker_order_id_uk
    ON core.core_order_quarantine (broker_order_id)
    WHERE status = 'active' AND broker_order_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS core_order_quarantine_active_ib_perm_id_uk
    ON core.core_order_quarantine (ib_perm_id)
    WHERE status = 'active' AND ib_perm_id IS NOT NULL;
