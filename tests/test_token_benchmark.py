"""Reference requests are mocked; these tests never need credentials or network."""

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from usagetrim.metrics import token_benchmark as benchmark


def test_import_and_default_command_never_contact_api(monkeypatch, capsys):
    import runpy
    import urllib.request

    def forbidden(*args, **kwargs):
        pytest.fail("implicit network access")

    monkeypatch.setattr(urllib.request, "build_opener", forbidden)
    namespace = runpy.run_path(benchmark.__file__)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-must-not-enable-network")
    namespace["main"]([])
    output = capsys.readouterr().out
    assert "secret-must-not-enable-network" not in output
    assert all(row["reference"] is None for row in json.loads(output)["samples"])


def install_transport(monkeypatch, payload=b'{"input_tokens": 42}', error=None):
    requests = []

    class Transport:
        def open(self, request, *, timeout):
            requests.append(request)
            assert timeout == 20
            if error is not None:
                raise error
            return io.BytesIO(payload)

    monkeypatch.setattr(benchmark, "build_opener", lambda *handlers: Transport())
    return requests


def test_offline_report_is_deterministic_and_has_no_fabricated_references(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("implicit network access")

    monkeypatch.setattr(benchmark, "build_opener", forbidden)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-activate-network")
    first = benchmark.benchmark_fallbacks()
    assert first == benchmark.benchmark_fallbacks()
    assert {row["sample"] for row in first["samples"]} == {
        "python",
        "json",
        "markdown",
        "prose",
        "whitespace-heavy",
        "unicode",
        "mixed",
    }
    for row in first["samples"]:
        assert row["reference"] is None
        assert row["errors_vs_contextual_increment"] is None
        assert row["estimates"]["utf8-bytes-div4-ceil"] == (row["utf8_bytes"] + 3) // 4


def test_reference_unicode_request_and_response(monkeypatch):
    requests = install_transport(monkeypatch)
    result = benchmark.count_anthropic_message_tokens(
        "Zażółć 漢字 🦙", model="claude-test-model", api_key="secret-test-key"
    )
    assert result == 42
    request = requests[0]
    assert request.full_url == benchmark.ENDPOINT
    assert request.get_method() == "POST"
    assert request.get_header("X-api-key") == "secret-test-key"
    assert request.get_header("Anthropic-version") == "2023-06-01"
    body = json.loads(request.data.decode("utf-8"))
    assert body == {
        "model": "claude-test-model",
        "messages": [{"role": "user", "content": "Zażółć 漢字 🦙"}],
    }


@pytest.mark.parametrize("key", [None, "", "  ", "secret\r\ninjected"])
def test_missing_or_invalid_key_precedes_network(monkeypatch, key):
    requests = install_transport(monkeypatch)
    with pytest.raises(benchmark.ReferenceCountError):
        benchmark.count_anthropic_message_tokens("sample", model="claude-test", api_key=key)
    assert requests == []


@pytest.mark.parametrize("model", ["claude", "claude-", "", "claude-test\ninvalid"])
def test_reference_requires_concrete_model(monkeypatch, model):
    requests = install_transport(monkeypatch)
    with pytest.raises(benchmark.ReferenceCountError, match="concrete"):
        benchmark.count_anthropic_message_tokens("sample", model=model, api_key="secret")
    assert requests == []


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_errors_do_not_expose_credentials_or_response_body(monkeypatch, status):
    secret = "secret-test-key"
    error = HTTPError(benchmark.ENDPOINT, status, secret, {}, io.BytesIO(secret.encode()))
    install_transport(monkeypatch, error=error)
    with pytest.raises(benchmark.ReferenceCountError) as caught:
        benchmark.count_anthropic_message_tokens("sample", model="claude-test", api_key=secret)
    assert str(status) in str(caught.value)
    assert secret not in str(caught.value)
    assert caught.value.__suppress_context__


def test_connection_error_is_sanitized(monkeypatch):
    install_transport(monkeypatch, error=URLError("secret-test-key"))
    with pytest.raises(benchmark.ReferenceCountError, match="unavailable") as caught:
        benchmark.count_anthropic_message_tokens(
            "sample", model="claude-test", api_key="secret-test-key"
        )
    assert "secret-test-key" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"invalid",
        b"[]",
        b"{}",
        b'{"input_tokens":true}',
        b'{"input_tokens":-1}',
        b'{"input_tokens":"42"}',
        b"x" * 65537,
    ],
)
def test_invalid_reference_responses(monkeypatch, payload):
    install_transport(monkeypatch, payload=payload)
    with pytest.raises(benchmark.ReferenceCountError):
        benchmark.count_anthropic_message_tokens("sample", model="claude-test", api_key="secret")


def test_redirect_cannot_forward_api_key():
    with pytest.raises(benchmark.ReferenceCountError, match="redirects"):
        benchmark._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org")


def test_report_subtracts_message_baseline_and_labels_contextual_errors(monkeypatch):
    def reference(text, *, model, api_key):
        # Synthetic transport values test arithmetic only, not tokenizer accuracy.
        return 12 if text == benchmark.REFERENCE_PREFIX else 32

    monkeypatch.setattr(benchmark, "count_anthropic_message_tokens", reference)
    report = benchmark.benchmark_fallbacks(anthropic_model="claude-test", api_key="secret")
    assert "secret" not in json.dumps(report)
    for row in report["samples"]:
        assert row["reference"]["api_message_input_tokens"] == 32
        assert row["reference"]["baseline_message_input_tokens"] == 12
        assert row["reference"]["contextual_increment"] == 20
        for method, count in row["estimates"].items():
            errors = row["errors_vs_contextual_increment"][method]
            assert errors["absolute_error_tokens"] == abs(count - 20)
            assert errors["relative_error"] == abs(count - 20) / 20


def test_nonpositive_reference_delta_has_no_relative_error(monkeypatch):
    monkeypatch.setattr(benchmark, "count_anthropic_message_tokens", lambda *args, **kwargs: 12)
    report = benchmark.benchmark_fallbacks(anthropic_model="claude-test", api_key="secret")
    assert all(row["errors_vs_contextual_increment"] is None for row in report["samples"])


def test_cli_requires_explicit_network_opt_in_and_key(monkeypatch, capsys):
    requests = install_transport(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for args in [
        ["--anthropic-model", "claude-test"],
        ["--fetch-anthropic-reference"],
        ["--fetch-anthropic-reference", "--anthropic-model", "claude-test"],
    ]:
        with pytest.raises(SystemExit) as caught:
            benchmark.main(args)
        assert caught.value.code == 2
    assert requests == []
    assert "Traceback" not in capsys.readouterr().err
