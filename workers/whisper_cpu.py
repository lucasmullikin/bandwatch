#!/usr/bin/env python3
"""whisper-cpu -- OVERFLOW transcription on the station itself, via whisper.cpp.

Deliberately a second tier, not a peer. Measured on the same 3 s airband clip,
the CPU tier took ~8.8 s and returned a fragment of broken grammar; the GPU
tier took ~0.5 s and returned the full, correct exchange. The gap is not
marginal, so this worker only claims clips older than MIN_AGE_MIN -- work the
GPU worker did not get to. Racing it would trade a good transcript for a bad
one on every clip.

whisper.cpp applies --no-speech-thold and --logprob-thold internally at the same
values the server gate uses, so its output is comparable, and every row records
which host and model produced it.
"""
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATION = os.environ.get("BANDWATCH_URL", "http://127.0.0.1:9111")
MODEL_NAME = os.environ.get("BANDWATCH_CPU_MODEL", "small.en")
# whisper.cpp ggml weights are a few hundred MB and are downloaded, not shipped.
MODEL = os.environ.get("BANDWATCH_CPU_MODEL_PATH") or os.path.join(
    ROOT, "models", "ggml-%s.bin" % MODEL_NAME)


def _tool(name):
    """Find an external binary on PATH, failing with a name a human can act on.

    Hardcoding a Homebrew prefix here was wrong on every machine but one:
    /opt/homebrew on Apple Silicon, /usr/local on Intel, and anywhere at all
    for a per-user install.
    """
    found = shutil.which(name)
    if not found:
        raise SystemExit(
            "bandwatch: %s is not on PATH.\n"
            "  The CPU transcription worker needs ffmpeg and whisper-cli.\n"
            "  Install them, or run bootstrap.sh." % name)
    return found
WORKER_ID = os.environ.get("BANDWATCH_WORKER_ID", "whisper-cpu")
# 30 min, not 10. At 10 the slow tier took 153 clips against the GPU worker's 120
# -- the GPU worker waits on a lease, work aged past the threshold, and the
# worse model won the race. The overflow tier should be a safety net, not the
# default path.
# In watchwords mode this is the SCREENING tier and should run promptly, not
# as overflow -- it is what finds the hits the good model then re-does.
MIN_AGE_MIN = float(os.environ.get("BANDWATCH_CPU_MIN_AGE", "0"))
IDLE_SLEEP = 60
BATCH = 4          # small: this box is also running both radios


def log(m):
    print("%s  %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), m), flush=True)


def api(path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        STATION.rstrip("/") + path, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def transcribe(mp3):
    wav = "/tmp/i7w_%d.wav" % os.getpid()
    subprocess.run([_tool("ffmpeg"), "-loglevel", "error", "-y",
                    "-i", mp3, "-ar", "16000", "-ac", "1", wav],
                   check=True, timeout=60)
    out = subprocess.run(
        [_tool("whisper-cli"), "-m", MODEL, "-f", wav,
         "-nt", "-np", "-nth", "0.60", "-lpt", "-1.00", "-t", "4"],
        capture_output=True, text=True, timeout=180)
    try:
        os.unlink(wav)
    except Exception:
        pass
    return " ".join(out.stdout.split()).strip()


def main():
    if not os.path.exists(MODEL):
        log("model missing: %s" % MODEL)
        return 2
    log("overflow worker up: %s, only clips older than %.0f min"
        % (MODEL_NAME, MIN_AGE_MIN))
    while True:
        try:
            pend = api("/api/pending?worker=%s&min_age=%s&tier=screen"
                       % (urllib.parse.quote(WORKER_ID), MIN_AGE_MIN)
                       ).get("pending", [])
        except Exception as e:
            log("mini unreachable: %s" % e)
            time.sleep(IDLE_SLEEP)
            continue
        if not pend:
            time.sleep(IDLE_SLEEP)
            continue
        log("overflow: %d clip(s) the GPU worker did not reach" % len(pend))
        for row in pend[:BATCH]:
            path = row.get("audio_path")
            if not path or not os.path.exists(path):
                try:
                    api("/api/transcript", {"id": row["id"], "text": "",
                                            "by": WORKER_ID, "model": MODEL_NAME})
                except Exception:
                    pass
                continue
            t0 = time.time()
            try:
                text = transcribe(path)
                api("/api/transcript", {
                    "id": row["id"], "text": text, "by": WORKER_ID,
                    "model": "whisper.cpp " + MODEL_NAME})
                log("#%s %s (%.1fs cpu) %s" % (row["id"], row.get("channel"),
                                               time.time() - t0, text[:60]))
            except Exception as e:
                log("#%s FAILED: %s" % (row.get("id"), e))
                try:
                    api("/api/transcript", {"id": row["id"], "text": "",
                                            "by": WORKER_ID, "model": MODEL_NAME})
                except Exception:
                    pass
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
