# Worker sandbox

**Current host state (2026-10-21):** gVisor `runsc release-20260608.0` is installed at
`/usr/local/bin/runsc`; `/etc/docker/daemon.json` registers `runsc` and
`runsc-debug`, and Docker live-restore is enabled. The paper launcher default is
`WORKER_SANDBOX_RUNTIME=runsc`; a smoke run of the worker image under
gVisor is verified and reports the `4.19.0-gvisor` kernel. The repo keeps a copy
of the same binary at `infra/bin/runsc` for reproducibility.

The generated Minecraft programs execute in a separate per-agent worker
container. The deployment has three layers, in order of trust:

1. **OCI runtime sandbox (recommended: gVisor `runsc`)** — mature open-source
   project from Google: <https://gvisor.dev/> / <https://github.com/google/gvisor>.
   It provides a user-space kernel and syscall boundary around the worker
   container. Compose exposes `WORKER_SANDBOX_RUNTIME` (empty by default):
   ```bash
   WORKER_SANDBOX_RUNTIME=runsc docker compose ... up -d
   ```
   After installing gVisor (`runsc install`, then register the runtime with the
   Docker daemon). The launcher passes `--runtime <name>` to `docker create`;
   it never accepts this from RPC.
2. **Container hardening** — read-only root, dropped capabilities,
   no-new-privileges, separate network namespace with only the fixed identity
   proxy, memory/PID/CPU limits, no host mounts.
3. **In-process guards (defense in depth, not security boundaries)** —
   Node module allowlist, read-only `fs` Proxy (`EPERM` on writes), and the
   existing seccomp/LD_PRELOAD worker image. These reduce mistakes but must not
   be described as the sandbox.

Alternatives considered: nsjail (<https://github.com/google/nsjail>) and
bubblewrap (<https://github.com/containers/bubblewrap>) are mature Linux
sandboxes. gVisor is used here because it is OCI-compatible and fits the
launcher's existing `docker create` path without weakening the fixed
image/network/read-only guarantees.

The local fake/unit-test worker has no Docker sandbox. Do not expose it to
untrusted code or use it to validate security claims.

Operational checks:
- `docker inspect <worker>` must show the expected runtime (when using runsc).
- The launcher must not accept arbitrary Docker args or mounts.
- Every paper/aligned run must use the remote worker image; `paper_aligned`
  Minecraft refuses to fall back to a local in-harness worker.
