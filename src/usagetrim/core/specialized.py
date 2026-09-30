from __future__ import annotations

import json
import re
import shlex
from pathlib import PurePath

from usagetrim.core.cache import ContextCache
from usagetrim.core.diff_slimmer import slim_git_diff
from usagetrim.core.json_slimmer import slim_json, slim_json_data
from usagetrim.core.spill import spill_large_output

# `git log` indents commit messages by exactly four spaces. Patch bodies (-p) and
# --stat blocks sit at other indents, so they must be detected explicitly instead
# of being mistaken for message text.
_COMMIT_RE = re.compile(r"^commit ([0-9a-f]{7,40})\b")
_DIFF_RE = re.compile(r"^diff --(?:git|cc|combined) ")
_STAT_FILE_RE = re.compile(r"^ \S.*\|\s+(?:\d+|Bin)\b")
_STAT_SUMMARY_RE = re.compile(r"^ \d+ files? changed")


def _starts_payload(line: str) -> bool:
    """Detect the first line of a --stat block or a -p patch body."""
    return bool(_DIFF_RE.match(line) or _STAT_FILE_RE.match(line) or _STAT_SUMMARY_RE.match(line))


def _compact_payload(payload_lines: list[str], max_context_lines: int) -> str:
    """Compact a commit's --stat/-p payload instead of discarding it.

    Stat blocks are already dense and are kept verbatim; patch bodies are routed
    through the shared diff compactor so added and removed lines survive.
    """
    diff_start = next((i for i, line in enumerate(payload_lines) if _DIFF_RE.match(line)), None)
    if diff_start is None:
        return "\n".join(payload_lines).strip("\n")

    stat_part = "\n".join(payload_lines[:diff_start]).strip("\n")
    diff_part = "\n".join(payload_lines[diff_start:])
    slimmed = slim_git_diff(diff_part, max_context_lines=max_context_lines).strip("\n")
    return f"{stat_part}\n{slimmed}".strip("\n") if stat_part else slimmed


def filter_git_log(raw_log: str, max_commits: int = 15, max_context_lines: int = 2) -> str:
    """Compress verbose git log into dense 1-line format, saving ~75% tokens.

    Patch bodies (`-p`) and stat blocks (`--stat`) are compacted rather than
    dropped, and never leak into the commit message.
    """
    if not raw_log.strip():
        return raw_log

    lines = raw_log.splitlines()
    commits: list[str] = []
    current_hash = ""
    current_author = ""
    current_msg: list[str] = []
    current_payload: list[str] = []
    in_payload = False

    def flush_commit():
        nonlocal current_hash, current_author, current_msg, current_payload, in_payload
        if current_hash:
            msg = " ".join(current_msg).strip()
            author_short = current_author.split("<")[0].strip() if current_author else ""
            entry = f"{current_hash[:7]} [{author_short}] {msg}".rstrip()
            payload = _compact_payload(current_payload, max_context_lines)
            if payload:
                entry = f"{entry}\n{payload}"
            commits.append(entry)
        current_hash = ""
        current_author = ""
        current_msg = []
        current_payload = []
        in_payload = False

    for line in lines:
        commit_match = _COMMIT_RE.match(line)
        if commit_match:
            # Unified diff bodies always prefix their lines, so a bare `commit <sha>`
            # at column zero reliably terminates the previous commit's payload.
            flush_commit()
            current_hash = commit_match.group(1)
        elif in_payload:
            current_payload.append(line)
        elif _starts_payload(line):
            in_payload = True
            current_payload.append(line)
        elif line.startswith("Author:"):
            current_author = line.replace("Author:", "").strip()
        elif line.startswith("Date:"):
            continue
        elif line.startswith("    "):
            current_msg.append(line.strip())

    flush_commit()

    if len(commits) > max_commits:
        omitted = len(commits) - max_commits
        return "\n".join(
            commits[:max_commits] + [f"[... {omitted} older commits omitted by usagetrim ...]"]
        )
    return "\n".join(commits) if commits else raw_log


def filter_git_status(raw_status: str) -> str:
    """Group and condense massive untracked file listings in git status."""
    lines = raw_status.splitlines()
    untracked_dirs: dict[str, int] = {}
    cleaned_lines: list[str] = []
    in_untracked = False

    for line in lines:
        stripped = line.strip()
        if "Untracked files:" in line:
            in_untracked = True
            cleaned_lines.append(line)
            continue

        if in_untracked:
            if stripped.startswith("(") and stripped.endswith(")"):
                continue
            if not stripped:
                in_untracked = False
                # Flush untracked summary
                if untracked_dirs:
                    for d, count in untracked_dirs.items():
                        cleaned_lines.append(f"\t{d}/ ({count} untracked files)")
                    untracked_dirs = {}
                cleaned_lines.append(line)
                continue

            # Check if untracked line has a folder
            parts = stripped.split("/", 1)
            if len(parts) > 1:
                top_dir = parts[0]
                untracked_dirs[top_dir] = untracked_dirs.get(top_dir, 0) + 1
            else:
                cleaned_lines.append(line)
        else:
            cleaned_lines.append(line)

    if untracked_dirs:
        for d, count in untracked_dirs.items():
            cleaned_lines.append(f"\t{d}/ ({count} untracked files)")

    return "\n".join(cleaned_lines)


def filter_git_diff(raw_output: str, max_context_lines: int = 2) -> str:
    """Compact ``git diff`` / ``git show`` payloads via the shared diff slimmer.

    Lockfiles and generated artifacts collapse to a one-line notice; added and
    removed lines in source hunks are preserved. Non-diff output is untouched.
    """
    if not raw_output.strip() or "diff --git" not in raw_output:
        return raw_output
    return slim_git_diff(raw_output, max_context_lines=max_context_lines)


# Cargo / nextest print one progress line per passing test. Collapse those hard,
# keep fail/pass identity + test names + assertion lines, and drop stack frames.
_NEXTEST_PASS = re.compile(r"^PASS\s+\[\s*[0-9.]+s\]\s+.+")
_CARGO_PASS_LINE = re.compile(r"^(?:test \S+ \.\.\. ok|PASS\s+\[\s*[0-9.]+s\]\s+.+)$")
_CARGO_FAILED_LINE = re.compile(r"^(?:test \S+ \.\.\. FAILED|FAIL\s+\[\s*[0-9.]+s\]\s+.+)$")
_CARGO_BACKTRACE_START = re.compile(r"^stack backtrace:\s*$", re.IGNORECASE)
_CARGO_BACKTRACE_NOTE = re.compile(
    r"^note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\s*$"
)


def _is_cargo_pass_progress(line: str) -> bool:
    return bool(_CARGO_PASS_LINE.fullmatch(line.strip()))


def filter_cargo_test(raw_output: str) -> str:
    """Collapse cargo/nextest pass progress; keep failures dense.

    Passing ``test ... ok`` / nextest ``PASS [..]`` runs collapse to one marker.
    Failed tests keep their status line, name, panic location, and assertion
    (including ``left:`` / ``right:``). Stack backtraces are dropped — they are
    the bulk of the tokens and do not add identity beyond the assertion line.
    Passes that appear after a failure stay as named lines. The trailing
    ``test result:`` / nextest ``Summary`` line is preserved.
    """
    lines = raw_output.splitlines()
    result: list[str] = []
    index = 0
    changed = False
    in_backtrace = False
    seen_failure = False

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if in_backtrace:
            if (
                stripped.startswith("test ")
                or stripped.startswith("test result:")
                or stripped.startswith("failures:")
                or stripped.startswith("error:")
                or _NEXTEST_PASS.fullmatch(stripped)
                or stripped.startswith("FAIL ")
                or stripped.startswith("Summary ")
                or stripped.startswith("────")
                or stripped.startswith("───")
            ):
                in_backtrace = False
            else:
                changed = True
                index += 1
                continue

        if _CARGO_BACKTRACE_START.match(stripped):
            in_backtrace = True
            changed = True
            index += 1
            continue

        if _CARGO_BACKTRACE_NOTE.match(stripped):
            changed = True
            index += 1
            continue

        if _CARGO_FAILED_LINE.fullmatch(stripped):
            seen_failure = True
            result.append(line)
            index += 1
            continue

        if _is_cargo_pass_progress(line):
            if seen_failure:
                result.append(line)
                index += 1
                continue
            end = index + 1
            while end < len(lines) and _is_cargo_pass_progress(lines[end]):
                end += 1
            passed = end - index
            result.append(f"[UsageTrim: {passed} passing tests, {passed} progress records]")
            changed = True
            index = end
            continue

        result.append(line)
        index += 1

    if not changed:
        return raw_output
    return "\n".join(result)


# Go test outputs pairs or lines of '=== RUN' and '--- PASS:'.
# Keep --- FAIL / panic assertion lines; drop goroutine stack dumps.
_GO_TEST_OK = re.compile(r"^\s*(?:=== RUN\s+\S+|--- PASS:\s+\S+\s+\([0-9.]+s\))$")
_GO_PASS_RECORD = re.compile(r"^\s*--- PASS:\s+\S+\s+\([0-9.]+s\)")
_GO_GOROUTINE = re.compile(r"^goroutine \d+ \[")
_GO_STACK_FILE = re.compile(r"^\S+\.go:\d+\s+\+0x[0-9a-fA-F]+")
_GO_STACK_FUNC = re.compile(r"^[0-9a-zA-Z_./\-]+(?:\.|·|/)\S*\(.*\)\s*$")


def filter_go_test(raw_output: str) -> str:
    """Collapse passing go test records; keep fail identity and panic lines dense."""
    lines = raw_output.splitlines()
    result: list[str] = []
    index = 0
    changed = False
    in_stack = False

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if in_stack:
            if (
                stripped.startswith("=== ")
                or stripped.startswith("--- ")
                or stripped.startswith("panic:")
                or stripped == "PASS"
                or stripped == "FAIL"
                or stripped.startswith("FAIL\t")
                or stripped.startswith("ok\t")
                or stripped.startswith("PASS\t")
            ):
                in_stack = False
            elif (
                _GO_GOROUTINE.match(stripped)
                or _GO_STACK_FILE.match(stripped)
                or _GO_STACK_FUNC.match(stripped)
                or stripped.startswith("created by ")
            ):
                changed = True
                index += 1
                continue
            else:
                # Unknown stack-adjacent noise — drop while in a dump.
                changed = True
                index += 1
                continue

        if _GO_GOROUTINE.match(stripped):
            in_stack = True
            changed = True
            index += 1
            continue

        if _GO_TEST_OK.match(stripped):
            end = index + 1
            while end < len(lines):
                if not _GO_TEST_OK.match(lines[end].strip()):
                    break
                end += 1
            chunk = lines[index:end]
            passed = sum(1 for ln in chunk if _GO_PASS_RECORD.match(ln.strip()))
            records = len(chunk)
            if passed > 0:
                result.append(f"[UsageTrim: {passed} passing tests, {records} progress records]")
                changed = True
                index = end
                continue
            result.extend(chunk)
            index = end
            continue

        result.append(line)
        index += 1

    if not changed:
        return raw_output
    return "\n".join(result)


# Jest and Vitest print progress records with checkmarks or PASS prefixes.
_JS_TEST_OK = re.compile(
    r"^\s*(?:PASS\s+\S+|[✓√]\s+.*(?:\([0-9.]+\s*m?s\)|\(\d+\s+tests?\)|$))\s*$"
)
_JS_DIAGNOSTIC = re.compile(
    r"\b(?:FAIL|failed|failure|failures|Error|AssertionError|panic|fatal|warning|timeout)\b|[✕×]",
    re.IGNORECASE,
)
# Jest console.* dumps and Vitest stdout| / stderr| / ● Console blocks.
_JS_CONSOLE_HEAD = re.compile(
    r"^\s*(?:"
    r"console\.(?:log|info|debug|warn|error|dir|table|trace)\b|"
    r"stdout\s*\||"
    r"stderr\s*\||"
    r"●\s*Console"
    r")\s*"
)
_JS_SUMMARY = re.compile(r"^\s*(?:Test (?:Suites|Files)|Tests|Snapshots|Time|Duration|Start at)\b")
_JS_FAIL_SUITE = re.compile(r"^\s*(?:FAIL\s+\S+|✕|×)")
# Vitest states a per-file test count; Jest prints a bare suite header instead.
_JS_FILE_COUNT_RE = re.compile(r"\((\d+)\s+tests?\)")
_JS_SUITE_RE = re.compile(r"^PASS\s+\S+")


def _js_console_head(line: str) -> bool:
    return bool(_JS_CONSOLE_HEAD.match(line.strip()))


def _js_hard_boundary(line: str) -> bool:
    """Suite/test/summary lines that end console folding (not another console dump)."""
    stripped = line.strip()
    if not stripped:
        return False
    return bool(
        _JS_TEST_OK.match(stripped) or _JS_FAIL_SUITE.match(stripped) or _JS_SUMMARY.match(stripped)
    )


def _js_should_keep_verbatim(line: str) -> bool:
    """True when the rest of the run must stay untouched (real failure signal).

    ``console.error`` must not trip this: ``\\bError\\b`` matches inside it under
    IGNORECASE, which used to freeze filtering for the rest of the log.
    """
    stripped = line.strip()
    if not stripped or _JS_TEST_OK.match(stripped) or _JS_CONSOLE_HEAD.match(stripped):
        return False
    if _JS_FAIL_SUITE.match(stripped):
        return True
    return bool(_JS_DIAGNOSTIC.search(line))


def _summarize_js_records(chunk: list[str]) -> str:
    """Describe a collapsed run without inflating the test count.

    A per-file record already accounts for the individual records printed beneath
    it, so the two are never added together. Counts that the output does not state
    are reported as files rather than guessed at.
    """
    declared = 0
    counted_files = 0
    bare_files = 0
    individual = 0

    for raw_line in chunk:
        line = raw_line.strip()
        match = _JS_FILE_COUNT_RE.search(line)
        if match:
            declared += int(match.group(1))
            counted_files += 1
        elif _JS_SUITE_RE.match(line):
            bare_files += 1
        else:
            individual += 1

    def plural(count: int, noun: str) -> str:
        return f"{count} {noun}" if count == 1 else f"{count} {noun}s"

    if not counted_files and not bare_files:
        return f"{individual} passing tests"
    if not counted_files:
        return f"{plural(bare_files, 'passing test file')}"
    if bare_files:
        return f"{declared} passing tests and {plural(bare_files, 'more test file')}"
    return f"{declared} passing tests in {plural(counted_files, 'file')}"


def filter_jest_vitest(raw_output: str) -> str:
    """Collapse passing Jest/Vitest records and console dumps; keep failures dense."""
    lines = raw_output.splitlines()
    result: list[str] = []
    index = 0
    collapsed = False

    while index < len(lines):
        line = lines[index]

        if _js_should_keep_verbatim(line):
            result.extend(lines[index:])
            break

        if _js_console_head(line):
            end = index + 1
            while end < len(lines):
                nxt = lines[end]
                if _js_hard_boundary(nxt):
                    break
                if _js_console_head(nxt):
                    end += 1
                    continue
                if not nxt.strip():
                    look = end + 1
                    while look < len(lines) and not lines[look].strip():
                        look += 1
                    if look >= len(lines) or _js_hard_boundary(lines[look]):
                        break
                end += 1
            omitted = end - index
            result.append(f"[UsageTrim: {omitted} console lines omitted]")
            collapsed = True
            index = end
            continue

        if _JS_TEST_OK.match(line.strip()):
            end = index + 1
            while end < len(lines):
                if not _JS_TEST_OK.match(lines[end].strip()):
                    break
                end += 1
            records = end - index
            summary = _summarize_js_records(lines[index:end])
            result.append(f"[UsageTrim: {summary}, {records} progress records]")
            collapsed = True
            index = end
            continue

        result.append(line)
        index += 1

    if not collapsed:
        return raw_output
    return "\n".join(result)


# TypeScript compiler pretty mode repeats source frames, squiggles, and nested
# type notes. Keep one dense row per unique diagnostic: file, line, rule, message.
_TSC_HEADER_RE = re.compile(
    r"^(\S+?):(\d+):(\d+)\s+-\s+error\s+(TS\d+):\s*(.*)$|"
    r"^(\S+?)\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.*)$"
)
_SQUIGGLE_RE = re.compile(r"^\s*[~^]+\s*$")
_TSC_FRAME_LINE_RE = re.compile(r"^(?:>\s*)?\d+\s+\||^[|\s~^]+$")
_TSC_SUMMARY_RE = re.compile(r"^Found \d+ errors?\b")
_TSC_FILE_TABLE_RE = re.compile(r"^(Errors\s+Files|\s+\d+\s+\S+:\d+\s*)$")


def filter_tsc(raw_output: str) -> str:
    """Compact verbose tsc pretty output into dense unique diagnostics."""
    lines = raw_output.splitlines()
    if not any("error TS" in line for line in lines):
        return raw_output

    result: list[str] = []
    seen: set[str] = set()
    summary: str | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _SQUIGGLE_RE.match(stripped) or _TSC_FRAME_LINE_RE.match(stripped):
            continue
        if _TSC_FILE_TABLE_RE.match(stripped):
            continue

        header = _TSC_HEADER_RE.match(stripped)
        if header:
            if header.group(1) is not None:
                path, lineno, col, code, message = header.group(1, 2, 3, 4, 5)
            else:
                path, lineno, col, code, message = header.group(6, 7, 8, 9, 10)
            entry = f"{path}:{lineno}:{col} {code} {message.strip()}"
            if entry not in seen:
                seen.add(entry)
                result.append(entry)
            continue

        if _TSC_SUMMARY_RE.match(stripped):
            summary = stripped
            continue

        # Drop nested type-continuation notes and leftover pretty padding.
        if stripped.startswith("Type ") or stripped.startswith("Type '"):
            continue

    if summary:
        result.append(summary)
    return "\n".join(result) if result else raw_output


# Ruff's default ``full`` format repeats source frames and caret underlines for
# every diagnostic. Keep the actionable header + optional help; drop the frame.
_RUFF_HEADER_RE = re.compile(r"^(\S+:\d+:\d+:\s+[A-Z]\d+\b.*)$")
_RUFF_HELP_RE = re.compile(r"^=\s*help:\s*(.+)$")
_RUFF_FRAME_LINE_RE = re.compile(r"^(?:\||\d+\s+\||[\^~]+)$|^(?:\||\d+\s+\|)")


def filter_ruff(raw_output: str) -> str:
    """Compact verbose Ruff ``full`` frames into dense single-line diagnostics."""
    lines = raw_output.splitlines()
    if not any(_RUFF_HEADER_RE.match(line.strip()) for line in lines):
        return raw_output

    result: list[str] = []
    current: str | None = None

    def flush():
        nonlocal current
        if current is not None:
            result.append(current)
            current = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        help_match = _RUFF_HELP_RE.match(stripped)
        if help_match and current is not None:
            current = f"{current} | help: {help_match.group(1).strip()}"
            continue

        if _RUFF_HEADER_RE.match(stripped):
            flush()
            current = stripped
            continue

        if current is not None and _RUFF_FRAME_LINE_RE.match(stripped):
            continue

        flush()
        result.append(stripped)

    flush()
    return "\n".join(result)


# BuildKit / docker build print one progress stream per step (`#12 ...`). Collapse
# routine progress; once a failure or final image tag appears, keep it intact.
_DOCKER_STEP_RE = re.compile(r"^#\d+\s")
_DOCKER_KEEP_RE = re.compile(
    r"(?i)(?:\berror\b|failed to solve|successfully tagged|"
    r"writing image sha256|naming to\s+\S+)"
)


def filter_docker_build(raw_output: str) -> str:
    """Collapse BuildKit layer progress while keeping failures and final tags."""
    if not raw_output.strip():
        return raw_output

    lines = raw_output.splitlines()
    if not any(_DOCKER_STEP_RE.match(line) for line in lines):
        return raw_output

    result: list[str] = []
    progress = 0
    collapsed = False

    def flush_progress() -> None:
        nonlocal progress, collapsed
        if progress:
            result.append(f"[UsageTrim: {progress} docker build progress lines]")
            progress = 0
            collapsed = True

    for index, line in enumerate(lines):
        if _DOCKER_KEEP_RE.search(line):
            flush_progress()
            # Failures and their trailing detail blocks stay verbatim.
            if re.search(r"(?i)\berror\b|failed to solve", line):
                result.extend(lines[index:])
                collapsed = True
                break
            result.append(line)
            continue

        if _DOCKER_STEP_RE.match(line):
            progress += 1
            continue

        flush_progress()
        result.append(line)

    flush_progress()
    if not collapsed:
        return raw_output
    return "\n".join(result)


# Pyright/basedpyright print a dense header, then indented path + source + caret.
_PYRIGHT_DIAG_RE = re.compile(
    r"^(\S+?:\d+:\d+ - (?:error|warning|information): .+)$",
    re.IGNORECASE,
)
_PYRIGHT_SUMMARY_RE = re.compile(
    r"^\d+ errors?, \d+ warnings?, \d+ informations?\s*$",
    re.IGNORECASE,
)


def filter_pyright(raw_output: str) -> str:
    """Compact Pyright source frames into dense diagnostic headers."""
    lines = raw_output.splitlines()
    if not any(_PYRIGHT_DIAG_RE.match(line.strip()) for line in lines):
        return raw_output

    result: list[str] = []
    changed = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _PYRIGHT_DIAG_RE.match(stripped) or _PYRIGHT_SUMMARY_RE.match(stripped):
            result.append(stripped)
            continue
        if line[:1].isspace():
            changed = True
            continue
        result.append(stripped)

    if not changed:
        return raw_output
    return "\n".join(result)


# ESLint stylish (default) prints an absolute path header then padded columns.
# Codeframe embeds source lines and caret underlines. Collapse both to dense
# ``file:line:col severity message rule`` rows while keeping the summary.
_ESLINT_STYLISH_DIAG_RE = re.compile(r"^(\d+):(\d+)\s+(error|warning|info)\s+(.+?)\s{2,}(\S+)\s*$")
_ESLINT_CODEFRAME_RE = re.compile(
    r"^(error|warning|info):\s+(.+?)\s+\(([^)]+)\)\s+at\s+(\S+):(\d+):(\d+):?\s*$"
)
_ESLINT_SUMMARY_RE = re.compile(r"^[✖×]\s+\d+\s+problems?\b")
_ESLINT_FIXABLE_RE = re.compile(r"potentially fixable with the `--fix` option")
_ESLINT_FRAME_LINE_RE = re.compile(r"^(?:>\s*)?\d+\s+\||^[|\s^~]+$")
_ESLINT_PATH_RE = re.compile(r"^(?:[A-Za-z]:)?[\\/].+\.[A-Za-z0-9]+$|^[^:\s].+\.[A-Za-z0-9]+$")


def _eslint_relpath(path: str) -> str:
    """Prefer a repo-relative looking suffix when stylish prints absolute paths."""
    normalized = path.replace("\\", "/")
    # Already relative — keep the full project path for actionable locations.
    if not normalized.startswith("/") and not re.match(r"^[A-Za-z]:/", normalized):
        return path
    markers = ("/src/", "/lib/", "/app/", "/apps/", "/packages/", "/test/", "/tests/")
    for marker in markers:
        idx = normalized.find(marker)
        if idx != -1:
            return normalized[idx + 1 :]
    return normalized.rsplit("/", 1)[-1]


# mypy --pretty repeats source/caret frames under each diagnostic.
# Keep error/note headers (with codes); drop the pretty frame body.
_MYPY_DIAG_RE = re.compile(r"^(\S+:\d+:(?:\d+:)?\s+(?:error|warning|note|unreachable):\s+.+)$")


def filter_mypy(raw_output: str) -> str:
    """Compact mypy ``--pretty`` frames into dense diagnostics with codes kept."""
    lines = raw_output.splitlines()
    if not any(
        ": error:" in line or ": warning:" in line or ": note:" in line or ": unreachable:" in line
        for line in lines
    ):
        return raw_output

    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _MYPY_DIAG_RE.match(stripped):
            result.append(stripped)
            continue
        if stripped.startswith(("Found ", "Success:")):
            result.append(stripped)
            continue
        # Drop pretty source context and caret underlines.
        continue

    return "\n".join(result)


def filter_eslint(raw_output: str) -> str:
    """Compact verbose ESLint stylish/codeframe output into dense unique diagnostics."""
    lines = raw_output.splitlines()
    has_stylish = any(_ESLINT_STYLISH_DIAG_RE.match(line.strip()) for line in lines)
    has_codeframe = any(_ESLINT_CODEFRAME_RE.match(line.strip()) for line in lines)
    if not has_stylish and not has_codeframe:
        return raw_output

    result: list[str] = []
    seen: set[str] = set()
    current_file: str | None = None
    summary: str | None = None

    def add(entry: str) -> None:
        if entry not in seen:
            seen.add(entry)
            result.append(entry)

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Stack frames and codeframe source rows are pure noise for agents.
        if stripped.startswith("at ") or _ESLINT_FRAME_LINE_RE.match(stripped):
            continue
        if _ESLINT_FIXABLE_RE.search(stripped):
            continue

        codeframe = _ESLINT_CODEFRAME_RE.match(stripped)
        if codeframe:
            severity, message, rule, path, lineno, col = codeframe.groups()
            add(f"{_eslint_relpath(path)}:{lineno}:{col} {severity} {message} ({rule})")
            current_file = None
            continue

        stylish = _ESLINT_STYLISH_DIAG_RE.match(stripped)
        if stylish and current_file is not None:
            lineno, col, severity, message, rule = stylish.groups()
            add(f"{current_file}:{lineno}:{col} {severity} {message.strip()} {rule}")
            continue

        if _ESLINT_SUMMARY_RE.match(stripped):
            summary = stripped
            current_file = None
            continue

        # Stylish file headers: a bare path line before indented diagnostics.
        if has_stylish and _ESLINT_PATH_RE.match(stripped) and not re.search(r":\d+:\d+", stripped):
            current_file = _eslint_relpath(stripped)
            continue

    if summary:
        result.append(summary)
    return "\n".join(result) if result else raw_output


_GH_BODY_KEYS = frozenset({"body", "bodyText", "messageBody", "text"})
_GH_ARRAY_CAPS = {
    "commits": 5,
    "files": 8,
    "reviews": 3,
    "comments": 3,
    "labels": 8,
    "assignees": 5,
    "reviewRequests": 5,
    "statusCheckRollup": 5,
}
_GH_TEXT_BODY_KEEP = 600


def _gh_argv(command: str) -> list[str] | None:
    if not command or "\n" in command:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    return words or None


def _is_gh_command(command: str) -> bool:
    words = _gh_argv(command)
    return bool(words) and PurePath(words[0]).name == "gh"


def _is_gh_json_command(command: str) -> bool:
    """Recognize ``gh api`` and any ``gh … --json`` invocation."""
    words = _gh_argv(command)
    if not words or PurePath(words[0]).name != "gh":
        return False
    if len(words) >= 2 and words[1] == "api":
        return True
    return any(word == "--json" or word.startswith("--json=") for word in words[1:])


def _is_gh_text_view_command(command: str) -> bool:
    words = _gh_argv(command)
    if not words or PurePath(words[0]).name != "gh" or len(words) < 3:
        return False
    if any(word == "--json" or word.startswith("--json=") for word in words[1:]):
        return False
    return words[1] in {"pr", "issue"} and words[2] == "view"


def _truncate_gh_string(value: str, limit: int = 400) -> str:
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return value[:limit] + f"... [{omitted} chars omitted]"


def _slim_gh_payload(data: object) -> object:
    """Prefer PR/issue signal fields; cap noisy arrays and long markdown bodies."""
    if isinstance(data, list):
        cap = 5
        kept = [_slim_gh_payload(item) for item in data[:cap]]
        if len(data) > cap:
            kept.append(f"... {len(data) - cap} array items omitted by usagetrim ...")
        return kept

    if not isinstance(data, dict):
        if isinstance(data, str):
            return _truncate_gh_string(data, 120)
        return data

    result: dict[str, object] = {}
    for key, value in data.items():
        if key in {"author", "user", "editor", "mergedBy"} and isinstance(value, dict):
            login = value.get("login")
            result[key] = {"login": login} if isinstance(login, str) else _slim_gh_payload(value)
            continue
        if key in _GH_BODY_KEYS and isinstance(value, str):
            result[key] = _truncate_gh_string(value, 400)
            continue
        if key == "commits" and isinstance(value, list):
            cap = _GH_ARRAY_CAPS["commits"]
            commits = []
            for item in value[:cap]:
                if isinstance(item, dict):
                    commits.append(
                        {
                            "oid": item.get("oid") or item.get("sha"),
                            "messageHeadline": item.get("messageHeadline")
                            or item.get("message_headline")
                            or item.get("message"),
                        }
                    )
                else:
                    commits.append(_slim_gh_payload(item))
            if len(value) > cap:
                commits.append(f"... {len(value) - cap} array items omitted by usagetrim ...")
            result[key] = commits
            continue
        if key == "files" and isinstance(value, list):
            cap = _GH_ARRAY_CAPS["files"]
            files = []
            for item in value[:cap]:
                if isinstance(item, dict):
                    files.append(
                        {
                            "path": item.get("path") or item.get("filename"),
                            "additions": item.get("additions"),
                            "deletions": item.get("deletions"),
                        }
                    )
                else:
                    files.append(_slim_gh_payload(item))
            if len(value) > cap:
                files.append(f"... {len(value) - cap} array items omitted by usagetrim ...")
            result[key] = files
            continue
        if key == "labels" and isinstance(value, list):
            names: list[object] = []
            for item in value[: _GH_ARRAY_CAPS["labels"]]:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    names.append(item["name"])
                elif isinstance(item, str):
                    names.append(item)
                else:
                    names.append(_slim_gh_payload(item))
            if len(value) > _GH_ARRAY_CAPS["labels"]:
                names.append(f"... {len(value) - _GH_ARRAY_CAPS['labels']} labels omitted ...")
            result[key] = names
            continue
        if key in _GH_ARRAY_CAPS and isinstance(value, list):
            cap = _GH_ARRAY_CAPS[key]
            kept = [_slim_gh_payload(item) for item in value[:cap]]
            if len(value) > cap:
                kept.append(f"... {len(value) - cap} array items omitted by usagetrim ...")
            result[key] = kept
            continue
        result[key] = _slim_gh_payload(value)
    return result


def filter_gh_text_view(raw_output: str) -> str | None:
    """Fold long markdown bodies from ``gh pr view`` / ``gh issue view`` text mode."""
    if not raw_output.strip():
        return None
    lines = raw_output.splitlines()
    meta: list[str] = []
    body_start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            body_start = index + 1
            break
        # gh prints ``key:\tvalue`` metadata before the blank-line-separated body.
        if ":\t" in line or (":" in line and index < 12):
            meta.append(line)
            body_start = index + 1
            continue
        body_start = index
        break
    else:
        return None

    body = "\n".join(lines[body_start:])
    if len(body) < _GH_TEXT_BODY_KEEP + 200:
        return None

    kept = body[:_GH_TEXT_BODY_KEEP].rstrip()
    omitted = len(body) - len(kept)
    parts = meta + [
        "",
        kept,
        f"[UsageTrim: {omitted} body chars omitted; full output recoverable via spill/CCR]",
    ]
    return "\n".join(parts)


# ``gh run view --log`` prefixes every line with ``job<TAB>step<TAB>timestamp``.
_GH_LOG_LINE = re.compile(
    r"^(?P<job>[^\t]*)\t(?P<step>[^\t]*)\t\ufeff?(?:\d{4}-\d\d-\d\dT[\d:.]+Z ?)?(?P<text>.*)$"
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_GH_LOG_SIGNAL = re.compile(
    r"##\[error\]|\berror\b(?!-)|\bfail(?:ed|ure)?\b(?!-)|FAIL:|Traceback|\bassert|\w*Exception\b"
    r"|panicked|would reformat|exit code [1-9]",
    re.IGNORECASE,
)
_GH_LOG_NOISE = re.compile(
    r"^(?:##\[(?:group|endgroup|debug|start-action|end-action)|\[command\]|Current runner version"
    r"|Runner Image|Hosted Compute Agent|Version: |Commit: |Build Date: |Worker ID: |Azure Region: "
    r"|Operating System|Ubuntu$|LTS$|Image: |Image Release: |Included Software|Secret source: "
    r"|Prepare workflow directory|Prepare all required actions|Getting action download info"
    r"|Download action repository|Complete job name|Post job cleanup|Cleaning up orphan processes"
    r"|Temporarily overriding HOME|Adding repository directory|git version |Syncing repository"
    r"|Fetching the repository|Determining the checkout|Checking out the ref|Removing |shell: )"
)
# Step inputs and environment are printed as ``with:``/``env:`` followed by indented pairs.
_GH_LOG_BLOCK_START = frozenset({"with:", "env:"})
_GH_LOG_STEP_TAIL = 12
_GH_LOG_AFTER_SIGNAL = 10
_GH_LOG_MAX_SIGNAL = 80


def _is_gh_run_log_command(command: str) -> bool:
    words = _gh_argv(command)
    if not words or PurePath(words[0]).name != "gh" or len(words) < 3:
        return False
    return (
        words[1] == "run"
        and words[2] == "view"
        and any(word in {"--log", "--log-failed"} for word in words[3:])
    )


def filter_gh_run_log(raw_output: str) -> str | None:
    """Fold ``gh run view --log`` / ``--log-failed`` down to failures and step tails.

    Runner setup, checkout plumbing and job cleanup are dropped, as are the
    per-line job/step/timestamp prefixes; each step keeps every error-like line
    plus its last few lines. The full log goes to the cache and stays
    recoverable by ref.
    """
    lines = raw_output.splitlines()
    parsed = [_GH_LOG_LINE.match(line) for line in lines]
    if len(lines) < 20 or sum(match is not None for match in parsed) < len(lines) // 2:
        return None

    groups: dict[tuple[str, str], list[str]] = {}
    for line, match in zip(lines, parsed):
        job, step, text = (match["job"], match["step"], match["text"]) if match else ("", "", line)
        text = _ANSI_ESCAPE.sub("", text).rstrip()
        if text:
            groups.setdefault((job, step), []).append(text)

    output: list[str] = []
    signal_budget = _GH_LOG_MAX_SIGNAL
    for (job, step), texts in groups.items():
        candidates: list[int] = []
        in_block = False
        for index, text in enumerate(texts):
            bare = text.lstrip("\ufeff")
            if bare.strip() in _GH_LOG_BLOCK_START:
                in_block = True
                continue
            if in_block and bare[:1].isspace():
                continue
            in_block = False
            if not _GH_LOG_NOISE.match(bare):
                candidates.append(index)
        signal = [index for index in candidates if _GH_LOG_SIGNAL.search(texts[index])]
        signal = signal[:signal_budget]
        signal_budget -= len(signal)
        position = {index: order for order, index in enumerate(candidates)}
        after = {
            candidates[order]
            for index in signal
            for order in range(position[index] + 1, position[index] + 1 + _GH_LOG_AFTER_SIGNAL)
            if order < len(candidates)
        }
        keep = sorted(set(signal) | after | set(candidates[-_GH_LOG_STEP_TAIL:]))
        if not keep:
            continue
        output.append(f"── {job} › {step}" if step and step != "UNKNOWN STEP" else f"── {job}")
        previous = -1
        for index in keep:
            if index - previous > 1:
                output.append(f"  … {index - previous - 1} lines omitted")
            output.append("  " + texts[index].replace("##[error]", "ERROR: "))
            previous = index

    compact = "\n".join(output)
    if len(compact) >= len(raw_output) * 0.8:
        return None
    ref_id = ContextCache().store(raw_output, source="gh-run-log")
    header = (
        f"// [usagetrim: CI log {len(lines):,} lines → {len(output):,} "
        f"(runner setup and passing output dropped). Ref: {ref_id}]\n"
    )
    return header + compact


# ---------------------------------------------------------------------------
# CI log folding — GitHub Actions groups, GitLab sections, CircleCI banners
# ---------------------------------------------------------------------------

_CI_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+")
_CI_GH_GROUP_START = re.compile(r"^##\[group\](.*)$")
_CI_GH_GROUP_END = re.compile(r"^##\[endgroup\]\s*$")
# GitLab: optional ANSI clear, then section_start:EPOCH:name (name may include [collapsed=true]).
_CI_GL_SECTION_START = re.compile(r"^(?:\x1b\[0K)?section_start:\d+:([^\r\n]+?)(?:\r.*)?$")
_CI_GL_SECTION_END = re.compile(r"^(?:\x1b\[0K)?section_end:\d+:")
# CircleCI 2.x machine executor commonly prints ``====>> Step Name``.
_CI_CIRCLE_BANNER = re.compile(r"^={3,}>>\s*(.+?)\s*$")
_CI_ERROR_RE = re.compile(
    r"(?:##\[error\]|\bError\b|\bAssertionError\b|\bFAILED\b|\bTraceback\b"
    r"|\berror\b(?!-)|exit code [1-9])",
    re.IGNORECASE,
)
_CI_SETUP_TEARDOWN_RE = re.compile(
    r"(?i)(?:"
    r"set\s*up\s*job|complete\s*job|post\s*job\s*cleanup|post\s+.+"
    r"|operating\s*system|runner\s*image|included\s*software"
    r"|actions/checkout|actions/cache|checkout|restore\s*cache|save\s*cache"
    r"|setup[- ](?:job|python|node|java|go|ruby|dotnet|php)"
    r"|get_sources|get-sources|prepare_script|cleanup_file_variables"
    r"|upload[- ]artifact|download[- ]artifact"
    r"|spin\s*up|spinning\s*up\s*environment|preparing\s*environment"
    r")"
)
_CI_FOLD_NOTICE_RE = re.compile(r"folded by usagetrim", re.IGNORECASE)
_CI_FAILED_TAIL = 20
_CI_MIN_FOLD_LINES = 3
_CI_MARKER_THRESHOLD = 2


def _ci_strip_timestamp(line: str) -> str:
    """Drop leading ISO-8601 runner timestamps for stable, denser output."""
    return _CI_TIMESTAMP_RE.sub("", line)


def _ci_clean_gl_title(raw: str) -> str:
    """Normalize a GitLab section name (drop ``[collapsed=true]`` suffix noise)."""
    title = raw.strip()
    bracket = title.find("[")
    if bracket > 0:
        title = title[:bracket].rstrip()
    return title or "section"


def _ci_is_setup_teardown(title: str) -> bool:
    return bool(_CI_SETUP_TEARDOWN_RE.search(title))


def _ci_group_has_error(body: list[str]) -> bool:
    return any(_CI_ERROR_RE.search(line) for line in body)


def _ci_fold_notice(title: str, line_count: int) -> str:
    display = title.strip() or "section"
    if len(display) > 80:
        display = display[:77] + "..."
    return f"  [... {line_count} lines of '{display}' folded by usagetrim ...]"


def _ci_emit_failed_group(title: str, body: list[str], kind: str) -> list[str]:
    """Keep error-like lines and the tail of a failed group, preserving order."""
    if not body:
        header = f"##[group]{title}" if kind == "github" else f">> {title} (FAILED)"
        return [header]

    keep: set[int] = set()
    for index, line in enumerate(body):
        if _CI_ERROR_RE.search(line):
            keep.add(index)
    tail_start = max(0, len(body) - _CI_FAILED_TAIL)
    keep.update(range(tail_start, len(body)))

    ordered = sorted(keep)
    out: list[str] = []
    if kind == "github":
        out.append(f"##[group]{title}")
    elif kind == "gitlab":
        out.append(f"section: {title}")
    else:
        out.append(f"====>> {title}")

    previous = -1
    for index in ordered:
        if previous >= 0 and index - previous > 1:
            out.append(f"  … {index - previous - 1} lines omitted")
        elif previous < 0 and index > 0:
            out.append(f"  … {index} lines omitted")
        out.append(body[index])
        previous = index
    if kind == "github":
        out.append("##[endgroup]")
    return out


def _ci_parse_segments(
    lines: list[str],
) -> list[tuple[str, str, list[str]]]:
    """Split cleaned lines into ``(kind, title, body)`` segments.

    ``kind`` is ``github``, ``gitlab``, ``circle``, or ``raw`` (ungrouped lines).
    CircleCI banners open a segment that runs until the next banner or EOF.
    """
    segments: list[tuple[str, str, list[str]]] = []
    raw_buf: list[str] = []
    active_kind: str | None = None
    active_title = ""
    active_body: list[str] = []

    def flush_raw() -> None:
        nonlocal raw_buf
        if raw_buf:
            segments.append(("raw", "", raw_buf))
            raw_buf = []

    def flush_active() -> None:
        nonlocal active_kind, active_title, active_body
        if active_kind is not None:
            segments.append((active_kind, active_title, active_body))
            active_kind = None
            active_title = ""
            active_body = []

    for line in lines:
        gh_start = _CI_GH_GROUP_START.match(line)
        if gh_start:
            flush_raw()
            flush_active()
            active_kind = "github"
            active_title = gh_start.group(1).strip() or "group"
            active_body = []
            continue
        if _CI_GH_GROUP_END.match(line):
            if active_kind == "github":
                flush_active()
            else:
                raw_buf.append(line)
            continue

        gl_start = _CI_GL_SECTION_START.match(line)
        if gl_start:
            flush_raw()
            flush_active()
            active_kind = "gitlab"
            active_title = _ci_clean_gl_title(gl_start.group(1))
            active_body = []
            continue
        if _CI_GL_SECTION_END.match(line):
            if active_kind == "gitlab":
                flush_active()
            else:
                raw_buf.append(line)
            continue

        circle = _CI_CIRCLE_BANNER.match(line)
        if circle:
            flush_raw()
            flush_active()
            active_kind = "circle"
            active_title = circle.group(1).strip() or "step"
            active_body = []
            continue

        if active_kind is not None:
            active_body.append(line)
        else:
            raw_buf.append(line)

    flush_raw()
    flush_active()
    return segments


def filter_ci_logs(raw_output: str) -> str:
    """Fold noisy CI log groups into one-line summaries; keep failures and errors.

    Handles GitHub Actions ``##[group]`` / ``##[endgroup]``, GitLab
    ``section_start:`` / ``section_end:``, and CircleCI ``====>>`` step banners.
    Successful setup/teardown groups (checkout, cache, set up job, post steps)
    collapse to a single notice. Failed groups keep error-like lines and a short
    tail. Leading ISO-8601 timestamps are stripped. Idempotent on already-short
    or already-folded input. Pure function (no network / cache I/O).

    CircleCI limitation: only the common ``====>>`` machine-executor banners are
    recognized; orb-specific or UI-exported shapes without those banners are left
    unchanged unless they also contain GitHub/GitLab markers.
    """
    if not raw_output.strip():
        return raw_output

    if _CI_FOLD_NOTICE_RE.search(raw_output):
        return raw_output

    original_lines = raw_output.splitlines()
    cleaned = [_ci_strip_timestamp(line) for line in original_lines]

    marker_hits = sum(
        1
        for line in cleaned
        if (
            _CI_GH_GROUP_START.match(line)
            or _CI_GL_SECTION_START.match(line)
            or _CI_CIRCLE_BANNER.match(line)
        )
    )
    # Already short / no foldable structure → leave alone (idempotent).
    if marker_hits < 1 and len(cleaned) < 40:
        return raw_output
    if marker_hits < 1:
        return raw_output

    segments = _ci_parse_segments(cleaned)
    if not segments:
        return raw_output

    result: list[str] = []
    folded_any = False

    for kind, title, body in segments:
        if kind == "raw":
            for line in body:
                result.append(line)
            continue

        has_error = _ci_group_has_error(body)
        is_setup = _ci_is_setup_teardown(title)

        if has_error:
            result.extend(_ci_emit_failed_group(title, body, kind))
            continue

        if is_setup and len(body) >= _CI_MIN_FOLD_LINES:
            result.append(_ci_fold_notice(title, len(body)))
            folded_any = True
            continue

        # Non-setup success: keep body; re-emit a lightweight header for context.
        if kind == "github":
            result.append(f"##[group]{title}")
            result.extend(body)
            result.append("##[endgroup]")
        elif kind == "gitlab":
            result.append(f"section: {title}")
            result.extend(body)
        else:
            result.append(f"====>> {title}")
            result.extend(body)

    if not folded_any and not any(_CI_ERROR_RE.search(line) for line in cleaned):
        # Nothing useful changed (e.g. only tiny groups) — stay idempotent.
        compact_probe = "\n".join(result)
        if len(compact_probe) >= len(raw_output) * 0.9:
            return raw_output

    ret = "\n".join(result)
    if raw_output.endswith("\n"):
        ret += "\n"
    # Never expand the prompt.
    if len(ret) >= len(raw_output):
        return raw_output
    return ret


def _is_ci_log_command(command: str) -> bool:
    """True for ``glab ci …``, ``circleci …``, and similar CI trace viewers."""
    try:
        words = shlex.split(command.strip())
    except ValueError:
        words = command.strip().split()
    if not words:
        return False
    binary = PurePath(words[0]).name.lower()
    if binary in {"glab", "glab.exe"}:
        return len(words) >= 2 and words[1].lower() == "ci"
    if binary in {"circleci", "circleci.exe"}:
        return True
    # Downloaded artifact / raw log inspection via common readers when the
    # path name itself signals a CI log (kept narrow to avoid false positives).
    if binary in {"cat", "bat", "less", "tail", "head"} and len(words) >= 2:
        joined = " ".join(words[1:]).lower()
        return any(
            token in joined
            for token in (
                "ci.log",
                "ci-log",
                "github-actions",
                "actions.log",
                "job.log",
                "gitlab-ci",
                "circleci",
            )
        )
    return False


def _looks_like_ci_log(raw_output: str) -> bool:
    """Detect foldable CI markers without relying on the invoking command."""
    hits = 0
    for line in raw_output.splitlines()[:400]:
        bare = _ci_strip_timestamp(line)
        if (
            _CI_GH_GROUP_START.match(bare)
            or _CI_GL_SECTION_START.match(bare)
            or _CI_CIRCLE_BANNER.match(bare)
        ):
            hits += 1
            if hits >= _CI_MARKER_THRESHOLD:
                return True
    return False


def filter_gh_command_output(command: str, raw_output: str) -> str | None:
    """Specialize ``gh pr view`` / ``gh api`` (and related) with JSON spill + slim.

    Large JSON payloads are written to the spill directory and CCR, then replaced
    with a structured slim that keeps PR/issue signal fields. Text-mode
    ``gh pr|issue view`` folds oversized markdown bodies.
    """
    if not raw_output or not _is_gh_command(command):
        return None

    stripped = raw_output.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if parsed is not None:
            slimmed_data = _slim_gh_payload(parsed)
            # Fall back to generic depth/string limits for residual nesting.
            slimmed_data = slim_json_data(
                slimmed_data, max_array_items=3, max_string_len=120, max_depth=6
            )
            # Indentation costs ~35% more tokens than the same data without it.
            slimmed = json.dumps(slimmed_data, separators=(",", ":"), ensure_ascii=False)
            explicit = _is_gh_json_command(command)
            # A recovery header on tiny or unslimmable output only adds tokens.
            if len(slimmed) >= len(raw_output) or (
                not explicit and len(slimmed) > len(raw_output) * 0.85
            ):
                return None

            spilled = spill_large_output(raw_output, source="gh-json")
            if spilled is not None:
                header = (
                    f"[UsageTrim spill: {spilled.bytes_written:,} bytes → {spilled.path}]\n"
                    f"// [usagetrim: raw JSON ({len(raw_output):,} bytes) compacted. "
                    f"Ref: {spilled.ref_id}]\n"
                )
            else:
                ref_id = ContextCache().store(raw_output, source="gh-json")
                header = (
                    f"// [usagetrim: raw JSON ({len(raw_output):,} bytes) compacted. "
                    f"Ref: {ref_id}]\n"
                )
            compact = header + slimmed
            return compact if len(compact) < len(raw_output) else None

    if _is_gh_run_log_command(command):
        return filter_gh_run_log(raw_output)
    if _is_gh_text_view_command(command):
        return filter_gh_text_view(raw_output)
    return None


def filter_json_output(raw_output: str, command: str = "") -> str | None:
    """Automatically slim large or verbose JSON output from commands.

    Targeted for commands like `gh api`, `gh pr view --json`, `docker inspect`,
    `curl`, `kubectl -o json`, or any command output that is valid JSON with
    substantial array or nested structures. Full uncompressed payload is cached
    in SQLite CCR with a recovery reference.
    """
    stripped = raw_output.strip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return None

    cmd_lower = command.lower().strip()
    is_explicit_json_cmd = any(
        kw in cmd_lower
        for kw in (
            "gh api",
            "gh pr",
            "gh issue",
            "--json",
            "docker inspect",
            "podman inspect",
            "-o json",
            "-o=json",
            "--format json",
            "--format=json",
            "--output json",
            "--output=json",
        )
    )

    # For general commands, avoid compacting small objects or short responses (< 10 lines and < 300 chars)
    if not is_explicit_json_cmd and len(stripped) < 300 and stripped.count("\n") < 10:
        return None

    try:
        json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None

    slimmed = slim_json(raw_output, max_array_items=3, cache_full=True)
    # Only return specialized output if we actually achieved significant reduction (>= 15%)
    if len(slimmed) <= len(raw_output) * 0.85:
        return slimmed

    return None


_CARGO_STEP_RE = re.compile(
    r"^\s*(?:Compiling|Downloading|Checking)\s+([a-zA-Z0-9_-]+)\s+v([^\s]+)"
)


def filter_cargo_build(raw_output: str) -> str:
    """Compact cargo build / cargo check output, suppressing routine compilation lines."""
    if not raw_output.strip():
        return raw_output

    lines = raw_output.splitlines()
    result: list[str] = []
    compiled_crates: list[str] = []

    def flush_crates():
        nonlocal compiled_crates
        if compiled_crates:
            if len(compiled_crates) > 2:
                result.append(f"[UsageTrim: compiled/checked {len(compiled_crates)} crates]")
            else:
                for c in compiled_crates:
                    result.append(f"   Compiling {c}")
            compiled_crates = []

    for line in lines:
        m = _CARGO_STEP_RE.match(line)
        if m:
            compiled_crates.append(f"{m.group(1)} v{m.group(2)}")
            continue

        flush_crates()
        result.append(line)

    flush_crates()
    return "\n".join(result)


_PIP_PROGRESS_RE = re.compile(r"^\s*(?:━+|\|+|\d+%).*(?:kB/s|MB/s|eta)")
_PIP_COLLECTING_RE = re.compile(r"^\s*Collecting\s+([a-zA-Z0-9_.-]+)")
_PIP_DOWNLOAD_RE = re.compile(r"^\s*(?:Downloading|Using cached)\s+([a-zA-Z0-9_.-]+)")
_PIP_SATISFIED_RE = re.compile(r"^\s*Requirement already satisfied:\s+([a-zA-Z0-9_.-]+)")


def _normalize_pip_pkg(raw: str) -> str:
    base = re.split(r"[><=~;\[\s]", raw)[0]
    base = base.split("-")[0]
    return base.lower()


def filter_pip_install(raw_output: str) -> str:
    """Compact pip / uv pip install output, suppressing progress bars and download lines."""
    if not raw_output.strip():
        return raw_output

    lines = raw_output.splitlines()
    result: list[str] = []
    downloaded_pkgs: set[str] = set()
    satisfied_count = 0

    def flush_downloads():
        nonlocal downloaded_pkgs
        if downloaded_pkgs:
            result.append(f"[UsageTrim: resolved/downloaded {len(downloaded_pkgs)} packages]")
            downloaded_pkgs = set()

    def flush_satisfied():
        nonlocal satisfied_count
        if satisfied_count > 0:
            result.append(f"[UsageTrim: {satisfied_count} requirements already satisfied]")
            satisfied_count = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if _PIP_PROGRESS_RE.search(stripped):
            continue

        m_sat = _PIP_SATISFIED_RE.match(stripped)
        if m_sat:
            flush_downloads()
            satisfied_count += 1
            continue

        m_coll = _PIP_COLLECTING_RE.match(stripped)
        if m_coll:
            flush_satisfied()
            downloaded_pkgs.add(_normalize_pip_pkg(m_coll.group(1)))
            continue

        m_down = _PIP_DOWNLOAD_RE.match(stripped)
        if m_down:
            flush_satisfied()
            downloaded_pkgs.add(_normalize_pip_pkg(m_down.group(1)))
            continue

        if stripped.startswith("Installing collected packages:"):
            flush_downloads()
            flush_satisfied()
            result.append(stripped)
            continue

        flush_downloads()
        flush_satisfied()
        result.append(line)

    flush_downloads()
    flush_satisfied()
    return "\n".join(result)


# uv sync / uv add print one ``+ pkg==ver`` line per change. Verbose mode also dumps
# thousands of ``DEBUG`` cache lines. Keep the summary and failures; fold the rest.
_UV_PROJECT_SUBCOMMANDS = frozenset(
    {
        "sync",
        "add",
        "remove",
        "lock",
        "upgrade",
        "tree",
        "export",
    }
)


def _uv_argv(command: str) -> list[str] | None:
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words or PurePath(words[0]).name.lower() not in {"uv", "uv.exe"}:
        return None
    return words


def _is_uv_project_command(command: str) -> bool:
    """Recognize ``uv sync`` / ``uv add`` / … but not ``uv pip install`` (pip filter)."""
    words = _uv_argv(command)
    if not words or len(words) < 2:
        return False
    index = 1
    while index < len(words) and words[index].startswith("-"):
        # Global flags may take values (``uv -n sync`` / ``uv --directory x sync``).
        flag = words[index]
        if flag in {
            "--directory",
            "--cache-dir",
            "--python",
            "--config-file",
            "-p",
        } and index + 1 < len(words):
            index += 2
            continue
        if flag.startswith("--") and "=" in flag:
            index += 1
            continue
        index += 1
    if index >= len(words):
        return False
    sub = words[index].lower()
    if sub == "pip":
        # ``uv pip install|uninstall|freeze|list`` keep the pip specializer / passthrough.
        rest = [w.lower() for w in words[index + 1 :] if not w.startswith("-")]
        return bool(rest) and rest[0] in {"sync", "compile"}
    return sub in _UV_PROJECT_SUBCOMMANDS


def filter_uv_project(raw_output: str) -> str:
    """Compact ``uv sync`` / ``uv add`` / ``uv remove`` / ``uv lock`` style output.

    Drops ``DEBUG`` noise and download/build progress, keeps resolution summaries and
    warnings/errors, and collapses per-package ``+``/``-``/``~`` change lists.
    """
    if not raw_output.strip():
        return raw_output

    lines = raw_output.splitlines()
    result: list[str] = []
    changes: list[tuple[str, str]] = []

    def flush_changes() -> None:
        nonlocal changes
        if not changes:
            return
        if len(changes) <= 3:
            result.extend(line for _, line in changes)
        else:
            added = sum(1 for marker, _ in changes if marker == "+")
            removed = sum(1 for marker, _ in changes if marker == "-")
            updated = sum(1 for marker, _ in changes if marker == "~")
            parts: list[str] = []
            if added:
                parts.append(f"+{added}")
            if removed:
                parts.append(f"−{removed}")
            if updated:
                parts.append(f"~{updated}")
            result.append(f"[UsageTrim: {' '.join(parts)} package changes collapsed]")
        changes = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("DEBUG ", "TRACE ")):
            continue
        # Per-package change lines: `` + pkg==1.0.0`` / `` - pkg==1.0.0`` / `` ~ pkg==1.0.0``.
        if len(stripped) > 2 and stripped[0] in "+-~" and stripped[1] == " ":
            marker = stripped[0]
            # Prefer the original leading-space form when present.
            changes.append((marker, line if line[:1] == " " else f" {stripped}"))
            continue
        # Download/build chatter (not the ``Downloaded N packages`` summary style).
        if re.match(
            r"^(?:Downloading|Downloaded|Building|Built)\b",
            stripped,
            re.IGNORECASE,
        ) and not re.match(r"^(?:Downloaded|Built)\s+\d+\s+", stripped, re.IGNORECASE):
            continue

        flush_changes()
        result.append(line)

    flush_changes()
    return "\n".join(result)


_NPM_WARN_DEPRECATED = re.compile(r"^\s*npm\s+warn\s+deprecated\s+(.*)")


def filter_npm_install(raw_output: str) -> str:
    """Compact npm/pnpm/yarn install output, grouping routine deprecations and funding messages."""
    if not raw_output.strip():
        return raw_output

    lines = raw_output.splitlines()
    result: list[str] = []
    deprecations: list[str] = []

    def flush_deprecations():
        nonlocal deprecations
        if deprecations:
            if len(deprecations) > 2:
                result.append(
                    f"[UsageTrim: {len(deprecations)} package deprecation warnings collapsed]"
                )
            else:
                for d in deprecations:
                    result.append(f"npm warn deprecated {d}")
            deprecations = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        m_dep = _NPM_WARN_DEPRECATED.match(stripped)
        if m_dep:
            deprecations.append(m_dep.group(1))
            continue

        if "packages are looking for funding" in stripped or stripped.startswith("run `npm fund`"):
            flush_deprecations()
            continue

        flush_deprecations()
        result.append(line)

    flush_deprecations()
    return "\n".join(result)


_PYTHON_TB_HEADER = re.compile(r"^Traceback \(most recent call (?:last|first)\):", re.MULTILINE)
_PYTHON_FRAME_START = re.compile(r'^  File "([^"]+)", line (\d+)(?:, in (.*))?')
_NODE_FRAME_START = re.compile(r"^\s+at\s+(?:.*?\s+\()?([^\s\)]+)(?:\))?")


def _is_library_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        kw in normalized
        for kw in (
            "site-packages/",
            ".venv/",
            "/lib/python",
            "<frozen ",
            "/usr/lib/",
            "/usr/local/lib/",
            "node_modules/",
            "node:internal/",
            "internal/process/",
            "internal/modules/",
        )
    )


def filter_traceback(raw_text: str) -> str:
    """Compact verbose Python and Node.js tracebacks by folding internal library frames."""
    if not raw_text.strip():
        return raw_text

    # Python traceback detection
    if _PYTHON_TB_HEADER.search(raw_text):
        lines = raw_text.splitlines()
        result: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if _PYTHON_TB_HEADER.match(line):
                result.append(line)
                i += 1
                frames: list[list[str]] = []
                while i < len(lines):
                    f_match = _PYTHON_FRAME_START.match(lines[i])
                    if f_match:
                        frame_lines = [lines[i]]
                        i += 1
                        while (
                            i < len(lines)
                            and not _PYTHON_FRAME_START.match(lines[i])
                            and (lines[i].startswith("    ") or lines[i].startswith("  "))
                        ):
                            if lines[i].strip() and not _PYTHON_TB_HEADER.match(lines[i]):
                                frame_lines.append(lines[i])
                                i += 1
                            else:
                                break
                        frames.append(frame_lines)
                    else:
                        break

                if frames:
                    k = 0
                    while k < len(frames):
                        frame = frames[k]
                        m = _PYTHON_FRAME_START.match(frame[0])
                        path = m.group(1) if m else ""
                        if _is_library_path(path):
                            lib_start = k
                            while k < len(frames):
                                km = _PYTHON_FRAME_START.match(frames[k][0])
                                kp = km.group(1) if km else ""
                                if _is_library_path(kp):
                                    k += 1
                                else:
                                    break
                            lib_count = k - lib_start
                            if lib_count > 2:
                                result.extend(frames[lib_start])
                                omitted = lib_count - 2
                                result.append(
                                    f"  ... [{omitted} library frames in site-packages/ omitted; full trace recoverable via usagetrim_retrieve] ..."
                                )
                                result.extend(frames[k - 1])
                            else:
                                for idx in range(lib_start, k):
                                    result.extend(frames[idx])
                        else:
                            result.extend(frame)
                            k += 1
                continue

            result.append(line)
            i += 1

        ret = "\n".join(result)
        return ret + "\n" if raw_text.endswith("\n") else ret

    # Node.js traceback detection
    node_err_match = re.search(r"^(?:[A-Za-z]+Error|Error):.*", raw_text, re.MULTILINE)
    if node_err_match and re.search(r"^\s+at\s+", raw_text, re.MULTILINE):
        lines = raw_text.splitlines()
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if re.match(r"^\s+at\s+", line):
                lib_frames: list[str] = []
                while i < len(lines) and re.match(r"^\s+at\s+", lines[i]):
                    lm = _NODE_FRAME_START.match(lines[i])
                    path = lm.group(1) if lm else ""
                    if _is_library_path(path):
                        lib_frames.append(lines[i])
                        i += 1
                    else:
                        break
                if len(lib_frames) > 2:
                    result.append(lib_frames[0])
                    result.append(
                        f"    ... [{len(lib_frames) - 2} internal/node_modules frames omitted; full trace recoverable via usagetrim_retrieve] ..."
                    )
                    result.append(lib_frames[-1])
                else:
                    result.extend(lib_frames)
                if i < len(lines) and re.match(r"^\s+at\s+", lines[i]):
                    result.append(lines[i])
                    i += 1
                continue
            result.append(line)
            i += 1
        ret = "\n".join(result)
        return ret + "\n" if raw_text.endswith("\n") else ret

    return raw_text


_HTTP_LOG_RE = re.compile(
    r"^\s*(?:\[\d{2}:\d{2}:\d{2}\]\s+)?(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)\s+(2\d\d|3\d\d)\b(.*)",
    re.IGNORECASE,
)
_VITE_HMR_RE = re.compile(r"^\s*\[vite\]\s+hmr\s+update\s+(\S+)", re.IGNORECASE)
_STATIC_ASSET_RE = re.compile(
    r"^\s*(?:\[\d{2}:\d{2}:\d{2}\]\s+)?GET\s+(/(?:_next|static|assets|@vite|node_modules)/\S+)\s+(2\d\d|3\d\d)\b",
    re.IGNORECASE,
)


def filter_dev_server_logs(raw_text: str) -> str:
    """Compact dev server outputs (Vite HMR, Next.js, repeated HTTP 200/304 access logs)."""
    if not raw_text.strip():
        return raw_text

    lines = raw_text.splitlines()
    result: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Check for Vite HMR updates
        if _VITE_HMR_RE.match(stripped):
            hmr_count = 0
            while i < len(lines) and _VITE_HMR_RE.match(lines[i].strip()):
                hmr_count += 1
                i += 1
            if hmr_count > 2:
                result.append(f"[UsageTrim: {hmr_count} Vite HMR updates collapsed]")
            else:
                for k in range(i - hmr_count, i):
                    result.append(lines[k])
            continue

        # Check for static asset requests
        if _STATIC_ASSET_RE.match(stripped):
            static_count = 0
            while i < len(lines) and _STATIC_ASSET_RE.match(lines[i].strip()):
                static_count += 1
                i += 1
            if static_count > 2:
                result.append(
                    f"[UsageTrim: {static_count} static asset requests (200/304 OK) collapsed]"
                )
            else:
                for k in range(i - static_count, i):
                    result.append(lines[k])
            continue

        # Check for repeated identical HTTP requests
        m_http = _HTTP_LOG_RE.match(stripped)
        if m_http:
            method, path, status = m_http.group(1), m_http.group(2), m_http.group(3)
            key = (method, path, status)
            rep_count = 1
            j = i + 1
            while j < len(lines):
                mj = _HTTP_LOG_RE.match(lines[j].strip())
                if mj and (mj.group(1), mj.group(2), mj.group(3)) == key:
                    rep_count += 1
                    j += 1
                else:
                    break
            if rep_count > 2:
                result.append(f"{method} {path} {status} [UsageTrim: repeated {rep_count}x]")
                i = j
                continue

        result.append(line)
        i += 1

    ret = "\n".join(result)
    return ret + "\n" if raw_text.endswith("\n") else ret


_NOISY_DIR_NAMES = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".git",
        ".next",
        ".nuxt",
        "dist",
        "build",
        "target",
        ".turbo",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "coverage",
        ".gradle",
        "vendor",
    }
)


def filter_directory_scan(raw_output: str) -> str:
    """Compact verbose find / tree / ls -R directory listings by collapsing noisy vendor/cache dirs."""
    if not raw_output.strip():
        return raw_output

    lines = raw_output.splitlines()
    result: list[str] = []
    noisy_dirs_seen: dict[str, int] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append(line)
            continue

        parts = stripped.replace("\\", "/").split("/")
        noisy_idx = next((idx for idx, part in enumerate(parts) if part in _NOISY_DIR_NAMES), None)

        if noisy_idx is not None:
            noisy_root = "/".join(parts[: noisy_idx + 1])
            noisy_dirs_seen[noisy_root] = noisy_dirs_seen.get(noisy_root, 0) + 1
            continue

        result.append(line)

    if noisy_dirs_seen:
        for root, count in sorted(noisy_dirs_seen.items()):
            result.append(
                f"{root}/ [... {count} items omitted by usagetrim; use targeted path to inspect ...]"
            )

    ret = "\n".join(result)
    return ret + "\n" if raw_output.endswith("\n") else ret


def filter_git_branch(raw_output: str) -> str:
    """Compact verbose git branch / git branch -a listings, grouping noisy remote branches."""
    if not raw_output.strip():
        return raw_output

    lines = raw_output.splitlines()
    result: list[str] = []
    remote_groups: dict[str, list[str]] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if line.startswith("*") or "-> " in line or not stripped.startswith("remotes/"):
            result.append(line)
            continue

        parts = stripped.split("/")
        if len(parts) >= 3:
            group_prefix = "/".join(parts[:3])
            remote_groups.setdefault(group_prefix, []).append(line)
        else:
            result.append(line)

    for prefix, group_lines in sorted(remote_groups.items()):
        if len(group_lines) > 2:
            result.append(f"  {prefix}/* [... {len(group_lines)} branches collapsed ...]")
        else:
            result.extend(group_lines)

    ret = "\n".join(result)
    return ret + "\n" if raw_output.endswith("\n") else ret


def filter_curl_http(raw_output: str) -> str:
    """Compact verbose curl -v and HTTP response logs."""
    if not raw_output.strip():
        return raw_output

    lines = raw_output.splitlines()
    result: list[str] = []
    header_count = 0
    in_headers = False

    routine_headers = {
        "date",
        "server",
        "etag",
        "keep-alive",
        "connection",
        "vary",
        "x-powered-by",
        "x-process-time",
        "access-control-allow-origin",
        "access-control-allow-credentials",
        "strict-transport-security",
    }

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("* "):
            if "Connected to" in stripped or "Trying" in stripped:
                result.append(stripped)
            continue

        if stripped.startswith("< "):
            in_headers = True
            hdr = stripped[2:].strip().lower()
            if hdr.startswith("http/"):
                result.append(stripped)
                continue
            hdr_name = hdr.split(":")[0].strip()
            if hdr_name in routine_headers:
                header_count += 1
                continue
            result.append(stripped)
            continue

        if in_headers and not stripped:
            in_headers = False
            if header_count > 0:
                result.append(f"< [... {header_count} routine response headers collapsed ...]")
                header_count = 0

        if "<script" in stripped.lower() or "<style" in stripped.lower():
            continue

        result.append(line)

    if header_count > 0:
        result.append(f"< [... {header_count} routine response headers collapsed ...]")

    body_text = "\n".join(result)
    return body_text + "\n" if raw_output.endswith("\n") else body_text


_SQL_QUERY_PATTERN = re.compile(
    r"(?:prisma:query|\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b)",
    re.IGNORECASE,
)
_SQL_ERROR_PATTERN = re.compile(
    r"(?:error|exception|failed|fatal|rollback|violat|denied|deadlock|timeout|traceback)",
    re.IGNORECASE,
)


def _normalize_sql_skeleton(line: str) -> str:
    """Extract a structural template of a SQL line by replacing literals and parameter bindings."""
    cleaned = re.sub(
        r"^\[?[0-9\-:,\. ]+\]?\s*(?:INFO|DEBUG|NOTICE)?\s*(?:(?:prisma:query|sqlalchemy\.engine(?:\.Engine)?|django\.db\.backends|query:?)\s*)?",
        "",
        line,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"'[^']*'", "'?'", cleaned)
    cleaned = re.sub(r'"[^"]*"', '"?"', cleaned)
    cleaned = re.sub(r"\b\d+\b", "?", cleaned)
    cleaned = re.sub(r"\$[0-9]+", "?", cleaned)
    cleaned = re.sub(r":\w+", "?", cleaned)
    cleaned = re.sub(r"%\([^)]+\)s", "?", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def filter_sql_logs(raw_text: str) -> str:
    """Fold repetitive SQL / ORM queries (Prisma, Django, SQLAlchemy, Drizzle) into compact counts."""
    if not raw_text.strip():
        return raw_text

    lines = raw_text.splitlines()
    result: list[str] = []
    pending_group: list[str] = []
    current_skeleton = ""

    def flush_pending():
        nonlocal pending_group, current_skeleton
        if not pending_group:
            return
        if len(pending_group) <= 2:
            result.extend(pending_group)
        else:
            result.append(pending_group[0])
            skel_display = current_skeleton[:80] + ("..." if len(current_skeleton) > 80 else "")
            collapsed_count = len(pending_group) - 1
            result.append(
                f"  [... {collapsed_count} repeated queries matching '{skel_display}' collapsed by usagetrim ...]"
            )
        pending_group = []
        current_skeleton = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_pending()
            result.append(line)
            continue

        if _SQL_ERROR_PATTERN.search(line):
            flush_pending()
            result.append(line)
            continue

        if _SQL_QUERY_PATTERN.search(line):
            skel = _normalize_sql_skeleton(line)
            if skel == current_skeleton and current_skeleton:
                pending_group.append(line)
                continue
            else:
                flush_pending()
                current_skeleton = skel
                pending_group.append(line)
                continue
        else:
            flush_pending()
            result.append(line)

    flush_pending()

    ret = "\n".join(result)
    return ret + "\n" if raw_text.endswith("\n") else ret


def _is_sql_dense_output(raw_output: str) -> bool:
    matches = sum(1 for line in raw_output.splitlines()[:60] if _SQL_QUERY_PATTERN.search(line))
    return matches >= 4


# ---------------------------------------------------------------------------
# ripgrep / recursive grep — cluster matches by file, drop context, cap density
# ---------------------------------------------------------------------------

_RG_JSON_TYPES = frozenset({"begin", "match", "context", "end", "summary"})
_RG_TEXT_MATCH_RE = re.compile(r"^(.+?):(\d+):(.*)$")
_RG_TEXT_CONTEXT_RE = re.compile(r"^(.+?)-(\d+)-(.*)$")


def _rg_path_from_data(data: object) -> str:
    if not isinstance(data, dict):
        return "?"
    path = data.get("path")
    if isinstance(path, dict):
        text = path.get("text")
        if isinstance(text, str):
            return text
        raw = path.get("bytes")
        if isinstance(raw, str):
            return raw
    if isinstance(path, str):
        return path
    return "?"


def _rg_line_text_from_data(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    lines = data.get("lines")
    if isinstance(lines, dict):
        text = lines.get("text")
        if isinstance(text, str):
            return text.rstrip("\n")
        raw = lines.get("bytes")
        if isinstance(raw, str):
            return raw.rstrip("\n")
    return ""


def _looks_like_rg_json(raw_output: str) -> bool:
    """True when output is mostly ripgrep ``--json`` NDJSON events."""
    parsed = 0
    checked = 0
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        checked += 1
        if checked > 40:
            break
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("type") in _RG_JSON_TYPES:
            parsed += 1
    return parsed >= 2 and parsed >= max(1, checked // 2)


def _parse_rg_json_matches(
    raw_output: str,
) -> tuple[list[tuple[str, int | None, str]], dict[str, int]]:
    """Return (ordered match tuples, per-file total counts) from ``rg --json``."""
    matches: list[tuple[str, int | None, str]] = []
    counts: dict[str, int] = {}
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "match":
            # begin / context / end / summary are dropped — context is the bulk.
            continue
        data = event.get("data")
        path = _rg_path_from_data(data)
        line_no = None
        text = ""
        if isinstance(data, dict):
            raw_ln = data.get("line_number")
            if isinstance(raw_ln, int):
                line_no = raw_ln
            text = _rg_line_text_from_data(data)
        matches.append((path, line_no, text))
        counts[path] = counts.get(path, 0) + 1
    return matches, counts


def _parse_rg_text_matches(
    raw_output: str,
) -> tuple[list[tuple[str, int | None, str]], dict[str, int]]:
    """Parse classic ``rg`` / ``grep -rn`` text lines; drop ``-C`` context rows."""
    matches: list[tuple[str, int | None, str]] = []
    counts: dict[str, int] = {}
    for line in raw_output.splitlines():
        stripped = line.rstrip("\n")
        if not stripped or stripped == "--":
            continue
        # Context lines use ``path-lineno-text``; matches use ``path:lineno:text``.
        if _RG_TEXT_CONTEXT_RE.match(stripped) and not _RG_TEXT_MATCH_RE.match(stripped):
            continue
        match = _RG_TEXT_MATCH_RE.match(stripped)
        if match:
            path, lineno_s, text = match.group(1), match.group(2), match.group(3)
            line_no = int(lineno_s)
            matches.append((path, line_no, text))
            counts[path] = counts.get(path, 0) + 1
            continue
        # ``grep -r`` without ``-n``: ``path:content`` (skip binary notices).
        if ": matches binary file" in stripped.lower():
            continue
        if ":" in stripped and not stripped.startswith("{"):
            path, text = stripped.split(":", 1)
            looks_like_path = "/" in path or path.endswith(
                (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".h", ".md", ".txt")
            )
            if path and text and looks_like_path:
                matches.append((path, None, text))
                counts[path] = counts.get(path, 0) + 1
    return matches, counts


def _format_rg_clustered(
    matches: list[tuple[str, int | None, str]],
    counts: dict[str, int],
    *,
    max_matches_per_file: int,
    max_total_matches: int,
) -> str:
    """Render clustered matches with per-file and global caps."""
    if not matches:
        return ""

    # Preserve first-seen file order.
    file_order: list[str] = []
    by_file: dict[str, list[tuple[int | None, str]]] = {}
    for path, line_no, text in matches:
        if path not in by_file:
            by_file[path] = []
            file_order.append(path)
        by_file[path].append((line_no, text))

    result: list[str] = []
    shown_total = 0
    omitted_file_matches = 0
    omitted_files = 0

    for path in file_order:
        file_matches = by_file[path]
        total_in_file = counts.get(path, len(file_matches))
        remaining_budget = max_total_matches - shown_total
        if remaining_budget <= 0:
            omitted_files += 1
            omitted_file_matches += total_in_file
            continue

        keep_n = min(max_matches_per_file, remaining_budget, total_in_file)
        shown = file_matches[:keep_n]
        omitted_here = total_in_file - keep_n
        if omitted_here > 0:
            header = (
                f"{path}: {total_in_file} matches "
                f"({keep_n} shown, {omitted_here} omitted by usagetrim)"
            )
        elif total_in_file == 1:
            header = f"{path}: 1 match"
        else:
            header = f"{path}: {total_in_file} matches"
        result.append(header)
        for line_no, text in shown:
            if line_no is None:
                result.append(f"  {text}")
            else:
                result.append(f"  {line_no}: {text}")
        shown_total += keep_n

    if omitted_files:
        result.append(
            f"[... {omitted_file_matches} matches across {omitted_files} more files "
            f"omitted by usagetrim ...]"
        )

    return "\n".join(result)


def filter_ripgrep_output(
    raw_output: str,
    max_matches_per_file: int = 3,
    max_total_matches: int = 30,
) -> str:
    """Cluster ripgrep/grep matches by file; drop context; cap match density.

    Handles both ``rg --json`` NDJSON event streams and classic ``path:line:text``
    (or ``grep -rn``) text output. Context events/lines are dropped — they are the
    bulk of tokens on ``-C`` / ``-A`` / ``-B`` searches. Files that dominate the
    hit list fold to a count header plus the first ``max_matches_per_file`` rows.
    """
    if not raw_output.strip():
        return raw_output

    is_json = _looks_like_rg_json(raw_output)
    if is_json:
        matches, counts = _parse_rg_json_matches(raw_output)
        had_context = any(
            '"type":"context"' in line or '"type": "context"' in line
            for line in raw_output.splitlines()
        )
    else:
        matches, counts = _parse_rg_text_matches(raw_output)
        had_context = any(
            bool(_RG_TEXT_CONTEXT_RE.match(line)) and not _RG_TEXT_MATCH_RE.match(line)
            for line in raw_output.splitlines()
            if line.strip() and line.strip() != "--"
        )

    if not matches:
        return raw_output

    total = sum(counts.values())
    max_per_file = max(counts.values()) if counts else 0
    needs_fold = had_context or total > max_total_matches or max_per_file > max_matches_per_file

    compact = _format_rg_clustered(
        matches,
        counts,
        max_matches_per_file=max_matches_per_file,
        max_total_matches=max_total_matches,
    )
    if not compact:
        return raw_output
    if needs_fold or len(compact) < len(raw_output):
        return compact + ("\n" if raw_output.endswith("\n") else "")
    return raw_output


def _is_ripgrep_or_recursive_grep(command: str) -> bool:
    """Recognize ``rg`` / ``grep -r`` (and absolute-path binaries)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    binary = PurePath(words[0]).name.lower()
    if binary in {"rg", "rg.exe"}:
        return True
    if binary in {"grep", "grep.exe", "ggrep"}:
        # Recursive forms: -r, -R, -rR, --recursive, or combined flags like -rn.
        for word in words[1:]:
            if word in {"-r", "-R", "--recursive"}:
                return True
            if word.startswith("-") and not word.startswith("--"):
                # Combined short flags: -rn, -rI, -HR, etc.
                if "r" in word[1:] or "R" in word[1:]:
                    return True
        return False
    return False


def _is_directory_scan_command(cmd_lower: str) -> bool:
    prefixes = (
        "find ",
        "find\t",
        "ls -r",
        "ls -la -r",
        "ls -al -r",
        "tree",
    )
    return cmd_lower in {"find", "find .", "tree"} or any(cmd_lower.startswith(p) for p in prefixes)


def auto_specialize_command_output(command: str, raw_output: str) -> str | None:
    """Detect if command has a specialized ultra-dense filter."""
    cmd_lower = command.lower().strip()
    cargo_sub = _cargo_subcommand(command.strip())
    gh_compact = filter_gh_command_output(command, raw_output)
    if gh_compact is not None:
        return gh_compact
    if cmd_lower.startswith("git log"):
        return filter_git_log(raw_output)
    elif cmd_lower.startswith("git status"):
        return filter_git_status(raw_output)
    elif cmd_lower.startswith("git branch"):
        return filter_git_branch(raw_output)
    elif cmd_lower.startswith(("git diff", "git show")):
        return filter_git_diff(raw_output)
    elif _is_ripgrep_or_recursive_grep(command.strip()):
        return filter_ripgrep_output(raw_output)
    elif cmd_lower.startswith(("curl ", "curl\t", "wget ", "http ", "https ")):
        return filter_curl_http(raw_output)
    elif cargo_sub in {"test", "nextest"}:
        return filter_cargo_test(raw_output)
    elif cargo_sub in {"build", "check"}:
        return filter_cargo_build(raw_output)
    elif _is_go_test_command(command.strip()):
        return filter_go_test(raw_output)
    elif _is_uv_project_command(command.strip()):
        return filter_uv_project(raw_output)
    elif any(
        cmd_lower.startswith(prefix)
        for prefix in (
            "pip install",
            "pip3 install",
            "uv pip install",
            "poetry add",
            "poetry install",
        )
    ) or any(kw in cmd_lower for kw in ("python -m pip install", "python3 -m pip install")):
        return filter_pip_install(raw_output)
    elif any(
        cmd_lower.startswith(prefix)
        for prefix in (
            "npm test",
            "npm run test",
            "pnpm test",
            "pnpm run test",
            "yarn test",
            "yarn run test",
            "bun test",
            "bun run test",
            "vitest",
            "npx vitest",
            "jest",
            "npx jest",
        )
    ):
        return filter_jest_vitest(raw_output)
    elif any(
        cmd_lower.startswith(prefix)
        for prefix in (
            "npm install",
            "npm i ",
            "pnpm install",
            "pnpm i ",
            "pnpm add",
            "yarn add",
            "yarn install",
            "bun add",
            "bun install",
        )
    ) or cmd_lower in ("npm i", "pnpm i", "yarn", "pnpm install", "npm install"):
        return filter_npm_install(raw_output)
    elif any(
        cmd_lower.startswith(prefix)
        for prefix in (
            "tsc",
            "npx tsc",
            "pnpm tsc",
            "yarn tsc",
            "bun x tsc",
        )
    ):
        return filter_tsc(raw_output)
    elif _is_mypy_command(cmd_lower):
        return filter_mypy(raw_output)
    elif _is_eslint_command(cmd_lower):
        return filter_eslint(raw_output)
    elif _is_ruff_command(cmd_lower):
        return filter_ruff(raw_output)
    elif _is_docker_build_command(cmd_lower):
        return filter_docker_build(raw_output)
    elif _is_pyright_command(cmd_lower):
        return filter_pyright(raw_output)
    elif _is_kubectl_command(command.strip()):
        return filter_kubectl(raw_output, command=command)
    elif _is_helm_command(command.strip()):
        return filter_helm(raw_output, command=command)
    elif _is_terraform_command(command.strip()):
        return filter_terraform_plan(raw_output)
    elif _is_ci_log_command(command.strip()):
        res = filter_ci_logs(raw_output)
        if res != raw_output:
            return res

    if _looks_like_ci_log(raw_output):
        res = filter_ci_logs(raw_output)
        if res != raw_output:
            return res

    if _is_directory_scan_command(cmd_lower):
        res = filter_directory_scan(raw_output)
        if res != raw_output:
            return res

    if _is_dev_server_output(cmd_lower, raw_output):
        res = filter_dev_server_logs(raw_output)
        if res != raw_output:
            return res

    if _is_traceback_output(cmd_lower, raw_output):
        res = filter_traceback(raw_output)
        if res != raw_output:
            return res

    if any(
        cmd_lower.startswith(p)
        for p in (
            "prisma",
            "npx prisma",
            "pnpm prisma",
            "alembic",
            "drizzle-kit",
            "npx drizzle-kit",
            "python manage.py",
            "python3 manage.py",
        )
    ) or any(db_sub in cmd_lower for db_sub in ("db:migrate", "db:seed", "makemigrations")):
        res = filter_sql_logs(raw_output)
        if res != raw_output:
            return res

    if _is_sql_dense_output(raw_output):
        res = filter_sql_logs(raw_output)
        if res != raw_output:
            return res

    # Check for large or verbose JSON output
    json_result = filter_json_output(raw_output, command=command)
    if json_result is not None:
        return json_result

    return None


def _is_dev_server_output(cmd_lower: str, raw_output: str) -> bool:
    dev_cmds = (
        "dev",
        "serve",
        "start",
        "vite",
        "next dev",
        "nuxt dev",
        "uvicorn",
        "fastapi dev",
    )
    if any(cmd_lower.startswith(c) or f" {c} " in f" {cmd_lower} " for c in dev_cmds):
        return True
    return bool(_VITE_HMR_RE.search(raw_output) or _STATIC_ASSET_RE.search(raw_output))


def _is_traceback_output(cmd_lower: str, raw_output: str) -> bool:
    return bool(
        _PYTHON_TB_HEADER.search(raw_output)
        or (
            re.search(r"^(?:[A-Za-z]+Error|Error):.*", raw_output, re.MULTILINE)
            and re.search(r"^\s+at\s+", raw_output, re.MULTILINE)
        )
    )


_CARGO_VALUE_FLAGS = frozenset(
    {
        "--manifest-path",
        "--target",
        "--target-dir",
        "--color",
        "--config",
        "-Z",
        "--profile",
        "--package",
        "-p",
        "--features",
        "--bin",
        "--example",
        "--test",
        "--bench",
        "--message-format",
        "--jobs",
        "-j",
    }
)


def _cargo_subcommand(command: str) -> str | None:
    """Return cargo's subcommand (``test``, ``nextest``, ``build``, …) or None."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words or PurePath(words[0]).name.lower() not in {"cargo", "cargo.exe"}:
        return None
    index = 1
    while index < len(words):
        word = words[index]
        if word == "--":
            return None
        if word.startswith("+"):
            index += 1
            continue
        if word in _CARGO_VALUE_FLAGS:
            index += 2
            continue
        if word.startswith("--") and "=" in word:
            index += 1
            continue
        if word.startswith("-"):
            index += 1
            continue
        return word.lower()
    return None


def _is_go_test_command(command: str) -> bool:
    """Recognize ``go test`` including absolute paths to the go binary."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) < 2:
        return False
    return PurePath(words[0]).name.lower() in {"go", "go.exe"} and words[1] == "test"


def _is_mypy_command(cmd_lower: str) -> bool:
    """Recognize direct and common launcher forms for mypy."""
    prefixes = (
        "mypy",
        "uv run mypy",
        "uvx mypy",
        "python -m mypy",
        "python3 -m mypy",
        "poetry run mypy",
        "pipenv run mypy",
    )
    return any(
        cmd_lower == prefix
        or cmd_lower.startswith(prefix + " ")
        or cmd_lower.startswith(prefix + "\t")
        for prefix in prefixes
    )


def _is_eslint_command(cmd_lower: str) -> bool:
    """Recognize direct and common launcher forms for ESLint."""
    prefixes = (
        "eslint ",
        "eslint\t",
        "npx eslint",
        "pnpm eslint",
        "yarn eslint",
        "bunx eslint",
        "bun x eslint",
        "npm exec eslint",
    )
    if cmd_lower == "eslint" or any(cmd_lower.startswith(prefix) for prefix in prefixes):
        return True
    return False


def _is_ruff_command(cmd_lower: str) -> bool:
    """Recognize direct and common launcher forms for Ruff."""
    prefixes = (
        "ruff ",
        "ruff\t",
        "uv run ruff",
        "uvx ruff",
        "python -m ruff",
        "python3 -m ruff",
    )
    if cmd_lower == "ruff" or any(cmd_lower.startswith(prefix) for prefix in prefixes):
        return True
    return False


def _is_docker_build_command(cmd_lower: str) -> bool:
    """Recognize docker/podman build and buildx build forms."""
    prefixes = (
        "docker build",
        "docker buildx build",
        "docker-compose build",
        "docker compose build",
        "podman build",
        "podman buildx build",
    )
    return any(cmd_lower == prefix or cmd_lower.startswith(prefix + " ") for prefix in prefixes)


def _is_pyright_command(cmd_lower: str) -> bool:
    """Recognize Pyright and basedpyright, including npx/pnpm/yarn launchers."""
    prefixes = (
        "pyright",
        "basedpyright",
        "npx pyright",
        "npx basedpyright",
        "pnpm pyright",
        "pnpm exec pyright",
        "yarn pyright",
        "yarn exec pyright",
        "bunx pyright",
        "bunx basedpyright",
    )
    return any(
        cmd_lower == prefix
        or cmd_lower.startswith(prefix + " ")
        or cmd_lower.startswith(prefix + "\t")
        for prefix in prefixes
    )


# ---------------------------------------------------------------------------
# kubectl / oc / helm — describe/get/logs noise and chart packaging chatter
# ---------------------------------------------------------------------------

_KUBECTL_GET_HEADER_RE = re.compile(
    r"^NAME\s+READY\s+STATUS\b|^NAME\s+STATUS\b|^NAME\s+AGE\b", re.I
)
_KUBECTL_EVENT_ROW_RE = re.compile(r"^\s*(Normal|Warning|Error)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.*)$")
# Successful readiness/liveness/startup probe chatter in kubectl logs.
_KUBECTL_PROBE_OK_RE = re.compile(
    r"(?i)(?:"
    r"\bkube-probe\b|"
    r'(?:GET|HEAD)\s+/(?:healthz|readyz|livez|health|ready|live)\b[^"\n]*\b200\b|'
    r'"(?:GET|HEAD)\s+/(?:healthz|readyz|livez|health|ready|live)[^"]*"\s+200\b|'
    r"\b(?:readiness|liveness|startup)\s+probe\s+succeeded\b|"
    r'"Probe succeeded".*probeType='
    r")"
)
_KUBECTL_PROBE_FAIL_RE = re.compile(
    r"(?i)(?:"
    r"probe\s+failed|"
    r"\b(?:healthz|readyz|livez)\b[^.\n]*(?:5\d\d|error|refused|timeout|unhealthy)|"
    r"\bUnhealthy\b"
    r")"
)
_KUBECTL_MIN_PROBE_FOLD = 3
# Refresh/read chatter from terraform plan/apply/destroy and OpenTofu (tofu).
_TF_REFRESH_RE = re.compile(
    r"^(\S+):\s+(?:Reading\.\.\.|Read complete after\b|Refreshing state\.\.\.|Refreshing\.\.\.)"
)
_TF_REFRESH_NOTICE_RE = re.compile(r"refresh/read lines collapsed", re.IGNORECASE)
_TF_UNCHANGED_FOLD_RE = re.compile(r"unchanged attributes folded", re.IGNORECASE)
_TF_CHANGED_LINE_RE = re.compile(r"^(\s*)([+~-])(?:\s|$)")
_TF_SIMPLE_ATTR_RE = re.compile(r"^(\s+)(?![+~-])(\S[^=\n]*?)\s*=\s*(.*)$")
_TF_PLAN_SUMMARY_RE = re.compile(
    r"^(?:Plan|No changes\.):\s+\d+\s+to add,\s+\d+\s+to change,\s+\d+\s+to destroy",
    re.IGNORECASE,
)
_TF_RESOURCE_HEADER_RE = re.compile(
    r"^\s*#\s+\S.+\s+will be\s+(?:created|updated|destroyed|replaced|read)",
    re.IGNORECASE,
)
_TF_ERROR_WARNING_RE = re.compile(r"(?i)(?:^\s*(?:Warning|Error):|^\s*[╷│╵]|\berror\b|\bwarning\b)")
_TF_MIN_UNCHANGED_FOLD = 3

_HELM_NOISE_RE = re.compile(
    r"(?i)^\.{0,3}(?:"
    r"Hang tight while we|"
    r"Successfully got an update from|"
    r"Update Complete\b|"
    r"Saving \d+ charts?\b|"
    r"Deleting outdated charts\b|"
    r"Downloading \S|"
    r"Pulled:\s|"
    r"Digest:\s|"
    r".*Happy Helming|"
    r"Skipping chart\b|"
    r"already exists and is not different\b|"
    r"Successfully packaged chart and saved it to:"
    r")"
)
_HELM_FOLD_NOTICE_RE = re.compile(r"chart/repo lines collapsed", re.IGNORECASE)
_HELM_MIN_NOISE_FOLD = 3


def filter_kubectl(raw_output: str, command: str = "") -> str:
    """Compact kubectl/oc get tables, describe dumps, and probe-heavy logs.

    Keeps unhealthy table rows, describe failure signal (State/Reason/Message,
    Warning events), and non-200 / failed probes. Folds routine annotations,
    env dumps, tolerations, Normal lifecycle events, and repetitive
    healthz/readyz/livez pings. Pure and idempotent on short / already-compacted
    input. Optional ``command`` is accepted for call-site clarity; content
    detection drives the path taken.
    """
    del command  # content-driven; kept for API symmetry with issue #52
    if not raw_output.strip():
        return raw_output

    lines = raw_output.splitlines()
    if any(_KUBECTL_GET_HEADER_RE.match(line.strip()) for line in lines):
        return _filter_kubectl_get(lines)
    if lines and lines[0].startswith("Name:"):
        return _filter_kubectl_describe(lines)
    return _filter_kubectl_logs(lines, raw_output)


_KUBECTL_READY_RE = re.compile(r"^(\d+)/(\d+)$")
_KUBECTL_RESTARTS_RE = re.compile(r"^(\d+)")


def _kubectl_row_is_routine(parts: list[str]) -> bool:
    """Report whether a `kubectl get pods` row carries nothing worth keeping.

    A row is routine only when nothing about it asks for attention. Restarts are the
    clearest sign a pod is unstable even while it currently reports Running, and a
    partially ready pod is degraded, so both are kept. Finished pods report 0/N ready
    by design, so readiness is only meaningful while a pod is still running.
    """
    ready = parts[1] if len(parts) > 1 else ""
    status = parts[2] if len(parts) > 2 else ""
    restarts = parts[3] if len(parts) > 3 else ""

    restart_match = _KUBECTL_RESTARTS_RE.match(restarts)
    if not restart_match or int(restart_match.group(1)):
        # An unreadable restart column means the row is not understood well enough
        # to drop, so it is kept.
        return False

    if status in {"Completed", "Succeeded"}:
        return True
    if status != "Running":
        return False

    ready_match = _KUBECTL_READY_RE.match(ready)
    return bool(ready_match) and ready_match.group(1) == ready_match.group(2)


def _filter_kubectl_get(lines: list[str]) -> str:
    header = None
    healthy: list[str] = []
    keep: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _KUBECTL_GET_HEADER_RE.match(stripped):
            header = line
            continue
        if _kubectl_row_is_routine(stripped.split()):
            healthy.append(stripped)
        else:
            keep.append(line)

    if not healthy:
        return "\n".join(lines)

    result: list[str] = []
    if header:
        result.append(header)
    result.append(f"[UsageTrim: {len(healthy)} healthy Running/Completed rows collapsed]")
    result.extend(keep)
    return "\n".join(result)


def _kubectl_is_probe_ok_line(line: str) -> bool:
    """True for routine successful health/readiness probe log lines."""
    if _KUBECTL_PROBE_FAIL_RE.search(line):
        return False
    return bool(_KUBECTL_PROBE_OK_RE.search(line))


def _filter_kubectl_logs(lines: list[str], raw_output: str) -> str:
    """Collapse consecutive successful health-probe lines; keep failures verbatim."""
    probe_ok_count = sum(1 for line in lines if _kubectl_is_probe_ok_line(line))
    if probe_ok_count < _KUBECTL_MIN_PROBE_FOLD:
        return raw_output

    result: list[str] = []
    pending: list[str] = []
    for line in lines:
        if _kubectl_is_probe_ok_line(line):
            pending.append(line)
            continue
        if len(pending) >= _KUBECTL_MIN_PROBE_FOLD:
            result.append(f"[UsageTrim: {len(pending)} health/ready probe lines collapsed]")
        else:
            result.extend(pending)
        pending = []
        result.append(line)
    if len(pending) >= _KUBECTL_MIN_PROBE_FOLD:
        result.append(f"[UsageTrim: {len(pending)} health/ready probe lines collapsed]")
    else:
        result.extend(pending)

    ret = "\n".join(result)
    if raw_output.endswith("\n") and not ret.endswith("\n"):
        ret += "\n"
    if len(ret) >= len(raw_output):
        return raw_output
    return ret


def _filter_kubectl_describe(lines: list[str]) -> str:
    result: list[str] = []
    index = 0
    collapsed = False
    annotation_count = 0
    env_count = 0
    normal_events = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        # Annotations: ... (inline or multi-line indented block)
        if stripped.startswith("Annotations:"):
            rest = stripped[len("Annotations:") :].strip()
            index += 1
            annotation_count = 1 if rest and rest != "<none>" else 0
            # Count continuing indented annotation keys / JSON continuation.
            while index < len(lines):
                nxt = lines[index]
                if not nxt.strip():
                    break
                # Next top-level section starts at column 0 with Key:
                if nxt[:1] not in " \t" and ":" in nxt:
                    break
                # Indented content belongs to annotations.
                if nxt.lstrip().startswith("kubectl.kubernetes.io/last-applied"):
                    annotation_count += 1
                elif re.match(r"^\s+\S", nxt) and ":" in nxt and not nxt.strip().startswith("{"):
                    annotation_count += 1
                elif re.match(r"^\s+", nxt):
                    # Continuation of previous annotation value (JSON blob etc.)
                    pass
                else:
                    break
                index += 1
            if annotation_count:
                result.append(
                    f"Annotations:      [UsageTrim: {annotation_count} annotations collapsed]"
                )
            else:
                result.append("Annotations:      <none>")
            collapsed = True
            continue

        # Routine Tolerations: (default NotReady/Unreachable etc.)
        if stripped.startswith("Tolerations:"):
            rest = stripped[len("Tolerations:") :].strip()
            index += 1
            tol_count = 1 if rest and rest != "<none>" else 0
            while index < len(lines):
                nxt = lines[index]
                if not nxt.strip():
                    break
                if nxt[:1] not in " \t" and ":" in nxt:
                    break
                if re.match(r"^\s+\S", nxt):
                    tol_count += 1
                    index += 1
                    continue
                break
            if tol_count:
                result.append(
                    f"Tolerations:      [UsageTrim: {tol_count} routine tolerations collapsed]"
                )
                collapsed = True
            else:
                result.append("Tolerations:      <none>")
            continue

        # Environment: under a container — collapse keys, keep secret refs briefly.
        if stripped == "Environment:" or stripped.startswith("Environment:"):
            # Preserve indent of the Environment header's parent context via spaces.
            indent = line[: len(line) - len(line.lstrip())]
            index += 1
            env_count = 0
            secret_refs: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                nxt_stripped = nxt.strip()
                if not nxt_stripped:
                    break
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if (
                    nxt_indent <= len(indent)
                    and ":" in nxt_stripped
                    and not nxt_stripped.startswith(("<", "-"))
                ):
                    # Next sibling field at same/less indent (Mounts, etc.)
                    break
                if (
                    nxt_indent <= len(indent)
                    and nxt_stripped.endswith(":")
                    and " " not in nxt_stripped
                ):
                    break
                # Env entry lines are more indented than "Environment:"
                if re.match(r"^\s+\S+:\s+", nxt):
                    env_count += 1
                    if "<set to the key" in nxt_stripped or "Optional:" in nxt_stripped:
                        secret_refs.append(nxt_stripped)
                    index += 1
                    continue
                if nxt_indent > len(indent):
                    index += 1
                    continue
                break
            result.append(f"{indent}Environment:      [UsageTrim: {env_count} env vars collapsed]")
            for ref in secret_refs[:5]:
                result.append(f"{indent}  {ref}")
            collapsed = True
            continue

        # Events section
        if stripped.startswith("Events:"):
            result.append(
                line if stripped == "Events:" or stripped.startswith("Events:") else "Events:"
            )
            index += 1
            # Skip header separator rows
            while index < len(lines) and (
                lines[index].strip().startswith("Type")
                or lines[index].strip().startswith("----")
                or not lines[index].strip()
            ):
                if lines[index].strip().startswith("Type"):
                    result.append(lines[index])
                index += 1
            warnings: list[str] = []
            while index < len(lines):
                ev = lines[index]
                ev_stripped = ev.strip()
                if not ev_stripped:
                    index += 1
                    continue
                # New top-level section would be rare after Events; stop on non-event.
                m = _KUBECTL_EVENT_ROW_RE.match(ev_stripped)
                if not m:
                    # Sometimes events are "  <none>"
                    if ev_stripped == "<none>":
                        result.append(ev)
                        index += 1
                        break
                    break
                kind = m.group(1)
                if kind == "Normal":
                    normal_events += 1
                else:
                    warnings.append(ev)
                index += 1
            if normal_events:
                result.append(f"  [UsageTrim: {normal_events} Normal events collapsed]")
                collapsed = True
            result.extend(warnings)
            continue

        result.append(line)
        index += 1

    if not collapsed:
        return "\n".join(lines)
    return "\n".join(result)


def filter_helm(raw_output: str, command: str = "") -> str:
    """Compact helm install/upgrade/dependency/package chatter; keep release signal.

    Preserves NOTES, Error/Warning lines, and release status fields (NAME, STATUS,
    REVISION, NAMESPACE, LAST DEPLOYED). Folds repetitive chart repository /
    packaging noise (Hang tight…, Saving N charts, Digests, Happy Helming).
    Pure and idempotent on short / already-compacted input.
    """
    del command
    if not raw_output.strip():
        return raw_output
    if _HELM_FOLD_NOTICE_RE.search(raw_output):
        return raw_output

    lines = raw_output.splitlines()
    noise_count = sum(1 for line in lines if _HELM_NOISE_RE.search(line.strip()))
    if noise_count < _HELM_MIN_NOISE_FOLD:
        return raw_output

    result: list[str] = []
    pending: list[str] = []
    for line in lines:
        if _HELM_NOISE_RE.search(line.strip()):
            pending.append(line)
            continue
        if len(pending) >= _HELM_MIN_NOISE_FOLD:
            result.append(f"[UsageTrim: {len(pending)} chart/repo lines collapsed]")
        else:
            result.extend(pending)
        pending = []
        result.append(line)
    if len(pending) >= _HELM_MIN_NOISE_FOLD:
        result.append(f"[UsageTrim: {len(pending)} chart/repo lines collapsed]")
    else:
        result.extend(pending)

    ret = "\n".join(result)
    if raw_output.endswith("\n") and not ret.endswith("\n"):
        ret += "\n"
    if len(ret) >= len(raw_output):
        return raw_output
    return ret


def _tf_is_simple_unchanged_attr(line: str) -> bool:
    """True for scalar unchanged attribute lines inside a plan resource block.

    Actionable ``+`` / ``~`` / ``-`` lines, block openers ending in ``{`` / ``[``,
    and already-emitted fold notices are never treated as foldable attributes.
    """
    if _TF_UNCHANGED_FOLD_RE.search(line) or _TF_REFRESH_NOTICE_RE.search(line):
        return False
    if _TF_CHANGED_LINE_RE.match(line):
        return False
    if _TF_RESOURCE_HEADER_RE.match(line):
        return False
    if _TF_PLAN_SUMMARY_RE.match(line.strip()):
        return False
    if _TF_ERROR_WARNING_RE.search(line):
        return False
    match = _TF_SIMPLE_ATTR_RE.match(line)
    if not match:
        return False
    value = match.group(3).rstrip()
    # Keep structural openers so nested blocks stay balanced.
    if value.endswith("{") or value.endswith("["):
        return False
    return True


def _tf_fold_unchanged_attributes(lines: list[str]) -> list[str]:
    """Fold runs of ≥3 simple unchanged attributes into one notice line."""
    result: list[str] = []
    run: list[str] = []

    def flush_run() -> None:
        nonlocal run
        if not run:
            return
        if len(run) >= _TF_MIN_UNCHANGED_FOLD:
            indent = re.match(r"^(\s*)", run[0]).group(1) if run[0] else "  "
            result.append(f"{indent}[... {len(run)} unchanged attributes folded ...]")
        else:
            result.extend(run)
        run = []

    for line in lines:
        if _tf_is_simple_unchanged_attr(line):
            run.append(line)
            continue
        flush_run()
        result.append(line)
    flush_run()
    return result


def _tf_collapse_refresh_lines(lines: list[str]) -> list[str]:
    """Collapse consecutive refresh/read chatter into a short count summary."""
    result: list[str] = []
    refresh_count = 0

    def flush_refresh() -> None:
        nonlocal refresh_count
        if refresh_count:
            result.append(f"[UsageTrim: {refresh_count} refresh/read lines collapsed]")
            refresh_count = 0

    for line in lines:
        if _TF_REFRESH_RE.match(line.strip()):
            refresh_count += 1
            continue
        flush_refresh()
        result.append(line)
    flush_refresh()
    return result


def filter_terraform_plan(raw_output: str) -> str:
    """Compact terraform/tofu plan|apply|destroy output for agent context.

    Collapses repeated ``Refreshing state...`` / ``Reading...`` / ``Refreshing...``
    chatter into a short count summary, folds long runs of unchanged scalar
    attributes inside resource diffs, and preserves Plan summaries, resource
    change headers (``# ... will be created/updated/destroyed``), actionable
    ``+`` / ``~`` / ``-`` lines, and error/warning blocks. Works for both
    Terraform and OpenTofu wording. Pure and order-preserving for kept lines;
    idempotent on already-short or already-compacted input.
    """
    if not raw_output.strip():
        return raw_output

    lines = raw_output.splitlines()
    has_refresh = any(_TF_REFRESH_RE.match(line.strip()) for line in lines)
    foldable_attr_count = sum(1 for line in lines if _tf_is_simple_unchanged_attr(line))
    has_foldable_attrs = foldable_attr_count >= _TF_MIN_UNCHANGED_FOLD

    # Already short / no foldable terraform noise → leave alone (idempotent).
    if not has_refresh and not has_foldable_attrs:
        return raw_output

    compacted = lines
    if has_refresh:
        compacted = _tf_collapse_refresh_lines(compacted)
    if sum(1 for line in compacted if _tf_is_simple_unchanged_attr(line)) >= _TF_MIN_UNCHANGED_FOLD:
        compacted = _tf_fold_unchanged_attributes(compacted)

    ret = "\n".join(compacted)
    if raw_output.endswith("\n") and not ret.endswith("\n"):
        ret += "\n"
    # Never expand the prompt.
    if len(ret) >= len(raw_output):
        return raw_output
    return ret


def filter_terraform(raw_output: str) -> str:
    """Alias for :func:`filter_terraform_plan` (kept for older call sites)."""
    return filter_terraform_plan(raw_output)


def _is_kubectl_command(command: str) -> bool:
    """Recognize kubectl/oc argv (get, describe, logs, and other cluster ops)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    return PurePath(words[0]).name.lower() in {
        "kubectl",
        "kubectl.exe",
        "oc",
        "oc.exe",
    }


def _is_helm_command(command: str) -> bool:
    """Recognize helm argv (install, upgrade, dependency, package, status, …)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    return PurePath(words[0]).name.lower() in {"helm", "helm.exe"}


def _is_terraform_command(command: str) -> bool:
    """Recognize terraform/tofu plan|apply|destroy (refresh noise + plan body)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    binary = PurePath(words[0]).name.lower()
    if binary not in {"terraform", "terraform.exe", "tofu", "tofu.exe"}:
        return False
    return any(word in {"plan", "apply", "destroy"} for word in words[1:])


def author_kubectl_describe_fixture() -> str:
    """Authored kubectl describe pod: annotations, env, Normal events dominate tokens."""
    last_applied = json.dumps(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "api",
                "namespace": "production",
                "labels": {f"label-{i}": f"value-{i}" for i in range(20)},
                "annotations": {f"anno-{i}": f"cfg-{i}-{'x' * 40}" for i in range(30)},
            },
            "spec": {
                "replicas": 3,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "api",
                                "image": "registry.example.com/api:1.4.2",
                                "env": [
                                    {"name": f"CFG_{i}", "value": f"setting-{i}-{'y' * 24}"}
                                    for i in range(40)
                                ],
                            }
                        ]
                    }
                },
            },
        }
    )
    annotation_lines = [
        "Annotations:      deployment.kubernetes.io/revision: 42",
        "                  kubectl.kubernetes.io/default-container: api",
        f"                  kubectl.kubernetes.io/last-applied-configuration:\n"
        f"                    {last_applied}",
    ]
    for i in range(25):
        annotation_lines.append(f'                  prometheus.io/scrape-{i}: "true"')
        annotation_lines.append(f"                  checksum/config-{i}: {'a' * 64}")

    env_lines = ["    Environment:"]
    for i in range(50):
        env_lines.append(f"      CFG_VAR_{i}:  value-for-config-{i}-{'z' * 20}")
    env_lines.append("      DATABASE_URL:  <set to the key 'url' of secret 'db-credentials'>")

    normal_events = []
    for i in range(40):
        normal_events.append(
            f"  Normal   Scheduled  {i}m    default-scheduler  "
            f"Successfully assigned production/api-7d8f9c-{i:04d} to node-{i % 8}"
        )
        normal_events.append(
            f"  Normal   Pulled     {i}m    kubelet            "
            f'Container image "registry.example.com/api:1.4.2" already present on machine'
        )
        normal_events.append(
            f"  Normal   Created    {i}m    kubelet            Created container api"
        )
        normal_events.append(
            f"  Normal   Started    {i}m    kubelet            Started container api"
        )

    return "\n".join(
        [
            "Name:             api-7d8f9c-xk2m9",
            "Namespace:        production",
            "Priority:         0",
            "Service Account:  api",
            "Node:             ip-10-0-1-5/10.0.1.5",
            "Start Time:       Mon, 21 Sep 2026 10:00:00 +0000",
            "Labels:           app=api",
            "                  pod-template-hash=7d8f9c",
            "                  version=1.4.2",
            *annotation_lines,
            "Status:           Running",
            "IP:               10.0.2.15",
            "IPs:",
            "  IP:  10.0.2.15",
            "Controlled By:  ReplicaSet/api-7d8f9c",
            "Containers:",
            "  api:",
            "    Container ID:  containerd://abcdef0123456789",
            "    Image:         registry.example.com/api:1.4.2",
            "    Image ID:      registry.example.com/api@sha256:" + ("ab" * 32),
            "    Port:          8080/TCP",
            "    Host Port:     0/TCP",
            "    State:          Waiting",
            "      Reason:       CrashLoopBackOff",
            "    Last State:     Terminated",
            "      Reason:       Error",
            "      Exit Code:    1",
            "      Started:      Mon, 21 Sep 2026 10:40:00 +0000",
            "      Finished:     Mon, 21 Sep 2026 10:40:02 +0000",
            "    Ready:          False",
            "    Restart Count:  12",
            *env_lines,
            "    Mounts:",
            "      /var/run/secrets/kubernetes.io/serviceaccount from kube-api-access (ro)",
            "Conditions:",
            "  Type              Status",
            "  Initialized       True",
            "  Ready             False",
            "  ContainersReady   False",
            "  PodScheduled      True",
            "Volumes:",
            "  kube-api-access:",
            "    Type:                    Projected (a volume that contains injected data)",
            "QoS Class:                   Burstable",
            "Node-Selectors:              <none>",
            "Tolerations:                 node.kubernetes.io/not-ready:NoExecute op=Exists for 300s",
            "                             node.kubernetes.io/unreachable:NoExecute op=Exists for 300s",
            "                             node.kubernetes.io/disk-pressure:NoSchedule op=Exists",
            "                             node.kubernetes.io/memory-pressure:NoSchedule op=Exists",
            "                             node.kubernetes.io/pid-pressure:NoSchedule op=Exists",
            "                             node.kubernetes.io/network-unavailable:NoSchedule op=Exists",
            "Events:",
            "  Type     Reason     Age    From               Message",
            "  ----     ------     ----   ----               -------",
            *normal_events,
            "  Warning  BackOff    2m     kubelet            "
            "Back-off restarting failed container api in pod api-7d8f9c-xk2m9",
            "  Warning  Unhealthy  90s    kubelet            "
            'Liveness probe failed: Get "http://10.0.2.15:8080/healthz": dial tcp '
            "10.0.2.15:8080: connect: connection refused",
        ]
    )


def author_kubectl_get_fixture() -> str:
    """Authored kubectl get pods -o wide with many healthy rows + two failures."""
    header = (
        "NAME                         READY   STATUS             RESTARTS   AGE   "
        "IP            NODE"
    )
    rows = [header]
    for i in range(60):
        rows.append(
            f"api-7d8f9c-{i:04d}             1/1     Running            0          "
            f"10d   10.0.2.{i}    node-{i % 8}"
        )
    rows.append(
        "worker-crash-aaaa             0/1     CrashLoopBackOff   14         "
        "5m    10.0.3.9      node-1"
    )
    rows.append(
        "worker-pending-bbbb           0/1     Pending            0          "
        "2m    <none>        <none>"
    )
    return "\n".join(rows)


def author_kubectl_logs_fixture() -> str:
    """Authored kubectl logs: repetitive health probes dwarf a real error."""
    probes: list[str] = []
    for i in range(40):
        probes.append(f'2026-09-21T10:00:{i:02d}Z 10.0.0.1 - - "GET /healthz HTTP/1.1" 200 2')
        probes.append(f'2026-09-21T10:00:{i:02d}Z 10.0.0.1 - - "GET /readyz HTTP/1.1" 200 1')
        probes.append(f"2026-09-21T10:00:{i:02d}Z kube-probe: readiness check ok for container api")
    return "\n".join(
        [
            *probes[:60],
            "2026-09-21T10:01:00Z ERROR auth: jwt verification failed: token expired",
            "Traceback (most recent call last):",
            '  File "/app/main.py", line 42, in handle',
            "    raise AuthError(token)",
            "AuthError: token expired",
            *probes[60:],
            "2026-09-21T10:02:00Z Liveness probe failed: HTTP probe failed with statuscode: 500",
            "2026-09-21T10:02:01Z GET /healthz 500 12ms",
        ]
    )


def author_helm_install_fixture() -> str:
    """Authored helm upgrade: repo/packaging noise around release status + NOTES."""
    noise = [
        "Hang tight while we grab the latest from your chart repositories...",
        '...Successfully got an update from the "bitnami" chart repository',
        '...Successfully got an update from the "stable" chart repository',
        '...Successfully got an update from the "ingress-nginx" chart repository',
        "Update Complete. ⎈Happy Helming!⎈",
        "Saving 5 charts",
        "Downloading nginx from repo https://charts.bitnami.com/bitnami",
        "Downloading common from repo https://charts.bitnami.com/bitnami",
        "Downloading postgresql from repo https://charts.bitnami.com/bitnami",
        "Pulled: registry-1.docker.io/bitnamicharts/nginx:15.0.2",
        "Digest: sha256:" + ("ab" * 32),
        "Deleting outdated charts",
        "Successfully packaged chart and saved it to: /tmp/api-1.4.2.tgz",
        "Skipping chart postgresql: already exists and is not different",
    ]
    return "\n".join(
        [
            *noise,
            'Release "api" has been upgraded. Happy Helming!',
            "NAME: api",
            "LAST DEPLOYED: Mon Sep 21 10:00:00 2026",
            "NAMESPACE: production",
            "STATUS: deployed",
            "REVISION: 4",
            "TEST SUITE: None",
            "NOTES:",
            "1. Get the application URL by running these commands:",
            "  export POD_NAME=$(kubectl get pods -l app=api -o jsonpath='{.items[0].metadata.name}')",
            "  echo http://127.0.0.1:8080/",
            "  kubectl port-forward $POD_NAME 8080:8080",
            "Warning: chart appVersion differs from image tag",
        ]
    )


def author_terraform_plan_fixture() -> str:
    """Authored terraform/tofu plan: refresh/read chatter dwarfs the actionable plan."""
    lines: list[str] = []
    for i in range(80):
        lines.append(f"data.aws_iam_policy_document.policy_{i}: Reading...")
        lines.append(
            f"data.aws_iam_policy_document.policy_{i}: Read complete after 0s "
            f"[id={i:04d}-{'a' * 40}]"
        )
    for i in range(60):
        lines.append(f"aws_security_group.svc_{i}: Refreshing state... [id=sg-{'b' * 8}{i:04d}]")
        lines.append(f"aws_instance.worker_{i}: Refreshing state... [id=i-{'c' * 8}{i:04d}]")
    for i in range(20):
        lines.append(f"module.vpc.aws_subnet.private[{i}]: Refreshing...")
    unchanged_attrs = [
        '        ami                         = "ami-0abcdef1234567890"',
        '        arn                         = "arn:aws:ec2:us-east-1:123456789012:instance/i-0abc"',
        "        associate_public_ip_address = false",
        '        availability_zone           = "us-east-1a"',
        "        cpu_core_count              = 1",
        "        cpu_threads_per_core        = 2",
        "        disable_api_termination     = false",
        "        ebs_optimized               = true",
        '        id                          = "i-0abc123def456"',
        '        instance_state              = "running"',
        '        key_name                    = "deploy"',
        "        monitoring                  = false",
        '        private_dns                 = "ip-10-0-1-10.ec2.internal"',
        '        private_ip                  = "10.0.1.10"',
        '        public_dns                  = ""',
        '        public_ip                   = ""',
        "        secondary_private_ips       = []",
        "        security_groups             = []",
        '        subnet_id                   = "subnet-0aabbccddeeff0011"',
        '        tenancy                     = "default"',
        "        user_data                   = null",
        '        vpc_security_group_ids      = ["sg-0123456789abcdef0"]',
    ]
    lines.extend(
        [
            "",
            "Terraform used the selected providers to generate the following execution plan.",
            "Resource actions are indicated with the following symbols:",
            "  + create",
            "  ~ update in-place",
            "",
            "Terraform will perform the following actions:",
            "",
            "  # aws_instance.api will be updated in-place",
            '  ~ resource "aws_instance" "api" {',
            *unchanged_attrs[:11],
            '      ~ instance_type               = "t3.small" -> "t3.medium"',
            *unchanged_attrs[11:],
            "    }",
            "",
            "  # aws_lb_listener_rule.canary will be created",
            '  + resource "aws_lb_listener_rule" "canary" {',
            "      + arn        = (known after apply)",
            "      + priority   = 100",
            '      + listener_arn = "arn:aws:elasticloadbalancing:us-east-1:123:listener/app/1"',
            "    }",
            "",
            "Plan: 1 to add, 1 to change, 0 to destroy.",
            "",
            "Warning: Deprecated attribute",
            "",
            '  on main.tf line 42, in resource "aws_instance" "api":',
            "  42:   ebs_optimized = true",
            "",
            "─────────────────────────────────────────────────────────────────────────────",
            "",
            "Note: You didn't use the -out option to save this plan, so Terraform can't",
            'guarantee to take exactly these actions if you run "terraform apply" now.',
        ]
    )
    return "\n".join(lines)
