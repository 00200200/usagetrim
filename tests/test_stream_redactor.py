"""Bounded streaming redaction; fixtures are synthetic, never real credentials."""

import importlib.util
import random
from pathlib import Path

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


@pytest.fixture(scope="module")
def scalar_redactor():
    path = Path(__file__).parent / "fixtures/execution/redactor_reference.py"
    spec = importlib.util.spec_from_file_location("scalar_redactor_reference", path)
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    return reference.ScalarRedactor


def _state(redactor):
    # Buffer segmentation is intentionally different. Observable policy state,
    # byte accounting, decoder carry and publication boundaries must be equal.
    return (
        redactor.incomplete,
        redactor.has_pending_input,
        redactor._line_bytes,
        redactor._terminal,
        redactor._control_length,
        redactor._pem,
        redactor._pending_value,
        redactor._pending_cr,
        redactor._tainted_line,
        redactor._decoder.getstate(),
        redactor._closed,
    )


def _equivalent(scalar_redactor, parts):
    before, after = scalar_redactor(), StreamingRedactor()
    published = []
    for part in parts:
        reference = list(before.feed(part))
        actual = list(after.feed(part))
        assert actual == reference
        assert _state(after) == _state(before)
        assert after._line_bytes <= LINE_LIMIT
        published.extend(actual)
    expected_tail, tail = list(before.finish()), list(after.finish())
    assert tail == expected_tail and _state(after) == _state(before)
    published.extend(tail)
    return "".join(published), after.incomplete


@pytest.mark.parametrize("secret", SECRETS)
def test_scalar_parity_at_every_secret_split(scalar_redactor, secret):
    data = ("ordinary ASCII before\n" + secret + "\nafter\n").encode()
    for split in range(len(data) + 1):
        text, incomplete = _equivalent(scalar_redactor, (data[:split], data[split:]))
        assert not incomplete and MASK in text
        assert secret not in text and "PRIVATE_BODY_EXAMPLE" not in text


@pytest.mark.parametrize("chunk", [1, 3, 7, 4095, 4096, 4097, 8192])
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"plain ASCII\twith spaces\nnext line without newline",
        "Zażółć 日本 😀\nASCII after UTF-8\n".encode(),
        b"one\r\ntwo\rrewritten\n",
        b"bad:\xff\xfe\xf0\x9f\n",
        b"gh\x1b[31mp_" + b"A" * 36 + b"\n",
        b"gh\x1b]title\x1b\\p_" + b"A" * 36 + b"\n",
        "gh\x9b31mp_".encode() + b"A" * 36 + b"\n",
        b"before\nprivate\x1b[?25lNEVER_PUBLISH\n",
        b"before\nprivate\x1b]unfinished",
        b"before\nprivate\x1b[" + b"1" * (CONTROL_LIMIT + 1),
        b"-----BEGIN PRIVATE KEY-----\nPRIVATE_BODY_EXAMPLE",
        b"a" * LINE_LIMIT + b"\n",
        b"a" * (LINE_LIMIT + 1) + b"\nnot published\n",
        b"a" * (LINE_LIMIT - 1) + "🙂\n".encode(),
        ("ż" * (LINE_LIMIT // 2)).encode() + b"\n",
        ("ż" * (LINE_LIMIT // 2)).encode() + b"A\n",
    ],
)
def test_scalar_parity_for_unicode_controls_limits_and_incomplete_data(
    scalar_redactor, chunk, data
):
    _equivalent(
        scalar_redactor, (data[start : start + chunk] for start in range(0, len(data), chunk))
    )


def test_scalar_parity_for_every_byte_and_fixed_seed_mixed_sequences(scalar_redactor):
    for value in range(256):
        data = b"prefix" + bytes([value]) + b"tail\n"
        _equivalent(scalar_redactor, (data[index : index + 1] for index in range(len(data))))
    rng = random.Random(0x141)
    atoms = [
        b"ordinary ASCII ",
        b"\n",
        b"\r",
        b"\t",
        b"\x00",
        b"\x07",
        b"\b",
        b"\x7f",
        b"\x1b[31m",
        b"\x1b[0m",
        b"\x1b[2D",
        b"\x1b]title\x07",
        b"\x1b]",
        b"\x1b\\",
        b"ghp_",
        b"A" * 36,
        b"Bearer ",
        b"\xff\xf0\x9f",
        "漢字🙂".encode(),
    ]
    for _ in range(200):
        data = b"".join(rng.choice(atoms) for _ in range(30))
        chunk = rng.choice([1, 3, 7, 11])
        _equivalent(
            scalar_redactor, (data[start : start + chunk] for start in range(0, len(data), chunk))
        )


def test_ascii_runs_remain_quarantined_and_always_use_record(monkeypatch):
    redactor = StreamingRedactor()
    record = redactor._record
    calls = []

    def checked(newline):
        calls.append(newline)
        return record(newline)

    monkeypatch.setattr(redactor, "_record", checked)
    assert list(redactor.feed(b"safe line")) == []
    assert calls == []
    assert list(redactor.feed(b"\nghp_" + b"A" * 18)) == ["safe line\n"]
    assert list(redactor.feed(b"A" * 18)) == []
    assert list(redactor.feed(b"\n")) == [MASK + "\n"]
    assert list(redactor.finish()) == []
    assert calls == [True, True]
