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
