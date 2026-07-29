ALTER TABLE investment_documents
    ADD COLUMN IF NOT EXISTS raw_content BYTEA;
