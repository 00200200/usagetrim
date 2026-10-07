"""Tests for Gradle & Maven verbose build log compactor for JVM projects."""

from __future__ import annotations

from usagetrim.core.specialized import (
    author_maven_gradle_fixture,
    auto_specialize_command_output,
    filter_jvm_build,
    filter_maven_gradle,
)
from usagetrim.filters import filter_maven_gradle as fmg_pkg
from usagetrim.filters.maven_gradle import (
    author_maven_gradle_fixture as fmg_fixture,
)
from usagetrim.filters.maven_gradle import (
    filter_jvm_build as fjb_facade,
)
from usagetrim.filters.maven_gradle import (
    filter_maven_gradle as fmg_facade,
)
from usagetrim.metrics.tokenizer import count_tokens


def test_filter_maven_folds_downloads_and_routine_plugins():
    raw = author_maven_gradle_fixture(tool="maven", has_error=False, num_downloads=60)
    compacted = filter_maven_gradle(raw)

    assert "[UsageTrim: suppressed 120 artifact download & progress events]" in compacted
    assert "[Step] maven-compiler-plugin:3.11.0:compile @ demo-service" in compacted
    assert "[Step] maven-surefire-plugin:3.2.5:test @ demo-service" in compacted
    assert "BUILD SUCCESS" in compacted
    assert "Total time:  6.820 s" in compacted

    # Routine verbose noise suppressed
    assert "Downloading from central:" not in compacted
    assert "Downloaded from central:" not in compacted
    assert "Using 'UTF-8' encoding to copy filtered resources." not in compacted
    assert "Changes detected - recompiling the module!" not in compacted


def test_filter_maven_token_reduction_exceeds_85_percent():
    raw = author_maven_gradle_fixture(tool="maven", has_error=False, num_downloads=100)
    compacted = filter_maven_gradle(raw)

    raw_tokens = count_tokens(raw).avg
    compacted_tokens = count_tokens(compacted).avg
    reduction_pct = (raw_tokens - compacted_tokens) / raw_tokens * 100.0

    assert reduction_pct >= 85.0, f"Expected >= 85% reduction, got {reduction_pct:.1f}%"


def test_filter_maven_preserves_compilation_errors_verbatim():
    raw = author_maven_gradle_fixture(tool="maven", has_error=True, num_downloads=40)
    compacted = filter_maven_gradle(raw)

    # Downloads folded
    assert "[UsageTrim: suppressed 80 artifact download & progress events]" in compacted

    # Compiler errors completely intact
    assert "COMPILATION ERROR :" in compacted
    assert "/app/src/main/java/com/example/UserService.java:[42,19] cannot find symbol" in compacted
    assert "symbol:   variable missingRepository" in compacted
    assert "location: class com.example.UserService" in compacted
    assert "/app/src/main/java/com/example/UserService.java:[58,5] ';' expected" in compacted
    assert "2 errors" in compacted
    assert "BUILD FAILURE" in compacted
    assert "Total time:  4.210 s" in compacted


def test_filter_gradle_folds_downloads_and_cached_tasks():
    raw = author_maven_gradle_fixture(tool="gradle", has_error=False, num_downloads=50)
    compacted = filter_maven_gradle(raw)

    assert "[UsageTrim: suppressed 50 artifact download & progress events]" in compacted
    assert "Gradle tasks up-to-date/cached" in compacted
    assert "BUILD SUCCESSFUL in 2s" in compacted
    assert "Download https://plugins.gradle.org" not in compacted


def test_filter_gradle_token_reduction_exceeds_85_percent():
    raw = author_maven_gradle_fixture(tool="gradle", has_error=False, num_downloads=100)
    compacted = filter_maven_gradle(raw)

    raw_tokens = count_tokens(raw).avg
    compacted_tokens = count_tokens(compacted).avg
    reduction_pct = (raw_tokens - compacted_tokens) / raw_tokens * 100.0

    assert reduction_pct >= 85.0, f"Expected >= 85% reduction, got {reduction_pct:.1f}%"


def test_filter_gradle_preserves_test_failures_and_stacktraces():
    raw = author_maven_gradle_fixture(tool="gradle", has_error=True, num_downloads=30)
    compacted = filter_maven_gradle(raw)

    assert "com.example.CalculatorTest > testDivisionByZero() FAILED" in compacted
    assert "java.lang.ArithmeticException: / by zero" in compacted
    assert "at com.example.Calculator.divide(Calculator.java:14)" in compacted
    assert "at com.example.CalculatorTest.testDivisionByZero(CalculatorTest.java:28)" in compacted
    assert "4 tests completed, 1 failed" in compacted
    assert "> Task :test FAILED" in compacted
    assert "FAILURE: Build failed with an exception." in compacted
    assert "BUILD FAILED in 3s" in compacted


def test_auto_specialize_maven_gradle_routing():
    raw_mvn = author_maven_gradle_fixture(tool="maven", has_error=False, num_downloads=20)
    raw_gradle = author_maven_gradle_fixture(tool="gradle", has_error=False, num_downloads=20)

    # Command-based routing
    res1 = auto_specialize_command_output("mvn clean package", raw_mvn)
    assert res1 is not None
    assert "[UsageTrim: suppressed 40 artifact download & progress events]" in res1

    res2 = auto_specialize_command_output("./gradlew test", raw_gradle)
    assert res2 is not None
    assert "[UsageTrim: suppressed 20 artifact download & progress events]" in res2

    res3 = auto_specialize_command_output("gradle build", raw_gradle)
    assert res3 is not None

    res4 = auto_specialize_command_output("./mvnw test", raw_mvn)
    assert res4 is not None

    # Output heuristics routing (without specific command)
    res_heuristic = auto_specialize_command_output("run_ci_step", raw_mvn)
    assert res_heuristic is not None
    assert "[UsageTrim: suppressed 40 artifact download & progress events]" in res_heuristic


def test_facade_and_alias_exports():
    assert filter_jvm_build is filter_maven_gradle
    assert fmg_facade is filter_maven_gradle
    assert fjb_facade is filter_maven_gradle
    assert fmg_pkg is filter_maven_gradle
    assert fmg_fixture is author_maven_gradle_fixture
