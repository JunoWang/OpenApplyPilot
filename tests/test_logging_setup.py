import logging
import stat

from applypilot.logging_setup import configure_file_logging, redact_text


def test_redact_text_removes_common_credentials() -> None:
    openai_secret = "sk-" + "example1234567890"
    gemini_secret = "AIza" + "ExampleCredential1234567890"
    message = (
        f"OPENAI_API_KEY={openai_secret} "
        f"GEMINI_API_KEY={gemini_secret}"
    )
    redacted = redact_text(message)
    assert "sk-example" not in redacted
    assert "AIzaExample" not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_file_logging_is_private_and_redacted(tmp_path) -> None:
    log_path = tmp_path / "logs" / "openapplypilot.log"
    configure_file_logging(log_path)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logging.getLogger("openapplypilot-test").info(
        "Authorization: Bearer sk-secret1234567890"
    )

    target_handler = next(
        handler
        for handler in root.handlers
        if getattr(handler, "_openapplypilot_log_path", None) == str(log_path.resolve())
    )
    target_handler.flush()
    content = log_path.read_text(encoding="utf-8")
    assert "sk-secret" not in content
    assert "[REDACTED]" in content
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600

    root.removeHandler(target_handler)
    target_handler.close()
