# Investment Research and Filing Intake

**Status:** Current  
**Last reviewed:** 2026-08-08

## Scope

The investment subsystem ingests company reports, selects standardized filing
facts deterministically, uses a strict model contract only for qualitative
analysis, applies deterministic scoring, fundamentals, trend, and valuation
rules, and presents regional, industry, company, and report research at
`/investment`.

It is decision support. It does not produce trade recommendations, entries,
exits, stops, targets, position sizes, or allocations. Report evidence remains
distinct from related news context and deterministic derived values.

### Relationship to dynamic research intelligence

This report lifecycle remains source-owned and deterministic for filing facts,
deltas, fundamentals, and valuation. The dynamic research engine consumes
bounded filing deltas, investment observations, and evidence-linked analyses
through normalized adapters; it does not duplicate report documents or recast
model prose as structured financial facts.

Dynamic `research_cases` discover cross-company or cross-industry economic
developments, maintain causal chains, value-capture dimensions,
counterevidence, and missing-data requests, then may extend a maintained theme.
The existing theme/thesis and company-dossier workspace remains the durable
human-maintained research layer. See
[Research Intelligence](research-intelligence.md).

## Architecture

```mermaid
flowchart LR
    Operator["Market operator"]
    Scheduler["Weekday filing scheduler"]
    API["Web/API investment routes"]
    Filing["Regulatory filing intake"]
    Manual["Manual file or public URL intake"]
    Regulators["SEC EDGAR<br/>Companies House<br/>EDINET<br/>OpenDART"]
    Extract["Primary-report text extraction"]
    XBRL["Accession-scoped SEC XBRL facts"]
    LLM["Qualitative analysis<br/>single configured default model"]
    Rules["Deterministic fundamentals,<br/>signal, trend, and valuation engine"]
    News["Deterministically classified news"]
    DB[("investment documents and analyses<br/>themes, theses, evidence, catalysts,<br/>risks, watch items, filing deltas, portfolio context")]
    UI["Investment dashboard and<br/>maintained research workspace"]

    Operator --> UI --> API
    Operator --> Manual --> API
    Scheduler --> Filing
    API --> Filing --> Regulators
    Filing --> Extract
    API --> Extract --> DB
    Regulators --> XBRL --> Rules
    DB --> LLM
    News --> LLM
    LLM --> Rules --> DB
    DB --> API --> UI
```

The browser reaches only the Web/API container. Authenticated API routes proxy
to the internal orchestrator with internal Basic authentication. The
orchestrator owns document extraction, filing discovery, model calls,
deterministic analysis, persistence, and durable filing jobs.

## User Interface

`GET /investment` serves the investment research shell. Its JavaScript loads:

- regional coverage for US, EU, and Asia;
- a nine-category industry ledger with score, momentum, company breadth,
  revenue growth, free-cash-flow margin, deterministic coverage, and
  company-counted driver and risk claims;
- one-click industry filtering across report trends, news, and company research;
- deterministic company and industry trend series;
- emerging themes from current-versus-prior classified-news windows;
- bounded company and industry news with explicit classification provenance;
- coverage, deterministic extraction, quality/freshness status, model cost, and
  duration summaries;
- the latest 100 report documents;
- the latest analysis for each company, bounded to 300 companies, with lazy
  drilldowns for metrics, fundamentals, signals, evidence, explicitly selected
  peers, free public market closes, valuation sensitivities, and quality
  warnings;
- manual file and public-URL intake;
- optional valuation overrides, separately labelled from public prices;
- report analysis, issuer-level filing-gap, and filing-collection controls.

Primary navigation labels this page **Investments**. The page renders explicit
loading, empty, success, and failure states and escapes untrusted values before
adding them to the DOM.

### Industry taxonomy

Every intake path, news classifier, stored document, stored analysis, aggregate,
and manual form uses the same canonical taxonomy:

1. Semiconductors & Compute
2. Software, Cloud & Communications
3. Energy & Utilities
4. Industrials & Materials
5. Financials & Real Estate
6. Healthcare
7. Consumer
8. Aerospace & Defence
9. Unclassified

Free-text aliases are mapped deterministically. Unknown and unsupported labels
remain `Unclassified`; the classifier does not invent a tenth category.


## Filing Sources and Universe

The checked-in `top_us_uk_eu_100` universe contains separate top-100 snapshots
for US, UK, and EU issuers: 300 configured companies in total. The snapshot
source and date are recorded in `orchestrator/investment_universe.py`.

| Source | Coverage and requirements |
| --- | --- |
| SEC EDGAR | US issuers and cross-listed companies with a CIK. No API key; a descriptive `SEC_USER_AGENT` with contact information is required. Complete accession directories, including exhibits, are bundled with bounded downloads. |
| Companies House | UK statutory accounts for companies with permanent company numbers. Requires `COMPANIES_HOUSE_API_KEY`. |
| EU ESEF / national OAMs | Decentralized. Cross-listed issuers can be covered through SEC; remaining issuers are reported as manual coverage. |
| EDINET | Implemented for configured Japanese issuers with EDINET identifiers and `EDINET_API_KEY`. The built-in US/UK/EU universe does not add Japanese issuers. |
| OpenDART | Implemented for configured Korean issuers with corporation identifiers and `OPENDART_API_KEY`. The built-in US/UK/EU universe does not add Korean issuers. |

Discovery is source-rate-limited. Companies are scanned with bounded worker
concurrency. A filing is deduplicated by `(filing_source, filing_id)`, with a
source-URL fallback for records created before source-native identifiers were
stored.

## Schedule and Durable Runs

Current repository defaults in `config/config.yaml`:

| Setting | Default |
| --- | --- |
| Enabled | `true` |
| Schedule | Weekdays at `08:00 UTC` |
| Run on orchestrator startup | `true` |
| Lookback | 730 days |
| Company workers | 1 |
| Automatic analysis after ingestion | `true` |
| Universe | `top_us_uk_eu_100` |

Scheduled and manually triggered collections create durable `cycle_runs` rows
with `run_kind = 'filings'` and `requested_component =
'investment_filings'`. They use accepted/running/completed lifecycle state,
heartbeats, correlation IDs, and the same durable finalization path as other
operator jobs. The scheduler sets `max_instances=1`.

Automatic analysis is enabled for newly ingested filings. The single company
worker and two bounded OCR page workers limit CPU pressure; model calls remain
subject to the daily LLM budget and failure contracts.

## Document Intake

The API accepts:

- uploaded PDF, DOCX, text, Markdown, HTML, CSV, JSON, and XML documents;
- bounded public report URLs after scheme, host, address, and redirect checks;
- metadata for company, symbol, region, industry, document type, report date,
  and source URL.

Uploads are limited to 20 MB at both the API stream and orchestrator service
boundaries. Extracted text is capped at 1,000,000 characters while retaining
the beginning, end, financial-statement windows, and analysis-signal windows.
The 120,000-character model excerpt independently reserves the beginning and
end before adding evidence-focused windows.

Regulator downloads may be up to 100 MB. Inline-XBRL report packages are read
with bounded member and total-uncompressed limits. PDFs use embedded text when
available, then deterministic page selection and layout-preserving Tesseract OCR
with at most two page workers. OCR and extracted report text remain inputs to
deterministic parsers; they are not treated as model-generated facts.

Content SHA-256 is unique, so identical report bytes are not stored twice. A
document moves through `ingested`, `analyzing`, `analyzed`, or `failed` state.
Only one analysis can claim a document at a time.

## Analysis Contract

The analysis path selects SEC Companyfacts records by exact filing accession
and annual period, parses UK inline-XBRL report packages, and extracts aligned
current/prior statement rows from layout-preserving report text. Revenue, cash
flow, capex, earnings, shares, balance-sheet values, gross margin, and net debt
are normalized by deterministic code. Legacy SEC bundles recover the largest
non-exhibit primary HTML document; new filing bundles prioritize and preserve
the regulator-identified primary document.

The `investment_analysis` stage uses `openai/gpt-5.6-luna` with a strict JSON
schema only for:

- classification: document type, sector, canonical industry, region, and
  confidence;
- qualitative demand, pricing, supply, competitive, and management signals;
- evidence-bounded summary and thesis;
- drivers, catalysts, risks with mitigations, and watch items.

The model schema contains no financial metric fields. Every report metric must
come from accession-scoped XBRL or an aligned statement row with a current/prior
period, unit (including an explicit unknown report-currency unit), and source
quote. Classified news may inform catalyst, industry, theme, and crowding
context, but remains separate from report evidence. One bounded correction call
is available for invalid JSON.

`investment_engine.py` applies public signal weights, period comparisons, free
cash flow, margins, ROA, ROE, leverage, liquidity, cash conversion, DCF,
valuation, and lifecycle rules. The dashboard derives dated industry series,
news momentum, and latest-company industry breadth without model arithmetic.
Peer comparison excludes the subject and selects up to eight same-industry
issuers by reporting region, growth, profitability, leverage, capital
intensity, and same-currency revenue scale. Every member exposes its distance
and selection reasons; missing financial features incur a deterministic
penalty. Medians, tie-aware percentiles, sample counts, and leave-one-out
median ranges use only the selected peers. Actual model, tokens, cost, status,
end-to-end duration, model-call duration, correlation ID, and document ID are
stored in the analysis and `processing_log`.

### Valuation semantics

DCF is a deterministic five-year scenario model, not a quoted or consensus
valuation. The company drilldown shows starting free cash flow, inferred annual
growth, WACC, terminal growth, forecast horizon, growth ceiling, shares, and net
debt. Inferred growth is capped at 20%; WACC must be positive and exceed
terminal growth. A bounded sensitivity ledger varies starting FCF, annual
growth, discount rate, and terminal growth independently, plus a WACC/terminal
grid. It reports enterprise-value and per-share ranges and the largest
independent range driver; missing bridge inputs remain unavailable.

Every latest-company annual analysis is assigned to exactly one DCF coverage
category:

- `calculated`: enterprise value was calculated and available net debt plus
  positive shares support a per-share scenario output;
- `enterprise_value_only`: enterprise value was calculated, but net debt or
  positive shares are unavailable for the equity and per-share bridge;
- `unavailable`: required starting FCF, comparable growth, or valid discount
  assumptions are unavailable.

`GET /api/investment/dashboard` adds these counts under
`research_summary.valuation_coverage`, together with actual market-price, P/E,
and margin-of-safety coverage. The keyless public-equities collector covers the
checked-in 300-company universe plus bounded active-thesis symbols. A symbol
with no stored daily bar receives one three-month bootstrap; later runs request
five days. Responses have byte/row bounds and must echo the requested provider
symbol. Each quote exposes provider URL, source/acquisition timestamps,
currency, exchange, and freshness.

Market-relative calculations require a positive price no older than seven
days. GBp quotes normalize to GBP. A public quote is used only when its currency
matches the filing; cross-currency and stale quotes remain visible but cannot
drive P/E or margin of safety. Explicit manual prices remain supported and are
labelled `manual_input`, with no public-source freshness claim. Stored shares,
net debt, discount rate, and terminal growth survive public-price refreshes.

## Persistence

### `investment_documents`

Stores normalized metadata, source-native filing identity, content hash,
extracted text, lifecycle status, and safe failure state. Important indexes
support company/date and industry/region dashboard queries.

### `investment_analyses`

Stores one current analysis per document, the prior comparable document link,
strict extracted facts, deterministic/enriched analysis, extraction provenance,
actual model, tokens, cost, and duration. Updating an analysis preserves the
document identity and comparison chain.

Deleting a document cascades to its analysis; deleting a previous document
clears the comparison link.

### `investment_research_observations`

Stores idempotent dated report and classified-news observations with company,
industry, metric, theme, narrative, score, and provenance fields. The dashboard
aggregates this ledger deterministically into report/news counts, company
breadth, filing-fact coverage, average scores, and themes without asking a model
to reconstruct history.

## HTTP Surface

Public authenticated API routes:

```text
GET  /investment
GET  /api/investment/dashboard
GET  /api/investment/analyses/{analysis_id}
POST /api/investment/documents
POST /api/investment/urls
POST /api/investment/documents/{document_id}/analyze
GET  /api/investment/filings/status
POST /api/investment/filings/collect
```

The API enforces bounded payloads and maps only approved upstream status codes.
The corresponding orchestrator routes are internal-only and require internal
Basic authentication.

## Credentials and Configuration

Set regulator credentials in private environment or operator state:

```dotenv
SEC_USER_AGENT=TradingDataInvestmentResearch/1.0 contact@example.com
COMPANIES_HOUSE_API_KEY=
EDINET_API_KEY=
OPENDART_API_KEY=
```

`SEC_USER_AGENT` should identify the operator and include a monitored contact.
Never commit live regulator keys. Missing optional keys disable only their
source; SEC coverage continues without an API key.


## Maintained Research Workspace

`/research` is the long-horizon workspace and is visually and temporally
separate from the intraday dashboard. Normalized `investment_themes` connect
to companies, industries, macro series, and other entities. Each thesis update
appends an `investment_thesis_versions` record before changing the current
thesis. Evidence links explicitly support, contradict, or provide context;
catalysts, risks, watch items, confidence components, invalidation conditions,
and review dates remain first-class fields.

`/research/themes/{theme_id}` presents the funnel from structural trend to
affected industries, candidate companies, evidence, expectations, and
valuation. `/research/companies/{company}` is a maintained dossier with the
business profile, active theses, filing-delta timeline, evidence, catalysts,
risks, and changes since the previous analysis. Read loaders are bounded,
fail-soft by section, and never expose full extracted documents.

Filing ingestion computes `investment_filing_deltas` deterministically before
any optional narrative analysis. Section hashes classify new, changed,
removed, and unchanged content; excerpts and normalized numeric facts are
bounded. Automatic model analysis after filing ingestion remains disabled by
default.

Optional portfolio context is read-only. An operator may import or maintain a
holdings set; derived sector, country, currency, rate/commodity sensitivity,
theme concentration, catalyst, and review-schedule summaries do not connect to
an execution API and cannot place or recommend trades.

## Autonomous Thesis Desk

`/research/theses` is the operator workspace for the bounded autonomous
thesis-fusion cycle. It is also summarized on `/investment` and `/research`.
Two scheduled weekday runs collect point-in-time evidence from the configured
news, transcript, filing, macro, price, options, insider-activity, and
positioning adapters for a private research cadence. Company evidence remains
source-owned and point-in-time.
Immutable daily Nasdaq snapshots preserve consensus dispersion and revision
history, announced earnings dates, institutional-holder changes, and reported
short-interest history. FINRA short volume is labelled as a delayed flow proxy
rather than short interest. The public source does not expose live borrow
availability, cost, or utilization, so those fields remain explicitly
unavailable until an authenticated securities-lending source is configured.
Option features retain one immutable source/symbol/capture snapshot, and
transcript/news adapters keep bounded verbatim excerpts instead of model
summaries.

Eight independently prompted research roles generate competing candidates.
Shape-incomplete JSON is validated against the strict schema and receives at
most one no-new-claims repair; semantic failures are never repaired.
Deterministic code then:

1. requires a named security, direction, horizon, quantified trend, current
   valuation context, dated measured expectations/positioning context, complete
   bull/base/bear paths, risks, and explicit invalidators;
2. requires field-level citation arrays for claim, consensus, variant
   perception, mechanism, catalyst, trend, valuation, and sentiment; their
   exact union must equal `evidence_refs`;
3. requires every current citation to resolve to a nonblank excerpt and at
   least three independent free-source families;
4. applies an independent semantic audit using only each field's own exact
   excerpts before promotion;
5. merges reruns by canonical theme, subject, direction, and horizon without
   adding another role's evidence to the representative narrative;
6. promotes only a complementary long/short pair for the same canonical
   security and horizon after both candidates survive source, actionability,
   semantic, and challenger gates; neutral or rejected variants never satisfy
   opposition;
7. attaches supporting and contradicting evidence and runs a bounded
   challenger against candidates and active theses under one shared per-cycle
   challenge cap;
8. gates and blends the opportunity score from evidence strength,
   contradiction-adjusted confidence, catalyst readiness, neglect, liquidity,
   and downside, preserving every missing input as a blocker; and
9. rechecks candidate/active status and a currently base-eligible complementary
   long/short opponent on every opportunity read, so a paused, closed,
   falsified, incomplete, or otherwise blocked side makes both sides
   non-rankable.

Expected value is a separate deterministic figure (probability-weighted
scenario returns net of transaction cost) that is never blended into the
opportunity score. Persisted opportunity ordering is eligible-first:
theses with a positive gated score rank before blocked ones, then by
expected value, opportunity score, confidence, catalyst readiness, neglect,
evaluation recency, and id.

The model cannot publish directly. Candidate identity, deduplication, evidence
relationships, scores, lifecycle transitions, scenario targets, and outcome
resolution are deterministic persistence operations. A breached thesis is
paused for review, never closed automatically.

Scenario price forecasts are frozen only against a close no older than seven
days at their `as_of` boundary. Mature forecasts resolve once as hit, miss, or
inconclusive from similarly fresh point-in-time market data; outcomes are
append-only. Calibration assigns each complete bull/base/bear forecast set to
exactly one realized scenario (the target closest to the terminal close), then
reports a multiclass Brier score and probability bins. Superseded forecasts are
not called mature merely because they were revised; maturity requires a
recorded outcome.

The public-equities collector combines the configured investment universe with
bounded active-thesis symbols. `max_symbols`, `max_concurrency`,
`include_investment_universe`, and `include_active_theses` bound that expansion.

Authenticated desk routes:

```text
GET  /research/theses
GET  /research/theses/{thesis_id}
GET  /api/research/theses/status
GET  /api/research/theses/groups
GET  /api/research/theses/groups/{group_id}
GET  /api/research/theses/opportunities
GET  /api/research/theses/{thesis_id}
POST /api/research/theses/run
```

Runtime limits and the explicit desk model override live under
`thesis_autonomy`. The checked-in personal-use profile pins the desk to
`nvidia/nemotron-3-super-120b-a12b:free` through OpenRouter. Its request price
is zero, but shared-provider availability and rate limits remain external
dependencies; every source and semantic gate still fails closed. Other
processors retain their selections under `llm.models`.

The profile caps evidence, promotions, output tokens, and per-run spend. Four
candidate and existing-thesis challenger calls share one hard per-cycle cap;
candidate calls consume the allowance before the bounded second pass. It
reserves a fixed share of the model budget for falsification, publishes
`partial` rather than concealing isolated role/source failures, and admits no
more than the configured number of event-triggered cycles per UTC day under a
transactional quota lock. The global daily LLM budget remains the final hard
cap. Durable job leasing, heartbeats, retries, deduplication,
generation-attempt telemetry, and fail-soft source/role isolation use the same
operational contracts as the rest of the platform.

## Incremental autonomous research maintenance

The autonomous research control plane turns the desk's accepted missing
evidence, falsification requirements, stale dependencies, unconfirmed
catalysts, matured forecasts and relevant source events into durable atomic
questions. A deterministic value-of-information planner selects only bounded
work under shared cost and runtime ceilings; unknown priority inputs remain
explicit blockers.

Selected questions execute through the existing `analysis_jobs` lease and
recovery machinery using an exact immutable skill version. Initial production
skills cover filing guidance deltas, issuer-specific peer read-through,
positioning divergence, targeted challenge and forecast resolution. Every
completion records evidence use, actual cost and runtime, before/after state
fingerprints, a material effect or a justified no-op, and source/skill
attribution. Only affected thesis or forecast state is recomputed.

Forecast outcomes remain append-only and contribute to calibration and
skill/source scorecards. Feedback may influence later operator-approved
prioritization or promotion decisions; it never rewrites production formulas,
auto-switches models or changes portfolio state.

See [Autonomous Research Control Plane](autonomous-research-control-plane.md)
for the complete lifecycle, point-in-time, budget, recovery and source
capability contracts.

## Operations

Check the read paths:

```bash
curl http://127.0.0.1:8000/api/investment/dashboard
curl http://127.0.0.1:8000/api/investment/filings/status
```

Use the **Collect filings now** button for an authenticated durable trigger, or
call `POST /api/investment/filings/collect` with CSRF protection from a browser
session. Monitor the returned job ID through durable run status and logs.

A failed source or company is counted in the filing summary without discarding
successful company results. Ingested documents remain available when analysis
is skipped, budget-blocked, or fails.

## Historical GPT-5.6 Luna Quality Check

Five annual reports—Microsoft, NXP Semiconductors, ArcelorMittal, Deutsche Bank,
and Sanofi—were run concurrently through the strict schema before the extraction
changes:

| Result | Observed |
| --- | ---: |
| Valid first-pass JSON | 5 / 5 |
| Unsupported numeric claims | 0 |
| Extracted financial metrics | 0 / 5 reports |
| Total prompt / completion tokens | 189,662 / 4,985 |
| Total cost | $0.026698 |
| Median individual latency | 8.752 s |
| Five-call wall time | 12.310 s |

Quality assessment: Luna was disciplined and correctly abstained, but the old
excerpt builder exposed accession metadata and XBRL taxonomy files instead of
the primary reports. The model alone was therefore safe but unusable for
financial research. After deterministic primary-document recovery and
accession-scoped XBRL were added, the Microsoft end-to-end run produced 17
deterministic report metrics plus verbatim qualitative evidence. It completed
in 19.3 seconds at $0.0066 on the final verification run.

A subsequent bounded production rerun analyzed 71 latest Companies House annual
reports. All 71 responses validated and were stored; 47 had deterministic filing
facts available at analysis time. The batch used 1,721,334 prompt tokens and
135,606 completion tokens, cost $0.296525, and averaged 16.4 seconds end to end
(9.0 seconds minimum, 61.8 seconds maximum). Spot checks of Microsoft, ICG, and
Spirax-Sarco showed evidence-bounded narratives, explicit thesis invalidators,
separate news context, and deterministic metric provenance. The 24 narrative-
only rows remain visibly marked as missing filing facts rather than presenting
model-inferred financials.

A parser-hardening rerun then replaced 36 affected annual-report analyses; all
36 stored successfully for $0.112680 at 15.7 seconds average model latency. A
final legacy-data scrub made 25 successful Luna calls (426,314 input and 38,804
output tokens, $0.076570, 14.6 seconds average end to end) and one timed-out
call ($0.002956, 397.2 seconds) whose report succeeded on a subsequent retry.
The deployed latest-company set has deterministic filing facts for 162 of 218
companies.
No displayed numeric metric lacks deterministic source/evidence, and no stored
report-text revenue, gross-profit, asset, liability, or equity comparison falls
outside the conservative one-third-to-three-times year-over-year guard.

## Local Read-Path Baseline

Five warm requests on 29 July 2026 measured:

| Route | Response bytes | Median ms | Min ms | Max ms |
| --- | ---: | ---: | ---: | ---: |
| `/investment` | 45,233 | 2.04 | 1.10 | 37.51 |
| `/api/investment/dashboard` | 43,103 | 9.14 | 8.77 | 66.05 |
| `/api/investment/filings/status` | 1,445 | 5.88 | 5.05 | 910.81 |

The filing-status maximum includes its first observed request and is retained
rather than discarded. A Chromium run measured 3.9 ms TTFB, 50.5 ms DOM content
loaded, and 76 ms first contentful paint; dashboard and filing-status API calls
completed in 13.5 ms and 8.9 ms. These are local acceptance measurements, not a
production SLA and not regulator-collection timings.
