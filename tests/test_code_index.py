import concurrent.futures
import re
import subprocess
from pathlib import Path

import pytest

from usagetrim.core.cache import ContextCache
from usagetrim.core.code_index import (
    CodeIndex,
    _build_file_graph,
    _calculate_pagerank,
    _language_family,
    digest,
    parse_source,
    read_symbol,
)
from usagetrim.core.skeleton import extract_symbol_or_range
from usagetrim.core.symbol_edit import replace_symbol
from usagetrim.mcp import server
from usagetrim.metrics.tokenizer import count_tokens


def test_pagerank_known_graph_includes_dangling_mass():
    scores = _calculate_pagerank({"A": {"B"}, "B": set()}, iterations=100)
    assert scores == pytest.approx({"A": 20 / 57, "B": 37 / 57}, abs=1e-12)
    assert sum(scores.values()) == pytest.approx(1.0)


def test_pagerank_isolated_nodes_and_empty_graph():
    assert _calculate_pagerank({}) == {}
    scores = _calculate_pagerank({"A": set(), "B": set(), "C": set()})
    assert scores == pytest.approx(dict.fromkeys(("A", "B", "C"), 1 / 3))
    assert sum(scores.values()) == pytest.approx(1.0)


def test_pagerank_is_deterministic_across_node_and_neighbor_order():
    first = {"A": ["B", "C"], "B": ["C"], "C": [], "D": ["A", "C"]}
    second = {"D": ["C", "A"], "C": [], "B": ["C"], "A": ["C", "B"]}
    assert _calculate_pagerank(first) == _calculate_pagerank(second)
    assert list(_calculate_pagerank(second)) == ["A", "B", "C", "D"]


def test_pagerank_target_only_nodes_and_duplicate_neighbors():
    assert _calculate_pagerank({"A": ["B", "B"]}) == _calculate_pagerank({"A": {"B"}, "B": set()})


@pytest.mark.parametrize("damping", [-0.1, 1.1, float("nan"), float("inf"), True, "0.85", None])
def test_pagerank_rejects_invalid_damping(damping):
    with pytest.raises(ValueError, match="damping"):
        _calculate_pagerank({}, damping=damping)


@pytest.mark.parametrize("iterations", [0, -1, True, False, 1.5, "20", None])
def test_pagerank_rejects_invalid_iterations(iterations):
    with pytest.raises(ValueError, match="iterations"):
        _calculate_pagerank({}, iterations=iterations)


@pytest.mark.parametrize("damping", [0.0, 1.0])
def test_pagerank_accepts_damping_boundaries(damping):
    scores = _calculate_pagerank({"A": {"B"}, "B": set()}, damping=damping)
    assert set(scores) == {"A", "B"}
    assert sum(scores.values()) == pytest.approx(1.0)
    if damping == 0:
        assert scores == {"A": 0.5, "B": 0.5}


@pytest.mark.parametrize(
    ("language", "family"),
    [
        ("python", "python"),
        ("javascript", "javascript-typescript"),
        ("typescript", "javascript-typescript"),
        ("tsx", "javascript-typescript"),
        ("rust", "rust"),
        ("go", "go"),
    ],
)
def test_graph_language_families(language, family):
    assert _language_family(language) == family


def _index_graph_sources(tmp_path, sources):
    root = tmp_path / "project"
    root.mkdir()
    for name, source in sources.items():
        (root / name).write_text(source, encoding="utf-8")
    index = CodeIndex(root)
    index.sync()
    with index.connect() as db:
        return _build_file_graph(db)


@pytest.mark.parametrize(
    "sources",
    [
        {
            "core.py": "class CoreService:\n    pass\n",
            "api.py": "CoreService()\nCoreService()\nCoreService()\n",
            "worker.py": "CoreService()\n",
            "helper.py": "def unrelated():\n    pass\n",
        },
        {
            "core.ts": "export class CoreService {}\n",
            "api.ts": "new CoreService();\nnew CoreService();\nnew CoreService();\n",
            "worker.js": "new CoreService();\n",
            "helper.ts": "export function unrelated() {}\n",
        },
    ],
    ids=["python", "typescript-javascript"],
)
def test_file_graph_multifile_hub_and_deduplicated_support(tmp_path, sources):
    graph, support = _index_graph_sources(tmp_path, sources)
    core, api, worker, helper = sources
    assert graph == {core: set(), api: {core}, worker: {core}, helper: set()}
    assert support[(core, "CoreService", 1)] == 2
    assert support[(helper, "unrelated", 1)] == 0
    scores = _calculate_pagerank(graph)
    assert scores[core] > max(scores[path] for path in (api, worker, helper))


@pytest.mark.parametrize("local_usage", ["", "shared()\n"])
def test_file_graph_does_not_infer_edges_from_locally_defined_names(tmp_path, local_usage):
    graph, support = _index_graph_sources(
        tmp_path,
        {
            "a.py": "def shared(): pass\n" + local_usage,
            "b.py": "def shared(): pass\n",
        },
    )
    assert graph == {"a.py": set(), "b.py": set()}
    assert support == {("a.py", "shared", 1): 0, ("b.py", "shared", 1): 0}


def test_file_graph_skips_ambiguous_external_target_files(tmp_path):
    graph, support = _index_graph_sources(
        tmp_path,
        {
            "core.py": "def shared(): pass\n",
            "other.py": "def shared(): pass\n",
            "api.py": "shared()\n",
        },
    )
    assert graph["api.py"] == set()
    assert support[("core.py", "shared", 1)] == 0
    assert support[("other.py", "shared", 1)] == 0


def test_file_graph_allows_multiple_definitions_in_one_target_file(tmp_path):
    graph, support = _index_graph_sources(
        tmp_path,
        {
            "core.py": "class A:\n    def run(self): pass\nclass B:\n    def run(self): pass\n",
            "api.py": "service.run()\nservice.run()\n",
        },
    )
    assert graph == {"core.py": set(), "api.py": {"core.py"}}
    assert support[("core.py", "A.run", 2)] == 0
    assert support[("core.py", "B.run", 4)] == 0


def test_file_graph_separates_python_and_typescript_names(tmp_path):
    graph, support = _index_graph_sources(
        tmp_path,
        {
            "python_client.py": "class Client: pass\n",
            "typescript_client.ts": "export class Client {}\nexport class WebOnly {}\n",
            "python_api.py": "Client()\nWebOnly()\n",
            "typescript_api.ts": "new Client();\n",
        },
    )
    assert graph == {
        "python_client.py": set(),
        "typescript_client.ts": set(),
        "python_api.py": {"python_client.py"},
        "typescript_api.ts": {"typescript_client.ts"},
    }
    assert support[("python_client.py", "Client", 1)] == 1
    assert support[("typescript_client.ts", "Client", 1)] == 1
    assert support[("typescript_client.ts", "WebOnly", 2)] == 0


def test_file_graph_excludes_text_and_parse_failures_but_keeps_empty_code(tmp_path):
    graph, support = _index_graph_sources(
        tmp_path,
        {
            "empty.py": "# No declarations\n",
            "broken.py": "def broken(:\n",
            "notes.md": "Documentation only\n",
        },
    )
    assert graph == {"empty.py": set()}
    assert support == {}


@pytest.fixture(params=["python", "typescript-javascript"])
def ranked_map_index(tmp_path, request):
    if request.param == "python":
        sources = {
            "core.py": "def local_helper(): pass\n\nclass CoreService: pass\n",
            "api.py": "CoreService()\n" + "helper()\n" * 12,
            "worker.py": "CoreService()\n",
            "utils.py": "def helper(): pass\n",
            "a_low.py": "class CoreUnused: pass\n",
        }
    else:
        sources = {
            "core.ts": "function localHelper() {}\nexport class CoreService {}\n",
            "api.ts": "new CoreService();\n" + "helper();\n" * 12,
            "worker.js": "new CoreService();\n",
            "utils.ts": "export function helper() {}\n",
            "a_low.ts": "export class CoreUnused {}\n",
        }
    root = tmp_path / "project"
    root.mkdir()
    for name, source in sources.items():
        (root / name).write_text(source, encoding="utf-8")
    return CodeIndex(root), next(iter(sources))


def test_map_pagerank_prefers_core_over_repeated_occurrence_frequency(ranked_map_index):
    index, core = ranked_map_index
    repo_map = index.query(mode="map", limit=1)
    symbols = index.query(mode="symbols", limit=1)
    assert "PageRank-ranked syntax map (not LSP)" in repo_map.splitlines()[0]
    assert repo_map.splitlines()[1].startswith(core + ":")
    assert " CoreService | " in repo_map.splitlines()[1]
    assert "utils." in symbols.splitlines()[1]
    assert " helper | " in symbols.splitlines()[1]
    assert "PageRank" not in symbols


def test_map_orders_symbols_within_a_file_by_external_support(ranked_map_index):
    index, core = ranked_map_index
    result = index.query(mode="map", file=core)
    entries = result.splitlines()[1:]
    assert len(entries) == 2
    assert all(entry.startswith(core + ":") for entry in entries)
    assert " CoreService | " in entries[0]
    assert "local_helper" in entries[1] or "localHelper" in entries[1]


def test_map_query_filters_candidates_without_losing_global_rank(ranked_map_index):
    index, core = ranked_map_index
    result = index.query(mode="map", query="CoRe")
    entries = result.splitlines()[1:]
    assert len(entries) == 2
    assert entries[0].startswith(core + ":") and " CoreService | " in entries[0]
    assert entries[1].startswith("a_low.") and " CoreUnused | " in entries[1]
    assert "No matches." in index.query(mode="map", query="no_such_symbol")


@pytest.mark.parametrize("limit", [1, 2])
def test_map_limit_selects_top_ranked_entries_and_preserves_omission_notice(
    ranked_map_index, limit
):
    index, _ = ranked_map_index
    complete = index.query(mode="map", limit=200)
    limited = index.query(mode="map", limit=limit)
    assert limited.splitlines()[1 : 1 + limit] == complete.splitlines()[1 : 1 + limit]
    assert len(limited.splitlines()) == limit + 2
    notice = "[More results omitted; narrow query/file or raise limit (max 200).]"
    assert limited.endswith(notice)
    assert notice not in complete


def test_map_ties_use_path_line_and_qualified_name_deterministically(tmp_path):
    (tmp_path / "b.ts").write_text("class Last {}\n", encoding="utf-8")
    (tmp_path / "a.ts").write_text("class Z {} class A {}\nclass Earlier {}\n", encoding="utf-8")
    index = CodeIndex(tmp_path)
    index.sync()
    first = index.query(mode="map")
    assert first == index.query(mode="map")
    assert [entry.split(" | ")[0] for entry in first.splitlines()[1:]] == [
        "a.ts:1-1 A",
        "a.ts:1-1 Z",
        "a.ts:2-2 Earlier",
        "b.ts:1-1 Last",
    ]


@pytest.mark.parametrize("sources", [{}, {"empty.py": "# No symbols\n"}, {"notes.md": "Notes\n"}])
def test_map_empty_or_no_symbol_repository(tmp_path, sources):
    for name, source in sources.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    result = CodeIndex(tmp_path).query(mode="map")
    assert result.startswith(f"# map: {len(sources)} files,")
    assert "PageRank-ranked syntax map (not LSP)" in result
    assert result.endswith("No matches.")


def test_map_preserves_incomplete_index_notice(tmp_path):
    (tmp_path / "good.py").write_text("def visible(): pass\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    result = CodeIndex(tmp_path).query(mode="map")
    assert "visible" in result
    assert "[Incomplete index: broken.py: SyntaxError: no reliable symbols]" in result


def test_qualified_symbol_preserves_source_and_rejects_ambiguity(tmp_path):
    path = tmp_path / "sample.py"
    source = 'class A:\n    def run(self):\n        return "first"\n\nclass B:\n    @staticmethod\n    def run():\n        # ważny komentarz 🐍\n        return "żółć"\n'
    path.write_text(source)
    result = extract_symbol_or_range(path, symbol="B.run")
    assert result.split("\n", 1)[1] == source[source.index("    @staticmethod") :].rstrip("\n")
    assert f"sha256={digest(source)}" in result
    with pytest.raises(ValueError, match="Ambiguous"):
        read_symbol(path, "run")
    assert '"first"' in read_symbol(path, "run@2")
    with pytest.raises(ValueError, match="not found"):
        read_symbol(path, "C.run")


@pytest.mark.parametrize(
    "extension,language,source,selector,fragment",
    [
        (
            ".ts",
            "typescript",
            'export class A { run(): string { return "🦊"; } }',
            "A.run",
            "run(): string",
        ),
        (
            ".tsx",
            "tsx",
            "export function View() { return <div>Hi</div>; }",
            "View",
            "export function",
        ),
        (".js", "javascript", "const square = (x) => x * x;", "square", "square = (x)"),
        (".rs", "rust", "struct A {}\nimpl A { fn run(&self) -> i32 { 42 } }", "A.run", "fn run"),
        (
            ".go",
            "go",
            "package main\ntype A struct {}\nfunc (a *A) Run() int { return 42 }",
            "A.Run",
            "func (a *A)",
        ),
        (".swift", "swift", "struct A { func run() -> Int { return 42 } }", "A.run", "func run"),
        (".java", "java", "class A { int run() { return 42; } }", "A.run", "int run"),
        (".c", "c", "int run(void) { return 42; }", "run", "int run"),
        (".cpp", "cpp", "int run() { return 42; }", "run", "int run"),
    ],
)
def test_multilanguage_exact_symbols(tmp_path, extension, language, source, selector, fragment):
    path = tmp_path / ("sample" + extension)
    path.write_text(source)
    symbols, _ = parse_source(source, language)
    assert selector in {s.qualified for s in symbols}
    assert fragment in read_symbol(path, selector)


def test_index_respects_git_ignores_symlinks_and_incremental_updates(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("ignored.py\n")
    (root / "ignored.py").write_text("def secret_ignored(): pass\n")
    (root / ".env.py").write_text("def secret_env(): pass\n")
    (root / "linked.py").symlink_to(root / "ignored.py")
    good = root / "good.py"
    good.write_text("def visible(): pass\nvisible()\n")
    index = CodeIndex(root)
    first = index.query()
    assert "1 files, 1 reindexed" in first and "visible" in first
    assert "secret" not in first
    original = Path.read_bytes
    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "read_bytes",
            lambda path: (_ for _ in ()).throw(AssertionError("warm index reread source")),
        )
        assert "0 reindexed" in index.query("occurrences", "visible")
    good.write_text("def changed(): pass\n")
    assert "changed" in index.query() and "visible" not in index.query()
    assert original(good)
    good.unlink()
    assert "No matches" in index.query()


def test_search_patterns_redaction_bounds_and_incomplete_index(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "good.py").write_text('def action():\n    print("żółć")\n    print("hello")\n')
    secret = "ghp_" + "a" * 36
    (root / "notes.md").write_text(
        f"deployment failed because authentication timed out\n{secret}\n"
    )
    (root / "broken.py").write_text("def broken(:\n")
    index = CodeIndex(root)
    assert "notes.md:chunk-start=1" in index.query("search", "authentication timed out")
    patterns = index.query("pattern", "print($A)", file="good.py", limit=1)
    assert 'print("żółć")' in patterns and "More results omitted" in patterns
    assert "Incomplete index" in patterns
    with index.connect() as db:
        chunks = str(db.execute("SELECT body FROM chunks").fetchall())
    assert secret not in chunks
    with pytest.raises(ValueError):
        index.query("search", "***")
    with pytest.raises(ValueError, match="inside root"):
        index.query(file="../outside.py")
    with pytest.raises(ValueError):
        index.query(limit=True)


def test_parallel_queries_share_consistent_index(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "sample.py").write_text("def work(): return 42\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: CodeIndex(root).query(), range(8)))
    assert all("sample.py:1" in result for result in results)
    with CodeIndex(root).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 1


def test_edit_preview_hash_syntax_crlf_and_surrounding_bytes(tmp_path):
    path = tmp_path / "sample.py"
    source = (
        "# before 🦊\r\nclass A:\r\n    def run(self):\r\n        return 1\r\n\r\n# untouched\r\n"
    )
    path.write_bytes(source.encode())
    path.chmod(0o640)
    replacement = "    def run(self):\n        # preserve me\n        return 2\n"
    preview = replace_symbol(path, "A.run", replacement, digest(source))
    assert "Preview only" in preview and path.read_bytes() == source.encode()
    with pytest.raises(ValueError, match="changed"):
        replace_symbol(path, "A.run", replacement, "outdated", apply=True)
    with pytest.raises((SyntaxError, ValueError)):
        replace_symbol(path, "A.run", "    def run(:", digest(source), apply=True)
    with pytest.raises(ValueError):
        replace_symbol(path, "A.run", "    def other(self): return 2", digest(source), apply=True)
    assert path.read_bytes() == source.encode()
    replace_symbol(path, "A.run", replacement, digest(source), apply=True)
    expected = source.replace("        return 1", "        # preserve me\r\n        return 2")
    assert path.read_bytes() == expected.encode()
    assert path.stat().st_mode & 0o777 == 0o640


def test_edit_aborts_concurrent_change(tmp_path, monkeypatch):
    from usagetrim.core import symbol_edit

    path = tmp_path / "sample.py"
    source = "def run(): return 1\n"
    path.write_text(source)
    original = symbol_edit.select_symbol
    calls = 0

    def changed_during_parse(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 2:
            path.write_text("def run(): return 999\n")
        return result

    monkeypatch.setattr(symbol_edit, "select_symbol", changed_during_parse)
    with pytest.raises(ValueError, match="changed while"):
        replace_symbol(path, "run", "def run(): return 2", digest(source), apply=True)
    assert "999" in path.read_text()
    assert not list(tmp_path.glob(".usagetrim-edit-*"))


def test_native_cli_code_and_guarded_edit(tmp_path):
    from typer.testing import CliRunner

    from usagetrim.cli import app

    root = tmp_path / "project"
    root.mkdir()
    source = "class A:\n    def run(self): return 1\nclass B:\n    def run(self): return 2\n"
    path = root / "sample.py"
    path.write_text(source)
    replacement = tmp_path / "replacement.py"
    replacement.write_text("    def run(self): return 3\n")
    runner = CliRunner()
    result = runner.invoke(app, ["code", str(root), "--mode", "symbols", "--query", "B.run"])
    assert result.exit_code == 0 and "B.run" in result.output
    result = runner.invoke(app, ["cat", str(path), "--symbol", "run"])
    assert (
        result.exit_code != 0 and "Ambiguous" in result.output and "Traceback" not in result.output
    )
    arguments = [
        "edit-symbol",
        str(path),
        "B.run",
        "--replacement-file",
        str(replacement),
        "--expected-hash",
        digest(source),
    ]
    assert runner.invoke(app, arguments).exit_code == 0 and path.read_text() == source
    assert runner.invoke(app, [*arguments, "--apply"]).exit_code == 0
    assert path.read_text() == source.replace("return 2", "return 3")
    assert runner.invoke(app, [*arguments, "--apply"]).exit_code != 0


def test_cache_search_scope_and_clear():
    cache = ContextCache()
    secret = "ghp_" + "b" * 36
    first = cache.store(
        "\n".join(["normal log"] * 90 + [f"timeout żółć {secret}"] + ["normal log"] * 90)
    )
    other = cache.store("timeout from unrelated project")
    result = cache.search(first, "timeout")
    assert "timeout żółć" in result and "unrelated" not in result and secret not in result
    assert "lines 91-120" in result
    assert "No matches" in cache.search(first, "nonexistent")
    assert "not found" in cache.search("tc_missing", "timeout")
    with pytest.raises(ValueError):
        cache.search(other, "***")
    cache.clear()
    assert "not found" in cache.search(first, "timeout")


def test_cache_search_does_not_mistake_error_output_for_missing_ref():
    cache = ContextCache()
    ref = cache.store("Error: authentication failed\n" + "irrelevant\n" * 100)
    assert "No matches" in cache.search(ref, "nonexistent")
    result = cache.search(ref, "authentication")
    assert "lines 1-30" in result and result.count("irrelevant") == 29


def test_code_events_have_no_source_or_query_in_telemetry(tmp_path):
    from usagetrim.core.monitor import Monitor
    from usagetrim.core.telemetry import TelemetryStore

    root = tmp_path / "project"
    root.mkdir()
    (root / "sample.py").write_text('def PRIVATE_SYMBOL(): return "PRIVATE_CONTENT"\n')
    server.handle_usagetrim_code({"root": str(root), "query": "PRIVATE_SYMBOL"})
    store = TelemetryStore()
    with store.connect() as db:
        rows = db.execute("SELECT * FROM events").fetchall()
    assert "PRIVATE_" not in str(rows)
    assert b"PRIVATE_" not in store.db_path.read_bytes()
    snapshot = Monitor([store.db_path.parent], tmp_path / "collector").snapshot()
    card = next(item for item in snapshot["integrations"] if item["name"] == "UsageTrim Code")
    assert card["last_event"] is not None


@pytest.fixture
def large_repo_map(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "core.py").write_text(
        "class CoreService: pass\n"
        + "\n".join(f"def operation_{number}(): return {number}" for number in range(40)),
        encoding="utf-8",
    )
    (root / "api.py").write_text("CoreService()\n", encoding="utf-8")
    (root / "worker.py").write_text("CoreService()\n", encoding="utf-8")
    index = CodeIndex(root)
    index.sync()
    return index


def test_mcp_map_budget_preserves_entries_rank_recovery_and_raw_telemetry(
    large_repo_map, monkeypatch
):
    index = large_repo_map
    full = index.query(mode="map", limit=100)
    recorded = []
    original_record = server._record

    def record(raw, output, **kwargs):
        recorded.append((raw, output))
        return original_record(raw, output, **kwargs)

    monkeypatch.setattr(server, "_record", record)
    output = server.handle_usagetrim_code(
        {"root": str(index.root), "mode": "map", "limit": 100, "max_tokens": 128}
    )
    assert count_tokens(output).claude <= 128
    assert output.splitlines()[0] == full.splitlines()[0]
    full_entries = full.splitlines()[1:]
    entries = [line for line in output.splitlines()[1:] if not line.startswith("[")]
    assert entries and entries[0] == full_entries[0]
    assert " CoreService | " in entries[0]
    assert all(entry in full_entries for entry in entries)
    positions = [full_entries.index(entry) for entry in entries]
    assert positions == sorted(positions)
    omitted = next(entry for entry in full_entries if entry not in entries)
    ref = re.search(r"tc_[a-f0-9]+", output).group()
    recovered = ContextCache().retrieve(ref)
    assert recovered == full and omitted in recovered
    assert recorded == [(full, output)]


def test_mcp_map_that_fits_is_unchanged_without_cache(large_repo_map, monkeypatch):
    full = large_repo_map.query(mode="map", limit=100)

    def unexpected_store(*args, **kwargs):
        pytest.fail("A fitting map must not create a recovery entry")

    monkeypatch.setattr(ContextCache, "store", unexpected_store)
    output = server.handle_usagetrim_code(
        {"root": str(large_repo_map.root), "mode": "map", "limit": 100, "max_tokens": 32000}
    )
    assert output == full and "tc_" not in output


def test_mcp_fitting_map_redacts_secret_in_file_path_without_recovery(tmp_path):
    secret = "ghp_" + "a" * 36
    (tmp_path / f"{secret}.py").write_text("def visible(): pass\n", encoding="utf-8")
    full = CodeIndex(tmp_path).query(mode="map")
    assert secret in full
    output = server.handle_usagetrim_code(
        {"root": str(tmp_path), "mode": "map", "max_tokens": 32000}
    )
    assert secret not in output
    assert "[REDACTED" in output
    assert "tc_" not in output
    assert count_tokens(output).claude <= 32000


@pytest.fixture
def map_with_oversized_entry(tmp_path):
    parameters = ", ".join(f"parameter_{number}: int" for number in range(30))
    (tmp_path / "sample.py").write_text(
        f"def giant({parameters}): pass\ndef short(): pass\ndef last(): pass\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path)
    index.sync()
    return index


def test_mcp_map_skips_oversized_entry_and_keeps_later_short_entries(map_with_oversized_entry):
    index = map_with_oversized_entry
    full = index.query(mode="map")
    giant, short, _ = full.splitlines()[1:]
    output = server.handle_usagetrim_code(
        {"root": str(index.root), "mode": "map", "max_tokens": 128}
    )
    assert count_tokens(output).claude <= 128
    assert "giant" not in output and short in output.splitlines()
    ref = re.search(r"tc_[a-f0-9]+", output).group()
    assert ContextCache().retrieve(ref) == full
    assert giant in ContextCache().retrieve(ref)


def test_mcp_map_preserves_limit_notice_and_limits_recovery(map_with_oversized_entry):
    index = map_with_oversized_entry
    full = index.query(mode="map", limit=1)
    assert count_tokens(full).claude > 128
    output = server.handle_usagetrim_code(
        {"root": str(index.root), "mode": "map", "limit": 1, "max_tokens": 128}
    )
    assert count_tokens(output).claude <= 128
    assert full.splitlines()[-1] in output.splitlines()
    ref = re.search(r"tc_[a-f0-9]+", output).group()
    recovered = ContextCache().retrieve(ref)
    assert recovered == full and "giant" in recovered
    assert " short | " not in recovered


def test_mcp_map_preserves_complete_incomplete_index_notice(large_repo_map):
    index = large_repo_map
    (index.root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    index.sync()
    full = index.query(mode="map", limit=100)
    output = server.handle_usagetrim_code(
        {"root": str(index.root), "mode": "map", "limit": 100, "max_tokens": 128}
    )
    assert count_tokens(output).claude <= 128
    assert full.splitlines()[-1].startswith("[Incomplete index:")
    assert full.splitlines()[-1] in output.splitlines()


def test_map_budget_reserves_metadata_and_rejects_impossible_budget():
    header = "# map: 200 files, 0 reindexed; PageRank-ranked syntax map (not LSP)"
    entry = "sample.py:1-2 giant | def giant(" + ", ".join(f"param_{i}" for i in range(200)) + "):"
    notice = "[More results omitted; narrow query/file or raise limit (max 200).]"
    full = "\n".join((header, entry, notice))
    ref = ContextCache().store(full, source="code-query")
    metadata = header + "\n" + notice + "\n" + server.recovery_notice(ref)
    minimum = count_tokens(metadata).claude
    assert minimum > 64
    assert server._budget_repo_map(full, minimum) == metadata
    with pytest.raises(ValueError, match="header, notices and recovery reference"):
        server._budget_repo_map(full, minimum - 1)
    with pytest.raises(ValueError, match="header, notices and recovery reference"):
        server._budget_repo_map(full, 64)


def test_map_budget_can_return_only_header_and_reference_at_64_tokens():
    full = "# map\nsample.py:1-1 giant | " + "parameter " * 200
    output = server._budget_repo_map(full, 64)
    assert count_tokens(output).claude <= 64
    ref = re.search(r"tc_[a-f0-9]+", output).group()
    assert output == "# map\n" + server.recovery_notice(ref)
    assert ContextCache().retrieve(ref) == full


def test_map_recovery_retains_existing_cache_redaction():
    secret = "ghp_" + "a" * 36
    full = "# map\nsample.py:1-1 giant | " + "parameter " * 200 + secret
    output = server._budget_repo_map(full, 64)
    ref = re.search(r"tc_[a-f0-9]+", output).group()
    recovered = ContextCache().retrieve(ref)
    assert secret not in recovered and secret not in output
    assert "[REDACTED" in recovered


@pytest.mark.parametrize("mode", ["symbols", "search"])
def test_non_map_code_modes_keep_generic_compression(large_repo_map, monkeypatch, mode):
    calls = []
    original_compress = server._compress

    def compress(text, budget, **kwargs):
        calls.append((text, budget, kwargs))
        return original_compress(text, budget, **kwargs)

    monkeypatch.setattr(server, "_compress", compress)
    output = server.handle_usagetrim_code(
        {"root": str(large_repo_map.root), "mode": mode, "query": "CoreService", "max_tokens": 128}
    )
    assert calls == [(large_repo_map.query(mode, "CoreService"), 128, {"source": "code-query"})]
    assert count_tokens(output).claude <= 128


def test_paused_map_keeps_existing_compression_bypass(large_repo_map, monkeypatch):
    full = large_repo_map.query(mode="map", limit=100)
    monkeypatch.setattr(server, "paused", lambda: True)
    assert (
        server.handle_usagetrim_code(
            {"root": str(large_repo_map.root), "mode": "map", "limit": 100, "max_tokens": 128}
        )
        == full
    )


def test_cli_code_map_budget_uses_complete_entries_and_recovery(large_repo_map):
    from typer.testing import CliRunner

    from usagetrim.cli import app

    full = large_repo_map.query(mode="map", limit=100)
    result = CliRunner().invoke(
        app,
        [
            "code",
            str(large_repo_map.root),
            "--mode",
            "map",
            "--limit",
            "100",
            "--budget",
            "128",
        ],
    )
    assert result.exit_code == 0, result.output
    assert count_tokens(result.stdout).claude <= 128
    assert result.stdout.splitlines()[0] == full.splitlines()[0]
    ref = re.search(r"tc_[a-f0-9]+", result.stdout).group()
    assert ContextCache().retrieve(ref) == full


def test_mcp_code_and_search_recovery_obey_output_budget(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "sample.py").write_text(
        "\n".join(f"def function_{i}(): return {i}" for i in range(100))
    )
    response = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "usagetrim_code",
                "arguments": {"root": str(root), "limit": 100, "max_tokens": 128},
            },
        }
    )
    assert not response["result"].get("isError")
    text = response["result"]["content"][0]["text"]
    assert count_tokens(text).claude <= 128
    ref = re.search(r"tc_[a-f0-9]+", text).group()
    recovered = server.handle_usagetrim_retrieve(
        {"ref_id": ref, "query": "function_99", "max_tokens": 256}
    )
    assert "function_99" in recovered and count_tokens(recovered).claude <= 256
    with pytest.raises(ValueError):
        server.handle_usagetrim_retrieve({"ref_id": ref, "query": "function_99", "lines": "1-2"})


def test_code_index_outline_mode(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = (
        "class AuthService:\n"
        "    def __init__(self, key: str) -> None:\n"
        "        self.key = key\n\n"
        "    def authenticate(self, user: str) -> bool:\n"
        "        return True\n\n"
        "def helper() -> None:\n"
        "    pass\n"
    )
    (root / "auth.py").write_text(source)
    index = CodeIndex(root)

    # Query outline with file param
    outline = index.query(mode="outline", file="auth.py")
    assert "# outline:" in outline
    assert "auth.py:1-6 [class_definition] AuthService" in outline
    assert "auth.py:2-3 [function_definition] AuthService.__init__" in outline
    assert (
        "auth.py:5-6 [function_definition] AuthService.authenticate | def authenticate(self, user: str) -> bool:"
        in outline
    )
    assert "auth.py:8-9 [function_definition] helper | def helper() -> None:" in outline

    # Query outline via MCP server
    mcp_res = server.handle_usagetrim_code(
        {"root": str(root), "mode": "outline", "file": "auth.py"}
    )
    assert "AuthService.authenticate" in mcp_res


def test_code_index_references_and_callers_mode(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    auth = (
        "class AuthService:\n    def verify_token(self, token: str) -> bool:\n        return True\n"
    )
    api = (
        "from auth import AuthService\n\n"
        "def login_endpoint(token: str) -> bool:\n"
        "    auth = AuthService()\n"
        "    return auth.verify_token(token)\n"
    )
    (root / "auth.py").write_text(auth)
    (root / "api.py").write_text(api)
    index = CodeIndex(root)

    # Query callers
    res = index.query(mode="callers", query="verify_token")
    assert "# callers:" in res
    assert "api.py:5:" in res
    assert "in login_endpoint [function_definition]" in res
    assert "auth.verify_token(token)" in res
    # Should not list verify_token's own declaration line in auth.py
    assert "def verify_token" not in res

    # Query references alias via MCP
    mcp_res = server.handle_usagetrim_code(
        {"root": str(root), "mode": "references", "query": "AuthService.verify_token"}
    )
    assert "login_endpoint" in mcp_res
