# Pyestro

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: GPL v3+](https://img.shields.io/badge/License-GPLv3%2B-blue.svg)](LICENSE)
[![Maestro Compatible](https://img.shields.io/badge/Maestro-compatible-success.svg)](docs/maestro_compat.md)
![Platform: Android](https://img.shields.io/badge/platform-Android-3DDC84.svg)

A Python runner for YAML flows **100% compatible** with the [Maestro](https://docs.maestro.dev/) syntax, running on Android via ADB (no helper APKs or daemons). iOS is not supported for now.

> **Golden rule:** every `.yaml` file accepted by Pyestro is also accepted by Maestro, with the same meaning. Pyestro **does not invent commands or keys**. If something from Maestro does not exist here yet, Pyestro rejects the flow with a clear message instead of misinterpreting it. See [docs/maestro_compat.md](docs/maestro_compat.md).

Project derived from AndroFlow, without its custom syntax extensions (`waitFor`, `sleep`, `contentDesc`, `contains`, etc.), which Maestro does not recognize.

## Requirements

- **Python 3.12+**
- [uv](https://docs.astral.sh/uv/)
- **Android SDK Platform Tools (`adb`)** and an emulator or physical device with USB debugging enabled

## Installation

### Global CLI (Recommended)

You do not need to clone this repository or write Python code to test your apps. Pyestro works as a global command-line tool (similar to Maestro):

```bash
uv tool install git+https://github.com/carlosxfelipe/pyestro.git
```

> **PATH tip:** If the `pyestro` command is not recognized right after installation, run `uv tool update-shell` to configure your terminal.

To update Pyestro in the future with the latest changes from the repository:

```bash
uv tool upgrade pyestro
```

---

### Local development (Contributors)

To contribute to the Pyestro engine or run the library's unit tests:

```bash
git clone https://github.com/carlosxfelipe/pyestro.git
cd pyestro
```

```bash
uv sync
```

When running from a local clone, prefix commands with `uv run` (for example, `uv run pyestro devices`).

## Usage

### List connected devices

```bash
pyestro devices
```

### Validate flows (no device required)

```bash
pyestro validate flows/
```

### Run flows

```bash
# Single flow or an entire directory (.yaml / .yml)
pyestro test flows/settings_search.yaml
pyestro test flows/

# Target a specific device/emulator
pyestro --serial emulator-5554 test flows/

# Pass environment variables
pyestro test flows/ -e USER=ana -e PASS=123

# Filter by tags
pyestro test flows/ --include-tags smoke --exclude-tags wip

# Stop on first failure & disable Android animations during the run
pyestro test flows/ --fail-fast --disable-animations

# Record video and generate HTML + JUnit XML reports in artifacts/
pyestro test flows/settings_search.yaml --record --report
```

### Common CLI options

| Option | Description |
| --- | --- |
| `-s`, `--serial` | Target a specific ADB device serial (global option before `devices`/`test`) |
| `-e`, `--env` | Set environment variables as `KEY=VALUE` (repeatable) |
| `--include-tags` | Only run flows containing all listed tags (comma-separated) |
| `--exclude-tags` | Skip flows containing any listed tag (comma-separated) |
| `--fail-fast` | Stop the suite at the first failing flow |
| `--disable-animations` | Temporarily disable Android animations via ADB during the run |
| `-r`, `--record` | Record a video of the run and save it to `artifacts/` |
| `--report` | Generate HTML and JUnit XML reports in `artifacts/` |
| `--report-html` / `--report-junit` | Custom output paths for HTML or JUnit XML reports |
| `--transliterate` | Strip accents in `inputText` (since ADB only types ASCII) |

## Example flow

Identical to what you would write for Maestro (more examples in [`flows/`](flows/)):

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

- **App lifecycle & device:** `launchApp`, `stopApp`, `killApp`, `clearState`, `clearKeychain` (no-op), `setPermissions`, `openLink`, `setOrientation`, `setAirplaneMode`, `toggleAirplaneMode`, `setDarkMode`, `toggleDarkMode`, `addMedia`
- **Gestures & navigation:** `tapOn`, `doubleTapOn`, `longPressOn`, `scroll`, `scrollUntilVisible`, `swipe`, `back`, `pressKey`, `hideKeyboard`
- **Text input:** `inputText`, `inputRandomText`, `inputRandomNumber`, `inputRandomEmail`, `inputRandomPersonName`, `eraseText`
- **Assertions & waits:** `assertVisible`, `assertNotVisible`, `assertDarkMode`, `assertLightMode`, `extendedWaitUntil`, `waitForAnimationToEnd`
- **Flow control & subflows:** `repeat` (`times` / `while`), `retry`, `runFlow` (`file` / `commands` / `when` / `env`)
- **Media & header:** `takeScreenshot`, `startRecording`, `stopRecording`, `onFlowStart`, `onFlowComplete`, `env`, `tags`, and `${VAR}` interpolation

For full syntax and compatibility details, see [docs/maestro_compat.md](docs/maestro_compat.md).

## Known limitations

Inherited from AndroFlow (which also used plain ADB); these are not regressions:

- **Screen reading via `uiautomator dump`:** slower and less faithful than Maestro's driver. Screens with continuous animations may fail with `"could not get idle state"`.
- **`inputText` only accepts ASCII:** a limitation of `adb shell input text`. Use `--transliterate` to strip accents (`Cartão` → `Cartao`); emojis and other non-ASCII characters are not supported.

Differences from AndroFlow: the screen cache was removed, so flows with many consecutive assertions may be slightly slower. The `transliterate` option is no longer a YAML key (Maestro does not recognize it) and became the `--transliterate` CLI flag.

## Observability and CI/CD

Pyestro generates visual artifacts, screen recordings, and test reports in `artifacts/` by default:

### 1. Screen Recording (`--record` / `-r`, `startRecording`, `stopRecording`)

- **Full-flow recording via CLI:** records the entire flow run via `adb shell screenrecord` and saves `artifacts/<flow>.mp4`:
  ```bash
  pyestro test flows/settings_search.yaml --record
  ```
- **In-flow Maestro commands (`startRecording` / `stopRecording`):** record specific parts of a flow (relative paths are saved under `artifacts/` as `.mp4`):
  ```yaml
  - startRecording:
      path: recordings/onboarding
      label: Capture onboarding
      optional: true
  - tapOn: "Get Started"
  - stopRecording
  ```

> **Duration limit:** Android's native `screenrecord` caps continuous recordings at 180 seconds (3 minutes). Pyestro prints a warning if a recording exceeds this limit.

### 2. Test Reports (HTML and JUnit XML)

Generate both an interactive, self-contained HTML report (`artifacts/report.html`) and a JUnit XML report (`artifacts/report.xml`):

```bash
pyestro test flows/ --report
```

Or specify custom output paths:

```bash
pyestro test flows/ --report-junit test-results/junit.xml --report-html artifacts/index.html
```

- **JUnit XML (`--report-junit`):** Standard format for CI/CD integration (GitHub Actions, GitLab CI, etc.).
- **Interactive HTML Report (`--report-html`):** Self-contained report with summary metrics, per-step duration timeline, failure error details, embedded `.mp4` video player(s), and a screenshot gallery (from `takeScreenshot` and automatic failure captures `<flow>-failure-<step>.png` / `.xml`).

## Development

```bash
uv run pytest
uv run ruff format
uv run ruff check
uv run pyestro --help
```

## License

This project is licensed under the GNU General Public License v3.0 or later ([GPL-3.0-or-later](LICENSE)).
