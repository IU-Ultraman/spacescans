#!/usr/bin/env bash
# Judge an R CMD check log the way the R gate wants it: no ERROR, and no WARNING except the
# licence one ("Non-standard license specification"), which stays until a licence is chosen
# (tier-2 decision of 2026-10-12). NOTEs are shown, not fatal. Once a standard licence is in
# DESCRIPTION the allowance matches nothing, and any WARNING fails.
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

allowed='* checking DESCRIPTION meta-information ... WARNING'
warn_lines="$(grep -E '\.\.\. WARNING$' "$log" || true)"
if [ -n "$warn_lines" ]; then
  while IFS= read -r line; do
    if [ "$line" != "$allowed" ]; then
      echo "::error::unexpected WARNING: $line"
      bad=1
    fi
  done <<< "$warn_lines"
fi

# The DESCRIPTION check may warn only about the licence: every line of its report must be
# part of the licence message (header, indented licence text, "Standardizable: FALSE").
report="$(awk -v a="$allowed" '$0 == a {on = 1; next} on && /^\* / {on = 0} on' "$log")"
if [ -n "$report" ]; then
  other="$(printf '%s\n' "$report" |
    grep -vE '^(Non-standard license specification:|  .*|Standardizable: FALSE)$' || true)"
  if [ -n "$other" ]; then
    echo "::error::the DESCRIPTION check warns about more than the licence:"
    printf '%s\n' "$other"
    bad=1
  elif ! printf '%s\n' "$report" | grep -qx 'Non-standard license specification:'; then
    echo "::error::the DESCRIPTION check warns, but not about the licence"
    bad=1
  else
    echo "Allowed: the licence WARNING (no licence chosen yet)."
  fi
fi

# The summary must agree with the WARNING lines counted above.
n_lines="$(printf '%s' "$warn_lines" | grep -c . || true)"
n_status="$(printf '%s\n' "$status" | grep -oE '[0-9]+ WARNING' | grep -oE '^[0-9]+' || echo 0)"
if [ "$n_lines" != "$n_status" ]; then
  echo "::error::$n_status WARNING(s) in the Status line, $n_lines WARNING line(s) in the log"
  bad=1
fi

exit "$bad"
