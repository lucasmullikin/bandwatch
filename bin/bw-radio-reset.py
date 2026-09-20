#!/usr/bin/env python3
"""Reset a wedged RTL-SDR at the USB level, and say honestly whether it worked.

Why this exists
---------------
An RTL-SDR can stop delivering samples while still enumerating perfectly. The
device is listed, the serial is right, the tuner is detected -- and every
attempt to open it fails, or opens and then returns nothing. readsb calls this
"SDR wedged, exiting!"; rtl_433 reports "PLL not locked"; rtl_power writes an
empty file and exits.

Restarting the software cannot fix it, because the fault is below the software.
On one station this cost two days: the watchdog restarted the stack on a loop
while the radio stayed dead, and the sweep lanes reported healthy the whole
time because they were still touching their output files.

A USB device reset clears the softer version of this. It does NOT clear every
version -- a dongle whose firmware has hung sometimes needs its power removed,
which means a physical replug. So this tool's job is as much to tell you WHICH
of those two you are in as it is to fix anything.

What it does
------------
1. takes the broker's pause flag for the device, with a BOUNDED hold, so the
   rotation yields instead of fighting for the radio -- and so a crash here
   cannot leave a radio parked forever
2. asks libusb whether the interface can be claimed, BEFORE touching anything
3. resets the device
4. asks again, AFTER
5. reports the before/after pair rather than asserting success

Step 4 is the point. A reset tool that reports "reset sent" has told you
nothing: the interesting question is whether the radio is usable now, and
that has to be measured after the fact, not assumed from a return code.

Usage
-----
    bw-radio-reset.py --list
    bw-radio-reset.py --serial 00000002
    bw-radio-reset.py --slot 1          # the logical device from your profile
    bw-radio-reset.py --all

Exit status is 0 only if every targeted radio is claimable afterwards.
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VID, PID = 0x0bda, 0x2838              # Realtek RTL2832U, every RTL-SDR
try:                                   # one definition, shared with the broker
    sys.path.insert(0, ROOT)
    from bandwatch_config import PAUSE_FMT
except Exception:                      # still usable on a broken checkout
    PAUSE_FMT = "/tmp/bandwatch_pause_dev%s"
HOLD_SECONDS = 45
YIELD_WAIT = 20


# --------------------------------------------------------------------------
# libusb through ctypes.
#
# Every function declares argtypes and restype. Without them ctypes assumes a
# 32-bit int return and truncates every pointer libusb hands back -- the first
# version of this segfaulted for exactly that reason, and a segfault in a
# recovery tool is worse than no recovery tool.
# --------------------------------------------------------------------------
LIB_CANDIDATES = [
    "libusb-1.0.dylib", "libusb-1.0.so.0", "libusb-1.0.so",
    "/opt/homebrew/lib/libusb-1.0.0.dylib",
    "/usr/local/lib/libusb-1.0.0.dylib",
    os.path.expanduser("~/homebrew/lib/libusb-1.0.0.dylib"),
]


class Desc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("bcdUSB", ctypes.c_uint16), ("bDeviceClass", ctypes.c_uint8),
                ("bDeviceSubClass", ctypes.c_uint8), ("bDeviceProtocol", ctypes.c_uint8),
                ("bMaxPacketSize0", ctypes.c_uint8), ("idVendor", ctypes.c_uint16),
                ("idProduct", ctypes.c_uint16), ("bcdDevice", ctypes.c_uint16),
                ("iManufacturer", ctypes.c_uint8), ("iProduct", ctypes.c_uint8),
                ("iSerialNumber", ctypes.c_uint8), ("bNumConfigurations", ctypes.c_uint8)]


def load_libusb():
    for name in LIB_CANDIDATES:
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        p, i, v = ctypes.POINTER, ctypes.c_int, ctypes.c_void_p
        lib.libusb_init.argtypes = [p(v)]; lib.libusb_init.restype = i
        lib.libusb_exit.argtypes = [v]; lib.libusb_exit.restype = None
        lib.libusb_get_device_list.argtypes = [v, p(p(v))]
        lib.libusb_get_device_list.restype = ctypes.c_ssize_t
        lib.libusb_free_device_list.argtypes = [p(v), i]
        lib.libusb_free_device_list.restype = None
        lib.libusb_get_device_descriptor.argtypes = [v, p(Desc)]
        lib.libusb_get_device_descriptor.restype = i
        lib.libusb_open.argtypes = [v, p(v)]; lib.libusb_open.restype = i
        lib.libusb_close.argtypes = [v]; lib.libusb_close.restype = None
        lib.libusb_reset_device.argtypes = [v]; lib.libusb_reset_device.restype = i
        lib.libusb_get_string_descriptor_ascii.argtypes = [v, ctypes.c_uint8,
                                                           ctypes.c_char_p, i]
        lib.libusb_get_string_descriptor_ascii.restype = i
        lib.libusb_kernel_driver_active.argtypes = [v, i]
        lib.libusb_kernel_driver_active.restype = i
        lib.libusb_detach_kernel_driver.argtypes = [v, i]
        lib.libusb_detach_kernel_driver.restype = i
        lib.libusb_claim_interface.argtypes = [v, i]
        lib.libusb_claim_interface.restype = i
        lib.libusb_release_interface.argtypes = [v, i]
        lib.libusb_release_interface.restype = i
        lib.libusb_ref_device.argtypes = [v]; lib.libusb_ref_device.restype = v
        lib.libusb_unref_device.argtypes = [v]; lib.libusb_unref_device.restype = None
        return lib
    raise SystemExit(
        "bandwatch: libusb not found. Tried: %s\n"
        "  Install it (brew install libusb) or set the path in LIB_CANDIDATES."
        % ", ".join(LIB_CANDIDATES))


def each_dongle(lib, ctx):
    """Yield (device_pointer, serial) for every RTL-SDR on the bus.

    Each yielded device is REFERENCED before it leaves here, and the caller
    must unref it (see `dongles`). libusb_free_device_list(lst, 1) unrefs every
    device in the list, so without our own reference the pointers are dangling
    the moment this generator is exhausted -- which is exactly what a list
    comprehension over it does. Reading them afterwards is a use-after-free
    that does not crash reliably, which is the worst kind: it survives testing
    and fails later, in a tool you only run when something is already wrong.
    """
    lst = ctypes.POINTER(ctypes.c_void_p)()
    n = lib.libusb_get_device_list(ctx, ctypes.byref(lst))
    try:
        for i in range(n):
            dev = lst[i]
            d = Desc()
            if lib.libusb_get_device_descriptor(dev, ctypes.byref(d)) != 0:
                continue
            if (d.idVendor, d.idProduct) != (VID, PID):
                continue
            h = ctypes.c_void_p()
            if lib.libusb_open(dev, ctypes.byref(h)) != 0:
                lib.libusb_ref_device(dev)
                yield dev, None            # present but unopenable
                continue
            buf = ctypes.create_string_buffer(64)
            ln = lib.libusb_get_string_descriptor_ascii(h, d.iSerialNumber, buf, 64)
            serial = buf.value.decode("ascii", "replace") if ln > 0 else None
            lib.libusb_close(h)
            lib.libusb_ref_device(dev)
            yield dev, serial
    finally:
        lib.libusb_free_device_list(lst, 1)


def dongles(lib, ctx):
    """Every RTL-SDR, as a list whose device pointers stay valid.

    Returns (devices, release). Call release() when done -- it drops the
    reference each device was given on the way out of each_dongle().
    """
    found = list(each_dongle(lib, ctx))

    # Named drop(), not release(): there is already a module-level release()
    # for the pause flag, and two functions with the same name in one file is
    # how a reader -- or a grep -- ends up looking at the wrong one.
    def drop():
        for dev, _sn in found:
            lib.libusb_unref_device(dev)
    return found, drop


def probe(lib, dev):
    """Can this device's interface 0 actually be claimed right now?

    This is the only question that matters, and it is asked by TRYING, not by
    reading a status flag. Releases immediately so the probe never becomes the
    thing holding the radio.
    """
    h = ctypes.c_void_p()
    if lib.libusb_open(dev, ctypes.byref(h)) != 0:
        return {"open": False, "kernel": None, "claim": None}
    try:
        kern = lib.libusb_kernel_driver_active(h, 0)
        rc = lib.libusb_claim_interface(h, 0)
        if rc == 0:
            lib.libusb_release_interface(h, 0)
        return {"open": True, "kernel": kern, "claim": rc}
    finally:
        lib.libusb_close(h)


def reset(lib, dev):
    h = ctypes.c_void_p()
    if lib.libusb_open(dev, ctypes.byref(h)) != 0:
        return None
    try:
        return lib.libusb_reset_device(h)
    finally:
        lib.libusb_close(h)


# --------------------------------------------------------------------------
# Yielding the radio
# --------------------------------------------------------------------------

def hold(slot):
    """Ask the broker for the radio, with a hold that expires on its own.

    A child process that sleeps and then dies is the whole mechanism: if this
    tool crashes, the PID dies with it, the flag goes stale, and the broker
    reclaims the radio. No exit trap to forget, which matters because a trap
    that never fires is how a radio ends up parked for hours.
    """
    pid = os.fork()
    if pid == 0:                                   # child: just wait, then go
        try:
            time.sleep(HOLD_SECONDS)
        finally:
            os._exit(0)
    # O_NOFOLLOW so a symlink planted at this predictable path cannot redirect
    # the write; O_EXCL so a flag another process is already holding is never
    # clobbered. Both matter because the path is guessable and world-writable.
    path = PAUSE_FMT % slot
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        os.kill(pid, 15)
        os.waitpid(pid, 0)
        raise SystemExit(
            "bandwatch: %s already exists -- another hold is in force.\n"
            "  If it is stale, the broker clears it once its PID dies." % path)
    with os.fdopen(fd, "w") as fh:
        fh.write("%d\n" % pid)
    return pid


def release(slot, pid):
    # Only remove the flag if it is still OURS. Two resets running at once
    # would otherwise delete each other's hold and both take the radio.
    path = PAUSE_FMT % slot
    try:
        with open(path) as fh:
            if fh.read().strip() == str(pid):
                os.unlink(path)
    except OSError:
        pass
    try:
        os.kill(pid, 15)
        os.waitpid(pid, 0)
    except OSError:
        pass


def serial_for_slot(slot):
    """The serial the profile pins to a logical device.

    Config is imported HERE, not at module scope, and only --slot needs it.
    A recovery tool must not require a working configuration to run: the
    moment you reach for this, something is already broken, and --list and
    --all have to work on a machine whose config is missing or wrong.
    """
    try:
        sys.path.insert(0, ROOT)
        import bandwatch_config as C
        doc = C.load("lanes")
        return ((doc.get("devices") or {}).get(str(slot)) or {}).get("serial")
    except Exception:
        return None


TUNER_PROCS = ("rtl_airband", "rtl_433", "rtl_power", "rtl_fm", "rtl_tcp",
               "readsb", "dump1090", "dump978", "acarsdec", "dumpvdl2",
               "direwolf", "multimon-ng")


def a_lane_is_running():
    """Is any decoder currently holding a radio?

    This is the difference between BUSY and BROKEN, and without it this tool
    lies. A radio in use by a running lane refuses the claim exactly like a
    wedged one does -- reporting that as a fault would send someone resetting
    a perfectly healthy dongle in the middle of a recording.

    Matched against the full argv in Python rather than with pgrep: pgrep
    excludes itself but not its siblings, so two concurrent pgreps match each
    OTHER and a busy device can read as free forever.
    """
    try:
        out = subprocess.run(["ps", "-axo", "command="],
                             capture_output=True, text=True, timeout=10)
    except Exception:                              # noqa: BLE001
        return False
    for line in (out.stdout or "").splitlines():
        if any(t in line for t in TUNER_PROCS):
            return True
    return False


def describe(p, busy=False):
    if not p["open"]:
        return "cannot even be opened"
    if p["claim"] == 0:
        return "claimable (healthy)"
    if busy:
        return ("in use by a running lane -- NOT a fault. Stop the stack, or "
                "let this tool take the pause flag, to test it properly")
    return "opens but claim refused (rc=%s, kernel_driver_active=%s) -- wedged" % (
        p["claim"], p["kernel"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--serial", help="reset the dongle with this serial")
    ap.add_argument("--slot", help="reset the dongle pinned to this logical device")
    ap.add_argument("--all", action="store_true", help="reset every RTL-SDR present")
    ap.add_argument("--list", action="store_true", help="show each dongle's state, change nothing")
    ap.add_argument("--no-hold", action="store_true",
                    help="skip the broker pause flag (only when the stack is stopped)")
    a = ap.parse_args()

    lib = load_libusb()
    ctx = ctypes.c_void_p()
    if lib.libusb_init(ctypes.byref(ctx)) != 0:
        raise SystemExit("bandwatch: libusb_init failed")
    # Bound before the try so the finally can always call it. Referencing a
    # name the try block assigns would raise NameError from the finally and
    # bury whatever actually went wrong.
    drop_found = lambda: None                      # noqa: E731
    try:
        found, drop_found = dongles(lib, ctx)
        if not found:
            drop_found()
            raise SystemExit("bandwatch: no RTL-SDR found on the bus")

        if a.list:
            busy = a_lane_is_running()
            print("%-12s %s" % ("SERIAL", "STATE"))
            for dev, sn in found:
                print("%-12s %s" % (sn or "?", describe(probe(lib, dev), busy)))
            if busy:
                print("\n  A lane is running, so a refused claim above means BUSY,")
                print("  not broken. 'bandwatch stop' first for a clean reading.")
            return 0

        want = a.serial
        if a.slot is not None:
            want = serial_for_slot(a.slot)
            if not want:
                raise SystemExit(
                    "bandwatch: no serial pinned for device %s in lanes.json.\n"
                    "  Pin it, or pass --serial." % a.slot)
        if not want and not a.all:
            raise SystemExit("bandwatch: give --serial, --slot, --all or --list")

        targets = [(d, s) for d, s in found if a.all or s == want]
        if not targets:
            raise SystemExit("bandwatch: serial %s is not on the bus" % want)

        # Everything from the first hold() onwards lives inside this try, so
        # no exception -- a Ctrl-C during the yield wait included -- can leave a
        # pause flag behind with a live PID. The bounded child is the backstop,
        # not the plan.
        held = []
        ok = True
        try:
            if not a.no_hold:
                slots = [a.slot] if a.slot is not None else ["0", "1"]
                for sl in slots:
                    held.append((sl, hold(sl)))
                print("holding %s for up to %ds; waiting for the rotation to yield"
                      % (", ".join("dev" + sl for sl, _ in held), HOLD_SECONDS))
                time.sleep(min(YIELD_WAIT, HOLD_SECONDS - 5))
            for dev, sn in targets:
                before = probe(lib, dev)
                print("\n%s" % (sn or "?"))
                print("  before: %s" % describe(before))
                rc = reset(lib, dev)
                # -4 is NOT_FOUND, which libusb returns when the reset caused a
                # re-enumeration. That is a success, not a failure.
                print("  reset:  rc=%s%s" % (rc, " (re-enumerated)" if rc == -4 else ""))
                time.sleep(2)
                # Do NOT probe `dev` again. A reset can re-enumerate the device,
                # which invalidates that pointer -- reading it is undefined and
                # the verdict it produces would be meaningless even if it did
                # not crash. Re-find the device by serial and probe THAT.
                after = {"open": False, "kernel": None, "claim": None}
                fresh, drop = dongles(lib, ctx)
                try:
                    for d2, s2 in fresh:
                        if s2 == sn:
                            after = probe(lib, d2)
                            break
                finally:
                    drop()
                print("  after:  %s" % describe(after))
                if after["claim"] == 0:
                    print("  -> usable")
                else:
                    ok = False
                    print("  -> STILL NOT USABLE.")
                    print("     A USB reset clears a stuck claim; it cannot clear a")
                    print("     dongle whose firmware has hung. That needs its power")
                    print("     removed: unplug it and plug it back in.")
        finally:
            for s, pid in held:
                release(s, pid)
            if held:
                print("\nreleased")
        return 0 if ok else 2
    finally:
        drop_found()
        lib.libusb_exit(ctx)


if __name__ == "__main__":
    sys.exit(main())
