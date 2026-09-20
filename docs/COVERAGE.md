# Journalistic coverage — what this system can and cannot support

Passive RF monitoring produces *leads*, not facts. This document sets the
standard of proof before anything heard here informs a story, and defines what
"reactive coverage" means operationally.

## The core rule

**A transcript is a lead. It is never, on its own, a fact.**

This is not caution for its own sake — it is measured. In this build, Whisper
rendered 30 seconds of channel noise as `"Thank you."` and 60 seconds of a weak
NOAA carrier as `"Thank you. Thank you."` Both were confidently produced, both
were fiction. A filter now rejects known silence artefacts, but the filter only
catches the *recognisable* hallucinations. It cannot catch a plausible-sounding
misheard sentence.

The same discipline already exists in `asr-agree`: no single transcript source
is reliably best, so tier by cross-source agreement rather than picking a winner.

## Standard of proof, by claim type

| Claim | What is required |
|---|---|
| "A transmission occurred on this frequency at this time" | The recording plus the lane log. **Publishable as-is** — this is instrument data, not interpretation. |
| "Someone said X" | Audio retained, a human has listened, and the transcript matches what a person hears. ASR alone is never sufficient. |
| "Person P said X" | **Not supportable from RF alone.** FRS/GMRS carry no identity. A voice is not an identifier. |
| "Agency A did B" | Requires corroboration from a records source — CAD, a report, a document. The radio establishes timing, never authority. |
| "Aircraft N was overhead" | ADS-B hex is a strong identifier; registry lookup is a separate, citable step. Callsign fields can be spoofed or blank. |
| "Vehicle V passed" | **Not supportable.** A TPMS ID identifies a *tyre sensor*, not a vehicle, an owner, or a person. |

That last row matters more than it looks. This system has logged eleven Toyota
TPMS identifiers. Those are stable, unique, and correlate to a specific car
across days. That is a movement-tracking capability, and treating it as
publishable identification would be both wrong and harmful.

## Retention has to outlive the story

The default is metadata for 14 days, local disk only. **That is a monitoring
retention, not an evidentiary one.** A story that develops over a month will
find its own source material already deleted.

So: a `preserve` flag on any event or recording exempts it from the sweep, and
preserved audio is hashed on preservation so a later file can be proven to be
the same file. Preserve deliberately, per item — not by widening the default,
which just fills the disk with noise nobody will ever review.

## What "reactive" actually means

Reactive coverage is a *tiered* response, not an alert firehose:

1. **Instrument tier — always on, never notifies.** Every lane logs. The
   known-good controls (FM broadcast, NOAA) must always show carriers; a silent
   control means the instrument failed, not that the band was quiet.
2. **Watchlist tier — immediate.** A term match on a transcript pages you now,
   with the audio attached. Terms are matched *after* transcription and are
   never fed to Whisper as a prompt — priming the model with "cedar hollow"
   makes it emit "cedar hollow", manufacturing the very hit you were watching for.
3. **Pattern tier — batched.** A device that has never been seen before. An
   aircraft loitering or descending. A channel that goes from silent to busy.
   These are only meaningful against a baseline, which is why the collector
   learns for 24 hours before alerting at all.
4. **Correlation tier — on request.** "What else was on the air within ten
   minutes of this transmission." This is where the unified feed earns its
   shape: voice and RF events share one timeline.

## Squawk codes beat transcripts

Aviation emergencies should be caught from **ADS-B squawk 7500 / 7600 / 7700**,
not from hearing "mayday" in a transcript. The squawk is a structured field that
cannot be mis-transcribed, arrives without transcription cost, and is unambiguous.
Where a structured signal exists, prefer it over ASR every time.

## Legal footing

- **Receiving is lawful in Idaho.** There is no state statute restricting
  ownership, use, or mobile operation of a scanner.
- **Encrypted traffic stays encrypted.** The local 700 MHz trunked system's
  carriers are strongly receivable here (+20–29 dB), but no attempt is made to
  defeat encryption, and none should be. A receivable carrier is not a
  readable one, and the difference is not a technical obstacle to route
  around.
- **Receiving and republishing are different questions.** Federal law treats
  divulging the contents of some intercepted communications separately from
  receiving them. Unencrypted broadcasts intended for the general public sit
  differently from private communications. Take advice before publishing
  transmission *contents* — this document does not settle that question.
- **The FRS/GMRS "privacy code" is not privacy.** It is a CTCSS squelch tone.
  People using those radios often believe they are private. That belief is
  wrong, but it is worth remembering when deciding what to do with what you hear.

## Chain of custody

For anything that may support reporting:

1. Preserve the original audio unmodified; never re-encode the copy of record.
2. Record sha256 at preservation time, alongside frequency, timestamp, lane and
   signal level.
3. Keep the ASR transcript **and** a human transcription as separate fields.
   Never overwrite one with the other; disagreement between them is information.
4. Keep the lane log for the capture window — it proves the receiver was
   configured as claimed.

## Known gaps

- **Voice identity is unavailable.** Nothing here identifies a speaker.
- **Coverage is not continuous.** Only the pinned FRS radio never rotates;
  every surveyed band is sampled, and the gaps are real and must be stated.
- **The antenna is a stock dipole**, so absence of signal is weak evidence of
  absence of transmission.
- **ASR is unverified on radio audio.** Accuracy was measured on a clean NOAA
  broadcast, not on clipped, squelch-gated handheld traffic. Assume worse.

## Presence sensing — Wi-Fi and BLE (T15/T16)

This section exists **before** the capability does, because presence sensing is
the one lane in this system that observes *people* rather than machines, and the
rules are much easier to write honestly now than after the first interesting
result.

Everything else here listens to transmitters that are, in effect, public
infrastructure: aircraft announcing themselves, tower controllers doing a public
job, a weather sensor in a garden. Wi-Fi and Bluetooth probe requests come from
phones in people's pockets. The technical distinction is small. The ethical one
is not.

### What this can actually establish

| Claim | What supports it |
|---|---|
| "A device was in range at 14:02" | A received frame. Solid — but see range, below. |
| "The same device returned on three days" | Only if the identifier was stable. Most are not. |
| "A device model was present" | IE fingerprinting clusters **models**, not people. |
| "A specific person was here" | **Nothing here supports this.** Do not claim it. |
| "Nobody was here" | **Nothing here supports this either.** |

### MAC randomisation is a limit, not an obstacle to be beaten

Modern iOS and Android rotate the MAC address in probe requests, often every
few minutes. Fingerprinting from information elements — supported rates, HT/VHT
capabilities, the ordering of tags — narrows a frame to a device *model and OS
version*, not to a handset and certainly not to a person.

Two devices of the same model produce the same fingerprint. In an apartment
block or a busy street that is a large group, not an individual. A system that
reports "the same device returned" when it means "a device of the same model
returned" is stating a falsehood with a confident interface around it, and that
is worse than reporting nothing.

**Rule: never present a fingerprint match as an identity match.** Store the
fingerprint and the evidence class separately, exactly as aircraft correlation
separates a hex match from a time-proximity guess.

### Range is not proximity

Received signal strength is not distance. It varies with orientation, the body
of the person carrying the device, walls, weather and transmit power. A strong
frame does not mean "outside the house" and a weak one does not mean "far away".

**Rule: never convert RSSI into a distance in metres in any output.** Report it
as signal strength, or as coarse buckets whose thresholds are stated.

A directional antenna or two receivers would change this. Neither exists here.

### Retention

Set deliberately tighter than the rest of the system, because the subject is
different:

| Data | Retention | Why |
|---|---|---|
| Raw frames / probe requests | **24 hours** | Enough to investigate an event; not a movement archive. |
| Derived fingerprints + first/last seen | **30 days** | Enough to establish a pattern; short enough to expire. |
| Anything correlated to a named person | **Not stored** | See below. |

The 24-hour and 30-day figures are the operator's instruction, recorded here so
that a later change is a visible decision rather than a drift.

### Lines this system does not cross

- **No correlation to identity.** Never join a fingerprint to a name, a
  household, a vehicle, or a social account, and never store a field intended
  to hold one.
- **No deanonymisation attempts.** Not by cross-referencing an OUI database to
  a purchase, not by matching against a neighbour's known device, not by
  timing correlation with a doorbell camera.
- **No content.** Presence sensing observes that a frame existed and what its
  headers said. It does not capture payloads, and monitor mode must not be used
  to collect traffic contents.
- **No targeting an individual.** The stated purpose is knowing what is near the
  property. A system aimed at following one specific person is a different
  system with different obligations, and this is not it.
- **Deauthentication, evil-twin, and any active attack are out of scope.**
  This is a receiver. It transmits nothing.

### Legal footing, and where it is unsettled

- **Passive reception of frames broadcast in the clear is materially different
  from intercepting communications**, but the boundary is not as clean as it is
  for a scanner listening to aviation voice. Probe requests are broadcast
  unsolicited by the device; payload traffic is not.
- **Monitor mode on macOS requires root and `/dev/bpf0` access**, and it
  disassociates the radio. That is a practical gate, and it is also a useful
  friction: nothing runs in monitor mode by accident.
- **Do not publish presence data about identifiable individuals**, and treat
  any presence claim as unpublishable on its own. It is a lead for the
  operator's own security awareness, which is the stated purpose.
- The overall caution in **Legal footing** above applies here with more force,
  not less. Take advice before doing anything with this beyond looking at it.

### Known gaps specific to presence

- **A quiet result means nothing.** A phone with Wi-Fi off, in a pocket, behind
  a wall, or simply not probing during the sample window is invisible. Absence
  of a device is not evidence of absence of a person.
- **Coverage is sampled here too.** If presence shares a radio with the rotation,
  it sees a fraction of the clock and must state which fraction.
- **The fingerprint library ages.** A new OS release changes IE ordering and
  silently reclassifies a known device as a new one. Treat a "new device"
  result after an OS update season with suspicion.
- **CoreWLAN redacts SSID and BSSID without Location Services**, so the scan
  path can return structurally valid results with the identifying fields
  blanked — which reads as a successful scan finding anonymous networks rather
  than as a permissions failure. Check for redaction explicitly.
