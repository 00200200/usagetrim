"""Native PowerShell PATH shims: accept OS argv, never re-enter a shell parser."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from usagetrim.core.hooks import (
    _SHELL_FINITE_SUBCOMMANDS,
    _SHELL_INTERCEPT_PREFIXES,
    should_intercept_command,
)

_MANIFEST = ".usagetrim-native-shims.json"
_NAMES = tuple(
    dict.fromkeys([*(p[0] for p in _SHELL_INTERCEPT_PREFIXES), *_SHELL_FINITE_SUBCOMMANDS])
)


def _manifest(directory: Path) -> dict[str, str]:
    marker = directory / _MANIFEST
    if directory.is_symlink() or marker.is_symlink():
        raise ValueError("native shim directory must not be a symlink")
    value = json.loads(marker.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("files"), dict)
    ):
        raise ValueError("unrecognized native shim directory")
    files = value["files"]
    if any(
        name not in {*_NAMES, *(name + ".exe" for name in _NAMES)}
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
        for name, digest in files.items()
    ):
        raise ValueError("invalid native shim manifest")
    return files


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _owned_directory(directory: Path) -> bool:
    """A marker name alone must not remove a user's directory from PATH."""
    try:
        files = _manifest(directory)
        return bool(files) and all(
            not (directory / name).is_symlink() and _digest(directory / name) == digest
            for name, digest in files.items()
        )
    except (OSError, ValueError):
        return False


def _search_path(directory: Path) -> list[str]:
    entries = []
    for item in os.environ.get("PATH", "").split(os.pathsep):
        try:
            candidate = Path(item or os.curdir).resolve()
        except (OSError, RuntimeError):
            # An unresolvable PATH entry cannot safely identify an executable.
            continue
        if candidate != directory and not _owned_directory(candidate):
            entries.append(item)
    return entries


def _find_application(name: str, entries: list[str]) -> str | None:
    if os.name != "nt":
        return (
            shutil.which(name, path=os.pathsep.join(item or os.curdir for item in entries))
            if entries
            else None
        )
    # shutil.which can prepend cwd on Windows, independently of the supplied
    # PATH. Search explicitly, so a shim in cwd cannot be selected recursively.
    extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
    for entry in entries:
        for extension in extensions:
            if extension and extension.startswith("."):
                path = Path(entry or os.curdir).absolute() / (name + extension)
                if path.is_file():
                    return str(path)
    return None


def prepare_powershell_shims(directory: Path) -> None:
    """Publish private launchers; refuse to overwrite an unowned/modified file."""
    directory = Path(directory)
    owned = {}
    if directory.exists() or directory.is_symlink():
        owned = _manifest(directory)
        if {p.name for p in directory.iterdir()} != {*owned, _MANIFEST} or any(
            (directory / name).is_symlink() or _digest(directory / name) != digest
            for name, digest in owned.items()
        ):
            raise ValueError("native shim directory contains user modifications")
    if os.name == "nt":
        # Reuse the installed native Python console launcher. Its existing entry
        # point recognizes our manifest in cli.main; no compiler/runtime is added.
        launcher = Path(sysconfig.get_path("scripts")) / "usagetrim.exe"
        if not launcher.is_file() or launcher.is_symlink():
            raise ValueError("PowerShell shims require the installed usagetrim.exe launcher")
        payload = launcher.read_bytes()
        suffix = ".exe"
        entries = _search_path(directory.resolve())
        # Batch files require cmd.exe parsing; leave their native PowerShell
        # invocation untouched instead of shadowing them with a .exe shim.
        names = [
            name
            for name in _NAMES
            if (application := _find_application(name, entries))
            and Path(application).suffix.lower() == ".exe"
        ]
    else:
        # This fixed bootstrap only expands "$@"; user argv is never shell text.
        payload = (
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + ' -m usagetrim.core.shell_shim "$0" "$@"\n'
        ).encode("utf-8")
        suffix = ""
        names = _NAMES
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".usagetrim-shims-stage-", dir=directory.parent) as tmp:
        stage = Path(tmp)
        files = {}
        for name in names:
            path = stage / (name + suffix)
            path.write_bytes(payload)
            path.chmod(0o700)
            files[path.name] = _digest(path)
        metadata = json.dumps({"version": 1, "files": files}, sort_keys=True).encode("utf-8")
        marker = stage / _MANIFEST
        marker.write_bytes(metadata)
        marker.chmod(0o600)
        if not directory.exists():
            os.replace(stage, directory)
        else:
            for path in stage.iterdir():
                target = directory / path.name
                if not target.exists() or target.read_bytes() != path.read_bytes():
                    os.replace(path, target)
            for name in owned.keys() - files.keys():
                (directory / name).unlink()


def remove_powershell_shims(profile: Path) -> None:
    """Delete only hash-verified owned artifacts; keep any user additions/edits."""
    directory = profile.parent / (".usagetrim-shims-" + profile.name)
    if not directory.exists() or directory.is_symlink():
        return
    try:
        files = _manifest(directory)
    except (OSError, ValueError):
        return
    for name, digest in files.items():
        path = directory / name
        if path.is_file() and not path.is_symlink() and _digest(path) == digest:
            path.unlink()
    (directory / _MANIFEST).unlink()
    if not any(directory.iterdir()):
        directory.rmdir()


def run_native_shim(shim: Path, arguments: list[str]) -> int:
    """Route once, with inherited handles and no PowerShell re-marshalling."""
    directory = shim.parent.resolve()
    owned = _manifest(directory)
    if shim.name not in owned or shim.is_symlink() or _digest(shim) != owned[shim.name]:
        raise ValueError("unrecognized native shim executable")
    name = shim.stem if shim.suffix.lower() == ".exe" else shim.name
    entries = _search_path(directory)
    os.environ["PATH"] = os.pathsep.join(entries)
    application = _find_application(name, entries)
    if application is None or Path(application).resolve().parent == directory:
        raise FileNotFoundError(f"original executable unavailable: {name}")
    argv = [application, *arguments]
    if os.name == "nt" and Path(application).suffix.lower() != ".exe":
        raise ValueError("native command changed to a non-exe target; reload the profile")
    if should_intercept_command([name, *arguments]):
        usagetrim = _find_application("usagetrim", entries)
        if os.name == "nt" and usagetrim and Path(usagetrim).suffix.lower() != ".exe":
            usagetrim = None
        argv = [usagetrim] if usagetrim else [sys.executable, "-m", "usagetrim.cli"]
        argv += ["run", "--", name, *arguments]
    if os.name != "nt":
        os.execv(argv[0], argv)
    # Native handles (including stdin) are inherited. The launcher process exits
    # with the child's code, so PowerShell itself computes $? and && normally.
    return subprocess.run(argv).returncode


def dispatch_if_native_shim() -> bool:
    """Recognize a copied Windows console launcher, never an ordinary CLI call."""
    shim = Path(sys.argv[0]).absolute()
    if os.name == "nt":
        shim = shim.with_name(shim.name.lower())
    if shim.stem in _NAMES and (shim.parent / _MANIFEST).is_file():
        # Python console launchers commonly strip .exe from sys.argv[0].
        try:
            owned = _manifest(shim.parent)
        except (OSError, ValueError):
            return False
        if shim.name not in owned and shim.name + ".exe" in owned:
            shim = shim.with_name(shim.name + ".exe")
        if shim.name not in owned or shim.is_symlink() or not shim.is_file():
            return False
        try:
            if _digest(shim) != owned[shim.name]:
                return False
        except OSError:
            return False
        try:
            code = run_native_shim(shim, sys.argv[1:])
        except (OSError, ValueError) as exc:
            print(f"UsageTrim native shim: {exc}", file=sys.stderr)
            code = 127
        raise SystemExit(code)
    return False


def main() -> None:
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--prepare":
            prepare_powershell_shims(Path(sys.argv[2]))
        elif len(sys.argv) >= 2:
            raise SystemExit(run_native_shim(Path(sys.argv[1]).absolute(), sys.argv[2:]))
        else:
            raise ValueError("native shim requires a launcher path")
    except (OSError, ValueError) as exc:
        print(f"UsageTrim native shim: {exc}", file=sys.stderr)
        raise SystemExit(127) from exc


if __name__ == "__main__":
    main()
