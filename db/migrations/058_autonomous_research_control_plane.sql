-- Persistent autonomous research organization: additive control-plane schema.
--
-- Coordination remains in PostgreSQL. Research work orders extend the existing
-- analysis_jobs lease engine instead of creating a second worker lease. Every
-- accepted planner decision, skill version, effect and outcome attribution is
-- inspectable after process restarts.
--
-- Rollback (dependency-safe): drop scorecard views; drop append-only/transition
-- triggers and functions; then drop outcome attributions, effects, source gaps,
-- dependency edges/nodes, work orders, plan decisions/plans, source
-- capabilities, skill versions and questions. Never drop analysis_jobs,
-- budget_reservations, market_events, thesis/forecast tables or their data.

-- ---------------------------------------------------------------------------
-- 1. Atomic durable research-question ledger.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS research_questions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fingerprint TEXT NOT NULL,
    question_key TEXT NOT NULL,
    origin_kind TEXT NOT NULL,
    question_type TEXT NOT NULL,
    atomic_question TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    accepted_cutoff TIMESTAMPTZ NOT NULL,
    required_evidence_shape JSONB NOT NULL DEFAULT '{}'::JSONB,
    acceptable_source_families TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    materiality NUMERIC(12, 9),
    uncertainty NUMERIC(12, 9),
    discrimination_power NUMERIC(12, 9),
    urgency NUMERIC(12, 9),
    freshness_gap NUMERIC(12, 9),
    resolvability NUMERIC(12, 9),
    estimated_cost_usd NUMERIC(12, 6),
    estimated_runtime_seconds INTEGER,
    expected_human_review_minutes NUMERIC(12, 3),
    priority_policy_version TEXT NOT NULL,
    priority_score NUMERIC(30, 12),
    priority_blockers TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    due_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    dirty_since TIMESTAMPTZ,
    latest_source_event_id UUID REFERENCES market_events (id) ON DELETE SET NULL,
    resolution_evidence_refs TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    resolution_summary TEXT,
    unresolved_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    CONSTRAINT research_questions_fingerprint_check
        CHECK (
            fingerprint ~ '^[0-9a-f]{64}$'
            AND question_key ~ '^[0-9a-f]{64}$'
        ),
    CONSTRAINT research_questions_origin_check
        CHECK (origin_kind IN (
            'promoted_candidate', 'falsification', 'stale_dependency',
            'catalyst_confirmation', 'forecast_resolution', 'source_event',
            'source_gap', 'manual'
        )),
    CONSTRAINT research_questions_type_check
        CHECK (question_type IN (
            'earnings_guidance_delta', 'filing_peer_readthrough',
            'positioning_divergence', 'thesis_challenge',
            'forecast_resolution', 'catalyst_confirmation',
            'evidence_refresh', 'source_gap'
        )),
    CONSTRAINT research_questions_target_kind_check
        CHECK (target_kind IN (
            'thesis', 'group', 'forecast', 'catalyst', 'entity', 'source'
        )),
    CONSTRAINT research_questions_text_bounds_check
        CHECK (
            LENGTH(BTRIM(atomic_question)) BETWEEN 1 AND 2000
            AND LENGTH(BTRIM(target_ref)) BETWEEN 1 AND 500
            AND LENGTH(priority_policy_version) BETWEEN 1 AND 64
        ),
    CONSTRAINT research_questions_evidence_shape_check
        CHECK (JSONB_TYPEOF(required_evidence_shape) = 'object'),
    CONSTRAINT research_questions_source_family_bound_check
        CHECK (CARDINALITY(acceptable_source_families) <= 32),
    CONSTRAINT research_questions_components_check
        CHECK (
            (materiality IS NULL OR materiality BETWEEN 0 AND 1)
            AND (uncertainty IS NULL OR uncertainty BETWEEN 0 AND 1)
            AND (discrimination_power IS NULL OR discrimination_power BETWEEN 0 AND 1)
            AND (urgency IS NULL OR urgency BETWEEN 0 AND 1)
            AND (freshness_gap IS NULL OR freshness_gap BETWEEN 0 AND 1)
            AND (resolvability IS NULL OR resolvability BETWEEN 0 AND 1)
        ),
    CONSTRAINT research_questions_estimates_check
        CHECK (
            (estimated_cost_usd IS NULL OR (
                estimated_cost_usd <> 'NaN'::NUMERIC
                AND estimated_cost_usd BETWEEN 0 AND 100
            ))
            AND (estimated_runtime_seconds IS NULL OR estimated_runtime_seconds BETWEEN 0 AND 86400)
            AND (expected_human_review_minutes IS NULL OR (
                expected_human_review_minutes <> 'NaN'::NUMERIC
                AND expected_human_review_minutes BETWEEN 0 AND 1440
            ))
            AND (priority_score IS NULL OR (
                priority_score <> 'NaN'::NUMERIC AND priority_score >= 0
            ))
        ),
    CONSTRAINT research_questions_collection_bounds_check
        CHECK (
            CARDINALITY(priority_blockers) <= 32
            AND CARDINALITY(resolution_evidence_refs) <= 256
        ),
    CONSTRAINT research_questions_status_check
        CHECK (status IN (
            'pending', 'planned', 'queued', 'running', 'resolved',
            'unresolvable', 'expired', 'cancelled'
        )),
    CONSTRAINT research_questions_attempt_bound_check
        CHECK (attempt_count BETWEEN 0 AND 100),
    CONSTRAINT research_questions_time_order_check
        CHECK (
            updated_at >= created_at
            AND (due_at IS NULL OR expires_at IS NULL OR expires_at >= due_at)
            AND (resolved_at IS NULL OR resolved_at >= created_at)
        ),
    CONSTRAINT research_questions_resolution_bounds_check
        CHECK (
            (resolution_summary IS NULL OR LENGTH(resolution_summary) <= 4000)
            AND (unresolved_reason IS NULL OR LENGTH(unresolved_reason) <= 1000)
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_questions_active_fingerprint
    ON research_questions (fingerprint)
    WHERE status IN ('pending', 'planned', 'queued', 'running');
CREATE INDEX IF NOT EXISTS idx_research_questions_active_key
    ON research_questions (question_key, accepted_cutoff DESC, id)
    WHERE status IN ('pending', 'planned', 'queued', 'running');
CREATE INDEX IF NOT EXISTS idx_research_questions_planner
    ON research_questions (
        priority_score DESC NULLS LAST, not_before, due_at, created_at, id
    )
    WHERE status IN ('pending', 'planned');
CREATE INDEX IF NOT EXISTS idx_research_questions_target
    ON research_questions (target_kind, target_ref, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_questions_list_status
    ON research_questions (status, id);
CREATE INDEX IF NOT EXISTS idx_research_questions_list_type
    ON research_questions (question_type, id);
CREATE INDEX IF NOT EXISTS idx_research_questions_list_target
    ON research_questions (target_kind, target_ref, id);
CREATE INDEX IF NOT EXISTS idx_research_questions_expiry
    ON research_questions (expires_at, id)
    WHERE status IN ('pending', 'planned', 'queued', 'running')
      AND expires_at IS NOT NULL;

-- Bounded operational activity probes used by the live topology.  Expression
-- indexes match the exact persisted activity timestamp semantics so MAX probes
-- do not scan complete durable ledgers as their history grows.
CREATE INDEX IF NOT EXISTS idx_analysis_jobs_activity
    ON analysis_jobs ((COALESCE(completed_at, started_at, created_at)) DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_jobs_type_activity
    ON analysis_jobs (
        job_type, (COALESCE(completed_at, started_at, created_at)) DESC
    );
CREATE INDEX IF NOT EXISTS idx_market_events_ingested
    ON market_events (ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_outbox_activity
    ON event_outbox (
        (COALESCE(completed_at, claimed_at, created_at)) DESC
    );
CREATE INDEX IF NOT EXISTS idx_ui_events_created
    ON ui_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_questions_updated
    ON research_questions (updated_at DESC);

-- Exact normalized predicates used by the positioning-divergence skill.  The
-- collectors persist uppercase symbols/market IDs and lowercase source IDs.
CREATE INDEX IF NOT EXISTS idx_option_snapshot_features_symbol_captured
    ON option_snapshot_features (symbol, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_positioning_reports_market_report
    ON positioning_reports (market_id, report_date DESC, source, category);

CREATE OR REPLACE FUNCTION guard_research_question_lifecycle()
RETURNS TRIGGER AS $$
DECLARE
    old_terminal BOOLEAN;
    transition_allowed BOOLEAN := FALSE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'research questions are retained for audit';
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
       OR NEW.question_key IS DISTINCT FROM OLD.question_key
       OR NEW.origin_kind IS DISTINCT FROM OLD.origin_kind
       OR NEW.question_type IS DISTINCT FROM OLD.question_type
       OR NEW.atomic_question IS DISTINCT FROM OLD.atomic_question
       OR NEW.target_kind IS DISTINCT FROM OLD.target_kind
       OR NEW.target_ref IS DISTINCT FROM OLD.target_ref
       OR NEW.accepted_cutoff IS DISTINCT FROM OLD.accepted_cutoff
       OR NEW.required_evidence_shape IS DISTINCT FROM OLD.required_evidence_shape
       OR NEW.acceptable_source_families IS DISTINCT FROM OLD.acceptable_source_families
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'research question identity is immutable';
    END IF;


    old_terminal := OLD.status IN ('resolved', 'unresolvable', 'expired', 'cancelled');
    IF old_terminal AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'terminal research question cannot return to active state';
    END IF;

    IF NEW.status IN ('planned', 'queued', 'running')
       AND NEW.status IS DISTINCT FROM OLD.status
       AND NEW.expires_at IS NOT NULL
       AND NEW.expires_at <= NOW() THEN
        RAISE EXCEPTION 'expired research question cannot enter active work';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        transition_allowed :=
            (OLD.status = 'pending' AND NEW.status IN (
                'planned', 'unresolvable', 'expired', 'cancelled'
            )) OR
            (OLD.status = 'planned' AND NEW.status IN (
                'pending', 'queued', 'unresolvable', 'expired', 'cancelled'
            )) OR
            (OLD.status = 'queued' AND NEW.status IN (
                'planned', 'running', 'unresolvable', 'expired', 'cancelled'
            )) OR
            (OLD.status = 'running' AND NEW.status IN (
                'planned', 'resolved', 'unresolvable', 'expired', 'cancelled'
            ));
        IF NOT transition_allowed THEN
            RAISE EXCEPTION 'invalid research question transition: % -> %', OLD.status, NEW.status;
        END IF;
    END IF;

    NEW.updated_at := NOW();
    IF NEW.status IN ('resolved', 'unresolvable', 'expired', 'cancelled') THEN
        NEW.resolved_at := COALESCE(NEW.resolved_at, NOW());
        IF NEW.status = 'resolved'
           AND (NEW.resolution_summary IS NULL OR BTRIM(NEW.resolution_summary) = '') THEN
            RAISE EXCEPTION 'resolved research question requires a bounded summary';
        END IF;
        IF NEW.status <> 'resolved'
           AND (NEW.unresolved_reason IS NULL OR BTRIM(NEW.unresolved_reason) = '') THEN
            RAISE EXCEPTION 'unresolved terminal research question requires a reason';
        END IF;
    ELSIF NEW.resolved_at IS NOT NULL THEN
        RAISE EXCEPTION 'active research question cannot have resolved_at';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS research_questions_lifecycle ON research_questions;
CREATE TRIGGER research_questions_lifecycle
BEFORE UPDATE OR DELETE ON research_questions
FOR EACH ROW EXECUTE FUNCTION guard_research_question_lifecycle();

-- ---------------------------------------------------------------------------
-- 2. Immutable, typed, versioned production skill registry.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS research_skill_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    supported_question_types TEXT[] NOT NULL,
    input_schema JSONB NOT NULL,
    output_schema JSONB NOT NULL,
    allowed_tools TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    allowed_source_families TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    point_in_time_requirements JSONB NOT NULL,
    model_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    model_policy JSONB NOT NULL DEFAULT '{}'::JSONB,
    maximum_cost_usd NUMERIC(12, 6) NOT NULL,
    maximum_runtime_seconds INTEGER NOT NULL,
    maximum_attempts INTEGER NOT NULL,
    validators TEXT[] NOT NULL,
    promotion_status TEXT NOT NULL DEFAULT 'draft',
    content_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_at TIMESTAMPTZ,
    CONSTRAINT research_skill_versions_identity_unique UNIQUE (skill_key, version),
    CONSTRAINT research_skill_versions_key_check
        CHECK (skill_key ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
    CONSTRAINT research_skill_versions_version_check CHECK (version >= 1),
    CONSTRAINT research_skill_versions_fingerprint_check
        CHECK (content_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT research_skill_versions_schema_check
        CHECK (
            JSONB_TYPEOF(input_schema) = 'object'
            AND JSONB_TYPEOF(output_schema) = 'object'
            AND JSONB_TYPEOF(point_in_time_requirements) = 'object'
            AND JSONB_TYPEOF(model_policy) = 'object'
        ),
    CONSTRAINT research_skill_versions_collection_bounds_check
        CHECK (
            CARDINALITY(supported_question_types) BETWEEN 1 AND 16
            AND CARDINALITY(allowed_tools) <= 16
            AND CARDINALITY(allowed_source_families) <= 32
            AND CARDINALITY(validators) BETWEEN 1 AND 32
        ),
    CONSTRAINT research_skill_versions_limits_check
        CHECK (
            maximum_cost_usd <> 'NaN'::NUMERIC
            AND maximum_cost_usd BETWEEN 0 AND 100
            AND maximum_runtime_seconds BETWEEN 1 AND 86400
            AND maximum_attempts BETWEEN 1 AND 20
        ),
    CONSTRAINT research_skill_versions_promotion_check
        CHECK (promotion_status IN ('draft', 'active', 'deprecated', 'disabled')),
    CONSTRAINT research_skill_versions_promotion_time_check
        CHECK (
            (promotion_status = 'draft' AND promoted_at IS NULL)
            OR (promotion_status <> 'draft' AND promoted_at IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_research_skill_versions_active
    ON research_skill_versions (skill_key, version DESC)
    WHERE promotion_status = 'active';

CREATE OR REPLACE FUNCTION guard_research_skill_version()
RETURNS TRIGGER AS $$
DECLARE
    transition_allowed BOOLEAN := FALSE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1 FROM research_work_orders WHERE skill_version_id = OLD.id LIMIT 1
        ) THEN
            RAISE EXCEPTION 'skill version used by a work order is immutable';
        END IF;
        RETURN OLD;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.skill_key IS DISTINCT FROM OLD.skill_key
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.supported_question_types IS DISTINCT FROM OLD.supported_question_types
       OR NEW.input_schema IS DISTINCT FROM OLD.input_schema
       OR NEW.output_schema IS DISTINCT FROM OLD.output_schema
       OR NEW.allowed_tools IS DISTINCT FROM OLD.allowed_tools
       OR NEW.allowed_source_families IS DISTINCT FROM OLD.allowed_source_families
       OR NEW.point_in_time_requirements IS DISTINCT FROM OLD.point_in_time_requirements
       OR NEW.model_allowed IS DISTINCT FROM OLD.model_allowed
       OR NEW.model_policy IS DISTINCT FROM OLD.model_policy
       OR NEW.maximum_cost_usd IS DISTINCT FROM OLD.maximum_cost_usd
       OR NEW.maximum_runtime_seconds IS DISTINCT FROM OLD.maximum_runtime_seconds
       OR NEW.maximum_attempts IS DISTINCT FROM OLD.maximum_attempts
       OR NEW.validators IS DISTINCT FROM OLD.validators
       OR NEW.content_fingerprint IS DISTINCT FROM OLD.content_fingerprint
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'skill version content is immutable; insert a new version';
    END IF;

    IF NEW.promotion_status IS DISTINCT FROM OLD.promotion_status THEN
        transition_allowed :=
            (OLD.promotion_status = 'draft' AND NEW.promotion_status IN ('active', 'disabled')) OR
            (OLD.promotion_status = 'active' AND NEW.promotion_status IN ('deprecated', 'disabled')) OR
            (OLD.promotion_status = 'deprecated' AND NEW.promotion_status = 'disabled');
        IF NOT transition_allowed THEN
            RAISE EXCEPTION 'invalid skill promotion transition: % -> %', OLD.promotion_status, NEW.promotion_status;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The trigger is installed after research_work_orders exists so a clean
-- migration never resolves a not-yet-created relation in its function body.

-- ---------------------------------------------------------------------------
-- 3. Immutable planner agendas and explanations.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS research_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id UUID NOT NULL,
    trigger_kind TEXT NOT NULL,
    trigger_ref TEXT,
    accepted_cutoff TIMESTAMPTZ NOT NULL,
    priority_policy_version TEXT NOT NULL,
    materiality_policy_version TEXT NOT NULL,
    cost_budget_usd NUMERIC(12, 6) NOT NULL,
    runtime_budget_seconds INTEGER NOT NULL,
    minimum_priority NUMERIC(30, 12) NOT NULL,
    status TEXT NOT NULL DEFAULT 'planning',
    considered_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    deferred_count INTEGER NOT NULL DEFAULT 0,
    reserved_cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
    reserved_runtime_seconds INTEGER NOT NULL DEFAULT 0,
    no_op_reason TEXT,
    error_kind TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT research_plans_trigger_check
        CHECK (trigger_kind IN ('scheduled', 'event', 'manual', 'recovery')),
    CONSTRAINT research_plans_status_check
        CHECK (status IN ('planning', 'completed', 'noop', 'failed')),
    CONSTRAINT research_plans_budget_check
        CHECK (
            cost_budget_usd <> 'NaN'::NUMERIC
            AND cost_budget_usd BETWEEN 0 AND 100
            AND runtime_budget_seconds BETWEEN 1 AND 86400
            AND minimum_priority <> 'NaN'::NUMERIC
            AND minimum_priority >= 0
            AND reserved_cost_usd <> 'NaN'::NUMERIC
            AND reserved_cost_usd BETWEEN 0 AND cost_budget_usd
            AND reserved_runtime_seconds BETWEEN 0 AND runtime_budget_seconds
        ),
    CONSTRAINT research_plans_count_check
        CHECK (
            considered_count BETWEEN 0 AND 1000
            AND selected_count BETWEEN 0 AND considered_count
            AND blocked_count BETWEEN 0 AND considered_count
            AND deferred_count BETWEEN 0 AND considered_count
            AND selected_count + blocked_count + deferred_count <= considered_count
        ),
    CONSTRAINT research_plans_text_bounds_check
        CHECK (
            LENGTH(priority_policy_version) BETWEEN 1 AND 64
            AND LENGTH(materiality_policy_version) BETWEEN 1 AND 64
            AND (trigger_ref IS NULL OR LENGTH(trigger_ref) <= 500)
            AND (no_op_reason IS NULL OR LENGTH(no_op_reason) <= 1000)
            AND (error_kind IS NULL OR LENGTH(error_kind) <= 200)
        ),
    CONSTRAINT research_plans_completion_check
        CHECK (
            (status = 'planning' AND completed_at IS NULL)
            OR (status <> 'planning' AND completed_at IS NOT NULL)
        ),
    CONSTRAINT research_plans_noop_check
        CHECK (status <> 'noop' OR (selected_count = 0 AND LENGTH(BTRIM(no_op_reason)) > 0))
);

CREATE INDEX IF NOT EXISTS idx_research_plans_started
    ON research_plans (started_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS research_plan_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES research_plans (id) ON DELETE CASCADE,
    question_id UUID NOT NULL REFERENCES research_questions (id) ON DELETE RESTRICT,
    decision TEXT NOT NULL,
    rank INTEGER,
    priority_score NUMERIC(30, 12),
    blockers TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    reason_codes TEXT[] NOT NULL,
    priority_snapshot JSONB NOT NULL,
    estimated_cost_usd NUMERIC(12, 6),
    estimated_runtime_seconds INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_plan_decisions_identity_unique UNIQUE (plan_id, question_id),
    CONSTRAINT research_plan_decisions_decision_check
        CHECK (decision IN ('selected', 'deferred', 'blocked')),
    CONSTRAINT research_plan_decisions_rank_check
        CHECK ((decision = 'selected' AND rank IS NOT NULL AND rank >= 1) OR (decision <> 'selected' AND rank IS NULL)),
    CONSTRAINT research_plan_decisions_score_check
        CHECK (priority_score IS NULL OR (priority_score <> 'NaN'::NUMERIC AND priority_score >= 0)),
    CONSTRAINT research_plan_decisions_reason_bounds_check
        CHECK (
            CARDINALITY(blockers) <= 32
            AND CARDINALITY(reason_codes) BETWEEN 1 AND 32
            AND JSONB_TYPEOF(priority_snapshot) = 'object'
        ),
    CONSTRAINT research_plan_decisions_estimates_check
        CHECK (
            estimated_cost_usd IS NULL OR (
                estimated_cost_usd <> 'NaN'::NUMERIC
                AND estimated_cost_usd BETWEEN 0 AND 100
            )
        ),
    CONSTRAINT research_plan_decisions_runtime_check
        CHECK (estimated_runtime_seconds IS NULL OR estimated_runtime_seconds BETWEEN 0 AND 86400)
);

CREATE INDEX IF NOT EXISTS idx_research_plan_decisions_question
    ON research_plan_decisions (question_id, created_at DESC);

CREATE OR REPLACE FUNCTION guard_research_planning_snapshot()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'accepted research planning snapshots are append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS research_plans_append_only ON research_plans;
CREATE TRIGGER research_plans_append_only
BEFORE UPDATE OR DELETE ON research_plans
FOR EACH ROW EXECUTE FUNCTION guard_research_planning_snapshot();

DROP TRIGGER IF EXISTS research_plan_decisions_append_only ON research_plan_decisions;
CREATE TRIGGER research_plan_decisions_append_only
BEFORE UPDATE OR DELETE ON research_plan_decisions
FOR EACH ROW EXECUTE FUNCTION guard_research_planning_snapshot();

-- ---------------------------------------------------------------------------
-- 4. Work orders: research state anchored to existing durable analysis jobs.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS research_work_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id UUID NOT NULL REFERENCES research_questions (id) ON DELETE RESTRICT,
    plan_id UUID NOT NULL REFERENCES research_plans (id) ON DELETE RESTRICT,
    skill_version_id UUID NOT NULL REFERENCES research_skill_versions (id) ON DELETE RESTRICT,
    analysis_job_id UUID NOT NULL UNIQUE REFERENCES analysis_jobs (id) ON DELETE RESTRICT,
    budget_reservation_id UUID REFERENCES budget_reservations (id) ON DELETE SET NULL,
    accepted_cutoff TIMESTAMPTZ NOT NULL,
    planning_policy_version TEXT NOT NULL,
    priority_snapshot JSONB NOT NULL,
    estimated_value NUMERIC(30, 12),
    reserved_cost_usd NUMERIC(12, 6) NOT NULL,
    reserved_runtime_seconds INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    result JSONB,
    material_effect_summary TEXT,
    error_kind TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    queued_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_work_orders_exact_identity_unique
        UNIQUE (question_id, skill_version_id, accepted_cutoff, input_fingerprint),
    CONSTRAINT research_work_orders_fingerprint_check
        CHECK (input_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT research_work_orders_policy_check
        CHECK (LENGTH(planning_policy_version) BETWEEN 1 AND 64),
    CONSTRAINT research_work_orders_snapshot_check
        CHECK (JSONB_TYPEOF(priority_snapshot) = 'object'),
    CONSTRAINT research_work_orders_budget_check
        CHECK (
            reserved_cost_usd <> 'NaN'::NUMERIC
            AND reserved_cost_usd BETWEEN 0 AND 100
            AND reserved_runtime_seconds BETWEEN 1 AND 86400
            AND (estimated_value IS NULL OR (estimated_value <> 'NaN'::NUMERIC AND estimated_value >= 0))
        ),
    CONSTRAINT research_work_orders_status_check
        CHECK (status IN (
            'planned', 'queued', 'leased', 'running', 'completed',
            'failed_retryable', 'failed_terminal', 'cancelled', 'stale'
        )),
    CONSTRAINT research_work_orders_attempt_check CHECK (attempt_count BETWEEN 0 AND 20),
    CONSTRAINT research_work_orders_result_bounds_check
        CHECK (
            (result IS NULL OR JSONB_TYPEOF(result) = 'object')
            AND (material_effect_summary IS NULL OR LENGTH(material_effect_summary) <= 4000)
            AND (error_kind IS NULL OR LENGTH(error_kind) <= 200)
        ),
    CONSTRAINT research_work_orders_time_order_check
        CHECK (
            updated_at >= created_at
            AND (queued_at IS NULL OR queued_at >= created_at)
            AND (started_at IS NULL OR started_at >= created_at)
            AND (completed_at IS NULL OR completed_at >= created_at)
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_work_orders_active_question
    ON research_work_orders (question_id)
    WHERE status IN ('planned', 'queued', 'leased', 'running', 'failed_retryable');
CREATE INDEX IF NOT EXISTS idx_research_work_orders_status
    ON research_work_orders (status, created_at, id)
    WHERE status IN ('planned', 'queued', 'leased', 'running', 'failed_retryable');
CREATE INDEX IF NOT EXISTS idx_research_work_orders_plan
    ON research_work_orders (plan_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_research_work_orders_updated
    ON research_work_orders (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_work_orders_list_status
    ON research_work_orders (status, id);
CREATE INDEX IF NOT EXISTS idx_research_work_orders_list_question
    ON research_work_orders (question_id, id);

CREATE OR REPLACE FUNCTION guard_research_work_order_lifecycle()
RETURNS TRIGGER AS $$
DECLARE
    old_terminal BOOLEAN;
    transition_allowed BOOLEAN := FALSE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'research work orders are retained for audit';
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.question_id IS DISTINCT FROM OLD.question_id
       OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
       OR NEW.skill_version_id IS DISTINCT FROM OLD.skill_version_id
       OR NEW.analysis_job_id IS DISTINCT FROM OLD.analysis_job_id
       OR NEW.budget_reservation_id IS DISTINCT FROM OLD.budget_reservation_id
       OR NEW.accepted_cutoff IS DISTINCT FROM OLD.accepted_cutoff
       OR NEW.planning_policy_version IS DISTINCT FROM OLD.planning_policy_version
       OR NEW.priority_snapshot IS DISTINCT FROM OLD.priority_snapshot
       OR NEW.estimated_value IS DISTINCT FROM OLD.estimated_value
       OR NEW.reserved_cost_usd IS DISTINCT FROM OLD.reserved_cost_usd
       OR NEW.reserved_runtime_seconds IS DISTINCT FROM OLD.reserved_runtime_seconds
       OR NEW.input_fingerprint IS DISTINCT FROM OLD.input_fingerprint THEN
        RAISE EXCEPTION 'research work-order accepted identity is immutable';
    END IF;

    old_terminal := OLD.status IN ('completed', 'failed_terminal', 'cancelled', 'stale');
    IF old_terminal AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'terminal research work order cannot return to active state';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        transition_allowed :=
            (OLD.status = 'planned' AND NEW.status IN ('queued', 'cancelled', 'stale')) OR
            (OLD.status = 'queued' AND NEW.status IN (
                'leased', 'running', 'failed_retryable', 'failed_terminal', 'cancelled', 'stale'
            )) OR
            (OLD.status = 'leased' AND NEW.status IN (
                'queued', 'running', 'failed_retryable', 'failed_terminal', 'cancelled', 'stale'
            )) OR
            (OLD.status = 'running' AND NEW.status IN (
                'completed', 'failed_retryable', 'failed_terminal', 'stale'
            )) OR
            (OLD.status = 'failed_retryable' AND NEW.status IN (
                'queued', 'leased', 'running', 'failed_terminal', 'cancelled', 'stale'
            ));
        IF NOT transition_allowed THEN
            RAISE EXCEPTION 'invalid research work-order transition: % -> %', OLD.status, NEW.status;
        END IF;
    END IF;

    NEW.updated_at := NOW();
    IF NEW.status = 'queued' THEN
        NEW.queued_at := COALESCE(NEW.queued_at, NOW());
    ELSIF NEW.status = 'running' THEN
        NEW.started_at := COALESCE(NEW.started_at, NOW());
    ELSIF NEW.status IN ('completed', 'failed_terminal', 'cancelled', 'stale') THEN
        NEW.completed_at := COALESCE(NEW.completed_at, NOW());
    END IF;
    IF NEW.status = 'completed' AND NEW.result IS NULL THEN
        RAISE EXCEPTION 'completed research work order requires a structured result';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS research_work_orders_lifecycle ON research_work_orders;
CREATE TRIGGER research_work_orders_lifecycle
BEFORE UPDATE OR DELETE ON research_work_orders
FOR EACH ROW EXECUTE FUNCTION guard_research_work_order_lifecycle();

DROP TRIGGER IF EXISTS research_skill_versions_guard ON research_skill_versions;
CREATE TRIGGER research_skill_versions_guard
BEFORE UPDATE OR DELETE ON research_skill_versions
FOR EACH ROW EXECUTE FUNCTION guard_research_skill_version();

-- ---------------------------------------------------------------------------
-- 5. Typed dependency graph, source capability registry and repeated gaps.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS research_dependency_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_type TEXT NOT NULL,
    node_key TEXT NOT NULL,
    state_fingerprint TEXT,
    accepted_cutoff TIMESTAMPTZ,
    dirty_since TIMESTAMPTZ,
    last_refreshed_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_dependency_nodes_identity_unique UNIQUE (node_type, node_key),
    CONSTRAINT research_dependency_nodes_type_check
        CHECK (node_type IN (
            'source_observation', 'source', 'entity', 'claim', 'evidence',
            'assumption', 'thesis', 'scenario', 'forecast', 'catalyst',
            'risk', 'playbook', 'watchlist', 'question', 'effect'
        )),
    CONSTRAINT research_dependency_nodes_key_check
        CHECK (LENGTH(BTRIM(node_key)) BETWEEN 1 AND 500),
    CONSTRAINT research_dependency_nodes_fingerprint_check
        CHECK (state_fingerprint IS NULL OR state_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT research_dependency_nodes_metadata_check
        CHECK (JSONB_TYPEOF(metadata) = 'object'),
    CONSTRAINT research_dependency_nodes_time_check
        CHECK (
            updated_at >= created_at
            AND (dirty_since IS NULL OR dirty_since >= created_at)
            AND (last_refreshed_at IS NULL OR last_refreshed_at >= created_at)
        )
);

CREATE INDEX IF NOT EXISTS idx_research_dependency_nodes_dirty
    ON research_dependency_nodes (dirty_since, id)
    WHERE dirty_since IS NOT NULL;

CREATE TABLE IF NOT EXISTS research_dependency_edges (
    source_node_id UUID NOT NULL REFERENCES research_dependency_nodes (id) ON DELETE CASCADE,
    target_node_id UUID NOT NULL REFERENCES research_dependency_nodes (id) ON DELETE CASCADE,
    edge_kind TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deactivated_at TIMESTAMPTZ,
    PRIMARY KEY (source_node_id, target_node_id, edge_kind),
    CONSTRAINT research_dependency_edges_kind_check
        CHECK (edge_kind IN (
            'supports', 'contradicts', 'depends_on', 'derived_from', 'measures',
            'mentions', 'affects', 'invalidates', 'resolves', 'supersedes'
        )),
    CONSTRAINT research_dependency_edges_no_self_check
        CHECK (source_node_id <> target_node_id),
    CONSTRAINT research_dependency_edges_active_time_check
        CHECK ((active AND deactivated_at IS NULL) OR (NOT active AND deactivated_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_research_dependency_edges_target
    ON research_dependency_edges (target_node_id, edge_kind, source_node_id)
    WHERE active;

CREATE TABLE IF NOT EXISTS research_source_capabilities (
    source_family TEXT NOT NULL,
    source_identifier TEXT NOT NULL,
    supported_question_types TEXT[] NOT NULL,
    supported_entities TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    geographic_coverage TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    asset_coverage TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    historical_depth_days INTEGER,
    point_in_time_safety TEXT NOT NULL,
    freshness_seconds INTEGER,
    typical_latency_seconds INTEGER,
    cost_usd NUMERIC(12, 6),
    rate_limit_per_minute INTEGER,
    licensing_restrictions TEXT,
    runtime_available BOOLEAN NOT NULL,
    recent_reliability NUMERIC(12, 9),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detail JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (source_family, source_identifier),
    CONSTRAINT research_source_capabilities_name_check
        CHECK (
            LENGTH(BTRIM(source_family)) BETWEEN 1 AND 100
            AND LENGTH(BTRIM(source_identifier)) BETWEEN 1 AND 200
        ),
    CONSTRAINT research_source_capabilities_collection_check
        CHECK (
            CARDINALITY(supported_question_types) BETWEEN 1 AND 32
            AND CARDINALITY(supported_entities) <= 100
            AND CARDINALITY(geographic_coverage) <= 100
            AND CARDINALITY(asset_coverage) <= 100
        ),
    CONSTRAINT research_source_capabilities_pit_check
        CHECK (point_in_time_safety IN ('point_in_time', 'current_only', 'unsafe', 'unknown')),
    CONSTRAINT research_source_capabilities_numeric_check
        CHECK (
            (historical_depth_days IS NULL OR historical_depth_days BETWEEN 0 AND 365000)
            AND (freshness_seconds IS NULL OR freshness_seconds BETWEEN 0 AND 31536000)
            AND (typical_latency_seconds IS NULL OR typical_latency_seconds BETWEEN 0 AND 86400)
            AND (cost_usd IS NULL OR (cost_usd <> 'NaN'::NUMERIC AND cost_usd BETWEEN 0 AND 100))
            AND (rate_limit_per_minute IS NULL OR rate_limit_per_minute BETWEEN 0 AND 1000000)
            AND (recent_reliability IS NULL OR recent_reliability BETWEEN 0 AND 1)
        ),
    CONSTRAINT research_source_capabilities_detail_check
        CHECK (
            JSONB_TYPEOF(detail) = 'object'
            AND (licensing_restrictions IS NULL OR LENGTH(licensing_restrictions) <= 1000)
        )
);

CREATE INDEX IF NOT EXISTS idx_research_source_capabilities_question
    ON research_source_capabilities USING GIN (supported_question_types);
CREATE INDEX IF NOT EXISTS idx_research_source_capabilities_checked
    ON research_source_capabilities (checked_at DESC);

INSERT INTO research_source_capabilities (
    source_family, source_identifier, supported_question_types,
    supported_entities, geographic_coverage, asset_coverage,
    historical_depth_days, point_in_time_safety, freshness_seconds,
    typical_latency_seconds, cost_usd, rate_limit_per_minute,
    licensing_restrictions, runtime_available, recent_reliability, detail
) VALUES
    (
        'issuer_filing', 'investment_documents',
        ARRAY['earnings_guidance_delta', 'filing_peer_readthrough', 'catalyst_confirmation', 'evidence_refresh'],
        ARRAY['issuer'], ARRAY['US', 'EU', 'ASIA'], ARRAY['equity'],
        NULL, 'point_in_time', NULL, NULL, 0, NULL,
        'Persisted source terms govern redistribution.', TRUE, NULL,
        '{"availability_basis":"investment_documents.created_at","semantic_scope":"issuer filings and filing deltas"}'::JSONB
    ),
    (
        'issuer_material', 'investment_documents',
        ARRAY['earnings_guidance_delta', 'catalyst_confirmation', 'evidence_refresh'],
        ARRAY['issuer'], ARRAY['US', 'EU', 'ASIA'], ARRAY['equity'],
        NULL, 'point_in_time', NULL, NULL, 0, NULL,
        'Persisted source terms govern redistribution.', TRUE, NULL,
        '{"availability_basis":"investment_documents.created_at","semantic_scope":"accepted issuer materials"}'::JSONB
    ),
    (
        'market_price', 'market_data',
        ARRAY['forecast_resolution', 'positioning_divergence'],
        ARRAY['symbol'], ARRAY[]::TEXT[], ARRAY['equity', 'fx', 'index', 'commodity'],
        NULL, 'point_in_time', NULL, NULL, 0, NULL,
        NULL, TRUE, NULL,
        '{"availability_basis":"market_data.available_at","semantic_scope":"terminal price observations"}'::JSONB
    ),
    (
        'options', 'option_snapshot_features',
        ARRAY['positioning_divergence'],
        ARRAY['symbol'], ARRAY['US'], ARRAY['equity', 'index'],
        NULL, 'point_in_time', NULL, NULL, 0, NULL,
        'Provider terms govern redistribution.', TRUE, NULL,
        '{"availability_basis":"option_snapshot_features.available_at","semantic_scope":"options analytics only"}'::JSONB
    ),
    (
        'cftc', 'positioning_reports',
        ARRAY['positioning_divergence'],
        ARRAY['market_id'], ARRAY['US'], ARRAY['fx', 'commodity', 'index'],
        NULL, 'point_in_time', 604800, NULL, 0, NULL,
        'Public regulatory data; verify source terms before redistribution.', TRUE, NULL,
        '{"availability_basis":"positioning_reports.acquired_at and created_at","semantic_scope":"reported CFTC positioning"}'::JSONB
    ),
    (
        'finra', 'positioning_reports',
        ARRAY['positioning_divergence'],
        ARRAY['market_id'], ARRAY['US'], ARRAY['equity'],
        NULL, 'point_in_time', 86400, NULL, 0, NULL,
        'Public regulatory data; verify source terms before redistribution.', TRUE, NULL,
        '{"availability_basis":"positioning_reports.acquired_at and created_at","semantic_scope":"reported FINRA positioning"}'::JSONB
    ),
    (
        'all_attached_point_in_time_evidence', 'investment_thesis_evidence',
        ARRAY['thesis_challenge'],
        ARRAY['thesis'], ARRAY[]::TEXT[], ARRAY[]::TEXT[],
        NULL, 'point_in_time', NULL, NULL, 0, NULL,
        'Underlying evidence source terms govern redistribution.', TRUE, NULL,
        '{"availability_basis":"investment_thesis_evidence.available_at","semantic_scope":"accepted evidence already attached to a thesis"}'::JSONB
    )
ON CONFLICT (source_family, source_identifier) DO NOTHING;

CREATE TABLE IF NOT EXISTS research_source_gaps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fingerprint TEXT NOT NULL,
    question_type TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    missing_capability TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_observed_at TIMESTAMPTZ NOT NULL,
    last_observed_at TIMESTAMPTZ NOT NULL,
    latest_source_event_id UUID REFERENCES market_events (id) ON DELETE SET NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    bounded_summary TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_source_gaps_fingerprint_check
        CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT research_source_gaps_count_check CHECK (occurrence_count BETWEEN 1 AND 1000000),
    CONSTRAINT research_source_gaps_time_check CHECK (last_observed_at >= first_observed_at),
    CONSTRAINT research_source_gaps_text_bounds_check
        CHECK (
            LENGTH(BTRIM(target_ref)) BETWEEN 1 AND 500
            AND LENGTH(BTRIM(missing_capability)) BETWEEN 1 AND 500
            AND LENGTH(BTRIM(bounded_summary)) BETWEEN 1 AND 1000
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_source_gaps_active
    ON research_source_gaps (fingerprint) WHERE active;
CREATE INDEX IF NOT EXISTS idx_research_source_gaps_repeated
    ON research_source_gaps (occurrence_count DESC, last_observed_at DESC)
    WHERE active;

-- ---------------------------------------------------------------------------
-- 6. Append-only effects, forecast outcome attribution and scorecards.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS research_effects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_id UUID NOT NULL UNIQUE REFERENCES research_work_orders (id) ON DELETE RESTRICT,
    question_id UUID NOT NULL REFERENCES research_questions (id) ON DELETE RESTRICT,
    affected_target_kind TEXT NOT NULL,
    affected_target_ref TEXT NOT NULL,
    before_state_fingerprint TEXT NOT NULL,
    after_state_fingerprint TEXT NOT NULL,
    effect_type TEXT NOT NULL,
    material BOOLEAN NOT NULL,
    materiality_policy_version TEXT NOT NULL,
    evidence_attached TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    evidence_removed TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    source_families TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    scenario_changes JSONB NOT NULL DEFAULT '{}'::JSONB,
    forecast_changes JSONB NOT NULL DEFAULT '{}'::JSONB,
    status_changes JSONB NOT NULL DEFAULT '{}'::JSONB,
    cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
    runtime_ms INTEGER NOT NULL,
    event_to_verified_latency_ms INTEGER,
    human_review_minutes NUMERIC(12, 3),
    evidence_reused_count INTEGER NOT NULL DEFAULT 0,
    evidence_acquired_count INTEGER NOT NULL DEFAULT 0,
    justified_noop_reason TEXT,
    skill_version_id UUID NOT NULL REFERENCES research_skill_versions (id) ON DELETE RESTRICT,
    model_slug TEXT,
    prompt_version TEXT,
    question_type TEXT NOT NULL,
    accepted_cutoff TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_effects_fingerprint_check
        CHECK (
            before_state_fingerprint ~ '^[0-9a-f]{64}$'
            AND after_state_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    CONSTRAINT research_effects_type_check
        CHECK (effect_type IN (
            'thesis_status', 'falsification_state', 'scenario_probability',
            'expected_value', 'confidence', 'opportunity', 'core_evidence',
            'forecast', 'catalyst', 'unresolved_question', 'justified_noop'
        )),
    CONSTRAINT research_effects_material_check
        CHECK (
            (material AND effect_type <> 'justified_noop' AND justified_noop_reason IS NULL)
            OR (
                NOT material
                AND effect_type = 'justified_noop'
                AND LENGTH(BTRIM(justified_noop_reason)) BETWEEN 1 AND 1000
            )
        ),
    CONSTRAINT research_effects_collection_bounds_check
        CHECK (
            CARDINALITY(evidence_attached) <= 256
            AND CARDINALITY(evidence_removed) <= 256
            AND CARDINALITY(source_families) <= 32
        ),
    CONSTRAINT research_effects_change_shape_check
        CHECK (
            JSONB_TYPEOF(scenario_changes) = 'object'
            AND JSONB_TYPEOF(forecast_changes) = 'object'
            AND JSONB_TYPEOF(status_changes) = 'object'
        ),
    CONSTRAINT research_effects_cost_runtime_check
        CHECK (
            cost_usd <> 'NaN'::NUMERIC AND cost_usd BETWEEN 0 AND 100
            AND runtime_ms BETWEEN 0 AND 86400000
            AND (event_to_verified_latency_ms IS NULL OR event_to_verified_latency_ms BETWEEN 0 AND 2592000000)
            AND (human_review_minutes IS NULL OR (
                human_review_minutes <> 'NaN'::NUMERIC
                AND human_review_minutes BETWEEN 0 AND 1440
            ))
            AND evidence_reused_count BETWEEN 0 AND 100000
            AND evidence_acquired_count BETWEEN 0 AND 100000
        ),
    CONSTRAINT research_effects_text_bounds_check
        CHECK (
            LENGTH(BTRIM(affected_target_ref)) BETWEEN 1 AND 500
            AND LENGTH(materiality_policy_version) BETWEEN 1 AND 64
            AND (model_slug IS NULL OR LENGTH(model_slug) <= 200)
            AND (prompt_version IS NULL OR LENGTH(prompt_version) <= 100)
        )
);

CREATE INDEX IF NOT EXISTS idx_research_effects_target
    ON research_effects (affected_target_kind, affected_target_ref, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_effects_created
    ON research_effects (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_research_effects_skill
    ON research_effects (skill_version_id, question_type, created_at DESC);

CREATE TABLE IF NOT EXISTS research_outcome_attributions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    forecast_outcome_id UUID NOT NULL REFERENCES investment_forecast_outcomes (id) ON DELETE RESTRICT,
    work_order_id UUID NOT NULL REFERENCES research_work_orders (id) ON DELETE RESTRICT,
    skill_version_id UUID NOT NULL REFERENCES research_skill_versions (id) ON DELETE RESTRICT,
    question_type TEXT NOT NULL,
    source_families TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    model_slug TEXT,
    prompt_version TEXT,
    horizon_context TEXT NOT NULL,
    industry_context TEXT,
    accepted_cutoff TIMESTAMPTZ NOT NULL,
    outcome_status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_outcome_attributions_identity_unique
        UNIQUE (forecast_outcome_id, work_order_id),
    CONSTRAINT research_outcome_attributions_status_check
        CHECK (outcome_status IN ('hit', 'miss', 'inconclusive')),
    CONSTRAINT research_outcome_attributions_source_bound_check
        CHECK (CARDINALITY(source_families) <= 32),
    CONSTRAINT research_outcome_attributions_text_bound_check
        CHECK (
            LENGTH(BTRIM(horizon_context)) BETWEEN 1 AND 100
            AND (industry_context IS NULL OR LENGTH(industry_context) <= 200)
            AND (model_slug IS NULL OR LENGTH(model_slug) <= 200)
            AND (prompt_version IS NULL OR LENGTH(prompt_version) <= 100)
        )
);

CREATE INDEX IF NOT EXISTS idx_research_outcome_attributions_skill
    ON research_outcome_attributions (skill_version_id, question_type, created_at DESC);

CREATE OR REPLACE FUNCTION reject_append_only_research_record()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS research_effects_append_only ON research_effects;
CREATE TRIGGER research_effects_append_only
BEFORE UPDATE OR DELETE ON research_effects
FOR EACH ROW EXECUTE FUNCTION reject_append_only_research_record();

DROP TRIGGER IF EXISTS research_outcome_attributions_append_only ON research_outcome_attributions;
CREATE TRIGGER research_outcome_attributions_append_only
BEFORE UPDATE OR DELETE ON research_outcome_attributions
FOR EACH ROW EXECUTE FUNCTION reject_append_only_research_record();

CREATE OR REPLACE VIEW research_control_plane_metrics AS
SELECT
    (SELECT COUNT(*) FROM research_questions WHERE status = 'pending') AS pending_questions,
    (SELECT COUNT(*) FROM research_questions WHERE status = 'planned') AS planned_questions,
    (SELECT COUNT(*) FROM research_questions WHERE status = 'queued') AS queued_questions,
    (SELECT COUNT(*) FROM research_questions WHERE status = 'running') AS running_questions,
    (SELECT COUNT(*) FROM research_questions
       WHERE status IN ('pending', 'planned')
         AND due_at IS NOT NULL AND due_at < NOW()) AS stale_thesis_debt,
    (SELECT COUNT(*) FROM research_work_orders
       WHERE status IN ('planned', 'queued', 'leased', 'running', 'failed_retryable')) AS active_work_orders,
    (SELECT COUNT(*) FROM research_work_orders WHERE status = 'completed') AS completed_work_orders,
    (SELECT COUNT(*) FROM research_effects WHERE material) AS material_effects,
    (SELECT COUNT(*) FROM research_effects WHERE NOT material) AS justified_noops,
    (SELECT COUNT(*) FROM investment_forecast_outcomes) AS matured_forecast_outcomes;

CREATE OR REPLACE VIEW research_productivity_daily AS
WITH effect_daily AS (
    SELECT
        DATE_TRUNC('day', e.created_at) AS metric_day,
        COUNT(*) AS completed_work,
        COUNT(*) FILTER (WHERE e.material) AS material_updates,
        COUNT(*) FILTER (WHERE NOT e.material) AS justified_noops,
        COALESCE(SUM(e.cost_usd), 0) AS total_cost_usd,
        CASE
            WHEN COUNT(*) FILTER (WHERE e.material) = 0 THEN NULL
            ELSE SUM(e.cost_usd) / COUNT(*) FILTER (WHERE e.material)
        END AS cost_per_material_update,
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY e.event_to_verified_latency_ms
        ) FILTER (
            WHERE e.event_to_verified_latency_ms IS NOT NULL
        ) AS median_event_to_verified_latency_ms,
        SUM(e.evidence_reused_count) AS evidence_reused,
        SUM(e.evidence_acquired_count) AS evidence_acquired,
        CASE
            WHEN SUM(e.evidence_reused_count + e.evidence_acquired_count) = 0 THEN NULL
            ELSE SUM(e.evidence_reused_count)::NUMERIC
                 / SUM(e.evidence_reused_count + e.evidence_acquired_count)
        END AS evidence_reuse_ratio
    FROM research_effects e
    GROUP BY DATE_TRUNC('day', e.created_at)
),
decision_daily AS (
    SELECT
        DATE_TRUNC('day', d.created_at) AS metric_day,
        COUNT(*) FILTER (
            WHERE d.decision = 'deferred'
              AND 'active_work_order' = ANY (d.reason_codes)
        )::NUMERIC / NULLIF(COUNT(*), 0) AS duplicate_work_rate
    FROM research_plan_decisions d
    GROUP BY DATE_TRUNC('day', d.created_at)
)
SELECT
    e.*,
    d.duplicate_work_rate
FROM effect_daily e
LEFT JOIN decision_daily d USING (metric_day);

CREATE OR REPLACE VIEW research_skill_scorecards AS
SELECT
    sv.skill_key,
    sv.version,
    wo.id AS sample_work_order_id,
    wo.status,
    e.question_type,
    e.material,
    e.cost_usd,
    e.runtime_ms,
    oa.outcome_status,
    e.created_at
FROM research_skill_versions sv
JOIN research_work_orders wo ON wo.skill_version_id = sv.id
LEFT JOIN research_effects e ON e.work_order_id = wo.id
LEFT JOIN research_outcome_attributions oa ON oa.work_order_id = wo.id;

CREATE OR REPLACE VIEW research_source_scorecards AS
SELECT
    source_family,
    e.question_type,
    e.material,
    e.cost_usd,
    e.evidence_reused_count,
    e.evidence_acquired_count,
    oa.outcome_status,
    e.created_at
FROM research_effects e
CROSS JOIN LATERAL UNNEST(e.source_families) AS source_family
LEFT JOIN research_outcome_attributions oa ON oa.work_order_id = e.work_order_id;

CREATE OR REPLACE VIEW research_forecast_calibration AS
SELECT
    a.skill_version_id,
    a.question_type,
    a.horizon_context,
    a.industry_context,
    a.accepted_cutoff,
    a.outcome_status,
    f.forecast_type,
    f.direction,
    f.target_date,
    o.measured_at,
    a.created_at
FROM research_outcome_attributions a
JOIN investment_forecast_outcomes o ON o.id = a.forecast_outcome_id
JOIN investment_thesis_forecasts f ON f.id = o.forecast_id;
