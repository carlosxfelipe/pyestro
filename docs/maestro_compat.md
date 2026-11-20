# Maestro compatibility

## Principle

> Every `.yaml` that Pyestro accepts is also a valid `.yaml` in Maestro, with the same meaning.

To guarantee this, Pyestro:

1. **Rejects** commands and keys that do not exist in Maestro (AndroFlow had several: `waitFor`, `sleep`, `wakeUp`, `contentDesc`, `contains`, `regex`, the `steps:` format...).
2. **Rejects with a dedicated message** commands that exist in Maestro but are not implemented yet (`evalScript`, `assertTrue`, `copyTextFrom`...), so it never executes something differently from what is expected.
3. Uses Maestro's **semantics**, not just its syntax.

Use `pyestro validate flows/` to check the syntax without a device.

## Implemented semantics

| Topic | Behavior (same as Maestro) |
| --- | --- |
| `text` | Full-match, case-insensitive regex; matches `text`, `content-desc` or `hint`. An invalid regex becomes a literal comparison. |
| `id` | Regex against the `resource-id` (with or without `package:id/`). |
| Multiple candidates | The deepest one in the tree wins; `index` chooses among them. |
| Timings | Always in **milliseconds** (`timeout: 5000`). Units such as `5s` are refused. |
| `swipe: direction: UP` | The finger moves up (the content moves down). |
| `scroll` / `scrollUntilVisible: DOWN` | Scrolls the content down. |
| `launchApp` | `stopApp: true` and `permissions: {all: allow}` by default; `clearState`, per-group `permissions` (`notifications`, `location`, `camera`...), and `arguments` (passed as Android Intent extras via `am start`). |
| `${VAR}` | Variables from the `env` header, from `-e` and from `MAESTRO_*` in the shell (the CLI wins over the header). Supports `${VAR}`, `${VAR \|\| 'default'}` and `${VAR ?? 'default'}`; other JS expressions are refused. |
| `runFlow` | Path relative to the current file; the command's `env` applies to the subflow; `when` accepts `visible`, `notVisible`, `platform`, `true`. |
| Header | `appId`, `name`, `tags`, `env`, `onFlowStart`, `onFlowComplete`; other Maestro keys are accepted and ignored. |

## Commands

Supported: `launchApp`, `stopApp`, `killApp`, `clearState`, `clearKeychain` (no-op on Android), `tapOn`, `doubleTapOn`, `longPressOn`, `inputText`, `inputRandomText`, `inputRandomNumber`, `inputRandomEmail`, `inputRandomPersonName`, `eraseText`, `pressKey`, `hideKeyboard`, `back`, `scroll`, `scrollUntilVisible`, `swipe`, `openLink`, `assertVisible`, `assertNotVisible`, `extendedWaitUntil`, `waitForAnimationToEnd`, `takeScreenshot`, `startRecording`, `stopRecording`, `repeat`, `retry`, `runFlow`.

Recognized but **not yet supported** (explicit error): `assertTrue`, `evalScript`, `runScript`, `copyTextFrom`, `pasteText`, `setLocation`, `setOrientation`, `setAirplaneMode`, `toggleAirplaneMode`, `travel`, `addMedia`, `openBrowser`, `setPermissions`, `assertScreenshot` and the AI commands.

## Known differences (do not affect syntax)

- `takeScreenshot` and `startRecording` save to `artifacts/` (use an absolute `path` for another location). `startRecording`/`stopRecording` use `adb shell screenrecord` (subject to Android's 180s per-recording limit).
- Screen reading uses `uiautomator dump`, which is slower and less faithful than Maestro's driver; `waitToSettleTimeoutMs`, `speed` and `visibilityPercentage` are validated but ignored.
- `inputText` only types ASCII via ADB; use `--transliterate` to strip accents.
- `retryTapIfNoChange` defaults to `false`.
- Accepted without effect on Android via ADB: `launchApp: clearKeychain` (iOS-only) and `openLink: autoVerify`.
- `optional: true` skips both assertion failures and ADB command errors for that step.
- `runFlow` nesting is capped at 20 levels to fail cleanly on circular subflows.

## How to keep compatibility

When adding a command: copy the name and keys exactly from the [Maestro documentation](https://docs.maestro.dev/), validate unknown keys with `_check_keys` and add the case to `tests/test_flow.py`. Never create your own syntax shortcuts.

> [!NOTE]
> The tests check the syntax against the documentation, but have **not** been compared with a real Maestro binary. Before a release, run a few flows with `maestro test` to verify.
