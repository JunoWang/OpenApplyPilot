# Contributing to OpenApplyPilot

Thank you for your interest in contributing to OpenApplyPilot. This guide covers everything you need to get started.

## Development Setup

### Prerequisites

- Python 3.11 or higher
- Git
- Node.js 18+, Chrome, and Claude Code CLI when changing Stage 6

### Clone and Install

```bash
git clone https://github.com/JunoWang/OpenApplyPilot.git
cd OpenApplyPilot
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,auto-apply]"
python -m pip install --no-deps python-jobspy
python -m pip install pydantic tls-client requests markdownify regex
python -m playwright install chromium
```

This installs OpenApplyPilot in editable mode with development and Stage 6 dependencies, then downloads the Chromium browser binary for Playwright.

### Verify Installation

```bash
applypilot --version
python -m pytest -q
ruff check src tests
```

## How to Contribute

### Adding New Workday Employers

Workday employer portals are configured in `src/applypilot/config/employers.yaml`. To add a new employer:

1. Find the company's Workday career portal URL (usually `https://company.wd5.myworkdaysite.com/`)
2. Identify the Workday instance number (wd1, wd3, wd5, etc.) and the tenant ID
3. Add an entry to `src/applypilot/config/employers.yaml`:

```yaml
employers:
  company_key:
    name: "Company Name"
    tenant: "company_tenant_id"
    site_id: "External"
    base_url: "https://company.wd5.myworkdayjobs.com"
```

4. Run the relevant discovery tests, then smoke-test with `applypilot run discover`
5. Submit a PR with the new entry

### Adding New Career Sites

Direct career site scrapers are configured in `src/applypilot/config/sites.yaml`. To add a new site:

1. Inspect the company's careers page and identify the job listing structure
2. Add an entry to `src/applypilot/config/sites.yaml` with a search or static URL:

```yaml
sites:
  - name: "Company Name"
    url: "https://company.com/careers?q={query_encoded}"
    type: search
```

3. Run the relevant discovery tests, then smoke-test with `applypilot run discover`
4. Submit a PR

### Bug Fixes and Features

1. Check existing [issues](https://github.com/JunoWang/OpenApplyPilot/issues) to avoid duplicating work
2. For new features, open an issue first to discuss the approach
3. Fork the repo and create a feature branch from `main`
4. Write your code with type hints and docstrings
5. Add tests for new functionality
6. Update the CHANGELOG.md under an `[Unreleased]` section
7. Submit a PR

## Running Tests

```bash
# Run all tests
python -m pytest -q

# Run a specific test file
python -m pytest tests/test_scorer.py -v

# Run with coverage
python -m pytest tests/ --cov=src/applypilot --cov-report=term-missing
```

## Linting and Code Style

ApplyPilot uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```bash
# Check for issues
ruff check src tests

# Auto-fix what can be fixed
ruff check src tests --fix

# Format code
ruff format src tests
```

### Code Style Guidelines

- **Type hints**: All function signatures must have type annotations
- **Docstrings**: All public functions and classes must have docstrings (Google style)
- **Naming**: snake_case for functions and variables, PascalCase for classes
- **Imports**: Sorted by Ruff (isort-compatible)
- **Line length**: 120 characters maximum

## PR Guidelines

- **One feature per PR.** Keep changes focused and reviewable.
- **Include tests.** New features need test coverage. Bug fixes need a regression test.
- **Update CHANGELOG.md.** Add your changes under `[Unreleased]`.
- **Write a clear PR description.** Explain what changed and why.
- **Keep commits clean.** Squash fixup commits before requesting review.
- **CI must pass.** All linting and tests must be green.

## Project Structure

```
OpenApplyPilot/
├── src/applypilot/       # Main package
│   ├── __init__.py
│   ├── cli.py            # CLI entry points
│   ├── pipeline.py       # Stages 1-5 orchestration and persistence
│   ├── database.py       # Local SQLite schema and application history
│   ├── discovery/        # Stage 1: job discovery scrapers
│   ├── enrichment/       # Stage 2: description extraction
│   ├── scoring/          # Stages 3-5: scoring, tailoring, export
│   ├── apply/            # Stage 6: browser automation
│   ├── config/           # Package-shipped YAML registries
│   └── wizard/           # First-time local setup
├── tests/                # Test suite
├── docs/                 # Documentation
└── pyproject.toml        # Package configuration
```

## License

By contributing to OpenApplyPilot, you agree that your contributions will be licensed under the [GNU Affero General Public License v3.0](LICENSE).
