#!/usr/bin/env bash
# Read-only schedule-state audit. Never enable a workflow automatically.
set -euo pipefail
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
failed=0
for wf in ingest.yml weekly.yml ci.yml; do
  if ! state=$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${wf}" --jq .state); then
    printf 'Could not read state for %s; no change requested\n' "$wf" >&2
    failed=1
    continue
  fi
  case "$state" in
    active|disabled_manually)
      printf 'Preserved %s state=%s; automatic enable is disabled\n' "$wf" "$state"
      ;;
    disabled_inactivity)
      printf 'Workflow %s is inactive; owner review required, no change requested\n' "$wf" >&2
      failed=1
      ;;
    *)
      printf 'Unknown or unsupported state for %s: %s; no change requested\n' "$wf" "$state" >&2
      failed=1
      ;;
  esac
done
exit "$failed"
