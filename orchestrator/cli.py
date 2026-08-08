from pathlib import Path

import click
from sqlalchemy import text

from collectors import get_all_collectors
from config_loader import load_config
from db import check_connection, check_tables_exist, get_session
from logging_config import setup_logging
from migrate import run_migrations
from orchestrator import (
    get_last_collection_runs,
    run_collector,
    run_full_cycle,
    run_processor,
)
from processors import get_all_processors

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
            click.echo(
                f"  {symbol} {collector_id}: {status} "
                f"({collector_result['records_fetched']} fetched, "
                f"{collector_result['records_written']} written, "
                f"{collector_result['duration_ms']}ms)"
            )
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


@cli.command()
def migrate():
    """Apply pending database migrations."""
    config = load_config()
    applied = run_migrations(config)
    if applied:
        click.echo(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        click.echo("No pending migrations.")


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


@cli.command("benchmark-models")
@click.option(
    "--models",
    default="deepseek/deepseek-v4-flash-0731,openai/gpt-5.6-luna",
    help="Comma-separated pinned OpenRouter slugs to compare.",
)
@click.option("--suite", default="core", help="Fixture suite directory to run.")
@click.option(
    "--runs",
    default=3,
    type=click.IntRange(min=1),
    show_default=True,
    help="Runs per case and model.",
)
@click.option(
    "--output",
    default=None,
    help="Artifact directory (default: ../artifacts/model-benchmarks/<timestamp>).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Bypass the daily budget cap for this explicit evaluation run.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Write fixture request bodies only; performs no model calls.",
)
@click.option(
    "--omit-temperature",
    is_flag=True,
    help="Omit temperature uniformly for models that do not support sampling controls.",
)
def benchmark_models(models, suite, runs, output, force, dry_run, omit_temperature):
    """Offline model evaluation harness (spec §18). Paid inference only on
    explicit operator invocation; never runs during production processing."""
    from datetime import UTC, datetime
    from pathlib import Path

    from model_benchmark import (
        FixtureError,
        parse_model_list,
        run_benchmark,
    )

    try:
        model_list = parse_model_list(models)
    except FixtureError as exc:
        click.echo(f"Invalid --models: {exc}", err=True)
        raise SystemExit(1) from exc
    if runs < 1:
        click.echo("--runs must be at least 1", err=True)
        raise SystemExit(1)
    if output is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_dir = Path(__file__).resolve().parent.parent / (
            f"artifacts/model-benchmarks/{timestamp}"
        )
    else:
        output_dir = Path(output)
    config = None
    if not dry_run:
        container_path = Path("/app/config/config.yaml")
        local_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
        config = load_config(
            str(container_path if container_path.exists() else local_path)
        )
    setup_logging(level=(config or {}).get("logging", {}).get("level", "INFO"))
    click.echo(f"Benchmark: suite={suite} models={','.join(model_list)} runs={runs}")
    try:
        summary = run_benchmark(
            config,
            models=model_list,
            suite=suite,
            runs=runs,
            output_dir=output_dir,
            force=force,
            dry_run=dry_run,
            include_temperature=not omit_temperature,
        )
    except FixtureError as exc:
        click.echo(f"Fixture error: {exc}", err=True)
        raise SystemExit(1) from exc
    if dry_run:
        click.echo(f"Dry run complete: {summary['cases']} cases -> {output_dir}")
        return
    for model, metrics in summary.get("models", {}).items():
        click.echo(
            f"{model}: first_pass={metrics.get('schema_valid_first_pass_rate', 0):.0%} "
            f"after_repair={metrics.get('schema_valid_after_repair_rate', 0):.0%} "
            f"mean_cost=${metrics.get('mean_cost_usd', 0):.4f}"
        )
    click.echo(f"Artifacts: {output_dir}")


@cli.command("benchmark-score")
@click.option(
    "--artifact",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Completed benchmark artifact directory.",
)
@click.option(
    "--review",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="blind-review-scores.json downloaded from blind-review.html.",
)
def benchmark_score(artifact, review):
    """Validate blind scores and finalize the weighted promotion decision."""
    from model_benchmark import FixtureError, apply_blind_review_scores

    try:
        summary = apply_blind_review_scores(artifact, review)
    except FixtureError as exc:
        click.echo(f"Blind review error: {exc}", err=True)
        raise SystemExit(1) from exc
    decision = summary["decision"]
    click.echo(f"Blind review complete: {decision['blind_review_complete']}")
    click.echo(f"Recommended: {decision.get('recommended') or 'none eligible'}")
    click.echo(f"Updated: {artifact / 'summary.json'}")


def _status_symbol(status: str) -> str:
    if status == "success":
        return click.style("OK", fg="green", bold=True)
    elif status == "partial":
        return click.style("PARTIAL", fg="yellow", bold=True)
    else:
        return click.style("FAIL", fg="red", bold=True)


if __name__ == "__main__":
    cli()
