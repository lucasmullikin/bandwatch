#!/usr/bin/env python3
"""Generate rtl_airband configs from one channel table.

The table lives in your config directory (config/bandplan.json), not in this
file, so the band plan you monitor is yours and the generator is shared. Run
this after any edit to it; the .conf files are OUTPUT and hand-editing one
means the next run silently reverts you.

Two rules baked in:
  * centre the tuner OFF the channel block -- the RTL-SDR DC spike lands at the
    tuner centre and would sit on whichever channel you centred on.
  * no squelch_threshold -- rtl_airband then MEASURES the noise floor per
    channel. A fixed -32 guessed wrong and recorded 30s of noise that Whisper
    rendered as "Thank you."
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bandwatch_config as C  # noqa: E402

OUT = C.CONF_DIR


def load_sets():
    """Read the channel table, failing loudly on the shapes that bite.

    A malformed entry here becomes a config rtl_airband accepts and a lane that
    records nothing, so every field is checked before a file is written.
    """
    doc = C.load("bandplan")
    sets = doc.get("sets")
    if not isinstance(sets, dict) or not sets:
        raise C.ConfigError(
            "bandplan.json has no 'sets' object.\n"
            "  See config/examples/bandplan.json for the shape.")
    out = {}
    placeholders = []
    for name, spec in sets.items():
        if not isinstance(spec, dict):
            raise C.ConfigError("bandplan.json: set %r is not an object" % name)
        if spec.get("replace_me"):
            placeholders.append(name)
            continue
        mode = spec.get("mode")
        if mode not in ("am", "nfm"):
            raise C.ConfigError(
                "bandplan.json: set %r has mode %r; expected \"am\" or \"nfm\"" % (name, mode))
        chans = spec.get("channels")
        if not isinstance(chans, list) or not chans:
            raise C.ConfigError("bandplan.json: set %r has no channels" % name)
        pairs = []
        for ch in chans:
            if not (isinstance(ch, list) and len(ch) == 2):
                raise C.ConfigError(
                    "bandplan.json: set %r has a channel that is not [label, mhz]: %r" % (name, ch))
            label, freq = ch
            if not isinstance(freq, (int, float)):
                raise C.ConfigError(
                    "bandplan.json: set %r channel %r has a non-numeric frequency %r"
                    % (name, label, freq))
            pairs.append((str(label), float(freq)))
        out[name] = (mode, pairs, spec.get("note", ""))
    if placeholders and "--allow-placeholders" not in sys.argv:
        raise C.ConfigError(
            "these station sets are still placeholders: %s\n"
            "  They carry example frequencies for somewhere that is not where you\n"
            "  are. Generating them would produce configs that record silence and\n"
            "  report themselves perfectly healthy while doing it.\n"
            "  Edit them in %s and remove \"replace_me\", or delete the sets you do\n"
            "  not want. To generate everything else meanwhile:\n"
            "      python3 make_confs.py --allow-placeholders"
            % (", ".join(sorted(placeholders)), C.path("bandplan.json")))
    return out


def build(name, mode, chans, note, rec_dir):
    lo = min(f for _, f in chans)
    hi = max(f for _, f in chans)
    span = hi - lo
    # Centre on the block MIDPOINT so the worst-case offset is span/2 -- offsetting
    # below the block instead pushes the top channel past the +/-1.2 MHz a 2.4 MHz
    # slice can reach. Then nudge only if a channel would land on DC.
    centre = round((lo + hi) / 2.0, 4)
    for _ in range(12):
        if all(abs(f - centre) > 0.060 for _, f in chans):
            break
        centre = round(centre + 0.100, 4)
    worst = max(abs(f - centre) for _, f in chans)
    if worst > 0.95:
        raise SystemExit(
            f"{name}: {span*1000:.0f} kHz span needs {worst*2:.2f} MHz of tuner "
            f"-- exceeds the ~2.0 MHz USABLE passband at 2.4 MSPS "
            f"(the edges roll off). Split it into two lanes.")
    rec = os.path.join(rec_dir, name)
    # AM squelch opens far more readily than FM on noise. The library default
    # SNR threshold is 9.54 dB, which produced 155 airband recordings in an
    # hour, 74% of them under 2 seconds. 15 dB keeps real transmissions and
    # drops the chatter.
    snr = 15.0 if mode == "am" else 11.0
    body = []
    for label, f in sorted(chans, key=lambda x: x[1]):
        body.append(f"""    {{
      freq = {f:.4f};
      modulation = "{mode}";
      label = "{label}";
      squelch_snr_threshold = {snr};
      outputs: (
        {{
          type = "file";
          directory = "{rec}";
          filename_template = "{label}";
          continuous = false;
          split_on_transmission = true;
        }}
      );
    }}""")
    return f"""# rtl_airband -- {name}
# {note}
#
# GENERATED by make_confs.py from config/bandplan.json. Do not hand-edit:
# the next run overwrites it and your change disappears without a word.
#
# {len(chans)} channel(s), {lo}-{hi} MHz ({span*1000:.0f} kHz span).
# centerfreq is the block midpoint (nudged if a channel sat on it): the DC spike
# lands at the tuner centre, and the worst channel offset is {max(abs(f-centre) for _,f in chans)*1000:.0f} kHz
# -- inside the +/-1200 kHz a 2.4 MHz slice reaches.
# No squelch_threshold -> rtl_airband measures the noise floor per channel.

devices:
(
  {{
    type = "rtlsdr";
    index = {{DEV}};
    gain = 40;
    centerfreq = {centre:.4f};
    correction = 0;
    mode = "multichannel";
    sample_rate = 2.4;
    channels:
    (
{",".join(body)}
    );
  }}
);
"""


def main():
    sets = load_sets()
    rec_dir = C.var("voice")
    os.makedirs(OUT, exist_ok=True)
    for name, (mode, chans, note) in sets.items():
        txt = build(name, mode, chans, note, rec_dir)
        with open(os.path.join(OUT, name + ".conf"), "w") as fh:
            fh.write(txt)
        lo = min(f for _, f in chans)
        hi = max(f for _, f in chans)
        import re as _re
        c = float(_re.search(r"centerfreq = ([\d.]+)", txt).group(1))
        worst = max(abs(f - c) for _, f in chans) * 1000
        print(f"  {name:<14} {mode:<4} {len(chans)} ch  {lo}-{hi} MHz  "
              f"span {(hi-lo)*1000:>4.0f} kHz  centre {c}  worst offset {worst:>4.0f} kHz")
    print("\n  wrote %d config(s) to %s" % (len(sets), OUT))


if __name__ == "__main__":
    try:
        main()
    except C.ConfigError as e:
        raise SystemExit("bandwatch: %s" % e)
