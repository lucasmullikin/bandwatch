#!/usr/bin/env bash
# Shell half of bandwatch_config.py. Source it; do not execute it.
#
#   . "$(dirname "$0")/bw-env.sh"
#
# Same three directories, same environment variables, same defaults. Keep this
# in step with bandwatch_config.py -- two resolvers that disagree is how a lane
# writes to one events directory while the console reads another and reports a
# silent band.

# ${BASH_SOURCE[0]} is this file even when sourced; $0 would be the caller.
_bw_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${BANDWATCH_ROOT:=$(cd "$_bw_here/.." && pwd)}"
: "${BANDWATCH_CONFIG:=$BANDWATCH_ROOT/config}"
: "${BANDWATCH_VAR:=$BANDWATCH_ROOT/var}"
# Decoders built from source rather than installed by a package manager
# (acarsdec, dump978, multimon-ng, RTLSDR-Airband on some systems).
: "${BANDWATCH_TOOLS:=$BANDWATCH_ROOT/tools}"

export BANDWATCH_ROOT BANDWATCH_CONFIG BANDWATCH_VAR BANDWATCH_TOOLS

# Short names for readability inside the scripts.
ROOT="$BANDWATCH_ROOT"
CONFIG="$BANDWATCH_CONFIG"
VAR="$BANDWATCH_VAR"
TOOLS="$BANDWATCH_TOOLS"
CONF_DIR="$CONFIG/conf"
PROFILE_DIR="$CONFIG/profiles"

# Homebrew is wherever this machine put it: /opt/homebrew on Apple Silicon,
# /usr/local on Intel, and anywhere at all for a per-user install. Ask brew
# rather than assuming, and stay silent if it is not installed -- the SDR
# binaries may well be on PATH already.
if [ -z "${BANDWATCH_BREW_PREFIX:-}" ] && command -v brew >/dev/null 2>&1; then
  BANDWATCH_BREW_PREFIX="$(brew --prefix 2>/dev/null || true)"
fi
if [ -n "${BANDWATCH_BREW_PREFIX:-}" ]; then
  export PATH="$BANDWATCH_BREW_PREFIX/bin:$BANDWATCH_BREW_PREFIX/sbin:$PATH"
fi

bw_die() { echo "bandwatch: $*" >&2; exit 1; }

bw_need_config() {
  [ -d "$CONFIG" ] || bw_die "no config directory at $CONFIG
  Copy the examples to start:
      cp -r $ROOT/config/examples/. $CONFIG/
  or point BANDWATCH_CONFIG at your own."
}
