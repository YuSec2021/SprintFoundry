#!/usr/bin/env python3
"""SprintFoundry release step — runs after an attested SPRINT PASS.

Invoked by orchestrate.py (never read into an agent's context):

    python3 scripts/release.py --project-dir <root> [--sprint N]

Three mechanical stages, in crash-safe order:
  1. version bump   — VERSION, CHANGELOG.md, MEMORY.md ledger (idempotent)
  2. release commit — commit + annotated tag (+ push when a remote exists)
  3. branch merge   — merge the sprint branch into base, with retry/lock recovery

Exit 0 on success, 2 when the branch merge needs a human (run-state already
carries needs_human=true and the recovery command).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time


def bump_version(sprint: "int | None" = None) -> tuple[int, str]:

    rs_path = pathlib.Path(".sprintfoundry/state/run-state.json")
    run_state = json.loads(rs_path.read_text()) if rs_path.exists() else {}
    origin = run_state.get("sprint_origin", "feature")

    # VERSION file is the primary machine-readable version source — never trust
    # .sprintfoundry/state/run-state.json alone. MEMORY.md is a recovery fallback if VERSION is missing.
    # .sprintfoundry/state/run-state.json.current_version can drift when re-processing historical sprints.
    version_file = pathlib.Path("VERSION")
    if version_file.exists():
        current = version_file.read_text().strip().lstrip("v") or "0.0.0"
    else:
        current = "0.0.0"
        mem = pathlib.Path("MEMORY.md")
        if mem.exists():
            for line in reversed(mem.read_text().splitlines()):
                if line.startswith("## Latest version:"):
                    current = line.split(":")[-1].strip().lstrip("v") or "0.0.0"
                    break
        if current == "0.0.0":
            current = str(run_state.get("current_version", "0.0.0")).strip().lstrip("v") or "0.0.0"
    contract = pathlib.Path("sprint-contract.md").read_text(errors="ignore") \
               if pathlib.Path("sprint-contract.md").exists() else ""
    eval_glob = sorted([
        *pathlib.Path(".").glob(".sprintfoundry/results/eval/eval-result-*.md"),
        *pathlib.Path(".").glob("eval-result-*.md"),
    ], key=lambda p: int(re.search(r"\d+", p.stem).group()))
    eval_text = eval_glob[-1].read_text(errors="ignore") if eval_glob else ""

    # The sprint that just PASSED. Never use current_sprint here: by the time this
    # script runs, update_run_state has already advanced current_sprint to the NEXT
    # pending sprint (or 0 when the plan is complete) — using it would record the
    # wrong sprint in the ledger and break the idempotency check.
    sprint_n = str(sprint if sprint is not None
                   else run_state.get("last_successful_sprint") or "?")
    mem_path = pathlib.Path("MEMORY.md")
    if mem_path.exists() and sprint_n.isdigit():
        for line in mem_path.read_text().splitlines():
            if not line.startswith("|") or line.startswith("| Sprint") or set(line.strip()) <= {"|", "-"}:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) > 3 and parts[1] == sprint_n and parts[3] == "PASS":
                # Idempotent no-op — but self-heal VERSION/CHANGELOG if a previous
                # run crashed between the MEMORY.md write and the VERSION write.
                for ln in reversed(mem_path.read_text().splitlines()):
                    if ln.startswith("## Latest version:"):
                        recorded = ln.split(":")[-1].strip().lstrip("v")
                        if recorded and recorded != current:
                            pathlib.Path("VERSION").write_text(recorded + "\n")
                            print(f"VERSION self-healed to {recorded} from MEMORY.md footer.")
                        break
                print(f"MEMORY.md already records Sprint {sprint_n} PASS; release bump is idempotent no-op.")
                return 0, 'release bump is idempotent no-op'

    major, minor, patch = map(int, current.split("."))

    MAJOR_ORIGINS  = {"major_feature", "replan"}
    PATCH_ORIGIN   = "bugfix"
    PATCH_EXCLUDES = ["new feature", "add ", "introduce", "new endpoint", "new page"]

    def _field_value(line, labels):
        stripped = re.sub(r"^\s*(?:[-*]|\d+\.)\s*", "", line).strip()
        if ":" not in stripped:
            return None
        label, value = stripped.split(":", 1)
        label = label.strip().lower()
        if label in labels:
            return value.strip().lower()
        return None

    def _is_negative(value: str) -> bool:
        return bool(re.match(r"^(no|none|false|n/a|not required|without)\b", value))

    def has_explicit_major_signal(text: str) -> bool:
        """Only explicit release/compatibility declarations can force a major bump."""
        for line in text.splitlines():
            value = _field_value(line, ("semver", "version bump"))
            if value and re.match(r"^major\b", value):
                return True

            value = _field_value(line, ("breaking change", "breaking changes"))
            if value and not _is_negative(value):
                return True

            value = _field_value(line, ("migration", "migration required"))
            if value and not _is_negative(value) and re.search(r"\b(yes|true|required)\b", value):
                return True

            value = _field_value(line, ("compatibility", "backward compatibility", "backwards compatibility"))
            if value and not _is_negative(value) and re.search(r"\b(breaking|broken|incompatible|not compatible)\b", value):
                return True

            value = _field_value(line, ("public api", "api compatibility"))
            if value and not _is_negative(value) and re.search(r"\b(remove|removed|replace|replaced|deprecate|deprecated|incompatible)\b", value):
                return True

        return False

    if (origin in MAJOR_ORIGINS
            or has_explicit_major_signal(contract)
            or "ARCHITECTURE DRIFT DETECTED" in eval_text):
        bump = "major"; major += 1; minor = 0; patch = 0
    elif (origin == PATCH_ORIGIN
            and not any(kw in contract.lower() for kw in PATCH_EXCLUDES)):
        bump = "patch"; patch += 1
    else:
        bump = "minor"; minor += 1; patch = 0

    new_version = f"{major}.{minor}.{patch}"

    # Safety guard: new version must never be less than (or equal to) what VERSION already has.
    # This prevents rollback when re-processing an out-of-order / lower-ID sprint.
    def _v(s):
        try: return tuple(int(x) for x in s.split("."))
        except: return (0, 0, 0)
    if _v(new_version) <= _v(current):
        # Force a patch bump on top of current instead
        cm, cn, cp = map(int, current.split("."))
        cp += 1
        new_version = f"{cm}.{cn}.{cp}"
        bump = "patch(guard)"
        major, minor, patch = cm, cn, cp

    # ── Write order is crash-safe by construction ───────────────────────────
    # 1. MEMORY.md ledger row FIRST — it is the idempotency marker checked above.
    #    A crash after this write makes the re-run a clean no-op (which then
    #    self-heals VERSION) instead of double-bumping.
    # 2. VERSION second, CHANGELOG last — both re-derivable from MEMORY.md.
    title_match = re.search(r"^#+\s+Sprint\s+\d+[:\s—-]+(.+)", contract, re.MULTILINE)
    sprint_title = title_match.group(1).strip()[:60] if title_match else "—"
    today = datetime.date.today().isoformat()
    if not mem_path.exists():
        mem_path.write_text(
            "# SprintFoundry Sprint Ledger\n"
            "<!-- ledger rows are append-only; footer metadata may be regenerated by Orchestrator -->\n\n"
            "| Sprint | Title | Status | Version | Date | Origin |\n"
            "|--------|-------|--------|---------|------|--------|\n"
        )
    mem_lines = mem_path.read_text().splitlines()
    # Remove old footer lines before appending
    mem_lines = [l for l in mem_lines if not l.startswith("## Latest version:") and not l.startswith("## Max sprint ID:")]
    # Append new row
    mem_lines.append(f"| {sprint_n} | {sprint_title} | PASS | v{new_version} | {today} | {origin} |")
    # Compute max_sprint_id from all rows
    max_id = 0
    for l in mem_lines:
        if l.startswith("|") and not l.startswith("| Sprint"):
            parts = [p.strip() for p in l.split("|")]
            if len(parts) > 1 and parts[1].isdigit():
                max_id = max(max_id, int(parts[1]))
    mem_lines.append(f"\n## Latest version: v{new_version}")
    mem_lines.append(f"## Max sprint ID: {max_id}")
    mem_path.write_text("\n".join(mem_lines) + "\n")
    print(f"MEMORY.md updated — sprint {sprint_n} PASS recorded.")

    pathlib.Path("VERSION").write_text(new_version + "\n")

    # Append to CHANGELOG.md
    entry = f"\n## v{new_version} — Sprint {sprint_n} [{bump.upper()} bump]\n"
    for obs in re.findall(r"Observation: (.+)", eval_text):
        entry += f"- {obs.strip()}\n"
    with open("CHANGELOG.md", "a") as f:
        f.write(entry)

    print(f"Version bump: {current} → {new_version}  ({bump})")
    print(f"VERSION and CHANGELOG.md updated.")
    return 0, f'version bump complete'


def release_commit() -> tuple[int, str]:
    """Commit the version artifacts and tag the release."""
    version_file = pathlib.Path("VERSION")
    if not version_file.exists():
        return 0, "no VERSION file; nothing to commit"
    new_version = version_file.read_text().strip()
    subprocess.run(["git", "add", "VERSION", "CHANGELOG.md", "MEMORY.md"], check=False)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        return 0, "release artifacts already committed"
    sprint = "?"
    rs = pathlib.Path(".sprintfoundry/state/run-state.json")
    if rs.exists():
        try:
            sprint = str(json.loads(rs.read_text()).get("current_sprint", "?"))
        except Exception:
            pass
    subprocess.run(
        ["git", "commit", "-m",
         f"chore(release): bump to v{new_version} after Sprint {sprint} PASS"],
        check=False,
    )
    subprocess.run(["git", "tag", "-a", f"v{new_version}", "-m", f"v{new_version}"],
                   check=False)
    if subprocess.run(["git", "remote", "get-url", "origin"],
                      capture_output=True).returncode == 0:
        subprocess.run(["git", "push", "origin", f"v{new_version}"], check=False)
    return 0, f"released v{new_version}"


def merge_sprint_branch(sprint: "int | None" = None) -> tuple[int, str]:

    rs_path = pathlib.Path(".sprintfoundry/state/run-state.json")
    rs = json.loads(rs_path.read_text()) if rs_path.exists() else {}
    sprint_branch = rs.get("active_branch", "")
    base_branch   = rs.get("base_branch", "main")
    # current_sprint has already advanced past the sprint that just passed.
    sprint_n      = sprint if sprint is not None else (
        rs.get("last_successful_sprint") or rs.get("current_sprint", "?"))

    # Nothing to merge if already on base or no branch recorded
    if not sprint_branch or sprint_branch == base_branch:
        print(f"[merge] No sprint branch to merge — sprint_branch={sprint_branch!r}, base={base_branch!r}")
        return 0, 'no sprint branch to merge'

    def run(cmd):
        return subprocess.run(cmd, shell=True, capture_output=True, text=True)

    def clear_stale_locks():
        """Remove git lock files left by crashed processes."""
        for lock in [".git/index.lock", ".git/MERGE_HEAD", ".git/CHERRY_PICK_HEAD"]:
            p = pathlib.Path(lock)
            if p.exists():
                try:
                    p.unlink()
                    print(f"[merge] Removed stale lock: {lock}")
                except Exception as e:
                    print(f"[merge] WARNING: could not remove {lock}: {e}")

    def update_run_state(updates: dict):
        data = json.loads(rs_path.read_text()) if rs_path.exists() else {}
        data.update(updates)
        rs_path.write_text(json.dumps(data, indent=2))

    MAX_RETRIES = 3
    last_error  = ""

    for attempt in range(1, MAX_RETRIES + 1):
        clear_stale_locks()

        # Make sure we are on the sprint branch before merging into base
        cur = run("git branch --show-current").stdout.strip()
        if cur != sprint_branch:
            r = run(f"git checkout {sprint_branch}")
            if r.returncode != 0:
                last_error = f"cannot checkout sprint branch: {r.stderr.strip()}"
                print(f"[merge] attempt {attempt}: {last_error}")
                time.sleep(5 * attempt)
                continue

        # Switch to base branch
        r = run(f"git checkout {base_branch}")
        if r.returncode != 0:
            last_error = f"cannot checkout {base_branch}: {r.stderr.strip()}"
            print(f"[merge] attempt {attempt}: {last_error}")
            run(f"git checkout {sprint_branch}")
            time.sleep(5 * attempt)
            continue

        # Attempt the merge
        msg = f"merge: sprint-{sprint_n} ({sprint_branch}) → {base_branch} after SPRINT PASS"
        r = run(f'git merge --no-ff {sprint_branch} -m "{msg}"')
        if r.returncode == 0:
            update_run_state({"active_branch": base_branch, "merge_retry_count": 0})
            return 0, f"merged {sprint_branch} into {base_branch}"

        # Merge failed — abort cleanly and maybe retry
        last_error = r.stderr.strip() or r.stdout.strip()
        print(f"[merge] attempt {attempt} FAILED: {last_error}")
        run("git merge --abort 2>/dev/null || true")
        clear_stale_locks()
        run(f"git checkout {sprint_branch}")

        if attempt < MAX_RETRIES:
            wait = 5 * attempt
            print(f"[merge] retrying in {wait}s …")
            time.sleep(wait)

    # All attempts exhausted
    print(f"[merge] FAILED after {MAX_RETRIES} attempts. Last error: {last_error}")
    update_run_state({
        "needs_human": True,
        "last_failure_reason": (
            f"Sprint {sprint_n} PASSED but branch merge failed after {MAX_RETRIES} attempts. "
            f"Last error: {last_error}. "
            f"To recover: git checkout {base_branch} && git merge --no-ff {sprint_branch} && "
            f"python3 -c \"import json,pathlib; d=json.loads(pathlib.Path('.sprintfoundry/state/run-state.json').read_text()); "
            f"d.update({{'needs_human':False,'active_branch':'{base_branch}','merge_retry_count':0}}); "
            f"pathlib.Path('.sprintfoundry/state/run-state.json').write_text(json.dumps(d,indent=2))\""
        )
    })
    return 2, f'merge failed: {last_error}'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--sprint", type=int, default=None)
    args = parser.parse_args()
    os.chdir(args.project_dir)

    stages = ((bump_version, True), (release_commit, False), (merge_sprint_branch, True))
    for stage, takes_sprint in stages:
        code, message = stage(args.sprint) if takes_sprint else stage()
        print(f"[{stage.__name__}] {message}")
        if code:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
