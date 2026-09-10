"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    _kill_process_tree,
    cleanup_on_exit,
    cleanup_worker,
    kill_all_chrome,
    launch_chrome,
    reset_worker_dir,
)
from applypilot.apply.dashboard import (
    add_event,
    get_state,
    get_totals,
    init_worker,
    render_full,
    update_state,
)
from applypilot.database import (
    create_application_record,
    get_application_record,
    get_connection,
    update_application_record,
)

logger = logging.getLogger(__name__)

ApprovalCallback = Callable[[Mapping[str, object]], bool]


def _job_company(job: Mapping[str, object]) -> str:
    """Return employer name, falling back to the discovery source."""
    return str(job.get("company") or job.get("site") or "")


# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites

    return load_blocked_sites()


# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------


def _make_mcp_config(cdp_port: int, *, include_gmail: bool = True) -> dict:
    """Build MCP config dict for a specific CDP port."""
    servers = {
        "playwright": {
            "command": "npx",
            "args": [
                "@playwright/mcp@latest",
                f"--cdp-endpoint=http://localhost:{cdp_port}",
                f"--viewport-size={config.DEFAULTS['viewport']}",
            ],
        }
    }
    if include_gmail:
        servers["gmail"] = {
            "command": "npx",
            "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
        }
    return {"mcpServers": servers}


def _build_claude_command(*, model: str, mcp_config_path: Path, phase: str) -> list[str]:
    """Return a least-privilege Claude Code command for one browser phase."""
    allowed_tools = ["mcp__playwright__*"]
    if phase == "prepare":
        allowed_tools.extend(["mcp__gmail__search_emails", "mcp__gmail__read_email"])
    return [
        "claude",
        "--model",
        model,
        "-p",
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--restricted",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ",".join(allowed_tools),
        "--disallowedTools",
        "mcp__gmail__send_email",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "-",
    ]


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


def acquire_job(target_url: str | None = None, min_score: int = 7, worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute(
                """
                SELECT url, title, company, site, application_url, tailored_resume_path,
                       tailored_docx_path, tailored_pdf_path, tailor_report_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND tailored_resume_path IS NOT NULL
                  AND COALESCE(apply_status, '') != 'in_progress'
                LIMIT 1
            """,
                (target_url, target_url, like, like),
            ).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = [min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            row = conn.execute(
                f"""
                SELECT url, title, company, site, application_url, tailored_resume_path,
                       tailored_docx_path, tailored_pdf_path, tailor_report_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND fit_score >= ?
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, url
                LIMIT 1
            """,
                [config.DEFAULTS["max_apply_attempts"]] + params,
            ).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats

        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """,
            (f"worker-{worker_id}", now, row["url"]),
        )
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def reacquire_application(application_id: str, worker_id: int = 0) -> dict:
    """Reclaim the job belonging to an interrupted application workflow."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT j.url, j.title, j.company, j.site, j.application_url,
                   j.tailored_resume_path, j.tailored_docx_path,
                   j.tailored_pdf_path, j.tailor_report_path, j.fit_score,
                   j.location, j.full_description, j.cover_letter_path,
                   j.apply_status, j.last_attempted_at, a.status AS application_status
            FROM applications AS a
            JOIN jobs AS j ON j.url = a.job_url
            WHERE a.id = ?
            """,
            (application_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Application not found: {application_id}")
        if row["application_status"] in {"submitted", "withdrawn"}:
            raise ValueError(f"Application {application_id} is already {row['application_status']}")
        if row["apply_status"] == "applied":
            raise ValueError("The job is already marked as applied")

        # A live worker may still own the browser. Only reclaim locks older
        # than twice the agent timeout (10 minutes).
        if row["apply_status"] == "in_progress" and row["last_attempted_at"]:
            attempted = datetime.fromisoformat(row["last_attempted_at"])
            if attempted.tzinfo is None:
                attempted = attempted.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - attempted).total_seconds()
            if age_seconds < 600:
                raise RuntimeError(
                    "This application is still owned by a recent worker; wait before attempting recovery."
                )

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE jobs
            SET apply_status = 'in_progress', agent_id = ?, last_attempted_at = ?
            WHERE url = ?
            """,
            (f"worker-{worker_id}", now, row["url"]),
        )
        conn.commit()
        job = dict(row)
        job.pop("apply_status", None)
        job.pop("last_attempted_at", None)
        job.pop("application_status", None)
        return job
    except Exception:
        conn.rollback()
        raise


def mark_result(
    url: str,
    status: str,
    error: str | None = None,
    permanent: bool = False,
    duration_ms: int | None = None,
    task_id: str | None = None,
) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute(
            """
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           verification_confidence = 'confirmation_screenshot'
            WHERE url = ?
        """,
            (now, duration_ms, task_id, url),
        )
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(
            f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """,
            (status, error or "unknown", duration_ms, task_id, url),
        )
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------


def gen_prompt(target_url: str, min_score: int = 7, model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    review_path = config.APPLICATION_REVIEW_DIR / "generated_prompt_review.png"
    prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=True,
        review_snapshot_path=str(review_path),
    )

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute(
            """
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """,
            (now, url),
        )
    else:
        conn.execute(
            """
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """,
            (reason or "manual", url),
        )
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------


def _archive_snapshot(source: Path, destination: str | None) -> bool:
    """Copy a worker screenshot into durable application evidence storage."""
    if not destination or not source.exists():
        return False
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(0o600)
    return True


def _validate_prepared_form(port: int) -> tuple[bool, list[str]]:
    """Read-only DOM check for empty required fields and a missing resume."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            pages = [page for context in browser.contexts for page in context.pages]
            if not pages:
                return False, ["application_page"]
            page = pages[-1]
            result = page.evaluate(
                """() => {
                    const visible = (el) => Boolean(
                      el.offsetWidth || el.offsetHeight || el.getClientRects().length
                    );
                    const labelFor = (el) => {
                      if (el.labels && el.labels.length) return el.labels[0].innerText.trim();
                      const container = el.closest('label, [class*="field"], [class*="Field"]');
                      const label = container && container.querySelector('label');
                      return label ? label.innerText.trim() : (el.getAttribute('aria-label') || el.name || el.type);
                    };
                    const missing = [];
                    for (const el of document.querySelectorAll('input, textarea, select')) {
                      if (el.disabled || el.type === 'hidden' || el.type === 'file' || !visible(el)) continue;
                      const label = labelFor(el);
                      const required = el.required || el.getAttribute('aria-required') === 'true' || label.includes('*');
                      if (!required) continue;
                      if ((el.type === 'checkbox' || el.type === 'radio')) {
                        const group = el.name
                          ? Array.from(document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`))
                          : [el];
                        if (!group.some((item) => item.checked)) missing.push(label);
                      } else if (!String(el.value || '').trim()) {
                        missing.push(label);
                      }
                    }
                    const pageText = document.body.innerText || '';
                    const resumeRequired = /Resume\\s*\\*/i.test(pageText);
                    const resumeUploaded = Array.from(document.querySelectorAll('input[type="file"]'))
                      .some((el) => el.files && el.files.length > 0);
                    if (resumeRequired && !resumeUploaded) missing.push('Resume');
                    return Array.from(new Set(missing));
                }"""
            )
            missing = [str(label)[:100] for label in result] if isinstance(result, list) else ["form_validation"]
            return not missing, missing
    except Exception:
        logger.exception("Could not validate prepared application form over CDP")
        return False, ["form_validation_unavailable"]


def run_job(
    job: dict,
    port: int,
    worker_id: int = 0,
    model: str = "sonnet",
    dry_run: bool = False,
    application_id: str | None = None,
    phase: str = "prepare",
) -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'ready_for_review', 'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    if phase not in {"prepare", "submit"}:
        raise ValueError(f"Unsupported apply phase: {phase}")

    # Claude runs in restricted mode and Playwright file operations are scoped
    # to this directory. Put upload inputs and screenshot outputs here first,
    # then archive evidence to the application review directory.
    worker_dir = reset_worker_dir(worker_id)
    snapshot_name = application_id or f"worker-{worker_id}"
    review_snapshot_path = None
    submission_snapshot_path = None
    agent_snapshot_path = worker_dir / f"{snapshot_name}.png"
    if phase == "prepare":
        review_snapshot_path = str(config.APPLICATION_REVIEW_DIR / f"{snapshot_name}.png")
        # The preparation phase is always non-submitting. The final action is
        # delegated to a separate, human-approved submit phase.
        agent_prompt = prompt_mod.build_prompt(
            job=job,
            tailored_resume=resume_text,
            dry_run=True,
            review_snapshot_path=str(agent_snapshot_path),
            upload_dir=worker_dir,
        )
    else:
        submission_snapshot_path = str(config.APPLICATION_REVIEW_DIR / f"{snapshot_name}_submitted.png")
        agent_snapshot_path = worker_dir / f"{snapshot_name}_submitted.png"
        agent_prompt = prompt_mod.build_submit_prompt(
            job,
            str(agent_snapshot_path),
        )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(
        json.dumps(_make_mcp_config(port, include_gmail=phase == "prepare")),
        encoding="utf-8",
    )

    # Restricted mode removes shell/code tools and ignores ambient project or
    # user customizations. Only the explicitly listed MCP tools remain usable.
    cmd = _build_claude_command(
        model=model,
        mcp_config_path=mcp_config_path,
        phase=phase,
    )

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    update_state(
        worker_id,
        status="applying",
        job_title=job["title"],
        company=_job_company(job),
        score=job.get("fit_score", 0),
        start_time=time.time(),
        actions=0,
        last_action="starting",
    )
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {_job_company(job)}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {_job_company(job)}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id, actions=cur_actions + 1, last_action=desc[:35])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=300)
        returncode = proc.returncode
        proc = None

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000)

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")
        if application_id:
            update_application_record(
                application_id,
                None,
                agent_log_path=str(job_log),
            )

        final_snapshot_path = review_snapshot_path or submission_snapshot_path
        _archive_snapshot(agent_snapshot_path, final_snapshot_path)

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', "", s).strip()

        if phase == "prepare" and "RESULT:READY_FOR_REVIEW" in output:
            form_answers = _extract_form_answers(output)
            if application_id and form_answers is not None:
                update_application_record(
                    application_id,
                    None,
                    form_answers=form_answers,
                )
            if _resume_upload_failed(form_answers):
                add_event(f"[W{worker_id}] FAILED ({elapsed}s): resume_not_uploaded")
                update_state(worker_id, status="failed", last_action="FAILED: resume_not_uploaded")
                return "failed:resume_not_uploaded", duration_ms
            valid_form, missing_fields = _validate_prepared_form(port)
            if not valid_form:
                reason = "required_fields_missing:" + ",".join(missing_fields[:5])
                add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:50]}")
                update_state(worker_id, status="failed", last_action=f"FAILED: {reason[:25]}")
                return f"failed:{reason}", duration_ms
            add_event(f"[W{worker_id}] READY FOR REVIEW ({elapsed}s): {job['title'][:30]}")
            update_state(worker_id, status="review", last_action=f"REVIEW ({elapsed}s)")
            return "ready_for_review", duration_ms

        # A dry run must never be recorded as applied, even if the agent ignores
        # its prompt and emits the wrong result code.
        if phase == "prepare" and "RESULT:APPLIED" in output:
            add_event(f"[W{worker_id}] UNSAFE DRY-RUN RESULT ({elapsed}s)")
            update_state(worker_id, status="failed", last_action="unsafe dry-run result")
            return "failed:dry_run_reported_applied", duration_ms

        for result_status in ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
            if f"RESULT:{result_status}" in output:
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(), last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms

        if "RESULT:FAILED" in output:
            for out_line in output.split("\n"):
                if "RESULT:FAILED" in out_line:
                    reason = (
                        out_line.split("RESULT:FAILED:")[-1].strip()
                        if ":" in out_line[out_line.index("FAILED") + 6 :]
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason, last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed", last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms
            return "failed:unknown", duration_ms

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms
    finally:
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


def _extract_form_answers(output: str) -> dict | None:
    """Parse the agent's one-line, non-sensitive form answer record."""
    marker = "FORM_ANSWERS_JSON:"
    offset = 0
    while True:
        marker_index = output.find(marker, offset)
        if marker_index < 0:
            return None
        raw = output[marker_index + len(marker):].lstrip(" \t\r\n`")
        if raw.startswith("json"):
            raw = raw[4:].lstrip(" \t\r\n")
        object_index = raw.find("{")
        if object_index < 0:
            offset = marker_index + len(marker)
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(raw[object_index:])
        except json.JSONDecodeError:
            offset = marker_index + len(marker)
            continue
        if isinstance(value, dict):
            return value
        offset = marker_index + len(marker)


def _resume_upload_failed(form_answers: dict | None) -> bool:
    """Fail closed when the agent explicitly reports a missing resume."""
    if not form_answers:
        return False
    for field, answer in form_answers.items():
        if "resume" not in str(field).casefold():
            continue
        normalized = str(answer).casefold()
        if any(term in normalized for term in ("not uploaded", "missing", "failed", "limitation")):
            return True
    return False


def _terminal_approval(payload: Mapping[str, object]) -> bool:
    """Render one approval request and require an explicit yes."""
    console = Console()
    kind = str(payload.get("kind", "approval"))
    console.print()
    console.print(f"[bold cyan]{kind.replace('_', ' ').title()}[/bold cyan]")
    console.print(f"  Application ID: {payload.get('application_id', '')}")
    console.print(f"  Job: {payload.get('job_title', '')}")
    console.print(f"  Company: {payload.get('company', '')}")
    for key in (
        "resume_path",
        "cover_letter_path",
        "tailor_report_path",
        "review_snapshot_path",
    ):
        if payload.get(key):
            console.print(f"  {key.replace('_', ' ').title()}: {payload[key]}")
    answer = console.input(f"[bold]{payload.get('question', 'Approve?')}[/bold] [y/N]: ")
    return answer.strip().lower() in {"y", "yes"}


def _persist_workflow_result(application_id: str, state: Mapping[str, object]) -> None:
    """Copy durable graph state into the user-facing application history."""
    status = str(state.get("status", "draft"))
    if status == "browser_session_expired":
        status = "ready_for_review"
    if status not in {
        "draft",
        "materials_approved",
        "ready_for_review",
        "approved",
        "submitted",
        "failed",
        "withdrawn",
    }:
        status = "failed"

    review_path = str(state.get("review_snapshot_path", "")) or None
    submission_path = str(state.get("submission_snapshot_path", "")) or None
    update_application_record(
        application_id,
        status,
        review_snapshot_path=review_path if review_path and Path(review_path).exists() else None,
        submission_snapshot_path=(submission_path if submission_path and Path(submission_path).exists() else None),
        last_error=str(state.get("last_error", "")),
        verification_confidence=(str(state.get("verification_confidence", "")) or None),
    )


def run_safe_workflow(
    job: dict,
    *,
    port: int,
    worker_id: int,
    model: str,
    application_id: str,
    allow_submission: bool,
    approval_callback: ApprovalCallback | None = None,
    resume: bool = False,
) -> tuple[str, int]:
    """Run one application through both durable human approval gates."""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.types import Command
    except ImportError as exc:
        raise RuntimeError(
            "Auto Apply dependencies are missing. Install with `pip install 'applypilot[auto-apply]'`."
        ) from exc

    from applypilot.apply.workflow import (
        build_safe_apply_graph,
        interrupt_payload,
        snapshot_interrupt_payload,
    )

    approve = approval_callback or _terminal_approval
    browser_session_id = str(uuid.uuid4())
    review_path = config.APPLICATION_REVIEW_DIR / f"{application_id}.png"
    submission_path = config.APPLICATION_REVIEW_DIR / f"{application_id}_submitted.png"

    def prepare_form(_state: Mapping[str, object]) -> Mapping[str, object]:
        result, duration_ms = run_job(
            job,
            port=port,
            worker_id=worker_id,
            model=model,
            application_id=application_id,
            phase="prepare",
        )
        if result == "ready_for_review" and not review_path.exists():
            result = "failed:missing_review_evidence"
        return {
            "agent_result": result,
            "duration_ms": duration_ms,
            "review_snapshot_path": str(review_path),
        }

    def submit_form(_state: Mapping[str, object]) -> Mapping[str, object]:
        result, duration_ms = run_job(
            job,
            port=port,
            worker_id=worker_id,
            model=model,
            application_id=application_id,
            phase="submit",
        )
        evidence_exists = submission_path.exists()
        if result == "applied" and not evidence_exists:
            result = "failed:missing_submission_evidence"
        return {
            "agent_result": result,
            "duration_ms": duration_ms,
            "submission_snapshot_path": str(submission_path),
            "verification_confidence": ("confirmation_screenshot" if result == "applied" else "unverified"),
        }

    initial_state = {
        "application_id": application_id,
        "job_url": job["url"],
        "job_title": job.get("title", ""),
        "company": _job_company(job),
        "resume_path": (job.get("tailored_pdf_path") or job.get("tailored_resume_path", "")),
        "cover_letter_path": job.get("cover_letter_path", "") or "",
        "tailor_report_path": job.get("tailor_report_path", "") or "",
        "allow_submission": allow_submission,
        "status": "draft",
    }
    thread_id = application_id
    if resume:
        application = get_application_record(application_id)
        thread_id = application.get("workflow_thread_id") or application_id
    graph_config = {"configurable": {"thread_id": thread_id}}

    with SqliteSaver.from_conn_string(str(config.APPLY_CHECKPOINT_DB_PATH)) as saver:
        graph = build_safe_apply_graph(
            checkpointer=saver,
            prepare_form=prepare_form,
            submit_form=submit_form,
            browser_session_id=browser_session_id,
        )

        def start_fresh_browser_attempt():
            """Start a new checkpoint thread when no live browser can be resumed."""
            nonlocal graph_config, thread_id
            thread_id = f"{application_id}:resume:{uuid.uuid4()}"
            graph_config = {"configurable": {"thread_id": thread_id}}
            update_application_record(
                application_id,
                None,
                workflow_thread_id=thread_id,
            )
            restarted_state = graph.invoke(initial_state, config=graph_config)
            return restarted_state, interrupt_payload(restarted_state)

        if resume:
            snapshot = graph.get_state(graph_config)
            if not snapshot.values:
                raise RuntimeError(f"No LangGraph checkpoint found for application {application_id}")
            state = dict(snapshot.values)
            payload = snapshot_interrupt_payload(snapshot)
            if not payload:
                status = state.get("status")
                safe_to_restart = status in {
                    "draft",
                    "materials_approved",
                    "failed",
                    "browser_session_expired",
                    "ready_for_review",
                } and not state.get("final_approved")
                if not safe_to_restart:
                    raise RuntimeError(
                        "The saved workflow may have reached submission; refusing automatic recovery."
                    )

                # Completed dry runs and browser preparation interrupted after
                # material approval have no resumable browser state. Start a
                # new durable attempt under the same application record. This
                # requires a fresh browser preparation and approval gates.
                state, payload = start_fresh_browser_attempt()
        else:
            state = graph.invoke(initial_state, config=graph_config)
            payload = interrupt_payload(state)

        while payload:
            kind = payload.get("kind")
            if kind == "final_submission_approval" and not allow_submission:
                # `--resume --dry-run` can only reject a pending final action.
                state = graph.invoke(Command(resume=False), config=graph_config)
                _persist_workflow_result(application_id, state)
                return "ready_for_review", int(state.get("duration_ms", 0))
            if kind == "final_submission_approval" and (state.get("prepared_browser_session_id") != browser_session_id):
                # Never reuse an approval or reviewed form from a dead browser.
                # Resume only far enough for the graph's session guard to force
                # a fresh preparation and a new final approval checkpoint.
                state = graph.invoke(Command(resume=True), config=graph_config)
                _persist_workflow_result(application_id, state)
                payload = interrupt_payload(state)
                continue

            if kind not in {"material_approval", "final_submission_approval"}:
                raise RuntimeError("Unexpected Auto Apply approval checkpoint")

            approved = approve(payload)
            if approved and kind == "material_approval":
                update_application_record(application_id, "materials_approved")
            elif approved and kind == "final_submission_approval":
                update_application_record(application_id, "approved")

            state = graph.invoke(Command(resume=approved), config=graph_config)
            _persist_workflow_result(application_id, state)
            if not approved:
                result = "withdrawn" if kind == "material_approval" else "ready_for_review"
                return result, int(state.get("duration_ms", 0))
            payload = interrupt_payload(state)

        return str(state.get("agent_result", "failed:no_result")), int(state.get("duration_ms", 0))


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired",
    "captcha",
    "login_issue",
    "not_eligible_location",
    "not_eligible_salary",
    "already_applied",
    "account_required",
    "not_a_job_application",
    "unsafe_permissions",
    "unsafe_verification",
    "sso_required",
    "site_blocked",
    "cloudflare_blocked",
    "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


def worker_loop(
    worker_id: int = 0,
    limit: int = 1,
    target_url: str | None = None,
    min_score: int = 7,
    headless: bool = False,
    model: str = "sonnet",
    dry_run: bool = False,
    resume_application_id: str | None = None,
) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.
        resume_application_id: Continue a workflow paused at an approval gate.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="", last_action="waiting for job", actions=0)

        try:
            if resume_application_id:
                job = reacquire_application(resume_application_id, worker_id=worker_id)
            else:
                job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            logger.error("Could not acquire application: %s", exc)
            add_event(f"[W{worker_id}] Cannot resume: {str(exc)[:60]}")
            update_state(worker_id, status="failed", last_action=str(exc)[:35])
            failed += 1
            break
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle", last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        application_id: str | None = None
        try:
            application_id = resume_application_id or create_application_record(job["url"])
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms = run_safe_workflow(
                job,
                port=port,
                worker_id=worker_id,
                model=model,
                application_id=application_id,
                allow_submission=not dry_run,
                resume=bool(resume_application_id),
            )

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "withdrawn":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Materials not approved")
            elif result == "applied":
                mark_result(
                    job["url"],
                    "applied",
                    duration_ms=duration_ms,
                    task_id=application_id,
                )
                applied += 1
                update_state(worker_id, jobs_applied=applied, jobs_done=applied + failed)
            elif result == "ready_for_review":
                review_path = config.APPLICATION_REVIEW_DIR / f"{application_id}.png"
                update_application_record(
                    application_id,
                    "ready_for_review",
                    review_snapshot_path=str(review_path) if review_path.exists() else None,
                )
                release_lock(job["url"])
                ws = get_state(worker_id)
                reviewed = ws.jobs_reviewed if ws else 0
                update_state(worker_id, jobs_reviewed=reviewed + 1)
                add_event(f"[W{worker_id}] Saved review without submitting")
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                if dry_run:
                    # Preview failures belong to the application attempt history,
                    # not the production job queue or its retry budget.
                    release_lock(job["url"])
                else:
                    mark_result(
                        job["url"], "failed", reason, permanent=_is_permanent_failure(result), duration_ms=duration_ms
                    )
                update_application_record(
                    application_id,
                    "failed",
                    last_error=reason,
                )
                failed += 1
                update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            if application_id and not resume_application_id:
                update_application_record(
                    application_id,
                    "failed",
                    last_error=str(e)[:500],
                )
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url or resume_application_id:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------


def main(
    limit: int = 1,
    target_url: str | None = None,
    min_score: int = 7,
    headless: bool = False,
    model: str = "sonnet",
    dry_run: bool = False,
    continuous: bool = False,
    poll_interval: int = 60,
    workers: int = 1,
    resume_application_id: str | None = None,
) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
        resume_application_id: Continue a workflow paused at an approval gate.
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    resume_application_id=resume_application_id,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0) for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            resume_application_id=resume_application_id,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {totals['reviewed']} ready for review, "
            f"{total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
