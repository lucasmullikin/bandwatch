"""One resolver for ROOT, CONFIG and VAR -- enforced, not just documented.

bw-env.sh and bandwatch_config.py exist so that a lane cannot write to one
events directory while the console reads another. The rule survives only while
every caller actually uses them, and a shell script that hard-codes "$ROOT/var"
looks correct in the repo: it is right whenever VAR has not been moved, which
is every developer checkout and no shared installation.

Found in bw-supervise.sh, where it mattered twice. The log directory was
created at $ROOT/var/logs while the log was written to $VAR/logs, and
desired_profile() read state.json from $ROOT/var -- so with VAR pointed
elsewhere the "restore whatever was running" step silently found nothing and
fell back to the environment default. The comment directly above it records
that a repair silently reverting a hand-picked mode had already been fixed
once.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SHELL_DIRS = ("bin",)

# $ROOT/var, ${ROOT}/var, $BANDWATCH_ROOT/var -- any spelling of the mistake.
BAD = re.compile(r'\$\{?(?:BANDWATCH_)?ROOT\}?/var\b')


def shell_scripts():
    for d in SHELL_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for fn in sorted(os.listdir(base)):
            p = os.path.join(base, fn)
            if not os.path.isfile(p):
                continue
            with open(p, "rb") as fh:
                if not fh.read(2) == b"#!":
                    continue
            with open(p, errors="replace") as fh:
                head = fh.readline()
            if "sh" in head:
                yield p


# bw-env.sh is the resolver. Stating the default IS its job -- it is the one
# place the fallback is allowed to be written down.
RESOLVER = "bw-env.sh"


# Python has the same trap with a different spelling. The shell scan above
# missed os.path.join(ROOT, "var", ...) entirely, and that is where it actually
# bit: the collector resolved every lane output against ROOT and silently read
# nothing for 35 minutes.
PY_BAD = re.compile(r'os\.path\.join\(\s*ROOT\s*,\s*["\']var["\']')
PY_CONF = re.compile(r'os\.path\.join\(\s*ROOT\s*,\s*["\']conf["\']')

# bandwatch_config.py and the per-module VAR fallbacks are the resolvers: the
# `os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")` line is the
# DEFINITION of the default, which is exactly what they are for.
PY_RESOLVER_OK = re.compile(r'environ\.get\(\s*["\']BANDWATCH_VAR["\']\s*\)\s*or')


def python_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".venv") and d not in ("tools", ".git")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def test_no_python_module_resolves_data_against_root():
    offenders = []
    for p in python_files():
        if os.path.basename(p).startswith("test_"):
            continue
        for n, line in enumerate(open(p, errors="replace"), 1):
            if line.lstrip().startswith("#") or PY_RESOLVER_OK.search(line):
                continue
            if PY_BAD.search(line) or PY_CONF.search(line):
                offenders.append("%s:%d  %s" % (
                    os.path.relpath(p, ROOT), n, line.strip()))
    assert not offenders, (
        "data and generated config live under BANDWATCH_VAR / BANDWATCH_CONFIG, "
        "not in the checkout:\n  " + "\n  ".join(offenders))


def test_the_python_guard_can_see_its_own_bug():
    assert PY_BAD.search('path = os.path.join(ROOT, "var", "events")')
    assert PY_CONF.search('freqs = chanmap.load(os.path.join(ROOT, "conf"))')
    assert not PY_BAD.search('VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")') or \
        PY_RESOLVER_OK.search('VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")')


def test_no_shell_script_hardcodes_root_var():
    offenders = []
    for p in shell_scripts():
        if os.path.basename(p) == RESOLVER:
            continue
        with open(p, errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if line.lstrip().startswith("#"):
                    continue          # prose may name the default
                if BAD.search(line):
                    offenders.append("%s:%d  %s" % (
                        os.path.relpath(p, ROOT), n, line.strip()))
    assert not offenders, (
        "these use $ROOT/var instead of $VAR, so they break the moment "
        "BANDWATCH_VAR points anywhere else:\n  " + "\n  ".join(offenders))


def test_the_guard_can_actually_see_the_pattern():
    """A scanner that matches nothing passes every scan.

    Without this, deleting the regex body would leave the test above green
    forever -- the shape of 'reports healthy, enforces nothing' this project
    keeps finding.
    """
    assert BAD.search('mkdir -p "$ROOT/var/logs"')
    assert BAD.search('cat "${ROOT}/var/state.json"')
    assert BAD.search('x="$BANDWATCH_ROOT/var/events"')
    assert not BAD.search('mkdir -p "$VAR/logs"')
    assert not BAD.search('"$ROOT/bin/bw-health.py"')


def test_there_are_shell_scripts_to_scan():
    """The other half: a scan over an empty list also passes."""
    found = list(shell_scripts())
    assert len(found) >= 5, found
