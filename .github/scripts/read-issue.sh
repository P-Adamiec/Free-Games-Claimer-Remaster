#!/usr/bin/env bash
# The labelling agent's only read tool: no raw gh, so no --jq or --template that could print the environment.
# Usage: read-issue.sh labels | read-issue.sh NUMBER (an issue on this run's list)
set -euo pipefail

list="$(dirname "$0")/triage-issues.txt"
if [[ $# -eq 1 && "$1" == "labels" ]]; then
  gh label list --limit 200 --json name,description --jq '.[] | "\(.name): \(.description)"'
elif [[ $# -eq 1 && "$1" =~ ^[0-9]+$ ]] && grep -qxF -- "$1" "$list" 2>/dev/null; then
  gh issue view "$1" --json number,title,body,state,stateReason,labels,comments
else
  echo "Usage: read-issue.sh labels | read-issue.sh NUMBER, where NUMBER is on this run's list" >&2
  exit 1
fi
