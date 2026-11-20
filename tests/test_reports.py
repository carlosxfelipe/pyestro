from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from pyestro.flow import interpolate_string
from pyestro.reports import generate_html_report, generate_junit_xml
from pyestro.runner import FlowResult, StepResult


def _result_with_failing_hook() -> FlowResult:
    return FlowResult(
        path=Path("a.yaml"),
        total=2,
        steps=[
            StepResult(1, "tapOn", True),
            StepResult(2, "back", True),
            StepResult(3, "onFlowComplete", False, "boom", hook=True),
        ],
    )


def test_failing_hook_does_not_produce_negative_skipped():
    result = _result_with_failing_hook()
    assert result.skipped == 0
    assert result.failed == 1
    assert not result.ok


def test_hook_failure_after_command_failure_keeps_skipped_count():
    result = FlowResult(
        path=Path("a.yaml"),
        total=4,
        steps=[
            StepResult(1, "tapOn", True),
            StepResult(2, "back", False, "x"),
            StepResult(3, "onFlowComplete", False, "boom", hook=True),
        ],
    )
    assert result.skipped == 2


def test_junit_counts_with_failing_hook(tmp_path):
    out = generate_junit_xml(_result_with_failing_hook(), tmp_path / "r.xml")
    suite = ET.parse(out).getroot().find("testsuite")
    assert suite is not None
    assert suite.get("tests") == "3"
    assert suite.get("skipped") == "0"
    assert len(suite.findall("testcase")) == 3


def test_html_media_links_are_relative_to_the_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    video = Path("artifacts/a.mp4")
    shot = Path("artifacts/a.png")
    video.parent.mkdir()
    video.write_bytes(b"x")
    shot.write_bytes(b"x")
    result = FlowResult(
        path=Path("a.yaml"),
        total=1,
        steps=[StepResult(1, "tapOn", True)],
        video_path=video,
        artifacts=[video, shot],
    )
    report = generate_html_report(result, Path("reports/index.html"))
    content = report.read_text(encoding="utf-8")
    assert re.search(r'<source src="\.\./artifacts/a\.mp4"', content)
    assert 'src="../artifacts/a.png"' in content


def test_quoted_literal_may_contain_operators():
    assert interpolate_string("${'a||b'}", {}) == "a||b"
    assert interpolate_string("${'a??b'}", {}) == "a??b"
