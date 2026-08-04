UPDATE investment_documents
SET industry = CASE
    WHEN LOWER(COALESCE(industry, '')) ~ '(aerospace|defence|defense|aircraft|aviation|military)' THEN 'Aerospace & Defence'
    WHEN LOWER(COALESCE(industry, '')) ~ '(semiconductor|memory|dram|nand|foundry|chip|processor|compute hardware|asic|silicon|electronic|connector|data storage)' THEN 'Semiconductors & Compute'
    WHEN LOWER(COALESCE(industry, '')) ~ '(energy|oil|petroleum|gas|lng|utility|utilities|power|electric|renewable|solar|wind|nuclear|drilling|subsea)' THEN 'Energy & Utilities'
    WHEN LOWER(COALESCE(industry, '')) ~ '(bank|insurance|financial|capital market|asset manag|fintech|private equity|credit|real estate|reit|payment|broker|broking|savings institution|investment|wealth)' THEN 'Financials & Real Estate'
    WHEN LOWER(COALESCE(industry, '')) ~ '(consumer|retail|e-commerce|ecommerce|food|beverage|apparel|automotive|automobile|travel|leisure|hospitality|restaurant|tobacco|cigarette|education|hotel|entertainment|household|discount store|warehouse club)' THEN 'Consumer'
    WHEN LOWER(COALESCE(industry, '')) ~ '(healthcare|health care|biotech|pharma|drug|medical|life science|biological|hospital|surgical|orthopedic|therapeutic)' THEN 'Healthcare'
    WHEN LOWER(COALESCE(industry, '')) ~ '(software|cloud|data cent(er|re)|datacenter|communication|telecom|information technology|technology|internet|digital|media|programming|computer|network|cybersecurity|audio streaming|ai infrastructure)' THEN 'Software, Cloud & Communications'
    WHEN LOWER(COALESCE(industry, '')) ~ '(industrial|automation|robot|machinery|material|chemical|mining|metal|construction|transport|logistics|manufactur|railroad|equipment|building product|hardware|steel|copper)' THEN 'Industrials & Materials'
    ELSE 'Unclassified'
END,
updated_at = NOW();

UPDATE investment_analyses AS analysis
SET facts = CASE
        WHEN jsonb_typeof(analysis.facts->'classification') = 'object'
        THEN jsonb_set(analysis.facts, '{classification,industry}', to_jsonb(document.industry), false)
        ELSE analysis.facts
    END,
    analysis = CASE
        WHEN jsonb_typeof(analysis.analysis->'classification') = 'object'
        THEN jsonb_set(analysis.analysis, '{classification,industry}', to_jsonb(document.industry), false)
        ELSE analysis.analysis
    END,
    updated_at = NOW()
FROM investment_documents AS document
WHERE document.document_id = analysis.document_id;

ALTER TABLE investment_documents
    DROP CONSTRAINT IF EXISTS investment_documents_industry_check;

ALTER TABLE investment_documents
    ADD CONSTRAINT investment_documents_industry_check
    CHECK (
        industry IS NULL OR industry IN (
            'Semiconductors & Compute',
            'Software, Cloud & Communications',
            'Energy & Utilities',
            'Industrials & Materials',
            'Financials & Real Estate',
            'Healthcare',
            'Consumer',
            'Aerospace & Defence',
            'Unclassified'
        )
    );
