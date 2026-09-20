# Lessons

Every entry here produced something that **looked healthy while doing nothing**.
That is the only membership criterion. A loud crash teaches you nothing worth
writing down; these are the failures that reported success.

They are grouped by what they teach rather than by which file they were found
in, because the shape recurs far more often than the specific bug does.

---

## The shape of the whole problem

> **Ask what this would look like if it had stopped working. If the answer is
> "exactly the same", that is the bug — whatever the code does.**

Three corollaries earned separately, each expensively:

**A guard you have never watched *decline* to fire is not a guard.** A
first-run suppression window returned `True` before checking whether it was
inside the window, silently swallowing every early alert. The positive test
passed. Nobody had run the negative.

**A monitor that cannot observe a negative result cannot detect drift.** If a
check can only ever report "fine", it reports "fine" after the thing it watches
is gone.

**A dead checker's last line is a permanent claim of health.** A watchdog that
crashed on startup left `"all devices producing"` in its log from a manual run
an hour earlier. Every human who looked, including the one who wrote it, read
that as current.

---

## Process detection deserves an explicit implementation

`pgrep` produced **three distinct silent failures** in this project. Not one
bug three times — three different holes.

1. **Two concurrent `pgrep -f <pattern>` processes match each other.** `pgrep`
   excludes *itself*, not its siblings. A supervisor polling every 30 s and a
   CLI running the same query meant the CLI saw the supervisor's `pgrep`,
   concluded the web server was already running, skipped starting it, and the
   supervisor then logged **"stack healthy again" with nothing listening.**
   Measured with a ghost process: literal pattern **8/8 false positives**,
   bracket pattern **0/8**.

   ```bash
   pgrep -f "webui/[s]erver.py"    # matches the real process; a pgrep's own
                                   # argv contains a literal [s] and does not
   ```

   The same hole means a genuinely dead component may **never** be restarted.

2. **`pgrep -a` does not exist on macOS.** It is Linux-only (`-l` here). With
   `-af` you get PIDs only, every match test fails, and a busy device reads as
   **free forever**.

3. **`pgrep` could not see its own parent.** A health probe recorded the
   supervisor as DOWN across ~48 consecutive samples while `ps` showed its PID
   and launchd reported it running. Reproducible from no other shell. The only
   distinguishing condition: being a **child of the process you are looking
   for, sharing its process group**.

**The fix was not a better pattern. It was to stop using `pgrep`** and scan
`ps -axo pid=,command=` in Python, where the matching rule is visible in code
instead of living in a BSD man page.

And: **a monitor that says "down" without recording WHAT IT LOOKED AT cannot be
debugged.** The third bug survived twelve hours until the check stored the PIDs
it had actually matched. Store the evidence beside the verdict.

---

## Scheduling: the lane that never got a turn

**Rotation that restarts at index 0 starves every late lane.** One device's
full cycle was 34 minutes, but restarts — supervisor repair, profile switch,
watchdog action, a live retune — arrived every few minutes. Lanes 4 through 8
**did not run once in a day.** Not "ran and heard nothing". *Never tuned.* The
console read "0 sensor events, 0 pages" and every health check was green.

Three parts to the fix, all necessary:

- Persist the cursor as a **lane ID**, not an index — editing a profile
  reorders the list.
- Write it **before** running the lane, not after.
- Carry it forward from persisted state at startup.

The moment it worked, a lane that had "never worked" produced valid decodes
immediately. It had never been broken.

**Every health check asked whether something that RAN produced output. None
asked whether a lane ran at all.** That gap is the entire hiding place. The
watchdog now faults any enabled lane unrun for three full cycles — and
**starvation must not trigger the restart repair, because restarting is what
causes it.**

**A profile can simply not contain the lane.** "Zero sensor events all day" was
a scheduling fact, not a hardware one: the daytime profile lacked the ISM
lanes. Check the profile before diagnosing the radio.

**A queue claim that excludes the asking worker's own rows starves it.** A
worker took the GPU lease, loaded a model, then found nothing to do.

**Lanes that exit on purpose are not failures.** `rtl_power -e 50` is *designed*
to stop. Mark them `self_terminating` or the supervisor fights them forever.

---

## Speech-to-text invents evidence

**Whisper hallucinates confidently on silence.** Thirty seconds of noise became
`"Thank you."` Across one airband sample, **59% of transcripts were
fabrication** — `"Deus Deus Deus"`, `"Hello Hello Hello"`.

Gate on the model's own `no_speech_prob`, `avg_logprob` and
`compression_ratio` — **and reject `NaN` explicitly.** `NaN` compares `False`
against every threshold, so a naive gate passes it straight through.

**Never prompt the model with your watchlist.** Priming it with a term makes it
emit that term. The hit is manufactured by the prompt, not heard on the air.
Transcribe clean; match afterwards.

**The hallucination corrupted a NUMERIC column, not just text.** The worker
reported duration as the end timestamp of the model's last segment; on a
fabricated transcript the invented segments run past the end of the audio, so
3.4-second clips were stored as 32.9 seconds — the same wrong value recurring
across unrelated channels, which is a hallucination fingerprint. The
`compression_ratio` gate correctly **rejected the transcript** and **the
fabricated duration was written anyway**, because the duration update never
asked whether the text had survived.

`COALESCE(measured, model)` — never the reverse.

---

## Measure it; do not derive it

**Duration from file size is broken on VBR.** Dividing by a fixed 32 kbps
assumed CBR; the encoder wrote **12.5–17 kbps on consecutive clips**. **93% of
stored durations were wrong**, some by 30 seconds. No divisor fixes this.

Measure with `ffprobe`, and **store NULL rather than a guess** when you cannot.
A queue treats NULL as "unknown, let it through"; a wrong number silently
mis-gates forever.

**A peak-hold sweep overstates a burst.** Six channels showed +16 dB on a 30 s
peak-hold. The recorded lane then produced **zero audio in 317 runs**, at both
11 dB and 7 dB squelch. A +16 dB peak is not a +16 dB transmission.

**`rtl_power` truncates its output**, so a line-count delta reads zero and a
working sweep flags itself failed. Use mtime.

---

## SDR-specific traps

**`rtl_airband` daemonises without `-F`.** The supervisor sees an instant exit
while the real process escapes supervision *still holding the dongle*.

**`rtl_airband` takes its device index from the CONFIG FILE.** Scanning argv
for `-d 0` reports the radio free while it is held.

**Both dongles ship as serial `00000001`.** Pin by serial, never by index:
librtlsdr's ordering is not stable across a replug, and a swapped index
presents as **bad reception**, never as an error.

**Centre the tuner on the block MIDPOINT.** The DC spike lands at the tuner
centre and will sit on whichever channel you centred on. Refuse spans over
~2.0 MHz — that is the *usable* passband at 2.4 MSPS, not 2.4.

**The first lane after any restart loses the USB race.** `usb_claim_interface
error -3`, dead in one second, slot burnt. Retry once on a fast non-zero exit.

**`rtl_433 -M time:iso` emits LOCAL time with no zone.** Mixed with UTC it
skews retention and staleness by exactly your UTC offset. Use `-M utc` and
normalise.

**A vertical dipole nulls at zenith.** For satellite work, a high pass is its
**worst** case, not its best — so "wait for a better pass" is backwards. An 82°
pass measured 1.6 dB SNR against the 6–8 dB needed and decoded zero bytes.

---

## macOS shell traps

**`timeout` does not exist.** The *shell* fails, and your probe prints
"closed" — a missing coreutil masquerading as a dead service.

**`set -o pipefail` plus `grep -q`**: grep closes the pipe, the producer dies
on SIGPIPE (141), and a working device reports missing. **Capture first, then
match.**

**bash 3.2.57 is what ships.** Under `set -u`, `"${EMPTY_ARRAY[@]}"` is an
*unbound variable error*, not an empty expansion. An empty optional-args array
killed a lane instantly on every run. Reproduce before blaming the program:

```bash
bash -c 'set -u; A=(); echo "${A[@]}"'
```

**`nohup` fails outright under launchd.** No controlling terminal, so BSD
`nohup` prints `can't detach from console: Inappropriate ioctl for device` and
**never runs the command**. Moving a supervisor from LaunchAgent to
LaunchDaemon meant it could start *nothing* — the whole stack down while
launchd correctly reported the supervisor healthy. Invisible for as long as it
ran in the GUI domain, where a terminal existed.

```bash
( trap '' HUP; exec cmd >>log 2>&1 </dev/null ) &   # same immunity, no console
```

**`nc` exits on stdin EOF.** Under `nohup`, stdin is `/dev/null`, so a tap dies
instantly and takes its producer with it. `nc -d` is mandatory — but macOS `nc
-d` then **busy-spins on the socket**, burning 95% of a core to move 658
bytes/sec. `socat -u` is unidirectional, never opens stdin, and measured
identical throughput at 0.0% CPU.

**`pkill -f <name>` matches your own shell**, and **`kill 0` signals the
caller's entire process group.** Both killed live SSH sessions. Kill named
PIDs.

**A remote port probe cannot see a loopback bind.** A service on
`127.0.0.1:8085` reads "closed" from every other host while perfectly healthy.
Call it from the box it lives on; an error *reply* is liveness proof, an open
port is not.

---

## Tests and schemas

**`CREATE TABLE IF NOT EXISTS` does not add columns.** A schema change stays
invisible until something queries the new column and fails.

**`sqlite3.connect()` CREATES the file.** A guard that checks whether the
database exists is satisfied by the empty database an earlier buggy run
created. Ask for the **table**, not the file. (This one bit again while
preparing this repository for release.)

**Anchor a source-order assertion on the CODE, not on a mention of the name.**
A test asserting a filter sat below an INSERT searched for the config key
`no_notify_rules` and matched a schema *comment* above the insert instead, so
it compared the wrong two positions and failed for the wrong reason. The
comment explaining this was already in the file, and got re-broken anyway while
rewriting the test.

**A grep-for-completion watcher matched YESTERDAY's log line** and reported a
run that never happened. Anchor on a timestamp or a byte offset, never on a
pattern that history also satisfies.

**A test that reads your live config or your production database can only pass
on your machine.** Several here did. They are now either fixture-driven or
skip cleanly — but a skip that nobody counts is a test that has quietly stopped
asserting anything, so `run-tests.sh` prints the skips.

**`freq` matches inside `centerfreq`.** Substring matching without a word
boundary made every config report a channel on DC.

---

## Data modelling

**A column that was never populated looks exactly like a quiet band.** One
frequency column was `NULL` on all 413 rows since the table was created: the
writer read a config key that was never set. Any frequency-keyed view returned
nothing, indistinguishable from "nobody transmitted".

**An alert silenced on purpose must not share a state with one that failed to
send.** Withheld alerts left at `notified=0` made the undelivered backlog grow
forever and mean nothing. Three states: `0` pending/failed, `1` delivered,
`2` withheld-by-policy.

**Never mark delivered before confirming delivery.** A watchlist alert fired
`curl` and set `notified=1` unconditionally. An unreachable notifier produced a
row claiming the alert had been sent — the worst available state, because
nothing retries and nothing looks wrong.

**A generator whose output gets hand-edited has no source of truth.** The
profile generator carried its catalogue inline while lanes were tuned directly
in the generated JSON. Within weeks regenerating would have silently deleted
six working lanes, and the only honest thing the generator could do was refuse
to run. The catalogue now lives in config; the output is disposable.

---

## Front end

**HTML inserted after the `<script>` tag kills every `let` below the first
`getElementById`.** Markup placed past `</script>` meant a null dereference
aborted top-level evaluation — but **hoisted functions still existed, so the
page looked completely normal** while an entire feature never initialised. Put
markup before the script.

**Discovery must not be gated on configuration.** Claiming a device should
change how it is *labelled*, never whether it is *shown*. An ownership filter
applied one line too early turns "we heard 14 devices, 4 are yours" into "we
heard 4 devices", and the panel looks entirely normal. There are now tests in
both directions.
