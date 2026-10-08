"""Specialized log compactor filters for developer tooling."""

from __future__ import annotations

from usagetrim.filters.android import filter_android, filter_android_logcat
from usagetrim.filters.ansible import (
    author_ansible_playbook_fixture,
    author_puppet_run_fixture,
    filter_ansible,
    filter_ansible_run,
    filter_puppet,
    filter_puppet_run,
)
from usagetrim.filters.compiler import (
    author_compiler_build_fixture,
    filter_compiler,
    filter_compiler_output,
)
from usagetrim.filters.maven_gradle import (
    author_maven_gradle_fixture,
    filter_jvm_build,
    filter_maven_gradle,
)
from usagetrim.filters.spark import (
    author_spark_log_fixture,
    filter_spark,
    filter_spark_log,
)
from usagetrim.filters.xcodebuild import filter_xcodebuild

__all__ = [
    "filter_xcodebuild",
    "filter_android",
    "filter_android_logcat",
    "filter_compiler_output",
    "filter_compiler",
    "author_compiler_build_fixture",
    "filter_maven_gradle",
    "filter_jvm_build",
    "author_maven_gradle_fixture",
    "filter_spark",
    "filter_spark_log",
    "author_spark_log_fixture",
    "filter_ansible",
    "filter_ansible_run",
    "filter_puppet",
    "filter_puppet_run",
    "author_ansible_playbook_fixture",
    "author_puppet_run_fixture",
]
