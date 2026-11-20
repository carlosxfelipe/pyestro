# AGENTS.md

Guidance for coding agents and contributors working on **Pyestro**.

## Golden Rule

Every `.yaml` flow accepted by Pyestro **must** be accepted by [Maestro](https://docs.maestro.dev/) with the same meaning.

- **Never invent commands, keys, or syntax shortcuts** (e.g. `waitFor`, `sleep`, `contentDesc`, `contains`, `steps:`).
- Reject unknown commands and keys explicitly instead of silently ignoring or reinterpreting them.
- Maestro commands that are recognized but not yet implemented must fail with a dedicated, clear error message (see `docs/maestro_compat.md`).
- Keep `docs/maestro_compat.md` in sync whenever command support or compatibility behavior changes.

## Project Structure

- `src/pyestro/cli.py` — Typer CLI (`devices`, `validate`, `test`) and CLI flags (`--serial`, `--env`, `--transliterate`, `--record`, `--report`, etc.).
- `src/pyestro/flow.py` — YAML flow parser, header/command validation, and `_check_keys` helper.
- `src/pyestro/runner.py` — Flow execution engine (`FlowRunner`, `FlowResult`, subflows, retries, loops, hooks).
- `src/pyestro/selectors.py` — Maestro selector matching against `uiautomator` XML hierarchy.
- `src/pyestro/android.py` — ADB wrapper (`AndroidDriver`, `AndroidError`) with no helper APKs or daemons.
- `src/pyestro/reports.py` — HTML and JUnit XML report generation (`generate_html_report`, `generate_junit_xml`).
- `tests/` — Unit tests (`test_android.py`, `test_flow.py`, `test_runner.py`, `test_selectors.py`).
- `flows/` — Example Maestro-compatible YAML flows.

## Adding or Modifying Commands

1. Check the official [Maestro documentation](https://docs.maestro.dev/) for the exact command name, parameter keys, defaults, and semantics.
2. Validate allowed/unknown keys in `src/pyestro/flow.py` using `_check_keys`.
3. Implement execution in `src/pyestro/runner.py` (and `src/pyestro/android.py` / `src/pyestro/selectors.py` if needed).
4. Add unit tests covering valid syntax, invalid keys/types, and execution behavior in `tests/test_flow.py` and `tests/test_runner.py`.
5. Update `docs/maestro_compat.md` (and `README.md` if user-facing).

## Tooling & Checks

- **Python:** 3.12+ managed with `uv`.
- **Tests:** `uv run pytest` (or `.venv/bin/pytest` in restricted environments).
- **Linter:** `uv run ruff check` (or `.venv/bin/ruff check`). Rules configured in `pyproject.toml`: `E`, `F`, `I`, `B`, `UP`, `SIM`.

Always run both tests and linter after making code changes.
