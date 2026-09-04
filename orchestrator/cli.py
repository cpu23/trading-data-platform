import json
from datetime import UTC, datetime

import click
from collectors import get_all_collectors
from config_loader import load_config
from logging_config import setup_logging
from processors import get_all_processors
from sqlalchemy import text

from db import check_connection, check_tables_exist, get_session
from orchestrator import (
    get_last_collection_runs,
    run_collector,
    run_full_cycle,
    run_processor,
)

REQUIRED_TABLES = [
    "macro_series",
    "econ_events",
    "market_data",
    "structured_opinions",
    "regime_classifications",
    "daily_briefings",
    "collection_log",
    "processing_log",
]


@click.group()
def cli():
    """Trading Data Platform CLI"""
    pass


@cli.command()
@click.argument("source_id", required=False)
@click.option("--all", "run_all", is_flag=True, help="Run all enabled collectors")
def collect(source_id, run_all):
    """Run a data collector on demand."""
    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))

    if run_all:
        click.echo("Running all enabled collectors...")
        result = run_full_cycle(config=config)
        for collector_id, collector_result in result["collectors"].items():
            status = collector_result["status"]
            symbol = _status_symbol(status)
            metric_keys = ("records_fetched", "records_written", "duration_ms")
            if all(key in collector_result for key in metric_keys):
                detail = (
                    f"{collector_result['records_fetched']} fetched, "
                    f"{collector_result['records_written']} written, "
                    f"{collector_result['duration_ms']}ms"
                )
            else:
                detail = str(collector_result.get("reason") or "no metrics")
            click.echo(f"  {symbol} {collector_id}: {status} ({detail})")
        if result.get("processors"):
            click.echo("\nRunning enabled processors...")
            for proc_id, proc_result in result["processors"].items():
                status = proc_result["status"]
                symbol = _status_symbol(status)
                click.echo(
                    f"  {symbol} {proc_id}: {status} ({proc_result['duration_ms']}ms)"
                )
        click.echo(f"\nOverall: {result['status']}")
    elif source_id:
        click.echo(f"Running collector: {source_id}...")
        result = run_collector(source_id, config=config)
        symbol = _status_symbol(result["status"])
        click.echo(
            f"{symbol} {result['status']} — "
            f"{result['records_fetched']} fetched, "
            f"{result['records_written']} written, "
            f"{result['duration_ms']}ms"
        )
        if result["error"]:
            click.echo(f"Error: {result['error']}", err=True)
    else:
        click.echo("Specify a collector name or use --all", err=True)
        raise SystemExit(1)


@cli.command()
@click.argument("processor_id", required=False)
@click.option("--all", "run_all", is_flag=True, help="Run all enabled processors")
def process(processor_id, run_all):
    """Run a processor on demand."""
    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))

    if run_all:
        click.echo("Running all enabled processors...")
        all_processors = get_all_processors()
        results = {}
        for proc_id, processor in all_processors.items():
            proc_config = config.get("processors", {}).get(proc_id, {})
            if not proc_config.get("enabled", False):
                click.echo(f"  - {proc_id}: disabled")
                continue

            depends_on = processor.get_depends_on()
            if depends_on:
                click.echo(f"  Checking dependencies for {proc_id}: {depends_on}")

            click.echo(f"  Running {proc_id}...")
            result = run_processor(proc_id, config=config)
            results[proc_id] = result
            symbol = _status_symbol(result["status"])
            click.echo(
                f"  {symbol} {proc_id}: {result['status']} ({result['duration_ms']}ms)"
            )
            if result.get("error"):
                click.echo(f"    Error: {result['error']}", err=True)

        if not results:
            click.echo("No enabled processors found.")
        else:
            click.echo(f"\nProcessed {len(results)} processor(s)")

    elif processor_id:
        click.echo(f"Running processor: {processor_id}...")
        result = run_processor(processor_id, config=config)
        symbol = _status_symbol(result["status"])
        click.echo(f"{symbol} {result['status']} — {result['duration_ms']}ms")
        if result.get("error"):
            click.echo(f"Error: {result['error']}", err=True)
        if result.get("opinion_id"):
            click.echo(f"Opinion ID: {result['opinion_id']}")
    else:
        available = list(get_all_processors().keys())
        click.echo("Specify a processor name or use --all", err=True)
        click.echo(f"Available processors: {', '.join(available)}", err=True)
        raise SystemExit(1)


@cli.command()
def status():
    """Show last collection run per collector."""
    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))

    runs = get_last_collection_runs(config=config)

    if not runs:
        click.echo("No collection runs found.")
        return

    for run in runs:
        symbol = _status_symbol(run.get("status", "unknown"))
        click.echo(
            f"  {symbol} {run['collector']}: {run['status']} "
            f"@ {run['started_at']} "
            f"({run.get('records_fetched', 0)} fetched, "
            f"{run.get('records_written', 0)} written, "
            f"{run.get('duration_ms', 0)}ms)"
        )


@cli.command()
def health():
    """Run health checks for all collectors."""
    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))

    collectors = get_all_collectors()
    all_healthy = True

    for source_id, collector in collectors.items():
        collector_config = config.get("collectors", {}).get(source_id, {})
        if not collector_config.get("enabled", True):
            click.echo(f"  - {source_id}: disabled")
            continue

        result = collector.health_check(config)
        if result["healthy"]:
            symbol = click.style("OK", fg="green", bold=True)
        else:
            symbol = click.style("FAIL", fg="red", bold=True)
            all_healthy = False

        click.echo(
            f"  {symbol} {source_id}: {result['message']} ({result['latency_ms']}ms)"
        )

    if not all_healthy:
        raise SystemExit(1)


@cli.command()
def regime():
    """Show the latest macro regime classification."""
    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))

    try:
        with get_session(config) as session:
            sql = text(
                "SELECT rc.scope, rc.regime, rc.sub_regime, rc.confidence, "
                "rc.supporting_data, rc.created_at, "
                "so.opinion_type, so.direction, so.timeframe, so.summary, "
                "so.key_factors, so.reasoning, so.model_used, so.prompt_version, "
                "so.tokens_used, so.cost_usd "
                "FROM regime_classifications rc "
                "JOIN structured_opinions so ON rc.opinion_id = so.opinion_id "
                "ORDER BY rc.created_at DESC LIMIT 1"
            )
            result = session.execute(sql)
            row = result.fetchone()

        if row is None:
            click.echo(
                "No regime classification found. Run 'process macro_regime' first."
            )
            return

        r = dict(row._mapping)
        supporting = r.get("supporting_data", {})
        if isinstance(supporting, str):
            import json

            try:
                supporting = json.loads(supporting)
            except (json.JSONDecodeError, TypeError):
                supporting = {}

        key_factors = r.get("key_factors", [])
        if isinstance(key_factors, str):
            import json

            try:
                key_factors = json.loads(key_factors)
            except (json.JSONDecodeError, TypeError):
                key_factors = []

        momentum_implications = supporting.get("momentum_implications", "")
        caution_flags = supporting.get("caution_flags", [])
        key_indicators = supporting.get("key_indicators", {})

        regime_display = r["regime"].upper() if r["regime"] else "UNKNOWN"
        sub_regime_display = (
            f" ({r['sub_regime'].replace('_', ' ').title()})"
            if r.get("sub_regime") and r["sub_regime"] != "null"
            else ""
        )
        direction_display = r.get("direction", "unknown").title()
        confidence_display = r.get("confidence", "unknown").title()
        timeframe_display = r.get("timeframe", "unknown").replace("_", " ").title()

        border = click.style("=" * 47, fg="cyan")
        click.echo(f"\n{border}")
        click.echo(click.style("  MACRO REGIME ASSESSMENT", fg="cyan", bold=True))
        created_str = (
            r["created_at"].strftime("%Y-%m-%d %H:%M UTC")
            if hasattr(r["created_at"], "strftime")
            else str(r["created_at"])
        )
        click.echo(f"  Generated: {created_str}")
        click.echo(f"  Model: {r.get('model_used', 'unknown')}")
        click.echo(f"  Prompt: {r.get('prompt_version', 'unknown')}")
        click.echo(f"{border}\n")

        click.echo(
            f"  Regime:     {click.style(regime_display, bold=True)}{sub_regime_display}"
        )
        click.echo(f"  Direction:  {direction_display}")
        click.echo(f"  Confidence: {confidence_display}")
        click.echo(f"  Timeframe:  {timeframe_display}")

        if r.get("summary"):
            click.echo("\n  Summary:")
            for line in r["summary"].split("\n"):
                click.echo(f"    {line.strip()}")

        if key_factors:
            click.echo("\n  Key Factors:")
            for factor in key_factors:
                click.echo(f"    * {factor}")

        if key_indicators:
            click.echo("\n  Key Indicators:")
            for indicator, value in key_indicators.items():
                if value is not None:
                    click.echo(f"    {indicator}: {value}")

        if momentum_implications:
            click.echo("\n  Momentum Implications:")
            for line in momentum_implications.split("\n"):
                click.echo(f"    {line.strip()}")

        if caution_flags:
            click.echo("\n  Caution Flags:")
            for flag in caution_flags:
                click.echo(click.style(f"    ! {flag}", fg="yellow"))

        click.echo(f"\n{border}\n")

    except Exception as exc:
        click.echo(click.style(f"Error fetching regime: {exc}", fg="red"))
        raise SystemExit(1) from exc


@cli.command()
def briefing():
    """Display today's daily briefing."""
    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))

    try:
        with get_session(config) as session:
            sql = text(
                "SELECT briefing_date, created_at, content, sections, "
                "model_used, prompt_version "
                "FROM daily_briefings "
                "WHERE briefing_date = CURRENT_DATE "
                "ORDER BY created_at DESC LIMIT 1"
            )
            result = session.execute(sql)
            row = result.fetchone()

        if row is None:
            with get_session(config) as session:
                sql = text(
                    "SELECT briefing_date, created_at, content, sections, "
                    "model_used, prompt_version "
                    "FROM daily_briefings "
                    "ORDER BY briefing_date DESC, created_at DESC LIMIT 1"
                )
                result = session.execute(sql)
                row = result.fetchone()

            if row is None:
                click.echo("No briefings have been generated yet.")
                click.echo(
                    "Run 'collect --all' then 'process --all' to generate a briefing."
                )
                return

            r = dict(row._mapping)
            briefing_date = r["briefing_date"]
            click.echo(
                click.style(
                    f"Note: No briefing for today. Showing most recent briefing from {briefing_date}.",
                    fg="yellow",
                )
            )
            click.echo("")
        else:
            r = dict(row._mapping)
            briefing_date = r["briefing_date"]

        created_str = (
            r["created_at"].strftime("%Y-%m-%d %H:%M UTC")
            if hasattr(r["created_at"], "strftime")
            else str(r["created_at"])
        )

        border = click.style("═" * 47, fg="cyan")
        click.echo(f"\n{border}")
        click.echo(
            click.style(f"  DAILY BRIEFING — {briefing_date}", fg="cyan", bold=True)
        )
        click.echo(f"  Generated: {created_str}")
        click.echo(f"  Model: {r.get('model_used', 'unknown')}")
        click.echo(f"  Prompt: {r.get('prompt_version', 'unknown')}")
        click.echo(f"{border}\n")

        content = r.get("content", "")
        if content:
            for line in content.split("\n"):
                click.echo(f"  {line}")
        else:
            click.echo("  No content available.")

        click.echo(f"\n{border}\n")

    except Exception as exc:
        click.echo(click.style(f"Error fetching briefing: {exc}", fg="red"))
        raise SystemExit(1) from exc


@cli.command("reconcile-analysis-atoms")
def reconcile_analysis_atoms():
    """Expire analysis atoms past their horizon (no model calls)."""
    import json as json_mod

    from atoms import expire_atoms

    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))
    with get_session(config) as session:
        summary = expire_atoms(session, config)
    click.echo(json_mod.dumps(summary, indent=2))


@cli.command("recompute-reaction-windows")
@click.option(
    "--limit",
    default=100,
    show_default=True,
    type=click.IntRange(1, 500),
    help="Maximum windows to recompute in this run.",
)
@click.option(
    "--event-id",
    "event_id",
    default=None,
    help="Optional event UUID to scope recompute to one event.",
)
@click.option(
    "--legacy-only",
    is_flag=True,
    help="Only re-derive pre-044 rows (volatility_version NULL or < current).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would change without updating any rows.",
)
def recompute_reaction_windows(
    limit: int, event_id: str | None, legacy_only: bool, dry_run: bool
):
    """Re-derive reaction windows with current selection and calendar rules."""
    import json as json_mod

    from reaction_windows import recompute_reaction_windows as recompute

    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))
    with get_session(config) as session:
        summary = recompute(
            session,
            config,
            limit=limit,
            event_id=event_id,
            legacy_only=legacy_only,
            dry_run=dry_run,
        )
    click.echo(json_mod.dumps(summary, indent=2))


@cli.command("filing-delta")
@click.argument("document_id")
def filing_delta(document_id: str):
    """Compute the deterministic delta for one ingested filing."""
    import json as json_mod

    from filing_deltas import compute_filing_delta

    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))
    summary = compute_filing_delta(config, document_id)
    click.echo(json_mod.dumps(summary, indent=2))


@cli.command("db-check")
def db_check():
    """Verify DB connection and table existence."""
    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))

    connected = check_connection(config)
    if connected:
        click.echo(
            click.style("OK", fg="green", bold=True) + " Database connection successful"
        )
    else:
        click.echo(
            click.style("FAIL", fg="red", bold=True) + " Database connection failed"
        )
        raise SystemExit(1)

    table_status = check_tables_exist(REQUIRED_TABLES, config=config)
    for table_name, exists in table_status.items():
        if exists:
            symbol = click.style("OK", fg="green")
        else:
            symbol = click.style("MISSING", fg="red")
        click.echo(f"  {symbol} {table_name}")

    missing = [t for t, exists in table_status.items() if not exists]
    if missing:
        click.echo(f"\nMissing tables: {', '.join(missing)}")
        raise SystemExit(1)


# ── News feed commands ────────────────────────────────────────────────────


@cli.group()
def news():
    """News feed: Reuters sitemap, Kobeissi tweets, unified feed."""
    pass


@news.command("reuters")
@click.option("--pages", type=int, default=None, help="Max sitemap pages to scan")
@click.option("--json", "output_json", is_flag=True)
def news_reuters(pages, output_json):
    """Poll Reuters sitemap for market-relevant articles."""
    from sources.news_feed import collect_and_publish
    from sources.reuters import run_reuters

    config = load_config()
    reuters_config = config.get("reuters", {})
    if not reuters_config.get("enabled"):
        click.echo("Reuters source is disabled in config.", err=True)
        raise SystemExit(1)
    pages = pages if pages is not None else reuters_config.get("max_pages", 3)
    result = collect_and_publish(
        "reuters", config, lambda: run_reuters(config, max_pages=pages)
    )
    if result.status == "error":
        click.echo(f"Reuters collection failed: {result.error}", err=True)
        raise SystemExit(1)
    articles = result.items
    if output_json:
        import json as json_mod

        click.echo(json_mod.dumps(articles, indent=2, default=str))
    else:
        click.echo(f"Found {len(articles)} new market-relevant articles:")
        for a in articles[:15]:
            kws = ", ".join(a.get("matched_keywords", []))
            click.echo(f"  {a['title'][:80]}")
            click.echo(f"    {kws} | {a['url']}")
        if len(articles) > 15:
            click.echo(f"  ... and {len(articles) - 15} more")


@news.command("kobeissi")
@click.option("--count", type=int, default=None, help="Number of tweets to fetch")
@click.option("--json", "output_json", is_flag=True)
def news_kobeissi(count, output_json):
    """Fetch Kobeissi Letter tweets."""
    from sources.kobeissi import run_kobeissi
    from sources.news_feed import collect_and_publish

    config = load_config()
    kobeissi_config = config.get("kobeissi", {})
    if not kobeissi_config.get("enabled"):
        click.echo("Kobeissi source is disabled in config.", err=True)
        raise SystemExit(1)
    count = count if count is not None else kobeissi_config.get("count", 20)
    result = collect_and_publish(
        "kobeissi", config, lambda: run_kobeissi(config, count=count)
    )
    if result.status == "error":
        click.echo(f"Kobeissi collection failed: {result.error}", err=True)
        raise SystemExit(1)
    tweets = result.items
    if output_json:
        import json as json_mod

        click.echo(json_mod.dumps(tweets, indent=2, default=str))
    else:
        click.echo(f"Found {len(tweets)} new tweets:")
        for t in tweets[:10]:
            syms = f"  ${'  $'.join(t.get('symbols', []))}" if t.get("symbols") else ""
            click.echo(f"  {t['title'][:100]}...{syms}")
        if len(tweets) > 10:
            click.echo(f"  ... and {len(tweets) - 10} more")


@news.command("feed")
@click.option("--days", type=int, default=None, help="Days of history to include")
@click.option("--json", "output_json", is_flag=True)
def news_feed_cmd(days, output_json):
    """Build unified feed.json from all sources."""
    from sources.news_feed import build_feed

    config = load_config()
    days = (
        days if days is not None else config.get("news_feed", {}).get("history_days", 7)
    )
    feed = build_feed(config, days=days)
    if output_json:
        import json as json_mod

        click.echo(json_mod.dumps(feed, indent=2, default=str))
    else:
        click.echo(
            f"Feed built: {feed['count']} items from {len(feed['sources'])} sources"
        )
        for item in feed["items"][:10]:
            click.echo(f"  [{item['source']}] {item['title'][:80]}")
        if feed["count"] > 10:
            click.echo(f"  ... and {feed['count'] - 10} more")


@news.command("all")
@click.option("--pages", type=int, default=None, help="Max Reuters sitemap pages")
@click.option("--count", type=int, default=None, help="Kobeissi tweets to fetch")
@click.option("--days", type=int, default=None, help="Feed history days")
def news_all(pages, count, days):
    """Run all news sources and build feed."""
    from pathlib import Path

    from sources.kobeissi import run_kobeissi
    from sources.news_feed import collect_and_publish
    from sources.news_storage import read_json
    from sources.reuters import run_reuters

    config = load_config()
    pages = (
        pages if pages is not None else config.get("reuters", {}).get("max_pages", 3)
    )
    count = count if count is not None else config.get("kobeissi", {}).get("count", 20)
    days = (
        days if days is not None else config.get("news_feed", {}).get("history_days", 7)
    )
    failed = False

    if config.get("reuters", {}).get("enabled", False):
        result = collect_and_publish(
            "reuters", config, lambda: run_reuters(config, max_pages=pages), days=days
        )
        if result.status == "error":
            click.echo(f"Reuters: failed — {result.error}", err=True)
            failed = True
        else:
            click.echo(f"Reuters: {len(result.items)} articles")
    else:
        click.echo("Reuters: disabled")

    if config.get("kobeissi", {}).get("enabled", False):
        result = collect_and_publish(
            "kobeissi", config, lambda: run_kobeissi(config, count=count), days=days
        )
        if result.status == "error":
            click.echo(f"Kobeissi: failed — {result.error}", err=True)
            failed = True
        else:
            click.echo(f"Kobeissi: {len(result.items)} tweets")
    else:
        click.echo("Kobeissi: disabled")

    output_dir = Path(
        config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news")
    )
    feed = read_json(output_dir / "feed.json", {"count": 0})
    click.echo(f"Feed: {feed.get('count', 0)} items total")
    if failed:
        raise SystemExit(1)


def _emit_json(value):
    click.echo(json.dumps(value, sort_keys=True, indent=2, default=str))


@cli.command("research-run")
@click.option(
    "--force", is_flag=True, help="Re-run model stages instead of cached outputs."
)
def research_run(force):
    """Queue the bounded macro and dynamic research workflow now."""
    from research_intelligence.operations import enqueue_research_job

    try:
        result = enqueue_research_job(
            load_config(),
            job_type="research_discovery",
            force=force,
            triggered_by="cli",
        )
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    _emit_json(result)


@cli.command("research-update")
@click.argument("case_id")
@click.option(
    "--force", is_flag=True, help="Re-run model stages instead of cached outputs."
)
def research_update(case_id, force):
    """Queue one research case for bounded incremental update."""
    from research_intelligence.operations import enqueue_research_job

    try:
        result = enqueue_research_job(
            load_config(),
            job_type="research_case_update",
            case_id=case_id,
            force=force,
            triggered_by="cli",
        )
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    _emit_json(result)


@cli.command("research-rebuild")
def research_rebuild():
    """Queue a bounded cache-bypassing research rebuild."""
    from research_intelligence.operations import enqueue_research_job

    try:
        result = enqueue_research_job(
            load_config(),
            job_type="research_discovery",
            force=True,
            triggered_by="cli",
        )
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    _emit_json(result)


@cli.command("research-retry")
@click.argument("job_id")
def research_retry(job_id):
    """Retry failed research work without mutating prior job history."""
    from research_intelligence.operations import retry_research_job

    try:
        result = retry_research_job(load_config(), job_id)
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    _emit_json(result)


@cli.command("research-status")
@click.option("--limit", default=20, type=click.IntRange(1, 100), show_default=True)
def research_status_command(limit):
    """Inspect research case counts, cold-data requests, costs, and jobs."""
    from research_intelligence.queries import research_status

    config = load_config()
    try:
        with get_session(config) as session:
            result = research_status(session, limit=limit)
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    _emit_json(result)


@cli.command("research-inspect")
@click.argument("case_id", required=False)
@click.option("--limit", default=20, type=click.IntRange(1, 100), show_default=True)
def research_inspect(case_id, limit):
    """Inspect one case and history, or a bounded current case list."""
    from research_intelligence.queries import case_history, get_case, list_cases

    config = load_config()
    try:
        with get_session(config) as session:
            if case_id:
                detail = get_case(session, case_id, detail_limit=limit)
                if detail is None:
                    raise click.ClickException("research case not found")
                result = {
                    **detail,
                    "history": case_history(session, case_id, limit=limit),
                }
            else:
                result = {"cases": list_cases(session, limit=limit)}
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    _emit_json(result)


def _research_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise click.BadParameter("expected an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise click.BadParameter("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _research_overrides(values: tuple[str, ...], label: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for value in values:
        stage, separator, setting = value.partition("=")
        if not separator or not stage.strip() or not setting.strip():
            raise click.BadParameter(f"{label} must use STAGE=VALUE")
        if stage.strip() in output:
            raise click.BadParameter(f"duplicate {label} stage: {stage.strip()}")
        output[stage.strip()] = setting.strip()
    return output


@cli.group("research")
def research_evaluation():
    """Point-in-time research replay and quality evaluation."""


@research_evaluation.command("replay")
@click.option("--as-of", "as_of", required=True, help="Timezone-aware ISO timestamp.")
@click.option(
    "--model", "models", multiple=True, help="Per-stage STAGE=MODEL override."
)
@click.option(
    "--prompt", "prompts", multiple=True, help="Per-stage STAGE=PATH override."
)
@click.option("--comparison-group", default=None, type=str)
@click.option("--force", is_flag=True, help="Bypass reusable model-stage outputs.")
def research_replay(as_of, models, prompts, comparison_group, force):
    """Replay source-owned database evidence at one strict cutoff."""
    from research_intelligence.replay import run_database_replay

    replay_as_of = _research_timestamp(as_of)
    model_overrides = _research_overrides(models, "model override")
    prompt_overrides = _research_overrides(prompts, "prompt override")
    failure: Exception | None = None
    result: dict[str, object] | None = None
    with get_session(load_config()) as session:
        try:
            run_id, execution, adapter_failures = run_database_replay(
                session,
                load_config(),
                replay_as_of,
                model_overrides=model_overrides,
                prompt_overrides=prompt_overrides,
                comparison_group=comparison_group,
                force=force,
            )
            result = {
                "run_id": run_id,
                "replay_as_of": execution.replay_as_of,
                "case_count": len(execution.cases),
                "candidate_count": execution.candidate_count,
                "errors": list(execution.errors),
                "adapter_failures": dict(adapter_failures),
            }
        except Exception as exc:
            failure = exc
    if failure is not None:
        raise click.ClickException(
            f"{type(failure).__name__}: {str(failure)[:300]}"
        ) from failure
    _emit_json(result)


@research_evaluation.group("benchmark")
def research_benchmark():
    """Run and compare version-controlled benchmark episodes."""


@research_benchmark.command("list")
def research_benchmark_list():
    """List authored replay episodes and dates."""
    from research_intelligence.benchmarks import list_benchmarks

    _emit_json(
        [
            {
                "id": item.episode_id,
                "version": item.version,
                "kind": item.episode_kind,
                "synthetic": item.synthetic,
                "replay_dates": list(item.replay_dates),
                "evidence_count": len(item.evidence),
            }
            for item in list_benchmarks()
        ]
    )


@research_benchmark.command("run")
@click.argument("benchmark_id")
@click.option(
    "--date", "dates", multiple=True, help="Authored ISO replay date; repeatable."
)
@click.option(
    "--model", "models", multiple=True, help="Per-stage STAGE=MODEL override."
)
@click.option(
    "--prompt", "prompts", multiple=True, help="Per-stage STAGE=PATH override."
)
@click.option("--comparison-group", default=None, type=str)
@click.option("--force", is_flag=True, help="Bypass reusable model-stage outputs.")
def research_benchmark_run(
    benchmark_id, dates, models, prompts, comparison_group, force
):
    """Run one benchmark across all authored dates or selected dates."""
    from research_intelligence.benchmarks import get_benchmark
    from research_intelligence.replay import run_benchmark_replay_date

    episode = get_benchmark(benchmark_id)
    requested_dates = tuple(_research_timestamp(value) for value in dates)
    selected = requested_dates or episode.replay_dates
    unsupported = [value for value in selected if value not in episode.replay_dates]
    if unsupported:
        raise click.BadParameter("date must be one of the benchmark's authored dates")
    model_overrides = _research_overrides(models, "model override")
    prompt_overrides = _research_overrides(prompts, "prompt override")
    results: list[dict[str, object]] = []
    failures: list[str] = []
    with get_session(load_config()) as session:
        for replay_as_of in selected:
            try:
                run_id, execution = run_benchmark_replay_date(
                    session,
                    load_config(),
                    episode,
                    replay_as_of,
                    model_overrides=model_overrides,
                    prompt_overrides=prompt_overrides,
                    comparison_group=comparison_group,
                    force=force,
                )
                results.append(
                    {
                        "run_id": run_id,
                        "replay_as_of": replay_as_of,
                        "case_count": len(execution.cases),
                        "candidate_count": execution.candidate_count,
                        "errors": list(execution.errors),
                    }
                )
            except Exception as exc:
                failures.append(
                    f"{replay_as_of.isoformat()} {type(exc).__name__}: {str(exc)[:240]}"
                )
    _emit_json(
        {
            "benchmark_id": benchmark_id,
            "runs": results,
            "failures": failures,
        }
    )
    if failures:
        raise click.ClickException(f"{len(failures)} benchmark date(s) failed")


@research_benchmark.command("compare")
@click.argument("left_run_id")
@click.argument("right_run_id")
def research_benchmark_compare(left_run_id, right_run_id):
    """Compare two runs only when deterministic inputs are identical."""
    from research_intelligence.evaluation import compare_replay_runs

    with get_session(load_config()) as session:
        try:
            result = compare_replay_runs(session, left_run_id, right_run_id)
        except Exception as exc:
            raise click.ClickException(
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc
    _emit_json(result)


@research_benchmark.command("annotate")
@click.argument("replay_run_id")
@click.option(
    "--overall-label",
    type=click.Choice(("pass", "partial", "fail", "unclear")),
    default=None,
)
@click.option(
    "--dimension",
    "dimensions",
    multiple=True,
    help="Human DIMENSION=LABEL review; repeatable.",
)
@click.option("--notes", default=None, type=str)
@click.option("--annotated-by", required=True, type=str)
@click.option("--expected-version", default=None, type=click.IntRange(min=0))
def research_benchmark_annotate(
    replay_run_id,
    overall_label,
    dimensions,
    notes,
    annotated_by,
    expected_version,
):
    """Append a versioned human review without changing deterministic scores."""
    from research_intelligence.scorecards import annotate_benchmark_scorecard

    annotations: dict[str, object] = {}
    if overall_label is not None:
        annotations["overall_label"] = overall_label
    if dimensions:
        annotations["dimension_labels"] = _research_overrides(
            dimensions, "dimension review"
        )
    if notes is not None:
        annotations["notes"] = notes
    with get_session(load_config()) as session:
        try:
            result = annotate_benchmark_scorecard(
                session,
                replay_run_id,
                annotations,
                annotated_by=annotated_by,
                expected_version=expected_version,
            )
        except Exception as exc:
            raise click.ClickException(
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc
    _emit_json(result)


@research_evaluation.command("metrics")
@click.option("--scope", default=None, type=str)
@click.option("--benchmark-id", default=None, type=str)
@click.option("--limit", default=50, type=click.IntRange(1, 200), show_default=True)
def research_metrics(scope, benchmark_id, limit):
    """Inspect persisted deterministic research-quality metrics."""
    from research_intelligence.queries import list_quality_metrics

    with get_session(load_config()) as session:
        try:
            rows = list_quality_metrics(
                session,
                metric_scope=scope,
                benchmark_id=benchmark_id,
                limit=limit,
            )
        except Exception as exc:
            raise click.ClickException(
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc
    _emit_json(rows)


@research_evaluation.command("cohorts")
@click.option("--since", default=None, help="Timezone-aware ISO timestamp.")
@click.option("--persist/--no-persist", default=True, show_default=True)
def research_cohorts(since, persist):
    """Calculate live case survival and weak-case cohort metrics."""
    from research_intelligence.evaluation import persist_live_case_cohorts
    from research_intelligence.queries import live_case_cohorts

    cutoff = _research_timestamp(since) if since else None
    with get_session(load_config()) as session:
        rows = live_case_cohorts(session, since=cutoff)
        inserted = persist_live_case_cohorts(session, rows) if persist else 0
    _emit_json({"cohorts": rows, "persisted": inserted})


@research_evaluation.command("inspect-replay")
@click.argument("replay_run_id")
@click.option("--limit", default=100, type=click.IntRange(1, 200), show_default=True)
def research_inspect_replay(replay_run_id, limit):
    """Inspect one replay, cases, timeline, and persisted metrics."""
    from research_intelligence.queries import get_replay_run

    with get_session(load_config()) as session:
        try:
            result = get_replay_run(session, replay_run_id, detail_limit=limit)
        except Exception as exc:
            raise click.ClickException(
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc
    if result is None:
        raise click.ClickException("research replay not found")
    _emit_json(result)


@cli.group("roles")
def roles_group():
    """Run or check orchestrator process roles."""


@roles_group.command("run")
@click.argument("role", type=click.Choice(["worker"]))
def roles_run(role):
    """Run one role process in the foreground (blocks until signal)."""
    import roles

    raise SystemExit(roles._ROLE_RUNNERS[role]())


@roles_group.command("check")
@click.argument("role", type=click.Choice(["worker"]))
@click.option(
    "--stale-after",
    type=float,
    default=None,
    help="Staleness window in seconds (default ROLE_HEARTBEAT_TIMEOUT_SECONDS or 90).",
)
def roles_check(role, stale_after):
    """One-shot durable liveness check; exit code is the health signal."""
    import roles

    raise SystemExit(roles.check_role(role, stale_after))


def _status_symbol(status: str) -> str:
    if status == "success":
        return click.style("OK", fg="green", bold=True)
    elif status == "partial":
        return click.style("PARTIAL", fg="yellow", bold=True)
    else:
        return click.style("FAIL", fg="red", bold=True)


if __name__ == "__main__":
    cli()
