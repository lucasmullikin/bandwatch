#!/bin/bash
# UAT 978 MHz -- US general aviation ADS-B, plus TIS-B and FIS-B.
#
# Complements 1090 rather than duplicating it. The FAA assigned 1090 to all
# flight levels and UAT only below 18,000 ft, so this band carries the small
# aircraft that 1090 largely does not: light singles, helicopters, gliders.
#
# It also carries two things 1090 never does:
#   TIS-B  traffic as GROUND RADAR sees it, rebroadcast -- including aircraft
#          with no ADS-B transmitter of their own
#   FIS-B  weather uplinked from ground stations: NEXRAD radar imagery,
#          METARs, TAFs, NOTAMs. Ground-based and always on, which makes it
#          the one weather-imagery source here that needs no satellite.
#
# Uses the rtl_sdr pipe rather than dump978's --sdr mode: SoapySDR on this
# machine searches /usr/local/lib for its modules while homebrew installs them
# under $HOME, so --sdr finds no device. The pipe needs no such plumbing.
set -uo pipefail

# Root, config and data directories, plus brew on PATH. One resolver, so a
# lane can never write to a different events directory than the console reads.
. "$(dirname "${BASH_SOURCE[0]}")/bw-env.sh"

DEV="${1:-1}"
EV="$VAR/events/uat978.jsonl"
D978="$TOOLS/dump978/dump978-fa"

RS=""; DD=""
cleanup() {
  [ -n "$DD" ] && kill "$DD" 2>/dev/null
  [ -n "$RS" ] && kill "$RS" 2>/dev/null
  return 0
}
trap cleanup EXIT INT TERM

# 2.083334 Msps is the rate UAT's 1.041667 Mbit/s signalling requires; it is
# not a round number and must not be "tidied".
rtl_sdr -d "$DEV" -f 978000000 -s 2083334 -g 48 - 2>/dev/null | \
  "$D978" --stdin --format CU8 --json-stdout >> "$EV" 2>/dev/null &
DD=$!
wait $DD
