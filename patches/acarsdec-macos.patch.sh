#!/bin/bash
# Make acarsdec build on macOS. Three upstream assumptions break here:
#
#  1. cmake_minimum_required is below what CMake 4 accepts   -> policy override
#  2. HOST_NAME_MAX is a GNU/Linux constant                  -> define it (the
#     buffer is sys_hostname[HOST_NAME_MAX+1], so array and index come from the
#     same macro; 255 is internally consistent, not an overflow)
#  3. pthread_tryjoin_np is a GNU extension with no macOS equivalent
#
# (3) is the only real code change, and it is confined to a SHUTDOWN path:
# rtl.c polls the reader thread for up to 10s, then raises SIGKILL. It is not
# in the decode path. The shim returns EBUSY while the thread is alive and
# reaps it once gone; the upstream SIGKILL fallback is left intact, so the
# worst case is identical to upstream.
set -euo pipefail
SRC="${1:-$HOME/bandwatch/tools/acarsdec}"
F="$SRC/rtl.c"
grep -q "ACARSDEC_MACOS_SHIM" "$F" && { echo "  already patched"; exit 0; }

python3 - "$F" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
shim = '''
/* ACARSDEC_MACOS_SHIM: pthread_tryjoin_np is a GNU extension. This is used
 * only in the shutdown wait below, never in the decode path. */
#if defined(__APPLE__)
#include <errno.h>
#include <signal.h>
static int pthread_tryjoin_np(pthread_t th, void **ret)
{
	if (pthread_kill(th, 0) == 0)
		return EBUSY;          /* still running */
	return pthread_join(th, ret);  /* exited: reap it */
}
#endif
'''
anchor = '#include "acarsdec.h"'
if anchor in s:
    s = s.replace(anchor, anchor + "\n" + shim, 1)
else:
    lines = s.split("\n")
    last = max(i for i, l in enumerate(lines) if l.startswith("#include"))
    lines.insert(last + 1, shim)
    s = "\n".join(lines)
open(p, "w").write(s)
print("  shim inserted into rtl.c")
PY
