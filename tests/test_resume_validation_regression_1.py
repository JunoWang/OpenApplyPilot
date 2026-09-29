from copy import deepcopy

from applypilot.scoring.validator import validate_json_fields, validate_tailored_resume


PROFILE = {
    "personal": {"full_name": "Test Candidate"},
    "resume_facts": {
        "preserved_companies": ["Snowflake"],
        "preserved_projects": [],
        "preserved_school": "University at Buffalo",
    },
    "skills_boundary": {"programming_languages": ["Python"]},
}


GENERATED = {
    "title": "Backend Engineer",
    "summary": "Built scalable solutions in Python.",
    "skills": {"Languages": "Python"},
    "experience": [
        {
            "header": "AI Research Intern",
            "subtitle": "Snowflake | 2025 - 2026",
            "bullets": ["Built scalable services."],
        }
    ],
    "projects": [
        {
            "header": "Research Copilot",
            "subtitle": "Python | 2026",
            "bullets": ["Built a retrieval workflow."],
        }
    ],
    "education": "University at Buffalo",
}


# Regression: ISSUE-003 — companies in experience subtitles were reported missing.
# Found by /qa on 2026-09-29
# Report: .gstack/qa-reports/qa-report-openapplypilot-e2e-2026-09-29.md
def test_company_in_experience_subtitle_is_preserved() -> None:
    result = validate_json_fields(GENERATED, PROFILE)

    assert result["passed"] is True
    assert not any("Snowflake" in error for error in result["errors"])


# Regression: ISSUE-003 — "scalable" was mistaken for the Scala language.
# Found by /qa on 2026-09-29
# Report: .gstack/qa-reports/qa-report-openapplypilot-e2e-2026-09-29.md
def test_watchlist_terms_require_token_boundaries() -> None:
    original = """Test Candidate

SUMMARY
Python engineer.

TECHNICAL SKILLS
Languages: Python

EXPERIENCE
AI Research Intern
Snowflake | 2025 - 2026
- Built services.

PROJECTS
None

EDUCATION
University at Buffalo
"""
    scalable = original.replace("Built services.", "Built scalable services.")

    accepted = validate_tailored_resume(scalable, PROFILE, original_text=original)

    assert accepted["passed"] is True
    assert not any("scala" in error.lower() for error in accepted["errors"])

    actual_scala = deepcopy(GENERATED)
    actual_scala["skills"] = {"Languages": "Python, Scala"}
    rejected = validate_json_fields(actual_scala, PROFILE)

    assert rejected["passed"] is False
    assert any("scala" in error.lower() for error in rejected["errors"])
