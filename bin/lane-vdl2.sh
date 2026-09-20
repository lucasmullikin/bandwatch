#!/bin/bash
# VDL Mode 2 -- the modern aircraft datalink replacing ACARS.
#
# Worth more than plain ACARS for one specific reason: every frame carries the
# aircraft's ICAO hex in the AVLC header, the SAME identifier ADS-B broadcasts.
# ACARS gives a tail number that has to be matched to a hex through a registry
# lookup that can be wrong; VDL2 gives the hex directly, so a text message
# joins to a tracked aircraft on a HEX MATCH rather than on inference.
#
# dumpvdl2 watches several channels at once inside one 1.05 Msps slice, so
# there is no reason to monitor only the common signalling channel.
#
# MEASURED 2026-09-01: signals arrive here at +6 to +8 dB above the noise
# floor. D8PSK needs roughly 15-20 dB, so this lane will decode nothing until
# the low-band antenna improves by about 10 dB -- which is what moving it
# outdoors typically buys. The lane is deliberately kept short until then, so
# it costs little while producing nothing.
set -uo pipefail

# Root, config and data directories, plus brew on PATH. One resolver, so a
# lane can never write to a different events directory than the console reads.
. "$(dirname "${BASH_SOURCE[0]}")/bw-env.sh"

DEV="${1:-0}"
EV="$VAR/events/vdl2.jsonl"

# The five US channels, all inside one tuner slice centred near 136.86.
CHANNELS="136725000 136775000 136875000 136975000 137000000"

# dumpvdl2 takes a device by ID or SERIAL. Serial is stable across the USB
# reordering that a replug causes; an index is not.
SERIAL=$(rtl_test -t 2>&1 | awk -v d="$DEV" '$1 == d":" {for(i=1;i<=NF;i++) if($i=="SN:") print $(i+1)}' | head -1)
[ -n "$SERIAL" ] || SERIAL="$DEV"

exec dumpvdl2 --rtlsdr "$SERIAL" --gain 49.6 \
     --output "decoded:json:file:path=$EV" \
     $CHANNELS
