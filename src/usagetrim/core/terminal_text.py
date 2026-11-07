"""Shared terminal escape patterns for cleaning and secret detection."""

import re

ANSI_CSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
ANSI_OSC_PATTERN = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ANSI_GENERIC_PATTERN = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
ANSI_SEQUENCE = re.compile(
    f"(?:{ANSI_OSC_PATTERN.pattern})|(?:{ANSI_CSI_PATTERN.pattern})|(?:{ANSI_GENERIC_PATTERN.pattern})"
)
