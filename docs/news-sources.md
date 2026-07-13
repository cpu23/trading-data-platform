# News sources

The production news feed contains two sources only: Reuters news sitemaps and
Kobeissi Letter posts fetched from TwitterAPI.io.

## Configuration and credentials

`config/config.yaml` controls each source. `reuters.enabled` and
`kobeissi.enabled` decide whether `news all` runs and reads that source.
TwitterAPI.io requires `TWITTERAPI_KEY`; Reuters requires no credential. Never
store credentials in source control.

Production state and snapshots live under `/var/lib/trading-data/news`. Compose
bind-mounts `./data/news` read-write into the orchestrator and read-only into the
API, so restarts preserve source cursors and both services see the same feed.

## Commands

Run from `orchestrator/`:

```bash
uv run python cli.py news reuters --pages 3
uv run python cli.py news kobeissi --count 20
uv run python cli.py news feed --days 7
uv run python cli.py news all
```

Schedule `news all` externally at the desired polling interval; both sources are
on-demand by default. Disabled sources are skipped.

## API

Authenticated endpoints:

- `GET /api/news/feed` returns the normalized feed.
- `GET /api/news/sources` reports enabled state, last poll, status, and error.

A feed item always includes `id`, `source`, `source_label`, `title`, `summary`,
`url`, `published`, `symbols`, `tags`, `engagement`, `media`, `meta`, and
`fetched_at`.

## Reliability and recovery

State and daily snapshots are replaced atomically. IDs are deduplicated, Reuters
keeps a bounded ordered URL cursor, and Twitter IDs are compared numerically.
Malformed state is treated as empty state and rewritten on the next successful
poll. Malformed snapshots are skipped with a warning; malformed or invalid
`feed.json` returns HTTP 503 rather than an application error. Deleting only a
corrupt state file forces safe rediscovery; snapshot ID deduplication prevents
duplicate feed entries.

Reuters sitemap availability and filtering determine coverage. TwitterAPI.io
rate limits, account timeline behavior, and API availability determine Kobeissi
coverage. This subsystem stores summaries and links, not full Reuters articles.
