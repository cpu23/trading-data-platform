import click

from config_loader import load_config
from db import check_connection, check_tables_exist, get_session, query_latest
from logging_config import setup_logging
from collectors import get_all_collectors
from processors import get_all_processors
from migrate import run_migrations
from orchestrator import (
    run_collector,
    run_full_cycle,
    run_processor,
    get_last_collection_runs,
)
from sqlalchemy import text


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
        click.echo(f"Specify a processor name or use --all", err=True)
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
            click.echo(f"\n  Summary:")
            for line in r["summary"].split("\n"):
                click.echo(f"    {line.strip()}")

        if key_factors:
            click.echo(f"\n  Key Factors:")
            for factor in key_factors:
                click.echo(f"    * {factor}")

        if key_indicators:
            click.echo(f"\n  Key Indicators:")
            for indicator, value in key_indicators.items():
                if value is not None:
                    click.echo(f"    {indicator}: {value}")

        if momentum_implications:
            click.echo(f"\n  Momentum Implications:")
            for line in momentum_implications.split("\n"):
                click.echo(f"    {line.strip()}")

        if caution_flags:
            click.echo(f"\n  Caution Flags:")
            for flag in caution_flags:
                click.echo(click.style(f"    ! {flag}", fg="yellow"))

        click.echo(f"\n{border}\n")

    except Exception as exc:
        click.echo(click.style(f"Error fetching regime: {exc}", fg="red"))
        raise SystemExit(1)


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
                click.echo("Run 'collect --all' then 'process --all' to generate a briefing.")
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
        raise SystemExit(1)


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


def _parse_since(since_str: str):
    """Parse --since argument: '24h', '7d', or ISO date."""
    from datetime import datetime, timedelta, timezone
    import re
    m = re.match(r"^(\d+)(h|d)$", since_str)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        return datetime.now(timezone.utc) - delta
    try:
        return datetime.fromisoformat(since_str)
    except ValueError:
        raise click.BadParameter(f"Invalid --since value: {since_str}. Use '24h', '7d', or ISO date.")


VALID_SECTIONS = {"homepage", "lex", "unhedged"}


@cli.group()
def ft():
    """Financial Times on-demand ingestion."""
    pass


@ft.command("discover")
@click.option("--sections", default="homepage,lex,unhedged", help="Comma-separated feed sections")
@click.option("--since", default="24h", help="Time window (e.g. 24h, 7d, or ISO date)")
@click.option("--json", "output_json", is_flag=True, help="Machine-readable JSON output")
def ft_discover(sections, since, output_json):
    """Discover FT articles from RSS without archive ingestion."""
    from sources.financial_times import run_financial_times
    from uuid import uuid4
    config = load_config()
    section_list = [s.strip() for s in sections.split(",")]
    invalid = [s for s in section_list if s not in VALID_SECTIONS]
    if invalid:
        click.echo(f"Invalid sections: {', '.join(invalid)}. Valid: {', '.join(sorted(VALID_SECTIONS))}", err=True)
        raise SystemExit(1)
    since_dt = _parse_since(since)
    result = run_financial_times(
        config=config,
        correlation_id=str(uuid4()),
        sections=tuple(section_list),
        since=since_dt,
        ingest=False,
    )
    if output_json:
        import json as json_mod
        click.echo(json_mod.dumps(result, indent=2, default=str))
    else:
        click.echo(f"Discovered {result['articles_discovered']} articles")
        for art in result.get("articles", []):
            click.echo(f"  {art.get('content_id', '?')}: {art.get('canonical_url', '?')}")


@ft.command("run")
@click.option("--sections", default="homepage,lex,unhedged")
@click.option("--since", default="24h")
@click.option("--until", default=None)
@click.option("--max-articles", type=int, default=None)
@click.option("--no-ingest", is_flag=True, help="Discovery only, no archive submission")
@click.option("--wait/--no-wait", default=True, help="Wait for archive captures")
@click.option("--json", "output_json", is_flag=True)
def ft_run(sections, since, until, max_articles, no_ingest, wait, output_json):
    """Run full FT collection (discover + archive)."""
    from sources.financial_times import run_financial_times
    from uuid import uuid4
    config = load_config()
    section_list = [s.strip() for s in sections.split(",")]
    invalid = [s for s in section_list if s not in VALID_SECTIONS]
    if invalid:
        click.echo(f"Invalid sections: {', '.join(invalid)}. Valid: {', '.join(sorted(VALID_SECTIONS))}", err=True)
        raise SystemExit(1)
    since_dt = _parse_since(since)
    until_dt = _parse_since(until) if until else None
    result = run_financial_times(
        config=config,
        correlation_id=str(uuid4()),
        sections=tuple(section_list),
        since=since_dt,
        until=until_dt,
        max_articles=max_articles,
        ingest=not no_ingest,
        wait_for_capture=wait,
    )
    if output_json:
        import json as json_mod
        click.echo(json_mod.dumps(result, indent=2, default=str))
    else:
        symbol = _status_symbol(result["status"])
        click.echo(f"{symbol} Discovered: {result['articles_discovered']}, Captured: {result['articles_captured']}, Failed: {result['articles_failed']}")
        for art in result.get("articles", []):
            status = art.get("status", "?")
            click.echo(f"  [{status}] {art.get('content_id', '?')}: {art.get('canonical_url', '?')}")


@ft.command("resume")
@click.argument("run_id")
@click.option("--json", "output_json", is_flag=True)
def ft_resume(run_id, output_json):
    """Resume a failed/partial FT collection run."""
    from sources.financial_times import resume_ft_captures
    from uuid import uuid4
    config = load_config()
    result = resume_ft_captures(config=config, correlation_id=str(uuid4()))
    if output_json:
        import json as json_mod
        click.echo(json_mod.dumps(result, indent=2, default=str))
    else:
        click.echo(f"Resumed {result['captures_resumed']} captures: {result['captures_succeeded']} succeeded, {result['captures_failed']} failed")


@ft.command("status")
@click.argument("run_id", required=False)
@click.option("--json", "output_json", is_flag=True)
def ft_status(run_id, output_json):
    """Show status of FT collection run(s)."""
    from sources.financial_times_repository import get_ft_run, get_recent_ft_runs
    config = load_config()
    with get_session(config) as session:
        if run_id:
            run = get_ft_run(session, run_id)
            if not run:
                click.echo(f"Run not found: {run_id}", err=True)
                raise SystemExit(1)
            runs = [run]
        else:
            runs = get_recent_ft_runs(session)
    if output_json:
        import json as json_mod
        click.echo(json_mod.dumps(runs, indent=2, default=str))
    else:
        if not runs:
            click.echo("No FT collection runs found.")
            return
        for run in runs:
            symbol = _status_symbol(run.get("status", "unknown"))
            click.echo(f"  {symbol} {run['run_id']}: {run.get('status')} — discovered: {run.get('articles_discovered', 0)}, captured: {run.get('articles_captured', 0)}, failed: {run.get('articles_failed', 0)}")


def _status_symbol(status: str) -> str:
    if status == "success":
        return click.style("OK", fg="green", bold=True)
    elif status == "partial":
        return click.style("PARTIAL", fg="yellow", bold=True)
    else:
        return click.style("FAIL", fg="red", bold=True)


if __name__ == "__main__":
    cli()
