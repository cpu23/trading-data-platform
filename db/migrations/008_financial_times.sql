-- Financial Times article tracking, archive captures, and collection runs.
-- Applied via: python cli.py migrate

CREATE TABLE IF NOT EXISTS ft_articles (
    article_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL UNIQUE,
    canonical_url TEXT NOT NULL UNIQUE,
    latest_title TEXT,
    latest_description TEXT,
    published_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    latest_version_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ft_articles_published_at
    ON ft_articles (published_at DESC);

DO $$
BEGIN
    CREATE TRIGGER ft_articles_updated_at
        BEFORE UPDATE ON ft_articles
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;


CREATE TABLE IF NOT EXISTS ft_article_observations (
    observation_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    article_id TEXT REFERENCES ft_articles(article_id),
    feed_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    rss_payload JSONB NOT NULL,
    UNIQUE(article_id, feed_id, observed_at)
);


CREATE TABLE IF NOT EXISTS ft_archive_captures (
    capture_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    article_id TEXT REFERENCES ft_articles(article_id),
    requested_url TEXT NOT NULL,
    archive_url TEXT,
    status TEXT NOT NULL,
    submitted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    raw_capture_path TEXT,
    raw_content_hash TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ft_archive_captures_article_status
    ON ft_archive_captures (article_id, status);

DO $$
BEGIN
    CREATE TRIGGER ft_archive_captures_updated_at
        BEFORE UPDATE ON ft_archive_captures
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;


CREATE TABLE IF NOT EXISTS ft_article_versions (
    version_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    article_id TEXT REFERENCES ft_articles(article_id),
    capture_id UUID REFERENCES ft_archive_captures(capture_id),
    archive_url TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT,
    byline TEXT,
    published_at TIMESTAMPTZ,
    body_text TEXT NOT NULL,
    word_count INTEGER NOT NULL,
    raw_capture_path TEXT,
    extraction_status TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(article_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_ft_article_versions_content_hash
    ON ft_article_versions (content_hash);

CREATE INDEX IF NOT EXISTS idx_ft_article_versions_captured_at
    ON ft_article_versions (captured_at DESC);


CREATE TABLE IF NOT EXISTS ft_collection_runs (
    run_id UUID PRIMARY KEY,
    correlation_id TEXT,
    status TEXT NOT NULL,
    sections_requested JSONB,
    since_requested TIMESTAMPTZ,
    until_requested TIMESTAMPTZ,
    articles_discovered INTEGER DEFAULT 0,
    articles_captured INTEGER DEFAULT 0,
    articles_failed INTEGER DEFAULT 0,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ft_collection_runs_correlation_id
    ON ft_collection_runs (correlation_id);

DO $$
BEGIN
    CREATE TRIGGER ft_collection_runs_updated_at
        BEFORE UPDATE ON ft_collection_runs
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
