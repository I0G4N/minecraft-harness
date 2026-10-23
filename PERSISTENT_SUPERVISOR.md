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

## 2026-10-23 15 分钟 run 的收尾误判：cleanup_failed

15 分钟 run（20:04–20:19）已经跑满 wall 并把 world 完整保存，但报告是
`infra_failed`。真正原因不是实验/预算/server 失败，而是**关闭 session 的单个 HTTP
请求超时**。

### 时间线（UTC）

- `12:19:42`：wall deadline 到点，Supervisor 停止 Pi runners；
- player1 仍有 `execution f19f342a...` 在运行（`timeout_ms=280000`，12:17:57 启动；
  wall 结束时还差约 2 分半才自然结束）；
- `gateway.close()` → `fence()` 并发 DELETE 各 agent 的 session；当时使用普通
  `runtime.request_timeout_s=10`；
- DELETE 触发 runtime `Session.close()`：先 `finish(current)`，再
  `killWorker('session_closed')` → remote worker `stop()` / `confirmStopped()`；
  remote worker 的确认 deadline 是 15s，launcher 的 `docker rm -f` / `inspect`
  各自还有 15s 超时，因此 teardown 合法地超过 10s；
- `12:19:53.088`：`TimeoutError` → `runtime_shutdown_unconfirmed`；
- 由于是外部 runtime URL 模式（`auto_spawn=false`），没有 owned-process fallback，
  `cleanup_failed` 把整个 run 标记为 `infra_failed`；
- 随后 world save / server stop / compose down 仍然执行，但残留了
  `mc-worker-paper4-15min-player1-984b5577`，印证 sandbox 当时仍在 teardown。

### 根因

1. 普通控制面 `request_timeout_s=10` 被复用到了 session DELETE；DELETE 的语义是
   “停止 sandbox + 确认旧进程退出 + 返回最终事件尾”，耗时天然更长；
2. `gateway.close()` 的 fence 只有一次尝试，没有在超时后重试幂等 DELETE；
3. 外部 runtime URL 模式没有 owned-process fallback；
4. `docker compose down` 不负责 launcher 创建的 per-execution worker sandbox，
   超时后容易留下 orphan 容器/网络。

### 修复

- 新增 `runtime.close_request_timeout_s`（默认 30s）和
  `runtime.close_timeout_s`（默认 60s）；
- `gateway.close()` 的 fence 在 bounded deadline 内重试幂等 GET/DELETE；
  只重试 `RuntimeUnavailable`（transport/超时），
  `RuntimeReliabilityError`（协议/审计矛盾）绝不重试；每次重试记录
  `runtime_shutdown_retry`；
- `scripts/container_supervisor.sh` 和 `run-paper.sh` 在 teardown 时按项目名
  `mc-worker-<project>-*` 清理 launcher 创建的 worker sandbox 残留。

### 验证

- 单元回归：`tests/test_gateway_longrun.py` 新增慢 DELETE 超时后重试成功测试；
  gateway 全量 13 tests 通过；
- 真实 1 分钟强制重试 run（`smoke-teardown-3`，故意设
  `close_request_timeout_s=0.2`）：4 个 agent 的 DELETE 第 1 次全部超时，player1/2/3
  第 2 次再次超时，共 **7 次 `runtime_shutdown_retry`**；随后全部成功，
  `run_status=budget_exhausted`，`SUPERVISOR_CONTAINER_EXIT ... rc=0`，
  无 `cleanup_failed`/`runtime_shutdown_unconfirmed`，worker/network 全部清理；
- 对照 1.5 分钟 run（`smoke-teardown-2`，`close_request_timeout_s=2`）：
  81 model calls / `$0.175`，rc=0，无 retry（teardown <2s），同样通过。
