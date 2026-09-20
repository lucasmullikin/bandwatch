"""A minimal pytest stand-in, so lanes/test_weathersat.py runs on the Mini.

The Mini's Python is PEP 668 externally managed, and installing pytest there
would mean --break-system-packages on a box running the live receivers. Every
other suite in this project is stdlib unittest; this shim keeps the one
pytest-style suite runnable without adding a production dependency or
rewriting 31 working tests.

Only what that file actually uses is implemented: `raises`, `approx`, and
`mark.parametrize`. It is deliberately not a general pytest: anything else
raises AttributeError rather than silently doing nothing, because a testing
shim that quietly no-ops is worse than no shim.
"""


class raises:
    """Context manager form only: `with pytest.raises(ValueError): ...`"""

    def __init__(self, expected, match=None):
        self.expected = expected
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError("DID NOT RAISE %r" % (self.expected,))
        if not issubclass(exc_type, self.expected):
            return False          # propagate: wrong exception is a real failure
        self.value = exc
        if self.match is not None:
            import re
            if not re.search(self.match, str(exc)):
                raise AssertionError("%r does not match %r" % (str(exc), self.match))
        return True


class approx:
    def __init__(self, expected, rel=None, abs=None):
        self.expected = expected
        self.rel = rel
        self.abs = abs if abs is not None else (1e-6 if rel is None else None)

    def _close(self, a, b):
        if self.abs is not None and abs(a - b) <= self.abs:
            return True
        if self.rel is not None and abs(a - b) <= self.rel * max(abs(a), abs(b)):
            return True
        return False

    def __eq__(self, other):
        try:
            return self._close(float(other), float(self.expected))
        except (TypeError, ValueError):
            return NotImplemented

    def __repr__(self):
        return "approx(%r)" % (self.expected,)


class _Mark:
    @staticmethod
    def parametrize(argnames, argvalues):
        """Record the cases on the function; the runner expands them."""
        names = ([a.strip() for a in argnames.split(",")]
                 if isinstance(argnames, str) else list(argnames))

        def deco(fn):
            cases = getattr(fn, "_parametrize", [])
            cases.append((names, list(argvalues)))
            fn._parametrize = cases
            return fn
        return deco

    @staticmethod
    def skip(*a, **k):
        def deco(fn):
            fn._skip = True
            return fn
        return deco


mark = _Mark()


def fail(msg=""):
    raise AssertionError(msg)


def skip(msg=""):
    raise _Skipped(msg)


class _Skipped(Exception):
    pass
