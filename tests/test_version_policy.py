from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# The version bump used to be an inline script inside SKILL.md, which meant the
# model had to read it into context to run it. It now lives in release.py, so
# these tests exercise the real production code path instead of a doc excerpt.
RELEASE = ROOT / "scripts" / "release.py"


def run_version_bump(project_dir: Path, contract: str) -> str:
    state_dir = project_dir / ".sprintfoundry"
    eval_dir = state_dir / "eval-results"
    eval_dir.mkdir(parents=True)
    (project_dir / "VERSION").write_text("1.1.7\n", encoding="utf-8")
    (project_dir / "sprint-contract.md").write_text(contract, encoding="utf-8")
    (eval_dir / "eval-result-2.md").write_text(
        "# Eval Result - Sprint 2\n\nSPRINT PASS\n",
        encoding="utf-8",
    )
    (state_dir / "run-state.json").write_text(
        json.dumps(
            {
                "current_sprint": 2,
                "current_version": "1.1.7",
                "sprint_origin": "feature",
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["python3", "-c",
         f"import runpy,sys; sys.argv=['release.py']; "
         f"m=runpy.run_path({str(RELEASE)!r}); print(m['bump_version'](2)[1])"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_contract_title_without_breaking_tests_does_not_force_major(tmp_path: Path) -> None:
    output = run_version_bump(
        tmp_path,
        "## Sprint 2: Without Breaking Tests\n\n"
        "### Features\n"
        "- Keep the existing test suite passing while updating protocol wording.\n",
    )

    assert (tmp_path / "VERSION").read_text(encoding="utf-8").strip() == "1.2.0"
    assert "(minor)" in output


def test_explicit_breaking_change_declaration_forces_major(tmp_path: Path) -> None:
    output = run_version_bump(
        tmp_path,
        "## Sprint 2: Replace legacy API\n\n"
        "Breaking changes: yes\n\n"
        "### Features\n"
        "- Remove the old public endpoint.\n",
    )

    assert (tmp_path / "VERSION").read_text(encoding="utf-8").strip() == "2.0.0"
    assert "(major)" in output
