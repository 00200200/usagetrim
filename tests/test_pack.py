from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from usagetrim.cli import app
from usagetrim.core.cache import ContextCache
from usagetrim.core.pack import pack_context, slice_repository_context


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
