from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from pyestro.android import (
    _PNG_SIGNATURE,
    AndroidDriver,
    AndroidError,
    _png_chunk,
    crop_png,
)
from pyestro.flow import FlowError, compile_commands

from .conftest import FakeDriver, hierarchy, node
from .test_runner import calls, runner


def _make_rgba_png(width: int, height: int) -> bytes:
    """Create a valid 8-bit RGBA PNG where pixel (x, y) encodes (x, y, 128, 255)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend((x & 0xFF, y & 0xFF, 128, 255))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"".join(
        [
            _PNG_SIGNATURE,
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", zlib.compress(bytes(raw))),
            _png_chunk(b"IEND", b""),
        ]
    )


def _decode_rgba_png(data: bytes) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    assert data.startswith(_PNG_SIGNATURE)
    pos = len(_PNG_SIGNATURE)
    width = height = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        cdata = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height = struct.unpack(">II", cdata[:8])
        elif ctype == b"IDAT":
            idat.extend(cdata)
    raw = zlib.decompress(bytes(idat))
    pixels: list[tuple[int, int, int, int]] = []
    stride = 1 + width * 4
    for y in range(height):
        row = raw[y * stride + 1 : (y + 1) * stride]
        for x in range(width):
            off = x * 4
            pixels.append((row[off], row[off + 1], row[off + 2], row[off + 3]))
    return width, height, pixels


def test_launch_app_arguments_validation():
    cmds = compile_commands(
        [
            {
                "launchApp": {
                    "appId": "com.app",
                    "arguments": {"user": "ana", "count": 3, "debug": True, "pi": 1.5},
                }
            }
        ],
        {},
    )
    assert cmds[0].spec["arguments"] == {
        "user": "ana",
        "count": 3,
        "debug": True,
        "pi": 1.5,
    }
    with pytest.raises(FlowError, match="arguments must be a key-value map"):
        compile_commands([{"launchApp": {"arguments": ["a"]}}], {})
    with pytest.raises(FlowError, match="must be text, number, or boolean"):
        compile_commands([{"launchApp": {"arguments": {"nested": [1]}}}], {})


def test_android_launch_app_with_arguments(monkeypatch: pytest.MonkeyPatch):
    driver = AndroidDriver()
    calls_log: list[tuple[str, ...]] = []

    def fake_shell(*args, timeout=30):
        calls_log.append(args)
        if args[0] == "cmd":
            return "priority=0 preferredOrder=0\ncom.app/.MainActivity\n"
        return ""

    monkeypatch.setattr(driver, "shell", fake_shell)
    driver.launch_app(
        "com.app", arguments={"name": "Ana", "age": 20, "vip": True, "score": 9.5}
    )
    assert calls_log[1] == (
        "am",
        "start",
        "-n",
        "com.app/.MainActivity",
        "--es",
        "name",
        "Ana",
        "--ei",
        "age",
        "20",
        "--ez",
        "vip",
        "true",
        "--ef",
        "score",
        "9.5",
    )


def test_android_open_link_with_browser_true(monkeypatch: pytest.MonkeyPatch):
    driver = AndroidDriver()
    calls_log: list[tuple[str, ...]] = []

    def fake_shell(*args, timeout=30):
        calls_log.append(args)
        if args[0] == "cmd":
            return "com.android.chrome/com.google.android.apps.chrome.Main\n"
        return ""

    monkeypatch.setattr(driver, "shell", fake_shell)
    driver.open_link("https://example.com", browser=True)
    assert calls_log[1] == (
        "am",
        "start",
        "-n",
        "com.android.chrome/com.google.android.apps.chrome.Main",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        "https://example.com",
    )


def test_scroll_until_visible_centers_element(write_flow, tmp_path):
    # Screen is 1000x2000 (center y=1000); element center y=1800 -> swipe up.
    driver = FakeDriver(hierarchy(node("Footer", bounds="[100,1750][900,1850]")))
    flow = write_flow(
        "appId: a.b\n---\n- scrollUntilVisible:\n    element: Footer\n"
        "    centerElement: true\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    swipes = calls(driver, "swipe")
    assert len(swipes) == 1
    _, sx, sy, ex, ey, _ = swipes[0]
    assert sx == ex == 500
    assert sy > ey


def test_crop_png_and_take_screenshot_crop_on(write_flow, tmp_path):
    full_png = _make_rgba_png(20, 20)
    cropped = crop_png(full_png, (5, 4, 10, 7))
    w, h, pixels = _decode_rgba_png(cropped)
    assert (w, h) == (5, 3)
    assert pixels[0] == (5, 4, 128, 255)
    assert pixels[-1] == (9, 6, 128, 255)

    class PngDriver(FakeDriver):
        def screenshot(self, path: Path) -> Path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(full_png)
            self.calls.append(("screenshot", p.name))
            return p

    driver = PngDriver(hierarchy(node("Card", bounds="[2,3][8,9]")))
    flow = write_flow(
        "appId: a.b\n---\n- takeScreenshot:\n    path: card\n    cropOn: Card\n"
    )
    result = runner(driver, tmp_path).run(flow)
    assert result.ok
    cw, ch, _ = _decode_rgba_png((tmp_path / "artifacts" / "card.png").read_bytes())
    assert (cw, ch) == (6, 6)


def test_optional_command_ignores_android_error(write_flow, tmp_path):
    class FailingDriver(FakeDriver):
        def tap(self, *args):
            raise AndroidError("adb died")

    flow = write_flow(
        "appId: a.b\n---\n- tapOn:\n    point: 10%,10%\n    optional: true\n- back\n"
    )
    result = runner(FailingDriver(), tmp_path).run(flow)
    assert result.ok


def test_runflow_cycle_fails_with_clear_message(write_flow, tmp_path):
    flow = write_flow("appId: a.b\n---\n- runFlow: flow.yaml\n")
    result = runner(FakeDriver(), tmp_path).run(flow)
    assert not result.ok
    error = result.steps[-1].error or ""
    assert "nesting deeper" in error


def test_launch_app_in_subflow_keeps_app_id(write_flow, tmp_path):
    write_flow("appId: sub.app\n---\n- launchApp: sub.app\n", name="sub.yaml")
    flow = write_flow("---\n- runFlow: sub.yaml\n- stopApp\n")
    driver = FakeDriver()
    result = runner(driver, tmp_path).run(Path(flow))
    assert result.ok
    assert ("stop_app", "sub.app") in driver.calls
