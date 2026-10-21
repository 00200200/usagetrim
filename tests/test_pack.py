from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from typer.testing import CliRunner

from usagetrim.cli import app
from usagetrim.core.cache import ContextCache
from usagetrim.core.pack import (
    _render_repository_pack,
    pack_context,
    pack_repository,
    slice_repository_context,
)
from usagetrim.metrics.tokenizer import count_tokens


def test_pack_context_basic(tmp_path: Path):
    # Setup a mock project
    (tmp_path / "src").mkdir()
    py_file = tmp_path / "src" / "app.py"
    py_file.write_text(
        "class App:\n"
        "    def run(self):\n"
        "        # long implementation\n"
        "        print('hello')\n"
        "        return 1\n"
    )

    readme = tmp_path / "README.md"
    readme.write_text("# My Project\nA cool project.\n")

    res = pack_context(root=tmp_path, budget=4000)
    assert res.file_count >= 2
    assert "app.py" in res.bundle_text
    assert "README.md" in res.bundle_text
    assert "# UsageTrim Context Bundle" in res.bundle_text
    assert res.packed_tokens <= 4000


def test_pack_context_force_skeleton(tmp_path: Path):
    code_file = tmp_path / "math_utils.py"
    code_file.write_text(
        "def compute_sum(a: int, b: int) -> int:\n"
        "    result = a + b\n"
        "    return result\n\n"
        "def compute_mul(a: int, b: int) -> int:\n"
        "    result = a * b\n"
        "    return result\n"
    )

    res = pack_context(root=tmp_path, budget=4000, force_skeleton=True)
    assert res.file_count == 1
    file_entry = res.files[0]
    assert file_entry.is_skeleton
    assert "def compute_sum" in file_entry.content
    assert "..." in file_entry.content
    assert "Recover: usagetrim retrieve" in file_entry.content
    assert file_entry.ref_id is not None
    assert "compute_sum" in ContextCache().retrieve(file_entry.ref_id)


def test_pack_context_secret_scrubbing(tmp_path: Path):
    secret_key = "ghp_" + "B" * 36
    env_file = tmp_path / "config.py"
    env_file.write_text(f"API_SECRET = '{secret_key}'\n")

    res = pack_context(root=tmp_path, budget=4000)
    assert secret_key not in res.bundle_text
    assert "[REDACTED" in res.bundle_text


def test_pack_context_budget_ceiling(tmp_path: Path):
    # Create several files with substantial content
    for i in range(5):
        f = tmp_path / f"module_{i}.py"
        lines = [f"def func_{i}_{j}():\n    return {j * 10}\n" for j in range(50)]
        f.write_text("".join(lines))

    # Restrict budget to small amount
    res = pack_context(root=tmp_path, budget=300)
    assert res.file_count == 5
    # Token ceiling is respected
    assert res.packed_tokens < 600
    assert any("lines omitted" in f.content for f in res.files)
    assert any(f.ref_id is not None for f in res.files)


def test_pack_context_specific_paths(tmp_path: Path):
    f1 = tmp_path / "wanted.py"
    f1.write_text("print('wanted')")
    f2 = tmp_path / "unwanted.py"
    f2.write_text("print('unwanted')")

    res = pack_context(paths=["wanted.py"], root=tmp_path, budget=1000)
    assert res.file_count == 1
    assert "wanted.py" in res.bundle_text
    assert "unwanted.py" not in res.bundle_text


def test_pack_context_to_dict(tmp_path: Path):
    f = tmp_path / "hello.py"
    f.write_text("print('world')")

    res = pack_context(root=tmp_path, budget=1000)
    d = res.to_dict()
    assert d["file_count"] == 1
    assert "bundle_text" in d
    assert len(d["files"]) == 1
    assert d["files"][0]["path"] == "hello.py"


def test_slice_repository_context_with_dependencies(tmp_path: Path):
    # Setup repository structure
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    # Dependency 1
    dep1 = src_dir / "helper.py"
    dep1.write_text(
        'def compute_helper(x: int) -> int:\n    """Helper calculation."""\n    return x * 42\n'
    )

    # Dependency 2
    dep2 = src_dir / "database.py"
    dep2.write_text(
        "class Database:\n"
        '    """Database client."""\n'
        "    def connect(self) -> bool:\n"
        "        return True\n"
    )

    # Target modified file importing both
    target = src_dir / "service.py"
    target.write_text(
        "from helper import compute_helper\n"
        "from database import Database\n\n"
        "def run_service(val: int) -> int:\n"
        "    db = Database()\n"
        "    return compute_helper(val)\n"
    )

    # Unrelated file
    unrelated = src_dir / "unrelated.py"
    unrelated.write_text("def unrelated_task(): pass\n")

    res = slice_repository_context(files=[target], root=tmp_path, budget=8000)
    assert res.file_count >= 2
    paths = {f.path for f in res.files}
    assert "src/service.py" in paths
    assert "src/helper.py" in paths or "src/database.py" in paths
    assert "src/unrelated.py" not in paths

    # Check that service.py is full source (not skeleton)
    service_entry = next(f for f in res.files if f.path == "src/service.py")
    assert not service_entry.is_skeleton
    assert service_entry.file_type == "modified"

    # Check that dependency is skeleton
    dep_entries = [f for f in res.files if f.file_type == "dependency"]
    for d in dep_entries:
        assert d.is_skeleton


def test_slice_repository_context_budget_enforcement(tmp_path: Path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    # Large target
    target = src_dir / "main.py"
    target.write_text("import utils\n" + "\n".join([f"x_{i} = {i}" for i in range(100)]))

    # Large dependency
    dep = src_dir / "utils.py"
    dep.write_text("\n".join([f"def util_func_{i}(): return {i}" for i in range(100)]))

    # Strict token budget ceiling
    res = slice_repository_context(files=[target], root=tmp_path, budget=250)
    assert res.packed_tokens <= 400


def test_slice_repository_context_xml_format(tmp_path: Path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    dep = src_dir / "util.py"
    dep.write_text("def helper(): return 1\n")

    target = src_dir / "main.py"
    target.write_text("from util import helper\n\nprint(helper())\n")

    res = slice_repository_context(files=[target], root=tmp_path, budget=4000, format_type="xml")
    assert "<repository_slice" in res.bundle_text
    assert "<modified_files" in res.bundle_text
    assert "src/main.py" in res.bundle_text


def test_slice_cli_command(tmp_path: Path):
    runner = CliRunner()
    target = tmp_path / "feature.py"
    target.write_text("def my_feature(): return 1\n")

    result = runner.invoke(
        app,
        [
            "slice",
            str(target),
            "--root",
            str(tmp_path),
            "--budget",
            "4000",
        ],
    )
    assert result.exit_code == 0
    assert "UsageTrim Repository Slice Context" in result.output
    assert "feature.py" in result.output


def test_repository_pack_xml_escapes_paths_content_and_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path / "cache"))
    secret = "ghp_" + "a" * 36
    credential = "postgres://alice:password@localhost/app"
    path = tmp_path / 'a&"<file>.py'
    path.write_text(
        f'def value() -> str:\n    return "</file> ]]> café & {secret} {credential} \x01"\n',
        encoding="utf-8",
    )
    result = pack_repository(tmp_path, mode="full")
    document = ET.fromstring(result.bundle_text)
    file = document.find("./files/file")
    assert file is not None and file.attrib["path"] == path.name
    assert document.find("file_summary") is not None
    assert document.find("directory_structure") is not None
    assert "</file> ]]> café &" in file.text
    assert "\x01" not in result.bundle_text
    assert secret not in result.bundle_text and credential not in result.bundle_text
    assert "[REDACTED" in result.bundle_text
    assert result.packed_tokens == count_tokens(result.bundle_text).openai


def test_repository_pack_markdown_uses_longer_fence_than_source(tmp_path):
    content = '```python\nprint("example")\n```\n````\n'
    (tmp_path / "sample.md").write_text(content)
    result = pack_repository(tmp_path, mode="full", format_type="markdown")
    assert "`````md\n" in result.bundle_text
    assert f"`````md\n{content}`````\n" in result.bundle_text
    assert result.packed_tokens == count_tokens(result.bundle_text).openai


def test_repository_pack_markdown_closes_fence_after_source_without_final_newline(tmp_path):
    (tmp_path / "sample.md").write_text("A `literal` without terminal newline")
    result = pack_repository(tmp_path, mode="full", format_type="markdown")
    assert "```md\nA `literal` without terminal newline\n```" in result.bundle_text


@pytest.mark.parametrize("format_type", ["xml", "markdown"])
@pytest.mark.parametrize("budget", [100, 150, 300])
def test_repository_pack_strict_budget_with_overhead_and_omissions(tmp_path, format_type, budget):
    for number in range(18):
        name = f"module_{number:02d}_with_an_unusually_long_name.py"
        (tmp_path / name).write_text(f"def function_{number}():\n    return {number}\n")
    result = pack_repository(tmp_path, format_type=format_type, budget=budget)
    assert result.packed_tokens == count_tokens(result.bundle_text).openai <= budget
    assert result.file_count + len(result.omitted_files) == 18
    assert len(result.omitted_files) > 0
    if format_type == "xml":
        document = ET.fromstring(result.bundle_text)
        assert int(document.find("omitted_files").attrib["count"]) == len(result.omitted_files)
    else:
        assert "## Omitted Files" in result.bundle_text


@pytest.mark.parametrize("format_type", ["xml", "markdown"])
def test_repository_pack_skips_file_that_only_fits_without_render_overhead(tmp_path, format_type):
    long_dir = tmp_path / ("long_directory_" + "x" * 80)
    long_dir.mkdir()
    first = long_dir / ("first_" + "y" * 80 + ".py")
    first.write_text('VALUE = "' + "word " * 60 + '"\n')
    (tmp_path / "small.md").write_text("small\n")
    unbudgeted = pack_repository(tmp_path, format_type=format_type, mode="full")
    large, small = unbudgeted.files

    def rendered_tokens(selected, detail):
        text = _render_repository_pack(
            root_name=tmp_path.name,
            format_type=format_type,
            mode="full",
            candidates=unbudgeted.files,
            selected=selected,
            source_tokens=unbudgeted.original_tokens,
            detail=detail,
        )
        return count_tokens(text).openai

    budget = rendered_tokens([], 2) + large.packed_tokens
    assert large.path == first.relative_to(tmp_path).as_posix()
    assert all(rendered_tokens([large], detail) > budget for detail in range(3))
    assert any(rendered_tokens([small], detail) <= budget for detail in range(3))

    result = pack_repository(tmp_path, format_type=format_type, mode="full", budget=budget)
    assert [file.path for file in result.files] == ["small.md"]
    assert result.omitted_files == [large.path]
    assert result.packed_tokens == count_tokens(result.bundle_text).openai <= budget
    assert (
        pack_repository(tmp_path, format_type=format_type, mode="full", budget=budget).bundle_text
        == result.bundle_text
    )


def test_repository_pack_tiny_budget_rejects_impossible_document(tmp_path):
    with pytest.raises(ValueError, match="budget is too small"):
        pack_repository(tmp_path, budget=1)


@pytest.mark.parametrize("format_type", ["xml", "markdown"])
def test_repository_pack_budget_handles_huge_file_long_path_and_hybrid(tmp_path, format_type):
    long_dir = tmp_path / ("long_directory_" + "x" * 60)
    long_dir.mkdir()
    path = long_dir / ("large_" + "y" * 60 + ".py")
    path.write_text("def large(value):\n" + "    value += 1\n" * 3000 + "    return value\n")
    (tmp_path / "small.py").write_text("VALUE = 1\n")
    result = pack_repository(
        tmp_path,
        format_type=format_type,
        mode="hybrid",
        focus=[long_dir.name],
        budget=100,
    )
    assert result.packed_tokens == count_tokens(result.bundle_text).openai <= 100
    assert result.file_count == 1 and result.files[0].path == "small.py"
    assert len(result.omitted_files) == 1
    if format_type == "xml":
        assert ET.fromstring(result.bundle_text).find("omitted_files").attrib["count"] == "1"
    else:
        assert "## Omitted Files" in result.bundle_text


def test_repository_pack_hybrid_focus_prefix_and_multiple_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path / "cache"))
    for rel in ("src/core/service.py", "src/core/model.py", "src/api.py", "tests/test_api.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "def handle(value: int) -> int:\n"
            + "".join(f"    value += {number}\n" for number in range(60))
            + "    return value\n"
        )
    result = pack_repository(tmp_path, mode="hybrid", focus=["src/core/", "tests/test_api.py"])
    modes = {file.path: file.is_skeleton for file in result.files}
    assert modes == {
        "src/core/model.py": False,
        "src/core/service.py": False,
        "src/api.py": True,
        "tests/test_api.py": False,
    }
    assert [file.path for file in result.files[:3]] == [
        "src/core/model.py",
        "src/core/service.py",
        "tests/test_api.py",
    ]
    document = ET.fromstring(result.bundle_text)
    assert {file.attrib["mode"] for file in document.findall("./files/file")} == {
        "full",
        "skeleton",
    }


def test_repository_pack_focus_and_path_security(tmp_path):
    (tmp_path / "okay.py").write_text("def okay(): pass\n")
    outside = tmp_path.parent / "outside-pack.py"
    (tmp_path / "linked.py").symlink_to(outside)
    (tmp_path / "loop.py").symlink_to("loop.py")
    for focus in ("../outside", str(tmp_path / "okay.py"), "linked.py", "loop.py"):
        with pytest.raises(ValueError):
            pack_repository(tmp_path, mode="hybrid", focus=[focus])
    with pytest.raises(ValueError, match="focus"):
        pack_repository(tmp_path, mode="full", focus=["okay.py"])
    with pytest.raises(ValueError):
        pack_repository(tmp_path, paths=["../outside"])
    with pytest.raises(ValueError):
        pack_repository(tmp_path, paths=["loop.py"])
    result = pack_repository(tmp_path, paths=["okay.py", "okay.py"], mode="full")
    assert result.file_count == 1


def test_repository_pack_redacts_path_metadata_and_excludes_env_file(tmp_path):
    secret = "ghp_" + "q" * 36
    (tmp_path / f"{secret}.py").write_text("def ordinary(): pass\n")
    (tmp_path / ".env").write_text("PASSWORD=very_private_value\n")
    result = pack_repository(tmp_path, mode="full")
    assert secret not in result.bundle_text
    assert "[REDACTED_GITHUB_TOKEN]" in result.bundle_text
    assert "very_private_value" not in result.bundle_text
    assert len(result.files) == 1


def test_repository_pack_strip_comments_and_unsupported_fallback(tmp_path):
    (tmp_path / "app.py").write_text("# remove me\ndef run():\n    return 42\n")
    (tmp_path / "app.ts").write_text(
        "// remove TS comment\nexport function run(): number { return 42; }\n"
    )
    (tmp_path / "notes.md").write_text("# Keep this Markdown note\n")
    result = pack_repository(tmp_path, mode="full", strip_comments=True)
    entries = {file.path: file for file in result.files}
    assert "remove me" not in entries["app.py"].content
    assert "def run" in entries["app.py"].content
    assert "remove TS comment" not in entries["app.ts"].content
    assert not entries["notes.md"].is_skeleton
    assert "Keep this Markdown note" in entries["notes.md"].content


def test_repository_pack_skeleton_recovery_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path / "cache"))
    secret = "ghp_" + "z" * 36
    path = tmp_path / "service.py"
    path.write_text(
        "def serve(value: int) -> int:\n"
        + f"    token = '{secret}'\n"
        + "".join(f"    value += {number}\n" for number in range(80))
        + "    return value\n"
    )
    result = pack_repository(tmp_path)
    file = result.files[0]
    assert file.is_skeleton and file.ref_id
    assert secret not in result.bundle_text
    recovered = ContextCache().retrieve(file.ref_id)
    assert secret not in recovered and "[REDACTED" in recovered
    assert "value += 79" in recovered and "value += 79" not in file.content


def test_repository_pack_malformed_code_and_determinism(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n")
    (tmp_path / "broken.ts").write_text("function broken(value: number { return value; }\n")
    (tmp_path / "data.txt").write_text("ordinary note\n")
    first = pack_repository(tmp_path)
    second = pack_repository(tmp_path)
    assert first.bundle_text == second.bundle_text
    assert all(not file.is_skeleton for file in first.files)
    assert "def broken(" in first.bundle_text
    assert "function broken(" in first.bundle_text


def test_repository_pack_invalid_utf8_uses_visible_full_fallback(tmp_path):
    (tmp_path / "damaged.py").write_bytes(b"def damaged():\n    return b'\xff'\n")
    result = pack_repository(tmp_path)
    assert result.file_count == 1
    assert not result.files[0].is_skeleton
    assert "def damaged" in result.bundle_text
    ET.fromstring(result.bundle_text)


def test_repository_pack_excludes_binary_even_with_source_extension(tmp_path):
    (tmp_path / "binary.py").write_bytes(b"\x00\x01\x02")
    result = pack_repository(tmp_path)
    assert result.file_count == 0
    assert b"\x00" not in result.bundle_text.encode()


def test_repository_pack_four_language_skeleton_reduces_final_bundle_over_75_percent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path / "cache"))
    sources = {
        "processor.py": """from collections import defaultdict

class BatchProcessor:
    def process(self, records: list[dict]) -> dict[str, list[int]]:
        grouped = defaultdict(list)
        rejected = []
        for record in records:
            if not isinstance(record, dict):
                rejected.append("invalid record")
                continue
            category = str(record.get("category", "unknown")).strip().lower()
            if not category:
                category = "unknown"
            amount = record.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                rejected.append(f"{category}: invalid amount")
                continue
            if amount < 0:
                rejected.append(f"{category}: negative amount")
                continue
            normalized = round(amount * 100)
            grouped[category].append(normalized)
        result = {}
        for category, values in grouped.items():
            values.sort()
            if values:
                result[category] = values
        if rejected:
            result["_rejected"] = rejected
        return result

    def totals(self, grouped: dict[str, list[int]]) -> dict[str, int]:
        totals = {}
        for category, values in grouped.items():
            if category.startswith("_"):
                continue
            running = 0
            for value in values:
                running += value
            totals[category] = running
        return totals

    def report(self, grouped: dict[str, list[int]]) -> list[str]:
        summary = self.totals(grouped)
        categories = sorted(summary)
        lines = ["Batch processing report"]
        grand_total = 0
        for category in categories:
            values = grouped[category]
            total = summary[category]
            grand_total += total
            count = len(values)
            minimum = min(values) if values else 0
            maximum = max(values) if values else 0
            average = total // count if count else 0
            lines.append(f"Category: {category}")
            lines.append(f"  Count: {count}")
            lines.append(f"  Total: {total}")
            lines.append(f"  Minimum: {minimum}")
            lines.append(f"  Maximum: {maximum}")
            lines.append(f"  Average: {average}")
        lines.append(f"Grand total: {grand_total}")
        return lines

    def validate(self, records: list[dict]) -> list[str]:
        problems = []
        seen = set()
        for position, record in enumerate(records):
            if not isinstance(record, dict):
                problems.append(f"Record {position}: expected mapping")
                continue
            identifier = record.get("id")
            if identifier is None:
                problems.append(f"Record {position}: missing id")
                continue
            if identifier in seen:
                problems.append(f"Record {position}: duplicate id")
            seen.add(identifier)
            category = record.get("category")
            if not isinstance(category, str) or not category.strip():
                problems.append(f"Record {position}: invalid category")
            amount = record.get("amount")
            if not isinstance(amount, (int, float)):
                problems.append(f"Record {position}: invalid amount")
        return problems
""",
        "processor.ts": """export interface RecordItem {
  category: string;
  amount: number;
  enabled: boolean;
}

export class BatchProcessor {
  process(records: RecordItem[]): Map<string, number[]> {
    const groups = new Map<string, number[]>();
    for (const record of records) {
      if (!record.enabled) continue;
      const category = record.category.trim().toLowerCase() || "unknown";
      if (!Number.isFinite(record.amount) || record.amount < 0) continue;
      const amount = Math.round(record.amount * 100);
      const values = groups.get(category) ?? [];
      values.push(amount);
      groups.set(category, values);
    }
    for (const values of groups.values()) {
      values.sort((left, right) => left - right);
    }
    return groups;
  }

  summarize(groups: Map<string, number[]>): Map<string, number> {
    const summary = new Map<string, number>();
    for (const [category, values] of groups) {
      let total = 0;
      for (const value of values) total += value;
      summary.set(category, total);
    }
    return summary;
  }

  report(groups: Map<string, number[]>): string[] {
    const summary = this.summarize(groups);
    const categories = [...summary.keys()].sort();
    const lines: string[] = ["Batch processing report"];
    let grandTotal = 0;
    for (const category of categories) {
      const values = groups.get(category) ?? [];
      const total = summary.get(category) ?? 0;
      grandTotal += total;
      const count = values.length;
      const minimum = count ? Math.min(...values) : 0;
      const maximum = count ? Math.max(...values) : 0;
      const average = count ? Math.floor(total / count) : 0;
      lines.push(`Category: ${category}`);
      lines.push(`  Count: ${count}`);
      lines.push(`  Total: ${total}`);
      lines.push(`  Minimum: ${minimum}`);
      lines.push(`  Maximum: ${maximum}`);
      lines.push(`  Average: ${average}`);
    }
    lines.push(`Grand total: ${grandTotal}`);
    return lines;
  }

  validate(records: RecordItem[]): string[] {
    const problems: string[] = [];
    const seen = new Set<string>();
    for (const [position, record] of records.entries()) {
      if (!record.category.trim()) {
        problems.push(`Record ${position}: missing category`);
      }
      if (!Number.isFinite(record.amount)) {
        problems.push(`Record ${position}: invalid amount`);
      }
      const key = `${record.category}:${record.amount}`;
      if (seen.has(key)) {
        problems.push(`Record ${position}: duplicate category and amount`);
      }
      seen.add(key);
      if (record.enabled && record.amount < 0) {
        problems.push(`Record ${position}: negative enabled amount`);
      }
    }
    return problems;
  }
}
""",
        "processor.go": """package service

import (
    "sort"
    "strconv"
)

type Record struct {
    Category string
    Amount int
    Enabled bool
}

func Process(records []Record) map[string][]int {
    grouped := make(map[string][]int)
    for _, record := range records {
        if !record.Enabled || record.Amount < 0 {
            continue
        }
        category := record.Category
        if category == "" {
            category = "unknown"
        }
        values := grouped[category]
        values = append(values, record.Amount)
        grouped[category] = values
    }
    for category, values := range grouped {
        sort.Ints(values)
        grouped[category] = values
    }
    return grouped
}

func Totals(grouped map[string][]int) map[string]int {
    totals := make(map[string]int)
    for category, values := range grouped {
        sum := 0
        for _, value := range values {
            sum += value
        }
        totals[category] = sum
    }
    return totals
}

func Report(grouped map[string][]int) []string {
    totals := Totals(grouped)
    categories := make([]string, 0, len(totals))
    for category := range totals {
        categories = append(categories, category)
    }
    sort.Strings(categories)
    lines := []string{"Batch processing report"}
    grandTotal := 0
    for _, category := range categories {
        values := grouped[category]
        total := totals[category]
        grandTotal += total
        minimum, maximum := 0, 0
        if len(values) > 0 {
            minimum = values[0]
            maximum = values[len(values)-1]
        }
        lines = append(lines, category)
        lines = append(lines, strconv.Itoa(len(values)))
        lines = append(lines, strconv.Itoa(total))
        lines = append(lines, strconv.Itoa(minimum))
        lines = append(lines, strconv.Itoa(maximum))
    }
    lines = append(lines, strconv.Itoa(grandTotal))
    return lines
}

func Validate(records []Record) []string {
    problems := make([]string, 0)
    seen := make(map[string]bool)
    for position, record := range records {
        if record.Category == "" {
            problems = append(problems, strconv.Itoa(position)+": missing category")
        }
        if record.Amount < 0 {
            problems = append(problems, strconv.Itoa(position)+": negative amount")
        }
        key := record.Category + ":" + strconv.Itoa(record.Amount)
        if seen[key] {
            problems = append(problems, strconv.Itoa(position)+": duplicate record")
        }
        seen[key] = true
        if !record.Enabled && record.Amount > 0 {
            problems = append(problems, strconv.Itoa(position)+": disabled amount")
        }
    }
    return problems
}
""",
        "processor.rs": """pub struct Record {
    pub category: String,
    pub amount: i64,
    pub enabled: bool,
}

pub fn process(records: &[Record]) -> std::collections::BTreeMap<String, Vec<i64>> {
    let mut grouped = std::collections::BTreeMap::new();
    for record in records {
        if !record.enabled || record.amount < 0 {
            continue;
        }
        let category = if record.category.is_empty() {
            "unknown".to_string()
        } else {
            record.category.trim().to_lowercase()
        };
        grouped.entry(category).or_insert_with(Vec::new).push(record.amount);
    }
    for values in grouped.values_mut() {
        values.sort();
    }
    grouped
}

pub fn totals(grouped: &std::collections::BTreeMap<String, Vec<i64>>)
    -> std::collections::BTreeMap<String, i64> {
    let mut totals = std::collections::BTreeMap::new();
    for (category, values) in grouped {
        let mut sum = 0;
        for value in values {
            sum += value;
        }
        totals.insert(category.clone(), sum);
    }
    totals
}

pub fn report(grouped: &std::collections::BTreeMap<String, Vec<i64>>) -> Vec<String> {
    let totals = totals(grouped);
    let mut lines = vec!["Batch processing report".to_string()];
    let mut grand_total = 0;
    for (category, values) in grouped {
        let total = totals.get(category).copied().unwrap_or(0);
        grand_total += total;
        let count = values.len();
        let minimum = values.first().copied().unwrap_or(0);
        let maximum = values.last().copied().unwrap_or(0);
        let average = if count > 0 { total / count as i64 } else { 0 };
        lines.push(format!("Category: {}", category));
        lines.push(format!("  Count: {}", count));
        lines.push(format!("  Total: {}", total));
        lines.push(format!("  Minimum: {}", minimum));
        lines.push(format!("  Maximum: {}", maximum));
        lines.push(format!("  Average: {}", average));
    }
    lines.push(format!("Grand total: {}", grand_total));
    lines
}

pub fn validate(records: &[Record]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for (position, record) in records.iter().enumerate() {
        if record.category.trim().is_empty() {
            problems.push(position.to_string() + ": missing category");
        }
        if record.amount < 0 {
            problems.push(position.to_string() + ": negative amount");
        }
        let key = record.category.clone() + ":" + &record.amount.to_string();
        if !seen.insert(key) {
            problems.push(position.to_string() + ": duplicate record");
        }
        if !record.enabled && record.amount > 0 {
            problems.push(position.to_string() + ": disabled amount");
        }
    }
    problems
}
""",
    }
    for name, source in sources.items():
        (tmp_path / name).write_text(source)
    full = pack_repository(tmp_path, mode="full")
    skeleton = pack_repository(tmp_path, mode="skeleton")
    assert skeleton.file_count == full.file_count == 4
    assert all(file.is_skeleton for file in skeleton.files)
    assert "class BatchProcessor" in skeleton.bundle_text
    assert "interface RecordItem" in skeleton.bundle_text
    assert "func Process" in skeleton.bundle_text
    assert "pub fn process" in skeleton.bundle_text
    assert "grouped.entry(category)" not in skeleton.bundle_text
    assert "sort.Ints(values)" not in skeleton.bundle_text
    reduction = 100 * (full.packed_tokens - skeleton.packed_tokens) / full.packed_tokens
    assert reduction > 75, (full.packed_tokens, skeleton.packed_tokens, reduction)
