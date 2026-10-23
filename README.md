# Minecraft 多 Agent 实验框架

依据 [A team of Gemini 4 Argon agents beats Minecraft](https://arxiv.org/pdf/2610.22319) 的公开方法，独立实现编程 Agent、Mineflayer 程序执行、协作记忆和证据评估。源码不导入本仓库现有产品运行时；[设计方案](../../harness_embodiment_work/Minecraft多Agent实验框架实现方案.md) 说明了方法基线和工程选择。

已实现 `strict` 与 `paper_aligned` 两种模式、受监管后台任务、独立 LLM Observer、编程容器、身份代理及证据/费用报告。逐节、逐图的实现追踪与差异见 [PAPER_ALIGNMENT.md](PAPER_ALIGNMENT.md)。真实 Docker worker 和 Mineflayer 隔离协议联调通过；宿主代理遗漏新 Docker 网段的原因已确认，验证时只临时豁免自建 peer 地址并全部撤销。正式部署仍需正确配置宿主代理。真实 Vanilla 和模型能力实验另行验收。

paper 模式的下一次运行另见 [PI_METHOD_ALIGNMENT.md](PI_METHOD_ALIGNMENT.md)：模型可见的 prompt/工具/环境说明按论文 §2.1–2.3 的公开类别收敛（`paper-framework-v4`），内部 context/记忆/恢复借鉴 Pi 的 context projection、compaction、effect sandwich 与记忆检索；`final11` 已停止并归档；6-agent 全栈 smoke 见 SIX_AGENT_SMOKE_2026-10-23.md。

## 快速运行离线实验

首版运行在 Linux/WSL2，使用 POSIX 进程锁；需要 uv、Python 3.11+、Node.js 22。uv 可为缺少 Python 3.11 的主机安装对应解释器。

```bash
cd experiments/minecraft_harness
uv sync --python 3.11 --locked
npm --prefix bot_runtime ci
uv run mc-harness doctor --config configs/offline-smoke.yaml
uv run mc-harness run --config configs/offline-smoke.yaml
```

命令返回 `run_dir`、`status`、`evaluation` 和报告路径。示例使用 FakeModel 与假 bot，四个 Agent 独立执行观察、聊天和事件读取；成功状态为 `diagnostic_pass`，`capability_success` 为 `null`。

```bash
uv run mc-harness status runs/<run-id>
uv run mc-harness report runs/<run-id>
uv run mc-harness serve runs/<run-id> --port 8899
```

## 实验观测台

```bash
uv run mc-harness serve runs --port 8899
```

打开 [本地观测台](http://127.0.0.1:8899/)，在顶部切换实验。实验总览、事件时间线、程序与任务、协作聊天、证据与评估、Observer、资源与费用共七个视图；可筛选和分页查看事件，打开程序源码/回执及证据详情，下载原始报告。手机也可查看。单运行目录的 `serve runs/<run-id>` 仍可用。

服务只绑定 `127.0.0.1`，只发现所选目录的直接子目录。浏览历史记录不会启动实验或调用模型；未知用量、证据缺口和未采样的资源历史保持明确。控制按钮需二次确认、本地操作者 token 和同源检查，再写入持久队列；返回的 `control_id` 用于核对执行结果。已退出实验的恢复通过 CLI 发起。使用说明、数据口径和浏览器验收见 [DASHBOARD.md](DASHBOARD.md)。CLI 和运行目录属于可信操作者，运行产物保存在 git-ignore 的 `runs/`。

[最终看板验收](validation/dashboard/acceptance.json)通过，包含真实浏览器交互和手机窄屏检查，原实验数据未改；[桌面预览](validation/dashboard/acceptance-browser/desktop-overview.png)和[手机预览](validation/dashboard/acceptance-browser/mobile-overview.png)可直接查看。

## 观测台界面

paper/whole-session 模板默认使用 DeepSeek `deepseek-flash`；还提供 `paper_tight_interface`（隐藏 history/artifact 工具和自动快照）、`leftover_policy: allow`、program-output chat、只读 Node fs sandbox 和存档世界地图。

观测台采用 Minecraft GUI 风格（分组物品槽导航、粘性运行 HUD、石质按钮、像素字体、末影/红石/金锭图标，
以及图表的 Minecraft 书页边框）；桌面侧栏可收起，≤980px 为抽屉导航，总览有快速入口，表格吸顶表头，
证据阶段按需展开，论文图表支持专注模式。装饰纹理来自 CC BY-SA 4.0 的 Pixel Perfection Legacy，像素
中文/拉丁字体为 SIL OFL 1.1 的 Cubic 11；这两类资产只在本地 dashboard 使用，不进入游戏运行时或评分证据。
来源与许可见 [ui-assets/ATTRIBUTION.md](src/minecraft_harness/static/ui-assets/ATTRIBUTION.md)。

## 论文图表的动态可视化

观测台「论文图表」页把论文的图 1/3/4/5/6/8/12/13/14 按本 run 的账本与服务端证据重建为可播放、
可交互的时间线/阶段/聊天/行为图；支持 Fig.3 泳道、Fig.4 十三步进度、聊天回放、行为与死因，并明确标出
尚未采集的末地战斗数据、3D 世界渲染与 in-game days。图表用 HTML/CSS 渲染，物品图标与玩家头像取自
CC BY-SA 4.0 的 Pixel Perfection Legacy 材质包（仅 dashboard 视觉资产）。详见
[观测台说明](DASHBOARD.md#论文图表的动态回放)。

## 实时第一视角（直播）

观测台的「实时第一视角」页可用 `prismarine-viewer` 并排展示每个玩家 bot 的真实游戏画面（浏览器实时渲染，
不是视频录像）。启用：bot 环境设置 `VIEWER_PORT`，实验配置写 `runtime.viewer_ports`；远程访问通过 SSH
隧道转发工作台端口和 viewer 端口。详见 [观测台说明](DASHBOARD.md#实时第一视角可选)。

## 真实 Minecraft 和模型

论文对齐模式使用 [minecraft-paper-aligned.yaml](configs/minecraft-paper-aligned.yaml) 和 [compose.paper.yaml](infra/compose.paper.yaml)。模板的模型、单价、调用和费用上限必须先填写；36 小时是运行上限，不会自行开始付费实验。部署步骤、launcher/worker 分离及镜像构建见 [infra/README.md](infra/README.md)。玩家数量不是只能在四个和静态 Compose 之间选择：`scripts/prepare_deployment.py` 按配置的 agent 数量生成精确数量的 identity/launcher/bot 服务、实验配置和 token 环境文件（详见下一节）。下列旧例仍为 strict 模式。

1. 按 [infra/README.md](infra/README.md) 获取并校验 Mojang 1.20.4 服务端，构建镜像。接受 Minecraft EULA 后才把 `ACCEPT_EULA` 设为 `true`；模板默认拒绝启动。
2. 用 `scripts/prepare_deployment.py --agents N` 生成精确 N 个执行端的 Compose、配置和 `infra/.env.<project>`；论文四玩家使用 paper_aligned，其他人数自动使用 strict 生命周期。不要直接改静态 `compose.paper.yaml` 或与其他项目共用 token。服务端使用 Survival/Easy，默认 seed 为 42。新 seed 必须使用新的服务器数据目录。
3. 复制 `configs/minecraft-example.yaml`，填写自己的工具调用模型网关与模型 ID。凭据通过 `MC_MODEL_API_KEY` 提供；每个 `BOTn_ADMIN_TOKEN` 在操作者环境中应与 Compose 相同。不要把密钥写入 YAML。模型配置还必须保证 `model.max_request_bytes` 不小于 `agents.context_chars` 与 `observer.context_chars`（含 Observer 覆盖值），否则预检查直接拒绝，避免运行中途因请求超出字节上限丢失 Observer。
4. 将 `MC_UID`/`MC_GID` 设为操作者的 UID/GID，让服务端和操作者都能读写专用实验数据目录，供保存与恢复使用。配置中的 `world_dir`、`log_path` 指向这个世界。
5. 执行 `uv run mc-harness doctor --config <配置路径>`，通过后运行 `uv run mc-harness run --config <配置路径>`。预检查读取兼容协议的 health 与 session 列表，总网络期限为 5 秒；paper 模式要求 v3。正式准入还验证实际世界规则与身份，然后才允许模型调用。

Python 操作者持有模型凭据。paper 部署的控制端与生成代码分属不同容器，worker 不持有管理凭据，不挂载世界、工作区、记忆、实验账本或 Docker socket。生成代码只通过固定身份代理连接服务端；可信 launcher 才能管理指定 worker 容器。宿主 bot 控制端点绑定 `127.0.0.1:3101..(3100+N)`，viewer 端口为 `(3100+N+1)..(3100+2N)`，不发布游戏端口。旧 strict Compose 使用 RPC relay 与共容器 worker，其权限边界较弱。角色对等，框架不提供采集、战斗、建造等人类编写的策略。

支持的模型接口为工具调用型 Chat Completions HTTP 协议。记录请求/响应、供应商返回的 usage 和工具配对，不假设内部推理内容可获取。模型接口的 `max_retries` 必须为 `0`；可重试故障由 Agent 按指数退避重新请求，每次尝试分别预留团队调用/费用预算，避免隐藏重试绕过预算。请求与响应大小、模型总期限和连续故障次数均有上限。

## 编程 Agent 和权限

每个 Agent 有独立 conversation、checkpoint 和 workspace。工具包括工作区读写、目录查看、游戏程序执行/状态/取消、记忆读写、事件读取和历史细节读取。游戏程序是异步 JS 函数体，可使用 `bot`、`globals`、`globalThis`、`console`、`require` 和受管生命周期工具。

示例只演示接口，不会作为默认技能加载给模型：

```javascript
console.log(bot.entity.position, bot.health, bot.inventory.items());
globals.observationCount = (globals.observationCount || 0) + 1;
return { observations: globals.observationCount };
```

私有笔记只有自己可读写；共享笔记由作者写、队友读，使用 revision/CAS。paper 身份代理在聊天关闭时过滤双向原始协议包，不能用 `_client` 绕过；strict 本地模式仅过滤工具投递，会明确记录这个消融限制。游戏内物品、建筑和移动仍可传递信息。

strict 的 Shell 默认关闭；设置 `workspace.provision: true` 可自动准备独立容器，内含 Git、rg、Python、Node、`mc` 及只读版本文档。容器使用 network=none，通过玩家 Unix socket 调用可信提交账本。自配 shell 容器须提供 `setsid --wait`、sh、timeout 等。外部搜索使用配置的 HTTPS 服务；`web_fetch` 仅访问明确允许的公网主机，缺失能力会进入对齐偏差。

自动准备的编码容器设置单文件 **16 MiB 内核硬限制**（`RLIMIT_FSIZE`，soft/hard 都为 16,777,216 字节）。工作区总字节数和文件数是**监测限制**：文件工具写入前检查，shell 启动前、运行期间每 1 秒及进程清理后检查；默认总字节数为 64,000,000，文件数为 4096。超额时终止 shell 进程组并确认整个编码容器停止，保留源码和超额文件；通过 `workspace_remove` 删除选定文件或空目录回收空间后，后续 shell 会重启同一容器。监测间隔内仍可能超额，默认 5 秒的全局运行目录／剩余磁盘守卫继续生效；这不等同于文件系统总量硬配额。[Docker ulimit 说明](https://docs.docker.com/reference/cli/docker/container/run/#set-ulimits-in-container---ulimit)、[Linux RLIMIT_FSIZE](https://www.man7.org/linux/man-pages/man2/getrlimit.2.html)。

strict 在程序结束后清理异步资源；paper 模式允许 `jobs.start(fn,{ttl_ms})` 注册的任务跨程序存活。数据/函数 globals 在同一 worker 内保留；取消先给最多 1 秒协作窗口，无法确认终止则重建 worker。后台任务有数量、期限、epoch 和日志边界，重启后不自动重放。

`node:vm` 和模块白名单不是安全边界。paper 部署要求远程 launcher、独立 PID/容器、原生文件写入/进程创建限制及固定身份代理，并在接纳游戏会话时检查边界元数据。旧 strict 共容器部署的限制仍见 [基础设施说明](infra/README.md#actual-security-boundary-and-current-limitations)。

## 按 agent 数量动态生成部署

`agents.count` 支持 **1 到 16**。静态 [compose.paper.yaml](infra/compose.paper.yaml) 保留为四人 paper 参考和模板，不再要求操作者手工复制/删除服务块：

```bash
# 生成 6 个玩家 + LLM Observer 的部署（不启动任何容器/实验）
python3 scripts/prepare_deployment.py --agents 6 --project mc-paper-6
# 默认写出：
#   infra/compose.mc-paper-6.yaml       精确 6 个 identity/launcher/bot
#   configs/mc-paper-6.yaml             6 个 runtime.urls/token_envs/viewer_ports
#   infra/.env.mc-paper-6               每玩家的 worker/admin/agent token，mode 0600

# EULA 已明确接受、上一套栈已 docker compose down，且准备好新的 server-data 后：
export PAPER_ENV_FILE=$PWD/infra/.env.mc-paper-6
export PAPER_CONFIG=$PWD/configs/mc-paper-6.yaml
export PAPER_COMPOSE_FILE=$PWD/infra/compose.mc-paper-6.yaml
export PAPER_RUN_DIR=<fresh-run-dir>

<deployment>/run-paper.sh --prepare   # 安全 preflight（不启动容器）：旧栈/网络/端口/world/run_dir/EULA/key/token + 两个 Pi extension + 无启动镜像 probe
<deployment>/run-paper.sh --start     # preflight 通过后 compose up → wait level.dat → marker → bot health → Supervisor
```

`--prepare` 的容器镜像 probe 用 `docker run --rm --network none`（只读挂载源码、venv
site-packages 与 `pi_runtime`）检查 `runner_container_python` 可 import `sys/httpx/yaml`；
Pi runtime 还要求 Node ≥ 22、Pi CLI 和**两个** extension（`minecraft-tools.js` +
`observer-tools.js`）。Docker daemon 不可用或 probe 失败时 preflight 只报告并以 rc=2 拒绝
`--start`，不会创建 compose 容器/网络；`PAPER_IMAGE_PROBE_TIMEOUT_S` 默认 120 s。

`--start` 会拒绝最终 11 或任何旧栈仍在运行、网络/端口仍被占用、world 已绑定别的 run、或
run_dir 非空的情况；它不会自动 stop 旧 run。必须先显式归档/停止旧栈（保留 run 目录），
并用新的 server-data/空 run_dir。

生成规则：

- 四个玩家保持 paper-aligned；其他人数自动使用 strict 生命周期和 strict 的 bot 环境变量。
  论文协议本身固定四玩家，`--mode paper` 对非四人会直接拒绝。
- Compose 中精确生成 N 组 identity/launcher/bot、N 个独立 worker 网络；bot 端口
  `3101..(3100+N)`，viewer 端口 `(3100+N+1)..(3100+2N)`。前四个 worker 子网保持
  现有 paper 部署的取值；第 5 个起使用不冲突的 `172.31.*/24`。
- 配置的 `runtime.urls`、`runtime.token_envs`、`runtime.viewer_ports` 都恰好覆盖这 N 个
  player；`runtime.runner_mode` 默认写成 `container`。
- 生成器只生成可检查文件，不调用 Docker、不启动 Supervisor；默认拒绝覆盖已有文件，
  需要重建时显式加 `--force`。生成的 `.env` 不把 token 打印到终端，模型 key 可选。
- `run-paper.sh` 会从配置的 `server.managed_container` 推导项目，并自动加载同项目
  `infra/.env.<project>`（也可用 `PAPER_ENV_FILE` 显式指定），避免复用 Supervisor 容器
  里上一套项目的旧 `BOT_*` token；默认的静态部署仍保持旧行为。
- Observer 不是游戏内玩家，没有 identity/launcher/bot 三元组；`observer.mode: llm` 时
  Supervisor 仍会动态创建它的独立 runner 容器。因此一次 run 的 runner 容器数为
  `N + (observer.mode == llm ? 1 : 0)`。Workspace provisioner 支持最多 16 玩家 + Observer；
  生成的 server 服务设置 `MAX_PLAYERS=N`，fresh data 启动时同步更新 `server.properties`
  （需要包含该 entrypoint 的 server 镜像；已有的旧数据目录不会自动改）。
- 同一 host 上一次只运行一套 Minecraft Compose（共享托管世界和 Docker 网络子网）；
  启动新项目前先停止旧栈，保留世界数据目录即可继续核对旧 run。

## 同机多 runner 进程/容器架构

`runtime.runner_mode` 支持三档：

```text
inprocess  旧行为，兼容历史 manifest
process    每个 agent 一个独立 Python 进程
container  每个 agent 一个独立 Docker 容器（推荐）
```

Supervisor 只保留控制面：世界/服务器管理、单写者 SQLite ledger、预算、共享记忆、
执行 gateway、workspace shell broker、评估与 dashboard/viewer。每个 runner 只获得
本 agent 的模型凭据/state 和一次性 RPC token，通过本地认证 Unix socket 访问控制面；
不持有 bot token、Docker socket、ledger/memory 文件或其他 agent 的状态。

runner 数量完全由配置动态派生，不写死 4 个或 5 个：Supervisor 为
`agents.ids` 中每个玩家各创建一个 process/container；`observer.mode: llm` 时再为
独立 Observer 创建一个。容器名、state 目录、RPC token 和 heartbeat 都按 agent ID
生成，因此 1、3、6、16 个玩家运行的是同一套代码路径，而不是复制五份 hand-written
配置。当前暂停 run 的旧 manifest 仍保持原有 inprocess 解释。

container 模式的隔离：

- 只挂载 `src` 与 venv `site-packages`（ro）、本 agent `agents/<id>`（rw，`/state`）、spec（ro）和单个 RPC socket 文件；
- 无 Docker socket、无 bot token、无 world/ledger/memory/其他 agent 挂载；
- read-only rootfs、cap-drop ALL、no-new-privileges、pids/memory/cpu 限制；
- 停止先 SIGTERM 走优雅 checkpoint，再删除容器；崩溃/心跳超时自动重建；
- RPC socket 放在 host-shared 短路径（默认 run_dir 父目录，可用
  `runtime.runner_container_shared_dir` 覆盖），Supervisor 与 Docker daemon 看到同一 socket；
  只挂 socket 文件，不暴露整个 run 目录。

- 每个 run 为每个 agent 生成随机 RPC token，socket 目录 0700、socket 0600；
- runner 崩溃只重启该 runner（有界次数，从 checkpoint 恢复，未决动作不重放）；
- 心跳超时自动重建；Supervisor 启动时 fence 上次残留的 runner 容器/进程；
- pause 通过 `budget.wait_admission` 长轮询冻结 runner，resume/stop 正常放行和收尾；
- shell 仍由控制面在隔离编码容器中执行，runner 永远不拿 Docker socket；
- `configs/minecraft-paper-runner-20261022.yaml` 使用 **container** 模式，也是
  `run-paper.sh` 下一次新鲜 run 的默认配置；
  `configs/minecraft-paper-whole-session.yaml` 保留 process 模式作为回退；
  旧的 `minecraft-paper-final-20261022.yaml` 保持 inprocess 默认值。当前暂停的
  `final11` 的冻结 manifest 仍是 inprocess，不能原地改成 container；下一次正式 run
  必须使用新的 run 目录和 runner 配置。

container 模式已把挂载/命名空间/capability/cgroup 边界做出来，但仍与 Supervisor 同 host，
不是论文的 5 台云机器；process 模式则仍是同 uid 进程边界。详见
[ARCHITECTURE_MULTIPROCESS.md](ARCHITECTURE_MULTIPROCESS.md) 和
[RUNNER_ARCH_CONTRACT.md](RUNNER_ARCH_CONTRACT.md)。

### Pi RPC 玩家 + Observer runtime（`runtime.runner_runtime`）

玩家和 Observer 都可以使用真实 Pi coding-agent runtime（`pi_runner.py` /
`pi_observer_runner.py` + `pi_model_proxy.py`），Supervisor/ledger/budget/gateway 保持
Python。`runtime.runner_runtime` 默认 `python`；设为 `pi` 时玩家启动
`python -m minecraft_harness.pi_runner`，Observer 启动
`python -m minecraft_harness.pi_observer_runner`（只读数据工具、无 bash/mc/team memory/broker）：

```yaml
runtime:
  runner_mode: container   # 论文 run 推荐；Pi bash 直接跑在 runner 内，process 模式不适合
  runner_runtime: pi
  # pi_cli / pi_context_window 有默认值，见 PI_METHOD_ALIGNMENT.md §9
```

- Pi CLI 1.4.0（Node ≥ 22，`pi_runtime/.../cli.js`）以 `--mode rpc` 长驻；built-in 工具
  `read/write/edit/bash`，cwd=自己的 workspace，session 在 `agents/<id>/pi-sessions`，
  runner 重启后用 `--continue` 续跑。首次部署/恢复依赖：
  `npm ci --prefix pi_runtime --ignore-scripts --no-audit --no-fund`。
- 每个 runner 一个 127.0.0.1 随机端口 OpenAI-compatible proxy（`pi_model_proxy.py`）：
  每次 model attempt 都经 `budget.wait_admission/validate/reserve/settle` 计量，并写
  `model_request/model_response` ledger + bounded artifact；上游模型 key 只存在 runner
  进程，不写 `models.json`、不传给 Pi 子进程。
- prompt 用 paper `paper-framework-v4` 玩家 prompt + Pi 环境附录；游戏只能通过 bash 里的
  `mc exec --file`。Pi extension `minecraft-tools.js` 额外提供 `memory_read/write/list/search`
  和 `web_search/web_fetch`（经 loopback tool bridge → ServiceHub；memory 仍走中央 CAS 存储，
  web 走配置的搜索/抓取与 team-wide web cap）。源码由 frozen
  `run_dir/source-snapshot/src` 提供；Pi CLI/extension/package lock 的哈希写进 manifest，
  重启时发现变更会 fail closed。
- 安全/长程：`pi_runner`/`pi_observer_runner` 启动时设置 `PR_SET_DUMPABLE=0`，Pi 子进程
  无法读取父 runner 的 `/proc/<pid>/environ`（model key/RPC token 不再暴露）；bridge token
  仍在 Pi 子进程 env（只授权固定工具）。Pi session 在 runner 启动时按
  `storage.pi_session_max_bytes` 剪枝/轮转，artifact 超过
  `storage.pi_artifact_max_bytes` 后只存 metadata-only ref；active session 超限会 clean
  abort 并记录事件。Pi runner 还增加了 upstream 连续失败 fail-fast、no-progress 上限和
  pause admission 超时。`max_turns` 约束 continuation 轮数；“不确定动作不重放”仍是
  prompt+CLI 约定；论文 run 必须用 container 模式。详见
  [PI_METHOD_ALIGNMENT.md](PI_METHOD_ALIGNMENT.md) 第 9–10 节。
- 已知残余（accepted / 后续）：Pi 子进程 env 仍含 bridge token 和 player
  `MC_AGENT_TOKEN`（游戏/CLI 必需）；model 可用自身 bash 读取该 env 并直接调用 loopback
  bridge，绕过 Pi 的 `tool_intent/tool_result` 投影（ServiceHub 仍记录 memory/reference
  事件），**不得据此宣称完整工具事件审计**。active session 超限时 clean abort 但不会
  自动轮转/重启，也没有操作者告警阈值（run 继续 degraded，54h 需人工监控）。run-wide
  per-runner restart-safe cap 已实现；artifact 会计由 ServiceHub `pi-budget.sqlite3` 集中控制（P1-5），model-writable marker 无法重置 usage。
- 验收：`PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages .test-venv/bin/python
  -m pytest -q tests/test_pi_model_proxy.py tests/test_pi_runner_manager.py
  tests/test_pi_runner.py tests/test_pi_e2e.py tests/test_pi_tools.py tests/test_servicehub_web.py`。
  `run-paper.sh` 默认只做 prepare（`--start` 才启动并负责 compose up→wait world→health→
  Supervisor）；prepare 会要求两个 Pi extension 并做无启动容器镜像 probe，当前 paused
  `final11` 不受影响。

## 游戏程序 CLI

玩家只能获得自己的 token。操作者先建立对应 session；玩家的 CLI 不具备 session 生命周期权限。

paper 模式下，玩家看到的是论文 §2.1 语义的程序输出：按顺序给出程序日志，最后给出返回值和失败状态，
**不是**执行账本的结构化 JSON。模型可见文本的上限是模型上下文预算，而不是单独的
`max_tool_result_chars` 展示截断；完整文本仍以 artifact 保存。超过上下文时会在输出中明确标记，
bot/Node 侧仍保留必要的日志/结果字节硬上限用于长程内存稳定。CLI 的 `mc exec --file` 默认输出同样是
日志加返回值，`--json` 才显示原始执行记录。

```bash
# 在该玩家隔离工作区设置 MC_RUNTIME_URL、MC_AGENT_ID、MC_AGENT_TOKEN。
uv run mc exec --file action.js --timeout-ms 60000
uv run mc status --execution-id <id>
uv run mc cancel --execution-id <id>
```

`--wait-ms` 控制客户端等待，`--timeout-ms` 控制服务端执行期限，两者独立。超出客户端等待时返回执行 ID，之后查询；网络断开返回 unknown，不能自动重新执行。通过 CLI 提交的源码也由 Node 事件流记录，Python Supervisor 收集后保存为内容寻址产物。

玩家工作区的 CLI 与 `mc_execute/mc_submit/mc_wait` 共用可信 SQLite 提交账本和持久请求 ID。paper CLI 拒绝直接连接原始 Node 端点。`mc submit` 不等待；`mc jobs` 查看后台任务。程序执行成功只代表程序正常结束，能力评分仍使用服务端证据。

## 暂停、恢复和预算

在另一终端执行：

```bash
uv run mc-harness pause runs/<run-id>
uv run mc-harness resume runs/<run-id>
uv run mc-harness pause runs/<run-id> --freeze-world
uv run mc-harness stop runs/<run-id>
uv run mc-harness checkpoint runs/<run-id>
uv run mc-harness restore runs/<run-id> --checkpoint-id <id>
```

普通 pause 暂停新推理和提交，世界继续运行。paper 模式还会停止生成代码及后台任务，立即提升可见运行代际，恢复后重新观察；strict 保留已有前台清理语义。`--freeze-world` 另行保存并停止 managed server，resume 后重连并广播恢复事件。

Supervisor 退出后，`resume` 从原 manifest、conversation、记忆、提交账本和预算恢复。旧版本 manifest 缺少的新配置项按当前默认值归一化并记录 `config_defaults_migrated`；缺少绝对截止时间时按原 manifest 创建时间加原墙钟预算保守推导，升级不会重新授予运行时间。旧 checkpoint 缺少 run ID 或评分 lineage 时会在世界修改前拒绝恢复。未决动作只查询状态，无法核实则标 unknown，不自动重放。玩家事件使用持久化全局 cursor，Node session 重建不会导致恢复通知被旧 cursor 跳过。工具在暂停时跨越世界 epoch，会被标为 invalidated，要求重新观察和决策。

paper 服务端每 300 秒执行可信 `save-all flush`；旧 strict 长程模板使用 60 秒。日志另按默认 1 秒周期增量采集。Minecraft 自身保存与 Agent 检查点分别持久化；任意 JS 函数不会跨进程序列化。普通恢复使用 `rollback=false` 延续既有评分基线。真正回滚携带所选 checkpoint 的源 epoch 与证据截止位置，舍弃其后被回滚分支的进度。

`checkpoint` 在活动实验内协调暂停、旧 bot 围栏、停服与完整数据快照，然后恢复运行。`status` 返回最近的 checkpoint ID；快照逐文件保存 SHA256 清单。`restore` 验证清单后先备份当前数据，再恢复所选快照并提升世界 epoch；拒绝篡改、symlink、路径越界和嵌套目录，恢复复制异常时尝试回滚。快照先写入隐藏临时目录，内容和目录 fsync 后才原子发布；复制前检查目标盘空余空间。恢复前持久化 journal；世界复制成功后先持久化完成 receipt，再删除 journal。中途进程退出后，下一次 resume 在启动世界前校验备份并回滚未完成的恢复；已复制完成的世界通过 receipt 补记评分分支后才确认，避免世界与账本之间的崩溃窗口。备份损坏则拒绝启动。两者要求专用 managed server，可能触发 bot globals 重建；不要直接复制正在写入的世界卷。

预算限制团队模型尝试总数和从启动开始的总墙钟时间；暂停、故障与 Supervisor 停机期间也计入原有期限，重启不会补回时间。所有并发 Agent 共用原子 admission。未知供应商 token/费用记为 unknown，并按 admission 时最坏预留计入保守成本，不再因此冻结后续付费调用；长程运行可以经受一次网络失败或停机取消。配置费用上限还必须提供计价表和输入 token 预留上界；这是费用估算/预留机制，供应商实际 tokenizer、缓存及错误请求计费仍需核对，不能视为账户的硬性账单限额。

## 评估与报告

目前支持 `observe_only`、`obtain_logs`、`craft_table`、`obtain_iron_ingot` 和 `kill_dragon`。前者仅诊断链路；能力任务必须有同一世界历史分支内的可信基线和后续服务端快照。

- 原木看服务器拾取统计；工作台看实际合成统计，recipe 解锁不算。
- 铁锭看服务端统计/新成就；末影龙看新获得的服务器 `kill_dragon` 成就。
- 收集器按实验玩家的 offline UUID 限定文件，并登记原始文件哈希；评分时重新校验，不相信调用者随意标注的 `source=server`。
- 程序日志、bot 内存和聊天均不能独立判成功；篡改的证据会被拒绝。
- 普通重启/冻结恢复保留初始基线与累计进度；checkpoint 回滚按保存位置截断旧分支，保留 checkpoint 已有进度并撤销其后的成功。

活动评分只处理新证据并保留有限快照；世界文件没有变化时不重复登记，服务器日志按持久化字节游标增量归档，识别轮转并在崩溃后去重。

规则报告模块流式读取账本，最终重新校验证据后输出 `reports/report.json` 和 `report.md`。另有 `observer.mode: llm` 启用独立 Agent：默认每 60 秒处理增量记录，重要事件提前唤醒，每条结论带引用；只能请求 Supervisor 验证基础设施修复。评审间隔是硬下限，Observer 自身记账事件不计入证据，一次评审有界合并多个证据页；每轮评审必须写入一条 `observer_report`（进度摘要、已核实事件与发现），有新证据却未报告时会用强制 `tool_choice` 补报一次。它没有 bot 或玩家记忆写权限，不能提高预算。paper 模式要求 Observer 持续工作；其故障会终止实验。日志回放只重新分析，不重放动作。

每次 run 保存 frozen manifest、源码与依赖锁哈希、SQLite 事件账本、JSONL 导出、原始代码和输出产物、各 Agent 检查点、记忆版本、服务端证据及报告。供应商未提供的用量或费用为 `null`。

## 长程实验配置

正式 trial 可以先准备再检查，准备动作不启动 Java、不接受 EULA：

```bash
uv run mc-harness prepare-trials --config <已填写预算的配置> --seeds random,random --repetitions 1 --run-dir runs/prepared
# EULA 已明确接受且相关环境变量已配置后，batch 才能启动专用 Compose。
uv run mc-harness batch --config <已填写预算的配置> --seeds random --repetitions 1
```

每次 trial 有独立世界、记忆和工作区。`batch.json` 声明每个 trial 的预算及整个批次的上界；已执行 trial 不会被自动重放。随机种子保留在操作者侧，新 run 不能默默复用旧玩家进度。

复制 [minecraft-longrun.yaml](configs/minecraft-longrun.yaml)，填写真实模型/凭据并核对示例计价表后运行。它提供 24 小时墙钟上限、有限恢复次数、心跳租约、60 秒证据采集、事件/产物/工作区配额与磁盘余量。预算、配额或连续故障到达上限时结束实验并留存原因，避免无限重试。SIGTERM/SIGINT 会进入 Supervisor 清理流程；`shutdown_timeout_s` 限定等待 Agent 取消的时间，世界文件复制必须安全结束，服务端保存另有 90 秒操作期限，所以完整退出可能超过 30 秒。服务端与 bot 健康检查可用于操作者观察，Compose 不擅自重启被围栏的会话。

框架在同一宿主、同一操作者下锁定 bot URL、服务器数据目录和 managed container，防止多个实验控制同一世界。多宿主部署应由外层调度器提供独占租约。外部 bot 网关失联且旧 worker 停止状态无法核实时，框架会安全终止并留下诊断，需要操作者恢复容器并核对后续恢复条件；自动恢复不覆盖这类无法证明旧动作已停止的故障。

详细故障注入、规模验证及真实环境验收边界见 [LONG_RUN_VALIDATION.md](LONG_RUN_VALIDATION.md)。

## 实验矩阵

离线机制检查可批量运行，每个 trial 都建立独立记忆和工作区：

```bash
uv run mc-harness batch --config configs/offline-smoke.yaml --seeds 42,43,44 --repetitions 2
```

假环境 seed 不代表真实世界生成。真实批次使用 paper 配置，通过 `prepare-trials` 为每个 trial 准备独立目录，再由 `batch` 顺序启动专用 Compose；已执行 trial 不会静默重跑。对比人数、聊天或共享记忆时，固定任务/世界初态，并说明总团队预算还是单 Agent 预算固定。

## 验证

**真实环境（2026-10-21）**：EULA 接受后，本机 Mojang Java 1.20.4（Survival/Easy）真实服务端 + 真实
Mineflayer bot + DeepSeek `deepseek-flash` 完成 1 Agent + LLM Observer 验证：`obtain_logs` 121.8 s
成功（服务器 stats 原木 delta=3）、`craft_table` 243.7 s 成功（工作台 delta=6），报告 token 与
费用为已知值；证据与启动要点见 `validation/2026-10-21-real-minecraft/`。4 Agent paper_aligned 与
日尺度运行仍未验证。本机容器间游戏网络受宿主代理限制，验证时使用 host 网络、未改宿主全局策略。

**2026-10-23 当前回归（宿主，无 Docker）**：全量 Python **753 passed / 3 skipped / 734
subtests**；Pi/Observer 定向 **196 passed / 119 subtests**；Node **51 passed**。3 个 skipped
为 Docker-gated 完整 compose 测试；本窗口已用真实 DeepSeek 跑 host + container Pi
credential smoke（synthetic Minecraft runtime；见 REAL_SMOKE_2026-10-23.md），但未跑完整
4-agent compose 长测；付费长 run 前仍需专用 Docker 窗口补做完整 smoke 并记录 image IDs。
`run-paper.sh --prepare` 的无启动镜像 probe 只覆盖 image/Node/Pi CLI/两个 extension/Python
imports，不替代真实 container 全栈短 smoke。

```bash
uv run python -m unittest discover -s tests -v
npm --prefix bot_runtime test
python3 -m unittest discover -s infra -p 'test_*.py' -v
docker compose --env-file infra/.env.example -f infra/compose.yaml config --quiet
```

当前回归结果、容器负向探针、一小时离线故障注入和十万事件规模验证统一记录在 [LONG_RUN_VALIDATION.md](LONG_RUN_VALIDATION.md)，功能与验收状态见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。旧版 175/34 项和 7 条资源告警是历史结果；监听器泄漏已定位修复，最新完整回归不再产生这些告警。

Python 测试包含真正 Node 子进程的端到端链路、幂等、超时、消息 cursor、记忆隔离、预算、压缩/恢复和伪成功拒绝。Node 测试包含执行生命周期及使用真正 Mineflayer 的协议夹具。FIFO 管理协议测试验证旧日志/聊天不能伪造保存确认。

真实模型短任务验收需另行填写模型配置、接受 EULA 并运行 Vanilla 服务端，再检查服务端证据。完整通关不属于本版本验收门槛。
