#!/bin/bash
# APRS lane: rtl_fm demodulates 144.390 NFM, direwolf decodes AX.25 packets.
#
# direwolf reads audio on stdin with "-" as the device. 24000 Hz is what its
# 1200-baud AFSK demodulator expects.
# Kill named PIDs on exit -- never `kill 0`, which signals the caller's group.
set -uo pipefail

# Root, config and data directories, plus brew on PATH. One resolver, so a
# lane can never write to a different events directory than the console reads.
. "$(dirname "${BASH_SOURCE[0]}")/bw-env.sh"

DEV="${1:-0}"
FREQ="${2:-144.390M}"
EV="$VAR/events/aprs.txt"
CONF="$CONF_DIR/direwolf.conf"

# direwolf looks for tocalls.yaml in the CWD; without it it still decodes but
# cannot resolve device identifiers.
cd "$CONF_DIR" 2>/dev/null || true

# direwolf looks for tocalls.yaml relative to its CWD and a few
# system paths, none of which contain the copy in conf/.
cd "$CONF_DIR" 2>/dev/null || true
RF=""; DW=""
cleanup(){ [ -n "$DW" ] && kill "$DW" 2>/dev/null; [ -n "$RF" ] && kill "$RF" 2>/dev/null; return 0; }
trap cleanup EXIT INT TERM

rtl_fm -d "$DEV" -f "$FREQ" -M fm -s 24000 -g 40 -E dc - 2>/dev/null | \
  direwolf -c "$CONF" -r 24000 -D 1 -t 0 -q hd - 2>&1 | \
  # Keep only AX.25 frames. direwolf's startup banner is ~150 lines and would
  # make an empty lane look productive -- the classic "healthy but producing
  # nothing" failure. A real frame looks like  SRC>DEST,PATH:payload
  grep --line-buffered -E "^(\[[0-9.]+\] )?[A-Z0-9-]{3,9}>[A-Z0-9-]{3,9}" >> "$EV" &
DW=$!
wait $DW
