# OpenApplyPilot

Local-first job discovery, resume tailoring, and human-approved application automation.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/JunoWang/OpenApplyPilot?style=social)](https://github.com/JunoWang/OpenApplyPilot)

OpenApplyPilot is derived from [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot). It keeps the original six-stage product flow, then rebuilds the parts that need stronger local persistence, factual resume validation, recoverable browser state, and human review. This repository is not affiliated with applypilot.app, useapplypilot.com, or other commercial products using the ApplyPilot name.

> **Project status:** macOS-first beta. Stages 1-5 work as a repeatable local pipeline. Stage 6A supports one explicitly selected application at a time, with a deterministic Ashby adapter and mandatory human approval. Bulk or continuous submission is intentionally disabled.

## What works today

| Capability | Current status |
|---|---|
| Multi-source discovery | Indeed, LinkedIn, ZipRecruiter, Google Jobs, and Glassdoor through JobSpy, plus 48 Workday employers and 30 configured career sites. Individual sources can still block scraping. |
| Full JD storage | Job descriptions are versioned in local SQLite and remain available later for interview preparation. |
| Fit scoring | OpenAI, Anthropic Claude, Gemini, Ollama, and OpenAI-compatible endpoints are supported with explicit provider/model selection. |
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

You need:

- Python 3.11 or newer
- Git
- One LLM provider: OpenAI, Anthropic, Gemini, or a local Ollama server

To use Stage 6 Auto Apply, you also need:

- Google Chrome
- Node.js 18+ with `npx`
- Claude Code CLI available as `claude`

Claude Code is the current browser agent even when scoring and tailoring use OpenAI, Gemini, or Ollama.

Check the command-line prerequisites before continuing:

```bash
python3.11 --version
git --version
node --version       # Stage 6 only
npx --version        # Stage 6 only
claude --version     # Stage 6 only
```

If a command is missing, install that prerequisite first. The remaining steps assume `python3.11` is available.

### 2. Clone this repository and install from source

```bash
git clone https://github.com/JunoWang/OpenApplyPilot.git
cd OpenApplyPilot

python3.11 -m venv .venv
source .venv/bin/activate

# Install OpenApplyPilot, Auto Apply, and job-board discovery.
python -m pip install ".[auto-apply]" python-jobspy

# Install the Chromium build used by discovery and enrichment.
python -m playwright install chromium
```

What the install command means:

- `python -m pip` uses `pip` from the active `.venv`, avoiding accidental installation into another Python.
- `.` installs OpenApplyPilot from the repository you just cloned.
- `[auto-apply]` adds LangGraph and its local SQLite checkpointer.
- `python-jobspy` enables discovery from the supported public job boards.

This is a normal user installation. The developer-only editable flag (`-e`) is intentionally omitted. Each time you open a new terminal, return to the repository and reactivate the environment before using `applypilot`:

```bash
cd OpenApplyPilot
source .venv/bin/activate
```

### 3. Create the local profile

```bash
applypilot init
applypilot doctor
python -m pip check
```

`doctor` should report the expected local files, provider, and installed tools. `pip check` should print `No broken requirements found.`

The setup wizard copies a master `.txt` or `.pdf` resume, asks for reusable application facts, creates search preferences, and writes provider settings under `~/.openapplypilot/`. AI stages require `resume.txt`; when starting from PDF, provide a plain-text copy when prompted.

The API key is saved in `~/.openapplypilot/.env` with owner-only permissions. It is not written into the repository. The wizard currently uses guided questions for `profile.json`; automatic extraction of all profile fields from the resume is not implemented yet.

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

Set `OPENAPPLYPILOT_<STAGE>_PROVIDER` and `OPENAPPLYPILOT_<STAGE>_MODEL` to route a specific `SCORE`, `TAILOR`, `COVER`, `DISCOVER`, or `ENRICH` stage differently. OpenApplyPilot never falls back to a different provider after a request failure.

## How to use it

### Test one real job first

Import a LinkedIn job URL, then use the canonical URL printed by the command:

```bash
applypilot add 'LINKEDIN_JOB_URL'

applypilot run score tailor cover pdf \
  --url 'CANONICAL_JOB_URL' \
  --limit 1 \
  --min-score 1 \
  --validation normal
```

This only creates local records and documents. It does not open an application form or submit anything.

### Run normal discovery and document generation

```bash
applypilot run
applypilot status
applypilot dashboard
```

Useful variants:

```bash
applypilot run discover enrich --workers 4
applypilot run score tailor cover pdf --min-score 8 --limit 20
applypilot run --dry-run
```

### Prepare an application without submitting

This is the recommended first Stage 6 test:

```bash
applypilot apply --url 'CANONICAL_JOB_URL' --dry-run
applypilot review
```

The dry-run can reach `ready_for_review`, but cannot mark the job applied, click Submit, or consume the production retry budget. The Review Center binds to `127.0.0.1` and displays the saved JD, tailored resume, cover letter, verified form answers, screenshot, agent log, and decision history.

From the Review Center you can request changes, reject, rerun the dry-run, or approve the reviewed materials and reopen a visible final browser review. The final Submit action still requires a separate explicit confirmation in the terminal.

### Run the submission flow

```bash
applypilot apply --url 'CANONICAL_JOB_URL'
```

To continue a saved application:

```bash
applypilot apply --resume APPLICATION_ID
```

Submission is irreversible. Read the saved answers and inspect the live form before approving the final terminal prompt.

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
- Stage 6 requires Claude Code CLI; the multi-provider LLM setting applies to discovery, enrichment, scoring, tailoring, and cover letters.
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
python -m pip install -e ".[dev,auto-apply]" python-jobspy
python -m pytest -q
ruff check src tests
```

## License

OpenApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE). You may use, modify, and distribute it under that license. If you offer a modified version over a network, review the AGPL source-availability obligations that apply to your deployment.
