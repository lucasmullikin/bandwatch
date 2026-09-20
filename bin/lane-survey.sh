#!/bin/bash
# lane-survey.sh DEV LOW HIGH BINSIZE INTERVAL EXITSEC OUTFILE
#
# rtl_power writes its output file with truncating semantics and only flushes
# completed hops. A sweep that is killed before it finishes therefore leaves
# NOTHING -- and destroys whatever the previous run had left.
#
# That is exactly what happened to milair_survey: 175 MHz at 2.4 MHz per hop is
# ~73 hops, and at -i 10 a single pass needs ~730s. It was given a 90-second
# slot. 219 of its 261 runs were killed mid-pass ("Signal caught, finishing scan
# pass") and var/events/milair_survey.csv sat at 0 bytes for its entire life --
# which is why milair_voice could never be enabled: there were no measured
# frequencies to enable it with.
#
# This wrapper sweeps into a PRIVATE temp file and APPENDS whatever completed to
# the canonical CSV. A short pass now contributes its hops instead of erasing
# the record.
set -uo pipefail

# Root, config and data directories, plus brew on PATH. One resolver, so a
# lane can never write to a different events directory than the console reads.
. "$(dirname "${BASH_SOURCE[0]}")/bw-env.sh"
DEV="$1"; LOW="$2"; HIGH="$3"; BIN="$4"; INTERVAL="$5"; EXITSEC="$6"; OUT="$7"

TMP="$(mktemp -t lanesurvey)" || exit 1
trap 'rm -f "$TMP"' EXIT INT TERM

rtl_power -d "$DEV" -f "${LOW}:${HIGH}:${BIN}" -g 40 -i "$INTERVAL" -e "$EXITSEC" "$TMP"
rc=$?

# Append even on a non-zero rc: a killed sweep still wrote every hop it finished,
# and those rows are real measurements. Reporting nothing would be the lie.
if [ -s "$TMP" ]; then
  rows=$(wc -l < "$TMP" | tr -d ' ')
  mkdir -p "$(dirname "$OUT")"
  cat "$TMP" >> "$OUT"
  echo "lane-survey: appended $rows row(s) to $OUT (rtl_power rc=$rc)"
else
  # Say so out loud. A survey that measured nothing must not look like a survey
  # that measured silence.
  echo "lane-survey: rtl_power produced NO rows (rc=$rc) -- nothing appended; the sweep never completed a hop"
fi
exit 0
