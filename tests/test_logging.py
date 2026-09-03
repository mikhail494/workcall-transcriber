import logging
from pathlib import Path

from workcall_transcriber.logging_config import configure_application_logging


def test_application_log_redacts_secrets_and_rotates(tmp_path: Path) -> None:
    hugging_face_style_token = "hf_" + "abcdefghijklmnopqrst"
    logger = configure_application_logging(tmp_path, known_secrets={"hf_real_secret"})

    logger.error("Worker reported hf_real_secret and %s", hugging_face_style_token)
    for handler in logger.handlers:
        handler.flush()

    content = (tmp_path / "workcall-transcriber.log").read_text(encoding="utf-8")
    assert "hf_real_secret" not in content
    assert hugging_face_style_token not in content
    assert "ERROR" in content
    logging.shutdown()


def test_application_log_redacts_a_secret_from_an_exception_trace(tmp_path: Path) -> None:
    hugging_face_style_token = "hf_" + "exception_trace_token"
    logger = configure_application_logging(tmp_path, known_secrets={hugging_face_style_token})

    try:
        raise RuntimeError(f"worker rejected {hugging_face_style_token}")
    except RuntimeError:
        logger.exception("Worker failed while authenticating")
    for handler in logger.handlers:
        handler.flush()

    content = (tmp_path / "workcall-transcriber.log").read_text(encoding="utf-8")
    assert hugging_face_style_token not in content
    assert "RuntimeError" in content
    logging.shutdown()
