# Runtime resource paths and memory limits

Status: 2026-10-24. All experiment resources are under `/ssd_data/caizihao`.

| Resource | Path |
| --- | --- |
| Host wrapper/repo | `/ssd_data/caizihao/minecraft/minecraft-harness` |
| Deployment harness (H) | `/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness` |
| Run ledgers/reports | `/ssd_data/caizihao/minecraft/minecraft-harness/mc-runs/<run>` |
| Supervisor/run logs | `/ssd_data/caizihao/minecraft/minecraft-harness/mc-logs` |
| Minecraft server data/world | `/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness/infra/server-data-*` |
| Pi runtime / extensions | `/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness/pi_runtime` |
| Pi/agent sessions, artifacts, memory | under each run directory |
| Server jar / build cache | `/ssd_data/caizihao/minecraft/minecraft-harness/infra/.cache` |

Docker daemon storage remains `/var/lib/docker` on the root filesystem because it
is shared by fleet/gitlab/nextcloud and other host workloads. Moving the Docker
data-root requires a daemon restart and a maintenance window; do not change it
from a run wrapper. Experiment data and logs do not use that path.

## Memory limits (research profile)

- detached Supervisor container: **8g** (`PAPER_SUPERVISOR_MEMORY` override)
- Minecraft server container: **8g**, Java `-Xms1G -Xmx3G`
- per-execution worker sandbox: **4g** (`WORKER_MAX_MEMORY`)
- bot runtime worker RSS guardian: **2 GiB** (`MAX_WORKER_RSS_BYTES`)
- bot / identity / launcher containers: **512m** each

Root filesystem is still close to full because of Docker image/build cache; keep
several GiB free and prune dangling images/cache before long runs.

## PIDs limits

- server / bot / identity / launcher: **256 pids**
- worker sandbox under runsc: `--pids-limit` is not supported; gVisor provides
  process isolation and the launcher-level limits above were raised.
