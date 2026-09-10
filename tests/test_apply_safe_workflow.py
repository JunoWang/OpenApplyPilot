import json
import sqlite3
from functools import partial
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from applypilot import database
from applypilot import view as dashboard_view
from applypilot.apply import launcher, prompt, workflow


def _insert_ready_job(conn, tmp_path: Path, *, url: str = "https://example.test/job") -> dict:
    resume_txt = tmp_path / "tailored.txt"
    resume_pdf = tmp_path / "tailored.pdf"
    resume_txt.write_text("Tailored resume", encoding="utf-8")
    resume_pdf.write_bytes(b"%PDF-1.4\n")
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, company, site, application_url, full_description, fit_score,
            tailored_resume_path, tailored_pdf_path, apply_status, discovered_at
        ) VALUES (?, 'Engineer', 'Example Co', 'LinkedIn', ?, 'Build systems', 8, ?, ?, NULL, ?)
        """,
        (url, url, str(resume_txt), str(resume_pdf), "2026-09-07T00:00:00Z"),
    )
    conn.commit()
    return {
        "url": url,
        "title": "Engineer",
        "company": "Example Co",
        "site": "LinkedIn",
        "application_url": url,
        "fit_score": 8,
        "tailored_resume_path": str(resume_txt),
        "cover_letter_path": None,
    }


def _profile() -> dict:
    return {
        "personal": {
            "full_name": "Test Candidate",
            "preferred_name": "Test",
            "email": "candidate@example.test",
            "phone": "+1 555 0100",
            "city": "Toronto",
            "country": "Canada",
        },
        "work_authorization": {
            "legally_authorized_to_work": "Yes",
            "require_sponsorship": "No",
        },
        "mobility": {"willing_to_relocate": True},
        "compensation": {
            "salary_expectation": "100000",
            "salary_range_min": "100000",
            "salary_range_max": "130000",
            "salary_currency": "CAD",
        },
        "experience": {"years_of_experience_total": "5", "target_role": "Engineer"},
        "availability": {},
        "eeo_voluntary": {},
    }


def test_target_url_acquires_job_with_null_apply_status(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    acquired = launcher.acquire_job(target_url=job["url"], worker_id=2)

    assert acquired is not None
    assert acquired["url"] == job["url"]
    stored = conn.execute("SELECT apply_status, agent_id FROM jobs WHERE url = ?", (job["url"],)).fetchone()
    assert tuple(stored) == ("in_progress", "worker-2")


def test_dry_run_prompt_stops_at_review_and_uses_distinct_result(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    review_path = tmp_path / "review.png"
    monkeypatch.setattr(prompt.config, "load_profile", _profile)
    monkeypatch.setattr(prompt.config, "load_search_config", dict)
    monkeypatch.setattr(prompt.config, "load_env", lambda: None)
    monkeypatch.setattr(prompt.config, "APPLICATION_REVIEW_DIR", tmp_path)
    monkeypatch.setattr(prompt.config, "APPLY_WORKER_DIR", tmp_path / "workers")

    value = prompt.build_prompt(
        job,
        "Tailored resume",
        dry_run=True,
        review_snapshot_path=str(review_path),
    )

    assert "RESULT:READY_FOR_REVIEW" in value
    assert "Never click Submit, Apply, Send" in value
    assert "Company: Example Co" in value
    assert str(review_path.resolve()) in value
    assert "Do NOT send an email" in value
    assert "Willing to Relocate: Yes" in value
    assert "willing to relocate: Yes" in value
    assert "Do not reject solely because the city differs" in value


def test_location_prompt_defaults_to_no_relocation_for_legacy_profiles() -> None:
    profile = _profile()
    profile.pop("mobility")

    value = prompt._build_location_check(profile, {})
    screening = prompt._build_screening_section(profile)

    assert '"Hybrid" or "onsite" in Toronto -> ELIGIBLE' in value
    assert "willing to relocate: No" in screening


def test_safe_graph_dry_run_requires_material_approval_and_never_submits() -> None:
    calls = {"prepare": 0, "submit": 0}

    def prepare(_state):
        calls["prepare"] += 1
        return {"agent_result": "ready_for_review", "duration_ms": 12}

    def submit(_state):
        calls["submit"] += 1
        return {"agent_result": "applied"}

    graph = workflow.build_safe_apply_graph(
        checkpointer=InMemorySaver(),
        prepare_form=prepare,
        submit_form=submit,
        browser_session_id="browser-a",
    )
    config = {"configurable": {"thread_id": "application-a"}}
    initial = {
        "application_id": "application-a",
        "job_url": "https://example.test/job",
        "allow_submission": False,
        "status": "draft",
    }

    paused = graph.invoke(initial, config=config)
    assert workflow.interrupt_payload(paused)["kind"] == "material_approval"
    completed = graph.invoke(Command(resume=True), config=config)

    assert completed["status"] == "ready_for_review"
    assert calls == {"prepare": 1, "submit": 0}


def test_form_answer_record_parser_ignores_invalid_payloads() -> None:
    output = """
FORM_ANSWERS_JSON:not-json
FORM_ANSWERS_JSON:{"Authorized": "Yes", "Salary": "120000 CAD"}
RESULT:READY_FOR_REVIEW
"""

    assert launcher._extract_form_answers(output) == {
        "Authorized": "Yes",
        "Salary": "120000 CAD",
    }


def test_claude_command_is_restricted_and_cannot_send_email(tmp_path) -> None:
    config_path = tmp_path / "mcp.json"

    prepare = launcher._build_claude_command(model="haiku", mcp_config_path=config_path, phase="prepare")
    submit = launcher._build_claude_command(model="haiku", mcp_config_path=config_path, phase="submit")

    assert "--restricted" in prepare
    assert "bypassPermissions" not in prepare
    assert "dontAsk" in prepare
    assert "mcp__gmail__send_email" in prepare
    prepare_allowed = prepare[prepare.index("--allowedTools") + 1]
    submit_allowed = submit[submit.index("--allowedTools") + 1]
    assert "mcp__playwright__*" in prepare_allowed
    assert "mcp__gmail__search_emails" in prepare_allowed
    assert submit_allowed == "mcp__playwright__*"


def test_submit_mcp_config_excludes_gmail() -> None:
    prepare = launcher._make_mcp_config(9222, include_gmail=True)
    submit = launcher._make_mcp_config(9222, include_gmail=False)

    assert set(prepare["mcpServers"]) == {"playwright", "gmail"}
    assert set(submit["mcpServers"]) == {"playwright"}


def test_safe_graph_requires_second_approval_before_submit() -> None:
    calls = {"prepare": 0, "submit": 0}

    def prepare(_state):
        calls["prepare"] += 1
        return {"agent_result": "ready_for_review"}

    def submit(_state):
        calls["submit"] += 1
        return {"agent_result": "applied", "verification_confidence": "confirmed"}

    graph = workflow.build_safe_apply_graph(
        checkpointer=InMemorySaver(),
        prepare_form=prepare,
        submit_form=submit,
        browser_session_id="browser-a",
    )
    config = {"configurable": {"thread_id": "application-b"}}
    initial = {
        "application_id": "application-b",
        "job_url": "https://example.test/job",
        "allow_submission": True,
        "status": "draft",
    }

    graph.invoke(initial, config=config)
    awaiting_submit = graph.invoke(Command(resume=True), config=config)
    assert workflow.interrupt_payload(awaiting_submit)["kind"] == "final_submission_approval"
    assert calls == {"prepare": 1, "submit": 0}

    completed = graph.invoke(Command(resume=True), config=config)
    assert completed["status"] == "submitted"
    assert calls == {"prepare": 1, "submit": 1}


def test_application_history_prefers_pdf_and_stores_evidence(tmp_path) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    application_id = database.create_application_record(job["url"], conn=conn)
    review = tmp_path / "review.png"
    review.write_bytes(b"image")

    database.update_application_record(
        application_id,
        "ready_for_review",
        form_answers={"authorized": True},
        review_snapshot_path=str(review),
        agent_log_path=str(tmp_path / "agent.log"),
        verification_confidence="unverified",
        conn=conn,
    )
    stored = database.get_application_record(application_id, conn=conn)

    assert stored["resume_path"].endswith("tailored.pdf")
    assert stored["workflow_thread_id"] == application_id
    assert stored["review_snapshot_path"] == str(review)
    assert stored["form_answers"] == {"authorized": True}
    assert json.loads(json.dumps(stored["form_answers"])) == {"authorized": True}


def test_safe_workflow_dry_run_persists_review_without_submit(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    application_id = database.create_application_record(job["url"], conn=conn)
    review_dir = tmp_path / "reviews"
    review_dir.mkdir()
    monkeypatch.setattr(launcher.config, "APPLICATION_REVIEW_DIR", review_dir)
    monkeypatch.setattr(launcher.config, "APPLY_CHECKPOINT_DB_PATH", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        launcher,
        "update_application_record",
        partial(database.update_application_record, conn=conn),
    )
    phases: list[str] = []

    def fake_run_job(*_args, application_id=None, phase="prepare", **_kwargs):
        phases.append(phase)
        if phase == "prepare":
            (review_dir / f"{application_id}.png").write_bytes(b"review")
            return "ready_for_review", 25
        raise AssertionError("dry run reached submit phase")

    monkeypatch.setattr(launcher, "run_job", fake_run_job)

    result, duration = launcher.run_safe_workflow(
        job,
        port=9222,
        worker_id=0,
        model="haiku",
        application_id=application_id,
        allow_submission=False,
        approval_callback=lambda _payload: True,
    )

    stored = database.get_application_record(application_id, conn=conn)
    assert (result, duration) == ("ready_for_review", 25)
    assert phases == ["prepare"]
    assert stored["status"] == "ready_for_review"
    assert stored["material_approved_at"] is not None
    assert stored["final_approved_at"] is None
    assert stored["submitted_at"] is None
    assert stored["review_snapshot_path"].endswith(f"{application_id}.png")


def test_safe_workflow_submit_requires_evidence_file(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    application_id = database.create_application_record(job["url"], conn=conn)
    review_dir = tmp_path / "reviews"
    review_dir.mkdir()
    monkeypatch.setattr(launcher.config, "APPLICATION_REVIEW_DIR", review_dir)
    monkeypatch.setattr(launcher.config, "APPLY_CHECKPOINT_DB_PATH", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        launcher,
        "update_application_record",
        partial(database.update_application_record, conn=conn),
    )

    def fake_run_job(*_args, application_id=None, phase="prepare", **_kwargs):
        if phase == "prepare":
            (review_dir / f"{application_id}.png").write_bytes(b"review")
            return "ready_for_review", 10
        # An APPLIED claim without its required screenshot must be rejected.
        return "applied", 20

    monkeypatch.setattr(launcher, "run_job", fake_run_job)
    decisions = iter([True, True])

    result, duration = launcher.run_safe_workflow(
        job,
        port=9222,
        worker_id=0,
        model="haiku",
        application_id=application_id,
        allow_submission=True,
        approval_callback=lambda _payload: next(decisions),
    )

    stored = database.get_application_record(application_id, conn=conn)
    assert (result, duration) == ("failed:missing_submission_evidence", 20)
    assert stored["status"] == "failed"
    assert stored["final_approved_at"] is not None
    assert stored["submitted_at"] is None
    assert stored["last_error"] == "failed:missing_submission_evidence"


def test_safe_workflow_never_requests_final_approval_without_review_evidence(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    application_id = database.create_application_record(job["url"], conn=conn)
    review_dir = tmp_path / "reviews"
    review_dir.mkdir()
    monkeypatch.setattr(launcher.config, "APPLICATION_REVIEW_DIR", review_dir)
    monkeypatch.setattr(launcher.config, "APPLY_CHECKPOINT_DB_PATH", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        launcher,
        "update_application_record",
        partial(database.update_application_record, conn=conn),
    )
    monkeypatch.setattr(
        launcher,
        "run_job",
        lambda *_args, **_kwargs: ("ready_for_review", 11),
    )
    decisions: list[str] = []

    result, duration = launcher.run_safe_workflow(
        job,
        port=9222,
        worker_id=0,
        model="haiku",
        application_id=application_id,
        allow_submission=True,
        approval_callback=lambda payload: decisions.append(payload["kind"]) or True,
    )

    stored = database.get_application_record(application_id, conn=conn)
    assert (result, duration) == ("failed:missing_review_evidence", 11)
    assert decisions == ["material_approval"]
    assert stored["status"] == "failed"
    assert stored["final_approved_at"] is None
    assert stored["last_error"] == "failed:missing_review_evidence"


def test_html_dashboard_includes_application_history(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    application_id = database.create_application_record(job["url"], conn=conn)
    database.update_application_record(
        application_id,
        "ready_for_review",
        form_answers={"Authorized": "Yes"},
        conn=conn,
    )
    monkeypatch.setattr(dashboard_view, "get_connection", lambda: conn)

    output = tmp_path / "dashboard.html"
    dashboard_view.generate_dashboard(str(output))
    html = output.read_text(encoding="utf-8")

    assert "Application History" in html
    assert "ready_for_review" in html
    assert "Authorized" in html
    assert application_id in html


def test_resume_reprepares_after_browser_session_changes(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    application_id = database.create_application_record(job["url"], conn=conn)
    review_dir = tmp_path / "reviews"
    review_dir.mkdir()
    monkeypatch.setattr(launcher.config, "APPLICATION_REVIEW_DIR", review_dir)
    monkeypatch.setattr(launcher.config, "APPLY_CHECKPOINT_DB_PATH", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        launcher,
        "update_application_record",
        partial(database.update_application_record, conn=conn),
    )
    monkeypatch.setattr(
        launcher,
        "get_application_record",
        partial(database.get_application_record, conn=conn),
    )
    phases: list[str] = []

    def fake_run_job(*_args, application_id=None, phase="prepare", **_kwargs):
        phases.append(phase)
        if phase == "prepare":
            (review_dir / f"{application_id}.png").write_bytes(b"review")
            return "ready_for_review", 10
        (review_dir / f"{application_id}_submitted.png").write_bytes(b"proof")
        return "applied", 20

    monkeypatch.setattr(launcher, "run_job", fake_run_job)

    decisions = iter([True])

    def crash_at_final(payload):
        if payload["kind"] == "final_submission_approval":
            raise RuntimeError("simulated process stop")
        return next(decisions)

    try:
        launcher.run_safe_workflow(
            job,
            port=9222,
            worker_id=0,
            model="haiku",
            application_id=application_id,
            allow_submission=True,
            approval_callback=crash_at_final,
        )
    except RuntimeError as exc:
        assert str(exc) == "simulated process stop"
    else:  # pragma: no cover - protects the test setup
        raise AssertionError("workflow did not pause at final approval")

    result, duration = launcher.run_safe_workflow(
        job,
        port=9222,
        worker_id=0,
        model="haiku",
        application_id=application_id,
        allow_submission=True,
        approval_callback=lambda payload: payload["kind"] == "final_submission_approval",
        resume=True,
    )

    stored = database.get_application_record(application_id, conn=conn)
    assert (result, duration) == ("applied", 20)
    assert phases == ["prepare", "prepare", "submit"]
    assert stored["status"] == "submitted"
    assert stored["submission_snapshot_path"].endswith("_submitted.png")


def test_resume_of_completed_dry_run_starts_fresh_approved_attempt(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    application_id = database.create_application_record(job["url"], conn=conn)
    review_dir = tmp_path / "reviews"
    review_dir.mkdir()
    monkeypatch.setattr(launcher.config, "APPLICATION_REVIEW_DIR", review_dir)
    monkeypatch.setattr(launcher.config, "APPLY_CHECKPOINT_DB_PATH", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        launcher,
        "update_application_record",
        partial(database.update_application_record, conn=conn),
    )
    monkeypatch.setattr(
        launcher,
        "get_application_record",
        partial(database.get_application_record, conn=conn),
    )
    phases: list[str] = []

    def fake_run_job(*_args, application_id=None, phase="prepare", **_kwargs):
        phases.append(phase)
        if phase == "prepare":
            (review_dir / f"{application_id}.png").write_bytes(b"review")
            return "ready_for_review", 10
        (review_dir / f"{application_id}_submitted.png").write_bytes(b"proof")
        return "applied", 20

    monkeypatch.setattr(launcher, "run_job", fake_run_job)
    first_result, _ = launcher.run_safe_workflow(
        job,
        port=9222,
        worker_id=0,
        model="haiku",
        application_id=application_id,
        allow_submission=False,
        approval_callback=lambda _payload: True,
    )

    decisions: list[str] = []

    def approve(payload):
        decisions.append(payload["kind"])
        return True

    resumed_result, duration = launcher.run_safe_workflow(
        job,
        port=9222,
        worker_id=0,
        model="haiku",
        application_id=application_id,
        allow_submission=True,
        approval_callback=approve,
        resume=True,
    )

    stored = database.get_application_record(application_id, conn=conn)
    assert first_result == "ready_for_review"
    assert (resumed_result, duration) == ("applied", 20)
    assert phases == ["prepare", "prepare", "submit"]
    assert decisions == ["material_approval", "final_submission_approval"]
    assert stored["workflow_thread_id"].startswith(f"{application_id}:resume:")
    assert stored["status"] == "submitted"


def test_resume_dry_run_rejects_pending_final_without_submit(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    application_id = database.create_application_record(job["url"], conn=conn)
    review_dir = tmp_path / "reviews"
    review_dir.mkdir()
    monkeypatch.setattr(launcher.config, "APPLICATION_REVIEW_DIR", review_dir)
    monkeypatch.setattr(launcher.config, "APPLY_CHECKPOINT_DB_PATH", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        launcher,
        "update_application_record",
        partial(database.update_application_record, conn=conn),
    )
    monkeypatch.setattr(
        launcher,
        "get_application_record",
        partial(database.get_application_record, conn=conn),
    )
    phases: list[str] = []

    def fake_run_job(*_args, application_id=None, phase="prepare", **_kwargs):
        phases.append(phase)
        if phase != "prepare":
            raise AssertionError("dry-run resume reached submit phase")
        (review_dir / f"{application_id}.png").write_bytes(b"review")
        return "ready_for_review", 10

    monkeypatch.setattr(launcher, "run_job", fake_run_job)

    def stop_at_final(payload):
        if payload["kind"] == "final_submission_approval":
            raise RuntimeError("simulated process stop")
        return True

    try:
        launcher.run_safe_workflow(
            job,
            port=9222,
            worker_id=0,
            model="haiku",
            application_id=application_id,
            allow_submission=True,
            approval_callback=stop_at_final,
        )
    except RuntimeError as exc:
        assert str(exc) == "simulated process stop"

    result, duration = launcher.run_safe_workflow(
        job,
        port=9222,
        worker_id=0,
        model="haiku",
        application_id=application_id,
        allow_submission=False,
        approval_callback=lambda _payload: (_ for _ in ()).throw(
            AssertionError("pending final gate should be rejected automatically")
        ),
        resume=True,
    )

    stored = database.get_application_record(application_id, conn=conn)
    assert (result, duration) == ("ready_for_review", 10)
    assert phases == ["prepare"]
    assert stored["status"] == "ready_for_review"
    assert stored["submitted_at"] is None


def test_reacquire_application_handles_recent_and_stale_naive_locks(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    job = _insert_ready_job(conn, tmp_path)
    application_id = database.create_application_record(job["url"], conn=conn)
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    conn.execute(
        "UPDATE jobs SET apply_status = 'in_progress', last_attempted_at = ? WHERE url = ?",
        (launcher.datetime.now(launcher.timezone.utc).isoformat(), job["url"]),
    )
    conn.commit()

    try:
        launcher.reacquire_application(application_id)
    except RuntimeError as exc:
        assert "recent worker" in str(exc)
    else:  # pragma: no cover - protects the lock invariant
        raise AssertionError("a recent live lock was reclaimed")

    # Legacy rows may contain a naive UTC timestamp. They must not crash the
    # timezone-aware age calculation, and an old lock is safe to reclaim.
    conn.execute(
        "UPDATE jobs SET last_attempted_at = '2020-01-01T00:00:00' WHERE url = ?",
        (job["url"],),
    )
    conn.commit()
    acquired = launcher.reacquire_application(application_id, worker_id=3)

    assert acquired["url"] == job["url"]
    lock = conn.execute("SELECT apply_status, agent_id FROM jobs WHERE url = ?", (job["url"],)).fetchone()
    assert tuple(lock) == ("in_progress", "worker-3")


def test_existing_application_table_migrates_stage6_columns(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY)")
    conn.execute(
        """
        CREATE TABLE applications (
            id TEXT PRIMARY KEY,
            job_url TEXT NOT NULL,
            jd_snapshot_id INTEGER,
            company TEXT,
            job_title TEXT,
            application_url TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            resume_path TEXT,
            cover_letter_path TEXT,
            form_answers_json TEXT,
            review_snapshot_path TEXT,
            material_approved_at TEXT,
            final_approved_at TEXT,
            submitted_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    migrated = database.init_db(db_path)
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(applications)").fetchall()}

    assert set(database._APPLICATION_COLUMNS).issubset(columns)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 4
