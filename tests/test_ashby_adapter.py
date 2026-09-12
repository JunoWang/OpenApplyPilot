from applypilot.apply.adapters.ashby import build_answer_plan, is_ashby_page


def test_ashby_answer_plan_uses_only_profile_facts() -> None:
    profile = {
        "personal": {
            "full_name": "Candidate",
            "email": "candidate@example.test",
            "phone": "5550100",
            "city": "Houston",
            "linkedin_url": "linkedin.com/in/candidate",
        },
        "experience": {"education_level": "Master"},
        "work_authorization": {"require_sponsorship": False},
        "mobility": {"willing_to_relocate": True},
        "resume_facts": {"preserved_school": "Example University"},
        "application_facts": {
            "pronouns": "",
            "currently_enrolled": True,
            "worked_at_startup": True,
            "major_minor": "Computer Science",
        },
    }

    plan = build_answer_plan(profile)

    assert plan["Pronouns"] == ""
    assert plan["Do you have a college degree?"] is True
    assert plan["What institution(s) did you graduate from?"] == "Example University"
    assert plan["What was your major/minor?"] == "Computer Science"
    assert plan["currently enrolled"] is True
    assert plan["require"] is False
    assert plan["worked at a startup"] is True
    assert plan["open to working"] is True


def test_ashby_page_detection_is_exact() -> None:
    assert is_ashby_page("https://jobs.ashbyhq.com/example/123") is True
    assert is_ashby_page("https://evil.example/?next=jobs.ashbyhq.com") is False
