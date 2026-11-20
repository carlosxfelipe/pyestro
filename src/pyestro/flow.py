"""Reading and validation of Maestro-compatible YAML flows.

Pyestro only accepts syntax that Maestro also accepts. Valid Maestro commands
that Pyestro does not implement yet (e.g., ``evalScript``) are rejected with
a specific error message rather than being treated as typos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .android import KEY_NAMES, PERMISSION_GROUPS, normalize_key
from .selectors import Selector, SelectorError, selector_from_value


class FlowError(RuntimeError):
    """Invalid flow or failed step."""


# ----------------------------------------------------------------------
# ${...} interpolation
# ----------------------------------------------------------------------

_EXPR_RE = re.compile(r"\$\{([^{}]*)\}")
_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")

# `${maestro.copiedText}` only exists at run time (after copyTextFrom or
# setClipboard), so it is kept as-is while the flow is compiled and the runner
# substitutes it when the command executes.
COPIED_TEXT_EXPR = "maestro.copiedText"
COPIED_TEXT_TOKEN = "${" + COPIED_TEXT_EXPR + "}"


def _literal(expr: str) -> str | None:
    expr = expr.strip()
    if len(expr) >= 2 and expr[0] == expr[-1] and expr[0] in "'\"":
        return expr[1:-1]
    if _NUM_RE.match(expr):
        return expr
    return None


def _lookup(name: str, env: dict[str, str]) -> str | None:
    return env.get(name)


def _eval_expression(expr: str, env: dict[str, str]) -> str:
    """Evaluate the subset of JavaScript supported inside ``${...}``.

    Supports ``${VAR}``, ``${'text'}``, ``${VAR || 'default'}``, and
    ``${VAR ?? 'default'}``. Any other expression requires Maestro's JS engine.
    """
    if expr.strip() == COPIED_TEXT_EXPR:
        return COPIED_TEXT_TOKEN
    literal = _literal(expr)
    if literal is not None:
        return literal
    for op in ("||", "??"):
        if op in expr:
            left, right = (part.strip() for part in expr.split(op, 1))
            if not _IDENT_RE.match(left):
                break
            fallback = _literal(right)
            if fallback is None:
                if not _IDENT_RE.match(right):
                    break
                fallback = _lookup(right, env)
                if fallback is None:
                    raise FlowError(
                        f"Variable '{right}' is not defined in ${{{expr}}}."
                    )
            value = _lookup(left, env)
            empty = value is None or (op == "||" and value == "")
            return fallback if empty else str(value)
    else:
        literal = _literal(expr)
        if literal is not None:
            return literal
        name = expr.strip()
        if _IDENT_RE.match(name):
            value = _lookup(name, env)
            if value is None:
                raise FlowError(
                    f"Variable '{name}' is not defined (use env: in the header or -e)."
                )
            return str(value)
    raise FlowError(
        f"JavaScript expression not supported by Pyestro: ${{{expr}}}. "
        "Use only ${VAR}, ${VAR || 'default'}, or ${'text'}."
    )


def interpolate_string(text: str, env: dict[str, str]) -> str:
    return _EXPR_RE.sub(lambda m: _eval_expression(m.group(1), env), text)


def interpolate_data(
    data: Any, env: dict[str, str], skip_keys: frozenset[str] = frozenset()
) -> Any:
    if isinstance(data, str):
        return interpolate_string(data, env)
    if isinstance(data, dict):
        return {
            k: (v if k in skip_keys else interpolate_data(v, env, skip_keys))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [interpolate_data(item, env, skip_keys) for item in data]
    return data


# ----------------------------------------------------------------------
# Command catalog
# ----------------------------------------------------------------------

# Maestro commands not yet implemented (require JS, iOS, AI, etc.).
UNSUPPORTED_COMMANDS = frozenset(
    {
        "assertTrue",
        "assertScreenshot",
        "assertWithAI",
        "assertNoDefectsWithAI",
        "extractTextWithAI",
        "evalScript",
        "runScript",
        "travel",
        "openBrowser",
    }
)

CONFIG_KEYS = frozenset(
    {
        "appId",
        "url",
        "name",
        "tags",
        "env",
        "properties",
        "onFlowStart",
        "onFlowComplete",
        "jsEngine",
        "androidWebViewHierarchy",
        "ext",
    }
)

DIRECTIONS = frozenset({"UP", "DOWN", "LEFT", "RIGHT"})
ORIENTATIONS = frozenset(
    {"PORTRAIT", "LANDSCAPE_LEFT", "LANDSCAPE_RIGHT", "UPSIDE_DOWN"}
)
_TAP_EXTRAS = frozenset(
    {
        "optional",
        "label",
        "retryTapIfNoChange",
        "repeat",
        "delay",
        "waitToSettleTimeoutMs",
        "longPress",
    }
)
_ASSERT_EXTRAS = frozenset({"optional", "label"})


@dataclass
class Command:
    name: str
    spec: dict[str, Any] = field(default_factory=dict)
    label: str | None = None
    optional: bool = False
    nested: list[Command] = field(default_factory=list)

    @property
    def display(self) -> str:
        return self.name


@dataclass
class FlowConfig:
    app_id: str | None
    name: str | None
    tags: list[str]
    env: dict[str, str]
    on_start: list[Any]
    on_complete: list[Any]


# ----------------------------------------------------------------------
# Validation helpers
# ----------------------------------------------------------------------


def _check_keys(
    cmd: str, value: dict[str, Any], allowed: frozenset[str] | set[str]
) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise FlowError(
            f"{cmd}: unknown key(s): {', '.join(map(str, unknown))}. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )


def _bool(cmd: str, key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise FlowError(f"{cmd}: '{key}' must be true or false.")


def _int(cmd: str, key: str, value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise FlowError(f"{cmd}: '{key}' must be an integer.")
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise FlowError(f"{cmd}: '{key}' must be an integer.") from None
    if not isinstance(value, int) or value < minimum:
        raise FlowError(f"{cmd}: '{key}' must be an integer >= {minimum}.")
    return value


def _ms(cmd: str, key: str, value: Any) -> float:
    """Milliseconds (like Maestro) -> seconds."""
    if isinstance(value, bool):
        raise FlowError(f"{cmd}: '{key}' must be a number in milliseconds.")
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise FlowError(
                f"{cmd}: '{key}' must be a number in milliseconds "
                "(Maestro does not accept units like '5s')."
            ) from None
    if not isinstance(value, (int, float)) or value < 0:
        raise FlowError(f"{cmd}: '{key}' must be a number of ms >= 0.")
    return float(value) / 1000.0


def _text(cmd: str, key: str, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise FlowError(f"{cmd}: '{key}' must be text.")
    return str(value)


def _selector(cmd: str, value: Any, extras: frozenset[str] = frozenset()) -> Selector:
    try:
        return selector_from_value(value, extras)
    except SelectorError as exc:
        raise FlowError(f"{cmd}: {exc}") from exc


def _direction(cmd: str, value: Any, default: str = "DOWN") -> str:
    if value is None:
        return default
    direction = str(value).upper()
    if direction not in DIRECTIONS:
        raise FlowError(f"{cmd}: direction must be UP, DOWN, LEFT, or RIGHT.")
    return direction


def _optional_dict(cmd: str, value: Any, allowed: frozenset[str]) -> dict[str, Any]:
    """For argumentless commands: accepts empty or a map (e.g., only `label`)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FlowError(f"{cmd}: does not accept arguments (use a map or nothing).")
    _check_keys(cmd, value, allowed)
    return value


# ----------------------------------------------------------------------
# Per-command parsers: return (spec, label, optional)
# ----------------------------------------------------------------------

Parsed = tuple[dict[str, Any], str | None, bool]


def _label(cmd: str, value: dict[str, Any]) -> str | None:
    label = value.get("label")
    return None if label is None else _text(cmd, "label", label)


def _parse_permissions(cmd: str, perms: Any) -> dict[str, str]:
    if not isinstance(perms, dict):
        raise FlowError(f"{cmd}: permissions must be a map (e.g., all: allow).")
    for name, setting in perms.items():
        if str(setting).lower() not in ("allow", "deny", "unset"):
            raise FlowError(f"{cmd}: permissions.{name} must be allow, deny, or unset.")
    return {str(k): str(v).lower() for k, v in perms.items()}


def _p_launch_app(cmd: str, value: Any) -> Parsed:
    spec: dict[str, Any] = {
        "appId": None,
        "clearState": False,
        "stopApp": True,
        "permissions": {"all": "allow"},
        "arguments": {},
    }
    label = None
    if isinstance(value, str):
        spec["appId"] = value
    elif isinstance(value, dict):
        _check_keys(
            cmd,
            value,
            {
                "appId",
                "clearState",
                "clearKeychain",
                "stopApp",
                "permissions",
                "arguments",
                "label",
            },
        )
        if "arguments" in value:
            args = value["arguments"]
            if not isinstance(args, dict):
                raise FlowError(f"{cmd}: arguments must be a key-value map.")
            parsed_args: dict[str, str | int | float | bool] = {}
            for k, v in args.items():
                if not isinstance(v, (str, int, float, bool)):
                    raise FlowError(
                        f"{cmd}: arguments.{k} must be text, number, or boolean."
                    )
                parsed_args[str(k)] = v
            spec["arguments"] = parsed_args
        if "appId" in value:
            spec["appId"] = _text(cmd, "appId", value["appId"])
        for key in ("clearState", "stopApp"):
            if key in value:
                spec[key] = _bool(cmd, key, value[key])
        if "permissions" in value:
            spec["permissions"] = _parse_permissions(cmd, value["permissions"])
        label = _label(cmd, value)
    elif value is not None:
        raise FlowError(f"{cmd}: use '- launchApp', an appId, or a map.")
    return spec, label, False


def _p_set_permissions(cmd: str, value: Any) -> Parsed:
    if not isinstance(value, dict):
        raise FlowError(f"{cmd}: provide a map with 'permissions'.")
    _check_keys(cmd, value, {"appId", "permissions", "label"})
    if "permissions" not in value or not value["permissions"]:
        raise FlowError(f"{cmd}: provide 'permissions'.")
    app_id = _text(cmd, "appId", value["appId"]) if "appId" in value else None
    return (
        {
            "appId": app_id,
            "permissions": _parse_permissions(cmd, value["permissions"]),
        },
        _label(cmd, value),
        False,
    )


def _p_app_target(cmd: str, value: Any) -> Parsed:
    if value is None:
        return {"appId": None}, None, False
    if isinstance(value, str):
        return {"appId": value}, None, False
    if isinstance(value, dict):
        _check_keys(cmd, value, {"appId", "label"})
        app = value.get("appId")
        return (
            {"appId": None if app is None else _text(cmd, "appId", app)},
            _label(cmd, value),
            False,
        )
    raise FlowError(f"{cmd}: provide appId as text or nothing.")


def _p_tap(cmd: str, value: Any) -> Parsed:
    if value is None:
        raise FlowError(f"{cmd}: provide a selector.")
    sel = _selector(cmd, value, _TAP_EXTRAS)
    spec: dict[str, Any] = {
        "selector": sel,
        "retry": False,
        "repeat": 1,
        "delay": 0.1,
        "longPress": cmd == "longPressOn",
        "double": cmd == "doubleTapOn",
    }
    label = None
    optional = False
    if isinstance(value, dict):
        if "retryTapIfNoChange" in value:
            spec["retry"] = _bool(
                cmd, "retryTapIfNoChange", value["retryTapIfNoChange"]
            )
        if "repeat" in value:
            spec["repeat"] = _int(cmd, "repeat", value["repeat"], minimum=1)
        if "delay" in value:
            spec["delay"] = _ms(cmd, "delay", value["delay"])
        if "longPress" in value and _bool(cmd, "longPress", value["longPress"]):
            spec["longPress"] = True
        if "waitToSettleTimeoutMs" in value:
            _int(cmd, "waitToSettleTimeoutMs", value["waitToSettleTimeoutMs"])
        optional = (
            _bool(cmd, "optional", value["optional"]) if "optional" in value else False
        )
        label = _label(cmd, value)
    return spec, label, optional


def _p_assert(cmd: str, value: Any) -> Parsed:
    if value is None:
        raise FlowError(f"{cmd}: provide a selector.")
    sel = _selector(cmd, value, _ASSERT_EXTRAS)
    if sel.point is not None:
        raise FlowError(f"{cmd}: 'point' is not accepted in assertions.")
    optional = False
    label = None
    if isinstance(value, dict):
        optional = (
            _bool(cmd, "optional", value["optional"]) if "optional" in value else False
        )
        label = _label(cmd, value)
    return {"selector": sel}, label, optional


def _p_input_text(cmd: str, value: Any) -> Parsed:
    if isinstance(value, dict):
        _check_keys(cmd, value, {"text", "label"})
        if "text" not in value:
            raise FlowError(f"{cmd}: provide 'text'.")
        return {"text": _text(cmd, "text", value["text"])}, _label(cmd, value), False
    if value is None:
        raise FlowError(f"{cmd}: provide text.")
    if isinstance(value, bool):
        raise FlowError(f"{cmd}: quote values like yes/no/true/false.")
    return {"text": _text(cmd, "text", value)}, None, False


def _p_input_random(cmd: str, value: Any) -> Parsed:
    value = _optional_dict(cmd, value, frozenset({"length", "label"}))
    length = _int(cmd, "length", value["length"], minimum=1) if "length" in value else 8
    return {"length": length}, _label(cmd, value), False


def _p_erase_text(cmd: str, value: Any) -> Parsed:
    label = None
    if isinstance(value, dict):
        _check_keys(cmd, value, {"charactersToErase", "label"})
        label = _label(cmd, value)
        value = value.get("charactersToErase")
    chars = 50 if value is None else _int(cmd, "charactersToErase", value, minimum=1)
    return {"chars": chars}, label, False


def _p_press_key(cmd: str, value: Any) -> Parsed:
    label = None
    if isinstance(value, dict):
        _check_keys(cmd, value, {"key", "label"})
        label = _label(cmd, value)
        value = value.get("key")
    if not isinstance(value, str):
        raise FlowError(f"{cmd}: provide key name (e.g., Enter).")
    if normalize_key(value) not in KEY_NAMES:
        names = ", ".join(sorted(KEY_NAMES))
        raise FlowError(f"{cmd}: unknown key '{value}'. Available: {names}.")
    return {"key": value}, label, False


def _p_noarg(cmd: str, value: Any) -> Parsed:
    value = _optional_dict(cmd, value, frozenset({"label"}))
    return {}, _label(cmd, value), False


def _p_wait_animation(cmd: str, value: Any) -> Parsed:
    value = _optional_dict(cmd, value, frozenset({"timeout", "label"}))
    timeout = _ms(cmd, "timeout", value["timeout"]) if "timeout" in value else 15.0
    return {"timeout": timeout}, _label(cmd, value), False


def _p_swipe(cmd: str, value: Any) -> Parsed:
    if not isinstance(value, dict):
        raise FlowError(f"{cmd}: use direction, or start and end.")
    _check_keys(cmd, value, {"direction", "start", "end", "from", "duration", "label"})
    spec: dict[str, Any] = {"duration": 0.4, "from": None}
    if "duration" in value:
        spec["duration"] = _ms(cmd, "duration", value["duration"])
    if "direction" in value:
        if "start" in value or "end" in value:
            raise FlowError(f"{cmd}: use direction OR start/end, not both.")
        spec["direction"] = _direction(cmd, value["direction"])
        if "from" in value:
            spec["from"] = _selector(cmd, value["from"])
    elif "start" in value and "end" in value:
        if "from" in value:
            raise FlowError(f"{cmd}: 'from' can only be combined with direction.")
        from .selectors import parse_point

        try:
            spec["start"] = parse_point(value["start"])
            spec["end"] = parse_point(value["end"])
        except SelectorError as exc:
            raise FlowError(f"{cmd}: invalid start/end. {exc}") from exc
    else:
        raise FlowError(f"{cmd}: provide direction, or start and end.")
    return spec, _label(cmd, value), False


def _p_scroll_until_visible(cmd: str, value: Any) -> Parsed:
    if not isinstance(value, dict):
        raise FlowError(f"{cmd}: use a map with 'element'.")
    _check_keys(
        cmd,
        value,
        {
            "element",
            "direction",
            "timeout",
            "speed",
            "visibilityPercentage",
            "centerElement",
            "label",
        },
    )
    if "element" not in value:
        raise FlowError(f"{cmd}: provide 'element'.")
    spec: dict[str, Any] = {
        "selector": _selector(cmd, value["element"]),
        "direction": _direction(cmd, value.get("direction")),
        "timeout": _ms(cmd, "timeout", value["timeout"])
        if "timeout" in value
        else 20.0,
        "speed": _int(cmd, "speed", value["speed"]) if "speed" in value else 40,
        "centerElement": (
            _bool(cmd, "centerElement", value["centerElement"])
            if "centerElement" in value
            else False
        ),
    }
    if "visibilityPercentage" in value:
        _int(cmd, "visibilityPercentage", value["visibilityPercentage"])
    return spec, _label(cmd, value), False


def _p_open_link(cmd: str, value: Any) -> Parsed:
    if isinstance(value, dict):
        _check_keys(cmd, value, {"link", "autoVerify", "browser", "label"})
        if "link" not in value:
            raise FlowError(f"{cmd}: provide 'link'.")
        if "autoVerify" in value:
            _bool(cmd, "autoVerify", value["autoVerify"])
        browser = (
            _bool(cmd, "browser", value["browser"]) if "browser" in value else False
        )
        return (
            {"link": _text(cmd, "link", value["link"]), "browser": browser},
            _label(cmd, value),
            False,
        )
    if not isinstance(value, str) or not value.strip():
        raise FlowError(f"{cmd}: provide the URL as text.")
    return {"link": value, "browser": False}, None, False


def _p_extended_wait(cmd: str, value: Any) -> Parsed:
    if not isinstance(value, dict):
        raise FlowError(f"{cmd}: use 'visible' or 'notVisible' and 'timeout'.")
    _check_keys(cmd, value, {"visible", "notVisible", "timeout", "label"})
    if ("visible" in value) == ("notVisible" in value):
        raise FlowError(f"{cmd}: provide exactly one of 'visible' or 'notVisible'.")
    mode = "visible" if "visible" in value else "notVisible"
    return (
        {
            "mode": mode,
            "selector": _selector(cmd, value[mode], _ASSERT_EXTRAS),
            "timeout": (
                _ms(cmd, "timeout", value["timeout"]) if "timeout" in value else None
            ),
        },
        _label(cmd, value),
        False,
    )


def _p_screenshot(cmd: str, value: Any) -> Parsed:
    if isinstance(value, dict):
        _check_keys(cmd, value, {"path", "label", "cropOn"})
        if "path" not in value:
            raise FlowError(f"{cmd}: provide 'path'.")
        crop_on = _selector(cmd, value["cropOn"]) if "cropOn" in value else None
        return (
            {"path": _text(cmd, "path", value["path"]), "cropOn": crop_on},
            _label(cmd, value),
            False,
        )
    if not isinstance(value, str) or not value.strip():
        raise FlowError(f"{cmd}: provide the filename.")
    return {"path": value, "cropOn": None}, None, False


def _p_start_recording(cmd: str, value: Any) -> Parsed:
    if isinstance(value, dict):
        _check_keys(cmd, value, {"path", "label", "optional"})
        if "path" not in value:
            raise FlowError(f"{cmd}: provide 'path'.")
        path = _text(cmd, "path", value["path"])
        if not path.strip():
            raise FlowError(f"{cmd}: provide 'path'.")
        optional = (
            _bool(cmd, "optional", value["optional"]) if "optional" in value else False
        )
        return {"path": path}, _label(cmd, value), optional
    if not isinstance(value, str) or not value.strip():
        raise FlowError(f"{cmd}: provide the recording path.")
    return {"path": value}, None, False


def _parse_condition(cmd: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise FlowError(f"{cmd}: condition must be a map (visible, notVisible...).")
    # PyYAML (YAML 1.1) parses the `true:` key as the boolean True.
    value = {("true" if k is True else k): v for k, v in value.items()}
    _check_keys(cmd, value, {"platform", "visible", "notVisible", "true", "label"})
    cond: dict[str, Any] = {}
    for key in ("visible", "notVisible"):
        if key in value:
            cond[key] = _selector(cmd, value[key], _ASSERT_EXTRAS)
    if "platform" in value:
        platform = str(value["platform"]).lower()
        if platform not in ("android", "ios", "web"):
            raise FlowError(f"{cmd}: platform must be Android, iOS, or Web.")
        cond["platform"] = platform
    if "true" in value:
        cond["true"] = value["true"]
    if not cond:
        raise FlowError(f"{cmd}: empty condition.")
    return cond


def _check_commands(cmd: str, value: Any) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise FlowError(f"{cmd}: 'commands' must be a non-empty list.")
    return value


def _p_repeat(cmd: str, value: Any) -> Parsed:
    if not isinstance(value, dict):
        raise FlowError(f"{cmd}: use 'times' or 'while' and 'commands'.")
    _check_keys(cmd, value, {"times", "while", "commands", "label"})
    _check_commands(cmd, value.get("commands"))
    if ("times" in value) == ("while" in value):
        raise FlowError(f"{cmd}: provide exactly one of 'times' or 'while'.")
    spec: dict[str, Any] = {}
    if "times" in value:
        spec["times"] = _int(cmd, "times", value["times"], minimum=1)
    else:
        spec["while"] = _parse_condition(cmd, value["while"])
    return spec, _label(cmd, value), False


def _p_retry(cmd: str, value: Any) -> Parsed:
    if not isinstance(value, dict):
        raise FlowError(f"{cmd}: use 'maxRetries' and 'commands' or 'file'.")
    _check_keys(cmd, value, {"maxRetries", "commands", "file", "env", "label"})
    if ("commands" in value) == ("file" in value):
        raise FlowError(f"{cmd}: provide exactly one of 'commands' or 'file'.")
    if "commands" in value:
        _check_commands(cmd, value["commands"])
    retries = (
        _int(cmd, "maxRetries", value["maxRetries"]) if "maxRetries" in value else 1
    )
    if not 0 <= retries <= 3:
        raise FlowError(f"{cmd}: maxRetries must be between 0 and 3.")
    spec: dict[str, Any] = {
        "maxRetries": retries,
        "env": _parse_env(cmd, value.get("env")),
    }
    if "file" in value:
        spec["file"] = _text(cmd, "file", value["file"])
    return spec, _label(cmd, value), False


def _parse_env(cmd: str, value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FlowError(f"{cmd}: env must be a KEY: value map.")
    return {str(k): "" if v is None else str(v) for k, v in value.items()}


def _p_run_flow(cmd: str, value: Any) -> Parsed:
    if isinstance(value, str):
        return {"file": value, "env": {}, "when": None}, None, False
    if not isinstance(value, dict):
        raise FlowError(f"{cmd}: use a file path or a map.")
    _check_keys(cmd, value, {"file", "commands", "when", "env", "label"})
    if ("file" in value) == ("commands" in value):
        raise FlowError(f"{cmd}: provide exactly one of 'file' or 'commands'.")
    spec: dict[str, Any] = {
        "env": _parse_env(cmd, value.get("env")),
        "when": _parse_condition(cmd, value["when"]) if "when" in value else None,
    }
    if "file" in value:
        spec["file"] = _text(cmd, "file", value["file"])
    else:
        _check_commands(cmd, value["commands"])
    return spec, _label(cmd, value), False


def _p_set_orientation(cmd: str, value: Any) -> Parsed:
    if not isinstance(value, str):
        allowed = ", ".join(sorted(ORIENTATIONS))
        raise FlowError(f"{cmd}: provide orientation ({allowed}).")
    orientation = value.strip().upper()
    if orientation not in ORIENTATIONS:
        allowed = ", ".join(sorted(ORIENTATIONS))
        raise FlowError(f"{cmd}: orientation must be one of: {allowed}.")
    return {"orientation": orientation}, None, False


def _p_set_airplane_mode(cmd: str, value: Any) -> Parsed:
    if not isinstance(value, str) or value.strip().lower() not in (
        "enabled",
        "disabled",
    ):
        raise FlowError(f"{cmd}: provide 'enabled' or 'disabled'.")
    return {"enabled": value.strip().lower() == "enabled"}, None, False


def _p_add_media(cmd: str, value: Any) -> Parsed:
    if not isinstance(value, list) or not value:
        raise FlowError(f"{cmd}: provide a non-empty list of media file paths.")
    files: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise FlowError(f"{cmd}: each media entry must be a file path string.")
        files.append(item.strip())
    return {"files": files}, None, False


def _p_set_dark_mode(cmd: str, value: Any) -> Parsed:
    if value is None:
        raise FlowError(f"{cmd}: provide true or false.")
    return {"enabled": _bool(cmd, cmd, value)}, None, False


def _p_set_clipboard(cmd: str, value: Any) -> Parsed:
    if value is None or isinstance(value, dict | list):
        raise FlowError(f"{cmd}: provide the text to put in the clipboard.")
    return {"text": _text(cmd, "text", value)}, None, False


def _coordinate(cmd: str, key: str, value: Any, limit: float) -> float:
    if isinstance(value, bool):
        raise FlowError(f"{cmd}: '{key}' must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FlowError(f"{cmd}: '{key}' must be a number.") from None
    if not -limit <= number <= limit:
        raise FlowError(f"{cmd}: '{key}' must be between -{limit:g} and {limit:g}.")
    return number


def _p_set_location(cmd: str, value: Any) -> Parsed:
    if not isinstance(value, dict):
        raise FlowError(f"{cmd}: use a map with 'latitude' and 'longitude'.")
    _check_keys(cmd, value, {"latitude", "longitude", "label"})
    for key in ("latitude", "longitude"):
        if key not in value:
            raise FlowError(f"{cmd}: provide '{key}'.")
    return (
        {
            "latitude": _coordinate(cmd, "latitude", value["latitude"], 90),
            "longitude": _coordinate(cmd, "longitude", value["longitude"], 180),
        },
        _label(cmd, value),
        False,
    )


_PARSERS = {
    "launchApp": _p_launch_app,
    "stopApp": _p_app_target,
    "killApp": _p_app_target,
    "clearState": _p_app_target,
    "setPermissions": _p_set_permissions,
    "tapOn": _p_tap,
    "doubleTapOn": _p_tap,
    "longPressOn": _p_tap,
    "assertVisible": _p_assert,
    "assertNotVisible": _p_assert,
    "inputText": _p_input_text,
    "inputRandomText": _p_input_random,
    "inputRandomNumber": _p_input_random,
    "inputRandomEmail": _p_input_random,
    "inputRandomPersonName": _p_input_random,
    "eraseText": _p_erase_text,
    "pressKey": _p_press_key,
    "hideKeyboard": _p_noarg,
    "back": _p_noarg,
    "scroll": _p_noarg,
    "clearKeychain": _p_noarg,
    "waitForAnimationToEnd": _p_wait_animation,
    "swipe": _p_swipe,
    "scrollUntilVisible": _p_scroll_until_visible,
    "openLink": _p_open_link,
    "extendedWaitUntil": _p_extended_wait,
    "takeScreenshot": _p_screenshot,
    "startRecording": _p_start_recording,
    "stopRecording": _p_noarg,
    "setOrientation": _p_set_orientation,
    "setAirplaneMode": _p_set_airplane_mode,
    "toggleAirplaneMode": _p_noarg,
    "addMedia": _p_add_media,
    "setDarkMode": _p_set_dark_mode,
    "toggleDarkMode": _p_noarg,
    "assertDarkMode": _p_noarg,
    "assertLightMode": _p_noarg,
    "copyTextFrom": _p_assert,
    "pasteText": _p_noarg,
    "setClipboard": _p_set_clipboard,
    "setLocation": _p_set_location,
    "repeat": _p_repeat,
    "retry": _p_retry,
    "runFlow": _p_run_flow,
}

SUPPORTED_COMMANDS = frozenset(_PARSERS)
# Command key containing a list of nested commands.
_NESTED_KEY = "commands"


# ----------------------------------------------------------------------
# Compilation
# ----------------------------------------------------------------------


def split_command(raw: Any, position: str) -> tuple[str, Any]:
    if isinstance(raw, str):
        return raw, None
    if isinstance(raw, dict) and len(raw) == 1:
        name, value = next(iter(raw.items()))
        return str(name), value
    raise FlowError(
        f"Command {position}: each item must be a command name or a map "
        "with a single key."
    )


def compile_commands(
    raw_commands: list[Any], env: dict[str, str], prefix: str = ""
) -> list[Command]:
    """Validate and convert the raw YAML list into ``Command`` objects (fail-fast)."""
    if not isinstance(raw_commands, list):
        raise FlowError("The commands section must be a list.")
    commands: list[Command] = []
    for idx, raw in enumerate(raw_commands, start=1):
        position = f"{prefix}{idx}"
        name, raw_value = split_command(raw, position)
        try:
            commands.append(_compile_one(name, raw_value, env))
        except FlowError as exc:
            if str(exc).startswith("Command "):
                raise
            raise FlowError(f"Command {position}: {exc}") from exc
    return commands


def _compile_one(name: str, raw_value: Any, env: dict[str, str]) -> Command:
    if name in UNSUPPORTED_COMMANDS:
        raise FlowError(
            f"'{name}' is a Maestro command that Pyestro does not support yet."
        )
    parser = _PARSERS.get(name)
    if parser is None:
        raise FlowError(
            f"unknown command '{name}'. Not a Maestro command. "
            f"Supported: {', '.join(sorted(SUPPORTED_COMMANDS))}."
        )
    inner_env = dict(env)
    if name in ("runFlow", "retry") and isinstance(raw_value, dict):
        raw_env = raw_value.get("env")
        if isinstance(raw_env, dict):
            inner_env.update(
                {
                    str(k): interpolate_string(str(v), env) if v is not None else ""
                    for k, v in raw_env.items()
                }
            )
    value = interpolate_data(raw_value, env, frozenset({_NESTED_KEY, "env"}))
    if (
        name in ("runFlow", "retry")
        and isinstance(value, dict)
        and isinstance(value.get("env"), dict)
    ):
        value["env"] = {k: inner_env[str(k)] for k in value["env"]}
    spec, label, optional = parser(name, value)
    cmd = Command(name, spec, label, optional)
    if isinstance(raw_value, dict) and _NESTED_KEY in raw_value:
        cmd.nested = compile_commands(raw_value[_NESTED_KEY], inner_env, f"{name} › ")
    return cmd


def _config_env(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FlowError("env in the header must be a KEY: value map.")
    return {str(k): "" if v is None else str(v) for k, v in raw.items()}


def parse_config(raw: Any) -> FlowConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise FlowError("The flow header must be a map (appId, env, ...).")
    app_id = raw.get("appId")
    if app_id is not None and not isinstance(app_id, str):
        raise FlowError("appId must be text.")
    tags = raw.get("tags") or []
    if not isinstance(tags, list):
        raise FlowError("tags must be a list.")
    return FlowConfig(
        app_id=app_id,
        name=None if raw.get("name") is None else str(raw["name"]),
        tags=[str(t) for t in tags],
        env=_config_env(raw.get("env")),
        on_start=raw.get("onFlowStart") or [],
        on_complete=raw.get("onFlowComplete") or [],
    )


def read_flow_file(path: Path) -> tuple[Any, list[Any]]:
    """Read YAML and return (raw header, raw commands list)."""
    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise FlowError(f"Flow file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise FlowError(f"Invalid YAML in {path}: {exc}") from exc

    if len(docs) == 2 and (docs[0] is None or isinstance(docs[0], dict)):
        header, commands = docs
    elif len(docs) == 1 and isinstance(docs[0], list):
        header, commands = None, docs[0]
    else:
        raise FlowError(
            "Expected format (matching Maestro): header (appId...), '---' "
            "separator, and the list of commands."
        )
    if not isinstance(commands, list):
        raise FlowError("After '---', there must be a list of commands ('- command').")
    return header, commands


def permission_names(group: str) -> tuple[str, ...]:
    """Resolve `notifications`/`location`... or an Android permission name."""
    key = group.lower()
    if key in PERMISSION_GROUPS:
        return PERMISSION_GROUPS[key]
    return (group if "." in group else f"android.permission.{group}",)
