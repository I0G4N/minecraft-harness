# 同机多 runner 架构改造方案

状态：**Phase 1（独立进程）与 Phase 2（per-runner 容器）均已实现并通过测试**。
runner 数量由 `agents.ids` 动态派生（1..16 玩家，`observer.mode: llm` 时再加一个
Observer）；真实 Minecraft 的 identity/launcher/bot Compose 由
`scripts/prepare_deployment.py` 按同一人数生成。实验保持暂停，不自动启动。

## 已实现的目标架构

```text
Control Plane（Supervisor Python 进程，run-paper.sh 启动）
  ├── world/server manager
  ├── Recorder（SQLite ledger，单写者）
  ├── Budget（原子 reserve/settle）
  ├── MemoryStore
  ├── ExecutionGateway / submission broker
  ├── WorkspaceProvisioner + WorkspaceTools broker（Docker shell 留在控制面）
  ├── evaluator / report / dashboard / viewer
  └── RpcServer（Unix socket，每 run 随机 token）
        ↑
Agent runner player1（独立 OS 进程或独立容器）
Agent runner player2
Agent runner player3
Agent runner player4
Observer runner
```

`runtime.runner_mode` 三档：

```text
inprocess  旧行为，兼容历史 manifest
process    每个 agent 一个独立 Python 进程（Phase 1）
container  每个 agent 一个独立容器（Phase 2；配置默认推荐）
```

每个 runner 自己拥有：

- AgentLoop / ObserverAgent / context / compaction；
- 自己的模型客户端（从 env 读取模型密钥）；
- 自己的 checkpoint / history（仅 `run_dir/agents/<id>`）；
- 自己的 RPC token。

runner 不拥有：

- 其他玩家的 bot token（runner env 中根本没有 bot token）；
- world/server 管理权限；
- Docker socket（shell 由控制面的 WorkspaceTools 执行）；
- ledger/budget/memory 的原始文件句柄；
- 其他 agent 的 artifact 或 state。

## 代码位置

| 组件 | 文件 |
|---|---|
| RPC 协议 / server / client | `src/minecraft_harness/runner_rpc.py` |
| runner 进程入口 + 远端服务代理 | `src/minecraft_harness/runner.py` |
| 控制面 broker：ServiceHub / ArtifactVault / RunnerManager | `src/minecraft_harness/runners.py` |
| Supervisor 集成 | `supervisor.py` (`_start_runner_agents`, `_runner_environment`, `_runner_lost`) |
| AgentCore/AgentLoop/Observer 注入点 | `agents/core.py` (`state_root`, `workspace`, `artifact_api`) |
| 配置 | `runtime.runner_mode = inprocess | process | container` |
| 契约文档 | `RUNNER_ARCH_CONTRACT.md` |
| 测试 | `tests/test_runner_rpc.py`, `tests/test_runner_entry.py`, `tests/test_runner_integration.py` |

## 服务边界

runner 通过本地 Unix socket RPC 访问：

```text
recorder.append / put_artifact / read_events(observer only) / latest_event_seq
budget.validate_request / reserve / settle / stop_requested / wait_admission
memory.read / write / list
gateway.execute / submit / status / cancel / wait / jobs / cancel_job / read_events
workspace.read / write / remove / list / shell
artifact.grant / owned / read
supervisor.observer_infra_issue（仅 observer）
runner.ready / heartbeat / finished（runner -> 控制面）
```

控制面保证：

- ledger 单写者：所有 append 由 Supervisor 进程写入；RPC 并发通过 Recorder 锁串行化，`event_seq` 全局单调；
- budget reserve/settle 原子；runner 死亡时 `Budget.recover_abandoned` 以最坏预留费用释放 pending reservation，避免预算永久冻结；
- memory 由中央 MemoryStore 管理，actor_id 由 RPC 绑定身份强制；
- artifact ACL 由控制面 ArtifactVault 维护，runner 只能读已经发给自己的 artifact；
- workspace shell 由控制面 broker 执行，runner 不持有 Docker socket；
- RPC 使用每 run 每 agent 随机 token，socket 目录 0700、socket 0600；短路径放在 `/tmp/mc-runner-rpc-*` 以避免 AF_UNIX 108 字节限制。

## Phase 2：per-runner 容器

`runner_mode: container` 时，`RunnerManager` 用 Docker 启动一个独立容器：

- 只挂载：`<repo>/src` 与 venv `site-packages`（ro）、本 agent 的
  `run_dir/agents/<id>`（rw，挂到 `/state`）、该 agent 的 spec 文件（ro）、
  **单个 RPC socket 文件**（rw，挂到 `/run/mc-runner-rpc/control.sock`）；
- **不挂载** Docker socket、bot token、ledger/memory、其他 agent 的 state、world/server；
- `--read-only` rootfs + `/tmp` tmpfs、`--cap-drop=ALL`、`no-new-privileges`、
  `--pids-limit=256`、`--memory`、`--cpus`、独立容器名 + 归属 label；
- 容器内 Python 来自 `runner_container_image`（默认 `minecraft-harness-workspace:local`，
  内含 Python 3.11）；harness 源码与 venv site-packages 分别以只读目录挂载提供；
  不会挂载整个仓库，因此 `infra/server-data`（世界/日志）和历史 `runs/` 都不可见；
- 模型密钥通过显式 `-e` 传入；env 白名单只允许 PATH/代理/model/web 密钥名；
- 停止时先 `docker kill --signal=SIGTERM` 让 runner 走优雅 checkpoint 收尾，超时再
  `docker rm -f`；崩溃/心跳超时由 manager 删除容器并在同一 run 内重启；
- Supervisor 启动时按 run label fence 上次残留的 runner 容器；
- RPC socket 必须位于 host-shared 短路径（默认取 run_dir 的父目录，可用
  `runtime.runner_container_shared_dir` 覆盖），保证 supervisor 与 Docker daemon
  看到同一个 Unix socket。

容器镜像默认值面向当前部署；若要完全自包含，可构建专用 runner 镜像（见下文 Phase 2
后续），当前实现用 workspace 镜像提供 Python，仓库和 site-packages 走只读挂载。

## 进程生命周期

- `RunnerManager` 为每个 agent 启动 `sys.executable -m minecraft_harness.runner`
  （process）或 `docker run ... python -m minecraft_harness.runner`（container）；
- 启动后等待 `runner.ready`（超时 = `runtime.runner_startup_timeout_s`）；
- 每 `heartbeat_s` 秒心跳；超过 `runtime.runner_heartbeat_timeout_s` 无心跳则 SIGKILL 并重启；
- runner 崩溃（非 0 退出且没有 `runner.finished`）时：
  - `RpcServer.cancel_agent` 取消该 agent 仍在执行的长 RPC handler（shell/gateway）；
  - 预算清理；
  - 从 checkpoint + 中央 ledger 重启该 runner；
  - 其他 runner 不受影响；
- 重启上限 `runtime.runner_restart_limit`，超过则整个 run 标记 infra_failed；
- SIGTERM/SIGINT：runner 取消 agent task，写 checkpoint，发送 `runner.finished(status=stopped)`，退出 0；
- Supervisor 启动时会 fence manifest 中记录的上一次残留 runner PID。

暂停/恢复：

- `pause` 清除 `budget.admission` 和 `gateway.admission`；
- runner 在 turn/tool 边界调用 `budget.wait_admission` 长轮询，暂停期间模型调用计数冻结；
- `resume` 重新放行；`stop` 让 runner 退出并进入正常收尾。

## 配置

新增 `runtime` 字段（默认值保持旧的单进程行为）：

```yaml
runtime:
  runner_mode: process              # inprocess（默认兼容旧 manifest）| process
  runner_restart_limit: 5
  runner_startup_timeout_s: 90
  runner_heartbeat_timeout_s: 180
  # container 模式额外字段
  runner_container_image: minecraft-harness-workspace:local
  runner_container_python: /usr/bin/python3.11
  runner_container_network: bridge   # 只允许 bridge 或 none
  runner_container_memory_mb: 1024
  runner_container_cpus: 1.0
  runner_container_read_only: true
  runner_container_shared_dir: null   # 默认自动选 run_dir 的短父目录
```

已启用的配置：

- `configs/minecraft-paper-runner-20261022.yaml`：**container 模式**，也是 `run-paper.sh`
  下一次新鲜 run 的默认配置；
- `configs/minecraft-paper-whole-session.yaml`：process 模式（显式回退）；
- `configs/minecraft-paper-final-20261022.yaml`：保持 inprocess 默认值，用于兼容暂停 run 的 manifest。

`configs/minecraft-paper-final-20261022.yaml` **保持不显式设置**，这样暂停中的
`final11`（旧 manifest，无 runner_mode 字段）仍可按 inprocess 默认值 resume；
旧 manifest 用新代码校验后自动补 `runner_mode=inprocess`，不会改变配置身份。

## 验证

完整 Python suite：

```bash
docker run --rm -e PYTHONPATH=/repo/src \
  -v /ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness:/repo \
  -v /home/swarmbrain:/home/swarmbrain:ro -w /repo --network host \
  minecraft-harness-launcher-base:docker-cli-20261021 \
  /repo/.venv/bin/python -m unittest discover -s tests -q
```

结果：**420 tests OK**（含 23 RPC tests、47 runner-entry tests、4 process-runner integration tests）。

Process-runner 集成测试验证：

- process：每个 player 和 Observer 是独立 OS 进程（不同 PID）；
- container：每个 agent 是独立容器；`docker inspect` 验证只挂载源码/本 agent state/spec/RPC
  socket，且无 Docker socket、无 bot token、read-only rootfs、cap-drop ALL、内存/CPU/PID 限制；
- fake runtime 下 2-player 运行 `diagnostic_pass`，manifest/prompt/tools hash 与进程内一致；
- process 与 container 下 `kill` 一个 runner 后只有该 runner 重启，另一个 identity 不变；
- pause 后模型调用计数冻结、runner 容器/进程不变，resume 后继续，stop 正常收尾；
- 容器 stop 后无残留容器、无残留 RPC socket 目录；
- 深目录 run_dir（超过 AF_UNIX 路径限制）下仍能建立 RPC。

## 仍然存在的差异 / 风险

1. **process 模式仍是同 uid 进程边界**：可通过 `/proc` 或文件系统读取控制面可读内容。
   **container 模式**（推荐）才提供挂载/命名空间/capability/cgroup 边界。
2. **container 模式仍不是 5 台云机器**：所有 runner 与 Supervisor 在同一台 host 上；
   Phase 3 才把 runner 镜像部署到多台 VM 并用 mTLS RPC。
3. container 目前复用 workspace 镜像的 Python；若要完全自包含/最小攻击面，应构建专用
   `runner` 镜像（含 harness 与依赖，不挂源码和 site-packages）。
4. runner 容器内以 root 运行，但 rootfs 只读、cap-drop ALL、no-new-privileges，且只挂
   src/site-packages/本 agent state/spec/RPC socket；不是 userns 隔离，未来可加 `--user`
   与 cgroup/userns 策略。
5. Supervisor 被 SIGKILL 时，runner 容器会在 RPC 失败后约 1 秒自停，但 host-shared
   `.mc-runner-rpc-*` 目录可能残留；resume 会复用并在正常 stop 时删除，未 resume 的
   废弃 run 需要操作者清理。
3. 模型仍是 DeepSeek `deepseek-flash`，不是论文的 Gemini 4 Argon；
   同机 5 进程也不是论文的 5 台云机器。
4. 论文的 literal CLI 终端 vs tool-call shell envelope、context 上限、
   web 后端等既有差异不变（见 `PAPER_AUDIT_2026-10-22.md`）。
5. 控制面本身仍是单点：Supervisor 崩溃会终止整个 run（resume 从 checkpoint
   恢复）；runner 崩溃则被隔离重启。

## 下一步（可选）

- Phase 2：把 `runner.py` 包成 per-agent 容器，仅挂载 RPC socket 目录和本 agent
  state 目录；控制面 broker 保留 workspace shell 与 ledger。
- 如需完全消除 `/proc` 泄漏，必须使用容器或独立 uid。
- 在真正启动下一次论文实验前，用 `configs/minecraft-paper-runner-20261022.yaml`
  先跑一次 smoke（server/bots/launcher 就绪后由 wrapper 启动）。
