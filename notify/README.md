# Alerting

Alerting is **off by default**, and that is a design position rather than a
convenience. An alert path you have not calibrated teaches you to ignore it,
and once you ignore it the useful alert arrives and nothing happens.

Turn it on after you have watched the rules fire in the console for a while and
agree with what they chose.

## Backends

`notify/notifiers.py` ships three, selected by `notify.notifier` in
`config/bandwatch.json`:

| backend | what it does |
|---|---|
| `none` | record the alert, push nothing. The default. |
| `webhook` | `POST {"text": "..."}` to a URL. ntfy, Discord, Slack, your own endpoint. |
| `command` | run a program with `{"text": "..."}` on stdin. Anything with a CLI. |

`notify/examples/signal-cli.sh` is a worked `command` backend for Signal.

## Adding one

Write a function taking `(conf, text)` and returning `(ok, detail)`, add it to
`BACKENDS`, and document its config block in
`config/examples/bandwatch.json`.

Three rules it has to keep:

1. **Never raise.** A notifier that throws takes the collector down with it. An
   alerting system that can kill the thing it watches is worse than none.
2. **Report failures, never swallow them.** `detail` is stored beside the alert
   so "why was I not paged" has an answer.
3. **`argv` is a list, never a shell string.** An alert carries a transcript, a
   decoded pager page, a callsign — text that arrived over the air from someone
   you do not control. Handing that to a shell is command injection with extra
   steps.

## Three states, not two

Delivered, failed, and withheld-by-policy are distinct. Withholding an alert on
purpose must not leave it looking undelivered — do that and the backlog grows
forever and stops meaning anything. This bit was learned the hard way.
