#!/bin/bash
# POCSAG/FLEX pager lane. Text, not voice -- no transcription needed at all.
# rtl_fm demodulates NFM, multimon-ng decodes the pager protocols from raw PCM.
set -uo pipefail

# Root, config and data directories, plus brew on PATH. One resolver, so a
# lane can never write to a different events directory than the console reads.
. "$(dirname "${BASH_SOURCE[0]}")/bw-env.sh"

DEV="${1:-1}"
FREQ="${2:-929.6M}"
EV="$VAR/events/pagers.txt"
RF=""; MM=""
cleanup() { [ -n "$MM" ] && kill "$MM" 2>/dev/null; [ -n "$RF" ] && kill "$RF" 2>/dev/null; return 0; }
trap cleanup EXIT INT TERM

# FREQ may be a comma-separated list. rtl_fm then hops between them on
# squelch, which costs the first syllable of a transmission but covers more of
# the band than a single fixed channel. A squelch level is REQUIRED for hopping
# -- without -l rtl_fm sits on the first frequency and never moves, which looks
# exactly like the extra channels being quiet.
# macOS ships bash 3.2.57, where "${EMPTY[@]}" under  is an UNBOUND
# VARIABLE error, not an empty expansion. A separate empty SQUELCH array killed
# this lane instantly on every run. Everything goes in one always-non-empty
# array instead.
IFS=',' read -ra FLIST <<< "$FREQ"
FARGS=()
for f in "${FLIST[@]}"; do FARGS+=(-f "$f"); done
# rtl_fm only HOPS between frequencies when a squelch level is set; without -l
# it sits on the first one forever, which looks exactly like the other
# channels being quiet.
if [ "${#FLIST[@]}" -gt 1 ]; then FARGS+=(-l 30); fi

# -E dc removes the DC spike; 22050 is what multimon-ng expects
rtl_fm -d "$DEV" "${FARGS[@]}" -M fm -s 22050 -g 40 -E dc - 2>/dev/null | \
  "$TOOLS/multimon-ng/build/multimon-ng" \
    -a POCSAG512 -a POCSAG1200 -a POCSAG2400 -a FLEX \
    -f alpha -t raw - >> "$EV" 2>/dev/null &
MM=$!
wait $MM
