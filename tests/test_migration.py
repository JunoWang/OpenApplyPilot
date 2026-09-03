import sqlite3
import stat

from applypilot.migration import migrate_legacy_data


def _legacy_install(tmp_path):
    legacy_dir = tmp_path / ".applypilot"
    legacy_dir.mkdir()
    db_path = legacy_dir / "applypilot.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY,
            title TEXT,
            salary TEXT,
            description TEXT,
            location TEXT,
            site TEXT,
            strategy TEXT,
            discovered_at TEXT,
            full_description TEXT,
            application_url TEXT,
            detail_scraped_at TEXT,
            fit_score INTEGER,
            score_reasoning TEXT,
            scored_at TEXT,
            tailored_resume_path TEXT,
            applied_at TEXT,
            apply_status TEXT
        )
    """)
    conn.executemany(
        """
        INSERT INTO jobs VALUES (
            ?, ?, NULL, 'short description', 'Remote', 'Example', 'legacy',
            '2026-09-01T00:00:00Z', ?, ?, '2026-09-01T01:00:00Z',
            0, 'LLM error', '2026-09-01T02:00:00Z', ?, ?, ?
        )
        """,
        [
            (
                "https://example.test/1",
                "First role",
                "First complete JD",
                "https://apply.example.test/1",
                "/tmp/broken-resume.txt",
                "2026-09-01T03:00:00Z",
                "applied",
            ),
            (
                "https://example.test/2",
                "Second role",
                "Second complete JD",
                "https://apply.example.test/2",
                None,
                None,
                None,
            ),
        ],
    )
    conn.commit()
    conn.close()

    (legacy_dir / "profile.json").write_text('{"name": "Candidate"}', encoding="utf-8")
    (legacy_dir / "resume.txt").write_text("Resume", encoding="utf-8")
    (legacy_dir / ".env").write_text("OPENAI_API_KEY=test-only", encoding="utf-8")
    return legacy_dir


def test_migration_preview_does_not_write(tmp_path) -> None:
    legacy_dir = _legacy_install(tmp_path)
    target_dir = tmp_path / ".openapplypilot"

    report = migrate_legacy_data(
        legacy_dir=legacy_dir,
        target_dir=target_dir,
        dry_run=True,
    )

    assert report.source_jobs == 2
    assert report.jd_snapshots == 2
    assert report.imported_jobs == 0
    assert not target_dir.exists()


def test_migration_archives_imports_and_resets_ai_state(tmp_path) -> None:
    legacy_dir = _legacy_install(tmp_path)
    target_dir = tmp_path / ".openapplypilot"

    report = migrate_legacy_data(legacy_dir=legacy_dir, target_dir=target_dir)

    assert report.imported_jobs == 2
    assert report.skipped_jobs == 0
    assert report.jd_snapshots == 2
    assert set(report.copied_files) == {"profile.json", "resume.txt", ".env"}
    assert (legacy_dir / "applypilot.db").exists()
    assert report.archive_db is not None
    assert stat.S_IMODE((target_dir / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((target_dir / "openapplypilot.db").stat().st_mode) == 0o600

    conn = sqlite3.connect(target_dir / "openapplypilot.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM jobs ORDER BY url").fetchall()
    assert len(rows) == 2
    assert all(row["fit_score"] is None for row in rows)
    assert all(row["score_reasoning"] is None for row in rows)
    assert all(row["scored_at"] is None for row in rows)
    assert all(row["score_status"] == "pending" for row in rows)
    assert all(row["tailored_resume_path"] is None for row in rows)
    assert all(row["tailor_attempts"] == 0 for row in rows)
    assert all(row["applied_at"] is None for row in rows)
    assert all(row["apply_status"] is None for row in rows)
    assert conn.execute("SELECT COUNT(*) FROM jd_snapshots").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    conn.close()

    second = migrate_legacy_data(legacy_dir=legacy_dir, target_dir=target_dir)
    assert second.imported_jobs == 0
    assert second.skipped_jobs == 2
    assert second.jd_snapshots == 2
    assert second.archive_db == report.archive_db
