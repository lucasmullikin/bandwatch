#!/usr/bin/env bash
# Send a bandwatch alert through signal-cli's JSON-RPC daemon.
#
# Wire it up in config/bandwatch.json:
#     "notify": {
#       "enabled": true,
#       "notifier": "command",
#       "command": { "argv": ["/path/to/notify/examples/signal-cli.sh"] }
#     }
#
# and set SIGNAL_RPC / SIGNAL_RECIPIENT below, or export them in the
# environment the collector runs under. The recipient is deliberately NOT
# hard-coded here: a phone number in a repo is a phone number in everyone's
# clone, including every fork, forever.
#
# The daemon binds to loopback, so this has to run on the same host as
# signal-cli. It reads {"text": "..."} on stdin, which is what every bandwatch
# command notifier receives.
set -euo pipefail

RPC="${SIGNAL_RPC:-http://127.0.0.1:8085/api/v1/rpc}"
TO="${SIGNAL_RECIPIENT:-}"

[ -n "$TO" ] || { echo "SIGNAL_RECIPIENT is not set" >&2; exit 1; }

# Build the request with python rather than string interpolation: an alert
# carries decoded radio text, and pasting that into JSON by hand breaks on the
# first quote character and corrupts the message on every one after it.
python3 - "$RPC" "$TO" <<'PY'
import json, subprocess, sys, time
rpc, to = sys.argv[1], sys.argv[2]
text = (json.load(sys.stdin) or {}).get("text", "")
body = json.dumps({"jsonrpc": "2.0", "method": "send",
                   "params": {"recipient": [to], "message": text},
                   "id": "bandwatch-%d" % int(time.time())})
out = subprocess.run(
    ["curl", "-s", "-S", "--fail-with-body", "--max-time", "15",
     "-X", "POST", rpc, "-H", "Content-Type: application/json", "-d", body],
    capture_output=True, text=True)
sys.stdout.write(out.stdout.strip())
sys.exit(out.returncode)
PY
