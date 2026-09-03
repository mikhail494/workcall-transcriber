from pathlib import Path

from workcall_transcriber.security import CredentialStore, redact_text


def test_dpapi_credential_store_never_writes_a_plaintext_token(tmp_path: Path) -> None:
    credentials = CredentialStore(tmp_path / "credentials.dat")
    synthetic_token = "hf_" + "synthetic_secret_for_test_only"

    credentials.save_token(synthetic_token)

    assert credentials.is_configured() is True
    assert credentials.load_token() == synthetic_token
    assert synthetic_token.encode() not in (tmp_path / "credentials.dat").read_bytes()

    credentials.remove_token()

    assert credentials.is_configured() is False
    assert credentials.load_token() is None


def test_redaction_removes_known_and_hugging_face_style_secrets() -> None:
    hugging_face_style_token = "hf_" + "abcdefghijklmnopqrst"
    text = f"failure {hugging_face_style_token} known-super-secret"

    redacted = redact_text(text, {"known-super-secret"})

    assert hugging_face_style_token not in redacted
    assert "known-super-secret" not in redacted
