from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts import quality_gate


def sample_clone(first: str, second: str) -> dict:
    return {
        "format": "python",
        "lines": 12,
        "tokens": 84,
        "first": {"path": first, "start": 10, "end": 21},
        "second": {"path": second, "start": 40, "end": 51},
    }


def test_duplication_config_defaults_and_env_overrides() -> None:
    constitution = """
duplication_gate: fail
duplication_min_lines: 14
duplication_min_tokens: 90
duplication_max_new_clones: 1
"""

    configured = quality_gate.duplication_config(constitution, {})
    overridden = quality_gate.duplication_config(
        constitution,
        {
            "SPRINTFOUNDRY_DUPLICATION_GATE": "warn",
            "SPRINTFOUNDRY_DUPLICATION_MIN_LINES": "20",
        },
    )

    assert configured == {
        "mode": "fail",
        "min_lines": 14,
        "min_tokens": 90,
        "max_new_clones": 1,
    }
    assert overridden["mode"] == "warn"
    assert overridden["min_lines"] == 20
    assert overridden["min_tokens"] == 90


def test_load_jscpd_clones_normalizes_v4_and_v5_locations(tmp_path: Path) -> None:
    report = tmp_path / "jscpd-report.json"
    report.write_text(json.dumps({
        "duplicates": [
            {
                "format": "python",
                "lines": 12,
                "tokens": 84,
                "firstFile": {"name": "src/a.py", "start": 10, "end": 21},
                "secondFile": {
                    "path": "src/b.py",
                    "startLoc": {"line": 40},
                    "endLoc": {"line": 51},
                },
            }
        ]
    }))

    clones = quality_gate.load_jscpd_clones(report)

    assert clones == [sample_clone("src/a.py", "src/b.py")]


def test_only_clones_intersecting_changed_lines_are_candidates(tmp_path: Path) -> None:
    clones = [
        sample_clone("src/a.py", "src/shared.py"),
        sample_clone("src/old.py", "src/legacy.py"),
    ]

    candidates = quality_gate.clones_touching_changes(
        clones,
        {"src/a.py": [(15, 16)]},
        tmp_path,
    )

    assert candidates == [clones[0]]


def test_changed_line_ranges_parses_only_added_and_modified_lines(monkeypatch) -> None:
    diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -2,2 +2,3 @@
 unchanged
+added
@@ -20,1 +21,0 @@
-deleted
"""
    monkeypatch.setattr(
        quality_gate.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=diff),
    )

    ranges = quality_gate.changed_line_ranges("base..HEAD", ["src/a.py"])

    assert ranges == {"src/a.py": [(2, 4)]}


def test_duplication_warn_reports_but_fail_mode_blocks() -> None:
    candidates = [sample_clone("src/a.py", "src/shared.py")]

    warning = quality_gate.duplication_result(
        candidates,
        {"mode": "warn", "max_new_clones": 0},
    )
    failure = quality_gate.duplication_result(
        candidates,
        {"mode": "fail", "max_new_clones": 0},
    )
    allowed = quality_gate.duplication_result(
        candidates,
        {"mode": "fail", "max_new_clones": 1},
    )

    assert warning["passed"] is True
    assert warning["status"] == "warn"
    assert failure["passed"] is False
    assert failure["status"] == "fail"
    assert allowed["passed"] is True
