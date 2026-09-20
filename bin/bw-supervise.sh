#!/bin/bash
# Supervisor: keeps broker, collector and webui alive, and restarts any that die.
#
# launchd's KeepAlive cannot supervise these directly -- bandwatch start spawns
# three background processes and exits, so launchd would see it finish and
# respawn it forever. This stays resident instead and repairs what is missing.
set -uo pipefail

# Root, config and data directories, plus brew on PATH. One resolver, so a
# lane can never write to a different events directory than the console reads.
. "$(dirname "${BASH_SOURCE[0]}")/bw-env.sh"
# launchd hands a process a minimal PATH, so the brew prefix is added by
# bw-env.sh above rather than assumed here.
export PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin"

PROFILE="${BANDWATCH_PROFILE:-watch-day}"

LOG="$VAR/logs/supervise.log"
INTERVAL=30

mkdir -p "$ROOT/var/logs"
say(){ echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" >> "$LOG"; }

# The profile to restore is NOT the env default. Precedence:
#   1. the schedule / manual override  -- the only authority on what should run
#   2. whatever was running before      -- so a repair never changes the mode
#   3. BANDWATCH_PROFILE                      -- cold boot with no state yet
# Trusting the env default made a repair silently revert a hand-picked mode.
desired_profile(){
  local w c
  w="$("$ROOT/bin/schedule_check.py" --want 2>/dev/null)"
  if [ -n "$w" ] && [ -f "$PROFILE_DIR/$w.json" ]; then echo "$w"; return; fi
  c="$(python3 -c "import json;print(json.load(open('"'"'$ROOT/var/state.json'"'"')).get('"'"'profile'"'"') or '"'"''"'"')" 2>/dev/null)"
  if [ -n "$c" ] && [ -f "$PROFILE_DIR/$c.json" ]; then echo "$c"; return; fi
  echo "$PROFILE"
}

LAST_PRUNE=0
LAST_WATCHDOG=0
LAST_HEALTH=0
say "supervisor up (default=$PROFILE, pid $$)"
# a boot can race the USB enumerating the dongles
sleep 20

while true; do
  # one clock reading per pass, taken BEFORE anything that compares against it.
  # NOW_W used to be assigned below the watchdog block that read it, so under
  # `set -u` the loop exited at that line on EVERY iteration -- the supervisor
  # was killed by launchd, its process group went with it, and the whole stack
  # was restarted every ~20s. The watchdog never once ran.
  NOW_W=$(date +%s)
  NOW=$NOW_W

  missing=""
  pgrep -f "[b]roker.py"        >/dev/null 2>&1 || missing="$missing broker"
  pgrep -f "[c]ollector.py"     >/dev/null 2>&1 || missing="$missing collector"
  pgrep -f "webui/[s]erver.py"  >/dev/null 2>&1 || missing="$missing webui"

  # A component switched off on purpose is not a fault. Restarting a CRASHED
  # broker is this loop's whole job -- it is what made last night's collector
  # crash-loop survivable -- but it must not fight the operator's own off
  # switch. bandwatch stop leaves this marker; bandwatch start clears it.
  # The marker's FIRST FIELD says what was deliberately stopped: "all" for a
  # full bandwatch stop, "broker" for radios-off. Anything not named is still
  # guarded -- a radios-off must not also stop us restarting a CRASHED webui,
  # because that webui is the only way back to switching scanning on.
  if [ -n "$missing" ] && [ -f "$VAR/stopped" ]; then
    scope="$(awk 'NR==1{print $1}' "$VAR/stopped" 2>/dev/null)"
    if [ "$scope" = "all" ]; then
      kept=""
    else
      # Token filter in bash, NOT sed: BSD sed has no \b, so the word-boundary
      # form silently matched nothing and left `missing` untouched -- the
      # supervisor kept restarting a broker that was switched off on purpose,
      # while the substitution looked correct.
      kept=""
      for _m in $missing; do
        [ "$_m" = "$scope" ] || kept="$kept $_m"
      done
    fi
    if [ "$kept" != "$missing" ] && [ "${SAID_STOPPED:-0}" = "0" ]; then
      say "deliberately stopped ($(head -1 "$VAR/stopped" 2>/dev/null)) -- not restarting:$missing"
      SAID_STOPPED=1
    fi
    missing="$kept"
  elif [ ! -f "$VAR/stopped" ]; then
    SAID_STOPPED=0
  fi

  if [ -n "$missing" ]; then
    say "missing:$missing -- restarting the stack"
    "$ROOT/bin/bandwatch" stop  >> "$LOG" 2>&1
    sleep 3
    want_now="$(desired_profile)"
    say "restoring profile: $want_now"
    "$ROOT/bin/bandwatch" start "$want_now" >> "$LOG" 2>&1
    sleep 10
    still=""
    pgrep -f "[b]roker.py"       >/dev/null 2>&1 || still="$still broker"
    pgrep -f "webui/[s]erver.py" >/dev/null 2>&1 || still="$still webui"
    [ -n "$still" ] && say "RESTART DID NOT TAKE:$still" || say "stack healthy again"
  fi
  # schedule / override: prints a profile name ONLY when a change is due
  want="$("$ROOT"/bin/schedule_check.py 2>/dev/null || true)"
  if [ -n "$want" ] && [ -f "$PROFILE_DIR/$want.json" ]; then
    say "schedule wants $want -- switching"
    "$ROOT/bin/bandwatch" profile "$want" >> "$LOG" 2>&1
    sleep 10
  fi

  # satwatch: an OPTIONAL consumer, deliberately NOT in the `missing` list
  # above. That check restarts broker+collector+webui together, and taking the
  # radios down to recover a weather-satellite scheduler would be the wrong
  # trade -- worst case it would happen during the pass it exists to catch.
  if ! pgrep -f "[s]dr-satwatch.py" >/dev/null 2>&1; then
    if [ "$(python3 -c 'import json;print(json.load(open("'"$CONFIG"'/bandwatch.json")).get("sat_auto_capture", True))' 2>/dev/null)" = "True" ]; then
      say "satwatch not running -- starting it"
      ( trap '' HUP; exec "$ROOT/bin/bw-satwatch.py" \
          >> "$VAR/logs/satwatch.log" 2>&1 < /dev/null ) &
      sleep 2
    fi
  fi

  # health snapshot every 15 min. Every tuning decision made so far rested on
  # a number somebody measured by hand and then lost. This records them so
  # "is it better now?" is a query rather than another manual dig. Cheap and
  # side-effect free: reads tables and process state, takes no radio.
  if [ $((NOW_W - LAST_HEALTH)) -ge 900 ]; then
    LAST_HEALTH=$NOW_W
    "$ROOT/bin/bw-health.py" --quiet >> "$VAR/logs/health.log" 2>&1 || true
  fi

  # T23 watchdog every 5 min. The supervisor above only proves processes EXIST;
  # this proves the radios PRODUCE. A stale pause flag once idled a device for
  # three hours with every process alive and status reporting healthy.
  if [ $((NOW_W - LAST_WATCHDOG)) -ge 300 ]; then
    LAST_WATCHDOG=$NOW_W
    WD="$("$ROOT/bin/bw-watchdog.py" --quiet 2>&1)"
    if echo "$WD" | grep -q "FAULT"; then
      say "watchdog: $(echo "$WD" | grep -E "FAULT|REPAIR|NOTIFY" | tr "\n" " ")"
    fi
  fi

  # disk budget, hourly. Refuses outright if the budget is unreachable rather
  # than deleting everything chasing a target it cannot hit.
  if [ $((NOW - LAST_PRUNE)) -ge 3600 ]; then
    LAST_PRUNE=$NOW
    OUT="$("$ROOT/bin/bw_prune.py" --apply 2>&1)"
    if echo "$OUT" | grep -qE "CONFIG ERROR|evicted"; then
      say "prune: $(echo "$OUT" | grep -E "CONFIG ERROR|evicted|freed|OVER BY" | tr "\n" " ")"
    fi
  fi

  sleep "$INTERVAL"
done
