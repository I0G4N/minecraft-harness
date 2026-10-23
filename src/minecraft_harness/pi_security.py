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
import os
import struct
import sys
import threading
import time
from pathlib import Path


PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3

# NOTE (verified 2026-10-23): PR_SET_DUMPABLE=0 applies to the calling process
# only. Linux resets the dumpable attribute to 1 on execve(2), so neither a
# ``preexec_fn`` nor a prctl-then-exec wrapper can leave the Pi CLI child
# non-dumpable. We therefore keep the runner non-dumpable (it holds the real
# credentials) and remove every secret from the Pi child environment instead
# (bridge token via a one-shot inherited FD, MC token via a 0600 file, no model
# keys). The residual /proc/<pi>/environ exposure then contains only paths and
# non-secret routing values plus the already-closed bridge FD number.


class CredentialHygieneError(RuntimeError):
    pass


def set_process_dumpable(dumpable: bool = False) -> bool:
    """Set the calling process' dumpable flag; returns the applied bool."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        result = int(prctl(PR_SET_DUMPABLE, 0 if not dumpable else 1, 0, 0, 0))
    except (OSError, AttributeError, TypeError) as exc:
        raise CredentialHygieneError(
            "could not change process dumpability") from exc
    if result != 0:
        errno = ctypes.get_errno()
        raise CredentialHygieneError(
            "prctl(PR_SET_DUMPABLE) failed with errno " + str(errno))
    return dumpable


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


PRCTL_GUARD_FILENAME = "prctl_guard.so"


def prctl_guard_path(pi_runtime_root: Path | str) -> Path:
    """Absolute LD_PRELOAD guard path inside the pinned Pi runtime tree."""
    return Path(pi_runtime_root) / PRCTL_GUARD_FILENAME


def _split_proc_net_address(value: str) -> tuple[str, int] | None:
    try:
        address, port = value.split(":")
        return address.lower(), int(port, 16)
    except (AttributeError, TypeError, ValueError):
        return None


def _proc_net_client_inode(peer_port: int, local_port: int) -> int | None:
    """Inode of the peer's TCP socket (local=peer_port, remote=local_port)."""
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10:
                continue
            local = _split_proc_net_address(fields[1])
            remote = _split_proc_net_address(fields[2])
            if local is None or remote is None:
                continue
            # The peer holds the client-side socket: its local port is the
            # remote port we observe and its remote port is our local port.
            if local[1] != peer_port or remote[1] != local_port:
                continue
            try:
                inode = int(fields[9])
            except (TypeError, ValueError):
                continue
            if inode > 0:
                return inode
    return None


def _pid_owning_socket(inode: int) -> int | None:
    target = f"socket:[{inode}]"
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            descriptors = os.scandir(f"/proc/{pid}/fd")
        except OSError:
            continue
        try:
            for descriptor in descriptors:
                try:
                    if os.readlink(descriptor.path) == target:
                        return pid
                except OSError:
                    continue
        finally:
            descriptors.close()
    return None


def loopback_peer_pid(connection, *, attempts: int = 5,
                      delay_s: float = 0.01) -> int | None:
    """Resolve the PID that owns the client side of a loopback TCP connection.

    The Pi runner shares the host PID namespace with the Pi CLI, so a robust
    /proc/net/tcp{,6} inode lookup + /proc/<pid>/fd scan identifies the peer
    process without trusting the TCP source port (a shell can also connect to
    loopback). Returns None when the peer cannot be resolved.
    """
    try:
        peer = connection.getpeername()
        local = connection.getsockname()
    except OSError:
        return None
    if not isinstance(peer, tuple) or not isinstance(local, tuple):
        return None
    if len(peer) < 2 or len(local) < 2:
        return None
    try:
        peer_port = int(peer[1])
        local_port = int(local[1])
    except (TypeError, ValueError):
        return None
    for attempt in range(max(1, int(attempts))):
        inode = _proc_net_client_inode(peer_port, local_port)
        if inode is not None:
            pid = _pid_owning_socket(inode)
            if pid is not None:
                return pid
        if attempt + 1 < max(1, int(attempts)):
            time.sleep(max(0.0, float(delay_s)))
    return None


# The prctl guard library reports successful loopback connects as
# (pid, local_port, family) 12-byte records on an inherited, CLOEXEC pipe.
PEER_REPORT_FD_ENV = "HARNESS_PRCTL_GUARD_REPORT_FD"
_PEER_REPORT_RECORD = struct.Struct("<III")


class LoopbackPeerRegistry:
    """Map loopback source ports to the process that opened them.

    The LD_PRELOAD guard in the Pi process writes one record per successful
    loopback connect over an inherited pipe. The guard sets FD_CLOEXEC on the
    pipe in the constructor, so exec'd tool children cannot inherit (or forge)
    their writes; a shell that bypasses the guard simply never registers and is
    rejected by the allowlist.
    """

    def __init__(self, read_fd: int) -> None:
        self._fd = int(read_fd)
        self._lock = threading.Lock()
        self._ports: dict[int, int] = {}
        self._closed = False
        self._thread = threading.Thread(target=self._read_loop,
                                        name="pi-peer-registry", daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = os.read(self._fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while len(buffer) >= _PEER_REPORT_RECORD.size:
                pid, port, _family = _PEER_REPORT_RECORD.unpack_from(buffer)
                with self._lock:
                    self._ports[port] = pid
                buffer = buffer[_PEER_REPORT_RECORD.size:]
        with self._lock:
            self._closed = True

    def lookup(self, port: int, *, wait_s: float = 0.2) -> int | None:
        """PID that registered ``port``; short wait covers connect-report races."""
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            with self._lock:
                pid = self._ports.get(int(port))
                closed = self._closed
            if pid is not None or closed or time.monotonic() >= deadline:
                return pid
            time.sleep(0.001)

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass
