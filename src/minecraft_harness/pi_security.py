"""Linux credential-hygiene helpers for Pi/runner processes.

The Pi model proxy and runner RPC client need the upstream model key and the
runner token in their own environment. A model-directed Pi bash tool runs in a
child process with the same uid and can normally read
``/proc/<runner>/environ``. Clearing the process dumpable flag makes that
environment readable only by processes with CAP_SYS_PTRACE (not by the Pi
child), while keeping normal child spawning and fd-passing unchanged.
"""
from __future__ import annotations

import ctypes
import sys


PR_SET_DUMPABLE = 4


class CredentialHygieneError(RuntimeError):
    pass


def harden_process_credentials() -> bool:
    """Disable same-uid /proc/<pid>/environ access on Linux.

    Returns True when the flag was applied, False on non-Linux platforms. On
    Linux, failure to apply the flag is fatal so a run cannot silently expose
    the model key or runner token to a model-controlled shell.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        result = int(prctl(PR_SET_DUMPABLE, 0, 0, 0, 0))
    except (OSError, AttributeError, TypeError) as exc:
        raise CredentialHygieneError(
            "could not disable process dumpability for credential hygiene") from exc
    if result != 0:
        errno = ctypes.get_errno()
        raise CredentialHygieneError(
            "prctl(PR_SET_DUMPABLE, 0) failed with errno " + str(errno))
    return True
