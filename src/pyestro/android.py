from __future__ import annotations

import contextlib
import re
import shlex
import signal
import struct
import subprocess
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Generator, Sequence
from pathlib import Path


class AndroidError(RuntimeError):
    """Raised when an ADB operation fails."""


_HIERARCHY_REMOTE_TEMPLATE = "/sdcard/pyestro-window-{token}.xml"
_RECORD_REMOTE_TEMPLATE = "/sdcard/pyestro_record_{token}.mp4"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Limit imposed by Android's `screenrecord`: the recording stops by itself.
SCREENRECORD_MAX_SECONDS = 180

# Key names accepted by Maestro's `pressKey` (normalized: lowercase, and
# spaces/hyphens become "_"). E.g.: "Volume Up" -> "volume_up".
KEY_NAMES: dict[str, str] = {
    "enter": "KEYCODE_ENTER",
    "backspace": "KEYCODE_DEL",
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "lock": "KEYCODE_SLEEP",
    "power": "KEYCODE_POWER",
    "tab": "KEYCODE_TAB",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "remote_dpad_up": "KEYCODE_DPAD_UP",
    "remote_dpad_down": "KEYCODE_DPAD_DOWN",
    "remote_dpad_left": "KEYCODE_DPAD_LEFT",
    "remote_dpad_right": "KEYCODE_DPAD_RIGHT",
    "remote_dpad_center": "KEYCODE_DPAD_CENTER",
    "remote_media_play_pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "remote_media_stop": "KEYCODE_MEDIA_STOP",
    "remote_media_next": "KEYCODE_MEDIA_NEXT",
    "remote_media_previous": "KEYCODE_MEDIA_PREVIOUS",
    "remote_media_rewind": "KEYCODE_MEDIA_REWIND",
    "remote_media_fast_forward": "KEYCODE_MEDIA_FAST_FORWARD",
    "remote_system_navigation_up": "KEYCODE_SYSTEM_NAVIGATION_UP",
    "remote_system_navigation_down": "KEYCODE_SYSTEM_NAVIGATION_DOWN",
    "remote_button_a": "KEYCODE_BUTTON_A",
    "remote_button_b": "KEYCODE_BUTTON_B",
    "remote_menu": "KEYCODE_MENU",
    "tv_input": "KEYCODE_TV_INPUT",
    "tv_input_hdmi_1": "KEYCODE_TV_INPUT_HDMI_1",
    "tv_input_hdmi_2": "KEYCODE_TV_INPUT_HDMI_2",
    "tv_input_hdmi_3": "KEYCODE_TV_INPUT_HDMI_3",
}


def normalize_key(key: str) -> str:
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


# Maestro permission groups (`permissions: {location: allow}`).
PERMISSION_GROUPS: dict[str, tuple[str, ...]] = {
    "location": (
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.ACCESS_COARSE_LOCATION",
    ),
    "camera": ("android.permission.CAMERA",),
    "contacts": (
        "android.permission.READ_CONTACTS",
        "android.permission.WRITE_CONTACTS",
    ),
    "phone": (
        "android.permission.READ_PHONE_STATE",
        "android.permission.CALL_PHONE",
    ),
    "microphone": ("android.permission.RECORD_AUDIO",),
    "bluetooth": (
        "android.permission.BLUETOOTH_CONNECT",
        "android.permission.BLUETOOTH_SCAN",
    ),
    "storage": (
        "android.permission.READ_EXTERNAL_STORAGE",
        "android.permission.WRITE_EXTERNAL_STORAGE",
    ),
    "notifications": ("android.permission.POST_NOTIFICATIONS",),
    "medialibrary": (
        "android.permission.READ_MEDIA_IMAGES",
        "android.permission.READ_MEDIA_VIDEO",
        "android.permission.READ_MEDIA_AUDIO",
    ),
    "calendar": (
        "android.permission.READ_CALENDAR",
        "android.permission.WRITE_CALENDAR",
    ),
    "sms": ("android.permission.READ_SMS", "android.permission.SEND_SMS"),
}


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def crop_png(data: bytes, bounds: tuple[int, int, int, int]) -> bytes:
    """Crop an 8-bit non-interlaced PNG to ``(x1, y1, x2, y2)`` using stdlib."""
    if not data.startswith(_PNG_SIGNATURE):
        raise AndroidError("Cannot crop screenshot: data is not a valid PNG.")
    pos = len(_PNG_SIGNATURE)
    ihdr: tuple[int, int, int, int, int, int, int] | None = None
    idat_parts: list[bytes] = []
    extra_chunks: list[tuple[bytes, bytes]] = []
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        cdata = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", cdata)
        elif ctype == b"IDAT":
            idat_parts.append(cdata)
        elif ctype in (b"PLTE", b"tRNS"):
            extra_chunks.append((ctype, cdata))
        elif ctype == b"IEND":
            break
    if ihdr is None or not idat_parts:
        raise AndroidError("Cannot crop screenshot: missing IHDR or IDAT chunk.")
    width, height, bit_depth, color_type, comp, flt, interlace = ihdr
    samples = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if bit_depth != 8 or comp != 0 or flt != 0 or interlace != 0 or samples is None:
        raise AndroidError(
            f"Cannot crop PNG with bit_depth={bit_depth}, "
            f"color_type={color_type}, interlace={interlace}."
        )
    x1, y1, x2, y2 = bounds
    x1 = max(0, min(int(x1), width - 1))
    y1 = max(0, min(int(y1), height - 1))
    x2 = max(x1 + 1, min(int(x2), width))
    y2 = max(y1 + 1, min(int(y2), height))
    new_w, new_h = x2 - x1, y2 - y1

    raw = zlib.decompress(b"".join(idat_parts))
    stride = width * samples
    prev = bytearray(stride)
    out = bytearray()
    row_start = 0
    for y in range(y2):
        ftype = raw[row_start]
        src = raw[row_start + 1 : row_start + 1 + stride]
        row_start += 1 + stride
        curr = bytearray(src)
        if ftype == 1:  # Sub
            for i in range(samples, stride):
                curr[i] = (curr[i] + curr[i - samples]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                curr[i] = (curr[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = curr[i - samples] if i >= samples else 0
                curr[i] = (curr[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = curr[i - samples] if i >= samples else 0
                c = prev[i - samples] if i >= samples else 0
                curr[i] = (curr[i] + _paeth(a, prev[i], c)) & 0xFF
        elif ftype != 0:
            raise AndroidError(f"Unsupported PNG scanline filter: {ftype}")
        if y >= y1:
            out.append(0)
            out.extend(curr[x1 * samples : x2 * samples])
        prev = curr

    new_ihdr = struct.pack(">IIBBBBB", new_w, new_h, 8, color_type, 0, 0, 0)
    parts = [_PNG_SIGNATURE, _png_chunk(b"IHDR", new_ihdr)]
    for ctype, cdata in extra_chunks:
        parts.append(_png_chunk(ctype, cdata))
    parts.append(_png_chunk(b"IDAT", zlib.compress(bytes(out))))
    parts.append(_png_chunk(b"IEND", b""))
    return b"".join(parts)


class AndroidDriver:
    def __init__(self, serial: str | None = None, adb_path: str = "adb") -> None:
        self.serial = serial
        self.adb_path = adb_path
        self._cached_screen_size: tuple[int, int] | None = None
        # Unique token per instance: concurrent runs on the same device do not
        # overwrite each other's temporary files.
        token = uuid.uuid4().hex[:8]
        self._hierarchy_remote = _HIERARCHY_REMOTE_TEMPLATE.format(token=token)
        self._record_remote = _RECORD_REMOTE_TEMPLATE.format(token=token)

    def _base_cmd(self) -> list[str]:
        return [self.adb_path, "-s", self.serial] if self.serial else [self.adb_path]

    def _run(self, args: Sequence[str], *, timeout: float = 30) -> bytes:
        """Run adb and return the raw stdout (bytes)."""
        cmd = self._base_cmd() + list(args)
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=timeout, check=False
            )
        except FileNotFoundError as exc:
            raise AndroidError(
                "ADB was not found. Install Android SDK Platform Tools and "
                "add the adb executable to PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AndroidError(f"ADB command timed out: {shlex.join(cmd)}") from exc
        if result.returncode != 0:
            detail = _decode(result.stderr or result.stdout)
            raise AndroidError(f"ADB failed ({result.returncode}): {detail}")
        return result.stdout

    def _command(self, args: Sequence[str], *, timeout: float = 30) -> str:
        return _decode(self._run(args, timeout=timeout))

    def devices(self) -> list[tuple[str, str]]:
        output = self._command(["devices"])
        rows: list[tuple[str, str]] = []
        for line in output.splitlines()[1:]:
            line = line.strip()
            if not line or "\t" not in line:
                continue
            serial, state = line.split("\t", 1)
            rows.append((serial, state.strip()))
        return rows

    def shell(self, *args: str, timeout: float = 30) -> str:
        return self._command(["shell", *args], timeout=timeout)

    @staticmethod
    def _check_app_id(app_id: str) -> None:
        if not app_id or "/" in app_id or " " in app_id:
            raise ValueError(
                "appId must be the Android package name, e.g. com.example.app"
            )

    def _resolve_component(self, *args: str, error_context: str) -> str:
        output = self.shell(
            "cmd", "package", "resolve-activity", "--brief", *args, timeout=15
        )
        for line in reversed(output.splitlines()):
            candidate = line.strip()
            if "/" in candidate and " " not in candidate:
                return candidate
        raise AndroidError(f"Could not resolve activity ({error_context}): {output}")

    def launch_app(
        self,
        app_id: str,
        arguments: dict[str, str | int | float | bool] | None = None,
    ) -> None:
        self._check_app_id(app_id)
        if arguments:
            component = self._resolve_component(
                "-c",
                "android.intent.category.LAUNCHER",
                app_id,
                error_context=app_id,
            )
            extra_args: list[str] = []
            for key, val in arguments.items():
                qkey = shlex.quote(str(key))
                if isinstance(val, bool):
                    extra_args.extend(["--ez", qkey, "true" if val else "false"])
                elif isinstance(val, int):
                    extra_args.extend(["--ei", qkey, str(val)])
                elif isinstance(val, float):
                    extra_args.extend(["--ef", qkey, str(val)])
                else:
                    extra_args.extend(["--es", qkey, shlex.quote(str(val))])
            self.shell("am", "start", "-n", component, *extra_args, timeout=30)
            return
        self.shell(
            "monkey",
            "-p",
            app_id,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=30,
        )

    def stop_app(self, app_id: str) -> None:
        self._check_app_id(app_id)
        self.shell("am", "force-stop", app_id)

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(int(x)), str(int(y)))

    def double_tap(self, x: int, y: int) -> None:
        pt = f"{int(x)} {int(y)}"
        self.shell(f"input tap {pt}; input tap {pt}")

    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        self.swipe(x, y, x, y, duration_ms)

    def clear_app_data(self, app_id: str) -> None:
        self.clear_app(app_id)

    def requested_permissions(self, app_id: str) -> list[str]:
        """Permissions declared by the app (via `dumpsys package`)."""
        self._check_app_id(app_id)
        output = self.shell("dumpsys", "package", app_id, timeout=30)
        perms: list[str] = []
        in_block = False
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("requested permissions:"):
                in_block = True
                continue
            if in_block:
                if stripped.endswith(":") or not stripped:
                    in_block = False
                    continue
                name = stripped.split(":", 1)[0]
                if name and name not in perms:
                    perms.append(name)
        return perms

    def type_text(self, value: str, *, transliterate: bool = False) -> None:
        if not value.isascii():
            if transliterate:
                normalized = (
                    unicodedata.normalize("NFKD", value)
                    .encode("ascii", "ignore")
                    .decode("ascii")
                )
                if not normalized:
                    raise AndroidError(
                        f"The text {value!r} contained only emojis or characters "
                        "with no ASCII representation compatible with ADB."
                    )
                value = normalized
            else:
                raise AndroidError(
                    f"`adb shell input text` only supports ASCII characters "
                    f"({value!r}); "
                    "accents and emojis are not accepted by native ADB. "
                    "Use '--transliterate' to normalize automatically "
                    "(e.g. 'Cartão' -> 'Cartao')."
                )
        # `input text` converts "%s" into a space. A literal "%s" is split into
        # separate calls ("...%" and "s...") so it does not become a space.
        chunks = value.split("%s")
        pieces = [chunks[0]]
        for chunk in chunks[1:]:
            pieces[-1] += "%"
            pieces.append("s" + chunk)
        for piece in pieces:
            if piece:
                # adb forwards the line to the device's `sh`: quoting prevents
                # &, ;, |, $, quotes, etc. from being interpreted by the shell.
                self.shell("input", "text", shlex.quote(piece.replace(" ", "%s")))

    def back(self) -> None:
        self.shell("input", "keyevent", "KEYCODE_BACK")

    def is_keyboard_shown(self) -> bool | None:
        """Whether the soft keyboard is open; None if it cannot be determined."""
        try:
            output = self.shell("dumpsys", "input_method", timeout=15)
        except AndroidError:
            return None
        match = re.search(r"mInputShown=(true|false)", output)
        if not match:
            return None
        return match.group(1) == "true"

    def hide_keyboard(self) -> None:
        # Only skip Back when the keyboard is known to be closed; a Back with
        # the keyboard closed would navigate back or close the app.
        if self.is_keyboard_shown() is False:
            return
        self.back()

    def clear_app(self, app_id: str) -> None:
        self._check_app_id(app_id)
        self.shell("pm", "clear", app_id)

    def press_key(self, key: str | int) -> None:
        raw = str(key).strip()
        code = KEY_NAMES.get(normalize_key(raw))
        if not code:
            if raw.isdigit() or raw.startswith("KEYCODE_"):
                code = raw
            else:
                code = f"KEYCODE_{raw.upper()}"
        self.shell("input", "keyevent", code)

    def erase_text(self, characters: int = 50) -> None:
        count = max(1, int(characters))
        chunks = [min(count - i, 50) for i in range(0, count, 50)]
        for chunk in chunks:
            self.shell("input", "keyevent", *["67"] * chunk)

    def screen_size(self) -> tuple[int, int]:
        if self._cached_screen_size is not None:
            return self._cached_screen_size
        output = self.shell("wm", "size")
        matches = re.findall(r"(\d+)x(\d+)", output)
        if matches:
            w, h = int(matches[-1][0]), int(matches[-1][1])
            self._cached_screen_size = (w, h)
            return (w, h)
        return (1080, 2400)

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 400,
    ) -> None:
        self.shell(
            "input",
            "swipe",
            str(int(start_x)),
            str(int(start_y)),
            str(int(end_x)),
            str(int(end_y)),
            str(int(duration_ms)),
        )

    def scroll(self, direction: str = "DOWN") -> None:
        w, h = self.screen_size()
        direction_upper = direction.upper()
        if direction_upper == "DOWN":
            self.swipe(w // 2, int(h * 0.75), w // 2, int(h * 0.25))
        elif direction_upper == "UP":
            self.swipe(w // 2, int(h * 0.25), w // 2, int(h * 0.75))
        elif direction_upper == "LEFT":
            self.swipe(int(w * 0.8), h // 2, int(w * 0.2), h // 2)
        elif direction_upper == "RIGHT":
            self.swipe(int(w * 0.2), h // 2, int(w * 0.8), h // 2)
        else:
            raise ValueError(f"Unknown scroll direction: {direction}")

    def open_link(self, url: str, *, browser: bool = False) -> None:
        if not url:
            raise ValueError("URL cannot be empty.")
        if browser:
            component = self._resolve_component(
                "-a",
                "android.intent.action.VIEW",
                "-c",
                "android.intent.category.BROWSABLE",
                "-d",
                "http://",
                error_context="default browser",
            )
            self.shell(
                "am",
                "start",
                "-n",
                component,
                "-a",
                "android.intent.action.VIEW",
                "-d",
                shlex.quote(str(url)),
            )
            return
        self.shell(
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            shlex.quote(str(url)),
        )

    def dump_hierarchy(self) -> ET.Element:
        # Remove the previous dump before generating a new one: if uiautomator
        # fails (e.g. "could not get idle state") but returns 0, `cat` fails
        # instead of reading a stale screen. All in a single ADB round-trip.
        remote = self._hierarchy_remote
        script = (
            f"rm -f {remote} && "
            f"uiautomator dump --compressed {remote} >&2 && "
            f"cat {remote}"
        )
        try:
            output = self.shell(script, timeout=30)
        except AndroidError as exc:
            if "could not get idle state" in str(exc):
                raise AndroidError(
                    f"{exc}\n"
                    "       Tip: the screen did not reach an idle state. "
                    "Check for continuous animations in the app "
                    "(e.g. ActivityIndicator, "
                    "infinite Reanimated/Keyframe loops, videos or loaders)."
                ) from exc
            raise
        # On older devices (without shell protocol v2) stderr comes mixed in.
        start = output.find("<?xml")
        if start < 0:
            start = output.find("<hierarchy")
        try:
            return ET.fromstring(output[max(start, 0) :])
        except ET.ParseError as exc:
            raise AndroidError(
                "Could not parse the screen's XML hierarchy. "
                "Check that the emulator is unlocked."
            ) from exc

    def screenshot(self, destination: str | Path) -> Path:
        path = Path(destination)
        data = self._run(["exec-out", "screencap", "-p"], timeout=30)
        if not data.startswith(_PNG_SIGNATURE):
            raise AndroidError(
                f"Invalid screenshot received from the device: {_decode(data[:200])}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def wait(self, seconds: float) -> None:
        time.sleep(max(0, seconds))

    _ANIMATION_SETTINGS = (
        "window_animation_scale",
        "transition_animation_scale",
        "animator_duration_scale",
    )

    def disable_animations(self) -> None:
        """Disable Android animations via ADB to speed up tests."""
        for setting in self._ANIMATION_SETTINGS:
            self.shell("settings", "put", "global", setting, "0")

    def enable_animations(self) -> None:
        """Restore Android animations to the default scale (1.0) via ADB."""
        for setting in self._ANIMATION_SETTINGS:
            self.shell("settings", "put", "global", setting, "1")

    def get_animation_scales(self) -> dict[str, str]:
        """Read the current animation scale values (\"null\" if unset)."""
        return {
            setting: self.shell("settings", "get", "global", setting).strip()
            for setting in self._ANIMATION_SETTINGS
        }

    def restore_animation_scales(self, saved: dict[str, str]) -> None:
        """Restore values obtained by `get_animation_scales`."""
        for setting, value in saved.items():
            if value in ("", "null"):
                self.shell("settings", "delete", "global", setting)
            else:
                self.shell("settings", "put", "global", setting, value)

    @staticmethod
    def _normalize_permission(permission: str) -> str:
        """Short names (CAMERA) become android.permission.CAMERA.

        Names that already contain a dot (android.permission.X or custom
        third-party permissions) are kept as they are.
        """
        perm = str(permission).strip()
        if not perm:
            raise ValueError("Permission name cannot be empty.")
        return perm if "." in perm else f"android.permission.{perm}"

    def grant_permission(self, app_id: str, permission: str) -> None:
        """Grant a runtime permission to the app via pm grant."""
        self._check_app_id(app_id)
        self.shell("pm", "grant", app_id, self._normalize_permission(permission))

    def revoke_permission(self, app_id: str, permission: str) -> None:
        """Revoke a runtime permission from the app via pm revoke."""
        self._check_app_id(app_id)
        self.shell("pm", "revoke", app_id, self._normalize_permission(permission))

    _ORIENTATIONS: dict[str, str] = {
        "PORTRAIT": "0",
        "LANDSCAPE_LEFT": "1",
        "UPSIDE_DOWN": "2",
        "LANDSCAPE_RIGHT": "3",
    }

    def set_orientation(self, orientation: str) -> None:
        """Lock auto-rotation and set the device rotation via settings."""
        code = self._ORIENTATIONS.get(orientation.upper())
        if code is None:
            raise ValueError(f"Unknown orientation: {orientation}")
        self.shell("settings", "put", "system", "accelerometer_rotation", "0")
        self.shell("settings", "put", "system", "user_rotation", code)
        self._cached_screen_size = None

    def set_airplane_mode(self, enabled: bool) -> None:
        """Enable or disable airplane mode via ADB."""
        mode = "enable" if enabled else "disable"
        try:
            self.shell("cmd", "connectivity", "airplane-mode", mode)
        except AndroidError:
            self.shell(
                "settings",
                "put",
                "global",
                "airplane_mode_on",
                "1" if enabled else "0",
            )
            self.shell(
                "am",
                "broadcast",
                "-a",
                "android.intent.action.AIRPLANE_MODE",
                "--ez",
                "state",
                "true" if enabled else "false",
            )

    def get_airplane_mode(self) -> bool:
        """Return True when airplane mode is enabled."""
        try:
            output = self.shell("cmd", "connectivity", "airplane-mode").strip().lower()
            if "enabled" in output:
                return True
            if "disabled" in output:
                return False
        except AndroidError:
            pass
        val = self.shell("settings", "get", "global", "airplane_mode_on").strip()
        return val == "1"

    def toggle_airplane_mode(self) -> bool:
        """Toggle airplane mode and return the new state."""
        target = not self.get_airplane_mode()
        self.set_airplane_mode(target)
        return target

    def add_media(self, local_path: Path) -> str:
        """Push an image or video to the device gallery and trigger MediaScanner."""
        path = Path(local_path)
        if not path.is_file():
            raise AndroidError(f"Media file not found: {path}")
        remote_dir = (
            "/sdcard/Movies" if path.suffix.lower() == ".mp4" else "/sdcard/Pictures"
        )
        remote = f"{remote_dir}/{path.name}"
        self._run(["push", str(path), remote], timeout=60)
        self.shell(
            "am",
            "broadcast",
            "-a",
            "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d",
            shlex.quote(f"file://{remote}"),
        )
        return remote

    def set_dark_mode(self, enabled: bool) -> None:
        """Enable or disable system dark mode via cmd uimode."""
        self.shell("cmd", "uimode", "night", "yes" if enabled else "no")

    def is_dark_mode(self) -> bool:
        """Return True when Android system dark mode is active."""
        output = self.shell("cmd", "uimode", "night").strip().lower()
        if "yes" in output:
            return True
        if "no" in output:
            return False
        dump = self.shell("dumpsys", "uimode").lower()
        if "mcomputednightmode=true" in dump or "night=yes" in dump:
            return True
        if "mcomputednightmode=false" in dump or "night=no" in dump:
            return False
        raise AndroidError(f"Could not determine dark mode state: {output}")

    def toggle_dark_mode(self) -> bool:
        """Toggle system dark mode and return the new state."""
        target = not self.is_dark_mode()
        self.set_dark_mode(target)
        return target

    def wake_up_and_unlock(self) -> None:
        """Wake the device screen and dismiss the lock if necessary."""
        self.shell("input", "keyevent", "KEYCODE_WAKEUP")
        self.shell("wm", "dismiss-keyguard")

    def dismiss_system_dialogs(self) -> None:
        """Close system dialogs (popups, system notices)."""
        self.shell(
            "am", "broadcast", "-a", "android.intent.action.CLOSE_SYSTEM_DIALOGS"
        )

    def start_recording(
        self, remote_path: str | None = None
    ) -> subprocess.Popen[bytes]:
        """Start recording the device screen via ADB screenrecord."""
        remote_path = remote_path or self._record_remote
        cmd = self._base_cmd() + ["shell", "screenrecord", remote_path]
        try:
            return subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError as exc:
            raise AndroidError(
                "ADB was not found. Install Android SDK Platform Tools and "
                "add the adb executable to PATH."
            ) from exc
        except OSError as exc:
            raise AndroidError(f"Could not start screen recording: {exc}") from exc

    def stop_recording(
        self,
        process: subprocess.Popen[bytes],
        destination: str | Path,
        remote_path: str | None = None,
    ) -> Path | None:
        """Stop the screen recording, pull the video and clean up the device."""
        remote_path = remote_path or self._record_remote
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=2)
            time.sleep(0.5)
            self._run(["pull", remote_path, str(path)], timeout=30)
            self.shell("rm", "-f", remote_path)
            return path if path.is_file() and path.stat().st_size > 0 else None
        except Exception:
            # Best effort: do not leave the video behind on the device.
            with contextlib.suppress(Exception):
                self.shell("rm", "-f", remote_path)
            return None

    @contextlib.contextmanager
    def record_screen(
        self,
        destination: str | Path,
        remote_path: str | None = None,
    ) -> Generator[Path, None, None]:
        """Context manager that records the screen during a code block."""
        dest = Path(destination)
        proc = self.start_recording(remote_path=remote_path)
        try:
            yield dest
        finally:
            self.stop_recording(proc, dest, remote_path=remote_path)
