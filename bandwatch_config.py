#!/usr/bin/env python3
"""Where bandwatch finds its root, its config and its data.

One place, because the alternative is what this project started as: an absolute
path typed into thirty files, four of which pointed at a machine that no longer
existed.

Three directories, each overridable by environment variable:

  BANDWATCH_ROOT     the checkout itself           (default: this file's dir)
  BANDWATCH_CONFIG   your station's configuration  (default: $ROOT/config)
  BANDWATCH_VAR      recordings, events, logs      (default: $ROOT/var)

Splitting CONFIG out of ROOT is what lets one checkout be shared: the code is
public, your band plan and watchlist are not. Point BANDWATCH_CONFIG at a
private directory and nothing site-specific ever lives inside the repo.

Nothing here has a site-specific default. A missing setting raises rather than
guessing, because a guessed coordinate produces confident, wrong answers about
what flew over your house -- and a wrong answer that looks fine is the failure
mode this project spends most of its effort avoiding.
"""
import json
import os

ROOT = os.environ.get("BANDWATCH_ROOT") or os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("BANDWATCH_CONFIG") or os.path.join(ROOT, "config")
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")

# Generated artifacts. Both are OUTPUTS -- never hand-edit them, and never
# commit them. make_confs.py and make_profiles.py own these directories.
CONF_DIR = os.path.join(CONFIG, "conf")
PROFILE_DIR = os.path.join(CONFIG, "profiles")


class ConfigError(Exception):
    """A required setting is missing or malformed. Always fatal, never defaulted."""


def path(*parts):
    """A path inside the config directory."""
    return os.path.join(CONFIG, *parts)


def var(*parts):
    """A path inside the data directory."""
    return os.path.join(VAR, *parts)


def load(name, required=True):
    """Read one JSON file from the config directory.

    `name` is given without the .json suffix. If the file is absent but a
    worked example of it ships in config/examples/, the error says so -- a
    first run fails for an obvious reason rather than an obscure one.
    """
    p = path(name + ".json")
    try:
        with open(p) as fh:
            return json.load(fh)
    except FileNotFoundError:
        if not required:
            return {}
        hint = ""
        example = os.path.join(ROOT, "config", "examples", name + ".json")
        if os.path.exists(example):
            hint = ("\n  A worked example ships with bandwatch. Start from it:\n"
                    "      cp %s %s" % (example, p))
        raise ConfigError("missing config file: %s%s" % (p, hint))
    except ValueError as e:
        raise ConfigError("malformed JSON in %s: %s" % (p, e))


def require(cfg, key, why):
    """Fetch a setting that has no sensible default.

    `why` explains what breaks without it, because an error that only says
    'missing key' tells you what to type but not what you were about to get
    wrong.
    """
    if key not in cfg or cfg[key] in (None, ""):
        raise ConfigError("%s is not set in %s.\n  %s" % (key, path("bandwatch.json"), why))
    return cfg[key]


def station():
    """The receiving site: latitude, longitude, altitude in metres.

    Deliberately has no default. Distance-to-airport rules, low-aircraft
    alerting and satellite pass prediction are all measured FROM this point; a
    placeholder coordinate does not fail, it silently answers questions about
    somewhere else.
    """
    cfg = load("bandwatch")
    site = cfg.get("station") or {}
    for k in ("lat", "lon"):
        if not isinstance(site.get(k), (int, float)):
            raise ConfigError(
                "station.%s is not set in %s.\n"
                "  ADS-B range rules and satellite passes are computed from your\n"
                "  receiver's position. There is no safe default: an unset\n"
                "  coordinate would report aircraft distances from somewhere you\n"
                "  are not, and look entirely healthy doing it." % (k, path("bandwatch.json")))
    return (float(site["lat"]), float(site["lon"]), float(site.get("alt_m", 0.0)))
