import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from applypilot import cli, database, review_center
from applypilot.apply import launcher


def _review_record(tmp_path, monkeypatch):
    db_path = tmp_path / "review.db"
    conn = database.init_db(db_path)
    resume = tmp_path / "resume.pdf"
    screenshot = tmp_path / "review.png"
    profile = tmp_path / "profile.json"
    resume.write_bytes(b"%PDF-1.4\nreview")
    screenshot.write_bytes(b"png evidence")
    profile.write_text('{"name":"Candidate"}', encoding="utf-8")
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, company, application_url, full_description, fit_score,
            tailored_pdf_path, discovered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "https://example.test/job",
            "Backend Engineer",
            "Example Co",
            "https://apply.example.test/job",
            "Build reliable systems",
            8,
            str(resume),
            "2026-09-11T00:00:00Z",
        ),
    )
    conn.commit()
    application_id = database.create_application_record("https://example.test/job", conn=conn)
    database.update_application_record(
        application_id,
        "ready_for_review",
        form_answers={"Authorized to work?": "Yes"},
        review_snapshot_path=str(screenshot),
        conn=conn,
    )
    monkeypatch.setattr(review_center, "get_connection", lambda: database.get_connection(db_path))
    monkeypatch.setattr(
        review_center,
        "get_application_record",
        lambda application_id: database.get_application_record(
            application_id,
            conn=database.get_connection(db_path),
        ),
    )
    monkeypatch.setattr(
        review_center,
        "list_application_reviews",
        lambda application_id, **_kwargs: database.list_application_reviews(
            application_id,
            conn=database.get_connection(db_path),
        ),
    )
    monkeypatch.setattr(
        review_center,
        "record_application_review",
        lambda application_id, decision, **kwargs: database.record_application_review(
            application_id,
            decision,
            conn=database.get_connection(db_path),
            **kwargs,
        ),
    )
    monkeypatch.setattr(review_center.config, "PROFILE_PATH", profile)
    return conn, application_id


@pytest.fixture
def review_server(tmp_path, monkeypatch):
    conn, application_id = _review_record(tmp_path, monkeypatch)
    server = review_center._ReviewServer(
        ("127.0.0.1", 0),
        review_center.ReviewCenterHandler,
        csrf_token="test-csrf-token",
        nonce="test-nonce",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield conn, application_id, server, thread, base_url
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_review_center_renders_evidence_answers_and_safety_gate(review_server) -> None:
    _, application_id, _, _, base_url = review_server

    with urlopen(f"{base_url}/?id={application_id}") as response:
        html = response.read().decode()

    assert response.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert "Backend Engineer" in html
    assert "Authorized to work?" in html
    assert "Form screenshot" in html
    assert "Approve &amp; open final browser review" in html
    assert "This does not submit." in html


def test_review_center_rejects_invalid_csrf(review_server) -> None:
    _, application_id, _, _, base_url = review_server
    request = Request(
        f"{base_url}/decision/{application_id}",
        data=urlencode({"csrf_token": "wrong", "decision": "approved"}).encode(),
        method="POST",
    )

    with pytest.raises(HTTPError) as error:
        urlopen(request)

    assert error.value.code == 403


def test_review_center_serves_only_registered_artifacts(review_server) -> None:
    _, application_id, _, _, base_url = review_server

    with urlopen(f"{base_url}/artifact/{application_id}/resume_path") as response:
        assert response.read().startswith(b"%PDF")
        assert response.headers["Content-Type"] == "application/pdf"

    with pytest.raises(HTTPError) as error:
        urlopen(f"{base_url}/artifact/{application_id}/../../profile.json")

    assert error.value.code == 404


def test_review_center_does_not_fall_back_for_unknown_query_id(review_server) -> None:
    _, _, _, _, base_url = review_server

    with pytest.raises(HTTPError) as error:
        urlopen(f"{base_url}/?id=missing-application")

    assert error.value.code == 404


def test_review_center_records_change_request(review_server) -> None:
    conn, application_id, _, _, base_url = review_server
    request = Request(
        f"{base_url}/decision/{application_id}",
        data=urlencode(
            {
                "csrf_token": "test-csrf-token",
                "decision": "changes_requested",
                "notes": "Update the location answer",
            }
        ).encode(),
        method="POST",
    )

    with urlopen(request) as response:
        assert response.status == 200

    application = database.get_application_record(application_id, conn=conn)
    reviews = database.list_application_reviews(application_id, conn=conn)
    assert application["status"] == "changes_requested"
    assert reviews[0]["decision"] == "changes_requested"
    assert reviews[0]["notes"] == "Update the location answer"


def test_review_center_approval_returns_safe_transition_action(review_server) -> None:
    conn, application_id, server, thread, base_url = review_server
    request = Request(
        f"{base_url}/decision/{application_id}",
        data=urlencode({"csrf_token": "test-csrf-token", "decision": "approved"}).encode(),
        method="POST",
    )

    with urlopen(request) as response:
        transition = response.read().decode()
        assert "Review saved" in transition
        assert "final browser review" in transition
    thread.join(timeout=2)

    assert server.action is not None
    assert server.action.kind == "final_review"
    assert server.action.application_id == application_id
    assert database.get_application_record(application_id, conn=conn)["status"] == "review_approved"


def test_material_fingerprint_invalidates_changed_profile(tmp_path, monkeypatch) -> None:
    conn, application_id = _review_record(tmp_path, monkeypatch)
    application = database.get_application_record(application_id, conn=conn)
    before = review_center.material_fingerprint(application)

    review_center.config.PROFILE_PATH.write_text('{"name":"Changed"}', encoding="utf-8")

    assert review_center.material_fingerprint(application) != before


def test_external_job_links_allow_only_http_schemes() -> None:
    assert review_center._safe_external_url("https://example.test/job") == "https://example.test/job"
    assert review_center._safe_external_url("javascript:alert(1)") == "#"


def test_review_center_fails_closed_for_unknown_selected_id(tmp_path, monkeypatch) -> None:
    _review_record(tmp_path, monkeypatch)

    with pytest.raises(KeyError, match="Application not found"):
        review_center.serve_review_center(
            port=0,
            selected_id="missing-application",
            open_browser=False,
        )


def test_review_command_reuses_material_approval_but_keeps_final_gate(monkeypatch) -> None:
    action = review_center.ReviewAction("final_review", "application-id", "fingerprint")
    launched: dict = {}
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(review_center, "serve_review_center", lambda **_kwargs: action)
    monkeypatch.setattr(review_center, "material_fingerprint", lambda _application: "fingerprint")
    monkeypatch.setattr("applypilot.config.check_tier", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("applypilot.database.get_application_record", lambda _application_id: {"id": "application-id"})
    monkeypatch.setattr(launcher, "_terminal_approval", lambda _payload: False)
    monkeypatch.setattr(launcher, "main", lambda **kwargs: launched.update(kwargs))

    cli.review(application_id=None, port=0, no_open=True, model="haiku")

    callback = launched["approval_callback"]
    assert callback({"kind": "material_approval"}) is True
    assert callback({"kind": "final_submission_approval"}) is False
    assert launched["dry_run"] is False
    assert launched["resume_application_id"] == "application-id"
