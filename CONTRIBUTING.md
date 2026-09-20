# Contributing

Issues and pull requests are welcome. A few things are worth knowing before you
spend time on a change.

## The one rule

**A change that makes a failure quieter will be rejected**, however much code
it deletes.

Most of this codebase is shaped by failures that reported success: a supervisor
logging "healthy" with nothing running, lanes that had never once been tuned
while the console showed them green, a transcript column more than half
fabricated. `docs/LESSONS.md` is the catalogue.

So if a change turns an explicit failure into a default, a fallback, or a
swallowed exception, it needs to say in the PR why the new silence is safe.

Concretely, these will get pushed back on:

- adding a default for `station.lat` / `station.lon` / a device serial
- `except Exception: pass` around something that can fail meaningfully
- removing a negative control from a test because it "always passes"
- making a health check report a state it cannot actually observe

## Tests

```bash
./run-tests.sh
```

No radio, no network, no configured station. That is a hard requirement, not a
convenience — a test that needs your hardware only passes on your machine, and
this project already had several of those.

If you fix a bug, the test should fail before your fix. If you add a guard, add
the case where firing would be **wrong** — a guard nobody has watched decline to
fire is not a guard.

## Platform

macOS on Apple Silicon is what is tested. Linux PRs are genuinely welcome, but
please be explicit about what you verified and on what: an untested platform
claim is worse than an honest gap.

The seams are `bin/bw-supervise.sh` (supervision), `bin/bw-env.sh` (tool
discovery), and `workers/` (transcription). Lanes, broker, scheduler, event
store, analysis and console are already portable.

## Scope

Two things this project will not do, and PRs for them will be declined:

- **Defeating encryption.** A receivable carrier is not a readable one, and
  that difference is not an obstacle to route around.
- **Transmitting.** Receive only. No jamming, no GPS denial, no injection.

## Style

Match what is around you. In particular: comments here explain *why*, usually
by naming the failure that made the line necessary. A comment that restates the
code is noise; one that records what went wrong the first time is the most
valuable thing in the file.
