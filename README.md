# OpenApplyPilot

Local-first job discovery, resume tailoring, and human-approved application automation.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/JunoWang/OpenApplyPilot?style=social)](https://github.com/JunoWang/OpenApplyPilot)

OpenApplyPilot is derived from [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot). It keeps the original six-stage product flow, then rebuilds the parts that need stronger local persistence, factual resume validation, recoverable browser state, and human review. This repository is not affiliated with applypilot.app, useapplypilot.com, or other commercial products using the ApplyPilot name.

> **Project status:** macOS-first beta. Stages 1-5 work as a repeatable local pipeline. Stage 6A supports one explicitly selected application at a time, with a deterministic Ashby adapter and mandatory human approval. Bulk or continuous submission is intentionally disabled.

**Start here:** [Install on macOS](#macos-setup), then follow the [local end-to-end walkthrough](#local-end-to-end-walkthrough). This is a supervised workflow, not a one-click unattended application service. A successful application completed manually or with an external browser assistant does not prove that the built-in Stage 6 works on that ATS.

## What works today

| Capability | Current status |
|---|---|
| Multi-source discovery | Indeed, LinkedIn, ZipRecruiter, Google Jobs, and Glassdoor through JobSpy, plus 48 Workday employers and 30 configured career sites. Individual sources can still block scraping. |
| Full JD storage | Job descriptions are versioned in local SQLite and remain available later for interview preparation. |
| Fit scoring | OpenAI, Anthropic, Gemini, Ollama, OpenAI-compatible endpoints, Claude Code CLI, and Codex CLI are supported with explicit provider/model selection. |
| Tailored resumes | Per-job generation with deterministic fact checks, project-identity checks, an LLM factuality judge, retries, and reviewable TXT/DOCX/PDF artifacts. |
| Cover letters | Per-job cover letters saved locally and linked to the job record. |
| Safe Auto Apply | Claude Code fills one selected form. LangGraph persists two approval gates and recovery state. Dry-run cannot submit. |
| ATS verification | Ashby forms receive deterministic repair and DOM-level verification after agent navigation. Unknown required facts fail closed. |
| Review Center | A loopback-only web UI shows the JD, documents, verified answers, screenshot, agent log, and append-only review history. |
| Application history | Submitted, failed, withdrawn, and review-ready applications are stored as form-like records in SQLite. |

## The six-stage pipeline

`applypilot run` executes stages 1-5 plus document export. Stage 6 is always a separate command so discovering jobs can never silently turn into submitting applications.

| Stage | Command | Output and gate |
|---|---|---|
| 1. Discover | `applypilot run discover` | Deduplicated job records from configured boards and employer sites. |
| 2. Enrich | `applypilot run enrich` | Full JD, application URL, and an immutable JD snapshot. |
| 3. Score | `applypilot run score` | A 1-10 fit score and written reasoning against the master resume. |
| 4. Tailor | `applypilot run tailor` | A source-grounded resume that must pass validation before it is accepted. |
| 5. Cover and export | `applypilot run cover pdf` | Cover letter plus private TXT, DOCX, PDF, JD snapshot, unified diff, and validation report. |
| 6. Review and apply | `applypilot apply --url URL` | Material approval, form preparation, browser evidence, final approval, then and only then Submit. |

The Stage 6 safety flow is:

```text
select one stored job
  -> approve resume and cover letter
  -> prepare form without submitting
  -> inspect Review Center and visible browser
  -> approve the irreversible Submit action
  -> submit and save evidence
```

A rejected, changed, expired, or interrupted application never skips back into Submit. OpenApplyPilot either stops or prepares the form again and asks for fresh approval.

## What we improved and why

| Change | Why it was needed | User-visible result |
|---|---|---|
| Local schema v5 and versioned JD snapshots | A job URL alone is not enough when the posting disappears before an interview. | Jobs, historical JDs, pipeline events, and applications remain searchable on the Mac. |
| Provider-native LLM layer | Implicit fallback hides cost, model changes, and the real source of failures. | OpenAI, Anthropic, Gemini, and Ollama are selected explicitly; a failed provider is recorded instead of silently switching. |
| Source-grounded resume validator | The upstream tailoring path could fail to produce a usable resume or introduce unsupported claims. | Company, school, project identity, dates, numbers, and technical skills are checked before export. Failed output does not advance. |
| Review artifacts | A generated document is hard to trust without seeing exactly what changed. | Each job keeps the master-to-tailored diff, validation JSON, stored JD, and final files. |
| LangGraph only around Stage 6 | Browser work has pauses, restarts, and irreversible actions; ordinary scoring and tailoring do not need agent orchestration. | Approval state survives process restarts, while stages 1-5 remain simple Python services backed by SQLite. |
| Browser-session invalidation | A saved approval must not authorize a different or expired browser session. | Interrupted work is re-prepared and the final approval is cleared before submission can continue. |
| Deterministic Ashby adapter | An agent saying “done” does not prove that the real form contains the correct values. | Stable fields are repaired from the saved profile and the actual DOM is checked before review or submission. |
| Local Review Center | Terminal-only review makes long JDs, documents, answers, and screenshots difficult to compare. | One local page presents the entire application package and stores every decision with notes. |
| Material fingerprints | Approval should apply to exact files and answers, not merely an application ID. | Editing the profile, resume, cover letter, or answers invalidates the old material approval. |
| Isolated Chrome profile selection | Using the wrong signed-in Chrome profile can fill or submit under the wrong account. | The configured account email selects one Chrome profile and copies only session-related state into a private worker directory; Chrome password-store files are excluded. |

These changes improve correctness, auditability, privacy, and recovery. Their effectiveness is checked with regression tests and observable safety invariants: dry-run produces zero submissions, Submit is unreachable before both approvals, changed materials invalidate approval, stale browser sessions require re-preparation, and verified form values come from the browser DOM rather than the agent's summary. They do **not** yet prove a higher interview conversion rate; that requires real application outcomes over time.

## macOS setup

### 1. Install prerequisites

For stages 1-5:

- Python 3.11 or newer
- Git
- One LLM provider: an API provider, a local Ollama server, or an installed and authenticated Claude Code/Codex CLI

For Stage 6, also install:

- Google Chrome
- Node.js 18+ with `npx`
- Claude Code CLI available as `claude`, with authentication completed

Claude Code is the current browser agent even when scoring and tailoring use OpenAI, Gemini, or Ollama.

### 2. Clone this repository and install from source

```bash
git clone https://github.com/JunoWang/OpenApplyPilot.git
cd OpenApplyPilot

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[auto-apply]"

# Job-board discovery dependency. See the note below.
python -m pip install --no-deps python-jobspy
python -m pip install pydantic tls-client requests markdownify regex

python -m playwright install chromium
```

`python-jobspy` currently pins an exact NumPy version in its package metadata. Installing it with `--no-deps` avoids that resolver conflict; the following command installs its runtime dependencies while OpenApplyPilot's normal dependency set supplies pandas and NumPy.

### 3. Create the local profile

```bash
applypilot init
applypilot doctor
```

The setup wizard copies a master `.txt` or `.pdf` resume, asks for reusable application facts, creates search preferences, and writes provider settings under `~/.openapplypilot/`. AI stages require `resume.txt`; when starting from PDF, provide a plain-text copy when prompted.

The API key is saved in `~/.openapplypilot/.env` with owner-only permissions. It is not written into the repository. The wizard currently uses guided questions for `profile.json`; automatic extraction of all profile fields from the resume is not implemented yet.

Already initialized? Activate `.venv` and run `applypilot doctor`; you do not need to run `init` for each session. Keep `profile.json` and the master `resume.txt` consistent, especially contact details, current location, graduation month/year, work authorization, and availability. A change made in a chat, a selection helper, or one tailored resume is not automatically written back to the master profile.

### 4. Configure an LLM provider

`applypilot init` handles one provider interactively. You can also edit `~/.openapplypilot/.env`:

```dotenv
# Choose exactly one default provider.
OPENAPPLYPILOT_LLM_PROVIDER=openai
OPENAPPLYPILOT_LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=your-key
```

| Provider value | Required setting | Example model |
|---|---|---|
| `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-5` |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.0-flash` |
| `ollama` | `OLLAMA_BASE_URL` | `llama3.2` |
| `claude_cli` | Installed `claude` and its own login | `default` (CLI service default) |
| `codex_cli` | Installed `codex` and its own login | `default` (CLI service default) |

Set `OPENAPPLYPILOT_<STAGE>_PROVIDER` and `OPENAPPLYPILOT_<STAGE>_MODEL` to route a specific `SCORE`, `TAILOR`, `COVER`, `DISCOVER`, or `ENRICH` stage differently. OpenApplyPilot never falls back to a different provider after a request failure.

### 5. Optional: use CLI login instead of pipeline API keys

The CLI providers support scoring, tailoring (including the factuality judge), cover letters, and the text-based LLM calls in discovery/enrichment. They do not replace the scrapers. Stage 6 remains the existing Claude Code browser workflow, regardless of the text provider you choose.

Install a current official CLI and complete its own login first. Check without generating content:

```bash
# Choose the CLI you want to use:
claude auth status --text
codex login status
```

If needed, sign in with `claude auth login` or `codex login`. Do not paste tokens into the repository. Configure one default in `~/.openapplypilot/.env` (or select it in `applypilot init`):

```dotenv
OPENAPPLYPILOT_LLM_PROVIDER=codex_cli
OPENAPPLYPILOT_LLM_MODEL=default
OPENAPPLYPILOT_CLI_TIMEOUT_SECONDS=180
```

Use `claude_cli` instead to run all text stages through Claude Code. Explicitly replace an old API model setting with `default` or a model supported by the chosen CLI. Check for existing stage-specific settings: they override these defaults. For example, use Codex for scoring and Claude for resumes and letters without calling both for each task:

```dotenv
OPENAPPLYPILOT_SCORE_PROVIDER=codex_cli
OPENAPPLYPILOT_SCORE_MODEL=default
OPENAPPLYPILOT_TAILOR_PROVIDER=claude_cli
OPENAPPLYPILOT_TAILOR_MODEL=default
OPENAPPLYPILOT_COVER_PROVIDER=claude_cli
OPENAPPLYPILOT_COVER_MODEL=default
```

Then follow the same [end-to-end walkthrough](#local-end-to-end-walkthrough); no pipeline command changes are needed. `applypilot doctor` checks each document stage's configuration and executable, but does not verify CLI login, quota, or model access.

CLI transport behavior and limits:

- Each request uses a fresh temporary directory and stdin, not a resumed agent conversation. Claude runs with safe mode, no tools/MCP servers, and no session persistence. Codex ignores user configuration, uses ephemeral read-only execution, skips host skill discovery, and disables shell, plugins, hooks, multi-agent, browser and related tools. These flags require recent CLI versions; unsupported flags fail rather than silently weakening isolation.
- Pipeline API keys and provider endpoint variables are removed from the child environment. The CLI uses its own saved authentication; the app does not extract tokens. This is not a guarantee of free usage: authentication method, account limits, and service billing still apply. Resume/JD text still goes to the selected model service.
- Only successful CLI result envelopes are accepted. Failed login, quota exhaustion, timeout, or invalid output stops the request without automatic CLI retries or an API/provider fallback. Higher-level factual validation may request another generation when an otherwise successful answer fails validation.
- Temperature is not forwarded, and the API `max_tokens` setting is only an output-length instruction, not a hard CLI token cap. The configurable timeout (1–1800 seconds) stops the CLI process tree on macOS; start with one job and monitor account usage.
- CLI agents still carry a base instruction overhead even with skills disabled; using CLI login does not inherently consume fewer tokens than a direct API request.
- Existing resume factual checks and both application approval gates remain in place. This is a text-provider integration, not a Codex browser adapter or an unattended submission feature.

Official CLI references: [Claude programmatic mode](https://code.claude.com/docs/en/headless), [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive).

## Local end-to-end walkthrough

Complete installation above first. In each new terminal, enter your repository directory and activate the environment:

```bash
cd /path/to/OpenApplyPilot
source .venv/bin/activate
applypilot doctor
```

Replace `/path/to/OpenApplyPilot` with your checkout location. Resolve missing requirements reported by `doctor` before proceeding. Start with **one selected job** to limit model usage and make failures easy to inspect.

### 1. Discover jobs and inspect the list

Edit `~/.openapplypilot/searches.yaml` to set your target roles, locations, boards, and exclusions. See the [search configuration example](src/applypilot/config/searches.example.yaml). To request recent jobs from JobSpy, change the existing setting:

```yaml
defaults:
  results_per_site: 10
  hours_old: 24
```

Preserve the other settings in your file; this snippet is not a complete configuration. Then run:

```bash
applypilot run discover enrich --workers 1
applypilot status
applypilot dashboard
```

Important limits:

- `hours_old: 24` is a JobSpy request filter, not a freshness guarantee for every source. Workday and other career-site results may be older. Verify the posting time and official application page before selecting a job.
- Multi-source discovery is not an exhaustive search of the entire web. Sources can block requests or return incomplete results.
- There is no reliable single worldwide switch across all sources. Configure supported locations explicitly. Empty location accept filters currently reject known non-remote locations; remote results bypass those location rejection checks. Location exclusions are not a substitute for reviewing eligibility.
- Review local work authorization, sponsorship, clearance, graduation timing, and onsite requirements. A fit score does not establish eligibility. Deduplicate by employer requisition ID as well as URL.
- `--limit` limits score/tailor/cover/export work, **not discovery volume**. Keep query counts and `results_per_site` small for a first test.

The dashboard is a results view. Selecting a job for application still means passing its stored URL explicitly to the commands below; it does not automatically launch submissions.

#### Optional: standalone 24-hour selection UI

Some local test workspaces also contain `job_selection.py` alongside the repository. **This helper is not included in a fresh clone or the installed package.** Use this section only if that file already exists; otherwise use the built-in discovery flow above.

With the project virtual environment active, enter the directory containing the helper:

```bash
cd /path/to/workspace-containing-job_selection.py
python job_selection.py discover
python job_selection.py serve
```

Open <http://127.0.0.1:8765>, filter and check jobs, then save the list. Keep this terminal running and use another terminal for the pipeline. Press Ctrl+C to stop the helper server.

The helper searches a bounded first batch of LinkedIn worldwide results for six hardcoded AI/ML role queries. It stores results in `job-selection/latest.json` and selections in `job-selection/selection.json`, relative to the script. Its recent-post label is based on source text at discovery time; refresh discovery for a new day's search and recheck your selections. Saving is **not submission approval**, does not import jobs into SQLite, and does not automatically apply chat-based skip rules. Manually exclude unsuitable jobs and import each chosen link using step 2.

### 2. Choose one job and import it if needed

For a job already stored by built-in discovery, use its exact stored URL. For a LinkedIn link selected outside the database, import it first:

```bash
applypilot add 'LINKEDIN_JOB_URL'
```

Copy the canonical URL printed by `add`, then set it in the terminal used for the remaining steps:

```bash
JOB_URL='CANONICAL_OR_STORED_JOB_URL'
```

Replace the placeholder before running commands. Do not use a different URL variant for subsequent stages. Check existing application history and any manual submission receipts first to avoid applying twice.

### 3. Score and generate tailored materials

```bash
applypilot run score tailor cover pdf \
  --url "$JOB_URL" \
  --limit 1 \
  --min-score 1 \
  --validation normal
```

This creates local records and documents, not an application submission. Cloud model calls can incur charges and transmit resume/JD content to your configured provider. `--min-score 1` lets you test the one job you explicitly chose regardless of its fit score; it is not a suitability recommendation. Keep factual validation enabled and review the generated documents and diff before use.

For a larger, already reviewed pool, you can instead run `applypilot run score tailor cover pdf --min-score 8 --limit 20`. Avoid starting with bare `applypilot run`: it runs all preparation stages and may process more jobs than you intend.

### 4. Fill one form without submitting

```bash
applypilot apply --url "$JOB_URL" --dry-run
```

Follow the terminal's material-approval prompt. The browser can fill the form and reach `ready_for_review`, but this mode cannot submit or mark the job applied. **Dry-run is not offline:** navigating an employer site, entering fields, or uploading files can disclose application data to that site before final submission. Login, CAPTCHA, and unknown required answers may need your intervention.

`applypilot run --dry-run` is different: it only previews pipeline stages without executing them; it is not a browser fill test.

### 5. Review the application package

After form preparation stops, run:

```bash
applypilot review --port 8766
```

Use the local URL printed in the terminal. Port 8766 avoids a conflict if the optional selection helper is using 8765. The Review Center shows the stored JD, documents, verified answers, screenshots, agent log, and decision history. It is separate from the discovery dashboard and selection helper.

Check dates, contact details, work authorization, required answers, and the actual attachment. Request changes or reject if anything is wrong. From Review Center you can request another dry-run or approve the reviewed materials and open a fresh visible final review. Follow that workflow's terminal prompts; do not also start a second application process for the same job.

### 6. Submit with explicit approval and verify the receipt

Alternatively, stop the Review Center with Ctrl+C and use the terminal submission workflow:

```bash
applypilot apply --url "$JOB_URL"
```

Approve the exact materials, inspect the live form, and approve the final Submit prompt only when correct. Do not invent missing answers or bypass site verification. Submission is irreversible.

Verify the employer's confirmation page or receipt, then inspect local records:

```bash
applypilot status
applypilot review --port 8766
```

An agent saying “done” is not a submission receipt. If the result is uncertain, check the employer portal/email before retrying. For a saved workflow, use the application ID shown in the local records:

```bash
applypilot apply --resume APPLICATION_ID
```

Recovery can require fresh preparation and approval. Stage 6 supports one application at a time; do not wrap submission commands in an unattended batch loop.

If you completed an application manually or with an external browser assistant, that success is not automatically synchronized into this database. Once the job is stored and you have verified the receipt, record it without resubmitting:

```bash
applypilot apply --mark-applied "$JOB_URL"
```

This records a manual status; it does not recreate the external browser evidence. Retain the receipt separately.

### Common local problems

| Symptom | What to check |
|---|---|
| `applypilot: command not found` | Activate the checkout's `.venv`; install with `python -m pip install -e ".[auto-apply]"` if needed. |
| Missing provider key, Chrome, Claude, or workflow dependency | Run `applypilot doctor`. Stage 6 needs Claude Code even if tailoring uses another provider. |
| Port already in use | Use `applypilot review --port 8766`, or another unused port. Stop only the server you started. |
| Saved helper selections are missing from the dashboard | The helper JSON is separate from SQLite; import chosen LinkedIn URLs with `applypilot add`. |
| Old location or graduation date appears again | Update both the master profile and resume, regenerate the affected materials, and review again. |
| Form cannot be verified or requires login/CAPTCHA | Complete the required manual step. Do not treat a paused or failed run as a successful application. |

### Migrate an upstream ApplyPilot installation

Preview the migration before it writes anything:

```bash
applypilot migrate --dry-run
applypilot migrate
```

Migration reads `~/.applypilot/` without modifying it, creates a verified archive, imports job identity and full JDs, and resets old scores, generated materials, and application state so the new pipeline can rebuild them consistently.

## Local data and privacy

The default data root is `~/.openapplypilot/`. Override it with `OPENAPPLYPILOT_HOME`.

| Path | Contents |
|---|---|
| `openapplypilot.db` | Jobs, versioned JD snapshots, pipeline history, application records, and review decisions. |
| `profile.json` | Contact data, work authorization, reusable form answers, resume facts, and selected Chrome account email. |
| `.env` | Provider keys and runtime configuration. |
| `resume.txt` / `resume.pdf` | Master resume files. |
| `tailored_resumes/` | Tailored TXT, DOCX, PDF, JD copy, diff, and validation report. |
| `cover_letters/` | Per-job cover letters. |
| `application_reviews/` | Review and submission screenshots plus durable evidence. |
| `auto_apply_checkpoints.db` | Local LangGraph checkpoints for Stage 6. |
| `logs/openapplypilot.log` | Rotating runtime log with credential redaction. |
| `chrome-workers/` | Private, account-specific Chrome worker copies. |
| `archives/` | Content-addressed legacy database archives. |

LangSmith tracing is forced off unless `OPENAPPLYPILOT_LANGSMITH_OPT_IN=true` is set. To actually send traces, you must also configure LangSmith's own tracing and API-key variables. Do not expose the Review Center beyond localhost; it is designed as a single-user local tool, not a hosted service.

See [Local data storage](docs/data-storage.md) for schema and migration details and [ADR 0001](docs/architecture/0001-hybrid-pipeline-orchestration.md) for the LangGraph/LangSmith decision.

## CLI reference

```text
applypilot init                           First-time local setup
applypilot doctor                         Diagnose missing files and dependencies
applypilot add URL                        Import one LinkedIn job and full JD
applypilot run [STAGES...]                Run discover/enrich/score/tailor/cover/pdf
applypilot status                         Show local pipeline statistics
applypilot dashboard                      Open the job-results dashboard
applypilot apply --url URL --dry-run       Fill one form and stop before submission
applypilot review [--id APPLICATION_ID]    Open the local Review Center
applypilot apply --url URL                 Run the two-approval submission workflow
applypilot apply --resume APPLICATION_ID   Resume a saved workflow
applypilot apply --gen --url URL           Generate a non-submitting debug prompt
applypilot apply --mark-applied URL         Record a manual submission
applypilot apply --mark-failed URL          Record a manual failure
applypilot apply --reset-failed             Reset failed jobs for retry
applypilot migrate --dry-run                Preview legacy migration
```

Use `applypilot COMMAND --help` for every flag.

## Current limitations

- macOS is the first end-to-end supported environment. Windows and Linux paths exist but have not received the same real-browser validation.
- The deterministic post-agent ATS adapter currently covers Ashby. Other forms remain agent-driven and may stop for manual completion or fail verification.
- Stage 6 requires Claude Code CLI; the API/CLI text-provider setting applies to discovery, enrichment, scoring, tailoring, and cover letters, not the browser agent.
- Bulk workers and `--continuous` are disabled for Stage 6A. The system processes one selected application at a time.
- CAPTCHA, SSO, login walls, unusual widgets, and unknown required questions can require manual action. OpenApplyPilot does not invent answers.
- Resume-to-profile extraction is still pending; setup copies the resume but asks for structured profile fields.
- The project has safety and regression evidence, but no claim is made that automation improves interview or offer rates.

## Development and documentation

- [Contributing guide](CONTRIBUTING.md)
- [Local data storage](docs/data-storage.md)
- [Hybrid orchestration decision](docs/architecture/0001-hybrid-pipeline-orchestration.md)
- [Single-job tailoring acceptance record](docs/stage-3-single-job-validation.md)

Run the test suite with:

```bash
python -m pip install -e ".[dev,auto-apply]"
python -m pytest -q
ruff check src tests
```

## License

OpenApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE). You may use, modify, and distribute it under that license. If you offer a modified version over a network, review the AGPL source-availability obligations that apply to your deployment.
