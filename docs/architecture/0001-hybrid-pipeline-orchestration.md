# ADR 0001: Hybrid pipeline orchestration

- Status: Accepted
- Date: 2026-09-02

## Context

OpenApplyPilot has two different workflow shapes:

1. Discover, enrich, score, tailor, and cover/PDF are deterministic data
   stages with explicit inputs, outputs, retry rules, and SQLite checkpoints.
2. Auto-Apply is a long-running browser workflow that must pause for two
   human decisions: material approval and final submission approval.

The application is local-first. Resumes, job descriptions, contact details,
and application answers must not be sent to an observability service without
the user's explicit opt-in.

## Decision

- Keep the first five stages as ordinary Python services coordinated by a
  typed pipeline runner and persisted in local SQLite.
- Use provider-native adapters for OpenAI, Anthropic, Gemini, and Ollama.
  Provider or model failure is recorded explicitly and never triggers an
  implicit cross-provider fallback.
- Use LangGraph only for Auto-Apply state, durable pause/resume, and the two
  human approval gates.
- Keep LangSmith optional and disabled by default. If enabled later, traces
  must be redacted before export and clearly disclose that data leaves the
  local machine.
- Core operation, including Ollama operation, must not require a LangSmith
  account or network connection beyond the selected model provider and job
  sites.

## Consequences

- Deterministic stages remain easy to unit test, replay, and inspect in SQLite.
- Browser applications can resume safely after user review or process failure.
- Provider-specific capabilities and errors stay visible rather than being
  hidden behind a lowest-common-denominator API.
- LangGraph becomes an optional dependency only when the Auto-Apply stage is
  implemented; it is not added to the scoring/tailoring dependency path.
- Local structured logs and test fixtures are the default observability layer.
