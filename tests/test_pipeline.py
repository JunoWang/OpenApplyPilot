import pytest

from applypilot import pipeline
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
