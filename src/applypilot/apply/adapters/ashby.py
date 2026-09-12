"""Deterministic repair and answer capture for Ashby application forms."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError


@dataclass
class AshbyResult:
    handled: bool = False
    answers: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def is_ashby_page(url: str) -> bool:
    """Return whether a browser page is an Ashby-hosted application."""
    return urlparse(url).hostname == "jobs.ashbyhq.com"


def build_answer_plan(profile: dict) -> dict[str, object]:
    """Build truthful Ashby answers solely from the saved local profile."""
    personal = profile.get("personal", {})
    facts = profile.get("application_facts", {})
    experience = profile.get("experience", {})
    work_auth = profile.get("work_authorization", {})
    mobility = profile.get("mobility", {})
    resume_facts = profile.get("resume_facts", {})

    college_degree = facts.get("college_degree")
    if college_degree is None and experience.get("education_level"):
        college_degree = True

    return {
        "Name": personal.get("full_name", ""),
        "Email": personal.get("email", ""),
        "Phone Number": personal.get("phone", ""),
        "Pronouns": facts.get("pronouns", ""),
        "LinkedIn": personal.get("linkedin_url", ""),
        "Location": personal.get("city", ""),
        "Do you have a college degree?": college_degree,
        "What institution(s) did you graduate from?": (
            facts.get("institutions") or resume_facts.get("preserved_school", "")
        ),
        "What was your major/minor?": facts.get("major_minor", ""),
        "currently enrolled": facts.get("currently_enrolled"),
        "require": work_auth.get("require_sponsorship"),
        "worked at a startup": facts.get("worked_at_startup"),
        "open to working": facts.get(
            "open_to_onsite",
            mobility.get("willing_to_relocate"),
        ),
    }


def _field_container(page, question: str):
    return page.locator(".ashby-application-form-field-entry").filter(
        has_text=re.compile(re.escape(question), re.IGNORECASE)
    )


def _fill_labeled(page, label: str, value: object, errors: list[str]) -> None:
    locator = page.get_by_label(label, exact=True)
    if not locator.count():
        return
    if value is None or (not str(value).strip() and label != "Pronouns"):
        if locator.first.get_attribute("required") is not None:
            errors.append(f"missing_profile_value:{label}")
        return
    locator.first.fill(str(value))


def _choose_yes_no(page, question: str, value: object, errors: list[str]) -> None:
    container = _field_container(page, question)
    if not container.count():
        return
    if value is None:
        errors.append(f"missing_profile_value:{question}")
        return
    answer = "Yes" if bool(value) else "No"
    button = container.first.get_by_role("button", name=answer, exact=True)
    if not button.count():
        errors.append(f"missing_control:{question}")
        return
    button.first.click()
    if button.first.get_attribute("aria-pressed") != "true":
        errors.append(f"selection_not_applied:{question}")


def repair_and_collect(page, profile: dict, resume_path: Path) -> AshbyResult:
    """Repair an Ashby form deterministically without touching Submit."""
    if not is_ashby_page(page.url):
        return AshbyResult()

    result = AshbyResult(handled=True)
    plan = build_answer_plan(profile)

    for label in (
        "Name",
        "Email",
        "Phone Number",
        "Pronouns",
        "LinkedIn",
        "What institution(s) did you graduate from?",
        "What was your major/minor?",
    ):
        _fill_labeled(page, label, plan[label], result.errors)

    resume = page.get_by_label("Resume", exact=True)
    if resume.count():
        if not resume_path.is_file():
            result.errors.append("missing_resume_file")
        else:
            resume.first.set_input_files(str(resume_path))
            try:
                page.get_by_text(resume_path.name, exact=True).wait_for(state="visible", timeout=15_000)
            except PlaywrightError:
                result.errors.append("resume_upload_unconfirmed")

    city = str(plan["Location"] or "").strip()
    location_container = _field_container(page, "Location")
    if location_container.count():
        combo = location_container.first.get_by_role("combobox")
        if not city:
            result.errors.append("missing_profile_value:Location")
        elif combo.count():
            combo.fill(city)
            options = page.get_by_role("option").filter(
                has_text=re.compile(rf"^{re.escape(city)},", re.IGNORECASE)
            )
            try:
                options.first.wait_for(state="visible", timeout=10_000)
                options.first.click()
            except PlaywrightError:
                result.errors.append("location_option_not_found")
        else:
            result.errors.append("missing_control:Location")

    _choose_yes_no(page, "college degree", plan["Do you have a college degree?"], result.errors)
    _choose_yes_no(page, "currently enrolled", plan["currently enrolled"], result.errors)
    _choose_yes_no(page, "require", plan["require"], result.errors)
    _choose_yes_no(page, "worked at a startup", plan["worked at a startup"], result.errors)
    _choose_yes_no(page, "open to working", plan["open to working"], result.errors)

    # Capture actual browser state rather than trusting the agent's report.
    for label in (
        "Name",
        "Email",
        "Phone Number",
        "Pronouns",
        "LinkedIn",
        "What institution(s) did you graduate from?",
        "What was your major/minor?",
    ):
        locator = page.get_by_label(label, exact=True)
        if locator.count():
            result.answers[label] = locator.first.input_value()
    if location_container.count():
        result.answers["Location"] = location_container.first.get_by_role("combobox").input_value()
    if resume.count():
        result.answers["Resume"] = resume.first.evaluate(
            "el => el.files && el.files.length ? el.files[0].name : ''"
        )
    for question in (
        "college degree",
        "currently enrolled",
        "require",
        "worked at a startup",
        "open to working",
    ):
        container = _field_container(page, question)
        if container.count():
            question_text = container.first.inner_text().splitlines()[0].strip()
            selected = container.first.locator('button[aria-pressed="true"]')
            result.answers[question_text] = selected.first.inner_text().strip() if selected.count() else ""

    return result
