from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from .android import AndroidDriver, AndroidError
from .flow import FlowError
from .reports import generate_html_report, generate_junit_xml
from .runner import FlowResult, FlowRunner, peek_config

app = typer.Typer(
    name="pyestro",
    help="Run Maestro-format YAML flows on an Android emulator via ADB.",
    no_args_is_help=True,
)


@app.callback()
def main(
    ctx: typer.Context,
    serial: Annotated[
        str | None,
        typer.Option("--serial", "-s", help="ADB serial of the emulator/device."),
    ] = None,
) -> None:
    ctx.obj = {"driver": AndroidDriver(serial=serial)}


@app.command()
def devices(ctx: typer.Context) -> None:
    """List the devices available in ADB."""
    driver: AndroidDriver = ctx.obj["driver"]
    try:
        rows = driver.devices()
    except AndroidError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not rows:
        typer.echo("No device found. Start the emulator in Android Studio.")
        return
    typer.echo("SERIAL\t\tSTATE")
    for serial, state in rows:
        typer.echo(f"{serial}\t{state}")


@app.command("validate")
def validate_flows(
    flows: Annotated[
        list[Path],
        typer.Argument(
            help="YAML file(s) or directory to validate (no device needed)."
        ),
    ],
    env: Annotated[
        list[str] | None,
        typer.Option("--env", "-e", help="KEY=VALUE variable (repeatable)."),
    ] = None,
) -> None:
    """Validate the syntax of flows (Maestro format) without running them."""
    runner = FlowRunner(AndroidDriver(), env=_parse_env(env), echo=typer.echo)
    files = _collect_files(flows)
    if not files:
        typer.echo("No flow file (.yaml/.yml) found.", err=True)
        raise typer.Exit(1)
    failed = 0
    for file in files:
        try:
            runner.load_flow(file)
        except FlowError as exc:
            failed += 1
            typer.echo(f"ERROR {file}: {exc}", err=True)
        else:
            typer.echo(f"OK    {file}")
    raise typer.Exit(1 if failed else 0)


@app.command("test")
def test_flow(
    ctx: typer.Context,
    flows: Annotated[
        list[Path],
        typer.Argument(help="Path to YAML file(s) or directory."),
    ],
    disable_animations: Annotated[
        bool,
        typer.Option(
            "--disable-animations",
            help="Disable Android animations via ADB before running.",
        ),
    ] = False,
    record: Annotated[
        bool,
        typer.Option(
            "--record",
            "-r",
            help="Record a video of the flow run and save it to artifacts/.",
        ),
    ] = False,
    report_junit: Annotated[
        Path | None,
        typer.Option(
            "--report-junit",
            help="Path to write the JUnit XML report for CI/CD.",
        ),
    ] = None,
    report_html: Annotated[
        Path | None,
        typer.Option(
            "--report-html",
            help="Path to write the visual, interactive HTML report.",
        ),
    ] = None,
    report: Annotated[
        bool,
        typer.Option(
            "--report",
            help="Generate HTML and JUnit XML reports in artifacts/.",
        ),
    ] = False,
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            "-e",
            help="Environment variable as KEY=VALUE (repeatable).",
        ),
    ] = None,
    fail_fast: Annotated[
        bool,
        typer.Option(
            "--fail-fast",
            help="Stop the suite at the first failing flow.",
        ),
    ] = False,
    include_tags: Annotated[
        str | None,
        typer.Option(
            "--include-tags",
            help="Only run flows with ALL of these tags (comma-separated).",
        ),
    ] = None,
    exclude_tags: Annotated[
        str | None,
        typer.Option(
            "--exclude-tags",
            help="Skip flows with ANY of these tags (comma-separated).",
        ),
    ] = None,
    transliterate: Annotated[
        bool,
        typer.Option(
            "--transliterate",
            help="Strip accents in inputText (ADB only types ASCII).",
        ),
    ] = False,
    no_screen_cache: Annotated[
        bool,
        typer.Option(
            "--no-screen-cache",
            help="Disable the 1s hierarchy cache between consecutive assertions.",
        ),
    ] = False,
) -> None:
    """Run one or more YAML test flows."""
    driver: AndroidDriver = ctx.obj["driver"]
    saved_animations: dict[str, str] | None = None
    if disable_animations:
        typer.echo("Disabling Android animations via ADB...")
        try:
            saved_animations = driver.get_animation_scales()
            driver.disable_animations()
        except AndroidError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(2) from exc

    try:
        _run_suite(
            driver,
            flows,
            record=record,
            report=report,
            report_junit=report_junit,
            report_html=report_html,
            env=env,
            fail_fast=fail_fast,
            include_tags=_split_tags(include_tags),
            exclude_tags=_split_tags(exclude_tags),
            transliterate=transliterate,
            no_screen_cache=no_screen_cache,
        )
    finally:
        if saved_animations is not None:
            typer.echo("Restoring Android animations...")
            try:
                driver.restore_animation_scales(saved_animations)
            except AndroidError as exc:
                typer.echo(f"Warning: could not restore animations: {exc}")


def _parse_env(env: list[str] | None) -> dict[str, str]:
    cli_env: dict[str, str] = {}
    for item in env or []:
        if "=" in item:
            k, v = item.split("=", 1)
            cli_env[k.strip()] = v.strip()
        else:
            cli_env[item.strip()] = os.environ.get(item.strip(), "")
    return cli_env


def _collect_files(flows: list[Path]) -> list[Path]:
    files: list[Path] = []
    for item in flows:
        if item.is_dir():
            files.extend(sorted(item.glob("*.yaml")))
            files.extend(sorted(item.glob("*.yml")))
        else:
            files.append(item)
    return files


def _split_tags(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [t.strip() for t in value.split(",") if t.strip()]


def _filter_by_tags(
    files: list[Path], include: list[str] | None, exclude: list[str] | None
) -> list[Path]:
    if not include and not exclude:
        return files
    selected: list[Path] = []
    for file in files:
        try:
            tags = set(peek_config(file).tags)
        except FlowError:
            selected.append(file)  # let the error surface at run time
            continue
        if include and not set(include) <= tags:
            continue
        if exclude and tags & set(exclude):
            continue
        selected.append(file)
    return selected


def _run_suite(
    driver: AndroidDriver,
    flows: list[Path],
    *,
    record: bool,
    report: bool,
    report_junit: Path | None,
    report_html: Path | None,
    env: list[str] | None,
    fail_fast: bool,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    transliterate: bool = False,
    no_screen_cache: bool = False,
) -> None:
    if report:
        report_html = report_html or Path("artifacts/report.html")
        report_junit = report_junit or Path("artifacts/report.xml")

    cli_env = _parse_env(env)
    target_files = _collect_files(flows)

    if not target_files:
        typer.echo("No flow file (.yaml/.yml) found.", err=True)
        raise typer.Exit(1)

    target_files = _filter_by_tags(target_files, include_tags, exclude_tags)
    if not target_files:
        typer.echo("No flow matches the tag filter.", err=True)
        raise typer.Exit(1)

    runner = FlowRunner(
        driver,
        env=cli_env,
        echo=typer.echo,
        transliterate=transliterate,
        screen_cache_ttl=0.0 if no_screen_cache else 1.0,
    )
    results = []
    has_failure = False

    for flow in target_files:
        try:
            result = runner.run(flow, record=record)
        except FlowError as exc:
            # Invalid flow (YAML/validation): record it and move on to the next.
            typer.echo(f"Error running {flow}: {exc}", err=True)
            results.append(FlowResult.load_failure(flow, str(exc)))
            has_failure = True
            if fail_fast:
                break
            continue
        except AndroidError as exc:
            # Device/ADB error: continuing would not help.
            typer.echo(f"Error running {flow}: {exc}", err=True)
            results.append(FlowResult.load_failure(flow, str(exc)))
            has_failure = True
            break
        results.append(result)
        if not result.ok:
            has_failure = True
            if fail_fast:
                break

    if results:
        if report_junit:
            generate_junit_xml(results, report_junit)
            typer.echo(f"JUnit XML report written to: {report_junit}")
        if report_html:
            generate_html_report(results, report_html)
            typer.echo(f"Interactive HTML report written to: {report_html}")

    if len(target_files) > 1:
        passed_count = sum(1 for r in results if r.ok)
        total_count = len(target_files)
        typer.echo(f"\nSuite summary: {passed_count}/{total_count} flow(s) passed.")

    raise typer.Exit(1 if has_failure else 0)


if __name__ == "__main__":
    app()
