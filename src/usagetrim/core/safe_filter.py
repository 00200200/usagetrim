"""Conservative, recoverable filtering for automatic command-output routing."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePath

from usagetrim.core.cache import ContextCache
from usagetrim.core.redactor import redact_secrets
from usagetrim.metrics.tokenizer import count_tokens

# Avoid cache notices and extra work for already small command results.
_MIN_TOKENS = 256
_MAX_DIAG_LINE = 220
_COLOR = re.compile(r"\x1b\[[0-9;]*m")
_PYTHON = re.compile(r"python(?:\d+(?:\.\d+)*)?")
_PASSED = re.compile(
    # xdist -v may print ``[gw0] PASSED path::test`` (docs) or include ``[ N%]``.
    r"(?:\[gw\d+\]\s+(?:\[\s*\d+%\]\s+)?PASSED\s+\S+\.py::\S+"
    r"|\S+\.py::\S+\s+PASSED(?:\s+\[\s*\d+%\])?)\s*"
)
_DOTS = re.compile(r"(?:\S+\.py\s+)?(?P<dots>\.+)\s+\[\s*\d+%\]\s*")
_DIAGNOSTIC = re.compile(
    r"\b(?:traceback|error|errors|failed|failure|failures|warning|warnings|"
    r"exception|assertionerror|assert|fatal|panic)\b|^\s*[EF]\s+",
    re.IGNORECASE,
)
# xdist worker registration / node announcements (pre-test noise).
_XDIST_NODE = re.compile(r"^gw\d+\s+[A-Z]\s+\S+")
_CAPTURED_HEADER = re.compile(
    r"^-{4,}\s*Captured (stdout|stderr|log)(?:\s+\w+)?\s*-{4,}\s*$",
    re.IGNORECASE,
)
_SECTION_BOUNDARY = re.compile(
    r"^(?:[=_]{5,}|-{4,}\s*Captured\b|={5,}.*={5,})",
    re.IGNORECASE,
)
_PY_LOCATION = re.compile(r"^\S+\.py:\d+")
_HYPOTHESIS = re.compile(r"^Falsifying example:")
_FRAME_LINE = re.compile(r'^\s*File ".+", line \d+, in \S+\s*$')


def _pytest_command(command: str) -> bool:
    """Recognize direct invocations only; never guess what a shell pipeline runs."""
    if not command or "\n" in command:
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words or any(re.search(r"[|;&<>`$]", word) for word in words):
        return False
    if words[:2] == ["uv", "run"]:
        words = words[2:]
    if not words:
        return False
    executable = PurePath(words[0]).name
    return executable in {"pytest", "py.test"} or (
        bool(_PYTHON.fullmatch(executable)) and words[1:3] == ["-m", "pytest"]
    )


def _pytest_session(lines: list[str]) -> bool:
    """Require a whole pytest session shape when the command is unavailable."""
    nonempty = [_COLOR.sub("", line).strip() for line in lines if line.strip()]
    return bool(
        nonempty
        and re.fullmatch(r"=+ test session starts =+", nonempty[0])
        and any(re.fullmatch(r"collected \d+ items?.*", line) for line in nonempty[1:8])
        and re.fullmatch(r"=+ .+ in [0-9.]+s(?: \([^\n]*\))? =+", nonempty[-1])
    )


def _plain(line: str) -> str:
    return _COLOR.sub("", line.rstrip("\r\n"))


def _passed_count(line: str) -> int:
    line = _plain(line)
    if _PASSED.fullmatch(line):
        return 1
    match = _DOTS.fullmatch(line)
    return len(match["dots"]) if match else 0


def _truncate_diag_line(line: str) -> str:
    """Keep the lead of oversized assertion/hypothesis repr lines."""
    plain = _plain(line)
    if len(plain) <= _MAX_DIAG_LINE:
        return line
    ending = "\n" if line.endswith("\n") else ("\r\n" if line.endswith("\r\n") else "")
    kept = plain[:_MAX_DIAG_LINE].rstrip()
    omitted = len(plain) - len(kept)
    return f"{kept}… [UsageTrim: truncated {omitted} chars]{ending}"


def _compact_pytest_diagnostic(lines: list[str]) -> tuple[list[str], bool]:
    """Fold pytest failure noise while keeping the actionable failure signal.

    Captured stdout/stderr/log bodies, oversized hypothesis examples, recursive
    traceback frames, and extreme assertion repr lines are collapsed. Headers,
    locations, exception types, and the short summary stay.
    """
    result: list[str] = []
    changed = False
    i = 0
    while i < len(lines):
        plain = _plain(lines[i])
        stripped = plain.strip()

        if _CAPTURED_HEADER.match(stripped):
            result.append(lines[i] if lines[i].endswith(("\n", "\r")) else lines[i] + "\n")
            i += 1
            start = i
            while i < len(lines):
                nxt = _plain(lines[i]).strip()
                if _CAPTURED_HEADER.match(nxt) or _SECTION_BOUNDARY.match(nxt):
                    break
                i += 1
            omitted = i - start
            if omitted:
                result.append(f"[UsageTrim: {omitted} captured lines omitted]\n")
                changed = True
            continue

        if _HYPOTHESIS.match(stripped):
            result.append(_truncate_diag_line(lines[i]))
            if result[-1] != lines[i]:
                changed = True
            i += 1
            start = i
            while i < len(lines):
                nxt = _plain(lines[i]).strip()
                if (
                    not nxt
                    or _PY_LOCATION.match(nxt)
                    or _SECTION_BOUNDARY.match(nxt)
                    or _CAPTURED_HEADER.match(nxt)
                    or nxt.startswith("E ")
                    or nxt.startswith(">")
                ):
                    break
                i += 1
            omitted = i - start
            if omitted:
                result.append(f"[UsageTrim: {omitted} falsifying-example lines omitted]\n")
                changed = True
            continue

        if _FRAME_LINE.match(plain):
            end = i + 1
            while end < len(lines) and _plain(lines[end]) == plain:
                end += 1
            repeats = end - i
            if repeats > 3:
                result.append(lines[i] if lines[i].endswith(("\n", "\r")) else lines[i] + "\n")
                result.append(
                    f"[UsageTrim: identical traceback frame repeated {repeats - 1} more times]\n"
                )
                changed = True
                i = end
                continue

        trimmed = _truncate_diag_line(lines[i])
        if trimmed != lines[i]:
            changed = True
        result.append(trimmed)
        i += 1

    return result, changed


def recovery_notice(ref_id: str) -> str:
    """Return the standard full-output recovery notice, including its newline."""
    return f"[UsageTrim: full output after redaction: usagetrim retrieve {ref_id}]\n"


def safe_compact_output(text: str, *, command: str = "", exit_code: int | None = None) -> str:
    """Filter routine records without discarding actionable failure signal.

    Recognized pytest pass records, xdist worker announcements, and exact
    contiguous duplicate lines are compacted. On pytest output, diagnostic
    sections keep the failure headers, locations, and exception text, but fold
    captured I/O, oversized hypothesis examples, recursive frames, and extreme
    repr lines. Non-pytest diagnostics stay verbatim. Exit status is never
    inferred from pass records; callers retain ``exit_code`` separately. The
    redacted original is cached before a shorter result is used. Any
    cache/tokenizer failure returns the original after best-effort redaction.
    Counts are local estimates, not model billing or subscription usage.
    """
    original = redact_secrets(text)
    if not original:
        return original
    try:
        original_tokens = count_tokens(original).openai
    except Exception:
        return original
    if original_tokens < _MIN_TOKENS:
        return original

    lines = original.splitlines(keepends=True)
    pytest_output = _pytest_command(command) or _pytest_session(lines)
    result: list[str] = []
    i = 0
    changed = False
    while i < len(lines):
        line = lines[i]
        plain = _plain(line)

        if _DIAGNOSTIC.search(plain):
            if pytest_output:
                compacted_tail, tail_changed = _compact_pytest_diagnostic(lines[i:])
                result.extend(compacted_tail)
                changed = changed or tail_changed
            else:
                # Non-pytest: never reinterpret anything inside/after diagnostics.
                result.extend(lines[i:])
            break

        if pytest_output and _XDIST_NODE.match(plain.strip()):
            end = i + 1
            while end < len(lines) and _XDIST_NODE.match(_plain(lines[end]).strip()):
                end += 1
            count = end - i
            result.append(f"[UsageTrim: {count} xdist worker records]\n")
            changed = True
            i = end
            continue

        passed = _passed_count(line) if pytest_output else 0
        if passed:
            end = i + 1
            while end < len(lines):
                if _DIAGNOSTIC.search(_COLOR.sub("", lines[end])):
                    break
                count = _passed_count(lines[end])
                if not count:
                    break
                passed += count
                end += 1
            result.append(f"[UsageTrim: {passed} passing tests, {end - i} progress records]\n")
            changed = True
            i = end
            continue

        end = i + 1
        while end < len(lines) and lines[end] == line:
            end += 1
        result.append(line)
        if end - i > 1 and line.strip():
            if not line.endswith(("\n", "\r")):
                result.append("\n")
            result.append(f"[UsageTrim: preceding line repeated {end - i} times total]\n")
            changed = True
        else:
            result.extend(lines[i + 1 : end])
        i = end

    if not changed:
        return original
    compact = "".join(result)
    try:
        # Avoid writing cache entries for candidates that cannot possibly save.
        if count_tokens(compact).openai >= original_tokens:
            return original
        cache = ContextCache()
        ref = cache.store(original, source="safe-command-output")
        separator = "" if compact.endswith(("\n", "\r")) else "\n"
        compact += separator + recovery_notice(ref)
        if count_tokens(compact).openai >= original_tokens:
            return original
        return compact
    except Exception:
        # Filtering must not turn disk, database, or tokenizer problems into
        # missing command output. The caller still receives complete diagnostics.
        return original
