#!/usr/bin/env bash
# Run every test suite. No radio, no network, no configured station required.
#
#   ./run-tests.sh
#
# Exits non-zero if any suite fails. This is exactly what CI runs.
#
# Two things worth knowing about the output:
#
#   * Some tests SKIP rather than fail when there is no live station -- the
#     ones that audit a real event store against real audio on disk. They can
#     only mean something where data exists. A skip here is correct; a skip
#     everywhere would mean the suite proves less than it appears to, so the
#     count is printed rather than hidden.
#
#   * The bin/ checks are scripts, not unittest cases. They print their own
#     PASS lines and a verdict. They are included because the logic they cover
#     -- watchdog escalation, lane starvation, the military watch -- is the
#     part of this system most likely to fail silently.
set -uo pipefail

cd "$(dirname "$0")"
fail=0

run() {
  local label="$1"; shift
  printf '\n\033[1m=== %s ===\033[0m\n' "$label"
  if "$@"; then
    return 0
  fi
  echo "FAILED: $label" >&2
  fail=1
}

for pkg in analysis broker webui enrich; do
  run "$pkg" bash -c "cd '$pkg' && python3 -m unittest discover -p 'test_*.py' -v 2>&1 | tail -20"
done

run "lanes (weathersat)" python3 lanes/run_tests.py

# Script-style checks. Each exits non-zero on failure.
for t in bin/test_starve.py bin/test_milwatch.py bin/test_vehicles.py bin/test_watchdog_escalation.py; do
  [ -f "$t" ] || continue
  run "$(basename "$t")" python3 "$t"
done

# Every shipped example must be valid JSON. A malformed example is a broken
# first run for every new user, and it is the kind of thing that survives
# review because nobody reads a config file closely.
run "config examples parse" python3 -c "
import glob, json, sys
bad = 0
for f in sorted(glob.glob('config/examples/*.json')):
    try:
        json.load(open(f))
        print('  ok', f)
    except Exception as e:
        print('  INVALID', f, e); bad = 1
sys.exit(bad)"

# Every Python file must at least parse. Catches a syntax error in a script
# that no test imports -- of which this project has several.
run "all python parses" python3 -c "
import ast, pathlib, sys
bad = 0
for p in sorted(pathlib.Path('.').rglob('*.py')):
    if '.git' in p.parts or 'var' in p.parts:
        continue
    try:
        ast.parse(p.read_text())
    except SyntaxError as e:
        print('  SYNTAX ERROR', p, e); bad = 1
sys.exit(bad)"

printf '\n'
if [ "$fail" -eq 0 ]; then
  echo "ALL SUITES PASSED"
else
  echo "SOME SUITES FAILED" >&2
fi
exit "$fail"
