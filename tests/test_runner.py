from __future__ import annotations

from pathlib import Path

import pytest

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
