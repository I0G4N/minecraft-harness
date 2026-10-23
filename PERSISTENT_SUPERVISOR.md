# 长跑持久化 Supervisor

## 背景：2026-10-23 90 分钟 run 为什么提前结束

19:13 的收尾报告不是原因，真正结束发生在 **18:57:18**：

```text
systemd[...]: dsh-subprocess-...scope: Sending signal SIGTERM to process
              (bwrap/bash/python/docker) on client request
```

DSH 客户端请求清理它启动的子进程 scope，连带终止了：

- `run-paper.sh --start`
- `python -m minecraft_harness.cli run`
- Docker 栈本身没有被杀掉，所以留下 orphan containers

因此这是**进程托管方式**的问题，不是模型、预算、wall 或 Minecraft server 崩溃。
之后 resume 又遇到 `DeploymentInspectionError: Formal runtime deployment boundary not confirmed`，
最终把 run 收尾为 `infra_failed`。

## 修复：把 Supervisor 放进独立 Docker 容器

`run-paper.sh` 现在支持：

```bash
PAPER_SUPERVISOR_CONTAINER=1 ./run-paper.sh --start
```

流程：

1. wrapper 仍负责 preflight、compose up、等待 server `Done (`、bot health；
2. 然后启动一个 **detached Docker 容器** `<project>-supervisor`；
3. wrapper 退出，但 Supervisor 由 Docker daemon 托管，不再属于 DSH/SSH 的 cgroup；
4. Supervisor 容器内运行 `scripts/container_supervisor.sh`：
   - `python -m minecraft_harness.cli run`
   - 结束后保存/停止 server，`docker compose down`
   - 记录 `SUPERVISOR_CONTAINER_EXIT`

容器挂载：

- 宿主 harness / deployment 目录（同绝对路径）
- `/var/run/docker.sock`
- `/usr/bin/docker` 和 docker compose plugin（只读）
- 环境文件（含 model key，0600，不写入 run dir）

Supervisor 以宿主 uid/gid 运行，并加入 Docker socket 的 group（默认 gid 999，可用
`PAPER_DOCKER_GID` 覆盖）。

## 使用

```bash
cd /ssd_data/caizihao/minecraft/minecraft-harness

set -a
. /ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness/.tmp-.. /env
set +a

export PAPER_ENV_FILE=...
export PAPER_CONFIG=...
export PAPER_COMPOSE_FILE=...
export PAPER_RUN_DIR=...
export PAPER_LOG=...

./run-paper.sh --prepare
PAPER_SUPERVISOR_CONTAINER=1 ./run-paper.sh --start
docker logs -f <project>-supervisor
```

结束后：

```bash
docker ps -a --filter name=<project>-supervisor
```

## 验证

2026-10-23 2 分钟验证 run：

- wrapper 启动后立即退出；Supervisor 容器继续运行；
- 4/4 players + Observer ready；
- model calls 正常运行；
- 运行结束后容器自动执行 cleanup，`SUPERVISOR_CONTAINER_EXIT ... rc=0`；
- bot/server 栈已 down；
- viewer 端口在 bot 活跃期间返回 200。

## 注意

- 这个修复解决的是**客户端/SSH 断开导致 Supervisor 被 SIGTERM**；
- viewer 仍依赖 bot execution worker，worker 重启时视频短暂 502 是另一个问题；
- 正式长跑仍建议：
  - 使用独立的 run dir / fresh world；
  - 监控 `pi_session_sample/alert/rotated`、resource、observer；
  - 长 run 前先做 10–15 分钟持久化验证。
