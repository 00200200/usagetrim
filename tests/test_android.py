"""Tests for Android Gradle & adb logcat specialized filter."""

from __future__ import annotations

from usagetrim.core.specialized import (
    author_android_logcat_fixture,
    auto_specialize_command_output,
    filter_android,
    filter_android_logcat,
)
from usagetrim.filters import filter_android as fa1
from usagetrim.filters.android import filter_android_logcat as fa2
from usagetrim.metrics.tokenizer import count_tokens


def test_filter_android_logcat_strips_noise_and_preserves_fatal_exception():
    raw = author_android_logcat_fixture(has_crash=True, has_tombstone=False, has_anr=False)
    compacted = filter_android_logcat(raw)

    assert "[UsageTrim:" in compacted
    assert "background OS noise line(s) collapsed" in compacted
    assert "SurfaceFlinger" not in compacted
    assert "AudioFlinger" not in compacted

    # Preserves 100% of the crash stack trace
    assert "FATAL EXCEPTION: main" in compacted
    assert "Process: com.example.myapp, PID: 12345" in compacted
    assert "java.lang.NullPointerException: Attempt to invoke virtual method" in compacted
    assert "at com.example.myapp.MainActivity.onCreate(MainActivity.kt:42)" in compacted
    assert "Caused by: java.lang.IllegalStateException: View binding cannot be null" in compacted
    assert "at com.example.myapp.MainActivity.setupViews(MainActivity.kt:88)" in compacted


def test_filter_android_logcat_preserves_anr_blocks():
    raw = author_android_logcat_fixture(has_crash=False, has_tombstone=False, has_anr=True)
    compacted = filter_android_logcat(raw)

    assert "[UsageTrim:" in compacted
    assert "ANR in com.example.myapp (com.example.myapp/.MainActivity)" in compacted
    assert "PID: 12345" in compacted
    assert "Reason: Input dispatching timed out" in compacted
    assert "Load: 0.62 / 0.45 / 0.21" in compacted


def test_filter_android_logcat_preserves_native_tombstone():
    raw = author_android_logcat_fixture(has_crash=False, has_tombstone=True, has_anr=False)
    compacted = filter_android_logcat(raw)

    assert "[UsageTrim:" in compacted
    assert "*** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***" in compacted
    assert "signal 11 (SIGSEGV), code 1 (SEGV_MAPERR)" in compacted
    assert "pid: 12345, tid: 12345, name: com.example.myapp" in compacted
    assert "backtrace:" in compacted
    assert "#00 pc 0000000000042a10" in compacted
    assert "#01 pc 0000000000054321" in compacted


def test_filter_android_logcat_target_package_filtering():
    raw = (
        "04-12 10:14:20.100  1000  1000 D OtherApp: Working on other stuff\n"
        "04-12 10:14:20.200 12345 12345 D com.example.myapp: Initializing SDK client\n"
        "04-12 10:14:20.300  1000  1000 D SurfaceFlinger: Refreshing layer\n"
        "04-12 10:14:20.400 12345 12345 I MyAppTag: User tapped checkout button\n"
    )
    compacted = filter_android_logcat(raw, package="com.example.myapp")

    assert "Initializing SDK client" in compacted
    assert "OtherApp" not in compacted
    assert "SurfaceFlinger" not in compacted


def test_filter_android_logcat_token_reduction_over_85_percent():
    raw = author_android_logcat_fixture(has_crash=False, noise_count=60)
    compacted = filter_android_logcat(raw)

    raw_tokens = count_tokens(raw).avg
    compacted_tokens = count_tokens(compacted).avg
    reduction = 1.0 - (compacted_tokens / raw_tokens)

    assert reduction >= 0.85
    assert "[UsageTrim:" in compacted


def test_filter_android_logcat_folds_r8_and_gradle_tasks():
    raw = (
        "> Task :app:preBuild UP-TO-DATE\n"
        "> Task :app:compileDebugAidl NO-SOURCE\n"
        "> Task :app:mergeDebugResources UP-TO-DATE\n"
        "> Task :app:minifyDebugWithR8\n"
        "R8 is a new, work-in-progress rule-based shrinker...\n"
        "Note: The configuration keeps the following methods...\n"
        "Reading library jar /Users/developer/Android/sdk/platforms/android-34/android.jar\n"
        "Searching for referenced classes...\n"
        "e: /Users/developer/MyApp/app/src/main/java/Main.kt: (15, 20): Unresolved reference: foo\n"
        "> Task :app:compileDebugKotlin FAILED\n"
        "FAILURE: Build failed with an exception.\n"
    )
    compacted = filter_android_logcat(raw)

    assert "[UsageTrim:" in compacted
    assert "R8 is a new" not in compacted
    assert "Reading library jar" not in compacted
    assert "Unresolved reference: foo" in compacted
    assert "FAILURE: Build failed with an exception." in compacted


def test_filter_android_logcat_idempotency_on_clean_or_short():
    short = "adb devices\nList of devices attached\nemulator-5554\tdevice\n"
    assert filter_android_logcat(short) == short

    raw = author_android_logcat_fixture(has_crash=True)
    once = filter_android_logcat(raw)
    assert filter_android_logcat(once) == once


def test_auto_specialize_routes_android_commands():
    raw = author_android_logcat_fixture(has_crash=True)
    via_cmd = auto_specialize_command_output("adb logcat -d", raw)
    assert via_cmd is not None
    assert "FATAL EXCEPTION: main" in via_cmd
    assert "[UsageTrim:" in via_cmd

    # Output signature detection without adb in command
    via_output = auto_specialize_command_output("device-runner --dump", raw)
    assert via_output is not None
    assert "FATAL EXCEPTION: main" in via_output


def test_filter_package_exports():
    assert fa1 is filter_android
    assert fa2 is filter_android_logcat
