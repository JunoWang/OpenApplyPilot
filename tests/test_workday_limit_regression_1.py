from applypilot.discovery import workday


# Regression: ISSUE-004 — Workday ignored the user's results_per_site limit.
# Found by /qa on 2026-09-29
# Report: .gstack/qa-reports/qa-report-openapplypilot-e2e-2026-09-29.md
def test_workday_discovery_passes_configured_result_limit(monkeypatch) -> None:
    received: list[int] = []
    monkeypatch.setattr(
        workday.config,
        "load_search_config",
        lambda: {
            "defaults": {"results_per_site": 5},
            "queries": [{"query": "AI Engineer", "tier": 1}],
        },
    )
    monkeypatch.setattr(workday, "_load_location_filter", lambda _config: ([], []))

    def scrape_employers(**kwargs):
        received.append(kwargs["max_results"])
        return {"found": 0, "new": 0, "existing": 0}

    monkeypatch.setattr(workday, "scrape_employers", scrape_employers)

    workday.run_workday_discovery(
        employers={"example": {"name": "Example"}},
        workers=1,
    )

    assert received == [5]
