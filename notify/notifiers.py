#!/usr/bin/env python3
"""Where an alert goes once the policy has decided to send it.

Three backends ship: a webhook, an arbitrary command, and none. The command
backend is the general one -- anything with a CLI is reachable through it,
including signal-cli, ntfy, a pager gateway, or a two-line shell script.

The contract every backend keeps:

  * send() returns (ok, detail). It never raises. A notifier that throws takes
    the collector down with it, and an alerting system that can kill the thing
    it is watching is worse than no alerting.
  * A failure is reported, never swallowed. `detail` is stored next to the
    alert so that "why did I not get paged" has an answer.
  * Delivered, failed and withheld-by-policy are THREE states, not two.
    Withholding an alert on purpose must not leave it looking undelivered, or
    the backlog grows forever and stops meaning anything. The policy layer owns
    "withheld"; this module only distinguishes delivered from failed.

To add a backend: write a function taking (conf, text) -> (ok, detail), add it
to BACKENDS, and document its config block in config/examples/bandwatch.json.
"""
import json
import subprocess


def _webhook(conf, text):
    """POST the alert as JSON to a URL.

    Uses curl rather than urllib so that a proxy, a custom CA or a corporate
    TLS setup is the operating system's problem and not this project's.
    """
    url = (conf.get("url") or "").strip()
    if not url:
        return False, "notify.webhook.url is empty"
    payload = json.dumps({"text": text})
    argv = ["curl", "-s", "-S", "--fail-with-body",
            "--max-time", str(conf.get("timeout_s", 10)),
            "-X", str(conf.get("method", "POST")), url,
            "-H", "Content-Type: application/json"]
    for k, v in (conf.get("headers") or {}).items():
        argv += ["-H", "%s: %s" % (k, v)]
    argv += ["-d", payload]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except Exception as e:                      # noqa: BLE001 - never raise
        return False, "webhook failed: %s" % e
    if out.returncode != 0:
        return False, "webhook HTTP failure (curl %d): %s" % (
            out.returncode, (out.stderr or out.stdout).strip()[:200])
    return True, (out.stdout or "").strip()[:200]


def _command(conf, text):
    """Run a program with the alert JSON on stdin.

    argv is a list, never a shell string: an alert message contains attacker-
    influenced text -- a transcript, a decoded pager page, a callsign -- and
    handing that to a shell is a command injection with extra steps.
    """
    argv = conf.get("argv") or []
    if not argv:
        return False, "notify.command.argv is empty"
    if isinstance(argv, str):
        return False, ("notify.command.argv must be a LIST of arguments, not a "
                       "string -- a shell string here would let a decoded "
                       "transmission run commands on your machine")
    try:
        out = subprocess.run([str(a) for a in argv],
                             input=json.dumps({"text": text}),
                             capture_output=True, text=True,
                             timeout=conf.get("timeout_s", 30))
    except Exception as e:                      # noqa: BLE001 - never raise
        return False, "command failed: %s" % e
    if out.returncode != 0:
        return False, "command exited %d: %s" % (
            out.returncode, (out.stderr or out.stdout).strip()[:200])
    return True, (out.stdout or "").strip()[:200]


def _none(conf, text):
    """Record the alert and push nothing.

    The honest default. Alerting you have not calibrated trains you to ignore
    it, so bandwatch ships with the push disabled and the console showing
    everything.
    """
    return True, "notifier disabled; alert recorded only"


BACKENDS = {"webhook": _webhook, "command": _command, "none": _none}


def send(cfg, text):
    """Deliver one alert. Returns (ok, detail); never raises.

    Config shape (flat, matching the rest of bandwatch.json):

        "notify_enabled": true,
        "notifier": "webhook",
        "notify": { "webhook": { "url": "..." } }
    """
    cfg = cfg or {}
    if not cfg.get("notify_enabled"):
        return _none({}, text)
    name = cfg.get("notifier") or "none"
    fn = BACKENDS.get(name)
    if fn is None:
        return False, ("unknown notifier %r; expected one of %s"
                       % (name, ", ".join(sorted(BACKENDS))))
    return fn((cfg.get("notify") or {}).get(name) or {}, text)
