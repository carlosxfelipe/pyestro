# Pyestro

A Python runner for YAML flows **100% compatible with the [Maestro](https://docs.maestro.dev/) syntax**, running on Android via ADB (no helper APKs or daemons). iOS is not supported for now.

> **Golden rule:** every `.yaml` file accepted by Pyestro is also accepted by Maestro, with the same meaning. Pyestro **does not invent commands or keys**. If something from Maestro does not exist here yet, Pyestro rejects the flow with a clear message instead of misinterpreting it. See [docs/maestro_compat.md](docs/maestro_compat.md).

Project derived from AndroFlow, but without its syntax extensions (`waitFor`, `sleep`, `contentDesc`, `contains`...), which Maestro does not recognize.

## Requirements

- Python 3.11+, [uv](https://docs.astral.sh/uv/)
- Android SDK Platform Tools (`adb`) and an emulator or device with USB debugging

## Installation

```bash
uv tool install git+https://github.com/carlosxfelipe/pyestro.git   # global
# or, to develop:
uv sync
```

## Usage

```bash
pyestro devices
pyestro test flows/login.yaml
pyestro test flows/                      # all .yaml/.yml files in the folder
pyestro test flows/ -e USER=ana -e PASS=123
pyestro test flows/ --include-tags smoke --exclude-tags wip
pyestro test flows/login.yaml --record --report   # video + HTML/JUnit reports in artifacts/
```

Example flow (identical to what you would write for Maestro):

```yaml
appId: com.android.settings
name: Search in settings
tags: [smoke]
env:
  QUERY: display
---
- launchApp
- tapOn: "Search settings"
- inputText: ${QUERY}
- extendedWaitUntil:
    visible: "Display"
    timeout: 5000
- assertVisible: "Display.*"
- takeScreenshot: search
```

## Supported commands

`launchApp`, `stopApp`, `killApp`, `clearState`, `clearKeychain` (no-op), `tapOn`, `doubleTapOn`, `longPressOn`, `inputText`, `inputRandomText/Number/Email/PersonName`, `eraseText`, `pressKey`, `hideKeyboard`, `back`, `scroll`, `scrollUntilVisible`, `swipe`, `openLink`, `assertVisible`, `assertNotVisible`, `extendedWaitUntil`, `waitForAnimationToEnd`, `takeScreenshot`, `repeat` (`times`/`while`), `retry`, `runFlow` (`file`/`commands`/`when`/`env`), plus `onFlowStart`/`onFlowComplete`, `env`, `tags` and `${VAR}` in the header.

## Known limitations

Inherited from AndroFlow (which also used plain ADB); they are not regressions:

- **Screen reading via `uiautomator dump`:** slower and less faithful than Maestro's driver. Screens with continuous animations may fail with "could not get idle state".
- **`inputText` only accepts ASCII:** a limit of `adb shell input text`. Use `--transliterate` to strip accents (`Cartão` → `Cartao`); emojis and other characters are not supported.

Differences from AndroFlow: the screen cache was removed, so flows with many consecutive assertions may be a bit slower. The `transliterate` option is no longer a YAML key (Maestro does not recognize it) and became the `--transliterate` flag.

## Development

```bash
uv run pytest
uv run ruff check
```
