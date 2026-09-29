import importlib
from pathlib import Path

wizard = importlib.import_module("applypilot.wizard.init")


# Regression: ISSUE-001 — provider and CAPTCHA keys were echoed by the setup wizard.
# Found by /qa on 2026-09-29
# Report: .gstack/qa-reports/qa-report-openapplypilot-e2e-2026-09-29.md
def test_setup_hides_api_keys(tmp_path: Path, monkeypatch) -> None:
    prompts: list[tuple[str, dict]] = []
    answers = {
        "Provider": "openai",
        "OpenAI API key": "fake-openai-key",
        "Model": "gpt-4o-mini",
        "CapSolver API key": "fake-capsolver-key",
    }

    def ask(label: str, **kwargs):
        prompts.append((label, kwargs))
        return answers[label]

    monkeypatch.setattr(wizard.Prompt, "ask", ask)
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(wizard, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(wizard.shutil, "which", lambda _name: "/usr/bin/claude")

    wizard._setup_ai_features()
    wizard._setup_auto_apply()

    prompt_options = {label: kwargs for label, kwargs in prompts}
    assert prompt_options["OpenAI API key"]["password"] is True
    assert prompt_options["CapSolver API key"]["password"] is True
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


# Regression: ISSUE-001 — onboarding copied personal files with world-readable modes.
# Found by /qa on 2026-09-29
# Report: .gstack/qa-reports/qa-report-openapplypilot-e2e-2026-09-29.md
def test_setup_restricts_personal_file_permissions(tmp_path: Path, monkeypatch) -> None:
    source_resume = tmp_path / "source-resume.txt"
    source_resume.write_text("Test Candidate", encoding="utf-8")
    source_resume.chmod(0o644)

    resume_path = tmp_path / "home" / "resume.txt"
    profile_path = tmp_path / "home" / "profile.json"
    searches_path = tmp_path / "home" / "searches.yaml"
    resume_path.parent.mkdir()
    monkeypatch.setattr(wizard, "RESUME_PATH", resume_path)
    monkeypatch.setattr(wizard, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(wizard, "SEARCH_CONFIG_PATH", searches_path)

    def ask(label: str, **kwargs):
        if label == "Resume file path":
            return str(source_resume)
        if label == "Target location (e.g. 'Remote', 'Canada', 'New York, NY')":
            return "Remote"
        if label == "Search radius in miles (0 for remote-only)":
            return "0"
        if label.startswith("Target job titles"):
            return "Backend Engineer"
        if label == "Full name":
            return "Test Candidate"
        if label == "Email address":
            return "candidate@example.test"
        if label == "City":
            return "Test City"
        if label == "Country":
            return "USA"
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard.Prompt, "ask", ask)
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *args, **kwargs: False)

    wizard._setup_resume()
    wizard._setup_profile()
    wizard._setup_searches()

    assert resume_path.stat().st_mode & 0o777 == 0o600
    assert profile_path.stat().st_mode & 0o777 == 0o600
    assert searches_path.stat().st_mode & 0o777 == 0o600
