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
# Optional 8th argument so existing 7-argument callers keep working unchanged.
# survey_fm runs at 30: it is a known-good control against FM broadcast, where
# 40 overloads the front end and the "control" stops being one.
GAIN="${8:-40}"

TMP="$(mktemp -t lanesurvey)" || exit 1
trap 'rm -f "$TMP"' EXIT INT TERM

# -1 is SINGLE-SHOT: one sweep, then exit. An occupancy lane wants exactly that,
# and it avoids the path rtl_power's own help warns about -- "-i ... (buggy if a
# full sweep takes longer than the interval)", which is every multi-hop sweep
# here (8 hops x 10s against a 10s interval).
#
# -e stays as a backstop, but it is NOT trusted. Measured on this receiver:
#   rtl_power -f 162.35M:162.6M:5k -i 10 -e 30   ->  ran 139s, 0 rows
#   the same with -e 30s                         ->  ran 176s, 0 rows
# Both ignored SIGINT and SIGTERM ("Signal caught, aborting immediately." three
# times over) and only died to SIGKILL. rtl_power HANGS here -- intermittently,
# and "[R82XX] PLL not locked!" shows up in the lanes it happens to.
#
# So the runtime is bounded HERE rather than left to the broker's dwell. A hang
# then costs a known number of seconds and is reported as a hang, instead of
# arriving as an anonymous SIGKILL at the end of the slot -- and a SIGKILL
# mid-USB-transfer is what leaves the dongle claimed.
HARD_LIMIT=$(( EXITSEC + 15 ))
# macOS ships no timeout(1); brew coreutils provides it as timeout or gtimeout.
# Without one the sweep still runs -- unbounded, as before -- rather than the
# lane failing outright, because an unbounded sweep is the status quo and a
# broken lane is not.
TIMEOUT=""
for t in timeout gtimeout; do
  command -v "$t" >/dev/null 2>&1 && { TIMEOUT="$t"; break; }
done
if [ -n "$TIMEOUT" ]; then
  "$TIMEOUT" -s TERM -k 5 "$HARD_LIMIT" \
    rtl_power -1 -d "$DEV" -f "${LOW}:${HIGH}:${BIN}" -g "$GAIN" \
              -i "$INTERVAL" -e "$EXITSEC" "$TMP"
  rc=$?
else
  echo "lane-survey: no timeout(1) found (brew install coreutils) -- running" \
       "rtl_power UNBOUNDED. A hang will then run until the broker kills it."
  rtl_power -1 -d "$DEV" -f "${LOW}:${HIGH}:${BIN}" -g "$GAIN" \
            -i "$INTERVAL" -e "$EXITSEC" "$TMP"
  rc=$?
fi
if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  echo "lane-survey: rtl_power HUNG -- no exit after ${HARD_LIMIT}s, killed." \
       "It ignores -e and catchable signals when this happens; check the lane" \
       "log for '[R82XX] PLL not locked!'. A kill here can leave the dongle" \
       "claimed, which needs a replug."
fi

# Append even on a non-zero rc: a killed sweep still wrote every hop it finished,
# and those rows are real measurements. Reporting nothing would be the lie.
if [ -s "$TMP" ]; then
  rows=$(wc -l < "$TMP" | tr -d ' ')
  mkdir -p "$(dirname "$OUT")"
  cat "$TMP" >> "$OUT"
  echo "lane-survey: appended $rows row(s) to $OUT (rtl_power rc=$rc)"

  # Appending fixes truncation and introduces unbounded growth in its place.
  # One station's milair_survey.csv reached 19 MB this way, and nothing in the
  # project would ever have reclaimed it: the disk pruner truncates oversized
  # LOGS, and this is not a log. So the file is a rolling window -- when it
  # passes the cap, the OLDEST half goes and the newest rows stay, because a
  # sweep CSV is read for what the band is doing lately.
  cap_mb="${BANDWATCH_SURVEY_MAX_MB:-16}"
  cap=$(( cap_mb * 1024 * 1024 ))
  sz=$(wc -c < "$OUT" | tr -d ' ')
  if [ "$sz" -gt "$cap" ]; then
    keep=$(( cap / 2 ))
    tail -c "$keep" "$OUT" > "$OUT.trim" 2>/dev/null && mv "$OUT.trim" "$OUT"
    echo "lane-survey: $OUT passed ${cap_mb}MB -- kept the newest $(( keep / 1024 ))KB"
  fi
else
  # Say so out loud. A survey that measured nothing must not look like a survey
  # that measured silence.
  echo "lane-survey: rtl_power produced NO rows (rc=$rc) -- nothing appended; the sweep never completed a hop"
fi
exit 0
