import importlib

from applypilot import config, database, logging_setup

wizard = importlib.import_module("applypilot.wizard.init")


# Regression: ISSUE-002 — doctor reported a missing DB and log immediately after init.
# Found by /qa on 2026-09-29
# Report: .gstack/qa-reports/qa-report-openapplypilot-e2e-2026-09-29.md
def test_completed_wizard_initializes_database_and_log(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(wizard, "ensure_dirs", lambda: None)
    monkeypatch.setattr(wizard, "_setup_resume", lambda: None)
    monkeypatch.setattr(wizard, "_setup_profile", dict)
    monkeypatch.setattr(wizard, "_setup_searches", lambda: None)
    monkeypatch.setattr(wizard, "_setup_ai_features", lambda: None)
    monkeypatch.setattr(wizard, "_setup_auto_apply", lambda: None)
    monkeypatch.setattr(logging_setup, "configure_file_logging", lambda: calls.append("log"))
    monkeypatch.setattr(database, "init_db", lambda: calls.append("db"))
    monkeypatch.setattr(config, "get_tier", lambda: 1)

    wizard.run_wizard()

    assert calls == ["log", "db"]
