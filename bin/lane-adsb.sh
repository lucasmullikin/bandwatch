#!/bin/bash
# ADS-B lane: readsb owns one dongle, a BaseStation tap appends one line per
# message so the broker counts events like any other lane.
#
# --device=$DEV is MANDATORY. Without it readsb opens device #0 regardless of
# which radio the profile assigned, collides with whatever holds that radio,
# and dies with usb_claim_interface error -3 every single run.
#
# The tap is socat, not nc. `nc -d` WAS mandatory here -- without it nc reads
# stdin, and under nohup stdin is /dev/null, so nc sees instant EOF, exits and
# takes readsb down with it. But macOS nc with -d busy-spins on the socket: it
# burned 95% of a core to move 658 bytes/sec (measured 2026-09-18). socat -u is
# unidirectional and never opens stdin at all, so the EOF trap cannot apply.
# Verified against the live nc tap: identical byte counts over 50s, 0.0% CPU.
# Kill named PIDs on exit -- never `kill 0`, which signals the caller's group.
set -uo pipefail

# Root, config and data directories, plus brew on PATH. One resolver, so a
# lane can never write to a different events directory than the console reads.
. "$(dirname "${BASH_SOURCE[0]}")/bw-env.sh"

DEV="${1:-0}"
EV="$VAR/events/adsb.sbs"
PORT=$((30003 + DEV))          # per-device port so two radios never collide

RB=""; TAP=""
cleanup() {
  [ -n "$TAP" ] && kill "$TAP" 2>/dev/null
  [ -n "$RB" ] && kill "$RB" 2>/dev/null
  sleep 1
  [ -n "$RB" ] && kill -9 "$RB" 2>/dev/null
  return 0
}
trap cleanup EXIT INT TERM

# Community feed targets. Both are UNFILTERED aggregators: they do not honour
# aircraft blocking requests, which is why they are the ones useful for
# accountability work. FlightAware and FR24 are deliberately not here.
#
# beast_out is an outbound push from the readsb already running for the local
# lane -- no extra process and no extra radio time. silent_fail means an
# unreachable aggregator is a non-event, not a lane failure.
#
# MLAT IS DELIBERATELY NOT CONNECTED. It needs the receiver's true
# coordinates, and feeding a deliberately offset position would corrupt other
# stations' solutions rather than protect this one. ADS-B accuracy does not
# depend on receiver position at all, so nothing is lost.
FEEDS=""
if [ "${ADSB_FEED:-1}" = "1" ]; then
  FEEDS="--net-connector=feed.airplanes.live,30004,beast_out,silent_fail"
  FEEDS="$FEEDS --net-connector=feed.adsbexchange.com,30005,beast_out,silent_fail"
fi

readsb --device-type=rtlsdr --device="$DEV" --gain=auto --net \
       --net-sbs-port=$PORT $FEEDS --quiet &
RB=$!

# wait for the SBS port rather than a blind sleep
for _ in $(seq 1 20); do
  nc -z -G 1 -w 1 127.0.0.1 $PORT 2>/dev/null && break
  kill -0 $RB 2>/dev/null || { echo "readsb died before opening $PORT" >&2; exit 1; }
  sleep 1
done

socat -u TCP:127.0.0.1:$PORT OPEN:"$EV",append,create &
TAP=$!
wait $TAP
