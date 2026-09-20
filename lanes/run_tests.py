#!/usr/bin/env python3
"""Run the pytest-style weathersat suite under the local shim.

Expands @parametrize into one case per argument set so the reported count is
the real number of assertions exercised, not the number of function
definitions -- a suite that reports 17 when it ran 31 is lying about its own
coverage.
"""
import itertools
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pytest as _shim  # noqa: E402  (the local shim, not the real pytest)


def cases_for(fn):
    """Yield (label, kwargs) for one test function."""
    sets = getattr(fn, "_parametrize", None)
    if not sets:
        yield "", {}
        return
    # multiple stacked decorators produce a cartesian product, as pytest does
    per = []
    for names, values in sets:
        expanded = []
        for v in values:
            vals = v if (len(names) > 1 and isinstance(v, (tuple, list))) else (v,)
            expanded.append(dict(zip(names, vals)))
        per.append(expanded)
    for combo in itertools.product(*per):
        kw = {}
        for d in combo:
            kw.update(d)
        label = "[" + ",".join("%s=%r" % (k, v) for k, v in kw.items()) + "]"
        yield label, kw


def main():
    mod_name = sys.argv[1] if len(sys.argv) > 1 else "test_weathersat"
    mod = __import__(mod_name)
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    passed = failed = skipped = 0
    failures = []
    for name in names:
        fn = getattr(mod, name)
        if not callable(fn):
            continue
        if getattr(fn, "_skip", False):
            skipped += 1
            continue
        for label, kw in cases_for(fn):
            try:
                fn(**kw)
                passed += 1
            except _shim._Skipped:
                skipped += 1
            except Exception:
                failed += 1
                failures.append((name + label, traceback.format_exc()))
    for n, tb in failures:
        print("FAIL %s\n%s" % (n, tb))
    print("Ran %d tests in %s -- %s%s"
          % (passed + failed, mod_name,
             "OK" if not failed else "FAILED (failures=%d)" % failed,
             "" if not skipped else " (skipped=%d)" % skipped))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
