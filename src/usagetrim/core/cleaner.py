from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from usagetrim.core.cache import ContextCache
from usagetrim.core.redactor import redact_secrets

# Regular expressions for ANSI & terminal control sequences
ANSI_CSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
ANSI_OSC_PATTERN = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ANSI_GENERIC_PATTERN = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Progress indicator patterns (spinners, dots, hashes)
SPINNER_CHARS = {"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏", "◐", "◓", "◑", "◒", "-\\|/"}
PROGRESS_BAR_PATTERN = re.compile(r"(?:\[[=#\-\s]{5,}\]|\b\d{1,3}%\b|\b\d+/\d+\b)")

# Error & traceback signatures
ERROR_SIGNATURES = [
    re.compile(r"Traceback \(most recent call last\):", re.IGNORECASE),
    re.compile(r"={3,}\s*(?:FAILURES|ERRORS)\s*={3,}", re.IGNORECASE),
    re.compile(r"\bFAILED\s*(?:\(|\[)", re.IGNORECASE),
    re.compile(
        r"\b(?:AssertionError|ValueError|TypeError|KeyError|AttributeError|RuntimeError|Exception):"
    ),
    re.compile(r"\b(?:panic:|fatal error:|NullPointerException|Segmentation fault)", re.IGNORECASE),
    re.compile(r"\b(?:npm ERR!|yarn error|error\[E\d+\]:|TS\d+:)", re.IGNORECASE),
    re.compile(r"^(?:Error|FATAL|CRITICAL):", re.IGNORECASE | re.MULTILINE),
]


@dataclass
class CleanerOptions:
    max_lines: int = 80
    head_lines: int = 20
    tail_lines: int = 40
    strip_ansi: bool = True
    dedup_lines: bool = True
    preserve_errors: bool = True
    normalize_whitespace: bool = True
    redact_keys: bool = True
    enable_cache: bool = True


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences, OSC strings, and control characters."""
    if not text:
        return ""
    text = ANSI_OSC_PATTERN.sub("", text)
    text = ANSI_CSI_PATTERN.sub("", text)
    text = ANSI_GENERIC_PATTERN.sub("", text)
    return text


def resolve_carriage_returns(text: str) -> str:
    """Resolve carriage returns (\\r) by keeping the final overwrite of each line."""
    if "\r" not in text:
        return text

    resolved_lines: list[str] = []
    text = text.replace("\r\n", "\n")

    for raw_line in text.split("\n"):
        if "\r" in raw_line:
            parts = raw_line.split("\r")
            non_empty = [p for p in parts if p.strip()]
            resolved_lines.append(non_empty[-1] if non_empty else "")
        else:
            resolved_lines.append(raw_line)

    return "\n".join(resolved_lines)


def deduplicate_repetitive_lines(lines: list[str], max_consecutive: int = 2) -> list[str]:
    """Collapse consecutive duplicate or highly repetitive lines."""
    if not lines:
        return []

    result: list[str] = []
    prev_line = None
    repeat_count = 0

    def flush_repeat():
        nonlocal repeat_count, prev_line
        if repeat_count > max_consecutive:
            omitted = repeat_count - max_consecutive
            result.append(f"  [... {omitted} identical lines omitted by usagetrim ...]")
        repeat_count = 0

    for line in lines:
        stripped = line.strip()
        if stripped and stripped == prev_line:
            repeat_count += 1
            if repeat_count <= max_consecutive:
                result.append(line)
        else:
            flush_repeat()
            prev_line = stripped
            repeat_count = 1
            result.append(line)

    flush_repeat()
    return result


def find_first_error_index(lines: list[str]) -> int | None:
    """Find line index where error/traceback starts."""
    for idx, line in enumerate(lines):
        for sig in ERROR_SIGNATURES:
            if sig.search(line):
                return idx
    return None


def compact_terminal_output(raw_text: str, options: CleanerOptions | None = None) -> str:
    """High-efficiency terminal log compactor.

    - Strips ANSI colors and terminal garbage
    - Scrubs API keys & sensitive secrets
    - Resolves \\r progress bar overwrites
    - Deduplicates repeated lines
    - If errors/tracebacks exist: preserves full error & stacktrace, truncating routine logs
    - If no errors: preserves head and tail of output
    - Stores full original in local cache for 100% reversible retrieval via ref ID
    """
    if not raw_text:
        return ""

    opts = options or CleanerOptions()
    text = raw_text

    if opts.redact_keys:
        text = redact_secrets(text)

    if opts.strip_ansi:
        text = strip_ansi(text)

    text = resolve_carriage_returns(text)
    lines = text.splitlines()

    if opts.dedup_lines:
        lines = deduplicate_repetitive_lines(lines)

    total_lines = len(lines)
    if total_lines <= opts.max_lines:
        cleaned = "\n".join(lines)
        if opts.normalize_whitespace:
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    # Store in local cache to ensure 100% reversibility
    ref_tag = ""
    if opts.enable_cache:
        try:
            cache = ContextCache()
            ref_id = cache.store(raw_text, source="terminal_output")
            ref_tag = f" [ref: {ref_id}]"
        except Exception:
            pass

    # Truncation logic
    head_count = opts.head_lines
    tail_count = opts.tail_lines

    if opts.preserve_errors:
        err_idx = find_first_error_index(lines)
        if err_idx is not None:
            if err_idx < head_count:
                kept_head = lines[:head_count]
                kept_tail = (
                    lines[-tail_count:]
                    if total_lines > head_count + tail_count
                    else lines[head_count:]
                )
                omitted = max(0, total_lines - len(kept_head) - len(kept_tail))
                if omitted > 0:
                    summary_line = (
                        f"\n[... {omitted} lines of logs omitted by usagetrim{ref_tag} ...]\n"
                    )
                    res = kept_head + [summary_line] + kept_tail
                else:
                    res = kept_head + kept_tail
            else:
                kept_head = lines[:head_count]
                error_lines = lines[err_idx:]
                if len(error_lines) > tail_count * 2:
                    error_lines = (
                        lines[err_idx : err_idx + 20]
                        + [
                            f"\n[... {len(lines) - err_idx - 50} lines inside error omitted{ref_tag} ...]\n"
                        ]
                        + lines[-30:]
                    )

                omitted = max(0, err_idx - head_count)
                summary_line = (
                    f"\n[... {omitted} lines of routine output omitted by usagetrim{ref_tag} ...]\n"
                )
                res = kept_head + [summary_line] + error_lines

            cleaned = "\n".join(res)
            if opts.normalize_whitespace:
                cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            return cleaned.strip()

    # No error found
    kept_head = lines[:head_count]
    kept_tail = lines[-tail_count:]
    omitted = total_lines - head_count - tail_count
    summary_line = f"\n[... {omitted} lines omitted by usagetrim{ref_tag} ...]\n"
    res = kept_head + [summary_line] + kept_tail

    cleaned = "\n".join(res)
    if opts.normalize_whitespace:
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Semantic source-code cleaner (Biome/Ruff-style trivia folding)
# ---------------------------------------------------------------------------

_HASH_COMMENT_SUFFIXES = {".py", ".sh", ".bash", ".yaml", ".yml", ".toml", ".rb", ".r", ".pl"}
_SLASH_COMMENT_SUFFIXES = {
    ".ts",
    ".js",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".kt",
    ".swift",
}

_LICENSE_KEYWORDS = re.compile(
    r"(?i)\b(?:"
    r"spdx-license-identifier|copyright(?:\s+\(c\))?|all\s+rights\s+reserved|"
    r"licensed\s+under|apache\s+license|mit\s+license|bsd\s+license|"
    r"gnu\s+(?:general\s+)?public\s+license|mozilla\s+public\s+license|"
    r"licen[cs]e(?:d)?\s+to|as\s+is(?:\s+and\s+without)?\s+warranty|"
    r"without\s+warrant(?:y|ies)|permission\s+is\s+hereby\s+granted"
    r")\b"
)
_SPDX_ID = re.compile(r"(?i)SPDX-License-Identifier:\s*([A-Za-z0-9.+_-]+)")
_LICENSE_NAME = re.compile(
    r"(?i)\b((?:Apache(?:-\s*2\.0)?|MIT|BSD-\d-Clause|GPL-\d\.0(?:-only|-or-later)?|"
    r"LGPL-\d\.0|MPL-2\.0|ISC|Unlicense|CC0-1\.0))\b"
)

_CODEY_COMMENT = re.compile(
    r"(?:"
    r"\b(?:def|class|function|return|import|from|const|let|var|if|elif|else|for|while|"
    r"switch|case|try|catch|except|async|await|public|private|protected|static|void|"
    r"int|string|bool|true|false|null|None|self|this)\b|"
    r"[=;{}()\[\]]|->"
    r")"
)

_FOLD_NOTICE = re.compile(
    r"^\s*(?:#|//)\s*\[(?:License:|\.\.\..*(?:folded|omitted)\s+by\s+usagetrim)",
    re.IGNORECASE,
)

_SHEBANG = re.compile(r"^#!")
_HASHBANG_CODING = re.compile(r"^#.*coding[:=]\s*([-\w.]+)")


@dataclass
class CodeCleanerOptions:
    """Options for semantic source folding (licenses, blanks, dead comments)."""

    fold_license: bool = True
    fold_commented_code: bool = True
    collapse_blank_lines: bool = True
    fold_banner_docstrings: bool = True
    strip_remaining_comments: bool = False
    min_license_lines: int = 3
    min_commented_code_lines: int = 6
    max_blank_run: int = 1


def _comment_style(suffix: str) -> str:
    lowered = suffix.lower()
    if lowered in _SLASH_COMMENT_SUFFIXES:
        return "slash"
    if lowered in _HASH_COMMENT_SUFFIXES:
        return "hash"
    return "hash"


def _line_comment_prefix(style: str) -> str:
    return "//" if style == "slash" else "#"


def _is_full_line_comment(line: str, style: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if style == "slash":
        return stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*")
    return stripped.startswith("#")


def _comment_body(line: str, style: str) -> str:
    stripped = line.strip()
    if style == "slash":
        if stripped.startswith("//"):
            return stripped[2:].strip()
        if stripped.startswith("/*"):
            body = stripped[2:]
            if body.endswith("*/"):
                body = body[:-2]
            return body.strip()
        if stripped.startswith("*"):
            return stripped.lstrip("*").strip()
    if stripped.startswith("#"):
        return stripped[1:].strip()
    return stripped


def _looks_like_license_text(text: str) -> bool:
    return bool(_LICENSE_KEYWORDS.search(text))


def _detect_license_label(block_text: str) -> str:
    spdx = _SPDX_ID.search(block_text)
    if spdx:
        return spdx.group(1)
    named = _LICENSE_NAME.search(block_text)
    if named:
        return named.group(1).replace(" ", "")
    if re.search(r"(?i)\bapache\b", block_text):
        return "Apache-2.0"
    if re.search(r"(?i)\bmit\b", block_text):
        return "MIT"
    return "proprietary"


def _leading_trivia_end(lines: list[str]) -> int:
    """Index after shebang / encoding lines where a license banner may start."""
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].strip()
        if not stripped:
            idx += 1
            continue
        if _SHEBANG.match(stripped) or _HASHBANG_CODING.match(stripped):
            idx += 1
            continue
        break
    return idx


def fold_license_header(content: str, suffix: str = ".py", min_lines: int = 3) -> str:
    """Fold a leading SPDX/copyright banner into a one-line notice."""
    if not content:
        return content

    style = _comment_style(suffix)
    lines = content.splitlines()
    start = _leading_trivia_end(lines)
    if start >= len(lines):
        return content

    # Collect contiguous leading comment block (allow blank lines inside banner).
    block_indices: list[int] = []
    i = start
    saw_comment = False
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            if saw_comment and block_indices:
                # peek ahead: blank inside banner vs end of banner
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and _is_full_line_comment(lines[j], style):
                    block_indices.append(i)
                    i += 1
                    continue
            break
        if _is_full_line_comment(lines[i], style):
            block_indices.append(i)
            saw_comment = True
            i += 1
            continue
        # Opening block comment for C-style files handled via full-line /* already.
        break

    comment_lines = [lines[k] for k in block_indices if lines[k].strip()]
    if len(comment_lines) < min_lines:
        return content

    block_text = "\n".join(
        _comment_body(lines[k], style) for k in block_indices if lines[k].strip()
    )
    if not _looks_like_license_text(block_text):
        return content

    label = _detect_license_label(block_text)
    prefix = _line_comment_prefix(style)
    notice = f"{prefix} [License: {label} ({len(comment_lines)} lines folded)]"

    first = block_indices[0]
    last = block_indices[-1]
    new_lines = lines[:first] + [notice] + lines[last + 1 :]
    # Drop a single blank line immediately after the folded banner if present.
    if first + 1 < len(new_lines) and not new_lines[first + 1].strip():
        # Keep one blank for readability between notice and code? Prefer one blank max.
        pass
    ending = "\n" if content.endswith("\n") else ""
    return "\n".join(new_lines) + ending


def _is_fold_notice(line: str) -> bool:
    return bool(_FOLD_NOTICE.match(line))


def fold_commented_out_code(
    content: str,
    suffix: str = ".py",
    min_lines: int = 6,
) -> str:
    """Fold runs of consecutive commented-out code lines into a one-line notice."""
    if not content:
        return content

    style = _comment_style(suffix)
    prefix = _line_comment_prefix(style)
    lines = content.splitlines()
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if _is_fold_notice(line) or not _is_full_line_comment(line, style):
            out.append(line)
            i += 1
            continue

        body = _comment_body(line, style)
        if not _CODEY_COMMENT.search(body):
            out.append(line)
            i += 1
            continue

        run_start = i
        while i < len(lines):
            cand = lines[i]
            if _is_fold_notice(cand) or not _is_full_line_comment(cand, style):
                break
            cand_body = _comment_body(cand, style)
            if not cand_body:
                # blank comment line inside a dead block — allow
                i += 1
                continue
            if not _CODEY_COMMENT.search(cand_body) and not _looks_like_license_text(cand_body):
                # Non-code prose comment ends the dead-code run.
                break
            i += 1

        run_len = i - run_start
        if run_len >= min_lines:
            out.append(
                f"{prefix} [... {run_len} lines of commented-out code folded by usagetrim ...]"
            )
        else:
            out.extend(lines[run_start:i])

    ending = "\n" if content.endswith("\n") else ""
    return "\n".join(out) + ending


def collapse_blank_lines(content: str, max_run: int = 1) -> str:
    """Collapse oversized runs of blank lines without touching non-blank content."""
    if not content or max_run < 0:
        return content
    lines = content.splitlines()
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run <= max_run:
                out.append("")
            continue
        blank_run = 0
        out.append(line)
    ending = "\n" if content.endswith("\n") else ""
    return "\n".join(out) + ending


def _is_banner_docstring(text: str) -> bool:
    """True only for obvious license/copyright banners — never real descriptions."""
    body = text.strip()
    if not body:
        return False
    if not _looks_like_license_text(body):
        return False
    # Real module docs often mention license briefly; require banner-dominated text.
    words = re.findall(r"[A-Za-z]+", body)
    if len(words) > 120:
        return False
    license_hits = len(_LICENSE_KEYWORDS.findall(body))
    # Banner if license keywords dominate or the docstring is short boilerplate.
    if license_hits >= 2:
        return True
    if license_hits >= 1 and len(words) <= 40:
        return True
    return False


def fold_banner_docstrings(content: str, suffix: str = ".py") -> str:
    """Fold only obvious repeated license/copyright docstring banners.

    Descriptive module/class/method docstrings are left untouched.
    """
    if suffix.lower() != ".py" or not content:
        return content

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return content

    lines = content.splitlines()
    replacements: list[tuple[int, int, str]] = []

    def consider(node: ast.AST) -> None:
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        if not node.body:
            return
        first = node.body[0]
        if not isinstance(first, ast.Expr):
            return
        val = first.value
        if not isinstance(val, ast.Constant) or not isinstance(val.value, str):
            return
        doc = val.value
        if not _is_banner_docstring(doc):
            return
        # lineno/end_lineno are 1-based inclusive.
        start = getattr(first, "lineno", None)
        end = getattr(first, "end_lineno", None)
        if start is None or end is None:
            return
        n_lines = end - start + 1
        if n_lines < 2 and len(doc.splitlines()) < 2:
            # Tiny one-liners that are banners still fold when license-dominated.
            if len(doc.strip()) < 20:
                return
        label = _detect_license_label(doc)
        replacements.append(
            (start - 1, end - 1, f'"""[License: {label} ({n_lines} docstring lines folded)]"""')
        )

    consider(tree)
    for node in ast.walk(tree):
        if node is tree:
            continue
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            consider(node)

    if not replacements:
        return content

    # Apply from bottom to top so indices stay valid.
    replacements.sort(key=lambda t: t[0], reverse=True)
    for start, end, notice in replacements:
        lines[start : end + 1] = [notice]

    ending = "\n" if content.endswith("\n") else ""
    return "\n".join(lines) + ending


def _strip_remaining_comments(content: str, suffix: str) -> str:
    """Remove ordinary comments while preserving fold notices and code."""
    style = _comment_style(suffix)
    lines = content.splitlines()
    cleaned: list[str] = []
    in_block = False

    for line in lines:
        stripped = line.strip()
        if _is_fold_notice(line):
            cleaned.append(line)
            continue

        if style == "slash":
            if "/*" in stripped and "*/" in stripped and not stripped.startswith("//"):
                # Keep fold-only; strip mid-line block comments cautiously.
                line = re.sub(r"/\*.*?\*/", "", line)
                stripped = line.strip()
            elif "/*" in stripped and stripped.startswith("/*"):
                in_block = True
                continue
            elif "*/" in stripped and in_block:
                in_block = False
                continue
            if in_block:
                continue
            if stripped.startswith("//"):
                continue
            if "//" in line and not ('"' in line or "'" in line or "`" in line):
                line = line.split("//")[0].rstrip()
        else:
            if stripped.startswith("#"):
                continue
            if "#" in line and not ('"' in line or "'" in line):
                line = line.split("#")[0].rstrip()

        if line.strip():
            cleaned.append(line)
        elif cleaned and cleaned[-1].strip():
            # defer blank handling to collapse_blank_lines
            cleaned.append("")

    return "\n".join(cleaned)


def clean_source_code(
    content: str,
    suffix: str = ".py",
    options: CodeCleanerOptions | None = None,
) -> str:
    """Fold non-executable prose in source without changing executable semantics.

    - Leading SPDX / copyright banners → one-line ``[License: ...]`` notice
    - Large commented-out code runs → one-line fold notice
    - Oversized blank-line runs collapsed
    - Obvious license docstring banners folded (descriptive docs kept)
    """
    if not content:
        return ""

    opts = options or CodeCleanerOptions()
    text = content

    if opts.fold_license:
        text = fold_license_header(text, suffix=suffix, min_lines=opts.min_license_lines)

    if opts.fold_banner_docstrings:
        text = fold_banner_docstrings(text, suffix=suffix)

    if opts.fold_commented_code:
        text = fold_commented_out_code(text, suffix=suffix, min_lines=opts.min_commented_code_lines)

    if opts.strip_remaining_comments:
        text = _strip_remaining_comments(text, suffix=suffix)

    if opts.collapse_blank_lines:
        text = collapse_blank_lines(text, max_run=opts.max_blank_run)

    return text
