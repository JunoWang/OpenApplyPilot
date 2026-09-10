"""Import a user-selected job URL into the local pipeline database."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from applypilot.database import get_connection, save_jd_snapshot


def canonicalize_job_url(url: str) -> str:
    """Return a stable job URL, extracting LinkedIn's currentJobId when needed."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Job URL must be an absolute http(s) URL")
    host = parsed.netloc.lower().removeprefix("www.")
    if host.endswith("linkedin.com"):
        query_id = parse_qs(parsed.query).get("currentJobId", [None])[0]
        path_match = re.search(r"(?:-|/)(\d{8,12})(?:/)?$", parsed.path)
        job_id = query_id or (path_match.group(1) if path_match else None)
        if not job_id:
            raise ValueError("LinkedIn URL does not contain a job ID")
        return f"https://www.linkedin.com/jobs/view/{job_id}"
    return parsed._replace(query="", fragment="").geturl()


class _LinkedInJobParser(HTMLParser):
    """Extract the public LinkedIn job card and description without cookies."""

    _CLASS_TARGETS = {
        "top-card-layout__title": "title",
        "topcard__org-name-link": "company",
        "topcard__flavor--bullet": "location",
        "show-more-less-html__markup": "description",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, list[str]] = {
            "title": [],
            "company": [],
            "location": [],
            "description": [],
        }
        self._capture: str | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture:
            self._depth += 1
            if self._capture == "description" and tag == "li":
                self.values[self._capture].append("\n- ")
            elif self._capture == "description" and tag in {"br", "p", "ul"}:
                self.values[self._capture].append("\n")
            return

        classes = dict(attrs).get("class", "") or ""
        class_names = set(classes.split())
        for class_name, key in self._CLASS_TARGETS.items():
            if class_name in class_names and not self.values[key]:
                self._capture = key
                self._depth = 1
                return

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture == "description" and tag == "br":
            self.values[self._capture].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._capture:
            return
        self._depth -= 1
        if self._depth == 0:
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture:
            self.values[self._capture].append(data)

    def result(self) -> dict[str, str]:
        def clean_inline(parts: list[str]) -> str:
            return " ".join("".join(parts).split())

        description = "\n".join(
            line.strip() for line in "".join(self.values["description"]).splitlines() if line.strip()
        )
        return {
            "title": clean_inline(self.values["title"]),
            "company": clean_inline(self.values["company"]),
            "location": clean_inline(self.values["location"]),
            "full_description": description,
        }


def _fetch_public_html(url: str, timeout: int = 30) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36"
            )
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_linkedin_job(html: str) -> dict[str, str]:
    parser = _LinkedInJobParser()
    parser.feed(html)
    result = parser.result()
    if not result["title"] or not result["company"]:
        raise ValueError("LinkedIn public page did not expose job metadata")
    if len(result["full_description"]) < 200:
        raise ValueError("LinkedIn public page did not expose a complete job description")
    return result


def import_job_url(
    url: str,
    *,
    html: str | None = None,
    conn: Any | None = None,
) -> dict[str, str]:
    """Fetch, parse, and upsert one user-selected job with a JD snapshot."""
    canonical_url = canonicalize_job_url(url)
    host = urlparse(canonical_url).netloc.lower()
    if not host.endswith("linkedin.com"):
        raise ValueError("Direct import currently supports LinkedIn job URLs")

    parsed = parse_linkedin_job(html if html is not None else _fetch_public_html(canonical_url))
    now = datetime.now(timezone.utc).isoformat()
    if conn is None:
        conn = get_connection()
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, company, description, location, site, strategy,
            discovered_at, full_description, application_url,
            detail_scraped_at, enrichment_status, last_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'LinkedIn', 'direct_url', ?, ?, ?, ?,
                  'complete', ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            company = excluded.company,
            description = excluded.description,
            location = excluded.location,
            full_description = excluded.full_description,
            application_url = excluded.application_url,
            detail_scraped_at = excluded.detail_scraped_at,
            enrichment_status = 'complete',
            last_seen_at = excluded.last_seen_at,
            updated_at = excluded.updated_at
        """,
        (
            canonical_url,
            parsed["title"],
            parsed["company"],
            parsed["full_description"][:500],
            parsed["location"],
            now,
            parsed["full_description"],
            canonical_url,
            now,
            now,
            now,
        ),
    )
    conn.commit()
    save_jd_snapshot(
        conn,
        canonical_url,
        parsed["full_description"],
        source_url=canonical_url,
        captured_at=now,
    )
    return {"url": canonical_url, **parsed}
