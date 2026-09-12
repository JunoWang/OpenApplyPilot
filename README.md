<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Local-first, human-approved job application automation. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/JunoWang/OpenApplyPilot?style=social)](https://github.com/JunoWang/OpenApplyPilot)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 6-stage job application pipeline. It discovers jobs across 5+ boards, scores them against your resume with AI, tailors your resume per job, writes cover letters, and prepares browser applications. Auto-Apply requires explicit approval of the materials and a second explicit approval immediately before submission.

Three commands. That's it.

```bash
pip install 'applypilot[auto-apply]'
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot apply --url URL --dry-run  # approve materials, fill form, never submit
applypilot apply --url URL            # two approvals; second gate controls Submit
```

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, one supported LLM provider, Node.js (for npx), Claude Code CLI, Chrome

Runs all 6 stages, from job discovery to a reviewed application submission. Stage 6A intentionally handles one selected job at a time and disables continuous/bulk submission until the safety workflow is validated.

### Discovery + Tailoring Only
**Requires:** Python 3.11+ and OpenAI, Anthropic, Gemini, or Ollama

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Claude Code prepares one selected form; LangGraph checkpoints material approval and final submission approval locally |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation with two human approval gates | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, 46 Workday portals, 28 direct sites | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| LLM provider | Scoring, tailoring, cover letters | OpenAI, Anthropic Claude, Gemini, and Ollama are supported |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply | Install from [claude.ai/code](https://claude.ai/code) |

Cloud providers require their own API key. Ollama can run entirely on the local machine.

### Optional

| Component | What It Does |
|-----------|-------------|
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `applypilot init` under `~/.openapplypilot/`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), EEO defaults, and the Chrome account email used for auto-apply. Powers scoring, tailoring, and form auto-fill. On macOS, ApplyPilot resolves that email to its local Chrome profile and copies only that profile into an isolated worker directory; it never reads browser passwords.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
API keys and runtime config. Select a default with `OPENAPPLYPILOT_LLM_PROVIDER`
and `OPENAPPLYPILOT_LLM_MODEL`, then provide the matching `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or `OLLAMA_BASE_URL`. Each AI stage can
override the default with `OPENAPPLYPILOT_<STAGE>_PROVIDER` and
`OPENAPPLYPILOT_<STAGE>_MODEL`. See `.env.example` for the complete format.

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

### Local data layout

OpenApplyPilot keeps personal data on the local machine by default:

| Path | Contents |
|------|----------|
| `~/.openapplypilot/openapplypilot.db` | Jobs, versioned JD snapshots, pipeline runs, and application history |
| `~/.openapplypilot/profile.json` | Application profile, Chrome account selection, and reusable answers |
| `~/.openapplypilot/.env` | Provider keys; created with owner-only permissions |
| `~/.openapplypilot/tailored_resumes/` | Per-job tailored resumes |
| `~/.openapplypilot/cover_letters/` | Per-job cover letters |
| `~/.openapplypilot/application_reviews/` | Final review screenshots and submission evidence |
| `~/.openapplypilot/auto_apply_checkpoints.db` | Local LangGraph approval and resume checkpoints |
| `~/.openapplypilot/logs/openapplypilot.log` | Rotating, credential-redacted runtime log |
| `~/.openapplypilot/archives/` | Content-addressed legacy database backups |

Override the root directory with `OPENAPPLYPILOT_HOME` when needed.

To migrate an upstream ApplyPilot installation, preview first and then run the
same command without `--dry-run`:

```bash
applypilot migrate --dry-run
applypilot migrate
```

Migration imports discovery data and full JDs, but intentionally resets scores,
tailored materials, and application state. The source `~/.applypilot/` directory
is preserved after a verified archive is created.

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes 48 Workday employer portals (configurable in `employers.yaml`). Hits 30 direct career sites with custom extractors. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

Project names and their subtitles or dates are immutable. Tailoring can reorder
projects and rewrite supported bullet points, but any renamed, omitted, or newly
invented project fails validation before DOCX/PDF export.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
Claude Code launches a Chrome instance, navigates to the selected application page, fills personal information and work history, uploads the tailored resume and cover letter, and answers screening questions. LangGraph pauses before browser preparation for material approval and again on the completed form before the separate submit phase. Review screenshots, form answers, agent logs, approval times, and submission evidence remain under `~/.openapplypilot/`.

For supported ATS forms, deterministic adapters repair and verify the final browser state after agent navigation. The Ashby adapter fills stable labeled fields, selects autocomplete locations, uploads the approved resume, applies profile-backed Yes/No answers, clears unsupported optional values, and records the browser's actual field state. Unknown required answers fail closed instead of being guessed.

Stage 6A requires `--url`, one visible worker, and no continuous mode. A dry run ends at `ready_for_review`; it can never set the job to applied or consume the production retry budget. LangSmith tracing is disabled unless `OPENAPPLYPILOT_LANGSMITH_OPT_IN=true` is explicitly configured.

The Playwright MCP server is configured automatically at runtime per worker. No manual MCP setup needed.

```bash
# Utility modes (no Chrome/Claude needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
applypilot apply --gen --url URL       # generate prompt file for manual debugging
```

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot run score tailor pdf \
  --url JOB_URL --limit 1               # Safely test one stored job end to end
applypilot add LINKEDIN_JOB_URL          # Import one pasted LinkedIn job + JD
applypilot apply --url URL --dry-run    # Approve materials and stop at review
applypilot apply --url URL              # Review, then explicitly approve Submit
applypilot apply --resume APPLICATION_ID # Continue a saved application with fresh review
applypilot apply --url URL --headless   # Supported, but visible review is recommended
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open HTML results dashboard
```

The targeted Stage 3 command only scores, tailors, validates, and exports the
selected job. It does **not** submit an application. Approved outputs are saved
with owner-only permissions under `~/.openapplypilot/tailored_resumes/` as TXT,
DOCX, PDF, a JD snapshot, a unified diff, and a JSON validation report. See the
[single-job Stage 3 acceptance record](docs/stage-3-single-job-validation.md).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
