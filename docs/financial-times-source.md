# Financial Times On-Demand Source

## Overview

The Financial Times source discovers articles from FT RSS feeds and optionally
captures full text via archive.fo for private/internal analysis. It is
**on-demand only** — no automatic schedule. Articles are discovered through
RSS, persisted to the database, and optionally captured as validated HTML
snapshots through the archive.fo service.

## Feed Configuration

| Section    | URL                                    |
|------------|----------------------------------------|
| homepage   | `https://www.ft.com/?format=rss`       |
| lex        | `https://www.ft.com/lex?format=rss`    |
| unhedged   | `https://www.ft.com/unhedged?format=rss` |

Configured in `config/config.yaml` under the `financial_times` key:

```yaml
financial_times:
  enabled: true
  on_demand_only: true
  feeds:
    homepage: "https://www.ft.com/?format=rss"
    lex: "https://www.ft.com/lex?format=rss"
    unhedged: "https://www.ft.com/unhedged?format=rss"
  archive_host: "https://archive.fo"
  request_delay_seconds: 2
  poll_interval_seconds: 10
  max_poll_attempts: 12
  raw_storage_path: "/var/lib/trading-data/financial_times"
  default_window: "24h"
```

## CLI Commands

The FT source is accessed through the `ft` subcommand group:

```bash
python cli.py ft <command>
```

### Discover (RSS only)

Fetch RSS feeds and persist article metadata without archive capture:

```bash
python cli.py ft discover --sections homepage,lex,unhedged --since 24h
python cli.py ft discover --sections lex --since 7d --json
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--sections` | `homepage,lex,unhedged` | Comma-separated feed sections to query |
| `--since` | `24h` | Time window: `24h`, `7d`, or ISO datetime |
| `--json` | off | Machine-readable JSON output |

### Full Collection (discover + archive capture)

Run the complete pipeline: RSS discovery followed by archive.fo capture:

```bash
python cli.py ft run --sections homepage,lex,unhedged --since 24h --wait
python cli.py ft run --sections lex --since 12h --no-ingest
python cli.py ft run --sections lex --since 24h --max-articles 5 --wait --json
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--sections` | `homepage,lex,unhedged` | Comma-separated feed sections |
| `--since` | `24h` | Time window for discovery |
| `--until` | none | Upper bound (ISO datetime) |
| `--max-articles` | none | Cap on articles to capture |
| `--no-ingest` | off | Discovery only, skip archive submission |
| `--wait` / `--no-wait` | `--wait` | Block until captures complete |
| `--json` | off | Machine-readable JSON output |

### Resume Failed Captures

Pick up queued, submitted, or pending captures from a prior run:

```bash
python cli.py ft resume <run-id>
python cli.py ft resume <run-id> --json
```

### Check Status

View recent collection runs or inspect a specific run:

```bash
python cli.py ft status
python cli.py ft status <run-id> --json
```

## API Endpoints

### Trigger Collection

```
POST /api/financial-times
POST http://orchestrator:8000/run_financial_times
```

The API endpoint proxies to the orchestrator. Pass an optional JSON body:

```json
{
  "sections": ["homepage", "lex", "unhedged"],
  "since": "2026-07-10T00:00:00+00:00",
  "until": "2026-07-11T00:00:00+00:00",
  "max_articles": 10,
  "ingest": true,
  "wait_for_capture": true
}
```

Response (202 Accepted):

```json
{
  "job_id": "uuid",
  "accepted_at": "2026-07-11T12:00:00+00:00",
  "status_url": "/api/system/logs?correlation_id=<uuid>"
}
```

Returns 409 if a collection is already running.

## Database Schema

Five tables in migration `008_financial_times.sql`:

| Table | Purpose |
|-------|---------|
| `ft_articles` | Canonical article records (content ID, URL, title, publish date) |
| `ft_article_observations` | Per-feed RSS observations with raw payload (deduped by article + feed + timestamp) |
| `ft_archive_captures` | Capture attempts against archive.fo with status tracking |
| `ft_article_versions` | Validated, extracted article content (body text, word count, content hash) |
| `ft_collection_runs` | Collection run metadata (sections, counts, status, timestamps) |

Articles are deduplicated by `content_id` (extracted from the FT URL path).
Multiple observations from different feeds for the same article are merged.
Article versions are deduplicated by content hash — re-captures with identical
text produce a single version.

## Timestamps

| Field | Meaning |
|-------|---------|
| `published_at` | When FT published the article |
| `first_seen_at` / `last_seen_at` | When the system first/latest discovered it via RSS |
| `submitted_at` | When the capture was submitted to archive.fo |
| `captured_at` | When archive.fo completed the capture |

**Important:** For trading analysis, use `first_seen_at` or `captured_at` as
the system-availability timestamp, not FT's `published_at`, to avoid look-ahead
bias.

## Archive Capture Statuses

| Status | Meaning |
|--------|---------|
| `queued` | Waiting to be submitted |
| `submitted` | Sent to archive.fo |
| `pending` | Archive processing |
| `captured` | Successfully captured and validated |
| `invalid` | Capture failed validation (challenge page, too short, title mismatch) |
| `failed` | Technical failure (timeout, HTTP error) |
| `manual_review` | Needs human inspection |

## Resumability

Failed or pending captures can be resumed from any prior run:

```bash
python cli.py ft resume <run-id>
```

This picks up all captures with status `queued`, `submitted`, or `pending` and
drives them through the archive pipeline.

## Briefing Integration

The daily briefing processor automatically includes recently validated FT
articles when available. Configure lookback in `config.yaml`:

```yaml
financial_times:
  briefing_lookback_hours: 48
  briefing_max_articles: 10
  briefing_excerpt_max_words: 100
```

Workflow:

```bash
python cli.py ft run --sections homepage,lex,unhedged --since 24h --wait
python cli.py process briefing
```

The briefing queries `ft_article_versions` joined with `ft_articles` for
recently extracted articles with `extraction_status = 'ok'`. It truncates body
text to the configured excerpt length and injects the context into the briefing
prompt as a `{{financial_times_context}}` placeholder.

The briefing is safe to run even if FT collection hasn't been run — it
gracefully reports "no FT context available."

## Raw Storage

Captured HTML is stored at the configured path:

```yaml
financial_times:
  raw_storage_path: "/var/lib/trading-data/financial_times"
```

Files are content-addressed by SHA-256 hash. In Docker, this mounts to
`./data/financial_times/`.

## Disabling Full-Text Retention

To disable archive capture while keeping RSS discovery:

```yaml
financial_times:
  enabled: true
  on_demand_only: true
  # Remove or comment out archive_host to prevent captures
```

Or simply use the `--no-ingest` flag:

```bash
python cli.py ft run --sections homepage --since 24h --no-ingest
```

## Private/Internal Use

This source is for private analysis only. Full article text is not
redistributed. FT and archive URLs are preserved for citations. Do not expose
article-serving endpoints publicly.
