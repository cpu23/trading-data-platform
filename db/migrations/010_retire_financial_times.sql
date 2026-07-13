-- Retire Financial Times source tables: mark FT-prefixed tables as archived
-- historical lineage. No tables, columns, or data are dropped.
-- Gap 009: 009_official_data_sources existed on the dead-end branch
-- codex/market-intelligence-expansion (commits 9f42691, d6afd42) and was
-- never merged into the current lineage. Skipped intentionally.
-- Applied via: python cli.py migrate

COMMENT ON TABLE ft_articles IS 'Retired source data retained for historical lineage';
COMMENT ON TABLE ft_article_observations IS 'Retired source data retained for historical lineage';
COMMENT ON TABLE ft_archive_captures IS 'Retired source data retained for historical lineage';
COMMENT ON TABLE ft_article_versions IS 'Retired source data retained for historical lineage';
COMMENT ON TABLE ft_collection_runs IS 'Retired source data retained for historical lineage';
