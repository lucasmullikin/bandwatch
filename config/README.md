# config/

Your station's configuration. **Nothing in here is tracked** except the worked
examples in `examples/` and this file.

That split is deliberate and load-bearing: the code is public, your
coordinates, band plan, watchlist and notifier credentials are not.

## Files

| file | what it holds | required |
|---|---|---|
| `bandwatch.json` | coordinates, timezone, retention, alerting, transcription | **yes** |
| `lanes.json` | lane catalogue and rotation profiles; device serials | **yes** |
| `stations.json` | your band plan — the input to `make_confs.py` | for voice lanes |
| `schedule.json` | which profile runs at which hours | no |
| `sensors.json` | devices you have explicitly claimed as your own | no |
| `watchlist.json` | terms that escalate a clip to transcription | no |
| `conf/` | **generated** rtl_airband configs | — |
| `profiles/` | **generated** rotation profiles | — |

`conf/` and `profiles/` are **outputs**. Hand-edit one and the next generator
run reverts you without a word. Change `stations.json` or `lanes.json` and
re-run:

```bash
python3 make_confs.py
python3 make_profiles.py
```

## Starting from the examples

`bootstrap.sh` copies them for you. By hand:

```bash
cp config/examples/*.json config/
```

Then set, in this order:

1. `bandwatch.json` → `station.lat`, `station.lon`, `timezone`
2. `stations.json` → replace the three sets marked `"replace_me"`
3. `lanes.json` → `devices.*.serial` for each dongle

## Keeping it somewhere else

`BANDWATCH_CONFIG` overrides this directory entirely:

```bash
export BANDWATCH_CONFIG=~/my-station-config
```

This is the recommended setup if you want your configuration under version
control of its own — keep a private repo of station config, track this one for
code, and never think about the boundary again.

Related: `BANDWATCH_VAR` (recordings and the event store, default `var/`) and
`BANDWATCH_TOOLS` (decoders built from source, default `tools/`).

## Why several settings have no default

`station.lat`, `station.lon` and the device serials refuse to fall back to
anything.

A missing coordinate does not produce an error — it produces **confident
answers about somewhere you are not**. Aircraft distances, low-altitude alerts
and satellite pass predictions are all measured from that point, and every one
of them looks completely normal when computed from the wrong place.

The same applies to device serials: an unpinned radio presents as *bad
reception*, never as an error.

These fail loudly at startup because the alternative is failing quietly for
months.
