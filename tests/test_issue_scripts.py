"""The labelling agent's two scripts in .github/scripts/ refuse everything outside their job.

They run against a fake `gh` that answers from canned data and records every call.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
# WSL's bash cannot open Windows paths; Git Bash and the Linux CI can.
pytestmark = pytest.mark.skipif(not BASH or "system32" in BASH.lower(), reason="needs a POSIX bash")

FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_CALLS"
case "$1 $2" in
  "label list") printf '%s\\n' "type: bug" "type: feature" "status: needs info" "status: confirmed" "status: fixed in dev" "good first issue" ;;
  "issue view") printf '%s\\n' "A title" "$FAKE_STATE" "type: bug" "status: fixed in dev" ;;
esac
"""


@pytest.fixture
def run(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("edit-issue-labels.sh", "read-issue.sh"):
        # A Windows checkout may carry CRLF, which bash cannot run.
        (scripts / name).write_bytes((ROOT / ".github" / "scripts" / name).read_bytes().replace(b"\r\n", b"\n"))
    (scripts / "triage-issues.txt").write_bytes(b"83\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_bytes(FAKE_GH.encode())
    (bindir / "gh").chmod(0o755)
    calls = tmp_path / "calls.log"

    def _run(script, *args, state="OPEN"):
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_STATE": state, "FAKE_CALLS": str(calls)}
        result = subprocess.run([BASH, (scripts / script).as_posix(), *args], env=env, capture_output=True, text=True)
        edits = [line for line in calls.read_text().splitlines() if line.startswith("issue edit")] if calls.exists() else []
        calls.unlink(missing_ok=True)
        return result, edits

    return _run


def test_a_valid_label_is_added(run):
    result, edits = run("edit-issue-labels.sh", "--issue", "83", "--add-label", "type: feature")
    assert result.returncode == 0, result.stderr
    assert edits == ["issue edit 83 --add-label type: feature"]


def test_an_issue_off_the_list_is_refused(run):
    result, edits = run("edit-issue-labels.sh", "--issue", "1", "--add-label", "type: feature")
    assert result.returncode == 1 and not edits


@pytest.mark.parametrize("label", ["status: confirmed", "status: fixed in dev", "good first issue", "type: nonsense"])
def test_labels_the_agent_may_not_add(run, label):
    result, edits = run("edit-issue-labels.sh", "--issue", "83", "--add-label", label)
    assert result.returncode == 0 and not edits


def test_the_owners_status_stays_on_an_open_issue(run):
    _, edits = run("edit-issue-labels.sh", "--issue", "83", "--remove-label", "status: fixed in dev")
    assert not edits


def test_status_goes_once_the_issue_is_closed(run):
    _, edits = run("edit-issue-labels.sh", "--issue", "83", "--remove-label", "status: fixed in dev", state="CLOSED")
    assert edits == ["issue edit 83 --remove-label status: fixed in dev"]


def test_any_other_flag_is_refused(run):
    result, edits = run("edit-issue-labels.sh", "--issue", "83", "--body", "hello")
    assert result.returncode == 1 and not edits


@pytest.mark.parametrize("args", [("83", "--jq", "$ENV"), ("labels", "--jq", "$ENV"), ("1",), ("83; env",), ()])
def test_the_reader_takes_only_labels_or_a_listed_issue(run, args):
    result, _ = run("read-issue.sh", *args)
    assert result.returncode == 1


def test_the_reader_shows_a_listed_issue(run):
    result, _ = run("read-issue.sh", "83")
    assert result.returncode == 0 and "A title" in result.stdout
