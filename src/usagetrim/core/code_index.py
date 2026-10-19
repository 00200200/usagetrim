"""Local, incremental syntax index. Occurrences are not LSP-resolved references."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from ast_grep_py import SgRoot

from usagetrim.core.cache import DEFAULT_CACHE_DIR
from usagetrim.core.redactor import redact_secrets

LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".rs": "rust",
    ".go": "go",
    ".swift": "swift",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
}
TEXT_EXTENSIONS = {".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".sql", ".sh"}
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".build",
    "build",
    "dist",
    "vendor",
    ".next",
    ".cache",
    ".usagetrim",
}
DECLARATIONS = {
    "function_definition",
    "class_definition",
    "function_declaration",
    "class_declaration",
    "interface_declaration",
    "method_definition",
    "method_declaration",
    "function_item",
    "struct_item",
    "enum_item",
    "trait_item",
    "type_alias_declaration",
    "type_spec",
    "struct_declaration",
    "enum_declaration",
    "protocol_declaration",
    "method_signature",
}
IDENTIFIERS = {
    "identifier",
    "type_identifier",
    "field_identifier",
    "property_identifier",
    "simple_identifier",
    "namespace_identifier",
}
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 10000


def _language_family(language: str) -> str:
    """Group compatible syntax languages without resolving imports or names."""
    return "javascript-typescript" if language in {"javascript", "typescript", "tsx"} else language


def _build_file_graph(
    db: sqlite3.Connection,
) -> tuple[dict[str, set[str]], dict[tuple[str, str, int], int]]:
    """Build syntactic file edges and conservative per-definition support.

    An edge requires one external target file, even if that file has multiple
    matching definitions. Symbol support requires one definition in the entire
    language family. Repeated occurrences in one source file count only once.
    """
    families = {
        path: _language_family(language)
        for path, language in db.execute(
            "SELECT path,language FROM files WHERE language IS NOT NULL AND issue IS NULL ORDER BY path"
        )
    }
    graph = {path: set() for path in families}
    definitions = defaultdict(list)
    supporters = {}
    for path, name, qualified, line in db.execute(
        "SELECT path,name,qualified,line FROM symbols ORDER BY path,line,qualified"
    ):
        if path in families:
            symbol = (path, qualified, line)
            definitions[families[path], name].append(symbol)
            supporters[symbol] = set()
    for path, name in db.execute("SELECT DISTINCT path,name FROM occurrences ORDER BY path,name"):
        if path not in families:
            continue
        matches = definitions.get((families[path], name), ())
        # Declarations and local uses cannot establish an external reference.
        if any(symbol[0] == path for symbol in matches):
            continue
        targets = {symbol[0] for symbol in matches if symbol[0] != path}
        if len(targets) == 1:
            graph[path].update(targets)
        if len(matches) == 1 and matches[0][0] != path:
            supporters[matches[0]].add(path)
    return graph, {symbol: len(paths) for symbol, paths in supporters.items()}


def _calculate_pagerank(
    graph: Mapping[str, Collection[str]],
    damping: float = 0.85,
    iterations: int = 20,
) -> dict[str, float]:
    """Run deterministic fixed-iteration PageRank with uniform dangling mass.

    Target-only nodes are treated as dangling nodes. Duplicate neighbors do not
    create extra votes; sorted traversal fixes floating-point accumulation order.
    """
    if type(damping) not in (int, float) or not 0 <= damping <= 1:
        raise ValueError("damping must be a number between 0 and 1")
    if type(iterations) is not int or iterations <= 0:
        raise ValueError("iterations must be a positive integer")
    nodes = sorted(set(graph) | {target for targets in graph.values() for target in targets})
    if not nodes:
        return {}
    neighbors = {node: sorted(set(graph.get(node, ()))) for node in nodes}
    size = len(nodes)
    scores = dict.fromkeys(nodes, 1 / size)
    for _ in range(iterations):
        dangling = sum(scores[node] for node in nodes if not neighbors[node])
        base = (1 - damping) / size + damping * dangling / size
        updated = dict.fromkeys(nodes, base)
        for node in nodes:
            if neighbors[node]:
                share = damping * scores[node] / len(neighbors[node])
                for target in neighbors[node]:
                    updated[target] += share
        scores = updated
    return scores


@dataclass
class Symbol:
    name: str
    qualified: str
    kind: str
    start: int
    end: int
    line: int
    end_line: int
    signature: str


def digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def parse_source(source: str, language: str) -> tuple[list[Symbol], list[tuple]]:
    if language == "python":
        ast.parse(source)  # Reject malformed Python instead of guessing a region.
    root = SgRoot(source, language).root()
    if root.find(kind="ERROR"):
        raise ValueError("Syntax errors; use a line read or text search instead")
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    symbols = []
    occurrences = []

    def visit(node, scope=()):
        kind = node.kind()
        name_node = node.field("name")
        if name_node is None and language in {"c", "cpp"} and kind == "function_definition":
            name_node = node.field("declarator")
            while name_node is not None and name_node.field("declarator") is not None:
                name_node = name_node.field("declarator")
        is_declaration = kind in DECLARATIONS
        if kind == "variable_declarator":
            value = node.field("value")
            is_declaration = value is not None and value.kind() in {
                "arrow_function",
                "function_expression",
            }
        child_scope = scope
        if is_declaration and name_node is not None:
            name = name_node.text()
            if language == "go" and kind == "method_declaration":
                receiver = node.field("receiver")
                if receiver:
                    types = receiver.find_all(kind="type_identifier")
                    if types:
                        scope = (*scope, types[-1].text())
            qualified = ".".join((*scope, name))
            span = node
            parent = node.parent()
            if parent and parent.kind() in {"decorated_definition", "export_statement"}:
                span = parent
            rng = span.range()
            start = rng.start.index
            # Include indentation when the declaration starts a line, not a
            # preceding inline declaration such as `class A { method() ... }`.
            line_start = offsets[rng.start.line]
            if not source[line_start:start].strip():
                start = line_start
            body = node.field("body")
            signature_end = body.range().start.index if body else node.range().end.index
            signature = " ".join(source[node.range().start.index : signature_end].split())[:240]
            symbols.append(
                Symbol(
                    name,
                    qualified,
                    kind,
                    start,
                    rng.end.index,
                    rng.start.line + 1,
                    rng.end.line + 1,
                    signature,
                )
            )
            child_scope = (*scope, name)
        elif kind == "impl_item":
            impl_type = node.field("type")
            if impl_type:
                child_scope = (*scope, impl_type.text())
        if node.is_named_leaf() and kind in IDENTIFIERS:
            rng = node.range()
            occurrences.append(
                (
                    node.text(),
                    rng.start.line + 1,
                    rng.start.column,
                    lines[rng.start.line].rstrip("\r\n")[:400],
                )
            )
        for child in node.children():
            if child.is_named():
                visit(child, child_scope)

    visit(root)
    return symbols, occurrences


def select_symbol(source: str, path: Path, selector: str) -> Symbol:
    language = LANGUAGES.get(path.suffix.lower())
    if not language:
        raise ValueError(f"Unsupported symbol language: {path.suffix}")
    symbols, _ = parse_source(source, language)
    name, separator, line_text = selector.rpartition("@")
    line = int(line_text) if separator else None
    query = name if separator else selector
    matches = [
        symbol
        for symbol in symbols
        if (symbol.qualified == query or ("." not in query and symbol.name == query))
        and (line is None or symbol.line == line)
    ]
    if not matches:
        raise ValueError(f"Symbol {selector!r} not found")
    if len(matches) != 1:
        choices = ", ".join(f"{symbol.qualified}@{symbol.line}" for symbol in matches[:12])
        raise ValueError(f"Ambiguous symbol; choose one of: {choices}")
    return matches[0]


def read_symbol(path: Path, selector: str) -> str:
    source = path.read_bytes().decode("utf-8")
    symbol = select_symbol(source, path, selector)
    return (
        f"# {path.name}:{symbol.line}-{symbol.end_line} {symbol.qualified} sha256={digest(source)}\n"
        + source[symbol.start : symbol.end]
    )


def terms(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > 512:
        raise ValueError("query must contain 1–512 characters")
    words = list(dict.fromkeys(re.findall(r"\w+", query, flags=re.UNICODE)))[:16]
    if not words:
        raise ValueError("query must contain a searchable word")
    return " OR ".join('"' + word + '"' for word in words)


def allowed_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in SKIP_DIRS for part in relative.parts):
        return False
    if path.name.startswith(".env") or path.suffix.lower() in {".pem", ".key", ".p12"}:
        return False
    if path.suffix.lower() not in LANGUAGES and path.suffix.lower() not in TEXT_EXTENSIONS:
        return False
    if any(
        parent.is_symlink()
        for parent in (path, *path.parents)
        if parent != root and root in parent.parents
    ):
        return False
    return path.is_file() and path.resolve().is_relative_to(root)


def source_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Git file listing timed out; choose a narrower root") from exc
    except OSError:
        result = None
    if result is not None and result.returncode == 0:
        paths = {root / os.fsdecode(name) for name in result.stdout.split(b"\0") if name}
        return sorted(path for path in paths if allowed_file(path, root))
    if result is not None and b"not a git repository" not in result.stderr:
        raise ValueError("Git file listing failed; index was not updated")
    # Unversioned folders: explicit root, bounded traversal, no symlink following.
    paths = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            path = Path(directory) / name
            if allowed_file(path, root):
                paths.append(path)
        if len(paths) > MAX_FILES:
            raise ValueError("Project exceeds 10000 files; choose a narrower root")
    return paths


class CodeIndex:
    def __init__(self, root: str | Path):
        root = Path(root)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("root must be an absolute project directory")
        self.root = root.resolve()
        folder = Path(os.environ.get("USAGETRIM_CACHE_DIR", str(DEFAULT_CACHE_DIR))) / "code-index"
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / (hashlib.sha256(str(self.root).encode()).hexdigest()[:24] + ".db")
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, stamp TEXT, sha TEXT, language TEXT, issue TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS symbols (path TEXT, name TEXT, qualified TEXT, kind TEXT, start INT, end INT, line INT, end_line INT, signature TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS occurrences (path TEXT, name TEXT, line INT, col INT, context TEXT)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS occurrence_name ON occurrences(name)")
            db.execute("CREATE INDEX IF NOT EXISTS symbol_scope ON symbols(path, line, end_line)")
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, first UNINDEXED, body, tokenize='unicode61')"
            )
        self.updated = 0
        self.issues = []

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def sync(self):
        paths = source_files(self.root)
        if len(paths) > MAX_FILES:
            raise ValueError("Project exceeds 10000 files; choose a narrower root")
        self.updated = 0
        self.issues = []
        with self.connect() as db:
            current = {row[0]: row[1] for row in db.execute("SELECT path, stamp FROM files")}
            seen = set()
            for path in paths:
                relative = str(path.relative_to(self.root))
                seen.add(relative)
                stat = path.stat()
                stamp = json.dumps([stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino])
                if current.get(relative) == stamp:
                    continue
                for table in ("files", "symbols", "occurrences", "chunks"):
                    db.execute(f"DELETE FROM {table} WHERE path=?", (relative,))
                language = LANGUAGES.get(path.suffix.lower())
                issue = None
                source = ""
                symbols, occurrences = [], []
                try:
                    if stat.st_size > MAX_FILE_BYTES:
                        raise ValueError("File exceeds 2 MiB; use a targeted read")
                    source = path.read_bytes().decode("utf-8")
                    if len(source.encode()) > MAX_FILE_BYTES or "\0" in source:
                        raise ValueError("Binary or oversized file")
                    if language:
                        symbols, occurrences = parse_source(source, language)
                except (ValueError, SyntaxError, OSError, RecursionError) as exc:
                    # Include no source excerpts in metadata error messages.
                    issue = type(exc).__name__ + ": no reliable symbols"
                db.execute(
                    "INSERT INTO files VALUES (?,?,?,?,?)",
                    (relative, stamp, digest(source), language, issue),
                )
                db.executemany(
                    "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            relative,
                            symbol.name,
                            symbol.qualified,
                            symbol.kind,
                            symbol.start,
                            symbol.end,
                            symbol.line,
                            symbol.end_line,
                            redact_secrets(symbol.signature),
                        )
                        for symbol in symbols
                    ],
                )
                db.executemany(
                    "INSERT INTO occurrences VALUES (?,?,?,?,?)",
                    [
                        (relative, name, line, col, redact_secrets(context))
                        for name, line, col, context in occurrences
                    ],
                )
                lines = redact_secrets(source).splitlines()
                db.executemany(
                    "INSERT INTO chunks(path,first,body) VALUES (?,?,?)",
                    [
                        (relative, offset + 1, "\n".join(lines[offset : offset + 40]))
                        for offset in range(0, len(lines), 40)
                    ],
                )
                self.updated += 1
            for stale in set(current) - seen:
                for table in ("files", "symbols", "occurrences", "chunks"):
                    db.execute(f"DELETE FROM {table} WHERE path=?", (stale,))
            self.issues = [
                f"{path}: {issue}"
                for path, issue in db.execute(
                    "SELECT path,issue FROM files WHERE issue IS NOT NULL"
                )
            ]
            return len(seen)

    def query(self, mode="map", query="", file=None, limit=30) -> str:
        if mode not in {
            "map",
            "symbols",
            "occurrences",
            "search",
            "pattern",
            "outline",
            "references",
            "callers",
        }:
            raise ValueError(
                "mode must be map, symbols, occurrences, search, pattern, outline, references, or callers"
            )
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be 1–200")
        if file is not None:
            target = (self.root / file).resolve()
            if not target.is_relative_to(self.root):
                raise ValueError("file must remain inside root")
            file = str(target.relative_to(self.root))
        if mode in {"occurrences", "search", "pattern"}:
            terms(query)  # Validate before scanning or parsing patterns.
        total = self.sync()
        header = f"# {mode}: {total} files, {self.updated} reindexed; syntax index (not LSP)\n"
        with self.connect() as db:
            if mode == "outline":
                target_file = file or (query if query and not query.startswith("-") else None)
                rows = db.execute(
                    """SELECT s.path,s.qualified,s.line,s.end_line,s.kind,s.signature
                    FROM symbols s WHERE (? IS NULL OR s.path=?)
                    ORDER BY s.path,s.line LIMIT ?""",
                    (target_file, target_file, limit + 1),
                ).fetchall()
                body = "\n".join(
                    f"{p}:{line}-{end} [{kind}] {name} | {signature}"
                    for p, name, line, end, kind, signature in rows[:limit]
                )
            elif mode in {"references", "callers"}:
                target_name = query.split(".")[-1] if query else ""
                if not target_name:
                    raise ValueError("query is required to find references/callers")
                rows = db.execute(
                    """SELECT o.path, o.line, o.col, o.context,
                       COALESCE((
                           SELECT s.qualified || ' [' || s.kind || ']'
                           FROM symbols s
                           WHERE s.path = o.path
                             AND o.line >= s.line AND o.line <= s.end_line
                             AND s.name != o.name
                           ORDER BY (s.end_line - s.line) ASC, s.line DESC
                           LIMIT 1
                       ), '<module>') AS caller
                    FROM occurrences o
                    WHERE o.name = ? AND (? IS NULL OR o.path = ?)
                      AND NOT EXISTS (
                          SELECT 1 FROM symbols def_s
                          WHERE def_s.path = o.path AND def_s.name = o.name AND def_s.line = o.line
                      )
                    ORDER BY o.path, o.line LIMIT ?""",
                    (target_name, file, file, limit + 1),
                ).fetchall()
                matched_files = len({r[0] for r in rows[:limit]})
                header += f"# References and callers for '{target_name}' across {matched_files} files (0 LSP daemons)\n"
                body = "\n".join(
                    f"{p}:{line}:{col + 1} in {caller} | {context.strip()}"
                    for p, line, col, context, caller in rows[:limit]
                )
            elif mode == "map":
                graph, support = _build_file_graph(db)
                scores = _calculate_pagerank(graph)
                header = (
                    f"# map: {total} files, {self.updated} reindexed; "
                    "PageRank-ranked syntax map (not LSP)\n"
                )
                rows = db.execute(
                    """SELECT path,qualified,line,end_line,signature FROM symbols
                    WHERE (? IS NULL OR path=?)
                    AND (?='' OR instr(lower(qualified),lower(?)) > 0)""",
                    (file, file, query, query),
                ).fetchall()
                rows.sort(
                    key=lambda row: (
                        -scores.get(row[0], 0),
                        -support.get((row[0], row[1], row[2]), 0),
                        row[0],
                        row[2],
                        row[1],
                    )
                )
                body = "\n".join(
                    f"{p}:{line}-{end} {name} | {signature}"
                    for p, name, line, end, signature in rows[:limit]
                )
            elif mode == "symbols":
                rows = db.execute(
                    """SELECT s.path,s.qualified,s.line,s.end_line,s.signature,
                    (SELECT COUNT(*) FROM occurrences o WHERE o.name=s.name) AS score
                    FROM symbols s WHERE (? IS NULL OR s.path=?)
                    AND (?='' OR instr(lower(s.qualified),lower(?)) > 0)
                    ORDER BY score DESC,s.path,s.line LIMIT ?""",
                    (file, file, query, query, limit + 1),
                ).fetchall()
                body = "\n".join(
                    f"{p}:{line}-{end} {name} | {signature}"
                    for p, name, line, end, signature, _ in rows[:limit]
                )
            elif mode == "occurrences":
                rows = db.execute(
                    """SELECT path,line,col,context FROM occurrences WHERE name=?
                    AND (? IS NULL OR path=?) ORDER BY path,line,col LIMIT ?""",
                    (query, file, file, limit + 1),
                ).fetchall()
                header += "# Syntactic name matches include definitions and unrelated same-name symbols. No semantic rename guarantee.\n"
                body = "\n".join(
                    f"{p}:{line}:{col + 1} {context}" for p, line, col, context in rows[:limit]
                )
            elif mode == "search":
                rows = db.execute(
                    """SELECT path,first,snippet(chunks,2,'','', ' … ',32) FROM chunks
                    WHERE chunks MATCH ? AND (? IS NULL OR path=?) ORDER BY bm25(chunks),path,first LIMIT ?""",
                    (terms(query), file, file, limit + 1),
                ).fetchall()
                body = "\n".join(
                    f"{p}:chunk-start={line}\n{text}" for p, line, text in rows[:limit]
                )
            else:
                rows = []
                for relative, language, expected in db.execute(
                    "SELECT path,language,sha FROM files WHERE language IS NOT NULL AND issue IS NULL AND (? IS NULL OR path=?) ORDER BY path",
                    (file, file),
                ):
                    path = self.root / relative
                    if not allowed_file(path, self.root):
                        continue
                    source = path.read_bytes().decode("utf-8")
                    if digest(source) != expected:
                        raise ValueError("File changed during query; retry on the current snapshot")
                    root = SgRoot(source, language).root()
                    for match in root.find_all(pattern=query):
                        rows.append(
                            (relative, match.range().start.line + 1, redact_secrets(match.text()))
                        )
                        if len(rows) > limit:
                            break
                    if len(rows) > limit:
                        break
                body = "\n".join(f"{p}:{line}\n{text}" for p, line, text in rows[:limit])
        if len(rows) > limit:
            body += "\n[More results omitted; narrow query/file or raise limit (max 200).]"
        if self.issues:
            body += "\n[Incomplete index: " + "; ".join(self.issues[:8]) + "]"
        return header + (body or "No matches.")
