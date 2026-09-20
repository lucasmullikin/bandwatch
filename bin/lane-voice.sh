#!/bin/bash
# Voice lane: rtl_airband demodulates every channel in the config simultaneously
# and writes one MP3 per transmission (squelch-gated, split_on_transmission).
#
# -F is MANDATORY: with no foreground flag rtl_airband DAEMONISES -- the parent
# forks and returns, the broker sees an instant exit, and the real process
# escapes supervision while still holding the dongle. -F is foreground without
# the ncurses waterfall (-f would spray escape codes into the log). -e sends
# messages to stderr instead of syslog so the lane log is useful.
# Kill the named PID on exit -- never `kill 0`.
set -uo pipefail

# Root, config and data directories, plus brew on PATH. One resolver, so a
# lane can never write to a different events directory than the console reads.
. "$(dirname "${BASH_SOURCE[0]}")/bw-env.sh"

DEV="${1:-0}"
TMPL="${2:-$CONF_DIR/frs.conf}"
AB="$TOOLS/RTLSDR-Airband/build/src/rtl_airband"
# template must END in XXXX or mktemp does not substitute
CONF="$(mktemp /tmp/airband_dev${DEV}_XXXXXX)"

AP=""
cleanup(){ [ -n "$AP" ] && kill "$AP" 2>/dev/null; sleep 1; [ -n "$AP" ] && kill -9 "$AP" 2>/dev/null; rm -f "$CONF"; return 0; }
trap cleanup EXIT INT TERM

sed "s/{DEV}/$DEV/g" "$TMPL" > "$CONF"
"$AB" -F -e -c "$CONF" &
AP=$!
wait $AP
