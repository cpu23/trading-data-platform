ALTER TABLE investment_documents
    DROP CONSTRAINT IF EXISTS investment_documents_type_check;

ALTER TABLE investment_documents
    ADD CONSTRAINT investment_documents_type_check
    CHECK (document_type IN (
        'annual_report', 'quarterly_report', 'investor_report',
        'earnings_release', 'investor_presentation',
        'regulatory_filing', 'other'
    ));
