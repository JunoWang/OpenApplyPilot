from unittest.mock import MagicMock

from applypilot.apply.adapters.ashby import _choose_yes_no, build_answer_plan, is_ashby_page


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


def _yes_no_page(*, pressed_states: list[str]) -> tuple[MagicMock, MagicMock]:
    page = MagicMock()
    container = page.locator.return_value.filter.return_value
    container.count.return_value = 1
    button = container.first.get_by_role.return_value
    button.count.return_value = 1
    button.first.get_attribute.side_effect = pressed_states
    return page, button.first


def test_choose_yes_no_leaves_matching_selection_untouched() -> None:
    page, button = _yes_no_page(pressed_states=["true", "true"])
    errors: list[str] = []

    _choose_yes_no(page, "worked at a startup", True, errors)

    button.click.assert_not_called()
    assert errors == []


def test_choose_yes_no_clicks_only_when_selection_is_missing() -> None:
    page, button = _yes_no_page(pressed_states=["false", "true"])
    errors: list[str] = []

    _choose_yes_no(page, "worked at a startup", True, errors)

    button.click.assert_called_once_with()
    assert errors == []
