"""Safe migration from the upstream ApplyPilot data directory.

The migration is intentionally one-way and non-destructive: it creates a
consistent SQLite archive, imports only discovery/enrichment data, and leaves
the original directory untouched until the user chooses to remove it.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import APP_DIR, LEGACY_APP_DIR
from applypilot.database import SCHEMA_VERSION, init_db, save_jd_snapshot


@dataclass
class MigrationReport:
    source_db: str
    target_db: str
    archive_db: str | None
    source_jobs: int
    imported_jobs: int
    skipped_jobs: int
    jd_snapshots: int
    copied_files: list[str]
    dry_run: bool

    def to_dict(self) -> dict:
        return asdict(self)


_PORTABLE_FILES = ("profile.json", "resume.txt", "resume.pdf", "searches.yaml", ".env")
_JOB_FIELDS = (
    "url",
    "title",
    "company",
    "salary",
    "description",
    "location",
    "site",
    "strategy",
    "discovered_at",
    "full_description",
    "application_url",
    "detail_scraped_at",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_legacy_db(path: Path) -> sqlite3.Connection:
    # immutable=1 prevents SQLite from trying to create -wal/-shm files in the
    # legacy directory, which also keeps preview and migration strictly read-only.
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _database_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for statement in connection.iterdump():
        digest.update(statement.encode("utf-8"))
    return digest.hexdigest()[:12]


def _archive_database(source: sqlite3.Connection, archive_dir: Path) -> Path:
    """Create a transactionally consistent, content-addressed archive."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.chmod(0o700)
    archive_path = archive_dir / f"legacy-applypilot-{_database_digest(source)}.db"
    if not archive_path.exists():
        destination = sqlite3.connect(archive_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
        archive_path.chmod(0o600)
    return archive_path


def _source_jobs(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "url" not in columns:
        raise RuntimeError("Legacy jobs table does not contain the required url column.")
    projections = [
        field if field in columns else f"NULL AS {field}"
        for field in _JOB_FIELDS
    ]
    return connection.execute(
        f"SELECT {', '.join(projections)} FROM jobs ORDER BY discovered_at"
    ).fetchall()


def _prepare_target_dirs(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(0o700)
    for name in (
        "archives",
        "logs",
        "tailored_resumes",
        "cover_letters",
        "chrome-workers",
        "apply-workers",
    ):
        directory = target_dir / name
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)


def _copy_portable_files(source_dir: Path, target_dir: Path) -> list[str]:
    copied: list[str] = []
    for name in _PORTABLE_FILES:
        source = source_dir / name
        target = target_dir / name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
            target.chmod(0o600)
            copied.append(name)
        elif target.is_file() and name in {".env", "profile.json", "resume.txt", "resume.pdf"}:
            target.chmod(0o600)
    return copied


def migrate_legacy_data(
    *,
    legacy_dir: Path = LEGACY_APP_DIR,
    target_dir: Path = APP_DIR,
    dry_run: bool = False,
) -> MigrationReport:
    """Archive and import legacy jobs/JDs while resetting broken AI stages."""
    legacy_dir = Path(legacy_dir).expanduser().resolve()
    target_dir = Path(target_dir).expanduser().resolve()
    source_db = legacy_dir / "applypilot.db"
    target_db = target_dir / "openapplypilot.db"

    if not source_db.is_file():
        raise FileNotFoundError(f"Legacy database not found: {source_db}")
    if legacy_dir == target_dir:
        raise ValueError("Legacy and target data directories must be different.")

    source = _open_legacy_db(source_db)
    try:
        rows = _source_jobs(source)
        jd_count = sum(bool((row["full_description"] or "").strip()) for row in rows)
        if dry_run:
            return MigrationReport(
                source_db=str(source_db),
                target_db=str(target_db),
                archive_db=None,
                source_jobs=len(rows),
                imported_jobs=0,
                skipped_jobs=0,
                jd_snapshots=jd_count,
                copied_files=[],
                dry_run=True,
            )

        _prepare_target_dirs(target_dir)
        archive_path = _archive_database(source, target_dir / "archives")
        target = init_db(target_db)
        imported = 0
        skipped = 0
        now = _utc_now()

        try:
            for row in rows:
                description = (row["full_description"] or "").strip() or None
                discovered_at = row["discovered_at"] or now
                cursor = target.execute(
                    """
                    INSERT OR IGNORE INTO jobs (
                        url, title, company, salary, description, location, site, strategy,
                        discovered_at, full_description, application_url,
                        detail_scraped_at, detail_error, enrichment_status,
                        fit_score, score_reasoning, scored_at, score_status, score_error,
                        tailored_resume_path, tailored_at, tailor_attempts,
                        tailor_status, tailor_error,
                        cover_letter_path, cover_letter_at, cover_attempts,
                        cover_status, cover_error,
                        applied_at, apply_status, apply_error, apply_attempts,
                        record_status, last_seen_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?,
                        NULL, NULL, NULL, 'pending', NULL,
                        NULL, NULL, 0, 'waiting', NULL,
                        NULL, NULL, 0, 'waiting', NULL,
                        NULL, NULL, NULL, 0, 'active', ?, ?
                    )
                    """,
                    (
                        row["url"],
                        row["title"],
                        row["company"],
                        row["salary"],
                        row["description"],
                        row["location"],
                        row["site"],
                        row["strategy"],
                        discovered_at,
                        description,
                        row["application_url"],
                        row["detail_scraped_at"],
                        "complete" if description else "pending",
                        discovered_at,
                        now,
                    ),
                )
                if cursor.rowcount == 1:
                    imported += 1
                else:
                    skipped += 1

                save_jd_snapshot(
                    target,
                    row["url"],
                    description,
                    source_url=row["application_url"] or row["url"],
                    captured_at=row["detail_scraped_at"] or discovered_at,
                )
            metadata = {
                "legacy_migration_source": str(source_db),
                "legacy_migration_archive": str(archive_path),
                "legacy_migration_completed_at": now,
                "schema_version": str(SCHEMA_VERSION),
            }
            target.executemany(
                """
                INSERT INTO system_metadata (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
                """,
                [(key, value, now) for key, value in metadata.items()],
            )
            target.commit()
        except Exception:
            target.rollback()
            raise

        snapshots = target.execute("SELECT COUNT(*) FROM jd_snapshots").fetchone()[0]
        target_db.chmod(0o600)
        copied_files = _copy_portable_files(legacy_dir, target_dir)
        return MigrationReport(
            source_db=str(source_db),
            target_db=str(target_db),
            archive_db=str(archive_path),
            source_jobs=len(rows),
            imported_jobs=imported,
            skipped_jobs=skipped,
            jd_snapshots=snapshots,
            copied_files=copied_files,
            dry_run=False,
        )
    finally:
        source.close()
