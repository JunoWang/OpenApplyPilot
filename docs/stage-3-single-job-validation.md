# Stage 3: single-job end-to-end validation

Stage 3 was accepted against a real, fully stored job description on September
3, 2026. The test target was Thomson Reuters' "Research Scientist, LLM Agents
(Foundational Research)" role. No application was submitted.

## Test flow

The exact-job mode keeps the blast radius to one database record:

```bash
applypilot run score tailor pdf \
  --url 'https://thomsonreuters.wd5.myworkdayjobs.com/External_Career_Site/job/United-Kingdom-London/Research-Scientist--LLM-Agents--Foundational-Research-_JREQ193529' \
  --limit 1 --min-score 1 --validation normal
```

The first live run reproduced the original tailoring failure. Validation caught
unsupported technical skills suggested by stale profile hints, and numeric
validation incorrectly treated phone-number digits in the code-injected header
as resume claims. No unapproved resume was exposed to later stages.

The implementation now derives the allowed skill boundary from the master
resume, validates numeric claims only in resume content, preserves education and
publications from the source, and requires both deterministic validation and the
LLM factuality judge to pass. The retry then completed successfully.

## Acceptance evidence

- Fit score: 7/10.
- Tailoring generation attempts: 1 on the successful retry.
- Deterministic validator: pass.
- Full source-grounded validator: pass, with one non-blocking renamed-project warning.
- LLM factuality judge: pass.
- Export: private TXT, one-page DOCX, and one-page searchable PDF plus JD,
  unified diff, and JSON report. Both rendered formats passed visual inspection.
- Database: the full JD and all artifact paths remain available for interview preparation.
- Submission: not attempted; `apply_status` remains unset.
- Automated suite: 24 tests, including targeted-run isolation, source-grounded
  validation, audit diff generation, HTML escaping, and ATS-oriented DOCX structure.

Run IDs and timestamps are retained locally in `pipeline_runs` and
`stage_events`; logs are stored under `~/.openapplypilot/logs/`.
