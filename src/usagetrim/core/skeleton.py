from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any


def skeletonize_python(source_code: str) -> str:
    """Extract signatures, classes, methods, docstrings and replace bodies with `...` using Python AST."""
    try:
        tree = ast.parse(source_code)
    except Exception:
        # Fallback to regex if syntax error (e.g. invalid code snippet)
        return _skeletonize_by_regex(source_code)

    lines: list[str] = []

    # Module docstring
    docstring = ast.get_docstring(tree)
    if docstring:
        lines.append(f'"""{docstring}"""\n')

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # Keep imports
            try:
                lines.append(ast.unparse(node))
            except Exception:
                pass
        elif isinstance(node, ast.ClassDef):
            lines.append("")
            # Decorators
            for dec in node.decorator_list:
                try:
                    lines.append(f"@{ast.unparse(dec)}")
                except Exception:
                    pass
            # Class header
            bases = [ast.unparse(b) for b in node.bases]
            bases_str = f"({', '.join(bases)})" if bases else ""
            lines.append(f"class {node.name}{bases_str}:")

            class_doc = ast.get_docstring(node)
            if class_doc:
                lines.append(f'    """{class_doc}"""')

            has_methods = False
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    has_methods = True
                    for dec in item.decorator_list:
                        try:
                            lines.append(f"    @{ast.unparse(dec)}")
                        except Exception:
                            pass
                    fn_prefix = "async def " if isinstance(item, ast.AsyncFunctionDef) else "def "
                    try:
                        args_str = ast.unparse(item.args)
                    except Exception:
                        args_str = "..."
                    ret_str = f" -> {ast.unparse(item.returns)}" if item.returns else ""
                    lines.append(f"    {fn_prefix}{item.name}({args_str}){ret_str}:")
                    fn_doc = ast.get_docstring(item)
                    if fn_doc:
                        lines.append(f'        """{fn_doc}"""')
                    lines.append("        ...")
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    # Class field annotation
                    val = f" = {ast.unparse(item.value)}" if item.value else ""
                    lines.append(f"    {item.target.id}: {ast.unparse(item.annotation)}{val}")

            if not has_methods and not class_doc:
                lines.append("    ...")

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append("")
            for dec in node.decorator_list:
                try:
                    lines.append(f"@{ast.unparse(dec)}")
                except Exception:
                    pass
            fn_prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
            try:
                args_str = ast.unparse(node.args)
            except Exception:
                args_str = "..."
            ret_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            lines.append(f"{fn_prefix}{node.name}({args_str}){ret_str}:")
            fn_doc = ast.get_docstring(node)
            if fn_doc:
                lines.append(f'    """{fn_doc}"""')
            lines.append("    ...")

        elif isinstance(node, ast.Assign):
            # Top level constants (UPPERCASE)
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    lines.append(f"{target.id} = {ast.unparse(node.value)}")

    return "\n".join(lines)


def _skeletonize_by_regex(source: str) -> str:
    """Fast regex-based skeletonizer for TypeScript, JavaScript, Go, Rust, and unparseable Python."""
    lines: list[str] = []
    in_block = False
    brace_depth = 0

    # Signature patterns (TS/JS/Go/Rust/Python)
    sig_patterns = [
        re.compile(
            r"^\s*(?:export\s+)?(?:async\s+)?(?:function|def|class|interface|type|enum|struct)\b"
        ),
        re.compile(r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait|impl)\b"),
        re.compile(r"^\s*func\s+(?:\([^)]+\)\s+)?[A-Za-z0-9_]+\s*\("),
        re.compile(r"^\s*(?:public|private|protected|static|readonly|override|\bget\b|\bset\b)\s+"),
        re.compile(r"^\s*(?:import|from|package|use|#include)\b"),
    ]

    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if any(p.search(line) for p in sig_patterns):
            lines.append(line)
            if "{" in line and "}" not in line:
                in_block = True
                brace_depth = line.count("{") - line.count("}")
                lines.append("  ...")
            continue

        if in_block:
            brace_depth += line.count("{") - line.count("}")
            if brace_depth <= 0:
                in_block = False
                lines.append(line)
        elif stripped.startswith(("//", "/*", "*", "#")):
            # Doc comments
            if "TODO" in stripped or "NOTE" in stripped or "@" in stripped:
                lines.append(line)


def skeletonize_code_ast(source: str, suffix: str) -> str:
    """Extract AST-accurate code skeleton for TypeScript, JavaScript, Go, Rust, Java, and C/C++ using ast-grep."""
    lang_map = {
        ".ts": "typescript",
        ".tsx": "tsx",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".hpp": "cpp",
    }
    lang = lang_map.get(suffix.lower())
    if not lang:
        return _skeletonize_by_regex(source)

    try:
        from ast_grep_py import SgRoot

        root = SgRoot(source, lang)
        block_kind = (
            "compound_statement"
            if lang in {"c", "cpp"}
            else "statement_block"
            if lang in {"typescript", "tsx", "javascript"}
            else "block"
        )
        blocks = root.root().find_all(kind=block_kind)

        fn_parent_kinds = {
            "function_declaration",
            "function_definition",
            "method_definition",
            "method_declaration",
            "function_item",
            "constructor_declaration",
            "arrow_function",
            "function_expression",
        }

        edits = []
        for b in blocks:
            parent = b.parent()
            if parent and parent.kind() in fn_parent_kinds:
                edits.append(b.replace("{\n    ...\n  }"))

        if edits:
            result = root.root().commit_edits(edits)
            return result.strip()
    except Exception:
        pass

    return _skeletonize_by_regex(source)


def skeletonize_json(json_str: str, max_array_items: int = 2) -> str:
    """Minify and collapse large repetitive arrays in JSON."""
    try:
        data = json.loads(json_str)
    except Exception:
        return json_str

    def prune(obj: Any) -> Any:
        if isinstance(obj, list):
            if len(obj) <= max_array_items:
                return [prune(x) for x in obj]
            trimmed = [prune(x) for x in obj[:max_array_items]]
            trimmed.append(
                f"[... {len(obj) - max_array_items} more items omitted by usagetrim ...]"
            )
            return trimmed
        elif isinstance(obj, dict):
            return {k: prune(v) for k, v in obj.items()}
        return obj

    pruned = prune(data)
    return json.dumps(pruned, indent=2)


_IMAGE_MIME_PREFIXES = ("image/",)
_BINARY_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/octet-stream",
    }
)
_OUTPUT_TEXT_KEEP_LINES = 3


def _notebook_join_text(value: Any) -> str:
    """Normalize notebook source/text fields (string or list of lines) to one string."""
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    if value is None:
        return ""
    return str(value)


def _notebook_as_source_lines(text: str) -> list[str]:
    """Split text into nbformat-style source lines (newline retained except last)."""
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    if text and not text.endswith("\n") and lines:
        # splitlines(keepends=True) already drops a trailing bare line's newline correctly
        pass
    return lines


def _is_base64_blob(value: str) -> bool:
    """Heuristic: long contiguous base64-looking payload (typical embedded chart)."""
    if len(value) < 200:
        return False
    sample = value[:80].replace("\n", "").replace("\r", "")
    if not sample:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    return all(ch in allowed for ch in sample)


def _fold_output_text(text: str, keep_lines: int = _OUTPUT_TEXT_KEEP_LINES) -> list[str]:
    """Keep the first few output lines; fold the rest with an explicit marker."""
    lines = text.splitlines()
    if len(lines) <= keep_lines:
        return _notebook_as_source_lines(text if text.endswith("\n") or not text else text)
    kept = lines[:keep_lines]
    omitted = len(lines) - keep_lines
    folded = "\n".join(kept) + f"\n[... {omitted} output lines folded ...]\n"
    return _notebook_as_source_lines(folded)


def _slim_notebook_output(output: dict[str, Any]) -> dict[str, Any] | None:
    """Strip binary/base64 payloads and truncate verbose text from one cell output."""
    if not isinstance(output, dict):
        return None

    slimmed = dict(output)
    output_type = slimmed.get("output_type")

    if output_type == "stream":
        text = _notebook_join_text(slimmed.get("text"))
        slimmed["text"] = _fold_output_text(text)
        return slimmed

    if output_type in {"execute_result", "display_data"}:
        data = slimmed.get("data")
        if isinstance(data, dict):
            new_data: dict[str, Any] = {}
            for mime, payload in data.items():
                mime_l = str(mime).lower()
                joined = _notebook_join_text(payload)
                if mime_l.startswith(_IMAGE_MIME_PREFIXES) or mime_l in _BINARY_MIME_TYPES:
                    new_data[mime] = [f"[{mime} stripped by usagetrim]\n"]
                elif _is_base64_blob(joined):
                    new_data[mime] = [f"[{mime} base64 blob stripped by usagetrim]\n"]
                elif mime_l in {"text/plain", "text/markdown", "text/html", "application/json"}:
                    if mime_l == "application/json" and not isinstance(payload, str):
                        # Keep small structured JSON; fold oversized string dumps only.
                        dumped = json.dumps(payload, ensure_ascii=False)
                        if len(dumped) > 400:
                            new_data[mime] = _fold_output_text(dumped)
                        else:
                            new_data[mime] = payload
                    else:
                        new_data[mime] = _fold_output_text(joined)
                else:
                    if _is_base64_blob(joined) or len(joined) > 2000:
                        new_data[mime] = [f"[{mime} payload stripped by usagetrim]\n"]
                    else:
                        new_data[mime] = payload
            slimmed["data"] = new_data
        # Drop bulky output-level metadata (e.g. image size hints).
        slimmed["metadata"] = {}
        return slimmed

    if output_type == "error":
        traceback = slimmed.get("traceback")
        if isinstance(traceback, list) and len(traceback) > 8:
            omitted = len(traceback) - 6
            slimmed["traceback"] = (
                traceback[:3] + [f"[... {omitted} traceback lines folded ...]"] + traceback[-3:]
            )
        return slimmed

    # Unknown output types: drop binary-looking fields, keep the rest small.
    return slimmed


def _slim_cell_metadata(metadata: Any) -> dict[str, Any]:
    """Drop noisy cell metadata (widgets, scrolled flags) while keeping tags."""
    if not isinstance(metadata, dict):
        return {}
    keep_keys = {"tags"}
    return {k: v for k, v in metadata.items() if k in keep_keys}


def skeletonize_notebook(raw_ipynb: str, *, cache_full: bool = True) -> str:
    """Strip notebook cell outputs and base64 images while preserving cell source.

    Returns valid notebook JSON. Large binary / tabular outputs are removed or folded;
    the original notebook is stored in the CCR cache when compression is meaningful.
    """
    trimmed = raw_ipynb.strip()
    if not trimmed:
        return raw_ipynb

    try:
        notebook = json.loads(trimmed)
    except (json.JSONDecodeError, ValueError, TypeError):
        return raw_ipynb

    if not isinstance(notebook, dict) or "cells" not in notebook:
        return raw_ipynb

    cells_in = notebook.get("cells")
    if not isinstance(cells_in, list):
        return raw_ipynb

    slim_cells: list[dict[str, Any]] = []
    for cell in cells_in:
        if not isinstance(cell, dict):
            continue
        new_cell = dict(cell)
        # Preserve source verbatim (string or list-of-lines).
        if "source" in new_cell:
            new_cell["source"] = cell["source"]
        new_cell["metadata"] = _slim_cell_metadata(cell.get("metadata"))
        if new_cell.get("cell_type") == "code":
            new_cell["execution_count"] = None
            outputs = cell.get("outputs")
            if isinstance(outputs, list):
                slim_outputs = []
                for out in outputs:
                    slimmed = _slim_notebook_output(out) if isinstance(out, dict) else None
                    if slimmed is not None:
                        slim_outputs.append(slimmed)
                new_cell["outputs"] = slim_outputs
            else:
                new_cell["outputs"] = []
        slim_cells.append(new_cell)

    result = dict(notebook)
    result["cells"] = slim_cells

    # Notebook-level widget state is a common token hog unrelated to source.
    meta = dict(result.get("metadata") or {}) if isinstance(result.get("metadata"), dict) else {}
    meta.pop("widgets", None)
    result["metadata"] = meta

    result_str = json.dumps(result, ensure_ascii=False, indent=1)

    if cache_full and len(raw_ipynb) > len(result_str) * 1.3:
        from usagetrim.core.cache import ContextCache

        ref_id = ContextCache().store(raw_ipynb, source="notebook")
        meta = dict(result.get("metadata") or {})
        meta["usagetrim"] = {
            "ref": ref_id,
            "note": (
                f"raw notebook ({len(raw_ipynb):,} bytes) compacted; recover via usagetrim retrieve"
            ),
        }
        result["metadata"] = meta
        result_str = json.dumps(result, ensure_ascii=False, indent=1)

    return result_str


def strip_comments_and_blanks(content: str, suffix: str = ".py") -> str:
    """Semantically fold license/dead-comment trivia, then strip remaining comments.

    Executable code is left unchanged. License banners and large commented-out
    blocks become one-line notices; descriptive docstrings are retained.
    """
    from usagetrim.core.cleaner import CodeCleanerOptions, clean_source_code

    return clean_source_code(
        content,
        suffix=suffix,
        options=CodeCleanerOptions(strip_remaining_comments=True),
    )


def extract_symbol_or_range(
    file_path: str | Path,
    symbol: str | None = None,
    lines_range: str | None = None,
    skeleton: bool = False,
    strip_comments: bool = False,
) -> str:
    """Read a file with targeted extraction (symbol, line range, or full skeleton)."""
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    from usagetrim.core.lockfile import is_lockfile, summarize_lockfile

    content = p.read_text(encoding="utf-8", errors="replace")

    if is_lockfile(p.name):
        if symbol and not lines_range:
            return summarize_lockfile(content, p.name, query_package=symbol)
        if skeleton:
            return summarize_lockfile(content, p.name)

    if symbol and not lines_range:
        from usagetrim.core.code_index import read_symbol

        return read_symbol(p, symbol)

    if lines_range:
        # e.g. "10-50" or "42"
        all_lines = content.splitlines()
        if "-" in lines_range:
            start_s, end_s = lines_range.split("-", 1)
            start = max(1, int(start_s))
            end = min(len(all_lines), int(end_s))
        else:
            start = max(1, int(lines_range))
            end = start
        selected = all_lines[start - 1 : end]
        header = f"# [{p.name} lines {start}-{end} of {len(all_lines)}]\n"
        return header + "\n".join(selected)

    # Notebooks are always stripped on full reads: outputs/base64 dominate tokens.
    if p.suffix.lower() == ".ipynb":
        return skeletonize_notebook(content)

    if skeleton:
        if p.suffix == ".py":
            return skeletonize_python(content)
        elif p.suffix in {".ts", ".js", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp"}:
            return skeletonize_code_ast(content, p.suffix)
        elif p.suffix in {".json", ".yaml", ".yml"}:
            return skeletonize_json(content)

    if strip_comments:
        return strip_comments_and_blanks(content, suffix=p.suffix)

    return content
