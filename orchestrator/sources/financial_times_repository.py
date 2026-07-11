"""Persistence layer for Financial Times articles, captures, and versions.

All functions accept an active SQLAlchemy session and use parameterised SQL
with ON CONFLICT for idempotent writes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text


# ---------------------------------------------------------------------------
# Article + Observation upsert
# ---------------------------------------------------------------------------

def upsert_article_observation(
    session,
    article_id: str,
    content_id: str,
    canonical_url: str,
    title: str | None,
    description: str | None,
    published_at: datetime | None,
    feed_id: str,
    rss_payload: dict,
    now: datetime | None = None,
) -> dict:
    """Insert or update an article and record a new observation.

    Idempotent: re-observing the same (article, feed, timestamp) is a no-op.
    Returns the article row as a dict.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Upsert article — update metadata if it already exists
    article_sql = text("""
        INSERT INTO ft_articles
            (article_id, content_id, canonical_url, latest_title,
             latest_description, published_at, first_seen_at, last_seen_at)
        VALUES
            (:article_id, :content_id, :canonical_url, :latest_title,
             :latest_description, :published_at, :first_seen_at, :last_seen_at)
        ON CONFLICT (article_id) DO UPDATE SET
            latest_title = COALESCE(EXCLUDED.latest_title, ft_articles.latest_title),
            latest_description = COALESCE(EXCLUDED.latest_description, ft_articles.latest_description),
            published_at = COALESCE(EXCLUDED.published_at, ft_articles.published_at),
            last_seen_at = EXCLUDED.last_seen_at
    """)
    session.execute(article_sql, {
        "article_id": article_id,
        "content_id": content_id,
        "canonical_url": canonical_url,
        "latest_title": title,
        "latest_description": description,
        "published_at": published_at,
        "first_seen_at": now,
        "last_seen_at": now,
    })

    # Insert observation — ignore duplicates on (article_id, feed_id, observed_at)
    obs_sql = text("""
        INSERT INTO ft_article_observations
            (article_id, feed_id, observed_at, rss_payload)
        VALUES
            (:article_id, :feed_id, :observed_at, :rss_payload)
        ON CONFLICT (article_id, feed_id, observed_at) DO NOTHING
    """)
    session.execute(obs_sql, {
        "article_id": article_id,
        "feed_id": feed_id,
        "observed_at": now,
        "rss_payload": json.dumps(rss_payload),
    })

    return {
        "article_id": article_id,
        "content_id": content_id,
        "canonical_url": canonical_url,
        "latest_title": title,
        "latest_description": description,
        "published_at": published_at,
        "first_seen_at": now,
        "last_seen_at": now,
    }


# ---------------------------------------------------------------------------
# Article queries
# ---------------------------------------------------------------------------

def get_article_by_content_id(session, content_id: str) -> dict | None:
    """Get article record by content_id."""
    sql = text("SELECT * FROM ft_articles WHERE content_id = :content_id")
    result = session.execute(sql, {"content_id": content_id})
    row = result.fetchone()
    return dict(row._mapping) if row else None


# ---------------------------------------------------------------------------
# Capture management
# ---------------------------------------------------------------------------

def get_reusable_capture(session, article_id: str, requested_url: str) -> dict | None:
    """Get an existing valid capture for reuse.

    Returns the most recent captured/valid record for the same article + URL.
    """
    sql = text("""
        SELECT * FROM ft_archive_captures
        WHERE article_id = :article_id
          AND requested_url = :requested_url
          AND status = 'captured'
        ORDER BY completed_at DESC
        LIMIT 1
    """)
    result = session.execute(sql, {
        "article_id": article_id,
        "requested_url": requested_url,
    })
    row = result.fetchone()
    return dict(row._mapping) if row else None


def create_capture_attempt(
    session,
    article_id: str,
    requested_url: str,
    status: str = "queued",
) -> dict:
    """Create a new capture attempt record."""
    capture_id = str(uuid4())
    sql = text("""
        INSERT INTO ft_archive_captures
            (capture_id, article_id, requested_url, status, attempt_count)
        VALUES
            (:capture_id, :article_id, :requested_url, :status, 1)
    """)
    session.execute(sql, {
        "capture_id": capture_id,
        "article_id": article_id,
        "requested_url": requested_url,
        "status": status,
    })
    return {
        "capture_id": capture_id,
        "article_id": article_id,
        "requested_url": requested_url,
        "status": status,
        "attempt_count": 1,
    }


def mark_capture_status(
    session,
    capture_id: str,
    status: str,
    archive_url: str | None = None,
    error_message: str | None = None,
    raw_capture_path: str | None = None,
    raw_content_hash: str | None = None,
) -> None:
    """Update capture status and metadata."""
    now = datetime.now(timezone.utc)
    completed_at = now if status in ("captured", "failed", "invalid", "manual_review") else None

    sql = text("""
        UPDATE ft_archive_captures SET
            status = :status,
            archive_url = COALESCE(:archive_url, archive_url),
            error_message = COALESCE(:error_message, error_message),
            raw_capture_path = COALESCE(:raw_capture_path, raw_capture_path),
            raw_content_hash = COALESCE(:raw_content_hash, raw_content_hash),
            completed_at = COALESCE(:completed_at, completed_at),
            attempt_count = attempt_count + 1
        WHERE capture_id = :capture_id
    """)
    session.execute(sql, {
        "capture_id": capture_id,
        "status": status,
        "archive_url": archive_url,
        "error_message": error_message,
        "raw_capture_path": raw_capture_path,
        "raw_content_hash": raw_content_hash,
        "completed_at": completed_at,
    })


# ---------------------------------------------------------------------------
# Article versions
# ---------------------------------------------------------------------------

def insert_article_version_if_new(
    session,
    article_id: str,
    capture_id: str,
    archive_url: str,
    captured_at: datetime,
    content_hash: str,
    title: str | None,
    byline: str | None,
    published_at: datetime | None,
    body_text: str,
    word_count: int,
    raw_capture_path: str | None,
    extraction_status: str,
    parser_version: str,
) -> dict | None:
    """Insert a new version only if content_hash is new for this article.

    Returns the version dict if inserted, None if duplicate.
    """
    version_id = str(uuid4())
    sql = text("""
        INSERT INTO ft_article_versions
            (version_id, article_id, capture_id, archive_url, captured_at,
             content_hash, title, byline, published_at, body_text, word_count,
             raw_capture_path, extraction_status, parser_version)
        VALUES
            (:version_id, :article_id, :capture_id, :archive_url, :captured_at,
             :content_hash, :title, :byline, :published_at, :body_text, :word_count,
             :raw_capture_path, :extraction_status, :parser_version)
        ON CONFLICT (article_id, content_hash) DO NOTHING
    """)
    result = session.execute(sql, {
        "version_id": version_id,
        "article_id": article_id,
        "capture_id": capture_id,
        "archive_url": archive_url,
        "captured_at": captured_at,
        "content_hash": content_hash,
        "title": title,
        "byline": byline,
        "published_at": published_at,
        "body_text": body_text,
        "word_count": word_count,
        "raw_capture_path": raw_capture_path,
        "extraction_status": extraction_status,
        "parser_version": parser_version,
    })

    # rowcount == 0 means conflict hit — nothing inserted
    if result.rowcount == 0:
        return None

    return {
        "version_id": version_id,
        "article_id": article_id,
        "capture_id": capture_id,
        "archive_url": archive_url,
        "captured_at": captured_at,
        "content_hash": content_hash,
        "title": title,
        "byline": byline,
        "published_at": published_at,
        "body_text": body_text,
        "word_count": word_count,
        "extraction_status": extraction_status,
        "parser_version": parser_version,
    }


# ---------------------------------------------------------------------------
# Pending captures query
# ---------------------------------------------------------------------------

def get_pending_captures(session, article_id: str | None = None) -> list[dict]:
    """Get captures with status in (queued, submitted, pending)."""
    params: dict[str, Any] = {"statuses": ["queued", "submitted", "pending"]}
    if article_id is not None:
        sql = text("""
            SELECT * FROM ft_archive_captures
            WHERE article_id = :article_id
              AND status = ANY(:statuses)
            ORDER BY created_at
        """)
        params["article_id"] = article_id
    else:
        sql = text("""
            SELECT * FROM ft_archive_captures
            WHERE status = ANY(:statuses)
            ORDER BY created_at
        """)
    result = session.execute(sql, params)
    return [dict(row._mapping) for row in result.fetchall()]


# ---------------------------------------------------------------------------
# Collection runs
# ---------------------------------------------------------------------------

def insert_ft_run(
    session,
    run_id: str,
    correlation_id: str | None,
    sections_requested: list | None,
    since_requested: datetime | None,
    until_requested: datetime | None,
) -> dict:
    """Create an ft_collection_runs record."""
    now = datetime.now(timezone.utc)
    sql = text("""
        INSERT INTO ft_collection_runs
            (run_id, correlation_id, status, sections_requested,
             since_requested, until_requested, started_at)
        VALUES
            (:run_id, :correlation_id, 'running', :sections_requested,
             :since_requested, :until_requested, :started_at)
    """)
    session.execute(sql, {
        "run_id": run_id,
        "correlation_id": correlation_id,
        "sections_requested": json.dumps(sections_requested) if sections_requested else None,
        "since_requested": since_requested,
        "until_requested": until_requested,
        "started_at": now,
    })
    return {
        "run_id": run_id,
        "correlation_id": correlation_id,
        "status": "running",
        "started_at": now,
    }


def update_ft_run(
    session,
    run_id: str,
    status: str,
    articles_discovered: int | None = None,
    articles_captured: int | None = None,
    articles_failed: int | None = None,
    error_message: str | None = None,
) -> None:
    """Update ft_collection_runs record."""
    now = datetime.now(timezone.utc)
    completed_at = now if status in ("completed", "failed") else None

    sql = text("""
        UPDATE ft_collection_runs SET
            status = :status,
            articles_discovered = COALESCE(:articles_discovered, articles_discovered),
            articles_captured = COALESCE(:articles_captured, articles_captured),
            articles_failed = COALESCE(:articles_failed, articles_failed),
            error_message = COALESCE(:error_message, error_message),
            completed_at = COALESCE(:completed_at, completed_at)
        WHERE run_id = :run_id
    """)
    session.execute(sql, {
        "run_id": run_id,
        "status": status,
        "articles_discovered": articles_discovered,
        "articles_captured": articles_captured,
        "articles_failed": articles_failed,
        "error_message": error_message,
        "completed_at": completed_at,
    })


def get_latest_ft_run(session) -> dict | None:
    """Get the most recent completed FT run for resumability."""
    sql = text("""
        SELECT * FROM ft_collection_runs
        WHERE status = 'completed'
        ORDER BY completed_at DESC
        LIMIT 1
    """)
    result = session.execute(sql)
    row = result.fetchone()
    return dict(row._mapping) if row else None


def get_ft_run(session, run_id: str) -> dict | None:
    """Get a specific FT collection run by ID."""
    sql = text("SELECT * FROM ft_collection_runs WHERE run_id = :run_id")
    result = session.execute(sql, {"run_id": run_id})
    row = result.fetchone()
    return dict(row._mapping) if row else None


def get_recent_ft_runs(session, limit: int = 10) -> list[dict]:
    """Get recent FT collection runs."""
    sql = text("SELECT * FROM ft_collection_runs ORDER BY started_at DESC LIMIT :limit")
    result = session.execute(sql, {"limit": limit})
    return [dict(row._mapping) for row in result.fetchall()]
