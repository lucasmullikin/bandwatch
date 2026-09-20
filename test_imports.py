"""Every module that uses a standard-library module must import it.

`bandwatch status` raised NameError: name 'os' is not defined on its first real
run. broker/status.py gained os.path.join and os.environ when the config
directory became relocatable, and never gained `import os`.

Nothing caught it. It compiles -- a missing import is a runtime error, not a
syntax one -- and it sits on a path no test reached, so the command was broken
from the day it was published to the day somebody typed it.

This is a linter's job, and there is no linter here: the project is stdlib-only
by design and the test venv holds pytest alone. So the check is done with ast,
which is also stdlib, and it is cheap.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# The ones whose absence produces NameError at runtime rather than ImportError
# at startup, which is what makes them survive to the user.
WATCHED = ("os", "sys", "json", "re", "time", "math", "shutil", "hmac",
           "socket", "sqlite3", "subprocess", "secrets", "hashlib",
           "argparse", "threading", "urllib", "collections", "glob", "signal")


def python_files():
    skip = (".venv", "tools", "models", ".git")
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".venv") and d not in skip]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def missing_imports(path):
    """Names used as `mod.attr` that are never imported or assigned locally."""
    try:
        tree = ast.parse(open(path, errors="replace").read())
    except SyntaxError:
        return []
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported.add(a.asname or a.name)
    used = {n.value.id for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    # A local named `json` shadows the module and is not a missing import.
    bound = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    bound |= {a.arg for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              for a in n.args.args + n.args.kwonlyargs}
    return sorted(m for m in WATCHED
                  if m in used and m not in imported and m not in bound)


def test_no_module_uses_an_unimported_stdlib_module():
    offenders = []
    for p in python_files():
        for m in missing_imports(p):
            offenders.append("%s uses %s.* but never imports it"
                             % (os.path.relpath(p, ROOT), m))
    assert not offenders, "\n  " + "\n  ".join(offenders)


def test_the_check_can_actually_see_the_bug(tmp_path):
    """A scanner that finds nothing passes every scan.

    This is the exact shape of the status.py bug, written out.
    """
    bad = tmp_path / "bad.py"
    bad.write_text("import json\ndef f(root):\n"
                   "    return json.load(open(os.path.join(root, 'x')))\n")
    assert missing_imports(str(bad)) == ["os"]


def test_the_check_does_not_cry_wolf_on_a_local_name(tmp_path):
    ok = tmp_path / "ok.py"
    ok.write_text("import os\n"
                  "def f():\n"
                  "    json = type('x', (), {'loads': staticmethod(lambda s: s)})\n"
                  "    return json.loads('1'), os.sep\n")
    assert missing_imports(str(ok)) == []


def test_there_are_files_to_scan():
    assert len(list(python_files())) >= 20
