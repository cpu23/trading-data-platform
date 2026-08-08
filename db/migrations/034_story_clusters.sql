-- Phase 6 deterministic canonical news-story state and immutable audit history.
-- Additive/idempotent. Rollback: stop story producers/readers, then drop
-- story_cluster_versions, story_cluster_members, and story_clusters.

CREATE TABLE IF NOT EXISTS story_clusters (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    canonical_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT,
    state TEXT NOT NULL,
    lane TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    last_material_change_at TIMESTAMPTZ NOT NULL,
    importance DOUBLE PRECISION NOT NULL,
    novelty DOUBLE PRECISION NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    entities JSONB NOT NULL DEFAULT '[]'::JSONB,
    markets JSONB NOT NULL DEFAULT '[]'::JSONB,
    source_count INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    change_summary TEXT,
    clustering_reason JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT story_clusters_state_check CHECK (
        state IN ('developing', 'confirmed', 'contradicted', 'stale', 'closed')
    ),
    CONSTRAINT story_clusters_lane_check CHECK (
        lane IN (
            'market_moving', 'watchlist_related', 'macro_central_banks',
            'filings_regulators', 'developing', 'low_confidence'
        )
    ),
    CONSTRAINT story_clusters_importance_check CHECK (importance BETWEEN 0 AND 1),
    CONSTRAINT story_clusters_novelty_check CHECK (novelty BETWEEN 0 AND 1),
    CONSTRAINT story_clusters_confidence_check CHECK (confidence BETWEEN 0 AND 1),
    CONSTRAINT story_clusters_source_count_check CHECK (source_count >= 1),
    CONSTRAINT story_clusters_version_check CHECK (version >= 1)
);

CREATE INDEX IF NOT EXISTS idx_story_clusters_lane_last_seen
    ON story_clusters (lane, last_seen_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_story_clusters_state_last_seen
    ON story_clusters (state, last_seen_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_story_clusters_entities_gin
    ON story_clusters USING GIN (entities);
CREATE INDEX IF NOT EXISTS idx_story_clusters_markets_gin
    ON story_clusters USING GIN (markets);

CREATE TABLE IF NOT EXISTS story_cluster_members (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cluster_id UUID NOT NULL REFERENCES story_clusters(id) ON DELETE CASCADE,
    market_event_id UUID REFERENCES market_events(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    source_label TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    url TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    similarity_score DOUBLE PRECISION NOT NULL,
    contribution_type TEXT NOT NULL,
    materially_changed BOOLEAN NOT NULL DEFAULT FALSE,
    clustering_reason JSONB NOT NULL DEFAULT '{}'::JSONB,
    entities JSONB NOT NULL DEFAULT '[]'::JSONB,
    markets JSONB NOT NULL DEFAULT '[]'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT story_cluster_members_source_item_unique UNIQUE (source, source_item_id),
    CONSTRAINT story_cluster_members_similarity_check CHECK (similarity_score BETWEEN 0 AND 1),
    CONSTRAINT story_cluster_members_contribution_check CHECK (
        contribution_type IN (
            'origin', 'repeated_coverage', 'material_update',
            'cross_source_confirmation', 'contradiction'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_story_cluster_members_cluster_time
    ON story_cluster_members (cluster_id, published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_story_cluster_members_event
    ON story_cluster_members (market_event_id)
    WHERE market_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS story_cluster_versions (
    id BIGSERIAL PRIMARY KEY,
    cluster_id UUID NOT NULL REFERENCES story_clusters(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    prior_state TEXT,
    state TEXT NOT NULL,
    contribution_type TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    member_id UUID REFERENCES story_cluster_members(id) ON DELETE SET NULL,
    snapshot JSONB NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT story_cluster_versions_unique UNIQUE (cluster_id, version)
);

CREATE INDEX IF NOT EXISTS idx_story_cluster_versions_cluster
    ON story_cluster_versions (cluster_id, version DESC);
