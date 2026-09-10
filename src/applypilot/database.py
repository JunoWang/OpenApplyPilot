"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import DB_PATH

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()

SCHEMA_VERSION = 4


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, "connections"):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, "connections"):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            company               TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,
            enrichment_status     TEXT DEFAULT 'pending',

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,
            score_status          TEXT DEFAULT 'pending',
            score_error           TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_docx_path    TEXT,
            tailored_pdf_path     TEXT,
            tailor_report_path    TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,
            tailor_status         TEXT DEFAULT 'waiting',
            tailor_error          TEXT,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,
            cover_status          TEXT DEFAULT 'waiting',
            cover_error           TEXT,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT,

            -- Record lifecycle
            record_status         TEXT NOT NULL DEFAULT 'active',
            last_seen_at           TEXT,
            updated_at             TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    _create_v2_tables(conn)
    _ensure_application_columns(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, _utc_now()),
    )
    conn.commit()
    if str(path) != ":memory:":
        Path(path).chmod(0o600)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "company": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    "enrichment_status": "TEXT DEFAULT 'pending'",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    "score_status": "TEXT DEFAULT 'pending'",
    "score_error": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_docx_path": "TEXT",
    "tailored_pdf_path": "TEXT",
    "tailor_report_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    "tailor_status": "TEXT DEFAULT 'waiting'",
    "tailor_error": "TEXT",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    "cover_status": "TEXT DEFAULT 'waiting'",
    "cover_error": "TEXT",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
    # Record lifecycle
    "record_status": "TEXT NOT NULL DEFAULT 'active'",
    "last_seen_at": "TEXT",
    "updated_at": "TEXT",
}


def _create_v2_tables(conn: sqlite3.Connection) -> None:
    """Create local-first history and observability tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version      INTEGER PRIMARY KEY,
            applied_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_metadata (
            key          TEXT PRIMARY KEY,
            value        TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jd_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url      TEXT NOT NULL,
            source_url   TEXT,
            content      TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            captured_at  TEXT NOT NULL,
            is_current   INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (job_url) REFERENCES jobs(url)
                ON UPDATE CASCADE ON DELETE CASCADE,
            UNIQUE (job_url, content_hash)
        );

        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id               TEXT PRIMARY KEY,
            mode             TEXT NOT NULL,
            requested_stages TEXT NOT NULL,
            status           TEXT NOT NULL,
            config_json      TEXT,
            log_path         TEXT,
            started_at       TEXT NOT NULL,
            finished_at      TEXT,
            error_summary    TEXT
        );

        CREATE TABLE IF NOT EXISTS stage_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        TEXT,
            job_url       TEXT,
            stage         TEXT NOT NULL,
            status        TEXT NOT NULL,
            attempt       INTEGER,
            provider      TEXT,
            model         TEXT,
            message       TEXT,
            error_code    TEXT,
            metadata_json TEXT,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (job_url) REFERENCES jobs(url)
                ON UPDATE CASCADE ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS applications (
            id                    TEXT PRIMARY KEY,
            job_url               TEXT NOT NULL,
            jd_snapshot_id        INTEGER,
            company               TEXT,
            job_title             TEXT,
            application_url       TEXT,
            status                TEXT NOT NULL DEFAULT 'draft',
            resume_path           TEXT,
            cover_letter_path     TEXT,
            form_answers_json     TEXT,
            review_snapshot_path  TEXT,
            material_approved_at  TEXT,
            final_approved_at     TEXT,
            submitted_at          TEXT,
            workflow_thread_id    TEXT,
            agent_log_path        TEXT,
            submission_snapshot_path TEXT,
            last_error            TEXT,
            verification_confidence TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            FOREIGN KEY (job_url) REFERENCES jobs(url)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY (jd_snapshot_id) REFERENCES jd_snapshots(id)
                ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jd_snapshots_job_current
            ON jd_snapshots(job_url, is_current, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started
            ON pipeline_runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_stage_events_run_stage
            ON stage_events(run_id, stage, created_at);
        CREATE INDEX IF NOT EXISTS idx_stage_events_job
            ON stage_events(job_url, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_applications_job
            ON applications(job_url, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_applications_status
            ON applications(status, updated_at DESC);
    """)


_APPLICATION_COLUMNS: dict[str, str] = {
    "workflow_thread_id": "TEXT",
    "agent_log_path": "TEXT",
    "submission_snapshot_path": "TEXT",
    "last_error": "TEXT",
    "verification_confidence": "TEXT",
}


def _ensure_application_columns(conn: sqlite3.Connection) -> list[str]:
    """Add Stage 6A application evidence/checkpoint columns in place."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(applications)").fetchall()}
    added: list[str] = []
    for column, dtype in _APPLICATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {column} {dtype}")
            added.append(column)
    if added:
        conn.commit()
    return added


def set_metadata(key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
    """Persist application metadata such as migration provenance."""
    if conn is None:
        conn = get_connection()
    conn.execute(
        """
        INSERT INTO system_metadata (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, _utc_now()),
    )
    conn.commit()


def save_jd_snapshot(
    conn: sqlite3.Connection,
    job_url: str,
    content: str | None,
    *,
    source_url: str | None = None,
    captured_at: str | None = None,
) -> int | None:
    """Store a deduplicated JD snapshot and mark it as the current version."""
    clean_content = (content or "").strip()
    if not clean_content:
        return None

    content_hash = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT id FROM jd_snapshots WHERE job_url = ? AND content_hash = ?",
        (job_url, content_hash),
    ).fetchone()
    if existing:
        snapshot_id = int(existing[0])
    else:
        conn.execute(
            "UPDATE jd_snapshots SET is_current = 0 WHERE job_url = ?",
            (job_url,),
        )
        cursor = conn.execute(
            """
            INSERT INTO jd_snapshots (
                job_url, source_url, content, content_hash, captured_at, is_current
            ) VALUES (?, ?, ?, ?, ?, 1)
            """,
            (job_url, source_url or job_url, clean_content, content_hash, captured_at or _utc_now()),
        )
        snapshot_id = int(cursor.lastrowid)

    conn.execute(
        "UPDATE jd_snapshots SET is_current = (id = ?) WHERE job_url = ?",
        (snapshot_id, job_url),
    )
    return snapshot_id


def create_pipeline_run(
    stages: list[str],
    *,
    mode: str,
    config: dict | None = None,
    log_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Create a durable record before pipeline execution starts."""
    if conn is None:
        conn = get_connection()
    run_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO pipeline_runs (
            id, mode, requested_stages, status, config_json, log_path, started_at
        ) VALUES (?, ?, ?, 'running', ?, ?, ?)
        """,
        (
            run_id,
            mode,
            json.dumps(stages),
            json.dumps(config or {}, sort_keys=True),
            log_path,
            _utc_now(),
        ),
    )
    conn.commit()
    return run_id


def finish_pipeline_run(
    run_id: str,
    status: str,
    *,
    error_summary: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Mark a pipeline run complete, partial, failed, or interrupted."""
    if conn is None:
        conn = get_connection()
    conn.execute(
        """
        UPDATE pipeline_runs
        SET status = ?, finished_at = ?, error_summary = ?
        WHERE id = ?
        """,
        (status, _utc_now(), error_summary, run_id),
    )
    conn.commit()


def record_stage_event(
    run_id: str | None,
    stage: str,
    status: str,
    *,
    job_url: str | None = None,
    attempt: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    message: str | None = None,
    error_code: str | None = None,
    metadata: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Append an immutable pipeline or per-job stage event."""
    if conn is None:
        conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO stage_events (
            run_id, job_url, stage, status, attempt, provider, model,
            message, error_code, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            job_url,
            stage,
            status,
            attempt,
            provider,
            model,
            message,
            error_code,
            json.dumps(metadata or {}, sort_keys=True),
            _utc_now(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def get_jd_history(
    job_url: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Return all saved JD versions for interview preparation."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, job_url, source_url, content, content_hash, captured_at, is_current
        FROM jd_snapshots WHERE job_url = ?
        ORDER BY captured_at DESC, id DESC
        """,
        (job_url,),
    ).fetchall()
    return [dict(row) for row in rows]


_APPLICATION_STATUSES = {
    "draft",
    "materials_approved",
    "ready_for_review",
    "approved",
    "submitted",
    "failed",
    "withdrawn",
}


def create_application_record(
    job_url: str,
    *,
    status: str = "draft",
    form_answers: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Create a form-like application history record from the current job state."""
    if status not in _APPLICATION_STATUSES:
        raise ValueError(f"Unsupported application status: {status}")
    if conn is None:
        conn = get_connection()
    job = conn.execute("SELECT * FROM jobs WHERE url = ?", (job_url,)).fetchone()
    if job is None:
        raise KeyError(f"Job not found: {job_url}")
    snapshot = conn.execute(
        """
        SELECT id FROM jd_snapshots
        WHERE job_url = ? AND is_current = 1
        ORDER BY captured_at DESC, id DESC LIMIT 1
        """,
        (job_url,),
    ).fetchone()
    application_id = str(uuid.uuid4())
    now = _utc_now()
    resume_path = job["tailored_pdf_path"] or job["tailored_resume_path"]
    conn.execute(
        """
        INSERT INTO applications (
            id, job_url, jd_snapshot_id, company, job_title, application_url,
            status, resume_path, cover_letter_path, form_answers_json,
            workflow_thread_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            application_id,
            job_url,
            snapshot[0] if snapshot else None,
            job["company"] or job["site"],
            job["title"],
            job["application_url"] or job_url,
            status,
            resume_path,
            job["cover_letter_path"],
            json.dumps(form_answers or {}, sort_keys=True),
            application_id,
            now,
            now,
        ),
    )
    conn.commit()
    return application_id


def update_application_record(
    application_id: str,
    status: str | None,
    *,
    form_answers: dict | None = None,
    review_snapshot_path: str | None = None,
    submission_snapshot_path: str | None = None,
    agent_log_path: str | None = None,
    last_error: str | None = None,
    verification_confidence: str | None = None,
    workflow_thread_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Update application status while preserving review/approval timestamps."""
    if status is not None and status not in _APPLICATION_STATUSES:
        raise ValueError(f"Unsupported application status: {status}")
    if conn is None:
        conn = get_connection()
    now = _utc_now()
    timestamp_column = {
        "materials_approved": "material_approved_at",
        "approved": "final_approved_at",
        "submitted": "submitted_at",
    }.get(status)
    assignments = ["updated_at = ?"]
    params: list = [now]
    if status is not None:
        assignments.append("status = ?")
        params.append(status)
    if form_answers is not None:
        assignments.append("form_answers_json = ?")
        params.append(json.dumps(form_answers, sort_keys=True))
    optional_values = {
        "review_snapshot_path": review_snapshot_path,
        "submission_snapshot_path": submission_snapshot_path,
        "agent_log_path": agent_log_path,
        "last_error": last_error,
        "verification_confidence": verification_confidence,
        "workflow_thread_id": workflow_thread_id,
    }
    for column, value in optional_values.items():
        if value is not None:
            assignments.append(f"{column} = ?")
            params.append(value)
    if timestamp_column:
        assignments.append(f"{timestamp_column} = ?")
        params.append(now)
    params.append(application_id)
    cursor = conn.execute(
        f"UPDATE applications SET {', '.join(assignments)} WHERE id = ?",
        params,
    )
    if cursor.rowcount != 1:
        raise KeyError(f"Application not found: {application_id}")
    conn.commit()


def get_application_record(
    application_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Return one application record with decoded form answers."""
    if conn is None:
        conn = get_connection()
    row = conn.execute(
        "SELECT * FROM applications WHERE id = ?",
        (application_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Application not found: {application_id}")
    item = dict(row)
    item["form_answers"] = json.loads(item.pop("form_answers_json") or "{}")
    return item


def list_application_records(
    *,
    limit: int = 100,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Return recent application records for the dashboard."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM applications ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result: list[dict] = []
    for row in rows:
        item = dict(row)
        item["form_answers"] = json.loads(item.pop("form_answers_json") or "{}")
        result.append(item)
    return result


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute("SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC").fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL").fetchone()[0]

    stats["with_description"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL").fetchone()[
        0
    ]

    stats["detail_errors"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL").fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL").fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL").fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(tailor_attempts, 0) >= 5 AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL").fetchone()[0]

    stats["apply_errors"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL").fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL"
    ).fetchone()[0]

    # Local history and observability
    stats["jd_snapshots"] = conn.execute("SELECT COUNT(*) FROM jd_snapshots").fetchone()[0]
    stats["pipeline_runs"] = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
    stats["applications"] = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]

    return stats


def store_jobs(conn: sqlite3.Connection, jobs: list[dict], site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, company, salary, description, location, site, strategy, "
                "discovered_at, last_seen_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    job.get("title"),
                    job.get("company"),
                    job.get("salary"),
                    job.get("description"),
                    job.get("location"),
                    site,
                    strategy,
                    now,
                    now,
                    now,
                ),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1
            conn.execute(
                "UPDATE jobs SET last_seen_at = ?, updated_at = ? WHERE url = ?",
                (now, now, url),
            )

    conn.commit()
    return new, existing


def get_jobs_by_stage(
    conn: sqlite3.Connection | None = None,
    stage: str = "discovered",
    min_score: int | None = None,
    limit: int = 100,
    job_url: str | None = None,
) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.
        job_url: When provided, restrict the result to exactly this job.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL",
        "enriched": "full_description IS NOT NULL",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": ("tailored_resume_path IS NOT NULL AND applied_at IS NULL AND application_url IS NOT NULL"),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    if job_url:
        where += " AND url = ?"
        params.append(job_url)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []
