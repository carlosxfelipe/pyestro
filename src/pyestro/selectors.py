"""Element selectors with Maestro semantics.

Followed rules (matching Maestro):

* ``text`` and ``id`` are **regular expressions** with full matching
  (``fullmatch``), case-insensitive. Text that is not a valid regex is matched
  literally.
* ``text`` matches the element's text, ``content-desc`` (accessibility label),
  or ``hint``.
* ``id`` matches the ``resource-id`` (with or without the ``package:id/``
  prefix).
* If multiple elements match, the deepest one in the tree wins; ``index``
  selects among candidates.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


class SelectorError(ValueError):
    """Invalid selector (unknown key, wrong value type, etc.)."""


@dataclass(frozen=True)
class UiElement:
    text: str
    hint: str
    resource_id: str
    content_desc: str
    class_name: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    enabled: bool
    checked: bool
    focused: bool
    selected: bool
    visible: bool
    index: int
    parent: int | None

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() == "true"


def parse_elements(root: ET.Element) -> list[UiElement]:
    """Flatten the uiautomator hierarchy while keeping each element's parent."""
    elements: list[UiElement] = []

    def walk(node: ET.Element, parent: int | None) -> None:
        match = _BOUNDS_RE.fullmatch(node.attrib.get("bounds", ""))
        current = parent
        if match:
            x1, y1, x2, y2 = (int(v) for v in match.groups())
            attr = node.attrib
            current = len(elements)
            elements.append(
                UiElement(
                    text=attr.get("text", ""),
                    hint=attr.get("hint", ""),
                    resource_id=attr.get("resource-id", ""),
                    content_desc=attr.get("content-desc", ""),
                    class_name=attr.get("class", ""),
                    bounds=(x1, y1, x2, y2),
                    clickable=_bool(attr.get("clickable")),
                    enabled=_bool(attr.get("enabled"), True),
                    checked=_bool(attr.get("checked")),
                    focused=_bool(attr.get("focused")),
                    selected=_bool(attr.get("selected")),
                    visible=_bool(attr.get("visible-to-user"), True)
                    and x2 > x1
                    and y2 > y1,
                    index=current,
                    parent=parent,
                )
            )
        for child in node:
            walk(child, current)

    walk(root, None)
    return elements


# ----------------------------------------------------------------------
# Selector
# ----------------------------------------------------------------------

# Recognized selector keys (all exist in Maestro syntax).
SELECTOR_KEYS = frozenset(
    {
        "text",
        "id",
        "index",
        "enabled",
        "checked",
        "focused",
        "selected",
        "point",
        "below",
        "above",
        "leftOf",
        "rightOf",
        "containsChild",
        "containsDescendants",
        "childOf",
        "traits",
    }
)
# Maestro keys that exist but do not apply to native Android.
_UNSUPPORTED_KEYS = {"css": "css is only available for web flows"}


@dataclass
class Selector:
    text: str | None = None
    id: str | None = None
    index: int | None = None
    enabled: bool | None = None
    checked: bool | None = None
    focused: bool | None = None
    selected: bool | None = None
    point: str | None = None
    below: Selector | None = None
    above: Selector | None = None
    left_of: Selector | None = None
    right_of: Selector | None = None
    contains_child: Selector | None = None
    contains_descendants: list[Selector] = field(default_factory=list)
    child_of: Selector | None = None

    def describe(self) -> str:
        parts = []
        for key in ("text", "id", "index", "point"):
            val = getattr(self, key)
            if val is not None:
                parts.append(f"{key}={val!r}")
        for key in ("enabled", "checked", "focused", "selected"):
            val = getattr(self, key)
            if val is not None:
                parts.append(f"{key}={val}")
        for key in ("below", "above", "left_of", "right_of", "child_of"):
            val = getattr(self, key)
            if val is not None:
                parts.append(f"{key}=({val.describe()})")
        return ", ".join(parts) or "<empty>"

    @property
    def is_point_only(self) -> bool:
        return self.point is not None


def _parse_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise SelectorError(f"'{key}' must be true or false.")


def _parse_index(value: Any) -> int:
    if isinstance(value, bool):
        raise SelectorError("'index' must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise SelectorError("'index' must be an integer.")


def _scalar_text(key: str, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SelectorError(f"'{key}' must be text.")
    return str(value)


_POINT_RE = re.compile(r"^\s*\d+(\.\d+)?%?\s*,\s*\d+(\.\d+)?%?\s*$")


def parse_point(value: Any) -> str:
    if not isinstance(value, str) or not _POINT_RE.match(value):
        raise SelectorError("'point' must be 'x,y' or 'x%,y%' (e.g., \"50%,50%\").")
    return value.strip()


def selector_from_value(
    value: Any, extra_keys: frozenset[str] = frozenset()
) -> Selector:
    """Convert text or a YAML map into a ``Selector``.

    ``extra_keys`` are *command*-level keys (e.g., ``optional``, ``label``)
    that coexist with the selector and should be ignored here.
    """
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return Selector(text=str(value))
    if not isinstance(value, dict):
        raise SelectorError("Selector must be text or a YAML map.")
    for key in value:
        if key in _UNSUPPORTED_KEYS:
            raise SelectorError(f"'{key}' is not supported: {_UNSUPPORTED_KEYS[key]}.")
        if key not in SELECTOR_KEYS and key not in extra_keys:
            allowed = ", ".join(sorted(SELECTOR_KEYS | extra_keys))
            raise SelectorError(f"Unknown selector key: '{key}'. Allowed: {allowed}.")
    sel = Selector()
    if "text" in value:
        sel.text = _scalar_text("text", value["text"])
    if "id" in value:
        sel.id = _scalar_text("id", value["id"])
    if "index" in value:
        sel.index = _parse_index(value["index"])
    for key in ("enabled", "checked", "focused", "selected"):
        if key in value:
            setattr(sel, key, _parse_bool(key, value[key]))
    if "point" in value:
        sel.point = parse_point(value["point"])
    for key, attr in (
        ("below", "below"),
        ("above", "above"),
        ("leftOf", "left_of"),
        ("rightOf", "right_of"),
        ("containsChild", "contains_child"),
        ("childOf", "child_of"),
    ):
        if key in value:
            setattr(sel, attr, selector_from_value(value[key]))
    if "containsDescendants" in value:
        raw = value["containsDescendants"]
        if not isinstance(raw, list) or not raw:
            raise SelectorError("'containsDescendants' must be a list of selectors.")
        sel.contains_descendants = [selector_from_value(item) for item in raw]
    criteria = (
        sel.text,
        sel.id,
        sel.index,
        sel.enabled,
        sel.checked,
        sel.focused,
        sel.selected,
        sel.point,
        sel.below,
        sel.above,
        sel.left_of,
        sel.right_of,
        sel.contains_child,
        sel.child_of,
    )
    if all(c is None for c in criteria) and not sel.contains_descendants:
        raise SelectorError("Empty selector: provide text, id, or another criterion.")
    return sel


# ----------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------

_FLAGS = re.IGNORECASE | re.DOTALL | re.MULTILINE


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, _FLAGS)
    except re.error:
        return None


def regex_matches(pattern: str, value: str) -> bool:
    """Literal equality or full-match regex (like Maestro)."""
    if not value:
        return False
    if pattern == value:
        return True
    compiled = _compiled(pattern)
    return bool(compiled and compiled.fullmatch(value))


def _id_matches(pattern: str, resource_id: str) -> bool:
    if not resource_id:
        return False
    if regex_matches(pattern, resource_id):
        return True
    short = resource_id.rsplit("/", 1)[-1]
    return regex_matches(pattern, short)


def _basic_match(element: UiElement, sel: Selector) -> bool:
    if sel.text is not None and not any(
        regex_matches(sel.text, candidate)
        for candidate in (element.text, element.content_desc, element.hint)
    ):
        return False
    if sel.id is not None and not _id_matches(sel.id, element.resource_id):
        return False
    return not any(
        wanted is not None and getattr(element, attr) != wanted
        for attr, wanted in (
            ("enabled", sel.enabled),
            ("checked", sel.checked),
            ("focused", sel.focused),
            ("selected", sel.selected),
        )
    )


def _is_descendant(
    elements: list[UiElement], element: UiElement, ancestor: int
) -> bool:
    parent = element.parent
    while parent is not None:
        if parent == ancestor:
            return True
        parent = elements[parent].parent
    return False


def _distance(a: UiElement, b: UiElement) -> int:
    ax, ay = a.center
    bx, by = b.center
    return abs(ax - bx) + abs(ay - by)


def candidates(elements: list[UiElement], sel: Selector) -> list[UiElement]:
    """Elements matching the selector, in order of preference."""
    result = [e for e in elements if e.visible and _basic_match(e, sel)]

    if sel.child_of is not None:
        parents = {p.index for p in candidates(elements, sel.child_of)}
        result = [e for e in result if e.parent in parents]
    if sel.contains_child is not None:
        children = candidates(elements, sel.contains_child)
        owners = {c.parent for c in children}
        result = [e for e in result if e.index in owners]
    for desc_sel in sel.contains_descendants:
        matched = candidates(elements, desc_sel)
        result = [
            e
            for e in result
            if any(_is_descendant(elements, m, e.index) for m in matched)
        ]

    relations = (
        (sel.below, lambda e, a: e.center[1] > a.center[1]),
        (sel.above, lambda e, a: e.center[1] < a.center[1]),
        (sel.left_of, lambda e, a: e.center[0] < a.center[0]),
        (sel.right_of, lambda e, a: e.center[0] > a.center[0]),
    )
    for anchor_sel, predicate in relations:
        if anchor_sel is None:
            continue
        anchors = candidates(elements, anchor_sel)
        if not anchors:
            return []
        anchor = anchors[0]
        result = [
            e
            for e in result
            if e.index != anchor.index
            and not _is_descendant(elements, anchor, e.index)
            and predicate(e, anchor)
        ]
        result.sort(key=lambda e, a=anchor: _distance(e, a))

    # Among nested matching elements, the deepest one wins.
    indexes = {e.index for e in result}
    result = [
        e
        for e in result
        if not any(
            other != e.index and _is_descendant(elements, elements[other], e.index)
            for other in indexes
        )
    ]
    return result


def find_element(elements: list[UiElement], sel: Selector) -> UiElement | None:
    found = candidates(elements, sel)
    if sel.index is not None:
        try:
            return found[sel.index]
        except IndexError:
            return None
    return found[0] if found else None
