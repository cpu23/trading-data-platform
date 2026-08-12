-- Durable admission reservations for the UTC-day LLM budget.
-- A paid call reserves an estimated cost before dispatch; the sum of
-- unreserved recorded spend plus active reservation estimates plus settled
-- reservation actuals (anchored to their reservation day) must fit under the
-- daily cap. Reservations settle with actual cost, or expire after their TTL
-- and release their estimate. Provenance (correlation, run kind, component,
-- requestor) is retained for audit, and lifecycle invariants are enforced by
-- CHECK constraints. Additive and idempotent.
-- Rollback: drop the table.

CREATE TABLE IF NOT EXISTS budget_reservations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    budget_day DATE NOT NULL,
    correlation_id UUID,
    run_kind TEXT,
    component TEXT,
    processor TEXT NOT NULL,
    requested_by TEXT,
    reason TEXT,
    estimated_usd NUMERIC(12, 6) NOT NULL,
    settled_usd NUMERIC(12, 6),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'settled', 'expired', 'released')),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    settled_at TIMESTAMPTZ,
    CONSTRAINT budget_reservations_estimate_positive CHECK (estimated_usd > 0),
    CONSTRAINT budget_reservations_settle_nonnegative CHECK (
        settled_usd IS NULL OR settled_usd >= 0
    ),
    CONSTRAINT budget_reservations_expiry_after_reservation CHECK (
        expires_at > reserved_at
    ),
    CONSTRAINT budget_reservations_processor_nonblank CHECK (
        length(trim(processor)) > 0
    ),
    CONSTRAINT budget_reservations_component_nonblank CHECK (
        component IS NULL OR length(trim(component)) > 0
    ),
    CONSTRAINT budget_reservations_run_kind_nonblank CHECK (
        run_kind IS NULL OR length(trim(run_kind)) > 0
    ),
    CONSTRAINT budget_reservations_active_unsettled CHECK (
        status <> 'active' OR (settled_usd IS NULL AND settled_at IS NULL)
    ),
    CONSTRAINT budget_reservations_settled_complete CHECK (
        status <> 'settled' OR (settled_usd IS NOT NULL AND settled_at IS NOT NULL)
    ),
    CONSTRAINT budget_reservations_released_complete CHECK (
        status <> 'released' OR (settled_usd = 0 AND settled_at IS NOT NULL)
    ),
    CONSTRAINT budget_reservations_expired_unsettled CHECK (
        status <> 'expired' OR (settled_usd IS NULL AND settled_at IS NULL)
    ),
    CONSTRAINT budget_reservations_settle_after_reserve CHECK (
        settled_at IS NULL OR settled_at >= reserved_at
    ),
    CONSTRAINT budget_reservations_day_matches_reservation CHECK (
        budget_day = (reserved_at AT TIME ZONE 'UTC')::date
    )
);

CREATE INDEX IF NOT EXISTS idx_budget_reservations_day_status
    ON budget_reservations (budget_day, status, expires_at);

CREATE INDEX IF NOT EXISTS idx_budget_reservations_correlation
    ON budget_reservations (correlation_id);
