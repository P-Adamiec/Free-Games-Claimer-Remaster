#!/usr/bin/env bash
# The labelling agent's only write tool: changes labels on one issue picked for this run, nothing else.
# Usage: edit-issue-labels.sh --issue 83 --add-label "type: bug" --remove-label "status: needs info"
set -euo pipefail

issue=""
add=()
remove=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) issue="$2"; shift 2 ;;
    --add-label) add+=("$2"); shift 2 ;;
    --remove-label) remove+=("$2"); shift 2 ;;
    *) echo "Only --issue, --add-label and --remove-label are accepted" >&2; exit 1 ;;
  esac
done

# The list sits next to this script, written read-only before the agent starts, so no argument or variable can widen it.
list="$(dirname "$0")/triage-issues.txt"
if ! [[ "$issue" =~ ^[0-9]+$ ]] || ! grep -qxF -- "$issue" "$list" 2>/dev/null; then
  echo "Issue '$issue' is not on this run's list" >&2
  exit 1
fi

# status: confirmed, status: fixed in dev, good first issue and help wanted stay the owner's.
may_add='^(type|store|area|resolution): |^status: needs info$'
may_remove='^(type|store|area|status|resolution): '

valid=$(gh label list --limit 200 --json name --jq '.[].name')
info=$(gh issue view "$issue" --json title,state,labels --jq '.title, .state, .labels[].name')
title=$(sed -n 1p <<<"$info")
state=$(sed -n 2p <<<"$info")
current=$(tail -n +3 <<<"$info")

args=()
changes=()
for label in "${add[@]}"; do
  if ! [[ "$label" =~ $may_add ]]; then
    echo "Not allowed to add: $label" >&2
  elif ! grep -qxF -- "$label" <<<"$valid"; then
    echo "No such label: $label" >&2
  elif ! grep -qxF -- "$label" <<<"$current"; then
    args+=(--add-label "$label")
    changes+=("+ $label")
  fi
done
for label in "${remove[@]}"; do
  if ! [[ "$label" =~ $may_remove ]]; then
    echo "Not allowed to remove: $label" >&2
  elif [[ "$label" == status:* && "$state" != "CLOSED" ]]; then
    echo "Not allowed to remove $label from an open issue" >&2
  elif grep -qxF -- "$label" <<<"$current"; then
    args+=(--remove-label "$label")
    changes+=("- $label")
  fi
done

if [[ ${#args[@]} -eq 0 ]]; then
  echo "#$issue: nothing to change"
  exit 0
fi

gh issue edit "$issue" "${args[@]}" >/dev/null
summary=$(printf '%s, ' "${changes[@]}")
echo "#$issue $title: ${summary%, }"
