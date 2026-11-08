"""Bounded streaming redaction; fixtures are synthetic, never real credentials."""

import pytest

from usagetrim.core.stream_redactor import CONTROL_LIMIT, LINE_LIMIT, MASK, StreamingRedactor

SECRETS = [
    "ghp_" + "A" * 36,
    "github_pat_" + "B" * 200,
    "sk-proj-" + "C" * 200,
    "sk-ant-api03-" + "D" * 200,
    "Bearer " + "E" * 200,
    "bEaReR\n\n" + "F" * 200,
    "AKIA" + "G" * 16,
    "AWS_SECRET_ACCESS_KEY=" + "H" * 40,
    "FwoG" + "I" * 200,
    "eyJ" + "J" * 20 + ".eyJ" + "K" * 20 + "." + "L" * 20,
    "eyAidHlwIjoiSldUIiwiYWxnIjoiSFMyNTYifQ.e30.synthetic_signature",
    "eyAidHlwIjoiSldUIiwiYWxnIjoiSFMyNTYifQ.e30.synthetic_signature.",
    "postgresql://user:password@localhost/db",
    "mongodb+srv://user:password@example/db",
    "CF_API_TOKEN\n\n" + "M" * 60,
    "-----BEGIN RSA PRIVATE KEY-----\nPRIVATE_BODY_EXAMPLE\n-----END RSA PRIVATE KEY-----",
]


def redact(parts):
    redactor = StreamingRedactor()
    output = []
    for part in parts:
        output.extend(redactor.feed(part))
        assert redactor._line_bytes <= LINE_LIMIT
    output.extend(redactor.finish())
    return "".join(output), redactor.incomplete


@pytest.mark.parametrize("secret", SECRETS)
def test_every_fragment_boundary_is_safe(secret):
    payload = ("safe prefix\n" + secret + "\nend\n").encode()
    expected, _ = redact([payload])
    for split in range(len(payload) + 1):
        actual, incomplete = redact([payload[:split], payload[split:]])
        assert actual == expected
        assert not incomplete
        assert secret not in actual
        assert "PRIVATE_BODY_EXAMPLE" not in actual
        assert MASK in actual and actual.startswith("safe prefix\n")


@pytest.mark.parametrize(
    "escape", ["\x1b[31m", "\x1b]0;title\x07", "\x1b]0;title\x1b\\", "\x9b31m"]
)
def test_terminal_sequences_cannot_hide_a_token_prefix(escape):
    text = "gh" + escape + "p_" + "A" * 36 + "\n"
    data = text.encode()
    output, incomplete = redact([bytes([byte]) for byte in data])
    assert output == MASK + "\n" and not incomplete
    assert "A" * 36 not in output and "\x1b" not in output


@pytest.mark.parametrize("text", ["", "plain", "one\ntwo\n", " \t\n", "Zażółć 漢字🙂\n"])
def test_clean_text_preserves_no_newline_and_unicode(text):
    data = text.encode()
    assert redact([bytes([byte]) for byte in data]) == (text, False)


def test_invalid_utf8_and_progress_controls_have_a_defined_view():
    raw = b"bad \xff\xf0\x9f\nred\x1b[31m\x1b[0m\rprogress\n"
    expected = "bad ��\n" + MASK + "\n"
    assert redact([bytes([byte]) for byte in raw]) == (expected, False)


def test_crlf_is_not_confused_with_a_terminal_rewrite():
    assert redact([b"normal\r", b"\nnext\r\n"]) == ("normal\nnext\n", False)


@pytest.mark.parametrize("control", [b"\x1b[2D", b"\x1b[?25l", b"\x1bP", b"\x0b"])
def test_unsupported_terminal_editing_fails_closed(control):
    output, incomplete = redact([b"safe\npending" + control + b"NEVER_PUBLISH\n"])
    assert incomplete and output.startswith("safe\n")
    assert "pending" not in output and "NEVER_PUBLISH" not in output


@pytest.mark.parametrize("control", [b"\r", b"\b"])
def test_rewrite_lines_cannot_publish_a_credential_body(control):
    output, incomplete = redact([b"hidden token body" + control + b"new prefix\n"])
    assert not incomplete and output == MASK + "\n"


def test_unbounded_secret_or_line_fails_closed_for_the_remaining_capture():
    redactor = StreamingRedactor()
    assert list(redactor.feed(b"safe\n")) == ["safe\n"]
    for _ in range(LINE_LIMIT + 5):
        assert list(redactor.feed(b"x")) == []
    assert redactor.incomplete and redactor._line_bytes == 0
    assert list(redactor.feed(b"-----BEGIN PRIVATE KEY-----\nNEVER_PUBLISH_ME\n")) == []
    tail = "".join(redactor.finish())
    assert "unavailable" in tail and "NEVER_PUBLISH_ME" not in tail


@pytest.mark.parametrize(
    "ending", [b"\x1b[", b"\x1b]unterminated", b"\x1b[" + b"1" * CONTROL_LIMIT]
)
def test_unfinished_or_oversized_terminal_syntax_is_not_complete(ending):
    output, incomplete = redact([b"safe\nprivate pending" + ending])
    assert incomplete and output.startswith("safe\n")
    assert "private pending" not in output


def test_unterminated_private_key_never_exposes_its_body():
    output, incomplete = redact([b"-----BEGIN PRIVATE KEY-----\nPRIVATE_BODY_EXAMPLE"])
    assert incomplete and "PRIVATE_BODY_EXAMPLE" not in output


def test_input_and_lifecycle_are_explicit():
    redactor = StreamingRedactor()
    with pytest.raises(TypeError):
        list(redactor.feed("not bytes"))
    list(redactor.finish())
    with pytest.raises(ValueError, match="closed"):
        list(redactor.feed(b"later"))
