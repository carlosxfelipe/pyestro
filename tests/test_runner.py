from __future__ import annotations

from pathlib import Path

import pytest

from pyestro.flow import FlowError
from pyestro.runner import FlowRunner

from .conftest import FakeDriver, hierarchy, node


def runner(driver, tmp_path: Path, **kwargs) -> FlowRunner:
    return FlowRunner(
        driver,
        default_timeout=0.05,
        poll_interval=0.01,
        artifacts_dir=tmp_path / "artifacts",
        echo=lambda _msg: None,
        **kwargs,
    )


def calls(driver, name: str):
    return [c for c in driver.calls if c[0] == name]


def test_launch_app_defaults_stop_and_grant_all(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow("appId: com.app\n---\n- launchApp\n")
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    names = [c[0] for c in driver.calls]
    assert names.index("stop_app") < names.index("launch_app")
    assert ("grant_permission", "com.app", "android.permission.CAMERA") in driver.calls


def test_launch_app_permission_deny_and_clear_state(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow(
        "appId: com.app\n---\n- launchApp:\n    clearState: true\n"
        "    stopApp: false\n    permissions:\n      all: unset\n"
        "      notifications: deny\n"
    )
    assert runner(driver, tmp_path).run(flow).ok
    assert ("clear_app", "com.app") in driver.calls
    assert not calls(driver, "stop_app")
    assert (
        "revoke_permission",
        "com.app",
        "android.permission.POST_NOTIFICATIONS",
    ) in driver.calls


def test_tap_on_text_taps_center(write_flow, tmp_path):
    driver = FakeDriver(hierarchy(node("Login", bounds="[100,200][300,400]")))
    flow = write_flow("appId: a.b\n---\n- tapOn: Login\n")
    assert runner(driver, tmp_path).run(flow).ok
    assert ("tap", 200, 300) in driver.calls


def test_tap_on_point_percent(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow('appId: a.b\n---\n- tapOn:\n    point: "50%,25%"\n')
    assert runner(driver, tmp_path).run(flow).ok
    assert ("tap", 500, 500) in driver.calls


def test_tap_on_missing_element_fails_and_stops(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow("appId: a.b\n---\n- tapOn: Nope\n- back\n")
    result = runner(driver, tmp_path).run(flow)
    assert not result.ok
    assert result.skipped == 1
    assert "Nope" in (result.steps[0].error or "")
    assert not calls(driver, "back")


def test_optional_does_not_fail(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow(
        "appId: a.b\n---\n- tapOn:\n    text: Nope\n    optional: true\n- back\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    assert calls(driver, "back")


def test_assert_not_visible(write_flow, tmp_path):
    driver = FakeDriver(hierarchy(node("Spinner", bounds="[0,0][10,10]")))
    ok = write_flow("appId: a.b\n---\n- assertNotVisible: Other\n")
    assert runner(driver, tmp_path).run(ok).ok
    bad = write_flow("appId: a.b\n---\n- assertNotVisible: Spinner\n", "bad.yaml")
    assert not runner(driver, tmp_path).run(bad).ok


def test_extended_wait_until_visible_appears_later(write_flow, tmp_path):
    driver = FakeDriver(
        hierarchy(),
        hierarchy(),
        hierarchy(node("Home", bounds="[0,0][10,10]")),
    )
    flow = write_flow(
        "appId: a.b\n---\n- extendedWaitUntil:\n    visible: Home\n    timeout: 2000\n"
    )
    assert runner(driver, tmp_path).run(flow).ok


def test_input_text_and_keys(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow(
        "appId: a.b\n---\n- inputText: hello\n- eraseText: 3\n- pressKey: Enter\n"
    )
    assert runner(driver, tmp_path).run(flow).ok
    assert calls(driver, "type_text")[0][1] == "hello"
    assert ("erase_text", 3) in driver.calls
    assert ("press_key", "Enter") in driver.calls


def test_swipe_up_moves_finger_up(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow("appId: a.b\n---\n- swipe:\n    direction: UP\n")
    assert runner(driver, tmp_path).run(flow).ok
    _, sx, sy, ex, ey, _dur = calls(driver, "swipe")[0]
    assert ey < sy
    assert sx == ex


def test_repeat_times(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow(
        "appId: a.b\n---\n- repeat:\n    times: 3\n    commands:\n      - back\n"
    )
    assert runner(driver, tmp_path).run(flow).ok
    assert len(calls(driver, "back")) == 3


def test_repeat_while_visible(write_flow, tmp_path):
    driver = FakeDriver(
        hierarchy(node("More", bounds="[0,0][10,10]")),
        hierarchy(node("More", bounds="[0,0][10,10]")),
        hierarchy(),
    )
    flow = write_flow(
        "appId: a.b\n---\n- repeat:\n    while:\n      visible: More\n"
        "    commands:\n      - back\n"
    )
    assert runner(driver, tmp_path).run(flow).ok
    assert calls(driver, "back")


def test_run_flow_file_with_env_and_relative_path(write_flow, tmp_path):
    write_flow("appId: a.b\n---\n- inputText: ${WHO}\n", "sub.yaml")
    flow = write_flow(
        "appId: a.b\n---\n- runFlow:\n    file: sub.yaml\n    env:\n      WHO: Ana\n"
    )
    driver = FakeDriver()
    assert runner(driver, tmp_path).run(flow).ok
    assert calls(driver, "type_text")[0][1] == "Ana"


def test_run_flow_when_false_skips(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow(
        "appId: a.b\n---\n- runFlow:\n    when:\n      platform: iOS\n"
        "    commands:\n      - back\n"
    )
    assert runner(driver, tmp_path).run(flow).ok
    assert not calls(driver, "back")


def test_retry_recovers(write_flow, tmp_path):
    driver = FakeDriver(
        hierarchy(),
        hierarchy(),
        hierarchy(),
        hierarchy(),
        hierarchy(node("Flaky", bounds="[0,0][10,10]")),
    )
    flow = write_flow(
        "appId: a.b\n---\n- retry:\n    maxRetries: 3\n    commands:\n"
        "      - tapOn: Flaky\n"
    )
    assert runner(driver, tmp_path).run(flow).ok


def test_cli_env_overrides_header_env(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow("appId: a.b\nenv:\n  X: header\n---\n- inputText: ${X}\n")
    assert runner(driver, tmp_path, env={"X": "cli"}).run(flow).ok
    assert calls(driver, "type_text")[0][1] == "cli"


def test_hooks_run_and_on_complete_runs_after_failure(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow(
        "appId: a.b\nonFlowStart:\n  - hideKeyboard\nonFlowComplete:\n  - back\n"
        "---\n- tapOn: Missing\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert not result.ok
    assert calls(driver, "hide_keyboard")
    assert calls(driver, "back")


def test_failure_saves_artifacts(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow("appId: a.b\n---\n- assertVisible: Nope\n")
    result = runner(driver, tmp_path).run(flow)
    assert any(p.suffix == ".xml" for p in result.artifacts)


def test_invalid_flow_raises_before_running(write_flow, tmp_path):
    from pyestro.flow import FlowError

    driver = FakeDriver()
    flow = write_flow("appId: a.b\n---\n- back\n- sleep: 10\n")
    with pytest.raises(FlowError):
        runner(driver, tmp_path).run(flow)
    assert not driver.calls


class _RecordingDriver(FakeDriver):
    def __init__(self, *screens, fail_start: bool = False):
        super().__init__(*screens)
        self.fail_start = fail_start
        self.next_id = 0

    def start_recording(self, remote_path: str | None = None):
        self.calls.append(("start_recording", remote_path))
        if self.fail_start:
            from pyestro.android import AndroidError

            raise AndroidError("screenrecord unavailable")
        self.next_id += 1
        return f"proc-{self.next_id}"

    def stop_recording(self, proc, destination, remote_path: str | None = None):
        dest = Path(destination)
        self.calls.append(("stop_recording", proc, dest, remote_path))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp4")
        return dest


def test_start_and_stop_recording_saves_under_artifacts(write_flow, tmp_path):
    driver = _RecordingDriver()
    flow = write_flow(
        "appId: a.b\n---\n"
        "- startRecording:\n"
        "    path: recordings/onboarding\n"
        "    label: Capture onboarding\n"
        "- back\n"
        "- stopRecording\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    expected = tmp_path / "artifacts" / "recordings" / "onboarding.mp4"
    assert result.video_path == expected
    assert expected in result.artifacts
    assert expected.read_bytes() == b"mp4"
    names = [c[0] for c in driver.calls]
    assert names == ["start_recording", "back", "stop_recording"]


def test_stop_recording_without_active_recording_is_noop(write_flow, tmp_path):
    driver = _RecordingDriver()
    flow = write_flow("appId: a.b\n---\n- stopRecording\n- back\n")
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    assert not calls(driver, "stop_recording")
    assert calls(driver, "back")


def test_start_recording_optional_ignores_driver_failure(write_flow, tmp_path):
    driver = _RecordingDriver(fail_start=True)
    flow = write_flow(
        "appId: a.b\n---\n"
        "- startRecording:\n"
        "    path: demo\n"
        "    optional: true\n"
        "- back\n"
        "- stopRecording\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    assert calls(driver, "back")
    assert not calls(driver, "stop_recording")


def test_start_recording_non_optional_fails_on_driver_error(write_flow, tmp_path):
    driver = _RecordingDriver(fail_start=True)
    flow = write_flow("appId: a.b\n---\n- startRecording: demo\n- back\n")
    result = runner(driver, tmp_path).run(flow)
    assert not result.ok
    assert result.skipped == 1
    assert not calls(driver, "back")


def test_start_recording_while_active_fails_unless_optional(write_flow, tmp_path):
    driver = _RecordingDriver()
    flow = write_flow(
        "appId: a.b\n---\n- startRecording: first.mp4\n- startRecording: second\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert not result.ok
    assert "already in progress" in (result.steps[1].error or "")
    # Flow cleanup in finally still stopped the first recording cleanly.
    assert len(calls(driver, "stop_recording")) == 1
    assert (tmp_path / "artifacts" / "first.mp4").is_file()


def test_recording_stopped_and_saved_on_step_failure(write_flow, tmp_path):
    driver = _RecordingDriver()
    flow = write_flow(
        "appId: a.b\n---\n"
        "- startRecording: failure_run\n"
        "- assertVisible: Missing\n"
        "- stopRecording\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert not result.ok
    expected = tmp_path / "artifacts" / "failure_run.mp4"
    assert expected.is_file()
    assert expected in result.artifacts
    assert len(calls(driver, "stop_recording")) == 1


def test_multiple_recordings_appear_in_html_report(write_flow, tmp_path):
    from pyestro.reports import generate_html_report

    driver = _RecordingDriver()
    flow = write_flow(
        "appId: a.b\n---\n"
        "- startRecording: part1\n"
        "- back\n"
        "- stopRecording\n"
        "- startRecording: part2\n"
        "- back\n"
        "- stopRecording\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    report_path = generate_html_report(result, tmp_path / "artifacts" / "report.html")
    content = report_path.read_text(encoding="utf-8")
    assert "part1.mp4" in content
    assert "part2.mp4" in content


def test_device_and_theme_commands_execution(write_flow, tmp_path):
    class ThemeDriver(FakeDriver):
        def __init__(self, dark: bool = False):
            super().__init__()
            self.dark = dark

        def is_dark_mode(self) -> bool:
            self.calls.append(("is_dark_mode",))
            return self.dark

    media = tmp_path / "pic.png"
    media.write_bytes(b"png")
    driver = ThemeDriver(dark=True)
    flow = write_flow(
        "appId: com.app\n---\n"
        "- setPermissions:\n    permissions:\n      notifications: deny\n"
        "- setOrientation: LANDSCAPE_LEFT\n"
        "- setAirplaneMode: enabled\n"
        "- toggleAirplaneMode\n"
        "- addMedia:\n    - pic.png\n"
        "- setDarkMode: true\n"
        "- toggleDarkMode\n"
        "- assertDarkMode\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    assert (
        "revoke_permission",
        "com.app",
        "android.permission.POST_NOTIFICATIONS",
    ) in driver.calls
    assert ("set_orientation", "LANDSCAPE_LEFT") in driver.calls
    assert ("set_airplane_mode", True) in driver.calls
    assert ("toggle_airplane_mode",) in driver.calls
    assert ("add_media", media.resolve()) in driver.calls
    assert ("set_dark_mode", True) in driver.calls
    assert ("toggle_dark_mode",) in driver.calls
    assert ("is_dark_mode",) in driver.calls


def test_assert_light_mode_fails_when_dark(write_flow, tmp_path):
    class DarkDriver(FakeDriver):
        def is_dark_mode(self) -> bool:
            return True

    flow = write_flow("appId: com.app\n---\n- assertLightMode\n")
    result = runner(DarkDriver(), tmp_path).run(flow)
    assert not result.ok
    assert "light mode" in (result.steps[0].error or "")


def test_add_media_missing_file_fails(write_flow, tmp_path):
    flow = write_flow("appId: com.app\n---\n- addMedia:\n    - missing.png\n")
    result = runner(FakeDriver(), tmp_path).run(flow)
    assert not result.ok
    assert "not found" in (result.steps[0].error or "")


def test_copy_text_from_then_paste_and_input_text(write_flow, tmp_path):
    driver = FakeDriver(hierarchy(node("CODE-1234", rid="otp", bounds="[0,0][10,10]")))
    flow = write_flow(
        "appId: a.b\n---\n"
        "- copyTextFrom:\n    id: otp\n"
        "- pasteText\n"
        "- inputText: 'Code: ${maestro.copiedText}'\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    assert calls(driver, "type_text") == [
        ("type_text", "CODE-1234"),
        ("type_text", "Code: CODE-1234"),
    ]


def test_copied_text_is_usable_in_selectors(write_flow, tmp_path):
    driver = FakeDriver(
        hierarchy(
            node("TOKEN42", rid="src", bounds="[0,0][10,10]"),
            node("TOKEN42", rid="dst", bounds="[0,20][10,30]"),
        )
    )
    flow = write_flow(
        "appId: a.b\n---\n"
        "- copyTextFrom:\n    id: src\n"
        "- assertVisible: '${maestro.copiedText}'\n"
    )
    assert runner(driver, tmp_path).run(flow).ok


def test_set_clipboard_feeds_paste_text(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow(
        "appId: a.b\n---\n- setClipboard: hello@example.com\n- pasteText\n"
    )
    assert runner(driver, tmp_path).run(flow).ok
    assert ("type_text", "hello@example.com") in driver.calls


def test_paste_text_without_copy_fails(write_flow, tmp_path):
    flow = write_flow("appId: a.b\n---\n- pasteText\n")
    result = runner(FakeDriver(), tmp_path).run(flow)
    assert not result.ok
    assert "nothing to paste" in (result.steps[0].error or "")


def test_copied_text_unset_fails_with_clear_message(write_flow, tmp_path):
    flow = write_flow("appId: a.b\n---\n- inputText: ${maestro.copiedText}\n")
    result = runner(FakeDriver(), tmp_path).run(flow)
    assert not result.ok
    assert "copyTextFrom or setClipboard" in (result.steps[0].error or "")


def test_set_location_calls_driver(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow(
        "appId: a.b\n---\n- setLocation:\n    latitude: 52.36\n    longitude: 4.88\n"
    )
    assert runner(driver, tmp_path).run(flow).ok
    assert ("set_location", 52.36, 4.88) in driver.calls


def test_travel_interpolates_points(write_flow, tmp_path):
    driver = FakeDriver()
    flow = write_flow(
        "appId: a.b\n---\n- travel:\n    points:\n"
        "      - '0.0, 0.0'\n      - '0.0, 0.01'\n    speed: 1000\n    timeout: 500\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    locs = calls(driver, "set_location")
    assert locs[0] == ("set_location", 0.0, 0.0)
    assert locs[-1] == ("set_location", 0.0, 0.01)


def test_assert_screenshot_passes_and_fails_by_threshold(write_flow, tmp_path):
    from pyestro.android import compare_png

    from .test_review_fixes import _make_rgba_png

    ref_png = _make_rgba_png(10, 10)
    diff_png = _make_rgba_png(10, 10)
    # 100% match against self, 0% when comparing different sizes raises AndroidError
    assert compare_png(ref_png, ref_png) == 100.0

    ref_file = tmp_path / "baseline.png"
    ref_file.write_bytes(ref_png)

    class ShotDriver(FakeDriver):
        def __init__(self, png_bytes: bytes, *screens):
            super().__init__(*screens)
            self.png_bytes = png_bytes

        def screenshot(self, path: Path) -> Path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(self.png_bytes)
            return p

    ok_flow = write_flow("appId: a.b\n---\n- assertScreenshot: baseline.png\n")
    assert runner(ShotDriver(diff_png), tmp_path).run(ok_flow).ok

    # Crop a 10x10 screen to [0,0][5,5] and compare against a 5x5 baseline.
    import struct
    import zlib

    from pyestro.android import _PNG_SIGNATURE, _png_chunk

    raw_zero = b"\x00" + b"\x00\x00\x00\x00" * 5
    bad_5x5 = b"".join(
        [
            _PNG_SIGNATURE,
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 5, 5, 8, 6, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(raw_zero * 5)),
            _png_chunk(b"IEND", b""),
        ]
    )
    (tmp_path / "card.png").write_bytes(bad_5x5)
    fail_flow = write_flow(
        "appId: a.b\n---\n- assertScreenshot:\n    path: card.png\n"
        "    cropOn: Card\n    thresholdPercentage: 95\n"
    )
    res = runner(
        ShotDriver(ref_png, hierarchy(node("Card", bounds="[0,0][5,5]"))),
        tmp_path,
    ).run(fail_flow)
    assert not res.ok
    assert "below 95%" in (res.steps[0].error or "")


def test_screen_cache_reuses_dump_and_invalidates_on_action(write_flow, tmp_path):
    class CountingDriver(FakeDriver):
        def __init__(self, *screens):
            super().__init__(*screens)
            self.dumps = 0

        def dump_hierarchy(self):
            self.dumps += 1
            return super().dump_hierarchy()

    screen = hierarchy(
        node("A", bounds="[0,0][10,10]"),
        node("B", bounds="[0,20][10,30]"),
    )
    flow = write_flow(
        "appId: a.b\n---\n- assertVisible: A\n- assertVisible: B\n"
        "- back\n- assertVisible: A\n"
    )

    cached = CountingDriver(screen)
    assert runner(cached, tmp_path).run(flow).ok
    # First two assertions share 1 dump; `back` invalidates before the 3rd assertion.
    assert cached.dumps == 2

    uncached = CountingDriver(screen)
    assert runner(uncached, tmp_path, screen_cache_ttl=0.0).run(flow).ok
    assert uncached.dumps == 3


def test_dotenv_file_loaded_with_cli_and_header_precedence(
    write_flow, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("USER=from_dotenv\nTOKEN=abc\n", encoding="utf-8")
    driver = FakeDriver()
    flow = write_flow(
        "appId: a.b\nenv:\n  USER: from_header\n---\n- inputText: ${USER}:${TOKEN}\n"
    )
    assert runner(driver, tmp_path).run(flow).ok
    assert ("type_text", "from_header:abc") in driver.calls

    driver2 = FakeDriver()
    assert runner(driver2, tmp_path, env={"USER": "from_cli"}).run(flow).ok
    assert ("type_text", "from_cli:abc") in driver2.calls


def test_dotenv_discovered_from_flow_ancestor_and_explicit_env_file(
    tmp_path, monkeypatch
):
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    project = tmp_path / "project"
    flow_dir = project / ".maestro"
    flow_dir.mkdir(parents=True)
    (project / ".env").write_text("USER=from_project_root\n", encoding="utf-8")
    custom = project / ".env.staging"
    custom.write_text("USER=from_staging\n", encoding="utf-8")

    flow = flow_dir / "login.yaml"
    flow.write_text("appId: a.b\n---\n- inputText: ${USER}\n", encoding="utf-8")

    driver = FakeDriver()
    assert runner(driver, tmp_path).run(flow).ok
    assert ("type_text", "from_project_root") in driver.calls

    driver2 = FakeDriver()
    assert runner(driver2, tmp_path, env_file=custom).run(flow).ok
    assert ("type_text", "from_staging") in driver2.calls

    with pytest.raises(FlowError, match="Env file not found"):
        runner(FakeDriver(), tmp_path, env_file=project / "nope.env").run(flow)


def test_cli_validate_supports_env_file(tmp_path):
    from typer.testing import CliRunner

    from pyestro.cli import app

    env_file = tmp_path / ".env.custom"
    env_file.write_text("USER=alice\n", encoding="utf-8")
    flow = tmp_path / "flow.yaml"
    flow.write_text("appId: a.b\n---\n- inputText: ${USER}\n", encoding="utf-8")

    cli = CliRunner()
    res = cli.invoke(app, ["validate", str(flow), "--env-file", str(env_file)])
    assert res.exit_code == 0, res.output

    bad = cli.invoke(
        app, ["validate", str(flow), "--env-file", str(tmp_path / "no.env")]
    )
    assert bad.exit_code == 1
