# Local data storage

OpenApplyPilot is local-first. The default data root is:

```text
~/.openapplypilot/
├── openapplypilot.db
├── profile.json
├── resume.txt
├── resume.pdf
├── searches.yaml
├── .env
├── archives/
├── application_reviews/
├── auto_apply_checkpoints.db
├── chrome-workers/          # isolated copy of only the selected Chrome account profile
├── logs/
├── tailored_resumes/
└── cover_letters/
```

Set `OPENAPPLYPILOT_HOME` to use a different root. The database, credentials,
profile, resume, and archives are restricted to the current macOS user.
The auto-apply browser account is selected by `browser.chrome_account_email`
in `profile.json`. Chrome's profile metadata is used to locate it; cookies and
passwords are not inspected by OpenApplyPilot.

## Database schema v4

- `jobs`: current job and stage state; AI and application failures remain
  explicit and retryable.
- `jd_snapshots`: immutable, hash-deduplicated JD history. The current snapshot
  is marked without removing earlier versions.
- `pipeline_runs`: one durable record per pipeline invocation, including the
  requested stages, result, timings, and local log path.
- `stage_events`: append-only stage history associated with a pipeline run and,
  when available, a job.
- `applications`: form-like application history with material approval, final
  approval, answers, document paths, review/submission screenshots, agent logs,
  verification state, and submission timestamps.
- `system_metadata`: migration provenance and local schema metadata.
- `schema_migrations`: applied schema versions.

`auto_apply_checkpoints.db` is a separate local LangGraph checkpoint database.
It allows the browser workflow to stop at approval gates without mixing
framework-owned checkpoint tables into `openapplypilot.db`. LangSmith tracing is
off by default; no checkpoint or application data is exported unless the user
explicitly opts in.

Continue an interrupted or `ready_for_review` application with:

```bash
applypilot apply --resume APPLICATION_ID
```

If the original Chrome session no longer exists—or the previous dry run already
ended—the old reviewed browser state and any final approval are not reusable.
OpenApplyPilot prepares the form again and requires fresh material and final
approval before submission.

## Legacy migration

Always preview first:

```bash
applypilot migrate --dry-run
applypilot migrate
```

The migration:

1. opens `~/.applypilot/applypilot.db` as immutable/read-only;
2. creates a consistent database archive under
   `~/.openapplypilot/archives/`;
3. imports job identity, discovery metadata, application URL, and full JD;
4. creates one current JD snapshot per imported description;
5. resets score, tailored material, cover-letter, and application state;
6. copies profile, resume, searches, and provider configuration when the
   target file does not already exist;
7. leaves the original directory untouched.

The operation is idempotent. Re-running it skips jobs already present and
does not create duplicate JD snapshots.
