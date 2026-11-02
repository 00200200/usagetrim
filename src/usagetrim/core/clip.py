"""In-chat clipboard and text compaction for AI assistants."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from usagetrim.core.cache import ContextCache
from usagetrim.core.diff_slimmer import slim_git_diff
from usagetrim.core.json_slimmer import slim_json
from usagetrim.core.redactor import redact_secrets
from usagetrim.core.safe_filter import safe_compact_output
from usagetrim.metrics.tokenizer import count_tokens

_COLOR = re.compile(r"\x1b\[[0-9;]*m")


def get_clipboard() -> str:
    """Read plain text from system clipboard (macOS pbpaste, or fallback)."""
    if platform.system() == "Darwin":
        try:
            return subprocess.check_output(["pbpaste"], text=True, timeout=2)
        except Exception:
            return ""
    return ""


def set_clipboard(text: str) -> bool:
    """Write via a native clipboard utility; success requires exit status zero.

    Prefer Wayland in mixed sessions, with X11 fallback when DISPLAY is set.
    subprocess.run kills and reaps the helper on timeout. No shell is involved.
    """
    system = platform.system()
    commands = []
    if system == "Darwin":
        commands = [["pbcopy"]]
    elif system == "Linux":
        if os.environ.get("WAYLAND_DISPLAY"):
            commands.append(["wl-copy"])
        if os.environ.get("DISPLAY"):
            commands.extend(
                [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
            )
    for command in commands:
        executable = shutil.which(command[0])
        if executable is None:
            continue
        try:
            result = subprocess.run(
                [executable, *command[1:]],
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            continue
        if result.returncode == 0:
            return True
    return False


@dataclass
class ClipResult:
    original_tokens: int
    compacted_tokens: int
    saved_tokens: int
    reduction_pct: float
    ref_id: str | None
    text: str
    content_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_tokens": self.original_tokens,
            "compacted_tokens": self.compacted_tokens,
            "saved_tokens": self.saved_tokens,
            "reduction_pct": self.reduction_pct,
            "ref_id": self.ref_id,
            "text": self.text,
            "content_type": self.content_type,
        }


def _compact_traceback(lines: list[str]) -> tuple[list[str], bool]:
    """Fold repeated recursive frames in exception tracebacks."""
    result: list[str] = []
    i = 0
    changed = False
    while i < len(lines):
        line = lines[i]
        # Detect frame repetition pattern: "  File ... line ... in ..."
        if line.strip().startswith("File ") and i + 1 < len(lines):
            frame = (line, lines[i + 1])
            repeats = 1
            j = i + 2
            while j + 1 < len(lines) and (lines[j], lines[j + 1]) == frame:
                repeats += 1
                j += 2
            if repeats > 3:
                result.append(frame[0])
                result.append(frame[1])
                result.append(
                    f"  [UsageTrim: identical recursive frame repeated {repeats - 1} times omitted]\n"
                )
                changed = True
                i = j
                continue
        result.append(line)
        i += 1
    return result, changed


def compact_text(text: str, budget: int = 2000) -> ClipResult:
    """Deterministically compact raw clipboard/chat text with secret scrubbing and CCR recovery."""
    cleaned = _COLOR.sub("", text)
    redacted = redact_secrets(cleaned)
    original_tokens = count_tokens(redacted).openai

    if not redacted.strip() or original_tokens <= 12:
        return ClipResult(
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
            saved_tokens=0,
            reduction_pct=0.0,
            ref_id=None,
            text=redacted,
            content_type="plain",
        )

    content_type = "plain"
    compacted = redacted
    ref_id = None

    # 1. Detect JSON
    stripped = redacted.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        try:
            json_preview = slim_json(stripped)
            if count_tokens(json_preview).openai < original_tokens:
                compacted = json_preview
                content_type = "json"
        except Exception:
            pass

    # 2. Detect Git diff
    if content_type == "plain" and ("diff --git " in redacted or "--- a/" in redacted):
        content_type = "diff"
        diff_res = slim_git_diff(redacted)
        if count_tokens(diff_res).openai < original_tokens:
            compacted = diff_res

    # 3. Detect pytest / test runner
    if content_type == "plain" and (
        "test session starts" in redacted or " PASSED" in redacted or " FAILURES " in redacted
    ):
        content_type = "pytest"
        test_res = safe_compact_output(redacted, command="pytest")
        if count_tokens(test_res).openai < original_tokens:
            compacted = test_res

    # 4. Detect traceback recursion and library frame compaction
    if content_type == "plain" and (
        "Traceback (most recent call last):" in redacted
        or (
            re.search(r"^(?:[A-Za-z]+Error|Error):.*", redacted, re.MULTILINE)
            and re.search(r"^\s+at\s+", redacted, re.MULTILINE)
        )
    ):
        content_type = "traceback"
        from usagetrim.core.specialized import filter_traceback

        tb_lines, changed = _compact_traceback(redacted.splitlines(keepends=True))
        base_tb = "".join(tb_lines) if changed else redacted
        tb_filtered = filter_traceback(base_tb)
        if count_tokens(tb_filtered).openai < original_tokens:
            compacted = tb_filtered

    # 5. General stream compaction: fold duplicate consecutive lines
    if content_type == "plain":
        lines = redacted.splitlines(keepends=True)
        res_lines: list[str] = []
        i = 0
        changed = False
        while i < len(lines):
            line = lines[i]
            j = i + 1
            while j < len(lines) and lines[j] == line:
                j += 1
            res_lines.append(line)
            if j - i > 2 and line.strip():
                if not line.endswith(("\n", "\r")):
                    res_lines.append("\n")
                res_lines.append(f"[UsageTrim: preceding line repeated {j - i} times total]\n")
                changed = True
                i = j
            else:
                res_lines.extend(lines[i + 1 : j])
                i = j
        if changed:
            compacted = "".join(res_lines)

    # 6. Check budget enforcement & CCR caching
    current_tokens = count_tokens(compacted).openai
    if current_tokens > budget:
        # Bounded truncation with recovery ref
        cache = ContextCache()
        ref_id = cache.store(redacted, source="clip-compaction")
        lines = compacted.splitlines(keepends=True)
        target_lines = max(4, int(len(lines) * (budget / current_tokens) * 0.8))
        head_count = target_lines // 2
        tail_count = target_lines - head_count
        head = lines[:head_count]
        tail = lines[-tail_count:] if tail_count > 0 else []
        omitted = len(lines) - len(head) - len(tail)
        trunc_block = (
            f"\n[... {omitted} lines omitted by UsageTrim to fit {budget} token budget. "
            f"Ref: {ref_id} - recover with: usagetrim retrieve {ref_id} ...]\n"
        )
        compacted = "".join(head) + trunc_block + "".join(tail)
        current_tokens = count_tokens(compacted).openai
        while current_tokens > budget and (len(head) > 2 or len(tail) > 2):
            if len(head) >= len(tail):
                head.pop()
            else:
                tail.pop(0)
            omitted = len(lines) - len(head) - len(tail)
            trunc_block = (
                f"\n[... {omitted} lines omitted by UsageTrim to fit {budget} token budget. "
                f"Ref: {ref_id} - recover with: usagetrim retrieve {ref_id} ...]\n"
            )
            compacted = "".join(head) + trunc_block + "".join(tail)
            current_tokens = count_tokens(compacted).openai
    elif compacted != redacted:
        cache = ContextCache()
        ref_id = cache.store(redacted, source="clip-compaction")
        if "usagetrim retrieve" not in compacted:
            compacted += (
                f"\n[UsageTrim: full original cached ({original_tokens} tokens). Ref: {ref_id}]\n"
            )
            current_tokens = count_tokens(compacted).openai

    saved = max(0, original_tokens - current_tokens)
    pct = round((saved / original_tokens) * 100, 1) if original_tokens else 0.0

    return ClipResult(
        original_tokens=original_tokens,
        compacted_tokens=current_tokens,
        saved_tokens=saved,
        reduction_pct=pct,
        ref_id=ref_id,
        text=compacted,
        content_type=content_type,
    )
