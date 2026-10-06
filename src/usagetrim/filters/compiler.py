"""CMake & GCC/Clang compilation warning & template error compactor."""

from __future__ import annotations

from usagetrim.core.specialized import (
    author_compiler_build_fixture,
    filter_compiler,
    filter_compiler_output,
)

__all__ = ["filter_compiler_output", "filter_compiler", "author_compiler_build_fixture"]
