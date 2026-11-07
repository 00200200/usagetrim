"""Shared terminal escape patterns for cleaning and secret detection."""

import re

# C1 CSI/OSC/ST are the single-character equivalents of ESC [, ESC ] and ESC \.
# Match both forms without changing offsets used by the redactor.
ANSI_CSI_PATTERN = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
ANSI_OSC_PATTERN = re.compile(r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)")
ANSI_GENERIC_PATTERN = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
ANSI_SEQUENCE = re.compile(
    f"(?:{ANSI_OSC_PATTERN.pattern})|(?:{ANSI_CSI_PATTERN.pattern})|(?:{ANSI_GENERIC_PATTERN.pattern})"
)
