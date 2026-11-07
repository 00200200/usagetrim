"""Unit tests for the stdlib secret redactor."""

from __future__ import annotations

import time

import pytest

from usagetrim.core.redactor import redact_secrets


@pytest.mark.parametrize(
    "escape", ["\x1b[31m", "\x1b[?25l", "\x1b]0;title\x07", "\x9b31m", "\x9d0;title\x9c"]
)
def test_redacts_secrets_across_ansi_without_removing_unrelated_escapes(escape):
    raw = "\x1b[32mstatus\x1b[0m\n" + "ghp_" + escape + "A" * 36 + "\nend"
    result = redact_secrets(raw)
    assert result == "\x1b[32mstatus\x1b[0m\n[REDACTED_GITHUB_TOKEN]\nend"
    assert redact_secrets(result) == result


def test_redacts_multiple_ansi_split_credentials_with_correct_offsets():
    raw = (
        "prefix ghp_\x1b[31m"
        + "A" * 18
        + "\x1b[0m"
        + "A" * 18
        + " next sk-proj-\x1b[32m"
        + "b" * 30
        + " suffix"
    )
    output = redact_secrets(raw)
    assert output == "prefix [REDACTED_GITHUB_TOKEN] next [REDACTED_OPENAI_KEY] suffix"


def test_redactor_preserves_terminal_bytes_when_no_secret_exists():
    raw = "\x1b[31mred\x1b[0m\n\x1b]0;title\x07plain"
    assert redact_secrets(raw) == raw


def test_redacts_github_tokens():
    ghp = "ghp_" + "A" * 36
    pat = "github_pat_" + "B" * 40
    gho = "gho_" + "C" * 36
    raw = f"tokens: {ghp} {pat} {gho}"
    out = redact_secrets(raw)
    assert ghp not in out
    assert pat not in out
    assert gho not in out
    assert out.count("[REDACTED_GITHUB_TOKEN]") == 3


def test_redacts_openai_and_anthropic():
    openai_proj = "sk-proj-" + "x" * 30
    openai_admin = "sk-admin-" + "y" * 30
    openai_legacy = "sk-" + "z" * 48
    anthropic = "sk-ant-api03-" + "w" * 40
    raw = f"{openai_proj}\n{openai_admin}\n{openai_legacy}\n{anthropic}"
    out = redact_secrets(raw)
    assert openai_proj not in out
    assert openai_admin not in out
    assert openai_legacy not in out
    assert anthropic not in out
    assert "[REDACTED_OPENAI_KEY]" in out
    assert "[REDACTED_ANTHROPIC_KEY]" in out
    # Anthropic must not be misclassified as OpenAI
    assert out.count("[REDACTED_ANTHROPIC_KEY]") == 1


def test_redacts_aws_akia_and_session():
    akia = "AKIAIOSFODNN7EXAMPLE"
    asia = "ASIAIOSFODNN7EXAMPLE"
    session = "AQoE" + ("a" * 120)
    raw = f"key={akia} temp={asia} session={session}"
    out = redact_secrets(raw)
    assert akia not in out
    assert asia not in out
    assert session not in out
    assert "[REDACTED_AWS_KEY]" in out
    assert "[REDACTED_AWS_SESSION]" in out


def test_redacts_slack_pem_bearer_jwt_gemini():
    slack = "xoxb-1234567890-abcdefghij"
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF6PZX6W+examplepembody==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    bearer = "Bearer FAKESECRET_g1h2i3j4k5l6m7n8o9p0"
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    gemini = "AIza" + ("Sy" + "D" * 33)  # AIza + 35 chars
    assert len(gemini) == 39
    raw = f"{slack}\n{pem}\nAuthorization: {bearer}\n{jwt}\n{gemini}"
    out = redact_secrets(raw)
    assert slack not in out
    assert "PRIVATE KEY-----" not in out or "[REDACTED_PEM]" in out
    assert "MIIEowIBAAKCAQEA" not in out
    assert "ya29.a0AfH6SMB" not in out
    assert jwt not in out
    assert gemini not in out
    assert "[REDACTED_SLACK_TOKEN]" in out
    assert "[REDACTED_PEM]" in out
    assert "Bearer [REDACTED_BEARER]" in out
    assert "[REDACTED_JWT]" in out
    assert "[REDACTED_GEMINI_KEY]" in out


def test_leaves_ordinary_code_and_short_words_alone():
    code = """
def ski_trip():
    return "sk-"  # incomplete stub, not a key

access = "AKIA"  # prefix only
slackish = "xox"
gh = "ghp_short"
uuid = "550e8400-e29b-41d4-a716-446655440000"
sha = "a" * 40
b64 = "SGVsbG8gV29ybGQh"  # short base64
print("hello world")
"""
    assert redact_secrets(code) == code


def test_leaves_already_redacted_placeholders_alone():
    text = "before [REDACTED_GITHUB_TOKEN] mid [REDACTED:github] after [REDACTED_OPENAI_KEY]"
    assert redact_secrets(text) == text
    # Idempotent on previously scrubbed agent output
    once = redact_secrets("ghp_" + "A" * 36)
    assert redact_secrets(once) == once


def test_false_positives_uuid_sha_base64():
    blob = "\n".join(
        [
            "id=550e8400-e29b-41d4-a716-446655440000",
            "sha256=" + ("f" * 64),
            "digest=" + ("Ab0/" * 20),  # generic base64, not JWT/AWS session
            "commit=deadbeefcafebabe0123456789abcdef01234567",
        ]
    )
    assert redact_secrets(blob) == blob


def test_empty_and_passthrough():
    assert redact_secrets("") == ""
    assert redact_secrets("plain log line without secrets") == "plain log line without secrets"


def test_redaction_latency_under_5ms_on_50kb():
    # Mostly-benign 50KB buffer: exercises the needle-gated fast path that agent
    # logs hit in practice. A couple of embedded secrets still get scrubbed.
    line = "INFO ok uuid=550e8400-e29b-41d4-a716-446655440000 sha=" + ("a" * 40) + "\n"
    text = line * ((50_000 // len(line.encode("utf-8"))) + 1)
    text = text[:50_000] + "\nleak ghp_" + "Z" * 36 + "\nleak sk-proj-" + "q" * 40 + "\n"
    assert len(text.encode("utf-8")) >= 50_000

    # Best-of-N after warm-up: CI hosts are noisy; require a sub-5ms sample.
    redact_secrets(text)
    best_ms = float("inf")
    out = text
    for _ in range(21):
        start = time.perf_counter()
        out = redact_secrets(text)
        best_ms = min(best_ms, (time.perf_counter() - start) * 1000)
    assert "[REDACTED_GITHUB_TOKEN]" in out
    assert "[REDACTED_OPENAI_KEY]" in out
    assert best_ms < 5.0, f"best redaction took {best_ms:.2f} ms on ~50KB"
