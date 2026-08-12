-- Durable content-addressed file storage for investment document uploads.
--
-- Async HTTP ingests persist the bounded upload on the shared news data
-- volume (content-addressed path, atomic write) instead of binding BYTEA;
-- the durable analysis worker extracts from the path later. Legacy rows keep
-- raw_content BYTEA; content_path is NULL for them. Additive and idempotent.
-- Rollback: ALTER TABLE investment_documents DROP COLUMN content_path;

ALTER TABLE investment_documents
    ADD COLUMN IF NOT EXISTS content_path TEXT;

CREATE INDEX IF NOT EXISTS idx_investment_documents_content_path
    ON investment_documents (content_path)
    WHERE content_path IS NOT NULL;
