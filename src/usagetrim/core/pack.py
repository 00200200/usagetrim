"""Context packager for AI coding assistants (local, AST-compacted, secret-scrubbed)."""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from usagetrim.core.cache import ContextCache
from usagetrim.core.code_index import CodeIndex, allowed_file, source_files
from usagetrim.core.redactor import redact_secrets
from usagetrim.core.skeleton import (
    extract_symbol_or_range,
    skeletonize_code_ast,
    skeletonize_python,
    strip_comments_and_blanks,
)
from usagetrim.metrics.tokenizer import count_tokens


@dataclass
class PackedFile:
    path: str
    original_tokens: int
    packed_tokens: int
    is_skeleton: bool
    ref_id: str | None
    content: str
    file_type: str = "modified"


@dataclass
class PackResult:
    root: Path
    file_count: int
    original_tokens: int
    packed_tokens: int
    saved_tokens: int
    reduction_pct: float
    files: list[PackedFile]
    bundle_text: str
    mode: str | None = None
    format_type: str | None = None
    omitted_files: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": redact_secrets(str(self.root)),
            "file_count": self.file_count,
            "original_tokens": self.original_tokens,
            "packed_tokens": self.packed_tokens,
            "saved_tokens": self.saved_tokens,
            "reduction_pct": self.reduction_pct,
            "bundle_text": self.bundle_text,
            "mode": self.mode,
            "format": self.format_type,
            "omitted_files": self.omitted_files or [],
            "files": [
                {
                    "path": f.path,
                    "original_tokens": f.original_tokens,
                    "packed_tokens": f.packed_tokens,
                    "is_skeleton": f.is_skeleton,
                    "ref_id": f.ref_id,
                    "file_type": getattr(f, "file_type", "modified"),
                }
                for f in self.files
            ],
        }


_PACK_FORMATS = {"xml", "markdown"}
_PACK_MODES = {"full", "skeleton", "hybrid"}
_SKELETON_EXTENSIONS = {".py", ".ts", ".go", ".rs"}
_COMMENT_STRIP_EXTENSIONS = _SKELETON_EXTENSIONS | {
    ".pyi",
    ".js",
    ".jsx",
    ".tsx",
    ".swift",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}
_PACK_PRIORITY = {".py": 0, ".ts": 1, ".go": 2, ".rs": 3}


def _pack_selector(root: Path, value: str | Path) -> str:
    """Validate a repo-relative selector without following a symlink outside root."""
    item = Path(value)
    if item.is_absolute() or ".." in item.parts or not item.parts:
        raise ValueError("focus must contain repo-relative paths without '..'")
    target = root / item
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("focus path cannot be resolved safely") from exc
    if not resolved.is_relative_to(root) or any(
        part.is_symlink()
        for part in (target, *target.parents)
        if part != root and root in part.parents
    ):
        raise ValueError("focus must stay inside the project root and avoid symlinks")
    return item.as_posix().rstrip("/")


def _pack_files(root: Path, paths: list[str | Path] | None) -> list[Path]:
    eligible = source_files(root)
    if not paths:
        return eligible
    selected: set[Path] = set()
    for entry in paths:
        item = Path(entry)
        if ".." in item.parts:
            raise ValueError("paths must not contain '..'")
        target = item if item.is_absolute() else root / item
        try:
            resolved = target.resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError("path cannot be resolved safely") from exc
        if not resolved.is_relative_to(root):
            raise ValueError("paths must stay inside the project root")
        if any(
            part.is_symlink()
            for part in (target, *target.parents)
            if part != root and root in part.parents
        ):
            raise ValueError("paths must not traverse symlinks")
        selected.update(path for path in eligible if path == target or target in path.parents)
    return sorted(selected)


def _xml_safe(value: str) -> str:
    """Replace characters forbidden by XML 1.0 before ElementTree escaping."""
    return "".join(
        char
        if ord(char) in {9, 10, 13}
        or 32 <= ord(char) <= 0xD7FF
        or 0xE000 <= ord(char) <= 0xFFFD
        or 0x10000 <= ord(char) <= 0x10FFFF
        else "\ufffd"
        for char in value
    )


def _pack_tree(paths: list[str], *, compact: bool) -> str:
    if compact:
        return "\n".join(
            sorted({path.split("/", 1)[0] + ("/" if "/" in path else "") for path in paths})
        )
    rows: set[tuple[str, int]] = set()
    for path in paths:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            rows.add(("/".join(parts[:depth]) + "/", depth - 1))
        rows.add((path, len(parts) - 1))
    return "\n".join(
        "  " * depth + name.rstrip("/").split("/")[-1] + ("/" if name.endswith("/") else "")
        for name, depth in sorted(rows)
    )


def _pack_fence(content: str) -> str:
    return "`" * max(3, max((len(run) for run in re.findall(r"`+", content)), default=0) + 1)


def _parseable_for_skeleton(source: str, suffix: str) -> bool:
    """Reject malformed syntax before calling skeletonizers with regex fallbacks."""
    if suffix == ".py":
        try:
            ast.parse(source)
            return True
        except (SyntaxError, ValueError):
            return False
    from ast_grep_py import SgRoot

    language = {".ts": "typescript", ".go": "go", ".rs": "rust"}[suffix]
    try:
        root = SgRoot(source, language).root()
        stack = [root]
        while stack:
            node = stack.pop()
            span = node.range()
            if node.kind() == "ERROR" or (node is not root and span.start.index == span.end.index):
                return False
            stack.extend(node.children())
        return True
    except (ValueError, RecursionError):
        return False


def _render_repository_pack(
    *,
    root_name: str,
    format_type: str,
    mode: str,
    candidates: list[PackedFile],
    selected: list[PackedFile],
    source_tokens: int,
    detail: int,
) -> str:
    selected_ids = {id(file) for file in selected}
    omitted = [file for file in candidates if id(file) not in selected_ids]
    all_paths = [file.path for file in candidates]
    if format_type == "xml":
        outer = ET.Element("repository")
        ET.SubElement(
            outer,
            "file_summary",
            {
                "name": _xml_safe(root_name),
                "mode": mode,
                "files": str(len(candidates)),
                "packed": str(len(selected)),
                "omitted": str(len(omitted)),
                "source_tokens": str(source_tokens),
            },
        )
        tree = ET.SubElement(outer, "directory_structure")
        tree.text = _xml_safe(_pack_tree(all_paths, compact=detail > 0)) if detail < 2 else ""
        files = ET.SubElement(outer, "files")
        for file in selected:
            attributes = {
                "path": _xml_safe(file.path),
                "mode": "skeleton" if file.is_skeleton else "full",
            }
            if file.ref_id:
                attributes["ref"] = file.ref_id
            element = ET.SubElement(
                files,
                "file",
                attributes,
            )
            element.text = _xml_safe(file.content)
        omitted_element = ET.SubElement(outer, "omitted_files", {"count": str(len(omitted))})
        if detail == 0:
            for file in omitted:
                ET.SubElement(omitted_element, "file", {"path": _xml_safe(file.path)})
        return ET.tostring(outer, encoding="unicode") + "\n"
    lines = [
        f"# Repository: {root_name}",
        "",
        "## File Summary",
        f"Mode: {mode}; files: {len(candidates)}; packed: {len(selected)}; omitted: {len(omitted)}; source tokens: {source_tokens}",
        "",
        "## Directory Structure",
    ]
    tree = _pack_tree(all_paths, compact=detail > 0) if detail < 2 else ""
    fence = _pack_fence(tree)
    lines.extend([fence, tree, fence, "", "## Files"])
    for file in selected:
        language = Path(file.path).suffix.lstrip(".") or "text"
        fence = _pack_fence(file.content)
        lines.extend(
            [
                "",
                f"### File: {file.path} ({'skeleton' if file.is_skeleton else 'full'}"
                + (f"; recover: usagetrim retrieve {file.ref_id}" if file.ref_id else "")
                + ")",
                f"{fence}{language}",
                file.content + ("" if file.content.endswith("\n") else "\n") + fence,
            ]
        )
    lines.extend(["", "## Omitted Files", f"Count: {len(omitted)}"])
    if detail == 0:
        lines.extend(file.path for file in omitted)
    return "\n".join(lines) + "\n"


def pack_repository(
    root: Path,
    format_type: str = "xml",
    mode: str = "skeleton",
    budget: int | None = None,
    focus: list[str] | None = None,
    *,
    paths: list[str | Path] | None = None,
    strip_comments: bool = False,
) -> PackResult:
    """Pack eligible files into a deterministic, complete, strictly budgeted document."""
    if (
        type(format_type) is not str
        or type(mode) is not str
        or format_type not in _PACK_FORMATS
        or mode not in _PACK_MODES
    ):
        raise ValueError("format must be xml or markdown; mode must be full, skeleton or hybrid")
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise ValueError("budget must be a positive integer or None")
    if focus and mode != "hybrid":
        raise ValueError("focus is only valid with hybrid mode")
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("root must be a directory")
    if focus is not None and (
        not isinstance(focus, list) or any(type(item) is not str for item in focus)
    ):
        raise ValueError("focus must be a list of repo-relative paths")
    focus_paths = [_pack_selector(root, item) for item in (focus or [])]
    safe_focus_paths = [redact_secrets(item).replace("\n", "\\n") for item in focus_paths]
    selected_paths = _pack_files(root, paths)
    candidates: list[PackedFile] = []
    full_sources: dict[str, str] = {}
    source_tokens = 0
    for path in selected_paths:
        relative = path.relative_to(root).as_posix()
        try:
            raw_bytes = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw_bytes:
            continue
        try:
            raw_text = raw_bytes.decode("utf-8")
            valid_utf8 = True
        except UnicodeDecodeError:
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            valid_utf8 = False
        cleaned = redact_secrets(raw_text)
        original_tokens = count_tokens(cleaned).openai
        source_tokens += original_tokens
        focused = any(relative == item or relative.startswith(item + "/") for item in focus_paths)
        skeleton = valid_utf8 and mode != "full" and not (mode == "hybrid" and focused)
        packed_content = cleaned
        if skeleton and path.suffix.lower() in _SKELETON_EXTENSIONS:
            if _parseable_for_skeleton(cleaned, path.suffix.lower()):
                proposed = (
                    skeletonize_python(cleaned)
                    if path.suffix.lower() == ".py"
                    else skeletonize_code_ast(cleaned, path.suffix.lower())
                )
                if proposed.strip():
                    ref = "tc_" + hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
                    if count_tokens(proposed).openai < original_tokens:
                        packed_content = proposed
                    else:
                        skeleton = False
                else:
                    skeleton = False
            else:
                skeleton = False
        else:
            skeleton = False
        if not skeleton and strip_comments and path.suffix.lower() in _COMMENT_STRIP_EXTENSIONS:
            packed_content = strip_comments_and_blanks(cleaned, suffix=path.suffix)
        safe_path = redact_secrets(relative).replace("\n", "\\n")
        if skeleton:
            full_sources[ref] = cleaned
        candidates.append(
            PackedFile(
                safe_path,
                original_tokens,
                count_tokens(packed_content).openai,
                skeleton,
                ref if skeleton else None,
                redact_secrets(packed_content),
                file_type="repository",
            )
        )
    candidates.sort(
        key=lambda file: (
            0
            if mode == "hybrid"
            and any(
                file.path == item or file.path.startswith(item + "/") for item in safe_focus_paths
            )
            else 1,
            _PACK_PRIORITY.get(Path(file.path).suffix.lower(), 9),
            file.path,
        )
    )
    root_name = redact_secrets(root.name)

    def render(selected: list[PackedFile], detail: int) -> tuple[str, int]:
        text = _render_repository_pack(
            root_name=root_name,
            format_type=format_type,
            mode=mode,
            candidates=candidates,
            selected=selected,
            source_tokens=source_tokens,
            detail=detail,
        )
        return text, count_tokens(text).openai

    packable = candidates
    count = len(packable)
    bundle_text, packed_tokens = render(packable, 0)
    if budget is not None and packed_tokens > budget:
        minimum_tokens = render([], 2)[1]
        if minimum_tokens > budget:
            raise ValueError("budget is too small for a valid repository pack")
        # A file whose content alone exceeds the available space cannot fit.
        # Skipping it lets smaller, lower-ranked files remain eligible.
        packable = [file for file in candidates if file.packed_tokens <= budget - minimum_tokens]
        choices = []
        for level in range(3):
            _, minimal_tokens = render([], level)
            if minimal_tokens > budget:
                continue
            selected = []
            text, tokens = render(selected, level)
            for file in packable:
                candidate_text, candidate_tokens = render([*selected, file], level)
                if candidate_tokens <= budget:
                    selected.append(file)
                    text, tokens = candidate_text, candidate_tokens
            choices.append((len(selected), -level, selected, text, tokens))
        if not choices:
            raise ValueError("budget is too small for a valid repository pack")
        count, _, packed_files, bundle_text, packed_tokens = max(choices, key=lambda x: x[:2])
    else:
        packed_files = packable[:count]
    if any(file.is_skeleton for file in packed_files):
        cache = ContextCache()
        for file in packed_files:
            if file.is_skeleton:
                stored = cache.store(full_sources[file.ref_id], source=f"pack:{file.path}")
                if stored != file.ref_id:
                    raise RuntimeError("recovery reference changed during packing")
    packed_ids = {id(file) for file in packed_files}
    omitted = [file.path for file in candidates if id(file) not in packed_ids]
    saved = max(0, source_tokens - packed_tokens)
    return PackResult(
        root,
        len(packed_files),
        source_tokens,
        packed_tokens,
        saved,
        round(100 * saved / source_tokens, 1) if source_tokens else 0.0,
        packed_files,
        bundle_text,
        mode,
        format_type,
        omitted,
    )


def pack_context(
    paths: list[str | Path] | None = None,
    root: Path | None = None,
    budget: int = 4000,
    force_skeleton: bool = False,
    format_type: str = "markdown",
) -> PackResult:
    """Pack repository files into an AI-optimized, token-budgeted prompt context."""
    if root is None:
        root = Path.cwd()
    root = root.resolve()

    selected_files: list[Path] = []
    if paths:
        for p in paths:
            resolved = (root / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            if resolved.is_file() and allowed_file(resolved, root):
                selected_files.append(resolved)
            elif resolved.is_dir():
                selected_files.extend(source_files(resolved))
    else:
        selected_files = source_files(root)

    # Deduplicate while preserving order
    seen: set[Path] = set()
    files_to_pack: list[Path] = []
    for f in selected_files:
        if f not in seen and f.is_file():
            seen.add(f)
            files_to_pack.append(f)

    cache = ContextCache()
    packed_files: list[PackedFile] = []
    total_original_tokens = 0
    running_packed_tokens = 0

    # First pass: collect files and compute skeletons
    for path in files_to_pack:
        rel_str = str(path.relative_to(root))
        try:
            raw_text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        cleaned = redact_secrets(raw_text)
        orig_tok = count_tokens(cleaned).openai
        total_original_tokens += orig_tok

        # Determine if we should skeletonize
        use_skeleton = force_skeleton or (orig_tok > 250)
        packed_text = cleaned
        ref_id = None

        if use_skeleton:
            try:
                skel = extract_symbol_or_range(path, skeleton=True)
                skel_tok = count_tokens(skel).openai
                if skel_tok < orig_tok:
                    ref_id = cache.store(cleaned, source=f"pack:{rel_str}")
                    packed_text = (
                        skel
                        + f"\n# [Full implementation omitted; {orig_tok} tokens. Recover: usagetrim retrieve {ref_id}]\n"
                    )
            except Exception:
                pass

        p_tok = count_tokens(packed_text).openai
        running_packed_tokens += p_tok
        packed_files.append(
            PackedFile(
                path=rel_str,
                original_tokens=orig_tok,
                packed_tokens=p_tok,
                is_skeleton=use_skeleton and (packed_text != cleaned),
                ref_id=ref_id,
                content=packed_text,
            )
        )

    # Second pass: budget enforcement
    # If running_packed_tokens exceeds budget, compress lowest-priority files further
    if running_packed_tokens > budget and packed_files:
        # Keep high-level tree intact, truncate remaining files
        budget_per_file = max(20, (budget - len(packed_files) * 25) // len(packed_files))
        for pf in packed_files:
            if pf.packed_tokens > budget_per_file:
                lines = pf.content.splitlines(keepends=True)
                keep_count = min(len(lines), max(4, budget_per_file // 5))
                if len(lines) > keep_count:
                    if not pf.ref_id:
                        full_path = root / pf.path
                        try:
                            orig = redact_secrets(full_path.read_text(errors="replace"))
                            pf.ref_id = cache.store(orig, source=f"pack:{pf.path}")
                        except Exception:
                            pass
                    trunc_note = (
                        f"\n# [... {len(lines) - keep_count} lines omitted. Ref: {pf.ref_id} ...]\n"
                    )
                    pf.content = "".join(lines[:keep_count]) + trunc_note
                    pf.packed_tokens = count_tokens(pf.content).openai

    # Construct final bundle text
    header = [
        f"# UsageTrim Context Bundle ({root.name})",
        f"Files: {len(packed_files)} · Estimated tokens: ~{sum(f.packed_tokens for f in packed_files)} (Budget: {budget})",
        "",
        "## File Summary",
        "```text",
    ]
    for pf in packed_files:
        mode_str = "AST Skeleton" if pf.is_skeleton else "Full Source"
        header.append(f"{pf.path:<40} {pf.packed_tokens:>6} tok  [{mode_str}]")
    header.append("```")
    header.append("")

    sections: list[str] = ["\n".join(header)]

    for pf in packed_files:
        lang = Path(pf.path).suffix.lstrip(".") or "text"
        title_suffix = " (AST Skeleton)" if pf.is_skeleton else ""
        sections.append(f"## {pf.path}{title_suffix}\n```{lang}\n{pf.content.strip()}\n```\n")

    bundle_text = "\n".join(sections)
    final_tokens = count_tokens(bundle_text).openai

    # Adaptive loop to strictly satisfy budget ceiling
    while final_tokens > budget and any(len(pf.content.splitlines()) > 5 for pf in packed_files):
        largest = max(packed_files, key=lambda f: f.packed_tokens)
        lines = largest.content.splitlines(keepends=True)
        if len(lines) <= 5:
            break
        new_keep = max(2, int(len(lines) * 0.5))
        if not largest.ref_id:
            try:
                full_path = root / largest.path
                orig = redact_secrets(full_path.read_text(errors="replace"))
                largest.ref_id = cache.store(orig, source=f"pack:{largest.path}")
            except Exception:
                pass
        trunc_note = f"\n# [... {len(lines) - new_keep} lines omitted. Ref: {largest.ref_id} ...]\n"
        largest.content = "".join(lines[:new_keep]) + trunc_note
        largest.packed_tokens = count_tokens(largest.content).openai

        hdr = [
            f"# UsageTrim Context Bundle ({root.name})",
            f"Files: {len(packed_files)} · Estimated tokens: ~{sum(f.packed_tokens for f in packed_files)} (Budget: {budget})",
            "",
            "## File Summary",
            "```text",
        ]
        for pf in packed_files:
            mode_str = "AST Skeleton" if pf.is_skeleton else "Full Source"
            hdr.append(f"{pf.path:<40} {pf.packed_tokens:>6} tok  [{mode_str}]")
        hdr.append("```\n")
        sec = ["\n".join(hdr)]
        for pf in packed_files:
            lang = Path(pf.path).suffix.lstrip(".") or "text"
            title_suffix = " (AST Skeleton)" if pf.is_skeleton else ""
            sec.append(f"## {pf.path}{title_suffix}\n```{lang}\n{pf.content.strip()}\n```\n")
        bundle_text = "\n".join(sec)
        final_tokens = count_tokens(bundle_text).openai

    saved = max(0, total_original_tokens - final_tokens)
    pct = round((saved / total_original_tokens) * 100, 1) if total_original_tokens else 0.0

    return PackResult(
        root=root,
        file_count=len(packed_files),
        original_tokens=total_original_tokens,
        packed_tokens=final_tokens,
        saved_tokens=saved,
        reduction_pct=pct,
        files=packed_files,
        bundle_text=bundle_text,
    )


def find_first_degree_dependencies(target_files: list[Path], root: Path) -> list[Path]:
    """Find 1st-degree imports and callers of target files within the project root."""
    dependencies: list[Path] = []
    seen: set[Path] = set(target_files)

    for file_path in target_files:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # Check Python imports
        if file_path.suffix in (".py", ".pyi"):
            try:
                tree = ast.parse(content, filename=str(file_path))
                for node in ast.walk(tree):
                    module_name = None
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            module_name = alias.name
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        module_name = node.module

                    if module_name:
                        rel_parts = module_name.split(".")
                        candidates = [
                            root / f"{'/'.join(rel_parts)}.py",
                            root / f"{'/'.join(rel_parts)}/__init__.py",
                            root / "src" / f"{'/'.join(rel_parts)}.py",
                            root / "src" / f"{'/'.join(rel_parts)}/__init__.py",
                            file_path.parent / f"{'/'.join(rel_parts)}.py",
                        ]
                        for cand in candidates:
                            cand_res = cand.resolve()
                            if (
                                cand_res.is_file()
                                and allowed_file(cand_res, root)
                                and cand_res not in seen
                            ):
                                seen.add(cand_res)
                                dependencies.append(cand_res)
                                break
            except SyntaxError:
                pass

        # Regex for generic relative imports in JS/TS/Go/Rust/C
        for match in re.finditer(
            r"""(?:import|from|require|#include)\s+['"<]([./\w_-]+)['">]""", content
        ):
            imp_path = match.group(1)
            if imp_path.startswith("."):
                base_dir = file_path.parent
                for ext in ("", ".ts", ".js", ".tsx", ".jsx", ".h", ".hpp", ".c", ".cpp"):
                    cand = (base_dir / (imp_path + ext)).resolve()
                    if cand.is_file() and allowed_file(cand, root) and cand not in seen:
                        seen.add(cand)
                        dependencies.append(cand)
                        break

    # Reverse dependencies: callers of symbols defined in target files
    try:
        index = CodeIndex(root)
        with index.connect() as db:
            for file_path in target_files:
                try:
                    rel_str = str(file_path.relative_to(root))
                except ValueError:
                    continue
                rows = db.execute("SELECT name FROM symbols WHERE path = ?", (rel_str,)).fetchall()
                for (sym_name,) in rows:
                    if len(sym_name) < 4 or sym_name.startswith("__"):
                        continue
                    occ_rows = db.execute(
                        "SELECT DISTINCT path FROM occurrences WHERE name = ? LIMIT 10",
                        (sym_name,),
                    ).fetchall()
                    for (occ_path,) in occ_rows:
                        cand = (root / occ_path).resolve()
                        if cand.is_file() and allowed_file(cand, root) and cand not in seen:
                            seen.add(cand)
                            dependencies.append(cand)
    except Exception:
        pass

    return dependencies


def slice_repository_context(
    git_range: str | None = None,
    files: list[str | Path] | None = None,
    root: Path | None = None,
    budget: int = 8000,
    format_type: str = "markdown",
) -> PackResult:
    """Intelligently slice repository context for targeted PR review or feature work.

    Packs modified files in full, skeletonizes 1st-degree dependencies, and strictly
    enforces the token budget ceiling.
    """
    if root is None:
        root = Path.cwd()
    root = root.resolve()

    modified_files: list[Path] = []
    if files:
        for p in files:
            resolved = (root / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            if resolved.is_file() and allowed_file(resolved, root):
                modified_files.append(resolved)
    elif git_range:
        res = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", git_range],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if not line.strip():
                    continue
                cand = (root / line.strip()).resolve()
                if cand.is_file() and allowed_file(cand, root):
                    modified_files.append(cand)
        else:
            raise ValueError(f"Git diff failed for range {git_range}: {res.stderr.strip()}")
    else:
        # Fall back to uncommitted changes or last commit
        res = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            for line in res.stdout.splitlines():
                cand = (root / line.strip()).resolve()
                if cand.is_file() and allowed_file(cand, root):
                    modified_files.append(cand)
        else:
            modified_files = source_files(root)[:5]

    # Deduplicate modified files preserving order
    seen_mod: set[Path] = set()
    unique_modified: list[Path] = []
    for f in modified_files:
        if f not in seen_mod:
            seen_mod.add(f)
            unique_modified.append(f)

    # Discover 1st-degree dependencies
    dependencies = find_first_degree_dependencies(unique_modified, root)

    cache = ContextCache()
    packed_files: list[PackedFile] = []
    total_original_tokens = 0
    running_tokens = 0

    # Pack modified files in full
    for path in unique_modified:
        rel_str = str(path.relative_to(root))
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        cleaned = redact_secrets(raw)
        orig_tok = count_tokens(cleaned).openai
        total_original_tokens += orig_tok
        p_tok = orig_tok
        running_tokens += p_tok
        packed_files.append(
            PackedFile(
                path=rel_str,
                original_tokens=orig_tok,
                packed_tokens=p_tok,
                is_skeleton=False,
                ref_id=None,
                content=cleaned,
                file_type="modified",
            )
        )

    # Pack 1st-degree dependencies as skeletons
    for path in dependencies:
        rel_str = str(path.relative_to(root))
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        cleaned = redact_secrets(raw)
        orig_tok = count_tokens(cleaned).openai
        total_original_tokens += orig_tok
        try:
            skel = extract_symbol_or_range(path, skeleton=True)
            ref_id = cache.store(cleaned, source=f"slice:{rel_str}")
            trunc_text = (
                f"\n# [Full implementation omitted; {orig_tok} tokens. "
                f"Recover: usagetrim retrieve {ref_id}]\n"
            )
            packed_content = skel + trunc_text
            p_tok = count_tokens(packed_content).openai
        except Exception:
            packed_content = cleaned
            p_tok = orig_tok
            ref_id = None

        running_tokens += p_tok
        packed_files.append(
            PackedFile(
                path=rel_str,
                original_tokens=orig_tok,
                packed_tokens=p_tok,
                is_skeleton=True,
                ref_id=ref_id,
                content=packed_content,
                file_type="dependency",
            )
        )

    def _render_bundle() -> str:
        if format_type.lower() == "xml":
            xml_lines = [
                f'<repository_slice root="{root.name}" budget="{budget}" '
                f'tokens="{sum(f.packed_tokens for f in packed_files)}" file_count="{len(packed_files)}">',
            ]
            mod_files = [f for f in packed_files if f.file_type == "modified"]
            dep_files = [f for f in packed_files if f.file_type == "dependency"]

            xml_lines.append(f'  <modified_files count="{len(mod_files)}">')
            for pf in mod_files:
                xml_lines.append(
                    f'    <file path="{pf.path}" tokens="{pf.packed_tokens}" '
                    f'skeleton="{str(pf.is_skeleton).lower()}">'
                )
                xml_lines.append(f"<![CDATA[\n{pf.content.strip()}\n]]>")
                xml_lines.append("    </file>")
            xml_lines.append("  </modified_files>")

            if dep_files:
                xml_lines.append(f'  <dependencies count="{len(dep_files)}">')
                for pf in dep_files:
                    xml_lines.append(
                        f'    <file path="{pf.path}" tokens="{pf.packed_tokens}" '
                        f'skeleton="{str(pf.is_skeleton).lower()}">'
                    )
                    xml_lines.append(f"<![CDATA[\n{pf.content.strip()}\n]]>")
                    xml_lines.append("    </file>")
                xml_lines.append("  </dependencies>")
            xml_lines.append("</repository_slice>")
            return "\n".join(xml_lines)
        else:
            header = [
                f"# UsageTrim Repository Slice Context ({root.name})",
                f"Files: {len(packed_files)} · Estimated tokens: "
                f"~{sum(f.packed_tokens for f in packed_files)} (Budget: {budget})",
            ]
            if git_range:
                header.append(f"Git Range: `{git_range}`")
            header.extend(
                [
                    "",
                    "## Slice Summary",
                    "```text",
                ]
            )
            for pf in packed_files:
                type_label = (
                    "Modified (Full)" if pf.file_type == "modified" else "1st-Degree Dep (Skeleton)"
                )
                header.append(f"{pf.path:<40} {pf.packed_tokens:>6} tok  [{type_label}]")
            header.append("```\n")

            sections = ["\n".join(header)]
            mod_files = [f for f in packed_files if f.file_type == "modified"]
            dep_files = [f for f in packed_files if f.file_type == "dependency"]

            if mod_files:
                sections.append("## Modified Files (Full Context)\n")
                for pf in mod_files:
                    lang = Path(pf.path).suffix.lstrip(".") or "text"
                    sections.append(f"### {pf.path}\n```{lang}\n{pf.content.strip()}\n```\n")

            if dep_files:
                sections.append("## 1st-Degree Dependencies (AST Skeletons)\n")
                for pf in dep_files:
                    lang = Path(pf.path).suffix.lstrip(".") or "text"
                    sections.append(
                        f"### {pf.path} (AST Skeleton)\n```{lang}\n{pf.content.strip()}\n```\n"
                    )

            return "\n".join(sections)

    bundle_text = _render_bundle()
    final_tokens = count_tokens(bundle_text).openai

    # Adaptive loop: strictly enforce token budget ceiling
    while final_tokens > budget and packed_files:
        dep_files = [f for f in packed_files if f.file_type == "dependency"]
        if dep_files:
            largest_dep = max(dep_files, key=lambda f: f.packed_tokens)
            lines = largest_dep.content.splitlines(keepends=True)
            if len(lines) > 4:
                keep = max(2, len(lines) // 2)
                trunc_note = f"\n# [... {len(lines) - keep} lines omitted ...]\n"
                largest_dep.content = "".join(lines[:keep]) + trunc_note
                largest_dep.packed_tokens = count_tokens(largest_dep.content).openai
            else:
                packed_files.remove(largest_dep)
        else:
            largest_mod = max(packed_files, key=lambda f: f.packed_tokens)
            lines = largest_mod.content.splitlines(keepends=True)
            if len(lines) <= 2:
                break
            keep = max(1, len(lines) // 2)
            if not largest_mod.ref_id:
                try:
                    orig = redact_secrets((root / largest_mod.path).read_text(errors="replace"))
                    largest_mod.ref_id = cache.store(orig, source=f"slice:{largest_mod.path}")
                except Exception:
                    pass
            trunc_note = (
                f"\n# [... {len(lines) - keep} lines omitted. Ref: {largest_mod.ref_id} ...]\n"
            )
            largest_mod.content = "".join(lines[:keep]) + trunc_note
            largest_mod.packed_tokens = count_tokens(largest_mod.content).openai

        bundle_text = _render_bundle()
        final_tokens = count_tokens(bundle_text).openai

    final_tokens = count_tokens(bundle_text).openai
    saved = max(0, total_original_tokens - final_tokens)
    pct = round((saved / total_original_tokens) * 100, 1) if total_original_tokens else 0.0

    return PackResult(
        root=root,
        file_count=len(packed_files),
        original_tokens=total_original_tokens,
        packed_tokens=final_tokens,
        saved_tokens=saved,
        reduction_pct=pct,
        files=packed_files,
        bundle_text=bundle_text,
    )
