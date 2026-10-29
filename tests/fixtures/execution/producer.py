"""Deterministic subprocess fixture; stdlib only, no sleeps or network access."""

import argparse
import json
import os


def write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        remaining = remaining[os.write(fd, remaining) :]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=[
            "stdout",
            "stderr",
            "mixed",
            "empty",
            "argv",
            "invalid-utf8",
            "ansi",
            "crlf",
            "cr",
            "line",
            "records",
        ],
        default="stdout",
    )
    parser.add_argument("--text", default="payload")
    parser.add_argument("--bytes", type=int, default=128)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument(
        "--no-newline", action="store_true", help="Do not append LF in stdout/stderr/line modes"
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.bytes < 0 or not 0 <= args.exit_code <= 255:
        parser.error("bytes must be nonnegative and exit-code must be 0–255")
    if os.name == "nt":
        import msvcrt

        for fd in (1, 2):
            msvcrt.setmode(fd, os.O_BINARY)
    if args.mode in {"stdout", "stderr"}:
        write_all(
            1 if args.mode == "stdout" else 2,
            (args.text + ("" if args.no_newline else "\n")).encode("utf-8"),
        )
    elif args.mode == "mixed":
        for fd, data in [(1, b"out1\n"), (2, b"err1\n"), (1, b"out2\n")]:
            write_all(fd, data)
    elif args.mode == "argv":
        values = args.args[1:] if args.args[:1] == ["--"] else args.args
        write_all(1, (json.dumps(values, ensure_ascii=False) + "\n").encode("utf-8"))
    elif args.mode in {"invalid-utf8", "ansi", "crlf", "cr"}:
        write_all(
            1,
            {
                "invalid-utf8": b"bad:\xff\xfe\n",
                "ansi": b"\x1b[31mred\x1b[0m\n",
                "crlf": b"one\r\ntwo\r\n",
                "cr": b"work1\rwork2\rdone\n",
            }[args.mode],
        )
    elif args.mode == "line":
        for offset in range(0, args.bytes, 65536):
            write_all(1, b"x" * min(65536, args.bytes - offset))
        if not args.no_newline:
            write_all(1, b"\n")
    elif args.mode == "records":
        # At most 64 KiB per block. Alternate stdout/stderr, exactly --bytes total.
        for offset in range(0, args.bytes, 65536):
            size = min(65536, args.bytes - offset)
            block = b"".join(
                f"{index:08d} INFO execution fixture record={index:08d}".ljust(63).encode() + b"\n"
                for index in range(offset // 64, (offset + size + 63) // 64)
            )[:size]
            write_all(1 if (offset // 65536) % 2 == 0 else 2, block)
    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
