#!/usr/bin/env bash
# Judge an R CMD check log the way the R gate wants it: no ERROR and no WARNING. NOTEs are
# shown, not fatal. (Until 0.3.0 the gate allowed the "Non-standard license specification"
# WARNING while no licence was chosen; the package is MIT now, so every WARNING fails.)
#
#   .github/scripts/r-check-status.sh <pkg>.Rcheck/00check.log
set -euo pipefail

log="${1:?usage: r-check-status.sh <pkg>.Rcheck/00check.log}"
if [ ! -f "$log" ]; then
  echo "::error::no R CMD check log at $log"
  exit 1
fi

status="$(grep -E '^Status: ' "$log" | tail -n 1 || true)"
if [ -z "$status" ]; then
  echo "::error::R CMD check did not finish: no Status line in $log"
  exit 1
fi
echo "$status"

bad=0
if grep -qE '\.\.\. ERROR$' "$log" || [[ "$status" == *ERROR* ]]; then
  echo "::error::R CMD check reported an ERROR"
  bad=1
fi

warn_lines="$(grep -E '\.\.\. WARNING$' "$log" || true)"
if [ -n "$warn_lines" ]; then
  while IFS= read -r line; do
    echo "::error::WARNING: $line"
  done <<< "$warn_lines"
  bad=1
fi

# The summary must agree with the WARNING lines counted above.
n_lines="$(printf '%s' "$warn_lines" | grep -c . || true)"
n_status="$(printf '%s\n' "$status" | grep -oE '[0-9]+ WARNING' | grep -oE '^[0-9]+' || echo 0)"
if [ "$n_lines" != "$n_status" ]; then
  echo "::error::$n_status WARNING(s) in the Status line, $n_lines WARNING line(s) in the log"
  bad=1
fi

exit "$bad"
