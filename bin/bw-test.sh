#!/bin/bash
# Run every test in the project, and FAIL if a file contributes zero tests.
#
# On 2026-09-02 lanes/test_weathersat.py silently ran NOTHING for its whole
# life: it is written for pytest, pytest was not installed, and the unittest
# runner reported "Ran 0 tests" without failing. Thirty-one tests covering the
# satellite pass scheduler -- the subsystem with a live capture armed against
# it -- had never executed, and nothing in any output said so.
#
# That is the house failure mode: a check that reports success while enforcing
# nothing. So a zero-test file is an ERROR here, not a quiet pass.
set -uo pipefail

# Root, config and data directories, plus brew on PATH. One resolver, so a
# lane can never write to a different events directory than the console reads.
. "$(dirname "${BASH_SOURCE[0]}")/bw-env.sh"
cd "$(dirname "$0")/.." || exit 2
PY="./.venv-test/bin/pytest"
[ -x "$PY" ] || { echo "no test venv -- python3 -m venv .venv-test && .venv-test/bin/pip install pytest"; exit 2; }

fail=0
total=0
for t in $(find . -name "test_*.py" -not -path "./tools/*" -not -path "./.venv*" | sort); do
  d=$(dirname "$t"); f=$(basename "$t")
  out=$(cd "$d" && "../${PY#./}" "$f" -q 2>&1 | tail -4)
  n=$(printf '%s' "$out" | grep -oE '[0-9]+ passed' | grep -oE '^[0-9]+')
  n=${n:-0}
  if printf '%s' "$out" | grep -qE 'failed|error'; then
    echo "FAIL   $t"; printf '%s\n' "$out" | sed 's/^/       /'; fail=$((fail+1))
  elif [ "$n" -eq 0 ]; then
    # the weathersat case: collected nothing and said nothing
    echo "ZERO   $t -- collected no tests; it is not protecting anything"
    fail=$((fail+1))
  else
    total=$((total+n))
  fi
done
echo "---"
echo "$total tests passed across $(find . -name 'test_*.py' -not -path './tools/*' -not -path './.venv*' | wc -l | tr -d ' ') files, $fail file(s) failing"
exit $([ "$fail" -eq 0 ] && echo 0 || echo 1)
