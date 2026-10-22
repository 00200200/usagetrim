from __future__ import annotations

import os
import stat
import sys
import tempfile
from collections.abc import Sequence
from fnmatch import fnmatchcase, translate
from pathlib import Path

_SHELL_INTERCEPT_PREFIXES = (
    ("pytest",),
    ("docker", "build"),
    ("docker", "compose", "build"),
    ("cargo", "test"),
    ("go", "test"),
    ("git", "diff"),
)
_SHELL_FINITE_SUBCOMMANDS = {
    "kubectl": ("get", "describe", "logs"),
    "terraform": ("plan", "show", "validate"),
}
_SHELL_GLOBAL_VALUE_FLAGS = {
    "kubectl": ("--context", "--namespace", "-n", "--cluster", "--user", "--kubeconfig"),
    "terraform": (),
}
_SHELL_GLOBAL_INLINE_FLAGS = {
    "kubectl": tuple(flag + "=*" for flag in _SHELL_GLOBAL_VALUE_FLAGS["kubectl"]),
    "terraform": ("-chdir=*",),
}
_SHELL_PASSTHROUGH_ARGS = {
    "pytest": (
        "--pdb",
        "--pdb=*",
        "--trace",
        "--trace=*",
        "--pdbcls",
        "--pdbcls=*",
        # ini overrides can inject debugger options via addopts.
        "-o*",
        "--override-ini",
        "--override-ini=*",
    ),
    # Conservative even for explicit =false and ambiguous short-flag bundles.
    "kubectl": (
        "--follow",
        "--follow=*",
        "-f*",
        "-[!-]*f*",
        "--watch",
        "--watch=*",
        "--watch-only",
        "--watch-only=*",
        "-w*",
        "-[!-]*w*",
        "--raw",
        "--raw=*",
    ),
}


def _shell_options(argv: Sequence[str]) -> Sequence[str]:
    """Only options before -- belong to the intercepted command."""
    return argv[: argv.index("--")] if "--" in argv else argv


def should_intercept_command(argv: Sequence[str]) -> bool:
    """Match known finite argv forms; uncertain/global options fail open."""
    if isinstance(argv, (str, bytes)) or not argv:
        return False
    command, args = argv[0], argv[1:]
    if command in _SHELL_FINITE_SUBCOMMANDS:
        while args and args[0].startswith("-"):
            if args[0] in _SHELL_GLOBAL_VALUE_FLAGS[command]:
                if len(args) < 2:
                    return False
                args = args[2:]
            elif any(
                fnmatchcase(args[0], pattern) for pattern in _SHELL_GLOBAL_INLINE_FLAGS[command]
            ):
                args = args[1:]
            else:
                return False
        if not args or args[0] not in _SHELL_FINITE_SUBCOMMANDS[command]:
            return False
        if command == "terraform" and args[0] == "plan":
            inputs = [arg for arg in _shell_options(args[1:]) if arg.startswith("-input")]
            if not inputs or any(arg != "-input=false" for arg in inputs):
                return False
    elif not any(tuple(argv[: len(prefix)]) == prefix for prefix in _SHELL_INTERCEPT_PREFIXES):
        return False
    return not any(
        fnmatchcase(argument, pattern)
        for argument in _shell_options(args)
        for pattern in _SHELL_PASSTHROUGH_ARGS.get(command, ())
    )


def generate_shell_wrapper(shell: str) -> str:
    """Return opt-in shell functions forwarding argv, without installation."""
    if shell == "fish":
        return _guard_shell_helpers(shell, _generate_fish_wrapper())
    if shell == "powershell":
        return _guard_shell_helpers(shell, _generate_powershell_wrapper())
    if shell not in {"bash", "zsh"}:
        raise ValueError("shell must be bash, zsh, fish or powershell")
    conditions = [
        " && ".join(f'[ "${{{index}-}}" = "{word}" ]' for index, word in enumerate(prefix, 1))
        for prefix in _SHELL_INTERCEPT_PREFIXES
    ]
    lines = [
        "# usagetrim: conservative high-volume command wrappers (bash/zsh)",
        "function _usagetrim_shell_intercept {",
        '    local _usagetrim_command="${1-}" _usagetrim_arg _usagetrim_input=0',
    ]
    for command, subcommands in _SHELL_FINITE_SUBCOMMANDS.items():
        lines.extend(
            [
                f'    if [ "$_usagetrim_command" = "{command}" ]; then',
                "        shift",
                '        while [ "$#" -gt 0 ]; do',
                '            case "$1" in',
            ]
        )
        value_flags = _SHELL_GLOBAL_VALUE_FLAGS[command]
        if value_flags:
            lines.append(
                "                "
                + "|".join(value_flags)
                + ') [ "$#" -ge 2 ] || return 1; shift 2 ;;'
            )
        lines.extend(
            [
                "                " + "|".join(_SHELL_GLOBAL_INLINE_FLAGS[command]) + ") shift ;;",
                "                -*) return 1 ;;",
                "                *) break ;;",
                "            esac",
                "        done",
                '        case "${1-}" in',
                "            " + "|".join(subcommands) + ") ;;",
                "            *) return 1 ;;",
                "        esac",
            ]
        )
        if command == "terraform":
            lines.extend(
                [
                    '        if [ "$1" = "plan" ]; then',
                    '            for _usagetrim_arg in "$@"; do',
                    '                case "$_usagetrim_arg" in',
                    "                    --) break ;;",
                    "                    -input=false) _usagetrim_input=1 ;;",
                    "                    -input*) return 1 ;;",
                    "                esac",
                    "            done",
                    '            [ "$_usagetrim_input" = 1 ] || return 1',
                    "        fi",
                ]
            )
        lines.append("    fi")
    for command, patterns in _SHELL_PASSTHROUGH_ARGS.items():
        lines.extend(
            [
                f'    if [ "$_usagetrim_command" = "{command}" ]; then',
                '        for _usagetrim_arg in "$@"; do',
                '            case "$_usagetrim_arg" in',
                "                --) break ;;",
                "                " + "|".join(patterns) + ") return 1 ;;",
                "            esac",
                "        done",
                "    fi",
            ]
        )
    lines.extend(
        [
            '    case "$_usagetrim_command" in kubectl|terraform) return 0 ;; esac',
            "    " + " ||\n    ".join("{ " + item + "; }" for item in conditions),
            "}",
            "function _usagetrim_shell_dispatch {",
            '    if "_usagetrim_shell_intercept" "$@"; then',
            '        command usagetrim run -- "$@"',
            "    else",
            '        command "$@"',
            "    fi",
            "}",
        ]
    )
    names = dict.fromkeys(
        [*(prefix[0] for prefix in _SHELL_INTERCEPT_PREFIXES), *_SHELL_FINITE_SUBCOMMANDS]
    )
    for name in names:
        function_check = (
            f"builtin declare -F {name} >/dev/null 2>&1"
            if shell == "bash"
            else f"(( ${{+functions[{name}]}} ))"
        )
        lines.extend(
            [
                f"if ! builtin alias {name} >/dev/null 2>&1 && ! {function_check}; then",
                # This form avoids alias expansion of the function name at parse time.
                f'    function {name} {{ "_usagetrim_shell_dispatch" {name} "$@"; }}',
                "fi",
            ]
        )
    return _guard_shell_helpers(shell, "\n".join(lines) + "\n")


def _guard_shell_helpers(shell: str, snippet: str) -> str:
    """Do not redefine another import's helpers or leave wrappers calling them."""
    if shell == "powershell":
        return (
            "if (-not (Microsoft.PowerShell.Core\\Get-Command -Name "
            "Test-UsageTrimShellIntercept,Invoke-UsageTrimShellDispatch "
            "-CommandType Alias,Function -ErrorAction SilentlyContinue)) {\n" + snippet + "}\n"
        )
    names = ("_usagetrim_shell_intercept", "_usagetrim_shell_dispatch")
    if shell == "fish":
        return (
            "if "
            + "; and ".join(f"not functions -q {name}" for name in names)
            + "\n"
            + snippet
            + "end\n"
        )
    checks = []
    for name in names:
        checks.append(f"! builtin alias {name} >/dev/null 2>&1")
        checks.append(
            f"! builtin declare -F {name} >/dev/null 2>&1"
            if shell == "bash"
            else f"! (( ${{+functions[{name}]}} ))"
        )
    return "if " + " && ".join(checks) + "; then\n" + snippet + "fi\n"


def _fish_pattern_test(patterns: Sequence[str]) -> str:
    """Compile the shared glob rules to a quoted PCRE expression for fish.

    PCRE's \\z matches the absolute end, like Python fnmatch's \\Z.
    Only fixed policy patterns are compiled, never user command text.
    """
    pattern = "|".join(translate(item).replace(r"\Z", r"\z") for item in patterns)
    quoted = "'" + pattern.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return f"string match --quiet --regex -- {quoted}"


def _generate_fish_wrapper() -> str:
    lines = [
        "# usagetrim: conservative high-volume command wrappers (fish)",
        "function _usagetrim_shell_intercept",
        "    test (count $argv) -gt 0; or return 1",
        '    set -l _usagetrim_command "$argv[1]"',
        "    set -l _usagetrim_input 0",
    ]
    for command, subcommands in _SHELL_FINITE_SUBCOMMANDS.items():
        lines.extend(
            [
                f'    if test "$_usagetrim_command" = {command}',
                "        set --erase argv[1]",
                "        while test (count $argv) -gt 0",
            ]
        )
        value_flags = _SHELL_GLOBAL_VALUE_FLAGS[command]
        if value_flags:
            lines.extend(
                [
                    '            if contains -- "$argv[1]" ' + " ".join(value_flags),
                    "                test (count $argv) -ge 2; or return 1",
                    "                set --erase argv[1..2]",
                    "            else if "
                    + _fish_pattern_test(_SHELL_GLOBAL_INLINE_FLAGS[command])
                    + ' "$argv[1]"',
                ]
            )
        else:
            lines.append(
                "            if "
                + _fish_pattern_test(_SHELL_GLOBAL_INLINE_FLAGS[command])
                + ' "$argv[1]"'
            )
        lines.extend(
            [
                "                set --erase argv[1]",
                "            else if string match --quiet -- '-*' \"$argv[1]\"",
                "                return 1",
                "            else",
                "                break",
                "            end",
                "        end",
                "        test (count $argv) -gt 0; or return 1",
                '        contains -- "$argv[1]" ' + " ".join(subcommands) + "; or return 1",
            ]
        )
        if command == "terraform":
            lines.extend(
                [
                    '        if test "$argv[1]" = plan',
                    "            for _usagetrim_arg in $argv",
                    '                if test "$_usagetrim_arg" = --',
                    "                    break",
                    '                else if test "$_usagetrim_arg" = -input=false',
                    "                    set _usagetrim_input 1",
                    "                else if string match --quiet -- '-input*' \"$_usagetrim_arg\"",
                    "                    return 1",
                    "                end",
                    "            end",
                    '            test "$_usagetrim_input" = 1; or return 1',
                    "        end",
                ]
            )
        lines.append("    end")
    for command, patterns in _SHELL_PASSTHROUGH_ARGS.items():
        lines.extend(
            [
                f'    if test "$_usagetrim_command" = {command}',
                "        for _usagetrim_arg in $argv",
                '            if test "$_usagetrim_arg" = --',
                "                break",
                "            else if " + _fish_pattern_test(patterns) + ' "$_usagetrim_arg"',
                "                return 1",
                "            end",
                "        end",
                "    end",
            ]
        )
    lines.append(
        '    contains -- "$_usagetrim_command" '
        + " ".join(_SHELL_FINITE_SUBCOMMANDS)
        + "; and return 0"
    )
    for prefix in _SHELL_INTERCEPT_PREFIXES:
        checks = [f"test (count $argv) -ge {len(prefix)}"]
        checks.extend(f'test "$argv[{index}]" = {word}' for index, word in enumerate(prefix, 1))
        lines.extend(["    if " + "; and ".join(checks), "        return 0", "    end"])
    lines.extend(
        [
            "    return 1",
            "end",
            "function _usagetrim_shell_dispatch",
            "    if _usagetrim_shell_intercept $argv",
            "        command usagetrim run -- $argv",
            "    else",
            "        command $argv",
            "    end",
            "end",
        ]
    )
    # Fish expands a list as argv without shell word splitting or reparsing.
    names = dict.fromkeys(
        [*(prefix[0] for prefix in _SHELL_INTERCEPT_PREFIXES), *_SHELL_FINITE_SUBCOMMANDS]
    )
    for name in names:
        lines.extend(
            [
                f"if not functions -q {name}",
                f"    function {name}",
                f"        _usagetrim_shell_dispatch {name} $argv",
                "    end",
                "end",
            ]
        )
    return "\n".join(lines) + "\n"


def _powershell_strings(values: Sequence[str]) -> str:
    return "@(" + ", ".join("'" + item.replace("'", "''") + "'" for item in values) + ")"


def _powershell_pattern(patterns: Sequence[str]) -> str:
    # .NET, like PCRE, uses \\z for the absolute end of input.
    pattern = "|".join(translate(item).replace(r"\Z", r"\z") for item in patterns)
    return "'" + pattern.replace("'", "''") + "'"


def _generate_powershell_wrapper() -> str:
    lines = [
        "# usagetrim: conservative high-volume command wrappers (PowerShell Core)",
        "function global:Test-UsageTrimShellIntercept {",
        "    param([string[]]$Argv)",
        "    if ($null -eq $Argv -or $Argv.Count -eq 0) { return $false }",
        "    $commandName = $Argv[0]",
        "    $position = 1",
    ]
    for command, subcommands in _SHELL_FINITE_SUBCOMMANDS.items():
        lines.extend(
            [
                f"    if ($commandName -ceq '{command}') {{",
                "        while ($position -lt $Argv.Count -and $Argv[$position].StartsWith('-')) {",
            ]
        )
        value_flags = _SHELL_GLOBAL_VALUE_FLAGS[command]
        if value_flags:
            lines.extend(
                [
                    f"            if ({_powershell_strings(value_flags)} -ccontains $Argv[$position]) {{",
                    "                if ($position + 1 -ge $Argv.Count) { return $false }",
                    "                $position += 2",
                    "            } elseif ($Argv[$position] -cmatch "
                    + _powershell_pattern(_SHELL_GLOBAL_INLINE_FLAGS[command])
                    + ") {",
                ]
            )
        else:
            lines.append(
                "            if ($Argv[$position] -cmatch "
                + _powershell_pattern(_SHELL_GLOBAL_INLINE_FLAGS[command])
                + ") {"
            )
        lines.extend(
            [
                "                $position++",
                "            } else { return $false }",
                "        }",
                "        if ($position -ge $Argv.Count -or "
                + _powershell_strings(subcommands)
                + " -cnotcontains $Argv[$position]) { return $false }",
            ]
        )
        if command == "terraform":
            lines.extend(
                [
                    "        if ($Argv[$position] -ceq 'plan') {",
                    "            $hasInputFalse = $false",
                    "            for ($i = $position + 1; $i -lt $Argv.Count; $i++) {",
                    "                if ($Argv[$i] -ceq '--') { break }",
                    "                if ($Argv[$i].StartsWith('-input', [System.StringComparison]::Ordinal)) {",
                    "                    if ($Argv[$i] -cne '-input=false') { return $false }",
                    "                    $hasInputFalse = $true",
                    "                }",
                    "            }",
                    "            if (-not $hasInputFalse) { return $false }",
                    "        }",
                ]
            )
        lines.append("    }")
    for command, patterns in _SHELL_PASSTHROUGH_ARGS.items():
        lines.extend(
            [
                f"    if ($commandName -ceq '{command}') {{",
                "        for ($i = $position; $i -lt $Argv.Count; $i++) {",
                "            if ($Argv[$i] -ceq '--') { break }",
                "            if ($Argv[$i] -cmatch "
                + _powershell_pattern(patterns)
                + ") { return $false }",
                "        }",
                "    }",
            ]
        )
    lines.append(
        "    if ("
        + _powershell_strings(tuple(_SHELL_FINITE_SUBCOMMANDS))
        + " -ccontains $commandName) { return $true }"
    )
    for prefix in _SHELL_INTERCEPT_PREFIXES:
        checks = [f"$Argv.Count -ge {len(prefix)}"]
        checks.extend(f"$Argv[{index}] -ceq '{word}'" for index, word in enumerate(prefix))
        lines.append("    if (" + " -and ".join(checks) + ") { return $true }")
    lines.extend(
        [
            "    return $false",
            "}",
            "function global:Invoke-UsageTrimShellDispatch {",
            "    param([string]$CommandName, [object[]]$CommandArgs)",
            "    if ($null -eq $CommandArgs) { $CommandArgs = @() }",
            "    [string[]]$originalArgv = @($CommandName) + @($CommandArgs)",
            "    if (Test-UsageTrimShellIntercept -Argv $originalArgv) {",
            "        $application = @(Microsoft.PowerShell.Core\\Get-Command -Name usagetrim -CommandType Application -ErrorAction Stop)[0]",
            "        & $application.Path 'run' '--' @originalArgv",
            "    } else {",
            "        $application = @(Microsoft.PowerShell.Core\\Get-Command -Name $CommandName -CommandType Application -ErrorAction Stop)[0]",
            "        & $application.Path @CommandArgs",
            "    }",
            "    $global:LASTEXITCODE = $LASTEXITCODE",
            "}",
        ]
    )
    names = dict.fromkeys(
        [*(prefix[0] for prefix in _SHELL_INTERCEPT_PREFIXES), *_SHELL_FINITE_SUBCOMMANDS]
    )
    for name in names:
        lines.extend(
            [
                f"if (-not (Microsoft.PowerShell.Core\\Get-Command -Name {name} -CommandType Alias,Function -ErrorAction SilentlyContinue)) {{",
                f"    function global:{name} {{ Invoke-UsageTrimShellDispatch -CommandName '{name}' -CommandArgs $args }}",
                "}",
            ]
        )
    return "\n".join(lines) + "\n"


_SHELL_WRAPPER_START = b"# >>> usagetrim shell wrapper >>>"
_SHELL_WRAPPER_END = b"# <<< usagetrim shell wrapper <<<"
_SHELL_WRAPPER_SEPARATOR = b"# usagetrim: preceding newline added"
_SHELL_WRAPPER_CREATED_PREFIX = b"# usagetrim: rc-created-by-installer"
_SHELL_WRAPPER_CREATED = _SHELL_WRAPPER_CREATED_PREFIX + b"=1"


def _shell_rc_path(shell: str, rc_path: str | Path | None) -> Path:
    if shell not in {"bash", "zsh", "fish", "powershell"}:
        raise ValueError("shell must be bash, zsh, fish or powershell")
    if shell == "powershell":
        folder = "Documents/PowerShell" if sys.platform == "win32" else ".config/powershell"
        default = Path.home() / folder / "Microsoft.PowerShell_profile.ps1"
    else:
        default = Path.home() / (".config/fish/config.fish" if shell == "fish" else f".{shell}rc")
    path = Path(rc_path) if rc_path is not None else default
    if path.is_symlink():
        raise ValueError("shell rc must not be a symlink")
    if path.exists() and not path.is_file():
        raise ValueError("shell rc must be a regular file")
    return path


def _shell_managed_block(content: bytes) -> tuple[int, int, bool, bool] | None:
    """Validate exact marker lines before any write; retain byte offsets."""
    starts, ends = [], []
    offset = 0
    for line in content.splitlines(keepends=True):
        if line.rstrip(b"\r\n") == _SHELL_WRAPPER_START:
            starts.append((offset, offset + len(line)))
        if line.rstrip(b"\r\n") == _SHELL_WRAPPER_END:
            ends.append((offset, offset + len(line)))
        offset += len(line)
    if (
        not starts
        and not ends
        and not (_SHELL_WRAPPER_START in content or _SHELL_WRAPPER_END in content)
    ):
        if _SHELL_WRAPPER_CREATED_PREFIX in content:
            raise ValueError("orphan shell wrapper provenance marker")
        return None
    if (
        len(starts) != 1
        or len(ends) != 1
        or content.count(_SHELL_WRAPPER_START) != 1
        or content.count(_SHELL_WRAPPER_END) != 1
        or starts[0][1] > ends[0][0]
    ):
        raise ValueError("malformed or duplicate shell wrapper markers")
    start, body = starts[0]
    end = ends[0][1]
    created = content[body : ends[0][0]].startswith(_SHELL_WRAPPER_CREATED + b"\n")
    if _SHELL_WRAPPER_CREATED_PREFIX in content and (
        not created or content.count(_SHELL_WRAPPER_CREATED_PREFIX) != 1
    ):
        raise ValueError("malformed shell wrapper provenance marker")
    added_newline = content[body : ends[0][0]].startswith(_SHELL_WRAPPER_SEPARATOR + b"\n")
    if added_newline and (start == 0 or content[start - 1 : start] != b"\n"):
        raise ValueError("inconsistent shell wrapper newline marker")
    return start, end, added_newline, created


def _write_shell_rc(path: Path, content: bytes) -> None:
    """Replace atomically, preserving existing permissions; new rc is private."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as staged:
            temporary = Path(staged.name)
            staged.write(content)
            staged.flush()
            os.chmod(temporary, mode)
            os.fsync(staged.fileno())
        # Close the staged file before replace (required by Windows sharing rules).
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read_shell_rc(shell: str, path: Path) -> bytes:
    content = path.read_bytes()
    if shell == "powershell":
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("PowerShell profile must be UTF-8; no changes made") from exc
        if b"\x00" in content:
            raise ValueError("PowerShell profile must be UTF-8; no changes made")
    return content


def install_shell_wrapper(shell: str, rc_path: str | Path | None = None) -> Path:
    """Install/update one validated managed block without changing other bytes."""
    path = _shell_rc_path(shell, rc_path)
    created = not path.exists()
    original = b"" if created else _read_shell_rc(shell, path)
    existing = _shell_managed_block(original)
    if existing is None:
        start = end = len(original)
        added_newline = bool(original and not original.endswith(b"\n"))
        prefix = original + (b"\n" if added_newline else b"")
    else:
        start, end, added_newline, created = existing
        prefix = original[:start]
    block = _SHELL_WRAPPER_START + b"\n"
    if created:
        block += _SHELL_WRAPPER_CREATED + b"\n"
    if added_newline:
        block += _SHELL_WRAPPER_SEPARATOR + b"\n"
    block += generate_shell_wrapper(shell).encode("utf-8") + _SHELL_WRAPPER_END + b"\n"
    updated = prefix + block + original[end:]
    if updated != original:
        _write_shell_rc(path, updated)
    return path


def uninstall_shell_wrapper(shell: str, rc_path: str | Path | None = None) -> Path:
    """Remove only the managed block, including a separator added by installation."""
    path = _shell_rc_path(shell, rc_path)
    if not path.exists():
        return path
    original = _read_shell_rc(shell, path)
    existing = _shell_managed_block(original)
    if existing is not None:
        start, end, added_newline, created = existing
        updated = original[: start - int(added_newline)] + original[end:]
        if created and not updated:
            path.unlink()
        else:
            _write_shell_rc(path, updated)
    return path


def generate_zsh_alias() -> str:
    return """
# usagetrim — Automatic token optimization for AI coding
alias cc-run="usagetrim run"
alias tcut="usagetrim"
"""


def install_zsh_hook() -> Path:
    """Append usagetrim helper alias to user's zshrc."""
    zshrc = Path.home() / ".zshrc"
    snippet = generate_zsh_alias()
    if zshrc.exists():
        content = zshrc.read_text(encoding="utf-8")
        if "usagetrim" not in content:
            zshrc.write_text(content + "\n" + snippet, encoding="utf-8")
    else:
        zshrc.write_text(snippet, encoding="utf-8")
    return zshrc


def setup_claude_code_mcp_config() -> str:
    """Generate the exact command to add usagetrim to Claude Code native MCP configuration."""
    return "claude mcp add --scope user usagetrim -- usagetrim mcp"
