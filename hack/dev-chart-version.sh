#!/usr/bin/env bash
# Print a chart-versioned dev build without changing the chart or publishing it.
set -euo pipefail

chart_file=${1:-charts/ao-data-platform/Chart.yaml}
commit_sha=${2:-$(git rev-parse HEAD)}

if [[ ! -f "$chart_file" ]]; then
  echo "Chart file does not exist: $chart_file" >&2
  exit 1
fi

chart_version=$(awk '/^version:/ {gsub(/"/, "", $2); print $2; exit}' "$chart_file")
if [[ ! "$chart_version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "Chart.yaml version must be a release version such as 5.2.0; got: $chart_version" >&2
  exit 1
fi
if [[ ! "$commit_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "The dev chart version requires a full Git commit SHA." >&2
  exit 1
fi

printf '%s-dev.g%s\n' "$chart_version" "$commit_sha"
