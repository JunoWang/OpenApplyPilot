import json
from pathlib import Path

from applypilot.apply import chrome


def _write_chrome_profiles(root: Path) -> None:
    (root / "Default").mkdir(parents=True)
    (root / "Profile 5").mkdir()
    (root / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    (root / "Profile 5" / "Preferences").write_text("{}", encoding="utf-8")
    (root / "Profile 5" / "Cookies").write_bytes(b"opaque-cookie-database")
    (root / "Local State").write_text(
        json.dumps(
            {
                "profile": {
                    "info_cache": {
                        "Default": {"user_name": "other@example.com"},
                        "Profile 5": {"user_name": "candidate@example.com"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_find_chrome_profile_by_email_matches_without_reading_profile_data(tmp_path: Path) -> None:
    _write_chrome_profiles(tmp_path)

    assert chrome.find_chrome_profile_by_email(tmp_path, "CANDIDATE@example.com") == "Profile 5"
    assert chrome.find_chrome_profile_by_email(tmp_path, "missing@example.com") is None


def test_setup_worker_profile_copies_only_selected_account(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "chrome"
    workers = tmp_path / "workers"
    _write_chrome_profiles(source)
    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", workers)
    monkeypatch.setattr(chrome.config, "get_chrome_user_data", lambda: source)
    monkeypatch.setattr(
        chrome.config,
        "load_profile",
        lambda: {
            "personal": {"email": "application@example.com"},
            "browser": {"chrome_account_email": "candidate@example.com"},
        },
    )

    worker_root, profile_name = chrome.setup_worker_profile(0)

    assert profile_name == "Profile 5"
    assert (worker_root / "Profile 5" / "Preferences").exists()
    assert (worker_root / "Profile 5" / "Cookies").read_bytes() == b"opaque-cookie-database"
    assert not (worker_root / "Default").exists()
    assert json.loads((worker_root / ".openapplypilot-profile.json").read_text()) == {
        "source_profile": "Profile 5"
    }


def test_explicit_missing_chrome_account_does_not_fall_back_to_default(
    tmp_path: Path, monkeypatch
) -> None:
    _write_chrome_profiles(tmp_path)
    monkeypatch.setattr(
        chrome.config,
        "load_profile",
        lambda: {"browser": {"chrome_account_email": "missing@example.com"}},
    )

    try:
        chrome._select_source_profile(tmp_path)
    except RuntimeError as exc:
        assert "No local Chrome profile matches" in str(exc)
    else:
        raise AssertionError("Expected an explicit account mismatch to fail closed")
