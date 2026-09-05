"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import json
import logging
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from applypilot.config import TAILORED_DIR
from applypilot.database import get_connection

log = logging.getLogger(__name__)


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (SUMMARY, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: first few lines before SUMMARY
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().upper() == "SUMMARY":
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    # The header may have 3 or 4 lines depending on whether location is included
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        # Could be location or contact -- check for email/phone indicators
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        # Detect section headers (all caps, no leading dash/bullet, longer than 3 chars)
        if (
            stripped
            and stripped == stripped.upper()
            and not stripped.startswith("-")
            and len(stripped) > 3
            and not stripped.startswith("\u2022")
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template ────────────────────────────────────────────────────────

def build_html(resume: dict) -> str:
    """Build professional resume HTML from parsed data.

    Args:
        resume: Parsed resume dict from parse_resume().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]

    # Skills
    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        skills = parse_skills(sections["TECHNICAL SKILLS"])
        rows = ""
        for cat, val in skills:
            rows += (
                '<div class="skill-row"><span class="skill-cat">'
                f"{escape(cat)}:</span> {escape(val)}</div>\n"
            )
        skills_html = f'<div class="section"><div class="section-title">Technical Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{escape(b)}</li>" for b in e["bullets"])
            subtitle = (
                f'<div class="entry-subtitle">{escape(e["subtitle"])}</div>'
                if e["subtitle"] else ""
            )
            items += (
                '<div class="entry"><div class="entry-title">'
                f'{escape(e["title"])}</div>{subtitle}<ul>{bullets}</ul></div>'
            )
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{escape(b)}</li>" for b in e["bullets"])
            subtitle = (
                f'<div class="entry-subtitle">{escape(e["subtitle"])}</div>'
                if e["subtitle"] else ""
            )
            items += (
                '<div class="entry"><div class="entry-title">'
                f'{escape(e["title"])}</div>{subtitle}<ul>{bullets}</ul></div>'
            )
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        education = "".join(
            f'<div class="education">{escape(line.strip())}</div>'
            for line in sections["EDUCATION"].splitlines()
            if line.strip()
        )
        edu_html = (
            '<div class="section"><div class="section-title">Education</div>'
            f"{education}</div>"
        )

    # Publications
    pub_html = ""
    if "PUBLICATIONS" in sections:
        publications = "".join(
            f'<div class="publication">{escape(line.strip())}</div>'
            for line in sections["PUBLICATIONS"].splitlines()
            if line.strip()
        )
        pub_html = (
            '<div class="section"><div class="section-title">Publications</div>'
            f"{publications}</div>"
        )

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = (
            '<div class="section"><div class="section-title">Summary</div>'
            f'<div class="summary">{escape(sections["SUMMARY"].strip())}</div></div>'
        )

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(escape(part) for part in contact_parts)

    # Location line (may be empty)
    location_html = (
        f'<div class="location">{escape(resume["location"])}</div>'
        if resume["location"] else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.25in 0.5in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.3;
    color: #1a1a1a;
}}
.header {{
    text-align: center;
    margin-bottom: 3px;
    padding-bottom: 3px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #1a3a5c;
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10.5pt;
    color: #3a6b8c;
    margin: 1px 0;
}}
.location {{
    font-size: 9pt;
    color: #555;
}}
.contact {{
    font-size: 9pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: 3px;
}}
.section-title {{
    font-size: 10pt;
    font-weight: 700;
    color: #1a3a5c;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid #2a7ab5;
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.summary {{
    font-size: 9.5pt;
    color: #333;
    line-height: 1.3;
}}
.skill-row {{
    font-size: 9.5pt;
    margin: 0;
    line-height: 1.28;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 2px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 10pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 9pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 0;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9.5pt;
    margin-bottom: 0;
    line-height: 1.28;
}}
.education {{
    font-size: 9.25pt;
    line-height: 1.25;
}}
.publication {{
    font-size: 9.25pt;
    line-height: 1.25;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{escape(resume['name'])}</div>
    <div class="title">{escape(resume['title'])}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{summary_html}
{skills_html}
{exp_html}
{proj_html}
{pub_html}
{edu_html}
</body>
</html>"""


# ── ATS-friendly DOCX Renderer ──────────────────────────────────────────

def _set_run_font(run, *, size: float, bold: bool = False, color: str = "1A1A1A") -> None:
    """Apply explicit Arial typography across Word and LibreOffice."""
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    run.font.name = "Arial"
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), "Arial")
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), "Arial")
    run.font.size = Pt(size)
    run.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)


def _add_docx_paragraph(
    document,
    text: str,
    *,
    size: float = 9.5,
    bold: bool = False,
    before: float = 0,
    after: float = 1.5,
    keep_with_next: bool = False,
):
    """Add a plain single-column paragraph using the ATS compact override."""
    from docx.shared import Pt

    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(before)
    paragraph.paragraph_format.space_after = Pt(after)
    paragraph.paragraph_format.line_spacing = 1.05
    paragraph.paragraph_format.keep_with_next = keep_with_next
    run = paragraph.add_run(text)
    _set_run_font(run, size=size, bold=bold)
    return paragraph


def _add_docx_section_heading(document, text: str) -> None:
    """Add an ATS-readable section heading with no decorative objects."""
    from docx.shared import Pt

    paragraph = document.add_paragraph(style="Heading 1")
    paragraph.paragraph_format.space_before = Pt(5)
    paragraph.paragraph_format.space_after = Pt(2)
    paragraph.paragraph_format.keep_with_next = True
    run = paragraph.add_run(text.upper())
    _set_run_font(run, size=10.5, bold=True, color="1A3A5C")


def build_docx(resume: dict, output_path: Path) -> Path:
    """Build a simple, single-column DOCX designed for ATS parsing.

    The base is ``compact_reference_guide`` with a named
    ``ats_single_column_resume`` override: Arial, 0.55 inch margins, compact
    paragraph rhythm, no tables, no text boxes, and no headers or footers.
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    document = Document()
    section = document.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.55)
    section.bottom_margin = Inches(0.55)
    section.left_margin = Inches(0.6)
    section.right_margin = Inches(0.6)
    section.header_distance = Inches(0.25)
    section.footer_distance = Inches(0.25)

    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(9.5)
    normal.paragraph_format.space_after = Pt(1.5)
    normal.paragraph_format.line_spacing = 1.05

    name = _add_docx_paragraph(document, resume["name"], size=18, bold=True, after=0)
    name.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title = _add_docx_paragraph(document, resume["title"], size=10.5, after=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if resume.get("location"):
        location = _add_docx_paragraph(document, resume["location"], size=9, after=0)
        location.alignment = WD_ALIGN_PARAGRAPH.CENTER
    contact = _add_docx_paragraph(document, resume.get("contact", ""), size=9, after=3)
    contact.alignment = WD_ALIGN_PARAGRAPH.CENTER

    sections = resume["sections"]
    if sections.get("SUMMARY"):
        _add_docx_section_heading(document, "Summary")
        _add_docx_paragraph(document, sections["SUMMARY"].strip())

    if sections.get("TECHNICAL SKILLS"):
        _add_docx_section_heading(document, "Technical Skills")
        for category, values in parse_skills(sections["TECHNICAL SKILLS"]):
            paragraph = _add_docx_paragraph(document, "", after=0.5)
            label = paragraph.add_run(f"{category}: ")
            _set_run_font(label, size=9.5, bold=True, color="1A3A5C")
            value = paragraph.add_run(values)
            _set_run_font(value, size=9.5)

    for section_name in ("EXPERIENCE", "PROJECTS"):
        if not sections.get(section_name):
            continue
        _add_docx_section_heading(document, section_name.title())
        for entry in parse_entries(sections[section_name]):
            _add_docx_paragraph(
                document,
                entry["title"],
                size=10,
                bold=True,
                after=0,
                keep_with_next=True,
            )
            if entry["subtitle"]:
                _add_docx_paragraph(
                    document,
                    entry["subtitle"],
                    size=9,
                    after=0.5,
                    keep_with_next=True,
                )
            for bullet in entry["bullets"]:
                paragraph = document.add_paragraph(style="List Bullet")
                paragraph.paragraph_format.left_indent = Inches(0.18)
                paragraph.paragraph_format.first_line_indent = Inches(-0.14)
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(0.7)
                paragraph.paragraph_format.line_spacing = 1.03
                run = paragraph.add_run(bullet)
                _set_run_font(run, size=9.25)

    if sections.get("PUBLICATIONS"):
        _add_docx_section_heading(document, "Publications")
        for line in sections["PUBLICATIONS"].splitlines():
            if line.strip():
                _add_docx_paragraph(document, line.strip(), after=0.5)

    if sections.get("EDUCATION"):
        _add_docx_section_heading(document, "Education")
        for line in sections["EDUCATION"].splitlines():
            if line.strip():
                _add_docx_paragraph(document, line.strip(), after=0.5)

    # Avoid leaking local usernames or office application defaults in metadata.
    document.core_properties.author = "OpenApplyPilot"
    document.core_properties.last_modified_by = "OpenApplyPilot"
    document.core_properties.title = f"Resume - {resume['title']}"

    # Disable auto-hyphenation for stable ATS text extraction.
    settings = document.settings.element
    auto_hyphenation = settings.find(qn("w:autoHyphenation"))
    if auto_hyphenation is None:
        auto_hyphenation = OxmlElement("w:autoHyphenation")
        settings.append(auto_hyphenation)
    auto_hyphenation.set(qn("w:val"), "0")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    output_path.chmod(0o600)
    return output_path


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    html = build_html(resume)

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out))
    log.info("PDF generated: %s", out)
    return out


def convert_to_docx(text_path: Path, output_path: Path | None = None) -> Path:
    """Convert a structured text resume to an ATS-friendly DOCX."""
    text_path = Path(text_path)
    resume = parse_resume(text_path.read_text(encoding="utf-8"))
    out = Path(output_path or text_path.with_suffix(".docx"))
    build_docx(resume, out)
    log.info("DOCX generated: %s", out)
    return out


def _record_export_artifacts(
    report_path: str | None,
    docx_path: Path,
    pdf_path: Path,
) -> None:
    """Add final DOCX/PDF paths to an existing tailoring audit report."""
    if not report_path:
        return

    path = Path(report_path)
    if not path.is_file():
        log.warning("Tailoring report is missing; export paths were not recorded: %s", path)
        return

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        report.setdefault("artifacts", {}).update(
            {"docx": str(docx_path), "pdf": str(pdf_path)}
        )
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        path.chmod(0o600)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        log.warning("Could not update tailoring report %s: %s", path, exc)


def batch_convert(
    limit: int = 50,
    job_url: str | None = None,
    force: bool = False,
) -> dict:
    """Generate DOCX and PDF artifacts for tailored resumes in the database.

    Selects only database-approved tailored resumes and converts artifact sets
    whose DOCX or PDF path has not yet been recorded.

    Args:
        limit: Maximum number of files to convert.
        job_url: When provided, export only the matching job.
        force: Rebuild artifacts even when both paths are already recorded.

    Returns:
        Counts and per-job results for generated artifacts.
    """
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return {"converted": 0, "errors": 0, "results": []}

    conn = get_connection()
    where = ["tailored_resume_path IS NOT NULL", "tailor_status = 'complete'"]
    params: list[str | int] = []
    if job_url:
        where.append("url = ?")
        params.append(job_url)
    if not force:
        where.append("(tailored_docx_path IS NULL OR tailored_pdf_path IS NULL)")
    query = f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY tailored_at DESC LIMIT ?"
    params.append(limit)
    jobs = [dict(row) for row in conn.execute(query, params).fetchall()]

    if not jobs:
        log.info("No selected approved tailored resumes need artifact export.")
        return {"converted": 0, "errors": 0, "results": []}

    log.info("Exporting DOCX and PDF for %d tailored resumes...", len(jobs))
    converted = 0
    errors = 0
    results: list[dict] = []
    for job in jobs:
        text_path = Path(job["tailored_resume_path"])
        try:
            docx_path = convert_to_docx(text_path)
            pdf_path = convert_to_pdf(text_path)
            pdf_path.chmod(0o600)
            _record_export_artifacts(job.get("tailor_report_path"), docx_path, pdf_path)
            conn.execute(
                "UPDATE jobs SET tailored_docx_path = ?, tailored_pdf_path = ?, updated_at = ? WHERE url = ?",
                (
                    str(docx_path),
                    str(pdf_path),
                    datetime.now(timezone.utc).isoformat(),
                    job["url"],
                ),
            )
            conn.commit()
            converted += 1
            results.append(
                {"url": job["url"], "docx_path": str(docx_path), "pdf_path": str(pdf_path)}
            )
        except Exception as exc:
            errors += 1
            results.append({"url": job["url"], "error": str(exc)})
            log.error("Failed to export %s: %s", text_path.name, exc)

    log.info("Done: %d/%d resume artifact sets generated in %s", converted, len(jobs), TAILORED_DIR)
    return {"converted": converted, "errors": errors, "results": results}
