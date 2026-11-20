from __future__ import annotations

import os
import random
import string
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .android import SCREENRECORD_MAX_SECONDS, AndroidDriver, AndroidError, crop_png
from .flow import (
    Command,
    FlowConfig,
    FlowError,
    compile_commands,
    parse_config,
    permission_names,
    read_flow_file,
)
from .selectors import (
    Selector,
    SelectorError,
    UiElement,
    find_element,
    parse_elements,
)

# Maestro waits up to ~7s for an element before failing.
DEFAULT_TIMEOUT = 7.0
# Guard against runFlow files that include each other.
MAX_SUBFLOW_DEPTH = 20

_FALSY = {"", "false", "0", "undefined", "null", "nan"}


@dataclass(frozen=True)
class StepResult:
    index: int
    command: str
    passed: bool
    error: str | None = None
    duration: float = 0.0
    hook: bool = False  # onFlowStart/onFlowComplete failure (not a flow command)


@dataclass
class FlowResult:
    path: Path
    total: int
    steps: list[StepResult] = field(default_factory=list)
    duration: float = 0.0
    video_path: Path | None = None
    artifacts: list[Path] = field(default_factory=list)

    @classmethod
    def load_failure(cls, path: Path, error: str) -> FlowResult:
        """Result of a flow that failed before running its first command."""
        return cls(
            path=path,
            total=1,
            steps=[StepResult(1, "load", passed=False, error=error)],
        )

    @property
    def passed(self) -> int:
        return sum(step.passed for step in self.steps)

    @property
    def failed(self) -> int:
        return len(self.steps) - self.passed

    @property
    def executed(self) -> int:
        """Flow commands that ran (hook failures are not counted)."""
        return sum(not step.hook for step in self.steps)

    @property
    def skipped(self) -> int:
        return max(0, self.total - self.executed)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def peek_config(path: str | Path) -> FlowConfig:
    """Read only a flow's header (used to filter by tags)."""
    header, _ = read_flow_file(Path(path))
    return parse_config(header)


def _env_from_os() -> dict[str, str]:
    """As in Maestro, shell variables prefixed with MAESTRO_ become env."""
    prefix = "MAESTRO_"
    return {k[len(prefix) :]: v for k, v in os.environ.items() if k.startswith(prefix)}


class FlowRunner:
    def __init__(
        self,
        driver: AndroidDriver,
        default_timeout: float = DEFAULT_TIMEOUT,
        *,
        env: dict[str, str] | None = None,
        artifacts_dir: str | Path = "artifacts",
        poll_interval: float = 0.25,
        transliterate: bool = False,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.driver = driver
        self.default_timeout = default_timeout
        self.env = dict(env or {})
        self.artifacts_dir = Path(artifacts_dir)
        self.poll_interval = poll_interval
        self.transliterate = transliterate
        self.echo = echo
        self._app_id: str | None = None
        self._flow_depth = 0
        self._launch_count = 0
        self._env: dict[str, str] = {}
        self._base_dir: Path = Path.cwd()
        self._current_result: FlowResult | None = None
        self._active_recording: tuple[Any, Path, str, float] | None = None
        self._handlers: dict[str, Callable[[Command], None]] = {
            "launchApp": self._launch_app,
            "stopApp": self._stop_app,
            "killApp": self._stop_app,
            "clearState": self._clear_state,
            "setPermissions": self._set_permissions,
            "tapOn": self._tap_on,
            "doubleTapOn": self._tap_on,
            "longPressOn": self._tap_on,
            "inputText": self._input_text,
            "inputRandomText": self._input_random,
            "inputRandomNumber": self._input_random,
            "inputRandomEmail": self._input_random,
            "inputRandomPersonName": self._input_random,
            "eraseText": self._erase_text,
            "pressKey": self._press_key,
            "hideKeyboard": lambda _c: self.driver.hide_keyboard(),
            "back": lambda _c: self.driver.back(),
            "scroll": lambda _c: self.driver.scroll("DOWN"),
            "clearKeychain": self._clear_keychain,
            "waitForAnimationToEnd": self._wait_animation,
            "swipe": self._swipe,
            "scrollUntilVisible": self._scroll_until_visible,
            "openLink": self._open_link,
            "assertVisible": self._assert_visible,
            "assertNotVisible": self._assert_not_visible,
            "extendedWaitUntil": self._extended_wait_until,
            "takeScreenshot": self._take_screenshot,
            "startRecording": self._start_recording,
            "stopRecording": self._stop_recording,
            "setOrientation": lambda c: self.driver.set_orientation(
                c.spec["orientation"]
            ),
            "setAirplaneMode": lambda c: self.driver.set_airplane_mode(
                c.spec["enabled"]
            ),
            "toggleAirplaneMode": lambda _c: self.driver.toggle_airplane_mode(),
            "addMedia": self._add_media,
            "setDarkMode": lambda c: self.driver.set_dark_mode(c.spec["enabled"]),
            "toggleDarkMode": lambda _c: self.driver.toggle_dark_mode(),
            "assertDarkMode": self._assert_dark_mode,
            "assertLightMode": self._assert_light_mode,
            "repeat": self._repeat,
            "retry": self._retry,
            "runFlow": self._run_flow,
        }

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _build_env(self, config: FlowConfig) -> dict[str, str]:
        return {**_env_from_os(), **config.env, **self.env}

    def load_flow(
        self, path: str | Path
    ) -> tuple[FlowConfig, list[Command], list[Command], list[Command]]:
        """Validate the whole flow before running any command."""
        flow_path = Path(path)
        header, raw_commands = read_flow_file(flow_path)
        config = parse_config(header)
        env = self._build_env(config)
        commands = compile_commands(raw_commands, env)
        on_start = compile_commands(config.on_start, env, "onFlowStart › ")
        on_complete = compile_commands(config.on_complete, env, "onFlowComplete › ")
        return config, commands, on_start, on_complete

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run(self, path: str | Path, *, record: bool = False) -> FlowResult:
        flow_path = Path(path)
        config, commands, on_start, on_complete = self.load_flow(flow_path)
        self._app_id = config.app_id
        self._env = self._build_env(config)
        self._base_dir = flow_path.resolve().parent

        recording_proc = None
        video_dest = self.artifacts_dir / f"{flow_path.stem}.mp4"
        if record and hasattr(self.driver, "start_recording"):
            self.echo("Starting screen recording via ADB screenrecord...")
            recording_proc = self.driver.start_recording()

        result = FlowResult(path=flow_path, total=len(commands))
        self._current_result = result
        self._active_recording = None
        flow_start = time.monotonic()
        self.echo(f"Pyestro: {flow_path} ({len(commands)} commands)")
        try:
            ok = self._run_hook("onFlowStart", on_start, flow_path, result)
            if ok:
                for index, command in enumerate(commands, start=1):
                    if not self._run_step(index, command, flow_path, result):
                        break
            self._run_hook("onFlowComplete", on_complete, flow_path, result)
        finally:
            result.duration = round(time.monotonic() - flow_start, 3)
            if self._active_recording is not None:
                self._stop_recording()
            if recording_proc is not None and hasattr(self.driver, "stop_recording"):
                self.echo("Stopping screen recording...")
                self._warn_if_screenrecord_truncated(result.duration)
                saved_video = self.driver.stop_recording(recording_proc, video_dest)
                if saved_video:
                    self._record_video_artifact(result, saved_video)
                    self.echo(f"       Execution video: {saved_video}")
            self._current_result = None

        self.echo(
            f"\nResult: {result.passed} ok, {result.failed} failed, "
            f"{result.skipped} skipped of {result.total} commands "
            f"({result.duration:.2f}s)."
        )
        return result

    def _run_step(
        self, index: int, command: Command, flow_path: Path, result: FlowResult
    ) -> bool:
        start = time.monotonic()
        error: str | None = None
        try:
            self._execute(command)
        except (FlowError, AndroidError, ValueError) as exc:
            error = str(exc)
        except Exception as exc:  # unexpected: show the type for debugging
            error = f"{type(exc).__name__}: {exc}"
        duration = round(time.monotonic() - start, 3)
        title = command.name + (f" [{command.label}]" if command.label else "")
        if error is None:
            result.steps.append(
                StepResult(index, command.name, True, duration=duration)
            )
            self.echo(f"  PASS {index:02d} {title} ({int(duration * 1000)}ms)")
            return True
        result.steps.append(
            StepResult(index, command.name, False, error=error, duration=duration)
        )
        self.echo(f"  FAIL {index:02d} {title}: {error} ({int(duration * 1000)}ms)")
        self._capture_failure(flow_path, index)
        return False

    def _run_hook(
        self, name: str, commands: list[Command], flow_path: Path, result: FlowResult
    ) -> bool:
        if not commands:
            return True
        try:
            self._run_commands(commands)
        except (FlowError, AndroidError, ValueError) as exc:
            result.steps.append(
                StepResult(len(result.steps) + 1, name, False, str(exc), hook=True)
            )
            self.echo(f"  FAIL {name}: {exc}")
            self._capture_failure(flow_path, len(result.steps))
            return False
        return True

    def _run_commands(self, commands: list[Command]) -> None:
        for command in commands:
            self._execute(command)

    def _execute(self, command: Command) -> None:
        handler = self._handlers.get(command.name)
        if handler is None:  # only happens if parser and handlers diverge
            raise FlowError(f"Command without implementation: {command.name}")
        try:
            handler(command)
        except (FlowError, AndroidError) as exc:
            if command.optional:
                self.echo(f"       (optional) {command.name} ignored: {exc}")
                return
            raise

    def _capture_failure(self, flow_path: Path, index: int) -> None:
        """Save a screenshot and the screen hierarchy at the moment of failure."""
        base = f"{flow_path.stem}-failure-{index:02d}"
        png = self.artifacts_dir / f"{base}.png"
        xml = self.artifacts_dir / f"{base}.xml"
        try:
            self.driver.screenshot(png)
            root = self.driver.dump_hierarchy()
            xml.parent.mkdir(parents=True, exist_ok=True)
            ET.ElementTree(root).write(xml, encoding="utf-8", xml_declaration=True)
            if self._current_result is not None:
                self._current_result.artifacts.extend([png, xml])
        except (AndroidError, OSError) as exc:
            self.echo(f"       Could not save failure artifacts: {exc}")
            return
        self.echo(f"       Failure artifacts: {png}, {xml}")

    # ------------------------------------------------------------------
    # Reading the screen
    # ------------------------------------------------------------------

    def _elements(self) -> list[UiElement]:
        return parse_elements(self.driver.dump_hierarchy())

    def _find_now(self, selector: Selector) -> UiElement | None:
        return find_element(self._elements(), selector)

    def _poll(
        self, selector: Selector, timeout: float, *, present: bool
    ) -> tuple[bool, UiElement | None]:
        """Poll the screen until the element is present/absent or the timeout."""
        deadline = time.monotonic() + timeout
        while True:
            error: AndroidError | None = None
            element: UiElement | None = None
            try:
                element = find_element(self._elements(), selector)
            except AndroidError as exc:
                error = exc
            else:
                if (element is not None) == present:
                    return True, element
            if time.monotonic() >= deadline:
                if error is not None:
                    raise FlowError(f"Failed to read the screen: {error}") from error
                return False, element
            time.sleep(self.poll_interval)

    def _find(self, selector: Selector, timeout: float) -> UiElement | None:
        found, element = self._poll(selector, timeout, present=True)
        return element if found else None

    # ------------------------------------------------------------------
    # Points and gestures
    # ------------------------------------------------------------------

    def _screen(self) -> tuple[int, int]:
        size = getattr(self.driver, "screen_size", None)
        return size() if callable(size) else (1080, 2400)

    def _resolve_point(self, spec: str) -> tuple[int, int]:
        w, h = self._screen()
        coords = []
        for raw, total in zip(spec.split(","), (w, h), strict=True):
            raw = raw.strip()
            if raw.endswith("%"):
                coords.append(int(total * float(raw[:-1]) / 100.0))
            else:
                coords.append(int(float(raw)))
        return coords[0], coords[1]

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _target_app(self, command: Command) -> str:
        app = command.spec.get("appId") or self._app_id
        if not app:
            raise FlowError(
                f"{command.name}: provide the appId (or declare it in the header)."
            )
        return str(app)

    def _launch_app(self, command: Command) -> None:
        spec = command.spec
        package = self._target_app(command)
        self._app_id = package
        self._launch_count += 1
        if spec["clearState"]:
            self.driver.clear_app(package)
        if spec["stopApp"]:
            self.driver.stop_app(package)
        self._apply_permissions(package, spec["permissions"])
        args = spec.get("arguments")
        if args:
            self.driver.launch_app(package, arguments=args)
        else:
            self.driver.launch_app(package)

    def _apply_permissions(self, package: str, permissions: dict[str, str]) -> None:
        # `all` first; later specific rules may override it.
        ordered = sorted(permissions.items(), key=lambda kv: kv[0] != "all")
        requested: list[str] | None = None
        for group, setting in ordered:
            if setting == "unset":
                continue
            if group == "all":
                if requested is None:
                    try:
                        requested = self.driver.requested_permissions(package)
                    except AndroidError:
                        requested = []
                names = tuple(requested)
            else:
                names = permission_names(group)
            for name in names:
                try:
                    if setting == "allow":
                        self.driver.grant_permission(package, name)
                    else:
                        self.driver.revoke_permission(package, name)
                except AndroidError:
                    # Non-runtime permissions (or ones the app does not declare)
                    # cannot be changed; Maestro ignores them too.
                    continue

    def _stop_app(self, command: Command) -> None:
        self.driver.stop_app(self._target_app(command))

    def _clear_state(self, command: Command) -> None:
        self.driver.clear_app(self._target_app(command))

    def _set_permissions(self, command: Command) -> None:
        self._apply_permissions(self._target_app(command), command.spec["permissions"])

    def _add_media(self, command: Command) -> None:
        for raw in command.spec["files"]:
            path = Path(raw)
            if not path.is_absolute():
                path = self._base_dir / path
            if not path.is_file():
                raise FlowError(f"addMedia: file not found: {path}")
            self.driver.add_media(path)

    def _assert_dark_mode(self, _command: Command) -> None:
        if not self.driver.is_dark_mode():
            raise FlowError("Expected device to be in dark mode.")

    def _assert_light_mode(self, _command: Command) -> None:
        if self.driver.is_dark_mode():
            raise FlowError("Expected device to be in light mode.")

    def _clear_keychain(self, _command: Command) -> None:
        self.echo("       clearKeychain is iOS-specific; ignored on Android.")

    def _tap_on(self, command: Command) -> None:
        spec = command.spec
        sel: Selector = spec["selector"]
        if sel.point is not None:
            point = self._resolve_point(sel.point)
        else:
            element = self._find(sel, self.default_timeout)
            if element is None:
                raise FlowError(f"Element not found: {sel.describe()}")
            point = element.center
        before = self._signature() if spec["retry"] else None
        for attempt in range(spec["repeat"]):
            if attempt:
                time.sleep(spec["delay"])
            self._tap(point, spec)
        if before is not None:
            time.sleep(0.5)
            if self._signature() == before:
                self._tap(point, spec)

    def _tap(self, point: tuple[int, int], spec: dict[str, Any]) -> None:
        if spec["double"]:
            self.driver.double_tap(*point)
        elif spec["longPress"]:
            self.driver.long_press(*point)
        else:
            self.driver.tap(*point)

    def _signature(self) -> list[tuple[Any, ...]] | None:
        try:
            return [
                (e.text, e.content_desc, e.resource_id, e.bounds, e.checked, e.selected)
                for e in self._elements()
            ]
        except AndroidError:
            return None

    def _input_text(self, command: Command) -> None:
        self._type(command.spec["text"])

    def _type(self, text: str) -> None:
        try:
            self.driver.type_text(text, transliterate=self.transliterate)
        except TypeError:
            self.driver.type_text(text)

    def _input_random(self, command: Command) -> None:
        length = command.spec["length"]
        rng = random.SystemRandom()
        if command.name == "inputRandomNumber":
            text = "".join(rng.choice(string.digits) for _ in range(length))
        elif command.name == "inputRandomEmail":
            user = "".join(rng.choice(string.ascii_lowercase) for _ in range(length))
            text = f"{user}@example.com"
        elif command.name == "inputRandomPersonName":
            first = rng.choice(["Ana", "Bruno", "Carla", "Diego", "Elisa", "Felipe"])
            last = rng.choice(["Silva", "Souza", "Costa", "Lima", "Alves", "Rocha"])
            text = f"{first} {last}"
        else:
            text = "".join(rng.choice(string.ascii_letters) for _ in range(length))
        self._type(text)

    def _erase_text(self, command: Command) -> None:
        self.driver.erase_text(command.spec["chars"])

    def _press_key(self, command: Command) -> None:
        self.driver.press_key(command.spec["key"])

    def _wait_animation(self, command: Command) -> None:
        """Wait for the screen to stop changing (two identical reads in a row)."""
        deadline = time.monotonic() + command.spec["timeout"]
        previous: list[tuple[Any, ...]] | None = None
        while time.monotonic() < deadline:
            current = self._signature()
            if current is not None and current == previous:
                return
            previous = current
            time.sleep(0.2)

    def _swipe(self, command: Command) -> None:
        spec = command.spec
        duration_ms = int(spec["duration"] * 1000)
        if "direction" in spec:
            w, h = self._screen()
            direction = spec["direction"]
            if spec["from"] is not None:
                element = self._find(spec["from"], self.default_timeout)
                if element is None:
                    raise FlowError(
                        f"swipe: element not found: {spec['from'].describe()}"
                    )
                sx, sy = element.center
            else:
                sx, sy = w // 2, h // 2
            ends = {
                "UP": (sx, int(h * 0.1)),
                "DOWN": (sx, int(h * 0.9)),
                "LEFT": (int(w * 0.1), sy),
                "RIGHT": (int(w * 0.9), sy),
            }
            if spec["from"] is None and direction in ("LEFT", "RIGHT"):
                sx = int(w * (0.9 if direction == "LEFT" else 0.1))
            ex, ey = ends[direction]
            self.driver.swipe(sx, sy, ex, ey, duration_ms)
            return
        sx, sy = self._resolve_point(spec["start"])
        ex, ey = self._resolve_point(spec["end"])
        self.driver.swipe(sx, sy, ex, ey, duration_ms)

    def _center_element(self, element: UiElement) -> None:
        w, h = self._screen()
        target_y = h // 2
        delta = element.center[1] - target_y
        if abs(delta) <= max(10, int(h * 0.05)):
            return
        max_half = int(h * 0.4)
        half = max(-max_half, min(max_half, delta // 2))
        if half != 0:
            self.driver.swipe(w // 2, target_y + half, w // 2, target_y - half, 300)

    def _scroll_until_visible(self, command: Command) -> None:
        spec = command.spec
        sel: Selector = spec["selector"]
        deadline = time.monotonic() + spec["timeout"]
        while True:
            try:
                element = self._find_now(sel)
                if element is not None:
                    if spec.get("centerElement"):
                        self._center_element(element)
                    return
            except AndroidError:
                pass  # unstable screen while scrolling: try again
            if time.monotonic() >= deadline:
                break
            self.driver.scroll(spec["direction"])
            time.sleep(self.poll_interval)
        raise FlowError(
            f"scrollUntilVisible: element not found after scrolling "
            f"{spec['direction']}: {sel.describe()}"
        )

    def _open_link(self, command: Command) -> None:
        if command.spec.get("browser"):
            self.driver.open_link(command.spec["link"], browser=True)
        else:
            self.driver.open_link(command.spec["link"])

    def _assert_visible(self, command: Command) -> None:
        sel: Selector = command.spec["selector"]
        if self._find(sel, self.default_timeout) is None:
            raise FlowError(f"Element is not visible: {sel.describe()}")

    def _assert_not_visible(self, command: Command) -> None:
        sel: Selector = command.spec["selector"]
        gone, _ = self._poll(sel, self.default_timeout, present=False)
        if not gone:
            raise FlowError(f"Element is still visible: {sel.describe()}")

    def _extended_wait_until(self, command: Command) -> None:
        spec = command.spec
        sel: Selector = spec["selector"]
        timeout = spec["timeout"] if spec["timeout"] is not None else 20.0
        present = spec["mode"] == "visible"
        done, _ = self._poll(sel, timeout, present=present)
        if not done:
            state = "appear" if present else "disappear"
            raise FlowError(f"Timeout waiting for element to {state}: {sel.describe()}")

    def _artifact_path(self, raw_path: str, extension: str) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.artifacts_dir / path
        if path.suffix.lower() != extension.lower():
            path = path.with_name(path.name + extension)
        return path

    def _warn_if_screenrecord_truncated(self, elapsed: float) -> None:
        if elapsed > SCREENRECORD_MAX_SECONDS:
            self.echo(
                f"       Warning: the recording lasted {elapsed:.0f}s and "
                f"Android's screenrecord limits recording to "
                f"{SCREENRECORD_MAX_SECONDS}s; "
                f"the video only covers the beginning."
            )

    @staticmethod
    def _record_video_artifact(result: FlowResult, path: Path) -> None:
        if result.video_path is None:
            result.video_path = path
        if path not in result.artifacts:
            result.artifacts.append(path)

    def _take_screenshot(self, command: Command) -> None:
        path = self._artifact_path(command.spec["path"], ".png")
        crop_sel: Selector | None = command.spec.get("cropOn")
        crop_element: UiElement | None = None
        if crop_sel is not None:
            crop_element = self._find(crop_sel, self.default_timeout)
            if crop_element is None:
                raise FlowError(
                    f"takeScreenshot: cropOn element not found: {crop_sel.describe()}"
                )
        self.driver.screenshot(path)
        if crop_element is not None and path.is_file():
            path.write_bytes(crop_png(path.read_bytes(), crop_element.bounds))
        if (
            self._current_result is not None
            and path not in self._current_result.artifacts
        ):
            self._current_result.artifacts.append(path)
        self.echo(f"       Screenshot: {path}")

    def _start_recording(self, command: Command) -> None:
        if self._active_recording is not None:
            raise FlowError(
                "startRecording: a recording is already in progress. "
                "Call stopRecording first."
            )
        if not hasattr(self.driver, "start_recording"):
            raise FlowError("startRecording: driver does not support recording.")
        dest = self._artifact_path(command.spec["path"], ".mp4")
        remote_path = f"/sdcard/pyestro_record_{uuid.uuid4().hex[:8]}.mp4"
        try:
            try:
                proc = self.driver.start_recording(remote_path=remote_path)
            except TypeError:
                proc = self.driver.start_recording()
        except (AndroidError, OSError) as exc:
            raise FlowError(f"startRecording failed: {exc}") from exc
        self._active_recording = (proc, dest, remote_path, time.monotonic())
        self.echo(f"       Recording started: {dest}")

    def _stop_recording(self, _command: Command | None = None) -> None:
        if self._active_recording is None:
            self.echo("       No active recording to stop.")
            return
        proc, dest, remote_path, started_at = self._active_recording
        self._active_recording = None
        self._warn_if_screenrecord_truncated(time.monotonic() - started_at)
        try:
            saved = self.driver.stop_recording(proc, dest, remote_path=remote_path)
        except TypeError:
            saved = self.driver.stop_recording(proc, dest)
        if saved:
            if self._current_result is not None:
                self._record_video_artifact(self._current_result, saved)
            self.echo(f"       Recording saved: {saved}")
        elif proc is not None:
            self.echo(f"       Warning: could not save recording to {dest}.")

    # ------------------------------------------------------------------
    # Control flow
    # ------------------------------------------------------------------

    def _condition_holds(self, cond: dict[str, Any]) -> bool:
        if "platform" in cond and cond["platform"] != "android":
            return False
        if "true" in cond:
            value = cond["true"]
            if isinstance(value, str):
                if value.strip().lower() in _FALSY:
                    return False
            elif not value:
                return False
        if "visible" in cond:
            sel = cond["visible"]
            # Short wait: the screen may be finishing a transition.
            if self._find(sel, min(self.default_timeout, 2.0)) is None:
                return False
        if "notVisible" in cond:
            try:
                if self._find_now(cond["notVisible"]) is not None:
                    return False
            except (AndroidError, SelectorError):
                return False
        return True

    def _repeat(self, command: Command) -> None:
        spec = command.spec
        if "times" in spec:
            for _ in range(spec["times"]):
                self._run_commands(command.nested)
            return
        while self._condition_holds(spec["while"]):
            self._run_commands(command.nested)

    def _retry(self, command: Command) -> None:
        spec = command.spec
        last: FlowError | None = None
        for attempt in range(spec["maxRetries"] + 1):
            try:
                if "file" in spec:
                    self._run_file(spec["file"], spec["env"])
                else:
                    self._run_commands(command.nested)
                return
            except (FlowError, AndroidError) as exc:
                last = exc if isinstance(exc, FlowError) else FlowError(str(exc))
                if attempt < spec["maxRetries"]:
                    self.echo(f"       retry {attempt + 1}/{spec['maxRetries']}: {exc}")
        assert last is not None
        raise last

    def _run_flow(self, command: Command) -> None:
        spec = command.spec
        if spec["when"] is not None and not self._condition_holds(spec["when"]):
            self.echo("       runFlow ignored: 'when' condition not met.")
            return
        if "file" in spec:
            self._run_file(spec["file"], spec["env"])
        else:
            self._run_commands(command.nested)

    def _run_file(self, file: str, extra_env: dict[str, str]) -> None:
        if self._flow_depth >= MAX_SUBFLOW_DEPTH:
            raise FlowError(
                f"runFlow: nesting deeper than {MAX_SUBFLOW_DEPTH} levels "
                f"(possible cycle while running '{file}')."
            )
        path = Path(file)
        if not path.is_absolute():
            path = self._base_dir / path
        header, raw = read_flow_file(path)
        config = parse_config(header)
        env = {**config.env, **self._env, **extra_env}
        commands = compile_commands(raw, env, f"{path.name} › ")
        saved_env, saved_dir, saved_app = self._env, self._base_dir, self._app_id
        initial_app = saved_app or config.app_id
        self._env, self._base_dir = env, path.resolve().parent
        self._app_id = initial_app
        self._flow_depth += 1
        launches_before = self._launch_count
        try:
            self._run_commands(commands)
        finally:
            self._flow_depth -= 1
            self._env, self._base_dir = saved_env, saved_dir
            # Keep the appId when a launchApp ran inside the subflow.
            if self._launch_count == launches_before:
                self._app_id = saved_app
