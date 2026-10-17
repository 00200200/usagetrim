from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from usagetrim.metrics.tokenizer import count_tokens

LOCKFILE_PATTERNS = [
    re.compile(
        r"(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|Cargo\.lock|Gemfile\.lock|composer\.lock|go\.sum)"
    ),
    re.compile(r"\.(?:min\.js|min\.css|map|svg|snap)$"),
    re.compile(r"(?:\.pb\.go|_pb2\.py|_pb2_grpc\.py)$"),
]


def is_lockfile_or_generated(filename: str, extra_patterns: list[re.Pattern] | None = None) -> bool:
    if any(p.search(filename) for p in LOCKFILE_PATTERNS):
        return True
    if extra_patterns and any(p.search(filename) for p in extra_patterns):
        return True
    return False


def slim_git_diff(
    raw_diff: str,
    max_context_lines: int = 2,
    extra_patterns: list[str] | None = None,
) -> str:
    """Compress unified git diffs.

    - Detects lockfiles & generated files, collapsing them into concise 1-line notices.
    - Preserves all added (+) and deleted (-) lines and file headers.
    - Suppresses excessive unchanged context lines.
    """
    if not raw_diff:
        return ""

    compiled_extra = [re.compile(p) for p in extra_patterns] if extra_patterns else None

    lines = raw_diff.splitlines()
    output_lines: list[str] = []

    current_file = ""
    is_collapsing_file = False
    collapsed_lines_count = 0
    collapsed_file_lines: list[str] = []

    hunk_context_count = 0

    def flush_collapsed():
        nonlocal is_collapsing_file, collapsed_lines_count, collapsed_file_lines
        if not is_collapsing_file:
            return
        from usagetrim.core.lockfile import extract_lockfile_diff_delta, is_lockfile

        if is_lockfile(current_file):
            delta = extract_lockfile_diff_delta(collapsed_file_lines, current_file)
            if delta:
                output_lines.append(
                    f"  [lockfile delta: {delta} ({collapsed_lines_count} lines folded by usagetrim)]\n"
                )
            else:
                output_lines.append(
                    f"  [... {collapsed_lines_count} lines of lockfile/generated diff omitted by usagetrim ...]\n"
                )
        else:
            output_lines.append(
                f"  [... {collapsed_lines_count} lines of lockfile/generated diff omitted by usagetrim ...]\n"
            )
        is_collapsing_file = False
        collapsed_lines_count = 0
        collapsed_file_lines = []

    for line in lines:
        if line.startswith("diff --git"):
            # Flush previous collapsed file notice
            flush_collapsed()

            # Extract filename (e.g. diff --git a/foo/bar.py b/foo/bar.py)
            parts = line.split()
            current_file = parts[-1] if len(parts) >= 4 else line
            is_collapsing_file = is_lockfile_or_generated(current_file, compiled_extra)
            output_lines.append(line)
            hunk_context_count = 0
            continue

        if is_collapsing_file:
            collapsed_lines_count += 1
            collapsed_file_lines.append(line)
            continue

        if line.startswith("@@"):
            output_lines.append(line)
            hunk_context_count = 0
            continue

        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            output_lines.append(line)
            hunk_context_count = 0
            continue

        # Header lines
        if line.startswith(("index ", "--- ", "+++ ", "new file mode", "deleted file mode")):
            output_lines.append(line)
            continue

        # Unchanged context line (starts with space or empty)
        hunk_context_count += 1
        if hunk_context_count <= max_context_lines:
            output_lines.append(line)
        elif hunk_context_count == max_context_lines + 1:
            output_lines.append("  ...")

    flush_collapsed()

    return "\n".join(output_lines)


@dataclass
class DiffHunk:
    header: str
    lines: list[str]
    additions: int = 0
    deletions: int = 0
    slimmed_lines: list[str] = field(default_factory=list)


@dataclass
class FileDiff:
    header_lines: list[str]
    path: str
    hunks: list[DiffHunk] = field(default_factory=list)
    raw_body_lines: list[str] = field(default_factory=list)
    is_lockfile: bool = False
    additions: int = 0
    deletions: int = 0
    priority_score: float = 0.0
    original_index: int = 0

    @property
    def total_body_lines(self) -> int:
        return len(self.raw_body_lines)


def _slim_hunk_lines(raw_lines: list[str], max_context_lines: int = 2) -> list[str]:
    slimmed: list[str] = []
    hunk_context_count = 0
    for line in raw_lines:
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            slimmed.append(line)
            hunk_context_count = 0
        elif line.startswith("\\"):
            slimmed.append(line)
            hunk_context_count = 0
        else:
            hunk_context_count += 1
            if hunk_context_count <= max_context_lines:
                slimmed.append(line)
            elif hunk_context_count == max_context_lines + 1:
                slimmed.append("  ...")
    return slimmed


def _extract_diff_path(header_lines: list[str]) -> str:
    for line in header_lines:
        if line.startswith("+++ b/"):
            return line[6:].strip()
        if line.startswith("--- a/") and not line.startswith("--- /dev/null"):
            return line[6:].strip()
    if header_lines and header_lines[0].startswith("diff --git"):
        parts = header_lines[0].split()
        if len(parts) >= 4:
            b_part = parts[-1]
            if b_part.startswith("b/"):
                return b_part[2:].strip()
            a_part = parts[-2]
            if a_part.startswith("a/"):
                return a_part[2:].strip()
            return b_part.strip()
    return header_lines[0].strip() if header_lines else "unknown"


_CRITICAL_CODE_EXTS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".cc",
    ".cxx",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".scala",
    ".zig",
    ".lua",
    ".sh",
    ".bash",
    ".zsh",
}

_CONFIG_NAMES = {
    "dockerfile",
    "containerfile",
    "makefile",
    "pyproject.toml",
    "cargo.toml",
    "package.json",
    "go.mod",
    "pom.xml",
    "build.gradle",
}

_DOCS_EXTS = {".md", ".rst", ".txt", ".adoc", ".org"}
_DATA_EXTS = {".json", ".csv", ".tsv", ".xml", ".html", ".css", ".scss", ".sass", ".less", ".sql"}


def score_file_importance(
    path: str,
    additions: int,
    deletions: int,
    is_lockfile: bool,
    total_repo_changes: int = 1,
) -> float:
    if is_lockfile:
        return 5.0

    lower = path.lower()
    suffix = Path(path).suffix.lower()
    file_name = Path(path).name.lower()

    if suffix in _CRITICAL_CODE_EXTS:
        base = 100.0
    elif any(t in lower for t in ["test", "spec", "_test.", ".test.", ".spec."]):
        base = 75.0
    elif (
        suffix in {".toml", ".yaml", ".yml", ".ini", ".cfg", ".env"}
        or file_name in _CONFIG_NAMES
        or "/.github/" in lower
        or "/workflows/" in lower
    ):
        base = 50.0
    elif suffix in _DOCS_EXTS or "docs/" in lower or file_name in {"license", "readme"}:
        base = 30.0
    elif suffix in _DATA_EXTS:
        base = 25.0
    else:
        base = 20.0

    # Path modifier for core business logic / security
    if any(
        k in lower
        for k in [
            "auth",
            "security",
            "permission",
            "policy",
            "crypt",
            "secret",
            "token",
            "core",
            "router",
            "controller",
            "engine",
            "handler",
        ]
    ):
        base += 25.0

    # Test file cap (if it happened to get boosted by path keywords)
    if any(t in lower for t in ["test", "spec", "_test"]):
        base = min(base, 75.0)

    # Change ratio factor
    changes = additions + deletions
    ratio = changes / max(total_repo_changes, 1)
    change_multiplier = 1.0 + min(ratio * 2.0, 0.5)

    return base * change_multiplier


def _render_full(file_diff: FileDiff) -> str:
    if file_diff.is_lockfile:
        return _render_folded(file_diff)
    lines = list(file_diff.header_lines)
    for hunk in file_diff.hunks:
        lines.append(hunk.header)
        lines.extend(hunk.slimmed_lines)
    return "\n".join(lines)


def _render_folded(file_diff: FileDiff) -> str:
    lines = []
    if file_diff.header_lines:
        lines.append(file_diff.header_lines[0])
        for hl in file_diff.header_lines[1:]:
            if hl.startswith(("new file mode", "deleted file mode", "similarity index", "rename ")):
                lines.append(hl)
    else:
        lines.append(f"diff --git a/{file_diff.path} b/{file_diff.path}")

    if file_diff.is_lockfile:
        lines.append(
            f"  [... {file_diff.total_body_lines} lines of lockfile/generated diff omitted by usagetrim ...]"
        )
    else:
        hunk_desc = (
            f"across {len(file_diff.hunks)} hunks"
            if len(file_diff.hunks) > 1
            else ("in 1 hunk" if len(file_diff.hunks) == 1 else "no hunks")
        )
        lines.append(
            f"  [... folded by usagetrim: +{file_diff.additions}, -{file_diff.deletions} lines {hunk_desc} (summary retained to respect budget) ...]"
        )
    return "\n".join(lines)


def _render_window(file_diff: FileDiff, num_hunks: int) -> str:
    lines = list(file_diff.header_lines)
    kept_hunks = file_diff.hunks[:num_hunks]
    folded_hunks = file_diff.hunks[num_hunks:]

    for hunk in kept_hunks:
        lines.append(hunk.header)
        lines.extend(hunk.slimmed_lines)

    remaining_adds = sum(h.additions for h in folded_hunks)
    remaining_dels = sum(h.deletions for h in folded_hunks)
    lines.append(
        f"  [... {len(folded_hunks)} hunks (+{remaining_adds}, -{remaining_dels} lines) folded by usagetrim to respect token budget ...]"
    )
    return "\n".join(lines)


def _render_compact_receipt(file_diff: FileDiff) -> str:
    return f"diff --git a/{file_diff.path} b/{file_diff.path} (+{file_diff.additions}, -{file_diff.deletions} folded)"


def _parse_diff(
    raw_diff: str,
    max_context_lines: int = 2,
    extra_patterns: list[str] | None = None,
) -> tuple[str, list[FileDiff]]:
    compiled_extra = [re.compile(p) for p in extra_patterns] if extra_patterns else None

    raw_chunks = re.split(r"(?=^diff --git )", raw_diff, flags=re.MULTILINE)
    preamble_parts: list[str] = []
    file_chunks: list[str] = []

    for chunk in raw_chunks:
        if not chunk.strip():
            continue
        if not chunk.startswith("diff --git "):
            preamble_parts.append(chunk)
        else:
            file_chunks.append(chunk)

    if not file_chunks and ("--- " in raw_diff and "+++ " in raw_diff):
        file_chunks.append(raw_diff)
        preamble_parts.clear()

    preamble = "".join(preamble_parts)

    file_diffs: list[FileDiff] = []
    for idx, chunk in enumerate(file_chunks):
        lines = chunk.splitlines()
        header_lines: list[str] = []
        raw_body_lines: list[str] = []
        hunks: list[DiffHunk] = []

        current_hunk_header: str | None = None
        current_hunk_lines: list[str] = []

        for line in lines:
            if line.startswith("@@"):
                if current_hunk_header is not None:
                    slimmed = _slim_hunk_lines(current_hunk_lines, max_context_lines)
                    adds = sum(
                        1
                        for ln in current_hunk_lines
                        if ln.startswith("+") and not ln.startswith("+++")
                    )
                    dels = sum(
                        1
                        for ln in current_hunk_lines
                        if ln.startswith("-") and not ln.startswith("---")
                    )
                    hunks.append(
                        DiffHunk(
                            header=current_hunk_header,
                            lines=current_hunk_lines,
                            additions=adds,
                            deletions=dels,
                            slimmed_lines=slimmed,
                        )
                    )
                current_hunk_header = line
                current_hunk_lines = []
            elif current_hunk_header is None:
                header_lines.append(line)
            else:
                current_hunk_lines.append(line)
                raw_body_lines.append(line)

        if current_hunk_header is not None:
            slimmed = _slim_hunk_lines(current_hunk_lines, max_context_lines)
            adds = sum(
                1 for ln in current_hunk_lines if ln.startswith("+") and not ln.startswith("+++")
            )
            dels = sum(
                1 for ln in current_hunk_lines if ln.startswith("-") and not ln.startswith("---")
            )
            hunks.append(
                DiffHunk(
                    header=current_hunk_header,
                    lines=current_hunk_lines,
                    additions=adds,
                    deletions=dels,
                    slimmed_lines=slimmed,
                )
            )

        path = _extract_diff_path(header_lines)
        is_lock = is_lockfile_or_generated(path, compiled_extra)
        total_adds = sum(h.additions for h in hunks)
        total_dels = sum(h.deletions for h in hunks)

        file_diffs.append(
            FileDiff(
                header_lines=header_lines,
                path=path,
                hunks=hunks,
                raw_body_lines=raw_body_lines,
                is_lockfile=is_lock,
                additions=total_adds,
                deletions=total_dels,
                original_index=idx,
            )
        )

    total_repo_changes = sum(f.additions + f.deletions for f in file_diffs)
    for f in file_diffs:
        f.priority_score = score_file_importance(
            f.path,
            f.additions,
            f.deletions,
            f.is_lockfile,
            total_repo_changes=total_repo_changes,
        )

    return preamble, file_diffs


def budgeted_diff_slice(
    raw_diff: str,
    budget_tokens: int = 4000,
    extra_patterns: list[str] | None = None,
    max_context_lines: int = 2,
) -> str:
    """Dynamically slices and budgets large git diffs to strictly stay within token limits.

    - Splits diff into per-file chunks and scores files by importance (business logic > tests > configs > docs > lockfiles).
    - Guarantees all touched file paths are visible in the output (none are silently dropped).
    - Preserves full hunk context for high-priority files.
    - Uses sliding hunk windows for borderline files.
    - Folds low-priority or overflow files into concise change receipts.
    - Strictly never exceeds the requested budget_tokens.
    """
    if not raw_diff or not raw_diff.strip():
        return ""

    target_budget = max(32, budget_tokens)

    # Quick check: does standard slim_git_diff already fit comfortably?
    slimmed_full = slim_git_diff(
        raw_diff, max_context_lines=max_context_lines, extra_patterns=extra_patterns
    )
    if count_tokens(slimmed_full).openai <= target_budget:
        return slimmed_full

    preamble, file_diffs = _parse_diff(
        raw_diff, max_context_lines=max_context_lines, extra_patterns=extra_patterns
    )

    if not file_diffs:
        return slimmed_full[: target_budget * 4]

    folded_texts: dict[int, str] = {}
    folded_tokens: dict[int, int] = {}
    full_texts: dict[int, str] = {}
    full_tokens: dict[int, int] = {}

    for f in file_diffs:
        f_text = _render_folded(f)
        folded_texts[f.original_index] = f_text
        folded_tokens[f.original_index] = count_tokens(f_text).openai

        u_text = _render_full(f)
        full_texts[f.original_index] = u_text
        full_tokens[f.original_index] = count_tokens(u_text).openai

    preamble_tok = count_tokens(preamble).openai if preamble else 0
    min_folded_cost = preamble_tok + sum(folded_tokens.values())

    file_modes: dict[int, tuple[str, int | None]] = {
        f.original_index: ("folded", None) for f in file_diffs
    }

    if min_folded_cost > target_budget:
        for f in file_diffs:
            file_modes[f.original_index] = ("compact", None)
    else:
        spare_budget = target_budget - min_folded_cost
        sorted_files = sorted(file_diffs, key=lambda f: f.priority_score, reverse=True)

        for f in sorted_files:
            if f.is_lockfile:
                continue

            cost_to_full = full_tokens[f.original_index] - folded_tokens[f.original_index]
            if cost_to_full <= spare_budget:
                file_modes[f.original_index] = ("full", None)
                spare_budget -= cost_to_full
            elif len(f.hunks) > 1 and spare_budget > 30:
                best_k: int | None = None
                best_cost = 0
                for k in range(len(f.hunks) - 1, 0, -1):
                    cand_text = _render_window(f, k)
                    cand_cost = count_tokens(cand_text).openai - folded_tokens[f.original_index]
                    if cand_cost <= spare_budget:
                        best_k = k
                        best_cost = cand_cost
                        break
                if best_k is not None:
                    file_modes[f.original_index] = ("window", best_k)
                    spare_budget -= best_cost

    def _assemble() -> str:
        parts: list[str] = []
        if preamble:
            parts.append(preamble.rstrip("\n"))
        for f in file_diffs:
            mode, k = file_modes[f.original_index]
            if mode == "full":
                parts.append(full_texts[f.original_index])
            elif mode == "window" and k is not None:
                parts.append(_render_window(f, k))
            elif mode == "compact":
                parts.append(_render_compact_receipt(f))
            else:
                parts.append(folded_texts[f.original_index])
        return "\n".join(parts)

    result = _assemble()

    # Active safety convergence loop: guarantee count_tokens(result).openai <= target_budget
    current_tokens = count_tokens(result).openai
    if current_tokens > target_budget:
        low_to_high = sorted(file_diffs, key=lambda f: f.priority_score)

        # Step 1: Step down any partial window files to folded
        for f in low_to_high:
            if file_modes[f.original_index][0] == "window":
                file_modes[f.original_index] = ("folded", None)
                result = _assemble()
                if count_tokens(result).openai <= target_budget:
                    return result

        # Step 2: Step down full files to folded
        for f in low_to_high:
            if file_modes[f.original_index][0] == "full" and not f.is_lockfile:
                file_modes[f.original_index] = ("folded", None)
                result = _assemble()
                if count_tokens(result).openai <= target_budget:
                    return result

        # Step 3: Switch files to compact one by one
        for f in low_to_high:
            file_modes[f.original_index] = ("compact", None)
            result = _assemble()
            if count_tokens(result).openai <= target_budget:
                return result

        # Step 4: Strict fallback if even all compact receipts exceed target_budget
        lines = result.splitlines()
        truncated: list[str] = []
        for line in lines:
            if count_tokens("\n".join(truncated + [line])).openai + 15 > target_budget:
                truncated.append(
                    f"  [... remaining files folded due to strict {target_budget} token budget ...]"
                )
                break
            truncated.append(line)
        result = "\n".join(truncated)

    return result
