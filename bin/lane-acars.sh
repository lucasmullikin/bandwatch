#!/bin/bash
# ACARS lane: aircraft TEXT messages. No transcription, no hallucination risk,
# no GPU -- the message is the data.
#
# acarsdec takes several frequencies on one dongle. 130.025/131.550/131.725 are
# the busiest US channels and span 1.7 MHz, inside one 2.4 MHz slice.
# (129.125 would push the span to 2.6 MHz -- past the usable passband.)
#
# Built from a macOS-patched tree: see patches/acarsdec-macos.patch.sh.
set -uo pipefail

# Root, config and data directories, plus brew on PATH. One resolver, so a
# lane can never write to a different events directory than the console reads.
. "$(dirname "${BASH_SOURCE[0]}")/bw-env.sh"

DEV="${1:-0}"
EV="$VAR/events/acars.jsonl"
AC="$TOOLS/acarsdec/build/acarsdec"

AP=""
cleanup(){ [ -n "$AP" ] && kill "$AP" 2>/dev/null; sleep 1; [ -n "$AP" ] && kill -9 "$AP" 2>/dev/null; return 0; }
trap cleanup EXIT INT TERM

# -o 4 = one JSON object per message on STDOUT.
# NOT -j: that is a UDP destination in acarsdec json format, not stdout.
"$AC" -r "$DEV" -g 42 -o 4 130.025 131.550 131.725 >> "$EV" 2>/dev/null &
AP=$!
wait $AP
