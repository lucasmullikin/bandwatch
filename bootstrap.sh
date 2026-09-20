#!/usr/bin/env bash
# Install what bandwatch needs and set up a config directory.
#
#   ./bootstrap.sh            install everything, create config from examples
#   ./bootstrap.sh --check    report what is missing, install nothing
#
# Safe to re-run. It never overwrites an existing config file.
#
# What this cannot do for you: pick your frequencies, set your coordinates, or
# decide what you are comfortable recording. Those are in config/, and
# docs/COVERAGE.md is worth reading before you turn anything on.
set -uo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }

missing=0

# ---------------------------------------------------------------- python
if ! command -v python3 >/dev/null 2>&1; then
  red "python3 not found. bandwatch needs Python 3.9 or newer."
  exit 1
fi
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' || {
  red "python3 is $PYV; bandwatch needs 3.9 or newer."; exit 1; }
green "python3 $PYV"
echo "  no Python packages needed -- the receiver, broker and console are stdlib only."
echo "  (the optional transcription workers want mlx-whisper or whisper.cpp; see workers/)"

# ---------------------------------------------------------------- homebrew
if ! command -v brew >/dev/null 2>&1; then
  red "Homebrew not found."
  echo "  bandwatch's decoders are C programs, not Python packages. Install brew:"
  echo "      /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  exit 1
fi
BREW_PREFIX="$(brew --prefix)"
green "homebrew at $BREW_PREFIX"

# ---------------------------------------------------------------- decoders
#
# Split by how they install, because that is what decides whether this script
# can do it for you.
#
#   FORMULAE   a plain `brew install` works
#   SOURCE     no formula, or the formula is broken on macOS -- you build it,
#              and the notes below say what bites
#
FORMULAE=(rtl-sdr sox socat ffmpeg lame mosquitto)
OPTIONAL=(direwolf multimon-ng dump978 satdump)

need_install=()
for f in "${FORMULAE[@]}"; do
  if brew list --formula "$f" >/dev/null 2>&1; then
    green "  have $f"
  else
    warn "  missing $f"
    need_install+=("$f")
    missing=1
  fi
done

echo
echo "Optional decoders (each enables one lane; skip any you do not want):"
for f in "${OPTIONAL[@]}"; do
  if brew list --formula "$f" >/dev/null 2>&1; then
    green "  have $f"
  else
    warn "  missing $f -- the matching lane stays disabled"
  fi
done

if [ "$CHECK_ONLY" -eq 0 ] && [ "${#need_install[@]}" -gt 0 ]; then
  echo
  echo "Installing: ${need_install[*]}"
  brew install "${need_install[@]}" || {
    red "brew install failed"; exit 1; }
fi

# ---------------------------------------------------------------- rtl_433
# rtl_433 is not in core homebrew. It has its own tap.
if command -v rtl_433 >/dev/null 2>&1; then
  green "  have rtl_433"
else
  warn "  missing rtl_433 -- the ISM/TPMS lanes need it"
  if [ "$CHECK_ONLY" -eq 0 ]; then
    brew tap merbanan/rtl_433 2>/dev/null || true
    brew install rtl_433 || warn "  rtl_433 install failed; see https://github.com/merbanan/rtl_433"
  fi
  missing=1
fi

# ---------------------------------------------------------------- built from source
cat <<'NOTES'

Built from source, not installed here (each is optional):

  RTLSDR-Airband   the voice lanes. Build with -DPLATFORM=native.
                   https://github.com/charlie-foxtrot/RTLSDR-Airband
  acarsdec         ACARS. Does NOT build clean on macOS -- see
                   patches/acarsdec-macos.patch.sh in this repo.
  dumpvdl2         VDL2.
  readsb           ADS-B. dump1090 also works; the lane script prefers readsb.

Put them under tools/, or anywhere on PATH, or set BANDWATCH_TOOLS.
NOTES

# ---------------------------------------------------------------- config
echo
if [ "$CHECK_ONLY" -eq 0 ]; then
  mkdir -p config
  created=0
  for ex in config/examples/*.json; do
    base="$(basename "$ex")"
    if [ -e "config/$base" ]; then
      echo "  keeping your config/$base"
    else
      cp "$ex" "config/$base"
      green "  created config/$base from the example"
      created=1
    fi
  done
  mkdir -p config/conf config/profiles "${BANDWATCH_VAR:-var}"/{events,voice,logs}
  [ "$created" -eq 1 ] && cat <<'NEXT'

Now edit config/ before starting anything:

  1. config/bandwatch.json   set station.lat / station.lon / timezone.
                             There is no default and the code refuses to run
                             without them: every distance and every satellite
                             pass is measured from that point, and a guessed
                             coordinate answers confidently about somewhere
                             you are not.

  2. config/stations.json    three sets are marked "replace_me" -- your local
                             tower, ground, and military air. make_confs.py
                             refuses to generate them until you edit or delete
                             them, for the same reason.

  3. config/lanes.json       set each device's SERIAL. Two dongles arrive from
                             the factory reporting the same one, and a swapped
                             index looks like bad reception, never an error:
                                 rtl_test -t
                                 rtl_eeprom -d 0 -s 00000001

Then generate the radio configs and start:

      python3 make_confs.py
      python3 make_profiles.py
      bin/bandwatch start
NEXT
fi

echo
if [ "$missing" -eq 1 ] && [ "$CHECK_ONLY" -eq 1 ]; then
  warn "some required tools are missing; re-run without --check to install them"
  exit 1
fi
green "bootstrap done"
