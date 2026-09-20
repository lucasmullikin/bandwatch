#!/usr/bin/env bash
# Install the supervisor as a launchd job so the station survives a reboot.
#
#   sudo bin/bw-install-daemon.sh            install and start
#   sudo bin/bw-install-daemon.sh --uninstall
#
# A LaunchDAEMON, not a LaunchAgent, and that distinction is the whole point:
# an Agent lives in the GUI domain and dies when its user's console session
# ends, so logging in as a different user silently takes the station down.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/bw-env.sh"

LABEL="com.bandwatch.supervise"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
RUN_USER="${SUDO_USER:-$USER}"

die() { echo "bandwatch: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root: sudo $0"
[ "$RUN_USER" != "root" ] || die "refusing to run the station as root.
  Invoke with sudo from your normal account so SUDO_USER is set."

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "system/$LABEL" 2>/dev/null
  rm -f "$PLIST"
  echo "uninstalled $LABEL"
  exit 0
fi

[ -x "$HERE/bw-supervise.sh" ] || die "missing $HERE/bw-supervise.sh"

# An existing LaunchAgent with the same job would be reloaded at the next
# console login and run a SECOND supervisor against the same dongles. Move it
# aside rather than leaving it to collide.
AGENT="/Users/$RUN_USER/Library/LaunchAgents/$LABEL.plist"
if [ -f "$AGENT" ]; then
  launchctl bootout "gui/$(id -u "$RUN_USER")/$LABEL" 2>/dev/null
  mv "$AGENT" "$AGENT.disabled-$(date +%Y%m%d%H%M%S)"
  echo "moved aside an old LaunchAgent -- two supervisors would fight over the radios"
fi

mkdir -p "$BANDWATCH_VAR/logs"
chown -R "$RUN_USER" "$BANDWATCH_VAR" 2>/dev/null || true

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>

  <!-- Run as the operator, never as root. The station needs USB and a home
       directory, not privilege. -->
  <key>UserName</key><string>$RUN_USER</string>

  <key>ProgramArguments</key>
  <array>
    <string>$HERE/bw-supervise.sh</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>BANDWATCH_ROOT</key><string>$BANDWATCH_ROOT</string>
    <key>BANDWATCH_CONFIG</key><string>$BANDWATCH_CONFIG</string>
    <key>BANDWATCH_VAR</key><string>$BANDWATCH_VAR</string>
    <key>BANDWATCH_TOOLS</key><string>$BANDWATCH_TOOLS</string>
  </dict>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>

  <!-- launchd hands a job a minimal PATH; the supervisor adds the brew prefix
       itself via bw-env.sh, but the base has to be sane. -->
  <key>WorkingDirectory</key><string>$BANDWATCH_ROOT</string>
  <key>StandardOutPath</key><string>$BANDWATCH_VAR/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$BANDWATCH_VAR/logs/launchd.err</string>
</dict>
</plist>
PLIST_EOF

chown root:wheel "$PLIST"
chmod 644 "$PLIST"

launchctl bootout "system/$LABEL" 2>/dev/null
launchctl bootstrap system "$PLIST" || die "bootstrap failed -- see $PLIST"
launchctl enable "system/$LABEL" 2>/dev/null

echo "installed $LABEL"
echo
echo "Verify it is ACTUALLY running, not merely loaded:"
echo "    launchctl print system/$LABEL | head -20"
echo "    tail -f $BANDWATCH_VAR/logs/launchd.err"
echo
echo "A job can report healthy while starting nothing -- check the supervisor's"
echo "own log says it started lanes, and that 'bandwatch status' agrees."
