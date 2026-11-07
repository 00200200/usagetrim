"""Controlled process trees for runner shutdown tests; no network access."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=[
            "wait",
            "ignore-term",
            "flood",
            "parent",
            "orphan",
            "leaf",
            "stubborn-parent",
            "stubborn-leaf",
        ],
    )
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory
    directory.mkdir(parents=True, exist_ok=True)
    name = "leaf" if args.mode in {"leaf", "stubborn-leaf"} else "root"
    (directory / f"{name}.pid").write_text(str(os.getpid()))
    if args.mode in {"ignore-term", "stubborn-leaf"} and os.name == "posix":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(1, os.O_BINARY)
        msvcrt.setmode(2, os.O_BINARY)
    # Complete safe lines plus an unfinished credential must never leak on abort.
    os.write(1, b"ready stdout\n")
    os.write(2, b"ready stderr\n")
    os.write(1, b"ghp_" + b"A" * 36)  # A partial raw line must remain quarantined.
    if args.mode in {"parent", "orphan", "stubborn-parent"}:
        mode = "stubborn-leaf" if args.mode == "stubborn-parent" else "leaf"
        subprocess.Popen([sys.executable, __file__, mode, str(directory)])
    (directory / f"{name}.ready").touch()
    if args.mode == "orphan":
        return 0
    gate = threading.Event()
    while not (directory / "release").exists():
        if args.mode == "flood":
            # Bounded writes alternate streams, exercising queue backpressure.
            for fd in (1, 2):
                data = memoryview(b"safe record\n" * 512)
                while data:
                    data = data[os.write(fd, data) :]
        else:
            gate.wait(0.02)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
