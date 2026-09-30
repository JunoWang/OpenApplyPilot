from applypilot.apply import prompt


# Regression: ISSUE-005 — cookie overlays blocked the agent before the Apply button.
# Found by /qa on 2026-09-30
# Report: .gstack/qa-reports/qa-report-openapplypilot-e2e-2026-09-30.md
def test_apply_prompt_handles_blocking_cookie_banners(tmp_path, monkeypatch) -> None:
    resume = tmp_path / "resume.txt"
    resume.write_text("Tailored resume", encoding="utf-8")
    resume.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(
        prompt.config,
        "load_profile",
        lambda: {
            "personal": {
                "full_name": "Test Candidate",
                "email": "candidate@example.test",
                "phone": "5550100",
                "city": "Houston",
                "country": "USA",
            },
            "work_authorization": {},
            "mobility": {},
            "compensation": {
                "salary_expectation": "100000",
                "salary_currency": "USD",
            },
            "experience": {},
            "availability": {},
            "eeo_voluntary": {},
        },
    )
    monkeypatch.setattr(prompt.config, "load_search_config", dict)
    monkeypatch.setattr(prompt.config, "load_env", lambda: None)

    value = prompt.build_prompt(
        {
            "url": "https://example.test/job",
            "title": "Engineer",
            "company": "Example",
            "site": "Workday",
            "fit_score": 8,
            "tailored_resume_path": str(resume),
        },
        "Tailored resume",
        dry_run=True,
        review_snapshot_path=str(tmp_path / "review.png"),
        upload_dir=tmp_path / "uploads",
    )

    assert "If a cookie/privacy banner blocks the page" in value
    assert "Decline, Reject, or Only necessary" in value
