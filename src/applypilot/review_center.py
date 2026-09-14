"""Local-only application Review Center with durable human decisions."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from applypilot import config
from applypilot.database import (
    get_application_record,
    get_connection,
    list_application_reviews,
    record_application_review,
)


@dataclass(frozen=True)
class ReviewAction:
    """Action returned to the CLI after the local Review Center closes."""

    kind: str
    application_id: str
    material_fingerprint: str


def material_fingerprint(application: dict) -> str:
    """Fingerprint exactly the local inputs covered by the material review."""
    digest = hashlib.sha256()
    digest.update(application.get("id", "").encode())
    digest.update(json.dumps(application.get("form_answers") or {}, sort_keys=True).encode())
    for raw_path in (
        application.get("resume_path"),
        application.get("cover_letter_path"),
        config.PROFILE_PATH,
    ):
        path = Path(raw_path).expanduser() if raw_path else None
        digest.update(str(path or "").encode())
        if not path or not path.is_file():
            digest.update(b"missing")
            continue
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _load_applications(selected_id: str | None = None) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT a.*, j.fit_score, j.location, j.salary, j.score_reasoning,
               COALESCE(js.content, j.full_description, j.description, '') AS jd_content
        FROM applications AS a
        JOIN jobs AS j ON j.url = a.job_url
        LEFT JOIN jd_snapshots AS js ON js.id = a.jd_snapshot_id
        ORDER BY
          CASE
            WHEN a.id = ? THEN -1
            WHEN a.status = 'ready_for_review' THEN 0
            WHEN a.status = 'changes_requested' THEN 1
            WHEN a.status = 'review_approved' THEN 2
            ELSE 3
          END,
          a.updated_at DESC
        LIMIT 100
        """,
        (selected_id or "",),
    ).fetchall()
    applications: list[dict] = []
    for row in rows:
        item = dict(row)
        item["form_answers"] = json.loads(item.pop("form_answers_json") or "{}")
        reviews = list_application_reviews(item["id"], conn=conn)
        item["reviews"] = reviews
        item["latest_review"] = reviews[0] if reviews else None
        applications.append(item)
    return applications


def _status_label(status: str) -> str:
    return {
        "ready_for_review": "Ready for review",
        "review_approved": "Review approved",
        "changes_requested": "Changes requested",
        "withdrawn": "Rejected",
        "submitted": "Submitted",
        "failed": "Failed",
    }.get(status, status.replace("_", " ").title())


def _artifact_url(application_id: str, kind: str) -> str:
    return f"/artifact/{quote(application_id)}/{quote(kind)}"


def _artifact_exists(application: dict, kind: str) -> bool:
    raw = application.get(kind)
    return bool(raw and Path(raw).expanduser().is_file())


def _safe_external_url(raw_url: str | None) -> str:
    parsed = urlparse(raw_url or "")
    if parsed.scheme not in {"http", "https"}:
        return "#"
    return raw_url or "#"


def render_review_center(
    applications: list[dict],
    *,
    selected_id: str | None,
    csrf_token: str,
    nonce: str,
) -> str:
    """Render the complete offline-capable Review Center page."""
    selected = next((item for item in applications if item["id"] == selected_id), None)
    if selected is None and applications:
        selected = applications[0]

    pending = sum(item["status"] == "ready_for_review" for item in applications)
    queue_items = ""
    for item in applications:
        active = " active" if selected and item["id"] == selected["id"] else ""
        queue_items += f"""
        <a class="queue-item{active}" href="/?id={quote(item["id"])}">
          <span class="queue-company">{escape(item.get("company") or "Unknown company")}</span>
          <strong>{escape(item.get("job_title") or "Untitled role")}</strong>
          <span class="queue-meta"><span class="status status-{escape(item["status"])}">{escape(_status_label(item["status"]))}</span><time>{escape((item.get("updated_at") or "")[:10])}</time></span>
        </a>"""

    if not applications:
        queue_items = '<div class="empty-small">No application records yet.</div>'

    if selected is None:
        main = """
        <main class="empty-state">
          <div class="empty-mark">✓</div>
          <h2>Your review queue is clear</h2>
          <p>Run a dry-run application and it will appear here with its documents, answers, and browser evidence.</p>
          <code>applypilot apply --url JOB_URL --dry-run</code>
        </main>"""
    else:
        app_id = escape(selected["id"])
        status = selected.get("status") or "draft"
        answers = selected.get("form_answers") or {}
        answer_rows = (
            "".join(
                f"<tr><th>{escape(str(question))}</th><td>{escape(str(answer) or 'Not provided')}</td></tr>"
                for question, answer in answers.items()
            )
            or '<tr><td colspan="2" class="muted">No verified form answers were saved.</td></tr>'
        )

        artifact_buttons = ""
        for kind, label in (
            ("resume_path", "Resume"),
            ("cover_letter_path", "Cover letter"),
            ("review_snapshot_path", "Form screenshot"),
            ("agent_log_path", "Agent log"),
        ):
            if _artifact_exists(selected, kind):
                artifact_buttons += (
                    f'<a class="artifact-link" href="{_artifact_url(selected["id"], kind)}" '
                    f'target="_blank" rel="noopener">{label}<span>↗</span></a>'
                )

        screenshot = ""
        if _artifact_exists(selected, "review_snapshot_path"):
            screenshot = f"""
            <a class="screenshot" href="{_artifact_url(selected["id"], "review_snapshot_path")}" target="_blank" rel="noopener">
              <img src="{_artifact_url(selected["id"], "review_snapshot_path")}" alt="Verified browser form for {escape(selected.get("job_title") or "application")}">
              <span>Open full-size evidence ↗</span>
            </a>"""
        else:
            screenshot = '<div class="artifact-empty">No browser screenshot was saved.</div>'

        history = (
            "".join(
                f"""
            <li><span class="decision-dot decision-{escape(review["decision"])}"></span><div><strong>{escape(review["decision"].replace("_", " ").title())}</strong><time>{escape(review["created_at"][:19])}</time><p>{escape(review.get("notes") or "No note")}</p></div></li>"""
                for review in selected.get("reviews", [])
            )
            or '<li class="muted">No review decisions yet.</li>'
        )

        controls = ""
        if status in {"ready_for_review", "review_approved"}:
            controls = f"""
            <form class="decision-form" method="post" action="/decision/{app_id}">
              <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
              <label for="review-notes">Review notes <span>required when requesting changes</span></label>
              <textarea id="review-notes" name="notes" rows="3" maxlength="4000" placeholder="Describe an incorrect answer or requested document change..."></textarea>
              <div class="secondary-actions">
                <button type="submit" name="decision" value="changes_requested" class="button button-secondary">Request changes</button>
                <button type="submit" name="decision" value="rerun_requested" class="button button-secondary">Re-run dry-run</button>
                <button type="submit" name="decision" value="rejected" class="button button-danger">Reject application</button>
              </div>
              <button type="submit" name="decision" value="approved" class="button button-primary">
                {"Open final browser review again" if status == "review_approved" else "Approve &amp; open final browser review"} <span>→</span>
              </button>
              <p class="safety-note"><strong>This does not submit.</strong> The irreversible Submit action still requires a separate explicit confirmation in the terminal.</p>
            </form>"""
        elif status == "changes_requested":
            controls = f"""
            <form class="decision-form" method="post" action="/decision/{app_id}">
              <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
              <p class="notice">Changes were requested. Re-run the dry-run after updating the profile or materials.</p>
              <button type="submit" name="decision" value="rerun_requested" class="button button-primary">Re-run dry-run <span>→</span></button>
            </form>"""
        else:
            controls = f'<div class="locked-panel"><strong>{escape(_status_label(status))}</strong><p>This review is closed. Its evidence and audit history remain available.</p></div>'

        main = f"""
        <main>
          <header class="application-header">
            <div>
              <div class="eyebrow">{escape(selected.get("company") or "Unknown company")} · Fit {escape(str(selected.get("fit_score") or "—"))}/10</div>
              <h2>{escape(selected.get("job_title") or "Untitled role")}</h2>
              <p>{escape(selected.get("location") or "Location not listed")} {("· " + escape(selected.get("salary"))) if selected.get("salary") else ""}</p>
            </div>
            <div class="header-actions">
              <span class="status status-{escape(status)}">{escape(_status_label(status))}</span>
              <a href="{escape(_safe_external_url(selected.get("application_url") or selected.get("job_url")))}" target="_blank" rel="noopener">Open job ↗</a>
            </div>
          </header>

          <section class="workspace-grid">
            <div class="review-content">
              <section class="panel evidence-panel">
                <div class="section-heading"><div><span class="step">01</span><h3>Browser evidence</h3></div><span class="verified">Verified DOM state</span></div>
                {screenshot}
              </section>

              <section class="panel">
                <div class="section-heading"><div><span class="step">02</span><h3>Verified form answers</h3></div><span class="count">{len(answers)} fields</span></div>
                <div class="answer-table"><table><tbody>{answer_rows}</tbody></table></div>
              </section>

              <section class="panel">
                <div class="section-heading"><div><span class="step">03</span><h3>Materials &amp; source</h3></div></div>
                <div class="artifact-grid">{artifact_buttons or '<span class="muted">No artifacts saved.</span>'}</div>
                <details class="jd"><summary>Saved job description</summary><div>{escape(selected.get("jd_content") or "No job description saved.")}</div></details>
              </section>
            </div>

            <aside class="decision-column">
              <section class="panel decision-panel"><span class="step">04</span><h3>Human decision</h3>{controls}</section>
              <section class="panel audit-panel"><h3>Audit trail</h3><ul>{history}</ul><code>{app_id}</code></section>
            </aside>
          </section>
        </main>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>Review Center · OpenApplyPilot</title>
<style nonce="{nonce}">
:root {{ --ink:#14233b; --muted:#637083; --line:#dce2e8; --paper:#fbfaf7; --surface:#fff; --navy:#172842; --green:#1e7258; --green-soft:#e8f3ee; --amber:#9b5a12; --amber-soft:#fff3df; --red:#a63b37; --red-soft:#fae9e7; --shadow:0 16px 42px rgba(20,35,59,.08); }}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:var(--ink); background:var(--paper); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; line-height:1.45; }}
a {{ color:inherit; }}
.shell {{ min-height:100vh; display:grid; grid-template-columns:300px minmax(0,1fr); }}
.sidebar {{ background:var(--navy); color:#fff; padding:28px 18px; position:sticky; top:0; height:100vh; overflow:auto; }}
.brand {{ display:flex; gap:12px; align-items:center; padding:0 8px 24px; border-bottom:1px solid rgba(255,255,255,.13); }}
.brand-mark {{ display:grid; place-items:center; width:38px; height:38px; border-radius:10px; background:#d7f36b; color:var(--navy); font-weight:900; }}
.brand strong {{ display:block; letter-spacing:-.02em; }} .brand span {{ display:block; color:#aeb9c8; font-size:12px; }}
.queue-title {{ display:flex; align-items:center; justify-content:space-between; margin:24px 8px 12px; color:#bac5d3; font-size:12px; text-transform:uppercase; letter-spacing:.12em; }}
.queue-title b {{ background:#d7f36b; color:var(--navy); border-radius:999px; min-width:24px; padding:3px 7px; text-align:center; }}
.queue-item {{ display:block; text-decoration:none; padding:14px; margin:6px 0; border:1px solid transparent; border-radius:12px; color:#edf2f7; }}
.queue-item:hover,.queue-item.active {{ background:rgba(255,255,255,.09); border-color:rgba(255,255,255,.14); }}
.queue-company {{ display:block; color:#aeb9c8; font-size:12px; margin-bottom:4px; }} .queue-item strong {{ display:block; font-size:14px; }}
.queue-meta {{ margin-top:10px; display:flex; align-items:center; justify-content:space-between; gap:8px; }} .queue-meta time {{ color:#8f9caf; font-size:11px; }}
main {{ min-width:0; padding:34px clamp(24px,4vw,58px) 70px; }}
.application-header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-start; margin-bottom:28px; }}
.eyebrow {{ color:var(--green); font-size:12px; font-weight:750; text-transform:uppercase; letter-spacing:.1em; }}
h2 {{ margin:6px 0 5px; font-family:Georgia,serif; font-size:clamp(30px,4vw,48px); line-height:1.06; letter-spacing:-.035em; }}
.application-header p {{ margin:0; color:var(--muted); }} .header-actions {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; justify-content:flex-end; }}
.header-actions a {{ font-size:13px; font-weight:700; text-decoration:none; border-bottom:1px solid currentColor; }}
.status {{ display:inline-flex; align-items:center; width:max-content; border-radius:999px; padding:4px 9px; font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:.06em; background:#e9edf2; color:#526073; }}
.status-ready_for_review,.status-review_approved {{ background:var(--green-soft); color:var(--green); }} .status-changes_requested {{ background:var(--amber-soft); color:var(--amber); }} .status-withdrawn,.status-failed {{ background:var(--red-soft); color:var(--red); }}
.workspace-grid {{ display:grid; grid-template-columns:minmax(0,1.6fr) minmax(310px,.7fr); gap:22px; align-items:start; }}
.review-content {{ display:grid; gap:22px; }} .decision-column {{ display:grid; gap:22px; position:sticky; top:24px; }}
.panel {{ background:var(--surface); border:1px solid var(--line); border-radius:16px; padding:22px; box-shadow:var(--shadow); }}
.section-heading {{ display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:18px; }} .section-heading>div {{ display:flex; align-items:center; gap:10px; }}
h3 {{ margin:0; font-family:Georgia,serif; font-size:20px; letter-spacing:-.015em; }} .step {{ display:grid; place-items:center; width:28px; height:28px; border:1px solid var(--line); border-radius:50%; color:var(--muted); font:700 10px ui-monospace,monospace; }}
.verified {{ color:var(--green); background:var(--green-soft); border-radius:6px; padding:5px 8px; font-size:11px; font-weight:800; }} .count,.muted {{ color:var(--muted); font-size:12px; }}
.screenshot {{ display:block; text-decoration:none; border:1px solid var(--line); border-radius:11px; overflow:hidden; background:#f1f3f5; }} .screenshot img {{ display:block; width:100%; max-height:520px; object-fit:contain; object-position:top; }} .screenshot span {{ display:block; padding:10px 14px; background:#fff; border-top:1px solid var(--line); color:var(--green); font-size:12px; font-weight:800; }}
.artifact-empty,.empty-small {{ padding:22px; border:1px dashed var(--line); border-radius:10px; color:var(--muted); font-size:13px; }}
.answer-table {{ border:1px solid var(--line); border-radius:10px; overflow:hidden; }} table {{ width:100%; border-collapse:collapse; font-size:13px; }} th,td {{ padding:11px 13px; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); }} tr:last-child th,tr:last-child td {{ border-bottom:0; }} th {{ width:52%; color:#47566a; font-weight:650; background:#f8f9fa; }} td {{ font-weight:700; overflow-wrap:anywhere; }}
.artifact-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }} .artifact-link {{ display:flex; justify-content:space-between; text-decoration:none; padding:12px 13px; border:1px solid var(--line); border-radius:9px; font-size:13px; font-weight:750; }} .artifact-link:hover {{ border-color:var(--green); color:var(--green); }}
.jd {{ margin-top:14px; border-top:1px solid var(--line); padding-top:14px; }} .jd summary {{ cursor:pointer; font-size:13px; font-weight:750; }} .jd div {{ white-space:pre-wrap; max-height:500px; overflow:auto; margin-top:12px; color:#425067; font-size:13px; }}
.decision-panel>.step {{ margin-bottom:12px; }} .decision-panel>h3 {{ margin-bottom:18px; }} .decision-form label {{ display:block; font-size:12px; font-weight:800; margin-bottom:7px; }} .decision-form label span {{ display:block; color:var(--muted); font-weight:500; }} textarea {{ display:block; width:100%; resize:vertical; border:1px solid #cbd3dc; border-radius:9px; padding:10px 11px; font:inherit; font-size:13px; }} textarea:focus {{ outline:3px solid rgba(30,114,88,.14); border-color:var(--green); }}
.secondary-actions {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:11px 0; }} .button {{ appearance:none; border:0; border-radius:9px; padding:11px 12px; cursor:pointer; font:inherit; font-size:12px; font-weight:750; text-align:center; }} .button-primary {{ display:flex; width:100%; justify-content:space-between; background:var(--green); color:#fff; padding:14px; }} .button-primary:hover {{ background:#165e48; }} .button-secondary {{ background:#eef1f4; color:var(--ink); }} .button-danger {{ background:var(--red-soft); color:var(--red); }}
.safety-note {{ color:var(--muted); font-size:11px; margin:12px 0 0; }} .safety-note strong {{ color:var(--ink); }} .notice,.locked-panel {{ padding:13px; border-radius:9px; background:var(--amber-soft); color:#71420d; font-size:13px; }} .locked-panel p {{ margin:5px 0 0; }}
.audit-panel ul {{ list-style:none; margin:16px 0; padding:0; display:grid; gap:14px; }} .audit-panel li {{ display:grid; grid-template-columns:10px 1fr; gap:9px; font-size:12px; }} .audit-panel li strong {{ display:inline-block; margin-right:8px; }} .audit-panel li time {{ color:var(--muted); font-size:10px; }} .audit-panel li p {{ margin:3px 0 0; color:var(--muted); }} .decision-dot {{ width:8px; height:8px; margin-top:5px; border-radius:50%; background:#aab3bf; }} .decision-approved {{ background:var(--green); }} .decision-rejected {{ background:var(--red); }} .decision-changes_requested {{ background:var(--amber); }} .audit-panel code {{ display:block; overflow-wrap:anywhere; color:var(--muted); font-size:10px; }}
.empty-state {{ min-height:100vh; display:grid; place-content:center; justify-items:center; text-align:center; }} .empty-mark {{ display:grid; place-items:center; width:56px; height:56px; border-radius:50%; background:var(--green-soft); color:var(--green); font-size:24px; }} .empty-state p {{ max-width:520px; color:var(--muted); }} .empty-state code {{ padding:10px 13px; background:#fff; border:1px solid var(--line); border-radius:8px; }}
@media(max-width:1000px) {{ .shell {{ grid-template-columns:240px minmax(0,1fr); }} .workspace-grid {{ grid-template-columns:1fr; }} .decision-column {{ position:static; }} }}
@media(max-width:720px) {{ .shell {{ display:block; }} .sidebar {{ position:static; height:auto; max-height:320px; }} main {{ padding:24px 16px 50px; }} .application-header {{ display:block; }} .header-actions {{ justify-content:flex-start; margin-top:16px; }} .artifact-grid,.secondary-actions {{ grid-template-columns:1fr; }} th,td {{ display:block; width:100%; }} th {{ border-bottom:0; padding-bottom:3px; }} td {{ padding-top:3px; }} }}
</style>
</head>
<body>
<div class="shell">
  <aside class="sidebar"><div class="brand"><div class="brand-mark">AP</div><div><strong>OpenApplyPilot</strong><span>Human review center</span></div></div><div class="queue-title"><span>Application queue</span><b>{pending}</b></div>{queue_items}</aside>
  {main}
</div>
<script nonce="{nonce}">
document.querySelectorAll('form').forEach((form) => form.addEventListener('submit', (event) => {{
  const clicked = event.submitter && event.submitter.value;
  const notes = form.querySelector('textarea');
  if (clicked === 'changes_requested' && notes && !notes.value.trim()) {{ event.preventDefault(); notes.setCustomValidity('Describe the changes you need.'); notes.reportValidity(); }}
  if (notes) notes.addEventListener('input', () => notes.setCustomValidity(''), {{once:true}});
  if (clicked === 'rejected' && !window.confirm('Reject this application? It will be closed without submitting.')) event.preventDefault();
}}));
</script>
</body>
</html>"""


def render_transition(message: str, *, nonce: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Review saved</title><style nonce="{nonce}">body{{margin:0;display:grid;place-content:center;min-height:100vh;background:#fbfaf7;color:#14233b;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:560px;padding:36px;background:#fff;border:1px solid #dce2e8;border-radius:16px;box-shadow:0 16px 42px rgba(20,35,59,.08)}}div{{display:grid;place-items:center;width:48px;height:48px;border-radius:50%;background:#e8f3ee;color:#1e7258;font-size:24px}}h1{{font-family:Georgia,serif}}p{{color:#637083;line-height:1.6}}</style></head><body><main><div>✓</div><h1>Review saved</h1><p>{escape(message)}</p><p>You can close this tab and return to the terminal.</p></main></body></html>"""


class _ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, *, csrf_token: str, nonce: str):
        super().__init__(address, handler)
        self.csrf_token = csrf_token
        self.nonce = nonce
        self.action: ReviewAction | None = None
        self.action_lock = threading.Lock()
        port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def request_shutdown(self) -> None:
        threading.Thread(target=self._delayed_shutdown, daemon=True).start()

    def _delayed_shutdown(self) -> None:
        time.sleep(0.15)
        self.shutdown()


class ReviewCenterHandler(BaseHTTPRequestHandler):
    server: _ReviewServer

    def log_message(self, _format: str, *_args) -> None:
        return

    def _trusted_host(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'none'; img-src 'self' data:; style-src 'nonce-{self.server.nonce}'; script-src 'nonce-{self.server.nonce}'; frame-src 'self'; form-action 'self'; base-uri 'none'",
        )
        self.end_headers()

    def _send_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = html.encode("utf-8")
        self._headers(status, "text/html; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_html(render_transition(message, nonce=self.server.nonce), status)

    def do_GET(self) -> None:
        if not self._trusted_host():
            self._error(HTTPStatus.BAD_REQUEST, "Untrusted host header.")
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            selected_id = parse_qs(parsed.query).get("id", [None])[0]
            applications = _load_applications(selected_id)
            if selected_id and not any(item["id"] == selected_id for item in applications):
                self._error(HTTPStatus.NOT_FOUND, "Application not found.")
                return
            self._send_html(
                render_review_center(
                    applications,
                    selected_id=selected_id,
                    csrf_token=self.server.csrf_token,
                    nonce=self.server.nonce,
                )
            )
            return
        if parsed.path.startswith("/artifact/"):
            self._serve_artifact(parsed.path)
            return
        self._error(HTTPStatus.NOT_FOUND, "Page not found.")

    def _serve_artifact(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            self._error(HTTPStatus.NOT_FOUND, "Artifact not found.")
            return
        _, application_id, kind = parts
        allowed = {
            "resume_path",
            "cover_letter_path",
            "review_snapshot_path",
            "submission_snapshot_path",
            "agent_log_path",
        }
        if kind not in allowed:
            self._error(HTTPStatus.NOT_FOUND, "Artifact not found.")
            return
        try:
            application = get_application_record(application_id)
        except KeyError:
            self._error(HTTPStatus.NOT_FOUND, "Application not found.")
            return
        raw_path = application.get(kind)
        artifact = Path(raw_path).expanduser().resolve() if raw_path else None
        if not artifact or not artifact.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Artifact not found.")
            return
        payload = artifact.read_bytes()
        content_type = mimetypes.guess_type(artifact.name)[0] or "application/octet-stream"
        self._headers(HTTPStatus.OK, content_type, len(payload))
        self.wfile.write(payload)

    def do_POST(self) -> None:
        if not self._trusted_host():
            self._error(HTTPStatus.BAD_REQUEST, "Untrusted host header.")
            return
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "decision":
            self._error(HTTPStatus.NOT_FOUND, "Page not found.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Invalid request length.")
            return
        if length <= 0 or length > 16_384:
            self._error(HTTPStatus.BAD_REQUEST, "Invalid request body.")
            return
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        if form.get("csrf_token", [""])[0] != self.server.csrf_token:
            self._error(HTTPStatus.FORBIDDEN, "The review session token is invalid. Refresh and try again.")
            return

        application_id = parts[1]
        decision = form.get("decision", [""])[0]
        notes = form.get("notes", [""])[0].strip()
        if decision == "changes_requested" and not notes:
            self._error(HTTPStatus.BAD_REQUEST, "Review notes are required when requesting changes.")
            return
        try:
            application = get_application_record(application_id)
            fingerprint = material_fingerprint(application)
            record_application_review(
                application_id,
                decision,
                notes=notes,
                material_fingerprint=fingerprint,
            )
        except (KeyError, ValueError) as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
            return

        if decision in {"approved", "rerun_requested"}:
            kind = "final_review" if decision == "approved" else "rerun_dry_run"
            with self.server.action_lock:
                self.server.action = ReviewAction(kind, application_id, fingerprint)
            message = (
                "The approved materials will now be prepared again in Chrome for the final browser review."
                if decision == "approved"
                else "The application will now run through a fresh non-submitting browser dry-run."
            )
            self._send_html(render_transition(message, nonce=self.server.nonce))
            self.server.request_shutdown()
            return

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/?id={quote(application_id)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def serve_review_center(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    selected_id: str | None = None,
    open_browser: bool = True,
    on_ready: Callable[[str], None] | None = None,
) -> ReviewAction | None:
    """Serve the Review Center until Ctrl+C or an executable review action."""
    if host != "127.0.0.1":
        raise ValueError("Review Center only supports the local 127.0.0.1 interface")
    if selected_id:
        applications = _load_applications(selected_id)
        if not any(item["id"] == selected_id for item in applications):
            raise KeyError(f"Application not found: {selected_id}")
    server = _ReviewServer(
        (host, port),
        ReviewCenterHandler,
        csrf_token=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(18),
    )
    actual_port = server.server_address[1]
    suffix = f"?id={quote(selected_id)}" if selected_id else ""
    url = f"http://{host}:{actual_port}/{suffix}"
    if on_ready:
        on_ready(url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return server.action
