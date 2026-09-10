from applypilot import database
from applypilot.ingest import canonicalize_job_url, import_job_url, parse_linkedin_job


LINKEDIN_HTML = """
<html><body>
<h1 class="top-card-layout__title topcard__title">Backend/AI Engineer</h1>
<a class="topcard__org-name-link">Reducto</a>
<span class="topcard__flavor topcard__flavor--bullet">San Francisco, CA</span>
<div class="show-more-less-html__markup">
  <strong>About the role<br></strong>
  Build production LLM document systems and scalable APIs.
  <ul><li>Integrate and optimize LLM calls.</li><li>Build document pipelines.</li></ul>
  Work directly with customers and founders. This is a detailed job description
  with enough public information to be saved as an immutable local JD snapshot.
</div>
</body></html>
"""


def test_canonicalizes_linkedin_search_and_slug_urls() -> None:
    assert (
        canonicalize_job_url("https://www.linkedin.com/jobs/search-results/?currentJobId=4339144508&refId=x")
        == "https://www.linkedin.com/jobs/view/4339144508"
    )
    assert (
        canonicalize_job_url("https://www.linkedin.com/jobs/view/backend-ai-engineer-at-reducto-4339144508")
        == "https://www.linkedin.com/jobs/view/4339144508"
    )


def test_parses_linkedin_public_job_page() -> None:
    parsed = parse_linkedin_job(LINKEDIN_HTML)

    assert parsed["title"] == "Backend/AI Engineer"
    assert parsed["company"] == "Reducto"
    assert parsed["location"] == "San Francisco, CA"
    assert "- Integrate and optimize LLM calls." in parsed["full_description"]


def test_imports_job_and_preserves_jd_snapshot(tmp_path) -> None:
    conn = database.init_db(tmp_path / "jobs.db")
    result = import_job_url(
        "https://www.linkedin.com/jobs/search-results/?currentJobId=4339144508",
        html=LINKEDIN_HTML,
        conn=conn,
    )

    job = conn.execute("SELECT * FROM jobs WHERE url = ?", (result["url"],)).fetchone()
    snapshots = database.get_jd_history(result["url"], conn=conn)
    assert job["title"] == "Backend/AI Engineer"
    assert job["company"] == "Reducto"
    assert job["strategy"] == "direct_url"
    assert job["enrichment_status"] == "complete"
    assert snapshots[0]["content"] == job["full_description"]
