from pathlib import Path

import pytest

from applypilot import database
from applypilot.scoring import scorer


class FakeClient:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = iter(responses)

    def chat(self, messages, **kwargs) -> str:
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def _job(url: str, title: str) -> tuple:
    return (
        url,
        title,
        "Example Co",
        "Remote",
        "A complete job description",
        "2026-09-02T12:00:00+00:00",
    )


def _database(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = database.init_db(db_path)
    conn.executemany(
        """
        INSERT INTO jobs (url, title, site, location, full_description, discovered_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            _job("https://example.test/1", "First role"),
            _job("https://example.test/2", "Second role"),
        ],
    )
    conn.commit()
    return conn


def test_score_parser_rejects_unstructured_response() -> None:
    with pytest.raises(ValueError, match="valid SCORE"):
        scorer._parse_score_response("This looks like a strong fit.")


def test_failed_score_stays_null_and_success_is_checkpointed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database(tmp_path)
    resume = tmp_path / "resume.txt"
    resume.write_text("Candidate resume", encoding="utf-8")
    fake = FakeClient(
        [
            "SCORE: 8\nKEYWORDS: Python, APIs\nREASONING: Strong relevant experience.",
            RuntimeError("provider unavailable"),
        ]
    )
    monkeypatch.setattr(scorer, "RESUME_PATH", resume)
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "get_client", lambda stage: fake)

    result = scorer.run_scoring()

    rows = conn.execute("SELECT url, fit_score, scored_at FROM jobs ORDER BY url").fetchall()
    assert result["scored"] == 1
    assert result["errors"] == 1
    assert rows[0]["fit_score"] == 8
    assert rows[0]["scored_at"] is not None
    assert rows[1]["fit_score"] is None
    assert rows[1]["scored_at"] is None
