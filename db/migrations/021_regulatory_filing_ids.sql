ALTER TABLE investment_documents
    ADD COLUMN IF NOT EXISTS filing_source TEXT,
    ADD COLUMN IF NOT EXISTS filing_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_investment_documents_filing_identity
    ON investment_documents (filing_source, filing_id)
    WHERE filing_source IS NOT NULL AND filing_id IS NOT NULL;
