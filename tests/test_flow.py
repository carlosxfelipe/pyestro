"""Maestro syntax compatibility guarantees."""

from __future__ import annotations

import pytest

from pyestro.flow import (
    FlowError,
    compile_commands,
    interpolate_string,
    parse_config,
    read_flow_file,
)


def compile_text(write_flow, text: str, env: dict[str, str] | None = None):
    header, raw = read_flow_file(write_flow(text))
    config = parse_config(header)
    return config, compile_commands(raw, {**config.env, **(env or {})})


def test_official_maestro_sample_is_accepted(write_flow):
    config, commands = compile_text(
        write_flow,
        """
appId: com.example.app
name: Login
tags: [smoke, login]
env:
  USER: ana
onFlowStart:
  - launchApp
---
- launchApp:
    appId: com.example.app
    clearState: true
    stopApp: false
    permissions:
      all: deny
      notifications: allow
- tapOn: "Log.*In"
- tapOn:
    id: login_button
    enabled: true
    below: "Password"
    retryTapIfNoChange: false
    repeat: 2
    delay: 200
- tapOn:
    point: "50%,50%"
- longPressOn: "Item"
- doubleTapOn:
    text: "Item"
    index: 1
- inputText: ${USER}
- eraseText: 5
- pressKey: Volume Up
- hideKeyboard
- back
- scroll
- scrollUntilVisible:
    element:
      text: "Footer"
    direction: DOWN
    timeout: 10000
    speed: 60
- swipe:
    direction: LEFT
- swipe:
    start: "90%,50%"
    end: "10%,50%"
    duration: 500
- assertVisible: "Welcome"
- assertNotVisible:
    id: spinner
- extendedWaitUntil:
    visible: "Home"
    timeout: 10000
- extendedWaitUntil:
    notVisible:
      id: spinner
    timeout: 15000
- waitForAnimationToEnd:
    timeout: 3000
- takeScreenshot: home
- startRecording: session
- startRecording:
    path: recordings/onboarding
    label: Capture onboarding
    optional: true
- stopRecording
- openLink: https://example.com
- repeat:
    times: 3
    commands:
      - scroll
- retry:
    maxRetries: 2
    commands:
      - tapOn: "Flaky"
- runFlow:
    when:
      visible: "Popup"
    commands:
      - tapOn: "Close"
- runFlow:
    file: sub.yaml
    env:
      A: "1"
- tapOn:
    text: "Optional thing"
    optional: true
    label: "Maybe"
""",
    )
    assert config.tags == ["smoke", "login"]
    assert len(commands) == 30


@pytest.mark.parametrize(
    "command",
    [
        "- sleep: 2000",
        "- waitFor: Home",
        "- wakeUp",
        "- dismissSystemDialogs",
        "- disableAnimations",
        "- grantPermissions: [CAMERA]",
    ],
)
def test_androflow_only_commands_are_rejected(write_flow, command):
    with pytest.raises(FlowError, match="Not a Maestro command"):
        compile_text(write_flow, f"appId: a.b\n---\n{command}\n")


@pytest.mark.parametrize(
    "selector",
    [
        "contentDesc: Home",
        "resourceId: x",
        "text: Home\n    contains: true",
        "text: Home\n    regex: true",
        "text: Home\n    timeout: 5000",
    ],
)
def test_androflow_only_selector_keys_are_rejected(write_flow, selector):
    with pytest.raises(FlowError, match="Unknown selector key"):
        compile_text(write_flow, f"appId: a.b\n---\n- tapOn:\n    {selector}\n")


def test_steps_object_format_is_rejected(write_flow):
    with pytest.raises(FlowError, match="Expected format"):
        compile_text(write_flow, "appId: a.b\nsteps:\n  - launchApp\n")


def test_durations_must_be_milliseconds(write_flow):
    with pytest.raises(FlowError, match="milliseconds"):
        compile_text(
            write_flow,
            "appId: a.b\n---\n- extendedWaitUntil:\n    visible: x\n    timeout: 5s\n",
        )


def test_maestro_commands_not_implemented_get_specific_error(write_flow):
    with pytest.raises(
        FlowError, match="Maestro command that Pyestro does not support"
    ):
        compile_text(write_flow, "appId: a.b\n---\n- evalScript: ${1 + 1}\n")


def test_unknown_command_key_is_rejected(write_flow):
    with pytest.raises(FlowError, match="unknown key\\(s\\)"):
        compile_text(write_flow, "appId: a.b\n---\n- launchApp:\n    foo: bar\n")


def test_unknown_key_in_nested_commands_is_rejected(write_flow):
    with pytest.raises(FlowError, match="Command repeat"):
        compile_text(
            write_flow,
            "appId: a.b\n---\n- repeat:\n    times: 2\n"
            "    commands:\n      - sleep: 1\n",
        )


def test_unknown_press_key_is_rejected(write_flow):
    with pytest.raises(FlowError, match="unknown key"):
        compile_text(write_flow, "appId: a.b\n---\n- pressKey: Banana\n")


def test_repeat_needs_times_or_while(write_flow):
    with pytest.raises(FlowError, match="exactly one"):
        compile_text(
            write_flow, "appId: a.b\n---\n- repeat:\n    commands:\n      - back\n"
        )


def test_when_true_key_parsed_by_yaml_as_boolean(write_flow):
    _, commands = compile_text(
        write_flow,
        "appId: a.b\n---\n- runFlow:\n    when:\n      true: ${X}\n    commands:\n"
        "      - back\n",
        {"X": "false"},
    )
    assert commands[0].spec["when"] == {"true": "false"}


def test_multi_key_command_item_is_rejected(write_flow):
    with pytest.raises(FlowError, match="single key"):
        compile_text(write_flow, "appId: a.b\n---\n- tapOn: a\n  back: b\n")


def test_run_flow_inline_env_is_visible_to_nested_commands(write_flow):
    _, commands = compile_text(
        write_flow,
        "appId: a.b\n---\n- runFlow:\n    env:\n      NAME: Bia\n    commands:\n"
        "      - inputText: ${NAME}\n",
    )
    assert commands[0].nested[0].spec["text"] == "Bia"


class TestRecordingSyntax:
    def test_start_recording_shorthand_and_map(self, write_flow):
        _, commands = compile_text(
            write_flow,
            "appId: a.b\n---\n"
            "- startRecording: demo\n"
            "- startRecording:\n"
            "    path: sub/clip\n"
            "    label: Onboarding\n"
            "    optional: true\n"
            "- stopRecording\n"
            "- stopRecording:\n"
            "    label: Done\n",
        )
        assert commands[0].spec == {"path": "demo"}
        assert not commands[0].optional
        assert commands[1].spec == {"path": "sub/clip"}
        assert commands[1].label == "Onboarding"
        assert commands[1].optional is True
        assert commands[2].name == "stopRecording"
        assert commands[3].label == "Done"

    @pytest.mark.parametrize(
        "yaml_cmd",
        [
            "- startRecording",
            "- startRecording: '   '",
            "- startRecording:\n    label: Missing path",
            "- startRecording:\n    path: '   '",
            "- startRecording:\n    path: demo\n    unknown: 1",
            "- stopRecording: demo",
        ],
    )
    def test_invalid_recording_commands_are_rejected(self, write_flow, yaml_cmd):
        with pytest.raises(FlowError):
            compile_text(write_flow, f"appId: a.b\n---\n{yaml_cmd}\n")


class TestInterpolation:
    def test_plain_and_default(self):
        env = {"A": "x", "EMPTY": ""}
        assert interpolate_string("${A}-${B || 'y'}", env) == "x-y"
        assert interpolate_string("${EMPTY || 'fallback'}", env) == "fallback"
        assert interpolate_string("${EMPTY ?? 'fallback'}", env) == ""

    def test_undefined_variable_fails(self):
        with pytest.raises(FlowError, match="not defined"):
            interpolate_string("${NOPE}", {})

    def test_arbitrary_javascript_is_rejected(self):
        with pytest.raises(FlowError, match="not supported"):
            interpolate_string("${A.toUpperCase()}", {"A": "x"})

    def test_load_env_file_parses_quotes_and_comments(self, tmp_path):
        from pyestro.flow import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text(
            "# comment\n\n"
            "HOST=https://api.local#frag # inline note\n"
            "export PASS='s3cr3t'\n"
            'USER="ana # not comment"\n'
            'GREETING="hi \\"world\\"" # trailing comment\n',
            encoding="utf-8",
        )
        assert load_env_file(env_file) == {
            "HOST": "https://api.local#frag",
            "PASS": "s3cr3t",
            "USER": "ana # not comment",
            "GREETING": 'hi "world"',
        }
        assert load_env_file(tmp_path / "missing.env") == {}

    def test_find_env_file_walks_up_and_stops_at_project_root(self, tmp_path):
        from pyestro.flow import find_env_file, resolve_dotenv

        # Outer .env outside project root must not leak past .git boundary.
        (tmp_path / ".env").write_text("LEAK=outer\n", encoding="utf-8")
        project = tmp_path / "app"
        (project / ".git").mkdir(parents=True)
        nested_flow = project / ".maestro" / "sub" / "smoke.yaml"
        nested_flow.parent.mkdir(parents=True)
        nested_flow.write_text("appId: a.b\n---\n- launchApp\n", encoding="utf-8")

        assert find_env_file(nested_flow) is None

        project_env = project / ".env"
        project_env.write_text("API_URL=https://staging.local\n", encoding="utf-8")
        assert find_env_file(nested_flow) == project_env.resolve()
        assert resolve_dotenv(nested_flow) == {"API_URL": "https://staging.local"}

        custom_env = project / ".env.prod"
        custom_env.write_text("API_URL=https://prod.local\n", encoding="utf-8")
        assert resolve_dotenv(nested_flow, custom_env) == {
            "API_URL": "https://prod.local"
        }

        with pytest.raises(FlowError, match="Env file not found"):
            resolve_dotenv(nested_flow, project / "missing.env")


class TestClipboardAndLocation:
    def test_valid_commands(self, write_flow):
        _, commands = compile_text(
            write_flow,
            "appId: a.b\n---\n"
            "- copyTextFrom:\n    id: otp\n    optional: true\n"
            "- copyTextFrom: Total\n"
            "- pasteText\n"
            "- setClipboard: abc\n"
            "- inputText: ${maestro.copiedText}\n"
            "- setLocation:\n    latitude: -23.55\n    longitude: '-46.63'\n"
            "- travel:\n    points:\n      - '48.85, 2.29'\n      - '48.86, 2.30'\n"
            "    speed: 100\n    timeout: 5000\n"
            "- assertScreenshot: home.png\n"
            "- assertScreenshot:\n    path: card.png\n"
            "    thresholdPercentage: 98\n    cropOn: Card\n",
        )
        assert commands[0].optional is True
        assert commands[2].name == "pasteText"
        assert commands[3].spec == {"text": "abc"}
        # Resolved at run time, so the token survives compilation.
        assert commands[4].spec == {"text": "${maestro.copiedText}"}
        assert commands[5].spec == {"latitude": -23.55, "longitude": -46.63}
        assert commands[6].spec == {
            "points": [(48.85, 2.29), (48.86, 2.30)],
            "speed": 100.0,
            "timeout": 5.0,
        }
        assert commands[7].spec == {
            "path": "home.png",
            "cropOn": None,
            "thresholdPercentage": 95.0,
        }
        assert commands[8].spec["thresholdPercentage"] == 98.0
        assert commands[8].spec["cropOn"].text == "Card"

    @pytest.mark.parametrize(
        "yaml_cmd",
        [
            "- copyTextFrom",
            "- copyTextFrom:\n    point: 50%,50%",
            "- pasteText: x",
            "- setClipboard",
            "- setClipboard:\n    text: a",
            "- setLocation: 1,2",
            "- setLocation:\n    latitude: 1",
            "- setLocation:\n    latitude: 91\n    longitude: 0",
            "- setLocation:\n    latitude: 0\n    longitude: abc",
            "- setLocation:\n    latitude: 0\n    longitude: 0\n    alt: 1",
            "- travel: 1",
            "- travel:\n    points: []",
            "- travel:\n    points:\n      - 'invalid'",
            "- travel:\n    points:\n      - '0, 0'\n    speed: 0",
            "- assertScreenshot",
            "- assertScreenshot:\n    path: a.png\n    thresholdPercentage: 101",
            "- assertScreenshot:\n    path: a.png\n    extra: 1",
        ],
    )
    def test_invalid_commands_rejected(self, write_flow, yaml_cmd):
        with pytest.raises(FlowError):
            compile_text(write_flow, f"appId: a.b\n---\n{yaml_cmd}\n")


class TestDeviceAndThemeCommands:
    def test_valid_device_and_theme_commands(self, write_flow):
        _, commands = compile_text(
            write_flow,
            "appId: a.b\n---\n"
            "- setPermissions:\n    permissions:\n      camera: allow\n"
            "- setOrientation: landscape_left\n"
            "- setAirplaneMode: enabled\n"
            "- toggleAirplaneMode\n"
            "- addMedia:\n    - ./photo.png\n"
            "- setDarkMode: true\n"
            "- toggleDarkMode\n"
            "- assertDarkMode\n"
            "- assertLightMode\n",
        )
        assert commands[0].spec == {
            "appId": None,
            "permissions": {"camera": "allow"},
        }
        assert commands[1].spec == {"orientation": "LANDSCAPE_LEFT"}
        assert commands[2].spec == {"enabled": True}
        assert commands[3].name == "toggleAirplaneMode"
        assert commands[4].spec == {"files": ["./photo.png"]}
        assert commands[5].spec == {"enabled": True}
        assert [c.name for c in commands[6:]] == [
            "toggleDarkMode",
            "assertDarkMode",
            "assertLightMode",
        ]

    @pytest.mark.parametrize(
        "yaml_cmd",
        [
            "- setPermissions",
            "- setPermissions:\n    appId: a.b",
            "- setPermissions:\n    permissions: {}\n",
            "- setPermissions:\n    permissions:\n      all: allow\n    extra: 1",
            "- setOrientation: DIAGONAL",
            "- setOrientation: 123",
            "- setAirplaneMode: true",
            "- setAirplaneMode: maybe",
            "- addMedia: []",
            "- addMedia: photo.png",
            "- setDarkMode",
            "- setDarkMode: dark",
        ],
    )
    def test_invalid_device_and_theme_commands_rejected(self, write_flow, yaml_cmd):
        with pytest.raises(FlowError):
            compile_text(write_flow, f"appId: a.b\n---\n{yaml_cmd}\n")
