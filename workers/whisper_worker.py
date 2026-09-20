#!/usr/bin/env python3
"""Transcribe voice clips with mlx-whisper, on whichever machine has the GPU.

Pulls untranscribed recordings from a bandwatch station's web API, transcribes
them, and posts the text back. It does not need to run on the station: a
receiver is a cheap always-on box, a GPU usually is not, so this is a separate
process that can live on another machine entirely. Point BANDWATCH_URL at the
station and give it the worker token.

THE ONE RULE: the watchlist is NEVER passed to whisper as an initial_prompt.
Priming the model with a term makes it emit that term -- the hit would be
manufactured by the prompt rather than heard on the air. Transcribe clean;
match afterwards. Any change that passes watchlist terms in as context is a
bug, not an optimisation.

Optional GPU arbitration: if several processes on this machine contend for one
GPU, set BANDWATCH_WHISPER_ARBITER=1 and BANDWATCH_ARBITER_CLIENT to the path
of a client module exposing lease(). Off by default -- most machines have no
such rule, and the worker runs unmanaged.
"""
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_arbiter_client = os.environ.get("BANDWATCH_ARBITER_CLIENT")
if _arbiter_client:
    sys.path.insert(0, _arbiter_client)
try:
    import lease as gpulease  # noqa: E402
except Exception:                      # the usual case: no arbiter on this host
    class _NoLease:
        class GpuLeaseError(RuntimeError):
            pass
    gpulease = _NoLease()

import socket
TENANT = os.environ.get("BANDWATCH_TENANT", "bandwatch-whisper")
# quality tier: in watchwords mode the GPU only ever sees clips the cheap
# screen already flagged -- a handful a day instead of continuous running
TIER = os.environ.get("BANDWATCH_TIER", "quality")
WORKER_ID = os.environ.get("BANDWATCH_WORKER_ID") or ("whisper-" + socket.gethostname().split(".")[0])
PRIORITY = 8
ARBITER = os.environ.get("BANDWATCH_ARBITER_URL", "http://127.0.0.1:8077")
STATE_PORT = int(os.environ.get("BANDWATCH_WHISPER_PORT", "8079"))
STATION = os.environ.get("BANDWATCH_URL", "http://127.0.0.1:9111")
MODEL = os.environ.get("BANDWATCH_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
IDLE_SLEEP = 30
# Off by default: most machines have no single-GPU-tenant rule, and a worker
# that blocks waiting for an arbiter nobody is running never transcribes
# anything. Turn it on where contention is real.
USE_ARBITER = os.environ.get("BANDWATCH_WHISPER_ARBITER", "0") != "0"
BATCH_MAX = 40

_model_loaded = threading.Event()
_drain = threading.Event()
_lock = threading.Lock()
_stats = {"transcribed": 0, "failed": 0, "last": None}


def log(msg):
    print("%s  %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- model

def load_model():
    """Import and warm mlx_whisper. Only ever called while holding the lease."""
    import mlx_whisper  # noqa: F401
    globals()["mlx_whisper"] = mlx_whisper
    _model_loaded.set()
    log("model ready: %s" % MODEL)


def eject_model():
    """Free Metal. MUST be idempotent and tolerate a partial load."""
    _model_loaded.clear()
    try:
        import mlx.core as mx
        mx.clear_cache()
    except Exception:
        pass
    globals().pop("mlx_whisper", None)
    log("Metal freed")


def transcribe(path_or_url):
    r = mlx_whisper.transcribe(path_or_url, path_or_hf_repo=MODEL,
                               language="en", verbose=False)
    segs = r.get("segments") or []
    dur = round(segs[-1]["end"], 1) if segs else None
    # Whisper's own signals beat string matching for spotting fabrication:
    # no_speech_prob is the model saying "there was nothing here", and
    # avg_logprob collapses when it is guessing.
    ns = max((sg.get("no_speech_prob", 0.0) for sg in segs), default=0.0)
    lp = min((sg.get("avg_logprob", 0.0) for sg in segs), default=0.0)
    cr = max((sg.get("compression_ratio", 0.0) for sg in segs), default=0.0)
    return (r.get("text") or "").strip(), dur, {
        "no_speech_prob": round(float(ns), 4),
        "avg_logprob": round(float(lp), 4),
        "compression_ratio": round(float(cr), 3),
    }


# ---------------------------------------------------------------- mini api

def worker_token():
    """Shared secret for /api/transcript on the station.

    That endpoint rewrites the copy of record for what a transmission said,
    and a watchlist match on the text sends a real Signal push -- so it is
    authenticated. Read from a file rather than baked in, and never logged.
    Returns "" if absent, so the failure is a clean 401 rather than a crash.
    """
    for path in (os.environ.get("BANDWATCH_WORKER_TOKEN_FILE"),
                 os.path.expanduser("~/.config/bandwatch-worker-token")):
        if not path:
            continue
        try:
            tok = io.open(path, encoding="utf-8").read().strip()
            if tok:
                return tok
        except OSError:
            continue
    return os.environ.get("BANDWATCH_WORKER_TOKEN", "")


def api(path, payload=None, timeout=20):
    url = STATION.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    tok = worker_token()
    if tok:
        headers["X-Worker-Token"] = tok
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def fetch_audio(name, dest):
    url = STATION.rstrip("/") + "/audio/" + urllib.parse.quote(name)
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as fh:
        fh.write(r.read())
    return dest


# ---------------------------------------------------------------- tenant endpoints

class StateHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.rstrip("/")
        if p == "/gpu_state":
            # the arbiter's H2 sweep reads exactly this key
            return self._j(200, {"loaded": _model_loaded.is_set(), **_stats})
        if p == "/health":
            return self._j(200, {"ok": True, "tenant": TENANT, **_stats})
        return self._j(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.rstrip("/")
        if p == "/eject":
            with _lock:
                eject_model()
            return self._j(200, {"ok": True, "loaded": False})
        if p == "/drain":
            _drain.set()
            return self._j(200, {"ok": True, "draining": True})
        if p == "/reload":
            _drain.clear()
            return self._j(200, {"ok": True})
        return self._j(404, {"error": "not found"})


def start_state_server():
    srv = ThreadingHTTPServer(("127.0.0.1", STATE_PORT), StateHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("tenant endpoints on 127.0.0.1:%d" % STATE_PORT)


def register():
    """lease.py does NOT register -- /lease 403s an unregistered tenant."""
    base = "http://127.0.0.1:%d" % STATE_PORT
    payload = {
        "tenant": TENANT,
        "kind": "mlx",
        "priority": PRIORITY,
        "label": "SDR voice transcription (whisper large-v3-turbo)",
        "endpoints": {
            "gpu_state": base + "/gpu_state",
            "eject": base + "/eject",
            "drain": base + "/drain",
            "reload": base + "/reload",
        },
    }
    req = urllib.request.Request(
        ARBITER.rstrip("/") + "/register",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read() or b"{}")
    log("registered with arbiter: %s" % body)


# ---------------------------------------------------------------- work

def drain_queue():
    """Transcribe everything pending. Runs only with the lease held."""
    done = 0
    while done < BATCH_MAX and not _drain.is_set():
        try:
            pending = api("/api/pending?worker=%s&tier=%s"
                          % (urllib.parse.quote(WORKER_ID), TIER)
                          ).get("pending", [])
        except Exception as e:
            log("cannot reach the station: %s" % e)
            return done
        if not pending:
            return done
        for row in pending:
            if _drain.is_set():
                return done
            name = os.path.basename(row.get("audio_path") or "")
            if not name:
                continue
            tmp = "/tmp/sdrwhisper_%s" % name
            try:
                fetch_audio(name, tmp)
                text, dur, conf = transcribe(tmp)
                res = api("/api/transcript",
                          {"id": row["id"], "text": text, "duration": dur,
                           "conf": conf, "by": WORKER_ID, "model": MODEL})
                _stats["transcribed"] += 1
                _stats["last"] = time.strftime("%H:%M:%S")
                hit = res.get("watchlist_hit")
                kept = res.get("kept", True)
                log("#%s %s (%.1fs) ns=%.2f lp=%.2f %s%s%s" % (
                    row["id"], row.get("channel"), dur or 0,
                    conf["no_speech_prob"], conf["avg_logprob"],
                    "" if kept else "[REJECTED] ",
                    (text[:64] + "…") if len(text) > 64 else text,
                    "  [WATCHLIST: %s]" % hit if hit else ""))
                done += 1
            except Exception as e:
                _stats["failed"] += 1
                log("#%s FAILED: %s" % (row.get("id"), e))
                # mark it done with empty text so one bad clip cannot wedge
                # the queue head forever
                try:
                    api("/api/transcript", {"id": row["id"], "text": ""})
                except Exception:
                    pass
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
            if done >= BATCH_MAX:
                break
    return done


def main():
    start_state_server()
    if USE_ARBITER:
        try:
            register()
        except Exception as e:
            log("registration FAILED (%s) -- /lease will 403 until this works" % e)
            return 2
    else:
        log("arbiter DISABLED -- this host has no single-Metal-tenant rule")

    log("worker %s polling %s every %ds" % (WORKER_ID, STATION, IDLE_SLEEP))
    while True:
        try:
            pending = api("/api/pending?worker=%s&tier=%s"
                          % (urllib.parse.quote(WORKER_ID), TIER)
                          ).get("pending", [])
        except Exception as e:
            log("mini unreachable: %s" % e)
            time.sleep(IDLE_SLEEP)
            continue

        if not pending:
            time.sleep(IDLE_SLEEP)
            continue

        log("%d clip(s) queued" % len(pending))
        try:
            if USE_ARBITER:
                with gpulease.run_tenant(TENANT, load_model, eject_model,
                                         priority=PRIORITY):
                    n = drain_queue()
            else:
                load_model()
                try:
                    n = drain_queue()
                finally:
                    eject_model()
            log("batch done: %d transcribed" % n)
        except gpulease.GpuLeaseError as e:
            log("lease denied (%s) -- backing off" % e)
            time.sleep(60)
        except Exception as e:
            log("worker error: %s" % e)
            time.sleep(30)


if __name__ == "__main__":
    sys.exit(main() or 0)
