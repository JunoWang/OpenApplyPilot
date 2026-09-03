import pytest

from applypilot import database, pipeline
from applypilot.scoring import scorer


def test_score_stage_surfaces_per_job_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scorer,
        "run_scoring",
        lambda: {"scored": 2, "errors": 1, "elapsed": 0.1, "distribution": []},
    )

    result = pipeline._run_score()

    assert result["status"] == "error: 1 scoring request(s) failed"
    assert result["scored"] == 2


def test_dry_run_is_recorded_in_local_database(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = database.init_db(tmp_path / "openapplypilot.db")
    monkeypatch.setattr(pipeline, "load_env", lambda: None)
    monkeypatch.setattr(pipeline, "ensure_dirs", lambda: None)
    monkeypatch.setattr(pipeline, "configure_file_logging", lambda: None)
    monkeypatch.setattr(pipeline, "init_db", lambda: conn)
    monkeypatch.setattr(pipeline, "get_stats", lambda: database.get_stats(conn))
    monkeypatch.setattr(
        pipeline,
        "create_pipeline_run",
        lambda stages, **kwargs: database.create_pipeline_run(stages, conn=conn, **kwargs),
    )
    monkeypatch.setattr(
        pipeline,
        "record_stage_event",
        lambda run_id, stage, status, **kwargs: database.record_stage_event(
            run_id,
            stage,
            status,
            conn=conn,
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "finish_pipeline_run",
        lambda run_id, status, **kwargs: database.finish_pipeline_run(
            run_id,
            status,
            conn=conn,
            **kwargs,
        ),
    )

    result = pipeline.run_pipeline(["score"], dry_run=True)

    run = conn.execute(
        "SELECT status, requested_stages FROM pipeline_runs WHERE id = ?",
        (result["run_id"],),
    ).fetchone()
    event = conn.execute(
        "SELECT stage, status FROM stage_events WHERE run_id = ?",
        (result["run_id"],),
    ).fetchone()
    assert run["status"] == "completed"
    assert run["requested_stages"] == '["score"]'
    assert (event["stage"], event["status"]) == ("score", "dry_run")
