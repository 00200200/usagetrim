"""Specialized log compactor filters for developer tooling."""

from __future__ import annotations

from usagetrim.filters.android import filter_android, filter_android_logcat
from usagetrim.filters.xcodebuild import filter_xcodebuild

__all__ = ["filter_xcodebuild", "filter_android", "filter_android_logcat"]
