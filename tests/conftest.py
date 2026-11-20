from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def node(
    text: str = "",
    *,
    rid: str = "",
    desc: str = "",
    bounds: str = "[0,0][100,100]",
    children: str = "",
    **attrs: str,
) -> str:
    extra = "".join(f' {k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return (
        f'<node text="{text}" resource-id="{rid}" content-desc="{desc}" '
        f'bounds="{bounds}"{extra}>{children}</node>'
    )


def hierarchy(*nodes: str) -> ET.Element:
    return ET.fromstring(f"<hierarchy>{''.join(nodes)}</hierarchy>")


class FakeDriver:
    """Fake driver: returns predefined screens and records actions."""

    def __init__(self, *screens: ET.Element) -> None:
        self.screens = list(screens) or [hierarchy()]
        self.calls: list[tuple] = []

    def dump_hierarchy(self) -> ET.Element:
        if len(self.screens) > 1:
            return self.screens.pop(0)
        return self.screens[0]

    def screen_size(self) -> tuple[int, int]:
        return (1000, 2000)

    def screenshot(self, path: Path) -> Path:
        self.calls.append(("screenshot", Path(path).name))
        return Path(path)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append((name, *args))
            if name == "requested_permissions":
                return ["android.permission.CAMERA"]

        return record


@pytest.fixture
def write_flow(tmp_path: Path):
    def _write(text: str, name: str = "flow.yaml") -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    return _write
