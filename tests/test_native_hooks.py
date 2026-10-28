import copy
import io
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from usagetrim.cli import app
from usagetrim.core import hooks, native_hooks, shell_shim

_SHELL_ROUTING_CASES = [
    (["pytest"], True),
    (["git"], False),
    (["docker"], False),
    (["cargo"], False),
    (["go"], False),
    (["pytest", "-q"], True),
    (["docker", "build", "."], True),
    (["docker", "compose", "build"], True),
    (["cargo", "test"], True),
    (["go", "test", "./..."], True),
    (["kubectl", "get", "pods"], True),
    (["kubectl", "describe", "pod", "api"], True),
    (["kubectl", "logs", "api"], True),
    (["kubectl", "--context", "dev", "get", "pods"], True),
    (["kubectl", "--context=dev", "-n", "default", "logs", "api"], True),
    (["kubectl", "proxy"], False),
    (["kubectl", "unknown-command"], False),
    (["kubectl"], False),
    (["kubectl", "--unknown", "dev", "get", "pods"], False),
    (["kubectl", "--context"], False),
    (["kubectl", "logs", "api", "-f"], False),
    (["kubectl", "logs", "api", "--follow"], False),
    (["kubectl", "logs", "api", "--follow=true"], False),
    (["kubectl", "logs", "api", "-ft"], False),
    (["kubectl", "get", "pods", "-w"], False),
    (["kubectl", "get", "pods", "--watch"], False),
    (["kubectl", "get", "pods", "--watch=true"], False),
    (["kubectl", "get", "pods", "--watch-only"], False),
    (["kubectl", "get", "pods", "-ow"], False),
    (["kubectl", "--context", "dev", "get", "pods", "-w"], False),
    (["kubectl", "get", "--raw=/api/v1/watch/pods"], False),
    (["terraform", "plan"], False),
    (["terraform", "plan", "-input=false"], True),
    (["terraform", "-chdir=infra", "plan", "-input=false"], True),
    (["terraform", "plan", "-input=true"], False),
    (["terraform", "plan", "-input=false", "-input=true"], False),
    (["terraform", "plan", "--", "-input=false"], False),
    (["terraform", "show"], True),
    (["terraform", "validate", "-json"], True),
    (["terraform", "-chdir=infra", "show"], True),
    (["terraform", "apply", "-auto-approve"], False),
    (["terraform", "destroy"], False),
    (["terraform", "import", "resource", "id"], False),
    (["terraform", "unknown-command"], False),
    (["terraform"], False),
    (["pytest", "--pdb"], False),
    (["pytest", "--trace"], False),
    (["pytest", "--pdbcls=IPython.terminal.debugger:TerminalPdb"], False),
    (["pytest", "-o", "addopts=--pdb"], False),
    (["pytest", "-oaddopts=--trace"], False),
    (["pytest", "--override-ini", "addopts=--pdb"], False),
    (["pytest", "--override-ini=addopts=--trace"], False),
    (["pytest", "--", "-o", "addopts=--pdb"], True),
    (["pytest", "--", "--pdb"], True),
    (["kubectl", "exec", "pod", "--", "echo", "-it"], False),
    (["kubectl", "exec", "-i", "pod", "--", "sh"], False),
    (["kubectl", "exec", "-t", "pod", "--", "sh"], False),
    (["kubectl", "exec", "-it", "pod", "--", "sh"], False),
    (["kubectl", "exec", "-ti", "pod", "--", "sh"], False),
    (["kubectl", "exec", "pod", "--stdin", "--", "sh"], False),
    (["kubectl", "exec", "pod", "--tty", "--", "sh"], False),
    (["kubectl", "exec", "pod", "--stdin=true", "--tty=true"], False),
    (["kubectl", "attach", "pod"], False),
    (["kubectl", "port-forward", "pod", "8080:80"], False),
    (["kubectl", "--namespace", "default", "exec", "-it", "pod"], False),
    (["kubectl", "--context=dev", "attach", "pod"], False),
    (["terraform", "console"], False),
    (["terraform", "-chdir=infra", "console"], False),
    (["git", "diff", "--staged"], True),
    (["git", "commit", "-m", "message"], False),
    (["git", "add", "."], False),
    (["docker", "run", "-it", "image"], False),
    (["docker", "compose build"], False),
    (["cargo", "build"], False),
    (["go", "run", "."], False),
    (["ssh", "host"], False),
    (["vim"], False),
    (["nvim"], False),
    (["nano"], False),
    (["top"], False),
    (["htop"], False),
    (["cd", "/tmp"], False),
    (["usagetrim", "run", "--", "pytest"], False),
    (["/usr/bin/usagetrim", "run", "--", "git", "diff"], False),
    ([], False),
]


@pytest.mark.parametrize("argv,expected", _SHELL_ROUTING_CASES)
def test_shell_interception_policy(argv, expected):
    assert hooks.should_intercept_command(argv) is expected


def test_shell_interception_does_not_parse_command_strings():
    assert not hooks.should_intercept_command("pytest -q")
    assert not hooks.should_intercept_command(["pytest; touch PWNED"])


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_shell_wrapper_routes_argv_without_recursion_or_interpretation(tmp_path, shell):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    snippet = hooks.generate_shell_wrapper(shell)
    assert "eval" not in snippet
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in {argv[0] for argv, _ in _SHELL_ROUTING_CASES if argv} | {"usagetrim"}:
        if "/" in name or name == "cd":
            continue
        path = bin_dir / name
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "print(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]))\n"
            "sys.exit(17)\n"
        )
        path.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
    cases = [(argv, expected) for argv, expected in _SHELL_ROUTING_CASES if argv]
    special = ["space name", "$(touch PWNED)", "; touch PWNED", "*", "", "a\nb", "'\"\\"]
    cases.extend([(["pytest", *special], True), (["git", "commit", *special], False)])
    for argv, expected in cases:
        if argv[0] == "cd" or "/" in argv[0]:
            continue
        result = subprocess.run(
            [
                executable,
                *(["--no-config"] if shell == "fish" else []),
                "-c",
                snippet + ("\n$argv" if shell == "fish" else '\n"$@"'),
                *([] if shell == "fish" else ["wrapper-test"]),
                *argv,
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 17, result.stderr
        assert json.loads(result.stdout) == (
            ["usagetrim", "run", "--", *argv] if expected else argv
        )
    assert not (tmp_path / "PWNED").exists()


def test_shell_wrapper_generator_validates_shell_and_uses_no_eval():
    for shell in ("bash", "zsh", "fish", "powershell"):
        snippet = hooks.generate_shell_wrapper(shell)
        assert "eval" not in snippet
        assert "unalias" not in snippet
    with pytest.raises(ValueError, match="bash, zsh, fish or powershell"):
        hooks.generate_shell_wrapper("unknown")


def test_fish_wrapper_generator_is_native_and_guards_all_command_names():
    snippet = hooks.generate_shell_wrapper("fish")
    assert "function _usagetrim_shell_intercept\n" in snippet
    assert "command usagetrim run -- $argv" in snippet
    assert "command $argv" in snippet
    assert '"$@"' not in snippet
    for name in ("git", "docker", "pytest", "cargo", "go", "kubectl", "terraform"):
        assert f"if not functions -q {name}\n    function {name}\n" in snippet


def test_powershell_wrapper_generator_uses_native_path_interception():
    snippet = hooks.generate_shell_wrapper("powershell")
    for forbidden in (
        "invoke-expression",
        "iex",
        "cmd /c",
        "function ",
        "$psnativecommandargumentpassing",
    ):
        assert forbidden not in snippet.lower()
    assert "usagetrim.core.shell_shim --prepare" in snippet
    assert "$env:PATH =" in snippet
    assert "@CommandArgs" not in snippet


@pytest.mark.skipif(os.name == "nt", reason="POSIX virtualenv interpreter symlink")
def test_powershell_wrapper_bootstrap_preserves_virtualenv_python(tmp_path, monkeypatch):
    python = tmp_path / "virtual env" / "python"
    python.parent.mkdir()
    python.symlink_to(sys.executable)
    monkeypatch.setattr(hooks.sys, "executable", str(python))
    snippet = hooks.generate_shell_wrapper("powershell")
    quoted = str(python.absolute()).replace("'", "''")
    assert f"& '{quoted}' -m usagetrim.core.shell_shim --prepare" in snippet
    assert str(python.resolve()).replace("'", "''") not in snippet


def _powershell_assert_shims_active(*names):
    commands = ", ".join(f"'{name}'" for name in names)
    return """
$shimDirectory = Join-Path ([IO.Path]::GetDirectoryName($env:WRAPPER_FILE)) ('.usagetrim-shims-' + [IO.Path]::GetFileName($env:WRAPPER_FILE))
$manifestPath = Join-Path $shimDirectory '.usagetrim-native-shims.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Shim manifest missing' }
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$firstPath = ($env:PATH -split [regex]::Escape([string][IO.Path]::PathSeparator))[0]
if ($firstPath -ne $shimDirectory) { throw "Shim directory is not first in PATH: $firstPath" }
foreach ($name in @(__COMMANDS__)) {
    $file = if ($IsWindows) { $name + '.exe' } else { $name }
    $expected = Join-Path $shimDirectory $file
    if (-not (Test-Path -LiteralPath $expected -PathType Leaf)) { throw "Shim launcher missing: $expected" }
    if ($manifest.files.PSObject.Properties.Name -notcontains $file) { throw "Unmanaged launcher: $file" }
    $application = @(Get-Command -Name $name -CommandType Application -All -ErrorAction Stop)[0]
    if ($application.Path -ne $expected) { throw "Unexpected application for ${name}: $($application.Path)" }
}
""".replace("__COMMANDS__", commands)


@pytest.mark.parametrize(
    "platform,relative",
    [
        ("win32", "Documents/PowerShell/Microsoft.PowerShell_profile.ps1"),
        ("linux", ".config/powershell/Microsoft.PowerShell_profile.ps1"),
        ("darwin", ".config/powershell/Microsoft.PowerShell_profile.ps1"),
    ],
)
def test_powershell_shell_profile_default_path(tmp_path, monkeypatch, platform, relative):
    monkeypatch.setattr(hooks.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(hooks.sys, "platform", platform)
    expected = tmp_path / relative
    assert hooks.install_shell_wrapper("powershell") == expected
    assert expected.is_file()
    hooks.uninstall_shell_wrapper("powershell")
    assert not expected.exists()


def test_powershell_shell_wrapper_routes_argv_and_preserves_native_arguments(tmp_path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    snippet = tmp_path / "wrapper.ps1"
    snippet.write_text(hooks.generate_shell_wrapper("powershell"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in {argv[0] for argv, _ in _SHELL_ROUTING_CASES if argv} | {"usagetrim"}:
        if "/" in name or name == "cd":
            continue
        path = bin_dir / name
        path.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            "print(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]))\n"
            "sys.exit(17)\n"
        )
        path.chmod(0o755)
    special = ["space name", "$(touch PWNED)", "; touch PWNED", "", "a\nb", "'\"\\"]
    cases = [
        *_SHELL_ROUTING_CASES,
        (["pytest", *special], True),
        (["git", "commit", *special], False),
    ]
    case_path = tmp_path / "cases.json"
    case_path.write_text(
        json.dumps([{"argv": argv, "expected": expected} for argv, expected in cases])
    )
    script = tmp_path / "test.ps1"
    script.write_text(
        """
$ErrorActionPreference = 'Stop'
$null = Get-Command pytest,git,docker -CommandType Application -All -ErrorAction Stop
. $env:WRAPPER_FILE
"""
        + _powershell_assert_shims_active("pytest", "git", "docker")
        + """
$cases = Get-Content -Raw -LiteralPath $env:CASE_FILE | ConvertFrom-Json
foreach ($case in $cases) {
    [string[]]$commandArgv = @($case.argv)
    if ($commandArgv.Count -eq 0 -or $commandArgv[0] -eq 'cd' -or $commandArgv[0].Contains('/')) { continue }
    $tail = @()
    if ($commandArgv.Count -gt 1) { $tail = @($commandArgv[1..($commandArgv.Count - 1)]) }
    & $commandArgv[0] @tail
    if ($LASTEXITCODE -ne 17) { throw 'Exit code changed' }
}
exit 0
"""
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "WRAPPER_FILE": str(snippet),
            "CASE_FILE": str(case_path),
        },
    )
    assert result.returncode == 0, result.stderr
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        ["usagetrim", "run", "--", *argv] if expected else argv
        for argv, expected in cases
        if argv and argv[0] != "cd" and "/" not in argv[0]
    ]
    assert not (tmp_path / "PWNED").exists()


@pytest.mark.parametrize("case", ["pipeline", "failure", "quoted_pathspec"])
def test_powershell_literal_native_command_semantics(tmp_path, executable, case):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip(f"pwsh is not installed: literal {case} regression")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    (repo / "nested").mkdir(parents=True)
    (repo / "root.py").write_text("root = True\n")
    (repo / "nested" / "child.py").write_text("child = True\n")
    subprocess.run([git, "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run([git, "-C", str(repo), "add", "."], check=True, capture_output=True)
    profile = tmp_path / "wrapper.ps1"
    profile.write_text(hooks.generate_shell_wrapper("powershell"))
    commands = {
        "pipeline": "'review-pipeline-payload' | git hash-object --stdin\n",
        "failure": """git definitely-invalid-review-command 2>$null && Write-Output 'WRONG-SUCCESS-BRANCH'
$nativeSuccess = $?
$nativeCode = $LASTEXITCODE
Write-Output ("{0}|{1}" -f $nativeSuccess, $nativeCode)
""",
        "quoted_pathspec": "git ls-files '*.py'\n",
    }
    outputs = []
    for wrapped in (False, True):
        script = tmp_path / ("wrapped.ps1" if wrapped else "native.ps1")
        activation = (
            "$null = Get-Command git -CommandType Application -All -ErrorAction Stop\n"
            ". $env:WRAPPER_FILE\n"
            + _powershell_assert_shims_active("git")
            + """
$probe = @(git diff --usagetrim-activation-probe)
if ($LASTEXITCODE -ne 0 -or ($probe | ConvertTo-Json -Compress) -cne '["run","--","git","diff","--usagetrim-activation-probe"]') {
    throw 'Interception probe did not pass through usagetrim run'
}
"""
            if wrapped
            else ""
        )
        script.write_text(activation + commands[case])
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=15,
            env={
                **os.environ,
                "PATH": str(executable.parent) + os.pathsep + os.environ["PATH"],
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "WRAPPER_FILE": str(profile),
            },
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.splitlines())
    assert outputs[1] == outputs[0]
    if case == "pipeline":
        assert len(outputs[0]) == 1 and len(outputs[0][0]) == 40
    elif case == "failure":
        assert outputs[0] == ["False|1"]
    else:
        assert outputs[0] == ["nested/child.py", "root.py"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX native exec bootstrap")
@pytest.mark.parametrize(
    "argv,intercept",
    [
        (["docker", "build", "."], True),
        (["pytest"], True),
        (["git", "ls-files", "*.py"], False),
    ],
)
def test_native_powershell_shim_preserves_os_argv_and_exit_code(
    tmp_path, monkeypatch, argv, intercept
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("docker", "pytest", "git", "usagetrim"):
        executable = bin_dir / name
        executable.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            "print(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]))\n"
            "sys.exit(17)\n"
        )
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    profile = tmp_path / "profile.ps1"
    hooks.install_shell_wrapper("powershell", profile)
    directory = tmp_path / ".usagetrim-shims-profile.ps1"
    shell_shim.prepare_powershell_shims(directory)
    initial = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    shell_shim.prepare_powershell_shims(directory)
    assert initial == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    special = ["space name", "", "a\nb", "'\"\\", "$(touch PWNED)", "; touch PWNED", "*", "-flag"]
    result = subprocess.run(
        [str(directory / argv[0]), *argv[1:], *special],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode == 17, result.stderr
    expected = (["usagetrim", "run", "--", *argv] if intercept else argv) + special
    assert json.loads(result.stdout) == expected
    assert not (tmp_path / "PWNED").exists()
    hooks.uninstall_shell_wrapper("powershell", profile)
    assert not directory.exists()
    assert not profile.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX native exec bootstrap")
def test_native_powershell_shim_forwards_stdin_and_literal_git_pathspec(tmp_path, monkeypatch):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    repo = tmp_path / "repo"
    (repo / "nested").mkdir(parents=True)
    (repo / "root.py").write_text("root = True\n")
    (repo / "nested" / "child.py").write_text("child = True\n")
    subprocess.run([git, "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run([git, "-C", str(repo), "add", "."], check=True, capture_output=True)
    directory = tmp_path / ".usagetrim-shims-profile.ps1"
    shell_shim.prepare_powershell_shims(directory)
    for args, stdin in [
        (["hash-object", "--stdin"], "review-pipeline-payload\n"),
        (["ls-files", "*.py"], None),
        (["definitely-invalid-review-command"], None),
    ]:
        direct = subprocess.run([git, *args], input=stdin, cwd=repo, capture_output=True, text=True)
        wrapped = subprocess.run(
            [str(directory / "git"), *args],
            input=stdin,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert (wrapped.stdout, wrapped.stderr, wrapped.returncode) == (
            direct.stdout,
            direct.stderr,
            direct.returncode,
        )


def test_native_powershell_shim_windows_launcher_normalizes_exe_name(tmp_path, monkeypatch):
    launcher = tmp_path / "git.exe"
    launcher.write_bytes(b"owned launcher")
    (tmp_path / ".usagetrim-native-shims.json").write_text(
        json.dumps({"version": 1, "files": {"git.exe": shell_shim._digest(launcher)}})
    )
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "git"), "ls-files", "*.py"])
    called = []

    def dispatch(path, args):
        called.append((path, args))
        return 17

    monkeypatch.setattr(shell_shim, "run_native_shim", dispatch)
    with pytest.raises(SystemExit) as exc:
        shell_shim.dispatch_if_native_shim()
    assert exc.value.code == 17
    assert called == [(tmp_path / "git.exe", ["ls-files", "*.py"])]


@pytest.mark.parametrize("name", ["usagetrim", "cli.py", "pytest", "git"])
def test_native_shim_activation_requires_owned_file(tmp_path, monkeypatch, name):
    executable = tmp_path / name
    executable.write_bytes(b"unrelated executable")
    marker = tmp_path / shell_shim._MANIFEST
    marker.write_text(json.dumps({"version": 1, "files": {"git": "0" * 64}}))
    monkeypatch.setattr(sys, "argv", [str(executable)])

    def unexpected(*args):
        pytest.fail("unowned executable activated shim dispatch")

    monkeypatch.setattr(shell_shim, "run_native_shim", unexpected)
    assert shell_shim.dispatch_if_native_shim() is False
    marker.write_text("invalid JSON")
    assert shell_shim.dispatch_if_native_shim() is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX native exec bootstrap")
def test_native_shim_lookup_excludes_owned_paths_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    first = tmp_path / ".usagetrim-shims-first.ps1"
    second = tmp_path / ".usagetrim-shims-second.ps1"
    for directory in (first, second):
        shell_shim.prepare_powershell_shims(directory)
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    user = tmp_path / ".usagetrim-shims-user"
    user.mkdir()
    (user / shell_shim._MANIFEST).write_text("{}")
    (user / "git").write_text("#!/bin/sh\nprintf 'user executable'\nexit 19\n")
    (user / "git").chmod(0o700)
    # Empty/relative entries and symlink aliases must not find another shim.
    path = os.pathsep.join([str(first), "", first.name, str(alias), str(second), user.name])
    result = subprocess.run(
        [str(first / "git"), "commit"],
        cwd=tmp_path,
        env={**os.environ, "PATH": path},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode, result.stdout, result.stderr) == (19, "user executable", "")
    result = subprocess.run(
        [str(first / "git"), "commit"],
        env={**os.environ, "PATH": os.pathsep.join([str(first), str(second)])},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 127
    assert "original executable unavailable" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows console launchers")
def test_native_shim_windows_exe_launcher_and_batch_passthrough(tmp_path, monkeypatch):
    git = shutil.which("git")
    if git is None or Path(git).suffix.lower() != ".exe":
        pytest.skip("native git.exe is not installed")
    bin_dir = tmp_path / "batch files"
    bin_dir.mkdir()
    for name, extension in [("docker", ".cmd"), ("kubectl", ".bat")]:
        (bin_dir / (name + extension)).write_text("@exit /b 23\n")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    directory = tmp_path / ".usagetrim-shims-profile.ps1"
    # This executes the actual copied usagetrim.exe, not a mocked Windows path.
    shell_shim.prepare_powershell_shims(directory)
    assert not (directory / "docker.exe").exists()
    assert not (directory / "kubectl.exe").exists()
    expected = subprocess.run([git, "--version"], capture_output=True)
    result = subprocess.run(
        [str(directory / "GIT.EXE"), "--version"], capture_output=True, timeout=20
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        expected.returncode,
        expected.stdout,
        expected.stderr,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX native exec bootstrap")
def test_native_shim_preserves_sole_empty_path_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    directory = tmp_path / ".usagetrim-shims-profile.ps1"
    shell_shim.prepare_powershell_shims(directory)
    git = tmp_path / "git"
    git.write_text("#!/bin/sh\nprintf cwd-git\n")
    git.chmod(0o700)
    result = subprocess.run(
        [str(directory / "git"), "commit"],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(directory) + os.pathsep},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "cwd-git", "")


@pytest.mark.skipif(os.name == "nt", reason="POSIX native exec bootstrap")
def test_native_powershell_shim_refuses_user_modifications_and_preserves_user_files(tmp_path):
    directory = tmp_path / ".usagetrim-shims-profile.ps1"
    shell_shim.prepare_powershell_shims(directory)
    user_file = directory / "user-note.txt"
    user_file.write_bytes(b"keep me")
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(ValueError, match="user modifications"):
        shell_shim.prepare_powershell_shims(directory)
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
    shell_shim.remove_powershell_shims(tmp_path / "profile.ps1")
    assert list(directory.iterdir()) == [user_file]
    assert user_file.read_bytes() == b"keep me"


def test_powershell_shell_wrapper_preserves_alias_and_function(tmp_path, executable):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    profile = tmp_path / "profile.ps1"
    hooks.install_shell_wrapper("powershell", profile)
    script = tmp_path / "test.ps1"
    script.write_text(
        """
$ErrorActionPreference = 'Stop'
function UserGit { 'user-alias' }
Set-Alias -Name git -Value UserGit
function pytest { 'user-function' }
. $env:WRAPPER_FILE
"""
        + _powershell_assert_shims_active("docker")
        + """
git diff
pytest -q
docker build .
exit 0
"""
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        text=True,
        capture_output=True,
        timeout=5,
        env={
            **os.environ,
            "PATH": str(executable.parent) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "WRAPPER_FILE": str(profile),
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "user-alias",
        "user-function",
        "run",
        "--",
        "docker",
        "build",
        ".",
    ]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
@pytest.mark.parametrize(
    "original", [b"", b"# user settings\n", b"export USER_FLAG=1", "# café\r\n".encode()]
)
def test_shell_wrapper_install_uninstall_preserves_bytes_and_permissions(tmp_path, shell, original):
    rc = tmp_path / "rc"
    rc.write_bytes(original)
    rc.chmod(0o640)
    assert hooks.install_shell_wrapper(shell, rc) == rc
    installed = rc.read_bytes()
    assert installed.count(b"# >>> usagetrim shell wrapper >>>") == 1
    assert installed.count(b"# <<< usagetrim shell wrapper <<<") == 1
    assert stat.S_IMODE(rc.stat().st_mode) == 0o640
    before = rc.stat().st_mtime_ns
    hooks.install_shell_wrapper(shell, rc)
    assert rc.read_bytes() == installed and rc.stat().st_mtime_ns == before
    hooks.uninstall_shell_wrapper(shell, rc)
    assert rc.read_bytes() == original
    assert stat.S_IMODE(rc.stat().st_mode) == 0o640
    before = rc.stat().st_mtime_ns
    hooks.uninstall_shell_wrapper(shell, rc)
    assert rc.read_bytes() == original and rc.stat().st_mtime_ns == before


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
def test_shell_wrapper_default_rc_is_private_and_uninstall_missing_is_noop(
    tmp_path, monkeypatch, shell
):
    monkeypatch.setattr(hooks.Path, "home", lambda: tmp_path)
    rc = tmp_path / (
        {
            "fish": ".config/fish/config.fish",
            "powershell": (
                "Documents/PowerShell/Microsoft.PowerShell_profile.ps1"
                if sys.platform == "win32"
                else ".config/powershell/Microsoft.PowerShell_profile.ps1"
            ),
        }.get(shell, f".{shell}rc")
    )
    assert hooks.uninstall_shell_wrapper(shell) == rc
    assert not rc.exists()
    assert hooks.install_shell_wrapper(shell) == rc
    assert stat.S_IMODE(rc.stat().st_mode) == 0o600
    installed, mtime = rc.read_bytes(), rc.stat().st_mtime_ns
    hooks.install_shell_wrapper(shell)
    assert rc.read_bytes() == installed and rc.stat().st_mtime_ns == mtime
    hooks.uninstall_shell_wrapper(shell)
    assert not rc.exists()
    hooks.uninstall_shell_wrapper(shell)
    assert not rc.exists()


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
def test_shell_wrapper_created_rc_preserves_appended_user_content(tmp_path, shell):
    rc = tmp_path / "rc"
    hooks.install_shell_wrapper(shell, rc)
    user_content = b"export USER_FLAG=1\n# keep this"
    with rc.open("ab") as stream:
        stream.write(user_content)
    hooks.uninstall_shell_wrapper(shell, rc)
    assert rc.read_bytes() == user_content


@pytest.mark.parametrize(
    "provenance",
    [
        b"# usagetrim: rc-created-by-installer=0\n",
        b"# usagetrim: rc-created-by-installer=1 extra\n",
        b"# usagetrim: rc-created-by-installer=1\n" * 2,
        b"# old wrapper\n# usagetrim: rc-created-by-installer=1\n",
    ],
)
def test_shell_wrapper_malformed_provenance_preserves_rc(tmp_path, provenance):
    rc = tmp_path / "rc"
    content = (
        b"# >>> usagetrim shell wrapper >>>\n" + provenance + b"# <<< usagetrim shell wrapper <<<\n"
    )
    rc.write_bytes(content)
    before = rc.stat().st_mtime_ns
    for operation in (hooks.install_shell_wrapper, hooks.uninstall_shell_wrapper):
        with pytest.raises(ValueError, match="provenance"):
            operation("bash", rc)
        assert rc.read_bytes() == content and rc.stat().st_mtime_ns == before


def test_shell_wrapper_updates_single_old_block_without_changing_user_content(tmp_path):
    rc = tmp_path / "rc"
    prefix, suffix = b"alias custom='echo keep'\n", b"export USER_FLAG=1"
    rc.write_bytes(
        prefix + b"# >>> usagetrim shell wrapper >>>\n# old wrapper\n"
        b"# <<< usagetrim shell wrapper <<<\n" + suffix
    )
    hooks.install_shell_wrapper("bash", rc)
    updated = rc.read_bytes()
    assert updated == (
        prefix
        + b"# >>> usagetrim shell wrapper >>>\n"
        + hooks.generate_shell_wrapper("bash").encode()
        + b"# <<< usagetrim shell wrapper <<<\n"
        + suffix
    )
    hooks.uninstall_shell_wrapper("bash", rc)
    assert rc.read_bytes() == prefix + suffix


@pytest.mark.parametrize(
    "content",
    [
        b"# >>> usagetrim shell wrapper >>>\n",
        b"# <<< usagetrim shell wrapper <<<\n",
        b"# <<< usagetrim shell wrapper <<<\n# >>> usagetrim shell wrapper >>>\n",
        (b"# >>> usagetrim shell wrapper >>>\n# <<< usagetrim shell wrapper <<<\n") * 2,
        b"# >>> usagetrim shell wrapper >>> trailing\n# <<< usagetrim shell wrapper <<<\n",
        b"# >>> usagetrim shell wrapper >>>\n# usagetrim: preceding newline added\n# <<< usagetrim shell wrapper <<<\n",
    ],
)
def test_shell_wrapper_invalid_markers_never_modify_rc(tmp_path, content):
    rc = tmp_path / "rc"
    rc.write_bytes(content)
    before = rc.stat().st_mtime_ns
    for operation in (hooks.install_shell_wrapper, hooks.uninstall_shell_wrapper):
        with pytest.raises(ValueError):
            operation("bash", rc)
        assert rc.read_bytes() == content and rc.stat().st_mtime_ns == before
    assert list(tmp_path.iterdir()) == [rc]


@pytest.mark.parametrize("operation", ["replace", "fsync", "chmod"])
def test_shell_wrapper_atomic_replace_failure_preserves_rc_and_cleans_staged_file(
    tmp_path, monkeypatch, operation
):
    rc = tmp_path / "rc"
    rc.write_bytes(b"# original without newline")

    def fail_replace(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(hooks.os, operation, fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        hooks.install_shell_wrapper("bash", rc)
    assert rc.read_bytes() == b"# original without newline"
    assert list(tmp_path.iterdir()) == [rc]


def test_shell_wrapper_refuses_symlink_rc(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"keep\n")
    rc = tmp_path / "rc"
    rc.symlink_to(target)
    for operation in (hooks.install_shell_wrapper, hooks.uninstall_shell_wrapper):
        with pytest.raises(ValueError, match="symlink"):
            operation("bash", rc)
    assert target.read_bytes() == b"keep\n" and rc.is_symlink()


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be", "utf-32", "cp1252"])
def test_powershell_profile_unsupported_encoding_is_never_modified(tmp_path, encoding):
    rc = tmp_path / "profile.ps1"
    original = 'Write-Output "café"\r\n'.encode(encoding)
    rc.write_bytes(original)
    before = rc.stat().st_mtime_ns
    for operation in (hooks.install_shell_wrapper, hooks.uninstall_shell_wrapper):
        with pytest.raises(ValueError, match="must be UTF-8"):
            operation("powershell", rc)
        assert rc.read_bytes() == original and rc.stat().st_mtime_ns == before
    assert list(tmp_path.iterdir()) == [rc]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
@pytest.mark.parametrize("helper", ["_usagetrim_shell_intercept", "_usagetrim_shell_dispatch"])
def test_shell_wrapper_preserves_preexisting_helpers(tmp_path, shell, helper):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    if shell == "fish":
        before = f"function {helper}\n    printf '%s\\n' USER-HELPER\nend\n"
        after = f"\n{helper}\nfunctions -q pytest; and exit 91\nexit 0\n"
    else:
        before = f"function {helper} {{ printf '%s\\n' USER-HELPER; }}\n"
        check = "builtin declare -F pytest" if shell == "bash" else "(( ${+functions[pytest]} ))"
        after = f"\n{helper}\nif {check} >/dev/null; then exit 91; fi\n"
    result = subprocess.run(
        [
            executable,
            *(["--no-config"] if shell == "fish" else []),
            "-c",
            before + hooks.generate_shell_wrapper(shell) + after,
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "USER-HELPER\n"


def test_powershell_shell_wrapper_preserves_preexisting_helpers(tmp_path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    profile = tmp_path / "profile.ps1"
    hooks.install_shell_wrapper("powershell", profile)
    script = tmp_path / "test.ps1"
    script.write_text("""
$ErrorActionPreference = 'Stop'
function Test-UsageTrimShellIntercept { 'USER-HELPER' }
. $env:WRAPPER_FILE
Test-UsageTrimShellIntercept
if (Get-Command -Name pytest -CommandType Function -ErrorAction SilentlyContinue) { exit 91 }
exit 0
""")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "WRAPPER_FILE": str(profile),
        },
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "USER-HELPER\n"


def test_bash_shell_wrapper_preserves_helper_alias_without_expansion(tmp_path):
    script = (
        "shopt -s expand_aliases\n"
        "alias _usagetrim_shell_dispatch='printf \"%s\\n\" USER-ALIAS'\n"
        + hooks.generate_shell_wrapper("bash")
        + "\n_usagetrim_shell_dispatch\n"
        + "if builtin declare -F pytest >/dev/null; then exit 91; fi\n"
    )
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, text=True, capture_output=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "USER-ALIAS\n"


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_shell_wrapper_preserves_existing_alias_and_function(tmp_path, executable, shell):
    shell_executable = shutil.which(shell)
    if shell_executable is None:
        pytest.skip(f"{shell} is not installed")
    rc = tmp_path / "rc"
    hooks.install_shell_wrapper(shell, rc)
    if shell == "fish":
        script = """
alias git 'printf "%s\\n" user-alias'
function pytest
    printf '%s\\n' user-function
end
source "$argv[1]"
git diff
pytest -q
docker build .
"""
    else:
        script = (
            ("shopt -s expand_aliases\n" if shell == "bash" else "")
            + """
alias git='printf "%s\\n" user-alias'
pytest() { printf '%s\\n' user-function; }
. "$1"
git diff
pytest -q
docker build .
"""
        )
    result = subprocess.run(
        [
            shell_executable,
            *(["--no-config"] if shell == "fish" else []),
            "-c",
            script,
            *([] if shell == "fish" else ["wrapper-test"]),
            str(rc),
        ],
        env={**os.environ, "PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "user-alias",
        "diff",
        "user-function",
        "run",
        "--",
        "docker",
        "build",
        ".",
    ]


def _event():
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -vv", "timeout": 30000},
        "tool_response": {
            "stdout": "".join(
                f"tests/test_api.py::test_case_{i} PASSED [ 80%]\n" for i in range(80)
            ),
            "stderr": "WARNING: endpoint unavailable\nTraceback:\n  app.py:42\n",
            "exitCode": 1,
            "interrupted": False,
            "isImage": False,
            "durationMs": 1234,
            "futureMetadata": {"nested": [1, "keep me", None]},
        },
    }


def test_claude_compacts_stdout_without_changing_failure_or_metadata():
    event = _event()
    before = copy.deepcopy(event)
    response = native_hooks.claude_post_tool_use(event)

    hook = response["hookSpecificOutput"]
    assert hook["hookEventName"] == "PostToolUse"
    result = hook["updatedToolOutput"]
    assert "80 passing tests" in result["stdout"]
    assert len(result["stdout"]) < len(before["tool_response"]["stdout"])
    assert {k: v for k, v in result.items() if k != "stdout"} == {
        k: v for k, v in before["tool_response"].items() if k != "stdout"
    }
    assert event == before
    assert set(response) == {"hookSpecificOutput"}
    assert set(hook) == {"hookEventName", "updatedToolOutput"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("hook_event_name", "PreToolUse"),
        ("hook_event_name", "PostToolUseFailure"),
        ("tool_name", "Read"),
        ("tool_name", "mcp__usagetrim__exec"),
        ("tool_response", "raw response"),
        ("tool_response", None),
        ("tool_input", []),
        ("tool_input", {"command": ["pytest"]}),
    ],
)
def test_unrecognized_event_fails_open(field, value):
    event = _event()
    event[field] = value
    assert native_hooks.claude_post_tool_use(event) == {}


@pytest.mark.parametrize("value", [None, [], "event", 0, {}])
def test_invalid_payload_fails_open(value):
    assert native_hooks.claude_post_tool_use(value) == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("interrupted", True),
        ("isImage", True),
        ("stdout", ["not text"]),
        ("stderr", {"error": "not text"}),
    ],
)
def test_unsupported_tool_output_is_not_partially_replaced(field, value):
    event = _event()
    event["tool_response"][field] = value
    assert native_hooks.claude_post_tool_use(event) == {}


def test_unchanged_short_output_does_not_add_context():
    event = _event()
    event["tool_response"]["stdout"] = "done\n"
    assert native_hooks.claude_post_tool_use(event) == {}


@pytest.mark.parametrize(
    "client,raw",
    [
        ("claude", "{bad json"),
        ("claude", ""),
        ("claude", "[]"),
        ("codex", json.dumps(_event())),
        ("claude", " " * (8 * 1024 * 1024 + 1)),
    ],
)
def test_protocol_fail_open_emits_only_empty_json(client, raw, capsys):
    output = io.StringIO()
    native_hooks.run_hook_filter(client, io.StringIO(raw), output)
    assert output.getvalue() == "{}\n"
    assert capsys.readouterr() == ("", "")


def test_filter_failure_does_not_break_hook_or_emit_logs(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError("unavailable local cache")

    monkeypatch.setattr(native_hooks, "safe_compact_output", fail)
    output = io.StringIO()
    native_hooks.run_hook_filter("claude", io.StringIO(json.dumps(_event())), output)
    assert output.getvalue() == "{}\n"
    assert capsys.readouterr() == ("", "")


def test_cli_hook_filter_outputs_one_valid_protocol_object():
    result = CliRunner().invoke(
        app, ["hook-filter", "--client", "claude"], input=json.dumps(_event())
    )
    assert result.exit_code == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["updatedToolOutput"]["exitCode"] == 1
    assert "80 passing tests" in response["hookSpecificOutput"]["updatedToolOutput"]["stdout"]


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
def test_cli_shell_hook_install_idempotent_and_uninstall(tmp_path, monkeypatch, shell):
    monkeypatch.setattr(hooks.Path, "home", lambda: tmp_path)
    rc = tmp_path / (
        {
            "fish": ".config/fish/config.fish",
            "powershell": (
                "Documents/PowerShell/Microsoft.PowerShell_profile.ps1"
                if sys.platform == "win32"
                else ".config/powershell/Microsoft.PowerShell_profile.ps1"
            ),
        }.get(shell, f".{shell}rc")
    )
    original = b"# KEEP_USER_SETTINGS"
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_bytes(original)
    runner = CliRunner()
    installed = runner.invoke(app, ["hook", "--install", "--client", shell])
    assert installed.exit_code == 0, installed.output
    assert str(rc) in installed.stdout.replace("\n", "")
    assert "Restart" in installed.stdout and "source" in installed.stdout
    content, mtime = rc.read_bytes(), rc.stat().st_mtime_ns
    assert b"# >>> usagetrim shell wrapper >>>" in content
    repeated = runner.invoke(app, ["hook", "--install", "--client", shell])
    assert repeated.exit_code == 0, repeated.output
    assert rc.read_bytes() == content and rc.stat().st_mtime_ns == mtime
    removed = runner.invoke(app, ["hook", "--uninstall", "--client", shell])
    assert removed.exit_code == 0, removed.output
    assert str(rc) in removed.stdout.replace("\n", "") and rc.read_bytes() == original


@pytest.mark.parametrize(
    "options,message",
    [
        (["--install", "--uninstall", "--client", "bash"], "mutually exclusive"),
        (["--install", "--client", "unknown"], "client must be"),
        (["--uninstall", "--client", "claude"], "only for bash, zsh, fish and powershell"),
        (["--uninstall", "--client", "shell"], "only for bash, zsh, fish and powershell"),
    ],
)
def test_cli_shell_hook_invalid_options_never_modify_files(tmp_path, monkeypatch, options, message):
    monkeypatch.setattr(hooks.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("COLUMNS", "240")
    for name in (".bashrc", ".zshrc"):
        (tmp_path / name).write_bytes(b"# keep\n")
    result = CliRunner().invoke(app, ["hook", *options], terminal_width=240)
    assert result.exit_code == 2
    assert message in result.output
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == {
        ".bashrc": b"# keep\n",
        ".zshrc": b"# keep\n",
    }


def test_cli_claude_hook_install_retains_native_flow(tmp_path, monkeypatch, executable):
    monkeypatch.setattr(native_hooks.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])
    result = CliRunner().invoke(app, ["hook", "--install", "--client", "claude"])
    assert result.exit_code == 0, result.output
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    assert {item["matcher"] for item in data["hooks"]["PostToolUse"]} == set(
        native_hooks.HOOK_MATCHERS
    )
    assert str(settings) in result.stdout.replace("\n", "")
    assert not (tmp_path / ".bashrc").exists()
    assert not (tmp_path / ".zshrc").exists()


def test_cli_shell_hook_legacy_alias_install_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.Path, "home", lambda: tmp_path)
    result = CliRunner().invoke(app, ["hook", "--install", "--client", "shell"])
    assert result.exit_code == 0, result.output
    content = (tmp_path / ".zshrc").read_text()
    assert 'alias cc-run="usagetrim run"' in content
    assert "# >>> usagetrim shell wrapper >>>" not in content


@pytest.fixture
def executable(tmp_path):
    path = tmp_path / "bin" / "usagetrim"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    path.chmod(0o755)
    return path


def test_install_preserves_hooks_permissions_and_original_backup(tmp_path, executable):
    settings = tmp_path / "settings.json"
    original = {
        "permissions": {"allow": ["Bash(git status)"], "deny": ["Read(.env)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "audit"}]}],
            "PostToolUse": [
                {"matcher": "Bash|Read", "hooks": [{"type": "command", "command": "existing"}]}
            ],
        },
        "env": {"LANG": "pl_PL.UTF-8"},
    }
    raw = json.dumps(original, ensure_ascii=False, indent=4) + "\n"
    settings.write_text(raw)
    settings.chmod(0o640)

    assert native_hooks.install_claude_hook(executable, settings) == settings
    installed = json.loads(settings.read_text())
    assert installed["permissions"] == original["permissions"]
    assert installed["env"] == original["env"]
    assert installed["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]
    added = len(native_hooks.HOOK_MATCHERS)
    assert installed["hooks"]["PostToolUse"][:-added] == original["hooks"]["PostToolUse"]
    assert [e["matcher"] for e in installed["hooks"]["PostToolUse"][-added:]] == [
        "^Bash$",
        "^mcp__",
    ]
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640
    backups = list(tmp_path.glob("settings.json.pre-usagetrim-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == raw
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o640

    first_bytes, first_mtime = settings.read_bytes(), settings.stat().st_mtime_ns
    native_hooks.install_claude_hook(executable, settings)
    assert settings.read_bytes() == first_bytes
    assert settings.stat().st_mtime_ns == first_mtime
    assert list(tmp_path.glob("settings.json.pre-usagetrim-*")) == backups


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        "[]",
        "null",
        '{"hooks": []}',
        '{"hooks": {"PostToolUse": {}}}',
        '{"hooks": {"PostToolUse": [{"matcher": "^Bash$", "hooks": null}]}}',
        '{"hooks": {"PostToolUse": [{"matcher": "^Bash$", "hooks": "bad"}]}}',
        '{"hooks": {"PostToolUse": [{"matcher": "^Bash$", "hooks": {}}]}}',
        '{"hooks": {"PostToolUse": [{"matcher": "^Bash$", "hooks": ["bad"]}]}}',
        '{"hooks": {"PostToolUse": ["bad"]}}',
        '{"hooks": {"PostToolUse": [{"matcher": "^Bash$"}]}}',
    ],
)
def test_malformed_settings_are_never_overwritten(tmp_path, executable, raw):
    settings = tmp_path / "settings.json"
    settings.write_text(raw)
    with pytest.raises((ValueError, TypeError)):
        native_hooks.install_claude_hook(executable, settings)
    assert settings.read_text() == raw
    assert list(tmp_path.glob("settings.json.pre-usagetrim-*")) == []


def test_new_install_is_private_and_shell_quotes_executable(tmp_path):
    executable = tmp_path / "space's $(touch PWNED) bin" / "usagetrim"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    executable.chmod(0o755)
    settings = tmp_path / "new claude home" / "settings.json"
    native_hooks.install_claude_hook(executable, settings)

    data = json.loads(settings.read_text())
    entry = data["hooks"]["PostToolUse"][0]
    assert entry["matcher"] == "^Bash$"
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    command = entry["hooks"][0]["command"]
    result = subprocess.run(
        command, shell=True, cwd=tmp_path, text=True, capture_output=True, check=True
    )
    assert result.stdout.splitlines() == ["hook-filter", "--client", "claude"]
    assert result.stderr == ""
    assert not (tmp_path / "PWNED").exists()


def test_missing_executable_does_not_change_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"permissions": {"deny": ["Bash(*)"]}}')
    before = settings.read_bytes()
    with pytest.raises(FileNotFoundError):
        native_hooks.install_claude_hook(tmp_path / "missing", settings)
    assert settings.read_bytes() == before
    assert list(tmp_path.glob("settings.json.pre-usagetrim-*")) == []
