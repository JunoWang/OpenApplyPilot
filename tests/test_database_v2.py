import json
import stat

from applypilot import database


def _insert_job(conn, url: str = "https://example.test/job") -> None:
    conn.execute(
        """
        INSERT INTO jobs (url, title, site, full_description, discovered_at)
        VALUES (?, 'Engineer', 'Example', 'Original job description', '2026-09-03T00:00:00Z')
        """,
        (url,),
    )
    conn.commit()


def test_v2_schema_and_jd_snapshot_history(tmp_path) -> None:
    db_path = tmp_path / "openapplypilot.db"
    conn = database.init_db(db_path)
    _insert_job(conn)

    table_names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert database.SCHEMA_VERSION == conn.execute("PRAGMA user_version").fetchone()[0]
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert {
        "jobs",
        "jd_snapshots",
        "pipeline_runs",
        "stage_events",
        "applications",
        "system_metadata",
        "schema_migrations",
    }.issubset(table_names)
    job_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    assert {
        "tailored_docx_path",
        "tailored_pdf_path",
        "tailor_report_path",
    }.issubset(job_columns)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    first_id = database.save_jd_snapshot(
        conn,
        "https://example.test/job",
        "Original job description",
    )
    duplicate_id = database.save_jd_snapshot(
        conn,
        "https://example.test/job",
        "Original job description",
    )
    second_id = database.save_jd_snapshot(
        conn,
        "https://example.test/job",
        "Updated job description",
    )
    conn.commit()

    assert first_id == duplicate_id
    assert second_id != first_id
    snapshots = conn.execute(
        "SELECT id, is_current FROM jd_snapshots ORDER BY id"
    ).fetchall()
    assert [(row[0], row[1]) for row in snapshots] == [(first_id, 0), (second_id, 1)]
    history = database.get_jd_history("https://example.test/job", conn=conn)
    assert history[0]["content"] == "Updated job description"
    assert history[0]["is_current"] == 1


def test_pipeline_run_events_and_application_stats(tmp_path) -> None:
    conn = database.init_db(tmp_path / "openapplypilot.db")
    _insert_job(conn)
    snapshot_id = database.save_jd_snapshot(
        conn,
        "https://example.test/job",
        "Original job description",
    )
    conn.commit()

    run_id = database.create_pipeline_run(
        ["score", "tailor"],
        mode="sequential",
        config={"min_score": 7},
        log_path="/tmp/openapplypilot.log",
        conn=conn,
    )
    database.record_stage_event(
        run_id,
        "score",
        "ok",
        metadata={"elapsed": 1.2},
        conn=conn,
    )
    database.finish_pipeline_run(run_id, "completed", conn=conn)

    application_id = database.create_application_record(
        "https://example.test/job",
        form_answers={"authorized": True},
        conn=conn,
    )
    database.update_application_record(
        application_id,
        "materials_approved",
        conn=conn,
    )

    run = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
    event = conn.execute("SELECT * FROM stage_events WHERE run_id = ?", (run_id,)).fetchone()
    stats = database.get_stats(conn)
    assert run["status"] == "completed"
    assert run["finished_at"] is not None
    assert event["stage"] == "score"
    assert json.loads(event["metadata_json"])["elapsed"] == 1.2
    assert stats["jd_snapshots"] == 1
    assert stats["pipeline_runs"] == 1
    assert stats["applications"] == 1
    applications = database.list_application_records(conn=conn)
    assert applications[0]["id"] == application_id
    assert applications[0]["jd_snapshot_id"] == snapshot_id
    assert applications[0]["status"] == "materials_approved"
    assert applications[0]["material_approved_at"] is not None
    assert applications[0]["form_answers"] == {"authorized": True}
