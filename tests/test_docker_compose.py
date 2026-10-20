"""Synthetic compose logs only: no container engine or external services."""

import pytest

from usagetrim.core import specialized
from usagetrim.metrics.tokenizer import count_tokens


def test_compose_compacts_interleaved_healthchecks_without_losing_crash_context():
    lines = []
    for i in range(200):
        lines.extend(["db-1 | [healthcheck] ping ok", "redis-1 | [healthcheck] ping ok"])
        if i >= 190:
            lines.append(f"api-1 | migration step {i}")
    lines.extend(
        [
            "api-1 | Traceback (most recent call last):",
            "api-1 | Error: cannot connect",
            "api-1 exited with code 1",
        ]
    )
    raw = "\n".join(lines) + "\n"
    result = specialized.auto_specialize_command_output("docker compose up", raw)
    assert result is not None
    assert len(result) < len(raw) * 0.2
    assert count_tokens(result).openai < count_tokens(raw).openai * 0.2
    for line in lines:
        if line.startswith("api-1"):
            assert line in result
    assert "db-1" in result and "redis-1" in result


@pytest.mark.parametrize(
    "command",
    [
        "docker compose up",
        "docker-compose logs",
        "podman compose logs -f",
        "podman-compose up",
        "docker compose -f dev.yml logs",
        "podman compose --project-name demo up",
    ],
)
def test_compose_command_dispatch(command):
    raw = "db_1 | [healthcheck] ping ok\n" * 30
    result = specialized.auto_specialize_command_output(command, raw)
    assert result is not None and len(result) < len(raw)


def test_compose_preserves_errors_and_the_last_ten_service_lines_before_exit():
    lines = ["[api] [healthcheck] ping ok" for _ in range(30)]
    lines.extend(["[api] ERROR failed healthcheck"] * 3)
    lines.append("api exited with code 2")
    raw = "\n".join(lines)
    result = specialized.filter_docker_compose(raw)
    assert result.count("ERROR failed healthcheck") == 3
    assert result.count("[api] [healthcheck] ping ok") >= 7
    assert "\n".join(lines[-11:]) in result


def test_compose_unknown_lines_and_short_inputs_are_unchanged():
    for raw in [
        "",
        "plain output\n",
        "db-1 | arbitrary repeated data\n" * 20,
        "db-1 | [healthcheck] ping ok\n" * 2,
    ]:
        assert specialized.filter_docker_compose(raw) == raw


def test_compose_does_not_merge_healthchecks_across_errors():
    raw = (
        "db-1 | [healthcheck] ping ok\n" * 10
        + "db-1 | ERROR lost connection\n"
        + "db-1 | [healthcheck] ping ok\n" * 10
    )
    result = specialized.filter_docker_compose(raw)
    assert result.count("[healthcheck] ping ok") == 2
    assert result.count("ERROR lost connection") == 1
    assert specialized.filter_docker_compose(result) == result


@pytest.mark.parametrize(
    "command",
    [
        "echo docker compose up",
        "docker compose exec web echo ping",
        "docker compose config",
        "docker compose logs; echo unsafe",
        "docker-compose --unknown logs",
    ],
)
def test_unrelated_or_ambiguous_commands_do_not_match_compose(command):
    assert not specialized._is_docker_compose_log_command(command)


def test_compose_http_and_redis_heartbeats_preserve_failures_and_crlf():
    lines = []
    for i in range(40):
        lines.extend(
            [
                f"redis-1 | 1:M 27 Sep 2026 12:00:{i:02}.000 * 10000 keys in 16 shards",
                f'web-1 | 127.0.0.1 - - [27/Sep/2026:12:00:{i:02} +0000] "GET /ping HTTP/1.1" 200 4',
            ]
        )
    lines.extend(['web-1 | "GET /ping HTTP/1.1" 500 4', "redis-1 | ERROR heartbeat failed"])
    result = specialized.filter_docker_compose("\r\n".join(lines) + "\r\n")
    assert len(result.splitlines()) == 6
    assert lines[-2] in result and lines[-1] in result
    assert "\n" not in result.replace("\r\n", "")


def test_compose_keeps_other_service_errors_in_original_order():
    raw = "db-1 | [healthcheck] ping ok\napi-1 | ERROR first\ndb-1 | [healthcheck] ping ok\napi-1 | ERROR second\ndb-1 | [healthcheck] ping ok\n"
    result = specialized.filter_docker_compose(raw)
    assert result.index("ERROR first") < result.index("ERROR second")
    assert result.count("ERROR") == 2


def test_compose_does_not_treat_a_quoted_health_request_in_an_exception_as_success():
    raw = 'api-1 | ValueError: "GET /ping HTTP/1.1" 200 4\n' * 4
    assert specialized.filter_docker_compose(raw) == raw
