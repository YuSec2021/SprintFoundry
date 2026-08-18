#!/usr/bin/env python3
"""SprintFoundry quality gate — static checks run between the Orchestrator's
commit and the Evaluator's black-box CHECK.

Invoked by orchestrate.py (never by an agent reading this file into context):

    python3 scripts/quality_gate.py --project-dir <root> [--sprint N]

Writes .sprintfoundry/results/quality/quality-gate-{N}.md and exits 0 on PASS,
1 on FAIL. Behaviour is documented in
plugins/sprintfoundry/skills/sf-orchestrator/references/quality-gate.md —
that document describes the gate; this file *is* the gate.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess


DUPLICATION_DEFAULTS = {
    "mode": "warn",
    "min_lines": 10,
    "min_tokens": 70,
    "max_new_clones": 0,
}


def duplication_config(constitution: str, environ=None) -> dict:
    """Read duplication policy from SPRINTFOUNDRY.md with env overrides."""
    env = os.environ if environ is None else environ

    def text_value(key: str, default: str) -> str:
        match = re.search(rf"(?mi)^\s*{re.escape(key)}\s*:\s*([^#\n]+)", constitution)
        return match.group(1).strip() if match else default

    def int_value(env_key: str, file_key: str, default: int) -> int:
        raw = env.get(env_key) or text_value(file_key, str(default))
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return default

    mode = str(
        env.get("SPRINTFOUNDRY_DUPLICATION_GATE")
        or text_value("duplication_gate", DUPLICATION_DEFAULTS["mode"])
    ).strip().lower()
    if mode not in {"off", "warn", "fail"}:
        mode = DUPLICATION_DEFAULTS["mode"]

    return {
        "mode": mode,
        "min_lines": int_value(
            "SPRINTFOUNDRY_DUPLICATION_MIN_LINES",
            "duplication_min_lines",
            DUPLICATION_DEFAULTS["min_lines"],
        ),
        "min_tokens": int_value(
            "SPRINTFOUNDRY_DUPLICATION_MIN_TOKENS",
            "duplication_min_tokens",
            DUPLICATION_DEFAULTS["min_tokens"],
        ),
        "max_new_clones": int_value(
            "SPRINTFOUNDRY_DUPLICATION_MAX_NEW_CLONES",
            "duplication_max_new_clones",
            DUPLICATION_DEFAULTS["max_new_clones"],
        ),
    }


def _location_line(location: dict, key: str) -> int:
    value = location.get(key)
    if isinstance(value, dict):
        value = value.get("line")
    if value is None:
        value = location.get(f"{key}Loc", {}).get("line")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def load_jscpd_clones(report_path: pathlib.Path) -> list:
    """Normalize the v4/v5 JSON clone shapes used by jscpd reporters."""
    data = json.loads(report_path.read_text(errors="ignore"))
    raw_clones = data.get("duplicates") or data.get("clones") or []
    clones = []
    for raw in raw_clones:
        first = raw.get("firstFile") or raw.get("first") or {}
        second = raw.get("secondFile") or raw.get("second") or {}

        def normalize(location: dict) -> dict:
            return {
                "path": str(location.get("name") or location.get("path") or ""),
                "start": _location_line(location, "start"),
                "end": _location_line(location, "end"),
            }

        clones.append({
            "format": str(raw.get("format") or "unknown"),
            "lines": int(raw.get("lines") or 0),
            "tokens": int(raw.get("tokens") or 0),
            "first": normalize(first),
            "second": normalize(second),
        })
    return clones


def changed_line_ranges(diff_ref: str, paths: list) -> dict:
    """Return added/modified line ranges for each changed source file."""
    if not paths:
        return {}
    if diff_ref == "ROOT":
        cmd = ["git", "show", "--format=", "--unified=0", "HEAD", "--", *paths]
    else:
        cmd = ["git", "diff", "--unified=0", "--no-color", diff_ref, "--", *paths]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return {}

    ranges = {}
    current = ""
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            ranges.setdefault(current, [])
            continue
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if current and match:
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            if count:
                ranges[current].append((start, start + count - 1))
    return ranges


def clones_touching_changes(clones: list, ranges: dict, project_root: pathlib.Path) -> list:
    """Keep clone pairs whose reported span intersects this sprint's changed lines."""
    root = project_root.resolve()

    def relative(name: str) -> str:
        path = pathlib.Path(name)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(root)
            except ValueError:
                return ""
        value = path.as_posix()
        return value[2:] if value.startswith("./") else value

    def intersects(location: dict) -> bool:
        path = relative(location["path"])
        start, end = location["start"], location["end"]
        if not path or not start or not end:
            return False
        return any(start <= changed_end and end >= changed_start
                   for changed_start, changed_end in ranges.get(path, []))

    return [clone for clone in clones
            if intersects(clone["first"]) or intersects(clone["second"])]


def duplication_result(candidates: list, policy: dict) -> dict:
    """Turn changed-line clone candidates into a quality-gate result."""
    mode = policy["mode"]
    allowed = policy["max_new_clones"]
    passed = mode != "fail" or len(candidates) <= allowed
    status = "pass" if not candidates or (mode == "fail" and passed) \
        else ("warn" if mode == "warn" else "fail")
    lines = [
        f"Mode: {mode}; changed-line clone candidates: {len(candidates)}; "
        f"allowed in fail mode: {allowed}.",
        "Candidates are evidence for Evaluator reuse review, not proof of a defect.",
    ]
    for clone in candidates[:10]:
        first, second = clone["first"], clone["second"]
        lines.append(
            f"- {first['path']}:{first['start']}-{first['end']} <-> "
            f"{second['path']}:{second['start']}-{second['end']} "
            f"({clone['lines']} lines, {clone['tokens']} tokens)"
        )
    return {"passed": passed, "status": status, "output": "\n".join(lines)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--sprint", type=int, default=None,
                        help="Sprint number; defaults to the eval-trigger value.")
    args = parser.parse_args()
    os.chdir(args.project_dir)
    return run_gate(args.sprint)


def run_gate(sprint_override: "int | None" = None) -> int:

    spec = json.loads(pathlib.Path("planner-spec.json").read_text()) \
           if pathlib.Path("planner-spec.json").exists() else {}
    stack = spec.get("tech_stack", {})
    frontend = stack.get("frontend", "").lower()
    backend  = stack.get("backend",  "").lower()

    results = {}   # tool -> {"passed": bool, "output": str}

    # Directories that must never be scanned by any linter.
    SKIP_DIRS = (".git", "node_modules", ".venv", "venv", "dist", "build",
                 "__pycache__", ".sprintfoundry", ".next", "coverage")

    def run(cmd, **kwargs):
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)
        return r.returncode, (r.stdout + r.stderr).strip()

    def has_files(*exts):
        """True if the project contains at least one file with any of these
        extensions, ignoring vendored/build directories."""
        for ext in exts:
            for p in pathlib.Path(".").rglob(f"*{ext}"):
                if not any(part in SKIP_DIRS for part in p.parts):
                    return True
        return False

    trigger_path = pathlib.Path(".sprintfoundry/signals/eval-trigger.txt")
    if not trigger_path.exists():
        trigger_path = pathlib.Path("eval-trigger.txt")  # legacy compatibility

    sprint_n = str(sprint_override) if sprint_override is not None else "?"
    if sprint_override is None and trigger_path.exists():
        m = re.search(r"sprint=(\d+)", trigger_path.read_text())
        sprint_n = m.group(1) if m else "?"

    # ── JavaScript / TypeScript ──────────────────────────────────────────────────
    if any(x in frontend for x in ["react", "next", "vue", "node", "express"]) or \
       any(x in backend  for x in ["node", "express", "fastify", "nest"]):
        rc, out = run("npx eslint . --ext .js,.jsx,.ts,.tsx --max-warnings=0 2>&1 | tail -20")
        results["eslint"] = {"passed": rc == 0, "output": out}

        rc, out = run("npx tsc --noEmit 2>&1 | tail -30")
        results["tsc"] = {"passed": rc == 0, "output": out}

        rc, out = run("npx jest --coverage --coverageThreshold='{\"global\":{\"lines\":70}}' 2>&1 | tail -30")
        results["jest-coverage"] = {"passed": rc == 0, "output": out}

        rc, out = run("npm audit --audit-level=high 2>&1 | tail -20")
        results["npm-audit"] = {"passed": rc == 0, "output": out}

    # ── Python ───────────────────────────────────────────────────────────────────
    if any(x in backend for x in ["python", "fastapi", "flask", "django", "poetry"]):
        def detect_python_version():
            if os.environ.get("SPRINTFOUNDRY_PYTHON_VERSION"):
                raw = os.environ["SPRINTFOUNDRY_PYTHON_VERSION"]
            elif pathlib.Path(".python-version").exists():
                raw = pathlib.Path(".python-version").read_text().splitlines()[0]
            elif pathlib.Path("runtime.txt").exists():
                raw = pathlib.Path("runtime.txt").read_text().splitlines()[0]
            elif pathlib.Path("pyproject.toml").exists():
                match = re.search(
                    r"(?m)^\s*requires-python\s*=\s*[\"']([^\"']+)[\"']",
                    pathlib.Path("pyproject.toml").read_text(errors="ignore"),
                )
                raw = match.group(1) if match else ""
            else:
                probe = subprocess.run(
                    "python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")'",
                    shell=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                raw = probe.stdout.strip()

            match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)(?!\d)", raw)
            return match.group(1) if match else "3.9"

        py = detect_python_version()
        uv_prefix = f"uv run --python {py}"
        results["python-env"] = {"passed": True, "output": f"Using uv-managed Python {py}"}

        rc, out = run(f"{uv_prefix} --with flake8 flake8 . --max-line-length=100 --exclude=.git,__pycache__,venv,.venv 2>&1 | tail -30")
        results["flake8"] = {"passed": rc == 0, "output": out}

        rc, out = run(f"{uv_prefix} --with mypy mypy . --ignore-missing-imports --no-error-summary 2>&1 | tail -30")
        results["mypy"] = {"passed": rc == 0, "output": out}

        rc, out = run(f"{uv_prefix} --with pytest --with pytest-cov pytest --cov=. --cov-fail-under=70 -q 2>&1 | tail -20")
        results["pytest-coverage"] = {"passed": rc == 0, "output": out}

        rc, out = run(f"{uv_prefix} --with pip-audit pip-audit --desc 2>&1 | tail -20")
        results["pip-audit"] = {"passed": rc == 0, "output": out}

    # ── Frontend assets: HTML / CSS / vanilla JS ─────────────────────────────────
    # These run by *file presence*, not tech_stack keyword, so plain static sites
    # (no framework) are still covered. Tools are fetched on demand via `npx --yes`.

    # HTML — htmlhint ships sane default rules, so it needs no project config.
    if has_files(".html", ".htm"):
        rc, out = run(
            'npx --yes htmlhint "**/*.html" "**/*.htm" '
            '--ignore "node_modules/**" --ignore "dist/**" --ignore "build/**" '
            '2>&1 | tail -20'
        )
        results["htmlhint"] = {"passed": rc == 0, "output": out}

    # CSS — stylelint requires a config. Use the project's if present; otherwise
    # write a self-contained ruleset (no `extends`, so no extra packages needed)
    # under .sprintfoundry/ to avoid polluting the project root.
    if has_files(".css"):
        project_cfgs = [".stylelintrc", ".stylelintrc.json", ".stylelintrc.js",
                        ".stylelintrc.cjs", ".stylelintrc.yaml", ".stylelintrc.yml",
                        "stylelint.config.js", "stylelint.config.cjs"]
        cfg_flag = ""
        if not any(pathlib.Path(c).exists() for c in project_cfgs):
            cfg = pathlib.Path(".sprintfoundry/results/quality/.stylelintrc.json")
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(json.dumps({
                "rules": {
                    "color-no-invalid-hex": True,
                    "block-no-empty": True,
                    "no-duplicate-selectors": True,
                    "no-invalid-double-slash-comments": True,
                    "property-no-unknown": True,
                    "unit-no-unknown": True,
                    "declaration-block-no-duplicate-properties": True,
                    "declaration-block-no-shorthand-property-overrides": True
                }
            }))
            cfg_flag = f'--config "{cfg}"'
        rc, out = run(
            f'npx --yes stylelint "**/*.css" {cfg_flag} '
            '--ignore-pattern "node_modules/**" --ignore-pattern "dist/**" '
            '--ignore-pattern "build/**" 2>&1 | tail -20'
        )
        results["stylelint"] = {"passed": rc == 0, "output": out}

    # Vanilla JS — only when the framework branch above did NOT already lint JS.
    # Relies on the project's ESLint config (Generator establishes one in Sprint 1);
    # if absent, ESLint reports it and the gate fails loudly rather than silently
    # skipping JS quality.
    if has_files(".js", ".mjs", ".cjs") and "eslint" not in results:
        rc, out = run(
            "npx --yes eslint . --ext .js,.mjs,.cjs --max-warnings=0 2>&1 | tail -20"
        )
        results["eslint-js"] = {"passed": rc == 0, "output": out}

    # ── Test-presence gate (MANDATORY for every sprint / every code change) ──────
    # Every sprint update must ship a corresponding automated test script — this is
    # a hard requirement on top of static analysis and the Evaluator's code review.
    # Compare the sprint's diff against its base: if application source code changed
    # but no test file was added or modified, FAIL. Pure docs / config / markup
    # changes are exempt (nothing behavioural to test; they have their own lint
    # gates). This runs for EVERY stack, including ones not otherwise recognised.
    def sprint_diff_context():
        def sh(cmd):
            code, out = run(cmd)
            return out.strip() if code == 0 else ""

        head = sh("git rev-parse HEAD 2>/dev/null")
        base = ""
        fence = pathlib.Path(".sprintfoundry/state/sprint-fence.json")
        if fence.exists():
            try:
                base = json.loads(fence.read_text()).get("base_commit", "") or ""
            except Exception:
                base = ""
        if not base:
            base_branch = "main"
            rs = pathlib.Path(".sprintfoundry/state/run-state.json")
            if rs.exists():
                try:
                    base_branch = json.loads(rs.read_text()).get("base_branch", "main") or "main"
                except Exception:
                    base_branch = "main"
            for cand in (base_branch, "main", "master"):
                mb = sh(f"git merge-base HEAD {cand} 2>/dev/null")
                # Ignore a base that resolves to HEAD itself (we are on/behind the
                # base branch) — that would yield an empty diff and hide the change.
                if mb and mb != head:
                    base = mb
                    break
        if base and base != head:
            diff_ref = f"{base}..HEAD"
            _, out = run(f"git diff --name-only {diff_ref}")
        else:
            parent = sh("git rev-parse --verify --quiet HEAD~1 2>/dev/null")
            if parent:  # no distinct base recorded — compare against the parent commit
                diff_ref = "HEAD~1..HEAD"
                _, out = run(f"git diff --name-only {diff_ref}")
            else:  # parentless root commit — --root lists its files as all-added
                diff_ref = "ROOT"
                _, out = run("git diff-tree --root --no-commit-id --name-only -r HEAD")
        return [f for f in out.splitlines() if f.strip()], diff_ref

    def is_test_file(path):
        name = path.lower().rsplit("/", 1)[-1]
        if any(seg in f"/{path.lower()}" for seg in
               ("/tests/", "/test/", "/__tests__/", "/spec/", "/e2e/", "/testing/")):
            return True
        return (
            name.startswith("test_") or name.endswith("_test.py") or name.endswith("_test.go")
            or ".test." in name or ".spec." in name
            or name.endswith(("test.js", "spec.js", "test.ts", "spec.ts", "test.tsx", "spec.tsx"))
        )

    CODE_EXTS = (
        ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
        ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".kt", ".swift",
        ".scala", ".vue", ".svelte", ".dart",
    )
    sprint_files, diff_ref = sprint_diff_context()
    changed_paths = [
        f for f in sprint_files
        if not any(part in SKIP_DIRS for part in pathlib.Path(f).parts)
    ]
    code_changed = [
        f for f in changed_paths
        if pathlib.Path(f).suffix.lower() in CODE_EXTS and not is_test_file(f)
    ]
    test_changed = [f for f in changed_paths if is_test_file(f)]
    if code_changed:
        if test_changed:
            results["test-presence"] = {
                "passed": True,
                "output": "Code changes ship tests:\n  " + "\n  ".join(test_changed[:20]),
            }
        else:
            results["test-presence"] = {
                "passed": False,
                "output": (
                    "No test script accompanies the code changes. Every sprint update "
                    "must ship a corresponding automated test.\n"
                    "Add/extend tests for these changed source files:\n  "
                    + "\n  ".join(code_changed[:30])
                ),
            }

    # ── Duplication evidence (jscpd + sprint changed-line filtering) ─────────────
    # A whole-repository percentage would punish a new sprint for historical debt.
    # Scan the repository, then retain only clone spans that overlap lines changed
    # by this sprint. WARN is the default; projects can opt into a hard gate in
    # SPRINTFOUNDRY.md after calibrating generated/fixture exclusions.
    constitution = pathlib.Path("SPRINTFOUNDRY.md").read_text(errors="ignore") \
        if pathlib.Path("SPRINTFOUNDRY.md").exists() else ""
    duplicate_policy = duplication_config(constitution)
    if code_changed:
        mode = duplicate_policy["mode"]
        if mode == "off":
            results["duplication"] = {
                "passed": True,
                "status": "skip",
                "output": "Disabled by duplication_gate: off.",
            }
        else:
            binary = "jscpd" if shutil.which("jscpd") else ""
            if not binary and shutil.which("npx"):
                binary = "npx --yes jscpd@5"

            if not binary:
                results["duplication"] = {
                    "passed": mode != "fail",
                    "status": "skip" if mode == "warn" else "fail",
                    "output": (
                        "jscpd is unavailable (no jscpd or npx on PATH). "
                        f"Configured mode: {mode}."
                    ),
                }
            else:
                report_dir = pathlib.Path(".sprintfoundry/results/quality") / f"jscpd-{sprint_n}"
                if report_dir.exists():
                    shutil.rmtree(report_dir)
                ignore = ",".join([
                    "**/.git/**", "**/node_modules/**", "**/.venv/**", "**/venv/**",
                    "**/dist/**", "**/build/**", "**/.sprintfoundry/**", "**/coverage/**",
                    "**/vendor/**", "**/generated/**", "**/fixtures/**", "**/snapshots/**",
                    "**/migrations/**", "**/tests/**", "**/test/**", "**/__tests__/**",
                    "**/e2e/**", "**/examples/**",
                ])
                command = (
                    f"{binary} . --reporters json --output {shlex.quote(str(report_dir))} "
                    f"--min-lines {duplicate_policy['min_lines']} "
                    f"--min-tokens {duplicate_policy['min_tokens']} --mode mild "
                    f"--ignore {shlex.quote(ignore)}"
                )
                rc, tool_output = run(command)
                reports = sorted(report_dir.rglob("*.json")) if report_dir.exists() else []
                if rc != 0 or not reports:
                    detail = tool_output[-600:] or "jscpd produced no JSON report"
                    results["duplication"] = {
                        "passed": mode != "fail",
                        "status": "skip" if mode == "warn" else "fail",
                        "output": f"Duplication scan unavailable in {mode} mode:\n{detail}",
                    }
                else:
                    try:
                        clones = load_jscpd_clones(reports[0])
                        ranges = changed_line_ranges(diff_ref, code_changed)
                        candidates = clones_touching_changes(clones, ranges, pathlib.Path.cwd())
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        candidates = []
                        results["duplication"] = {
                            "passed": mode != "fail",
                            "status": "skip" if mode == "warn" else "fail",
                            "output": f"Could not parse jscpd report in {mode} mode: {exc}",
                        }
                    else:
                        results["duplication"] = duplication_result(candidates, duplicate_policy)

    # ── Feature gate (SPRINTFOUNDRY §2b regression tests + §3 example) ────────────
    # Deterministic backstop for the Evaluator's semantic §2b/§3 check. For a
    # feature-type sprint that changes application source OUTSIDE the declared test
    # and example directories, require the diff to also touch:
    #   - feature_tests_dir  → §2b feature/CRUD regression suite
    #   - examples_dir       → §3 runnable example
    # Skipped when: SPRINTFOUNDRY.md is absent or declares neither dir; the sprint
    # origin is a bugfix; or SPRINTFOUNDRY.md sets `feature_gate: off`. The dirs are
    # read from SPRINTFOUNDRY.md so projects with non-default layouts still work.
    def declared_dir(text, key):
        m = re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*(\S+)", text)
        if not m:
            return ""
        # keep the stable prefix before any <placeholder>: tests/features/<feature> → tests/features/
        prefix = m.group(1).split("<", 1)[0].strip().rstrip("/")
        return prefix + "/" if prefix else ""

    spf_path = pathlib.Path("SPRINTFOUNDRY.md")
    run_state = {}
    _rs = pathlib.Path(".sprintfoundry/state/run-state.json")
    if _rs.exists():
        try:
            run_state = json.loads(_rs.read_text())
        except Exception:
            run_state = {}
    origin = str(run_state.get("sprint_origin", "") or "")
    FEATURE_ORIGINS = {"feature", "minor_feature", "major_feature", "replan"}
    if spf_path.exists() and origin in FEATURE_ORIGINS:
        spf_text = spf_path.read_text(errors="ignore")
        gate_off = re.search(r"(?mi)^\s*feature_gate\s*:\s*(off|false|no|0)\s*$", spf_text)
        feat_dir = declared_dir(spf_text, "feature_tests_dir")
        ex_dir = declared_dir(spf_text, "examples_dir")
        sprint_dir = declared_dir(spf_text, "sprint_tests_dir")
        if not gate_off and (feat_dir or ex_dir):
            declared = tuple(p for p in (feat_dir, ex_dir, sprint_dir) if p)
            # "app source" = changed code files NOT inside the declared test/example dirs
            app_source = [f for f in code_changed if not any(f.startswith(p) for p in declared)]
            if app_source:
                missing = []
                if feat_dir and not any(f.startswith(feat_dir) for f in changed_paths):
                    missing.append(f"§2b feature regression tests under {feat_dir}")
                if ex_dir and not any(f.startswith(ex_dir) for f in changed_paths):
                    missing.append(f"§3 runnable example under {ex_dir}")
                if missing:
                    results["feature-gate"] = {
                        "passed": False,
                        "output": (
                            "Feature sprint changed application source but is missing "
                            "required SPRINTFOUNDRY artifacts:\n  " + "\n  ".join(missing)
                            + "\nApplication source touched:\n  " + "\n  ".join(app_source[:20])
                            + "\n(Set `feature_gate: off` in SPRINTFOUNDRY.md to disable, e.g. "
                            "during pure scaffolding.)"
                        ),
                    }
                else:
                    results["feature-gate"] = {
                        "passed": True,
                        "output": "Feature sprint ships §2b regression tests and §3 example.",
                    }

    # ── 兜底：如果未能识别任何栈，只跑 git diff stat ─────────────────────────────
    if not results:
        rc, out = run("git diff HEAD~1..HEAD --stat 2>&1")
        results["git-diff-stat"] = {"passed": True, "output": out}

    # 写结果文件。新文件统一放在 .sprintfoundry/results/quality/，避免污染项目根目录。
    passed_all = all(v["passed"] for v in results.values())
    lines = [f"# Quality Gate — Sprint {sprint_n}"]
    lines.append(f"\n**Verdict: {'PASS' if passed_all else 'FAIL'}**\n")
    for tool, res in results.items():
        status = res.get("status", "pass" if res["passed"] else "fail")
        icon = {"pass": "✅", "warn": "⚠️", "skip": "➖", "fail": "❌"}[status]
        output_limit = 4000 if tool == "duplication" else 800
        lines.append(f"\n## {icon} {tool}\n```\n{res['output'][:output_limit]}\n```")

    out_dir = pathlib.Path(".sprintfoundry") / "results" / "quality"
    out_dir.mkdir(parents=True, exist_ok=True)
    for legacy in pathlib.Path(".").glob("quality-gate-*.md"):
        target = out_dir / legacy.name
        if not target.exists():
            shutil.move(str(legacy), str(target))
    (out_dir / f"quality-gate-{sprint_n}.md").write_text("\n".join(lines))
    print("PASS" if passed_all else "FAIL")
    return 0 if passed_all else 1

if __name__ == "__main__":
    raise SystemExit(main())
