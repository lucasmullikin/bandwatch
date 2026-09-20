# bandwatch

A supervised, always-on receiver for cheap SDR hardware. Two RTL-SDR dongles,
a dozen or more decoders, a rotation scheduler that provably does not starve
any lane, a SQLite event store, and a console that tells you what it *did not*
hear as readily as what it did.

**macOS / Apple Silicon.** See [Platform](#platform) — this is stated up front
rather than discovered later.

```
bandwatch start [profile]   run broker + collector
bandwatch stop              stop everything, release every dongle
bandwatch status            per-device lane health, with zero-event alarms
bandwatch lanes             what each device rotates through
bandwatch events            what has actually been heard
bandwatch device            prove the dongles are present and free
```

---

## The problem this actually solves

Pointing a receiver at a frequency is easy. Running one unattended for months
without it quietly lying to you is not.

Almost everything here exists because a previous version of it reported perfect
health while doing nothing. A supervisor that logged *"stack healthy again"*
with nothing listening. Five lanes that had never once been tuned while the
console showed them green. A transcript column where 59% of the entries were
fabricated by a speech model confidently hallucinating on silence. A duration
column that was wrong on 93% of rows.

So the design question throughout is not "does this work?" but **"what would
this look like if it had stopped working?"** — and if the answer is "exactly
the same", that is the bug, whatever the code does.

[`docs/LESSONS.md`](docs/LESSONS.md) is the catalogue. It is the most
transferable thing in the repository and reads perfectly well if you never run
the software.

## Why it is shaped this way

**A dongle is exclusive.** Two processes cannot open the same one. The broker
holds a `flock` per device and grants it to one lane at a time.

**One dongle is one 2.4 MHz slice — but everything inside that slice at once.**
Three airband channels 1.5 MHz apart are simultaneous and free. Airband to
military air (282 MHz apart) is not, at any price.

**Rotation, not preemption.** Priority scheduling starves low-priority lanes
while every health check reads green. Fixed rotation is the only policy whose
coverage you can actually state: 1/N of wall clock, and you know what you
missed.

**The rotation cursor is a lane ID, persisted before the lane runs.** An index
reorders when you edit a profile; a cursor that resets to 0 on restart starves
everything after position 3, and restarts happen every few minutes. This one
cost five lanes a full day of never being tuned while the console showed them
healthy.

**Voice cannot be sliced.** A transmission is 2–15 s, unpredictable, and never
repeats — miss the window and it is gone. Sensors repeat every 30–60 s and
ADS-B several times a second, so those rotate happily. Voice needs its own
radio.

**Split by antenna band, not by purpose.** You cannot retune an antenna between
duty cycles, and λ/4 runs from 6.9 cm at 1090 MHz to 63 cm at 118 MHz. A dongle
on a fixed lane gets a tuned antenna; a rotating dongle needs a wideband one.

**DSP detects. A model only ever summarises.** No LLM sits in the ingest path,
so events keep logging when every model host is down, and nothing that reaches
the event store was invented by a language model.

## What it receives

| Lane | What it gets you |
|---|---|
| Airband voice (AM) | tower, ground, approach |
| Military air (AM, UHF) | unencrypted, and frequently active |
| FRS / GMRS / MURS | handhelds; "privacy codes" are CTCSS, not encryption |
| 121.5 / 243.0 guard | distress. Silent until it is not |
| ADS-B 1090 + UAT 978 | aircraft, including a **structured** emergency squawk |
| POCSAG / FLEX paging | dispatch traffic in plain text — no ASR, no transcription error |
| rtl_433 on 315/433/915 | tyre sensors, weather stations, utility meters |
| rtl_power sweeps | occupancy: which channels are live *before* you record them |
| ACARS / VDL2 / APRS | aircraft text and ham telemetry |

Two lanes exist to be **known-good controls**: NOAA weather radio transmits
24/7 and FM broadcast always has carriers. If either goes quiet, the radio is
broken — not the band. Every station needs at least one lane whose silence is
unambiguous.

## Quickstart

```bash
git clone https://github.com/lucasmullikin/bandwatch
cd bandwatch
./bootstrap.sh            # installs decoders via brew, creates config/
```

Then edit three things, in this order:

1. **`config/bandwatch.json`** — `station.lat`, `station.lon`, `timezone`.
   There is no default and the code refuses to start without them. Every
   distance and every satellite pass is measured from that point, so a guessed
   coordinate does not fail — it answers confidently about somewhere you are
   not.

2. **`config/bandplan.json`** — what you actually record. Three sets ship
   marked `"replace_me"` (local tower, ground, military air) and
   `make_confs.py` refuses to generate them until you edit or delete them.
   Everything else — FRS, GMRS, MURS, both guard channels — is a national or
   international allocation and works as shipped in the US.

   (`config/stations.json` is a separate, wider *reference* list shown in the
   Tune panel. It records nothing. Keeping the two apart is deliberate: the
   menu should be longer than the meal, so you can see what you are choosing
   not to capture.)

3. **`config/lanes.json`** — each dongle's **serial**. Two dongles arrive from
   the factory reporting the same one, and librtlsdr's index order is not
   stable across a replug. A swapped index presents as *bad reception*, never
   as an error.

   ```bash
   rtl_test -t                        # what have you got
   rtl_eeprom -d 0 -s 00000001        # give them distinct serials
   ```

Then:

```bash
python3 make_confs.py     # band plan  -> rtl_airband configs
python3 make_profiles.py  # lanes      -> rotation profiles
bin/bandwatch start
bin/bandwatch status
```

The console is on `http://127.0.0.1:9111`.

**Before you enable transcription or alerting, read
[`docs/COVERAGE.md`](docs/COVERAGE.md).** Both are off by default and both
deserve the ten minutes.

## Configuration lives outside the code

`BANDWATCH_CONFIG` points at your station's configuration — coordinates, band
plan, watchlist, claimed sensors, notifier credentials. It defaults to
`config/`, which is gitignored except for the worked examples.

That split is what makes this repo publishable at all, and it is the same
mechanism that lets you keep your own configuration in a private repo while
tracking this one:

```bash
export BANDWATCH_CONFIG=~/my-station-config
bin/bandwatch start
```

See [`config/README.md`](config/README.md).

## Alerting

Off by default, and that is a design position: an alert path you have not
calibrated teaches you to ignore it, and then the one that mattered arrives and
nothing happens.

Three backends — `none`, `webhook`, and `command` (run any program with the
alert on stdin, which reaches anything with a CLI). See
[`notify/README.md`](notify/README.md).

Withheld-by-policy, delivered, and failed are three distinct states. Collapsing
the first two into one makes an undelivered backlog that grows forever and
means nothing.

## Who can reach the console

The console can start and stop the pipeline, take a radio out of the rotation
and retune it, silence alerting, and exempt recordings from the retention
sweep. So:

- **It binds `127.0.0.1` by default** — this machine only. To reach it from
  another device, tunnel rather than expose it:
  `ssh -L 9111:127.0.0.1:9111 <host>`
- **Reading is never gated.** A console you cannot look at is not a console.
- **Changing anything requires `ui_password`** whenever one is set.
- **bandwatch refuses to start** bound off-loopback with no password, rather
  than warning about it in a log nobody reads.

The **Settings** tab reports the current posture and how to change it. It
deliberately cannot change it itself: a console that can unlock itself is not
an access control, and a bind address can only take effect at startup.

A password over plain HTTP stops a casual visitor on your network. It does not
hide anything from someone who can watch the traffic — loopback plus a tunnel
is the stronger arrangement, and the Settings tab says so rather than implying
otherwise.

Transcription workers authenticate separately, with a token generated on first
use and stored `0600` outside git — `/api/transcript` rewrites the record of
what a transmission said, so it was never left open.

## Transcription

Optional, off by default, and the one part of this system capable of inventing
evidence.

Whisper hallucinates confidently on silence — 30 seconds of noise became
*"Thank you."* Output is gated on the model's own `no_speech_prob`,
`avg_logprob` and `compression_ratio`, with `NaN` rejected explicitly, because
`NaN` compares `False` against every threshold and sails through a naive gate.

**The watchlist is never passed to the model as a prompt.** Priming it with a
term makes it emit that term; the hit would be manufactured by the prompt
rather than heard on the air. Transcribe clean, match afterwards. Any change
that passes watchlist terms in as context is a bug, not an optimisation.

Workers run as separate processes, usually on a different machine — a receiver
is a cheap always-on box, a GPU is not. See [`workers/`](workers/).

## Standard of proof

[`docs/COVERAGE.md`](docs/COVERAGE.md) is not a disclaimer appended to the end
of a project. It is a design constraint that shaped the code, and the short
version is:

- **A transcript is a lead, never a fact.** It is a statistical reconstruction
  by a model that demonstrably invents text.
- **A sensor ID identifies a device, never a person.** A TPMS reading is a tyre
  sensor. Receiving one is not knowing whose car it is, and the code will not
  claim a device is yours unless you name it explicitly.
- **Coverage is sampled, and the sampling is stated.** A rotating lane hears
  1/N of wall clock. "Nothing heard" means "nothing heard during our slice" —
  the console reports it that way because the distinction is the whole point.
- **Encrypted traffic stays encrypted.** A receivable carrier is not a readable
  one, and the difference is not a technical obstacle to route around.
- **Receiving and republishing are different questions**, with different
  answers, in every jurisdiction.

The legal section is worked through for **Idaho, US**, and is labelled as such.
It is not legal advice and it does not travel. The reasoning is shown so you
can redo it for where you actually are.

## Why build it at all

The same hardware that makes a good hobby receiver makes a decent
accountability instrument. Public-safety aviation, military air, and dispatch
paging are all unencrypted and all matter to people who are trying to establish
what happened and when. A record that is honest about its own gaps is worth
considerably more than one that quietly implies completeness.

That is also why the coverage accounting is not optional decoration: a claim
about what was *not* heard is only as good as your knowledge of when you were
listening.

## Platform

macOS on Apple Silicon. Tested nowhere else, and claimed nowhere else.

Most of the SDR world runs Linux, so this is a real limitation rather than a
preference. It comes from launchd supervision, CoreAudio, mlx-whisper, and a
long tail of BSD-versus-GNU shell behaviour the code works around explicitly
(see `docs/LESSONS.md` — several of those traps *are* the macOS differences).

Platform-specific pieces are isolated so a Linux port has an obvious seam:
supervision in `bin/bw-supervise.sh` and the launchd plist, tool discovery in
`bin/bw-env.sh`, transcription in `workers/`. The lanes, broker, scheduler,
event store, analysis and console are portable already. PRs welcome; I cannot
test them.

## Requirements

- macOS, Apple Silicon
- **Python 3.9+, no packages.** The receiver, broker, scheduler, console and
  analysis are pure standard library. Only the optional transcription workers
  want third-party code.
- Two RTL-SDR v3 dongles with **distinct serials**
- Decoders via `bootstrap.sh`: `rtl-sdr`, `rtl_433`, `sox`, `socat`, `ffmpeg`,
  `lame`, `mosquitto`; optionally `direwolf`, `multimon-ng`, `dump978`,
  `satdump`, plus `RTLSDR-Airband`, `acarsdec`, `dumpvdl2` and `readsb` built
  from source
- Antennas are the binding constraint. A discone is worth more than a third
  dongle.

## Tests

```bash
./run-tests.sh
```

339 tests. No radio, no network and no configured station required — which is
also what CI runs on every push.

Some tests **skip** rather than fail without a live station: the ones auditing
a real event store against real audio on disk. A skip there is correct; the
count is printed rather than hidden, because a suite that has quietly stopped
asserting anything still shows green.

## Known limits

- **Weather-satellite imagery does not work indoors.** Measured, not
  estimated: an 82° METEOR-M2 pass gave 1.6 dB SNR against the 6–8 dB LRPT
  needs, BER 0.41, and zero bytes decoded in 17 minutes. Note that a *vertical*
  dipole **nulls at zenith**, so a high pass is its worst case, not its best —
  "wait for a better pass" is backwards indoors. A QFH or turnstile outdoors is
  the actual fix. Disabled by default; NOAA APT is moot anyway since 15/18/19
  were all decommissioned in 2025.
- **Drone detection is out of reach for this hardware.** The RTL-SDR v3 tops
  out at 1.766 GHz; drone control links and both Remote ID Wi-Fi bands sit
  above it. That is a frequency problem before it is anything else.
- **A peak-hold sweep overstates a burst.** Six VHF channels showed +16 dB on a
  30 s peak-hold and then produced zero audio across 317 recorded runs at two
  squelch settings. A +16 dB peak is not a +16 dB transmission — confirm with a
  recorded lane before believing a frequency.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). The short version: a change that
makes a failure quieter will be rejected, however much code it deletes.

## License

MIT. See [`LICENSE`](LICENSE).
