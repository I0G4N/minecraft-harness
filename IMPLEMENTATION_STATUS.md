# 实现与验证状态

日期：2026-10-21；分支：`harness_embodiment_v1`。论文实验框架已实现，完整单元/集成与隔离短测通过。**原冻结一小时脚本验收为 failed：14/16 运行检查通过，最终导出/报告在仍开启 tracemalloc 时超过夹具的 45 秒等待。**已定位并只修测试仪器，同一冻结框架的完整账本重放和独立收尾补证通过；不将原一小时改写为通过。真实 Vanilla 单 Agent 已通过（见下文），4 Agent paper_aligned、付费模型能力实验和日尺度运行仍未建立通过证据。

## 2026-10-22 paper 可见面 + Pi context/memory + 长程稳定性

- 玩家和 Observer 都使用真实 Pi coding-agent CLI（`runtime.runner_runtime: pi`，RPC 模式）：Pi 负责 loop/session/context/compaction；玩家工具为 built-in read/write/edit/bash + memory/web extension，游戏只经 `mc exec --file`；Observer 使用只读 `records_read/evidence_read/observer_report/infra_issue`，无 bash/mc/team-memory/broker。Python 只保留 Supervisor、ledger、budget、Minecraft gateway 和 python-mode 回退。下一轮 `minecraft-paper-runner-20261022.yaml` 已切 `runner_runtime: pi` + container。`pi_runtime` 依赖 pin 到 1.4.0，首次部署需 `npm ci --prefix pi_runtime`。
- 内部仍保留 Python runtime 的 Pi 风格 context projection / compaction / branch summary / effect sandwich：工具先写 `tool_intent`（仅参数 SHA256），崩溃后 `tool_effect_unknown` 且不重放；团队记忆 FTS5 ranked `memory_search`。这些只在 `runner_runtime=python` 或 Observer 路径使用。
- 长程稳定性：单玩家 model/infra 失败按 `agent_failure_policy=isolate` 记录 `agent_degraded` 并继续；Observer 失败默认 `observer_degraded` 降级；pause 不再消耗 wall budget；web 调用受 `budgets.max_web_calls` 限制；runner RPC 有默认超时；Compose 的 identity/launcher/bot 使用 `unless-stopped`；runner 重启加载 `run_dir/source-snapshot` 冻结源码。runner-mode Observer 通过 ledger 重要事件在 ≤1 s 内唤醒 review。
- 验证：Python **762 passed, 3 skipped, 734 subtests**；Pi/Observer 定向 **196 passed / 119 subtests**（含真实 Pi CLI host e2e、memory/web extension、fixture observer bridge/RPC、session/artifact retention）；Node **51 passed**。**本窗口已跑真实 DeepSeek 的 host + container Pi credential smoke**（synthetic Minecraft runtime；见 REAL_SMOKE_2026-10-23.md）：host 15/15 model/tool、container 13/13 model + 15/15 tool，均无 token/key 泄漏；3 个 Docker-gated 完整 compose 测试仍 skipped；已跑 6-agent 真实 Minecraft 全栈 smoke（6/6 runtime_ready，40/40 model，52/51 tools，0 infrastructure_error），详见 SIX_AGENT_SMOKE_2026-10-23.md。`run-paper.sh --prepare` 现在会在 compose up 之前检查**两个** Pi extension（`minecraft-tools.js` + `observer-tools.js`），并跑一次无启动容器镜像 probe（Node ≥ 22 / Pi CLI / 两扩展 / Python imports，`docker run --rm --network none`）；Docker 不可用时只报告并拒绝启动。`final11` 已停止并归档（旧 Supervisor 退出、compose down，run 目录保留）；6-agent smoke 已完成。`run-paper.sh` 默认只 prepare，`--start` 才启动。
- 详细设计、偏差与验收命令见 [PI_METHOD_ALIGNMENT.md](PI_METHOD_ALIGNMENT.md) 第 9 节。

## 2026-10-21 真实模型复核与修复（独立运行）

- **独立运行**：`--network none` 下 `configs/offline-smoke.yaml` 通过（`diagnostic_pass`）；框架自带 `pyproject.toml`、`uv.lock`、`node_modules`，不导入 `ai_native_harness`、`ai_native_world`、`sim2real` 或 `skill_engine`，不需要 zkdx 产品运行时、模型密钥或网络。
- **回归**：Python **347 项通过**（含 leftover 语义、单程序工具集、Observer 完整报告、in-game days、session-loss 恢复回归），Node **51 项通过**。
- **真实 DeepSeek（deepseek-flash）1 Agent + 1 LLM Observer、fake 游戏**：8 分钟 106 次模型调用（player 81 / observer 25）、**11 条 `observer_report`**、3 次补报失败、成本约 $0.24；期间一次停机取消产生的 unknown usage 按最坏预留计费后继续运行到墙钟上限。强制补报 + 补报重试后的 4 分钟复核：66 次调用（player 45 / observer 21）、**10 条 `observer_report`**、2 次补报失败、成本约 $0.13，报告正常生成。
- **修复前**：单一玩家 380 秒内触发 **190 次 Observer 调用（占 72% 成本）、0 条报告**（Observer 追自身记账事件，评审间隔失效）。修复后 Observer 评审间隔为硬下限，8 分钟 25 次调用、11 条报告。
- **收尾竞态修复**：paper_aligned 下 Observer 在停止/成功收尾时自然退出，此前会被判为 unexpected 并让整次运行以 `infra_failed` 结束；修复后 `paper_lifecycle_soak` 连续 25 次通过，全量套件连续 3 次通过。
- **仍未完成**：4 Agent paper_aligned、付费模型能力实验、日尺度运行；原冻结一小时 failed 结论保持不变。

## 2026-10-21 真实 Minecraft 环境验证（1 Agent）

操作者接受 Minecraft EULA（2026-10-21）后，本机以 Mojang Java 1.20.4、Survival/Easy 启动真实
服务端，真实 Mineflayer bot（player1）+ 真实 DeepSeek `deepseek-flash` + 独立 LLM Observer
完成端到端验证：

- `obtain_logs`（seed 42 新世界）：121.8 s 成功；服务器 stats `minecraft:picked_up` 原木 delta=3
  （要求 ≥1）；17 个程序（16 成功、1 超时），0 错误；3 条 Observer 报告；成本约 $0.12。
- `craft_table`（seed 43 新世界）：243.7 s 成功；服务器 stats `minecraft:crafted` 工作台 delta=6
  （要求 ≥1）；51 个程序（46 成功、5 失败），0 death；81 次模型调用；**报告 token/费用为已知值**
  （1,318,912 / 36,173 / $0.123）；3 条 Observer 报告；`unknown_usage_calls=0`。
- 真实环境相关修复：`infra/compose.yaml` 的 `tmpfs` flow-list YAML 写法在 compose v5 下被拆成非法
  挂载（改为 block 列表）；宿主代理/防火墙拦截容器间游戏网络，本机验证使用 host 网络（与
  `infra/README.md` 记录的 Mihomo 限制一致）；成功收尾增加有界优雅等待，避免取消在途模型请求
  导致 token/费用 unknown；Observer 补报重试附带允许的 `event_seq` 列表。
- 残余：Observer 真实模型报告仍有约半数引用校验失败（每次运行记录 3 条）；报告为 advisory，不影响
  确定性评分。每次实验需全新世界并删除 `server-data/.harness-world.json`（世界 run 绑定属预期保护）。
- 证据：`validation/2026-10-21-real-minecraft/`（真实服务器账号与运行目录不在其中）。

## 2026-10-21 实时第一视角（可选直播）

新增可选的人机可观察能力：每个 bot 可通过 `VIEWER_PORT` 启动 `prismarine-viewer` 第一人称渲染；观测台
（`mc-harness serve`）新增「实时第一视角」页，从冻结 manifest 的 `runtime.viewer_ports` 生成 iframe 卡片。

- 依赖与构建：`bot_runtime/package.json` 增加 `prismarine-viewer@1.33.0` 与 `@napi-rs/canvas`；Dockerfile 在
  `npm ci --ignore-scripts` 后运行 `scripts/canvas-shim.mjs` 把 `canvas` 映射到预编译实现。
- 安全默认：viewer 仅绑定 `127.0.0.1`（`VIEWER_BIND` 可覆盖），本机验证通过 SSH 隧道访问；不接收模型凭据，
  不改变评分与证据链。Compose 发布端口时需显式设置 `VIEWER_BIND=0.0.0.0` 并自行限制网络暴露。
- 验证：真实 Minecraft 1.20.4 运行中 viewer 返回 `Prismarine Viewer` 页面（HTTP 200），观测台
  `/api/status` 返回 `viewers: {player1: 3105}` 且「实时第一视角」页可用；同一 run 仍以服务器 stats 证据
  成功完成 `obtain_logs`。长时演示 run 见本地 `runs/_real-mc/run6`（未随仓库分发）。

## 当前实现

| 部分 | 实现与验证边界 |
| --- | --- |
| 两种运行口径 | `experiment.profile: strict / paper_aligned`；旧配置维持 strict，恢复不自动切换。Fake 游戏或模型明确标记 diagnostic，不能建立能力成功 |
| 实际环境校验 | 有界 NBT 解析与 seed、版本、生存/Easy、权限、初始进度、world origin/run 身份核对；`prepare-trials` 准备独立试验目录，不启动服务器或接受 EULA |
| 执行生命周期 | v3 提交/查询/等待/取消协议、唯一 request 与 epoch/sequence 围栏；paper 模式显式 jobs 可跨前台程序存活，受数量、run 截止时间、取消与重建约束 |
| 隔离边界 | 控制面、launcher 与代码执行容器分开；只读文件系统、原生写入/进程创建限制、固定身份游戏代理；聊天消融在协议包层覆盖直接 `_client` 访问 |
| 代码工作区 | 独立容器内 Git、rg、Python、Node、锁定文档和 `mc` CLI；network=none，通过专属 Unix broker 进入同一持久提交账本，Observer 不挂玩家 socket |
| Agent 与交接 | 共用 AgentCore，私有上下文/工作区；paper 模式有结构化、有预算的交接，原始历史和未决任务仍可分页读取，失败交接明确不完整 |
| 独立 Observer | 同模型配置、独立上下文和工作区，周期/故障唤醒、引用校验和修复请求去重；不能直接操作游戏、提高预算或判定通关 |
| 长程控制与世界恢复 | 持久控制队列、信号清理、绝对期限、有限恢复与独占锁；停服复制、预备份、空间预检、原子发布及 journal/receipt，账本事务衔接 rollback lineage |
| 服务端证据 | 独立周期采日志，保存世界默认间隔 300 秒；字节分块、旧 inode 尾部、轮转/截断缺口、午夜/时区及接收时间均保留。最终评分重新校验登记产物 |
| 统计与费用 | 首次真实程序启动为时钟原点，保留首次验证成功窗口和全会话窗口；玩家耗时/活动/jobs/错误/死亡、版本化聊天、代码/记忆和保守阶段证据；缓存、推理、角色和用途分账，不双计子集，未知为 null |
| 操作者入口 | 中文七视图实验观测台：多运行切换、事件筛选分页、程序源码/回执、聊天、Observer、证据窗口、资源费用和报告导出；手动确认后提交持久控制请求。外部 runtime 预检在模型 admission 前执行，不创建 session 或调用模型 |

源码位于本目录，不依赖原有 `ai_native_harness`、`ai_native_world`、`sim2real` 或 Skill Engine；不宣称获得作者未公开实现。

## 实验观测台增量

运行 `uv run mc-harness serve runs --port 8899` 后，浏览器打开 `http://127.0.0.1:8899/`。也可只开放某一个运行目录。界面以可读卡片、列表和时间线展示数据，原始 JSON 与源码保留在详情分页中；访问只读接口不会启动服务器、调用模型或重新评分。使用说明见 [DASHBOARD.md](DASHBOARD.md)。

服务端按类别、参与者、级别和关键词筛选后分页，读取范围与未知值显式展示。界面使用本地静态资源，不依赖 CDN。数据中的文本不解释为 HTML；控制仍要求本地 token、同源校验和明确确认，进入 Supervisor 的原有持久队列。统计区分前台与含后台活动，任务状态仅代表最近持久记录。

新运行由 Supervisor 持续写入 `resource_sample`，默认每 10 秒（受资源检查周期约束），使关闭浏览器后的历史仍可查询。采样包含 Supervisor Python RSS、存储和预算；不声称包含 worker 或 Java 服务端内存。旧运行只有最新状态时只显示单点，不能还原之前未采集的曲线。

- 看板增量后的完整 Python 套件 **304 项通过、ResourceWarning 为 0**；随后新增的 URL 编码请求 ID 回归也在平台专项 **10 项通过**中验证。原始日志与构建资源检查见 [implementation-checks.json](validation/dashboard/implementation-checks.json)。
- 数据接口在 **165,419 条事件**的原账本上，总览约 0.21 秒、程序页约 0.04 秒；读取前后原 DB SHA256 一致，见 [规模检查](validation/2026-10-21-dashboard-data-benchmark.json)。
- [HTTP 与浏览器整链验收](validation/dashboard/acceptance.json) **61 项通过**，其中[真实浏览器交互](validation/dashboard/acceptance-browser.json) **36 项通过**：桌面/手机导航、筛选分页、源码与证据详情、实验切换、735 采样跨 600 点历史窗口、返回最新、注入文本及控制二次确认。没有控制台/网络错误，原 DB、manifest 和报告 SHA256 不变。临时控制 fixture 只提交了一次已确认的暂停，原实验没有 POST。与下方冻结一小时资格分开，浏览看板及临时 UI fixture 不构成新的 Vanilla、模型或长程实验结果。

## 当前原始证据

- 仪器修正后完整 Python 套件 **276 项通过，26.229 秒**，见 [最终原始日志](validation/paper-python-after-instrumentation-fix.log)；以 `-W always::ResourceWarning` 运行，告警/unclosed 均为 0。冻结前 [276 项、27.842 秒记录](validation/paper-python-tests.log)同样保留。
- Node 完整套件 **47 项通过，2.751 秒**，见 [原始日志](validation/paper-node-runtime-tests.log)；infra 原生边界/FIFO **4 项通过，1.116 秒**。汇总见 [最终验收摘要](validation/2026-10-21-paper-final-summary.json)。
- [独立 Docker worker](validation/2026-10-21-remote-worker.json) **10 项检查通过**：后台任务、取消、原生写入/exec 拒绝、CPU 死循环硬重建、断连 fencing、launcher SIGKILL 独立清理及无遗留容器。
- [隔离协议联调](validation/2026-10-21-isolated-protocol.json) **7 项检查通过**：真实 Mineflayer 解码 synthetic chunk/inventory、固定身份、双向聊天包消融、期限重连与清理。它使用协议夹具，**不是 Vanilla**。
- [paper 生命周期与 Observer 联调](validation/2026-10-21-paper-lifecycle-soak.json) 20 秒、13 项检查通过：507 次 admission/settlement、223 次完整交接，四玩家与 Observer、后台 jobs 和费用角色/用途分账；不含外部模型。
- [Agent smoke](validation/2026-10-21-paper-agent-smoke.json) 12 秒离线 Supervisor 检查通过：382 次模型尝试结算、3 次 Node 故障恢复和停止清理。
- E 统计/费用专项 **20 项通过**；含轮转尾部被截短、9000 行无归一化遗漏、多线程游标、午夜、最终积压排空/期限/尾片段、空 cleanup、成功前/全会话窗口、代码/记忆和未知计价。
- [新版十万事件规模记录](validation/paper-evidence-scale.json)：144,842,752 字节 SQLite 账本，报告 **11.943319 秒**、Python 分配峰值 **41,931 字节**、RSS **28,667,904 → 35,655,680 字节**；恢复评分 **0.368676 秒**。66 个应用/脚本/锁文件的起止哈希一致，source tree 与最终冻结 hour 相同；准确统计 100,000 个执行终态。
- [实际编码工作区](validation/2026-10-21-workspace-smoke.json) **9 项检查通过**：network=none 的容器经 Unix CLI 到持久网关、request 不重派、账本归档、开发工具和锁定文档、外部身份拒绝、Observer 隔离与拥有容器清理。
- [工作区限制与恢复](validation/2026-10-21-workspace-limits-smoke.json) **11 项检查通过**。每文件 16 MiB 内核硬上限；聚合工作区配额在 shell 执行期间按秒检测，超限停止进程，不能称为内核聚合配额。
- [实际部署溯源](validation/2026-10-21-deployment-projection.json) **6 项检查通过**：实际 image ID、有限 own Compose inventory、非秘密配置白名单与 operator-only 记录。独立 [网络边界](validation/2026-10-21-network-boundary.json) 验证 isolated 无宿主桥地址及负向出口探测；普通 internal 对照受既有代理策略干扰，结论明确为 inconclusive。

本地 **11:52** 启动的[历史一小时 soak](validation/2026-10-21-paper-hour-soak.json) 已完成：15 项检查通过、13,710 次调用结算、state 为 stopped，未发生 finalization 错误。该进程存活期间源码发生过修改，故只作为历史机制结果，**不能替代最终冻结版本验收**。[只读限定说明](validation/2026-10-21-paper-hour-soak-qualification.json) 保留原始 SHA256 与状态；未改写原 JSON、账本或报告。

为排除混版本影响，[frozen_soak.py](scripts/frozen_soak.py) 复制源码/锁文件和私有只读 npm 依赖，移除 editable live import 路径并核对实际 Python/Node 来源，起止校验哈希。[原冻结一小时 raw](validation/2026-10-21-paper-hour-final.raw.json) 与 [wrapper 结果](validation/2026-10-21-paper-hour-final.json)均保留 failed：12,992 次调用全部结算、三次故障恢复、暂停、全 ACK 与无遗留进程检查通过；收尾超时导致 result 缺失。框架和 Node 源码未改，后续仅修 `scripts/longrun_soak.py` 的测量边界。

[限定说明](validation/2026-10-21-paper-hour-final-qualification.json) 分开记录以下补证，不把它们冒充原一小时通过：

- 同一冻结代码与 **165,419 事件**完整账本，export+report 在 tracing off/on 下分别 **5.052 / 40.882 秒**；三轮输出与原件逐字节相同，原 DB/raw/报告 SHA256 不变，见 [性能对照](validation/finalization-benchmark/results.json)。
- [原冻结框架外部仪器 hook](validation/2026-10-21-paper-finalization-hook.json) 20 秒：16/16 机制检查与 8/8 补证检查通过，312 次调用，验证 ending snapshot 之后关 tracing、原 stop 正常返回、三次故障恢复、无子进程与 pending。
- [未来仪器脚本实际运行](validation/2026-10-21-corrected-soak-fixture.json) 12 秒：16/16 检查、167 次调用，`stopped_before_finalization=true`。保留原 45 秒等待，没有扩大超时，也未另做第三次一小时运行。

## Docker 网络与历史记录

此前 [Docker smoke blocked](validation/2026-10-21-docker-fake-smoke.json) 是历史结果。本轮 [网络探针](validation/2026-10-21-proxy-network-probe.json) 已实际验证：本机代理未将新建容器地址视作本地网络；只对两个自建 peer 地址临时豁免后 TCP 可达，撤销后重新失败。隔离协议试验同样只使用本次自建地址的临时豁免，记录确认全部撤销。正式部署仍需管理员配置实验网段的代理策略；没有持久修改宿主全局策略。

第一轮的 Python 175 / Node 34 / FIFO 3、7 条资源告警、300 秒 soak 和旧镜像 ID 均属于历史证据，详见 [验证记录](LONG_RUN_VALIDATION.md)。旧告警已在后续修复与当前完整日志中复验，当前一次无告警不构成“所有资源路径绝无泄漏”的证明。旧镜像 ID 不能代表新增隔离控制面/worker。

## 运行入口与验收边界

- [minecraft-paper-aligned.yaml](configs/minecraft-paper-aligned.yaml) 是 36 小时上限模板，要求新世界、实际模型以及冻结价格和总预算；**不是 36 小时运行结果**。[minecraft-longrun.yaml](configs/minecraft-longrun.yaml) 保留 strict 长程模板。
- 按 [PAPER_ALIGNMENT.md](PAPER_ALIGNMENT.md) 核对论文方法、工程选择及偏差；完整故障与复验命令见 [LONG_RUN_VALIDATION.md](LONG_RUN_VALIDATION.md)。
- 普通恢复保留最早基线；真正 rollback 按 checkpoint 的 run 身份、source epoch 和证据 cutoff 截断分支。旧配置补默认值会留迁移事件，绝对期限不补回停机时间。
- 无法确认旧 worker 停止时安全终止；`shutdown_timeout_s` 只限定等待 Agent 取消，服务端保存和世界复制安全结束另计。
- Minecraft EULA 尚未明确接受；准备目录、下载或构建镜像不等于接受。真实 Vanilla 短任务、付费模型实际用量、完整通关和小时/日尺度真实运行均需独立验收。

## 2026-10-23 目标收口

- `/proc` 凭据：runner 进程 `PR_SET_DUMPABLE=0`，container runner 去掉 `--init` 使 hardened runner 为 PID 1；同 uid 读取 `/proc/1/environ` 已实测拒绝。
- Pi session/artifact retention：session 启动剪枝/活跃超限轮转，artifact 共享预算超限转 metadata-only；paper run 配置 1 GB/8 GB。
- Observer：`runner_runtime=pi` 时改用 `pi_observer_runner.py` + observer 只读 tool bridge/ServiceHub RPC，Python ObserverAgent 仅作回退。
- 安全审计 F1–F6 已修（PID1 env、artifact grant 所有权、viewed-event 证据、report key 注入、bridge 错误泄漏、infra issue id/kinds）。
- 验证：762 passed / 3 skipped / 734 subtests；Pi/Observer 定向 196 passed / 119 subtests。本窗口真实 DeepSeek host+container Pi credential smoke 均通过（synthetic runtime；见 REAL_SMOKE_2026-10-23.md）；3 个 Docker-gated 完整 compose 测试仍 skipped；已跑 6-agent 真实 Minecraft 全栈 smoke（6/6 runtime_ready，40/40 model，52/51 tools，0 infrastructure_error），详见 SIX_AGENT_SMOKE_2026-10-23.md。`run-paper.sh --prepare` 新增无启动镜像 probe：要求两个 Pi extension，检查 Node ≥ 22 / Pi CLI / Python imports，Docker 不可用或 probe 失败即拒绝 `--start`。

### 已知残余与未验证（2026-10-23，accepted / 后续 task）

- **Pi 子进程凭据残余（P2-3/P2-6，accepted）**：`/proc/<runner>/environ` 已不再暴露 model key / RPC token（`PR_SET_DUMPABLE=0`，container runner 无 `--init`）；但 Pi 子进程 env 仍含 bridge token（只授权 memory/web/observer 工具）和 player `MC_AGENT_TOKEN`（游戏/CLI 必需）。model 可用自身 bash 读取该 env 并直接调用 loopback bridge，从而绕过 Pi 的 `tool_intent/tool_result` 投影（ServiceHub 仍记录 memory/reference 事件）。该偏差已记录并接受，**不得据此宣称完整工具事件审计**。
- **active session 自动轮转（2026-10-23 已实现）**：Pi session cap 默认 2 GB；80/90/95% 发 `pi_session_alert`，每 60s 发 `pi_session_sample`；95% 触发有界 in-run rotation（默认最多 3 次）：停止当前 turn、发送 Pi `new_session`、归档旧 JSONL、用 compaction handoff prompt 继续，事件 `pi_session_rotation_started`/`pi_session_rotated` 显式记录。Observer 仍为 hard-stop 路径。
- **run-wide retention（P1-5 已修）**：artifact 会计由 ServiceHub `<run_dir>/pi-budget.sqlite3` 集中控制（per-agent + global cap、原子 take、RPC fail-closed）；model-writable `/state/pi-artifact-budget.json` 伪造/删除不能重置 usage。Session retention 仍为 filesystem-based。
- **容器 Pi smoke 本窗口未验证（P2-5）**：只有历史一次性 smoke；本窗口 3 个 Docker-gated 测试 skipped。付费长 run 前必须用专用 Docker 窗口跑真实 container Pi/Compose 短 smoke 并记录实际 image ID。

## 2026-10-23 P1 审计收口

- P1-1：Pi runtime 全树哈希（bundle/node_modules/两个 extension）fail closed。
- P1-2：mid-stream SSE 不再二次 HTTP 响应/终止符，只有 model_error + 一次 UNKNOWN settle。
- P1-3：paper pi+container 默认 `runner_egress=model_only`：runner `--network none`，host UDS PiModelForwarder 只转发 `/v1/chat/completions`，provider key 不进入 runner env。
- P1-4：ArtifactBudget 持久化（含 metadata）、玩家/Observer 关闭 retention、90% pressure、paper 5-agent 40GB < 51.5GB 上限。
- P2：wrapper 双 extension + no-start image probe；docs 当前 762 passed / 3 skipped / 734 subtests；bridge/MC token、完整 Docker 全栈 smoke 仍是 residual；Pi session 自动轮转/告警已实现。

## 2026-10-23 Pi 本地凭据隔离 + 真实烟测

- bridge token 改 inherited FD；本地 proxy apiKey 随机/首请求后删 models.json；MC token 移出 Pi env 到 0600 mc-env，只由 mc wrapper source。
- 真实 DeepSeek smoke：host 15 model/15 tool、container 13 model/15 tool，均 completed，env/run dir 无 token/key；container 为 model_only + --network none + UDS forwarder。
- 烟测中发现并修复 `_validate_model_forwarder` 用 `is_file()` 检查 Unix socket 的错误，以及容器 RPC shared dir 的 AF_UNIX 路径问题。
- 残余：Pi child `/proc/environ` 在 execve 后仍同 uid 可读但无秘密；mc-env 同 uid 可读（own-game scope）；loopback proxy/bridge 无 credential 时 401。


## 2026-10-23 Supervisor pause/recovery 死锁修复

- 现象：runtime recovery 作为 maintenance operation 运行时，pause control 被拒绝为 `maintenance operation already in progress`；同时 recovery 失败后主循环立即重启，`final11` 进入长时间 `recovering`。
- 修复：
  - `Supervisor.paused` 显式 gate，暂停时不再自动启动 runtime recovery / server health check；
  - recovery/server-health 期间的 pause/freeze control 会取消该 operation 并立即应用 control；
  - recovery 失败增加 backoff；达到 `max_recoveries` 后停止自动 recovery，等待 operator pause/stop/resume；
  - resume 清除 paused/recovery-exhausted gate。
- 测试：新增 pause/resume control unit test；全量 **762 passed / 3 skipped / 734 subtests**。
- `final11` 已停止并归档；新代码只影响未来启动的 Supervisor。


## 2026-10-23 8-agent paper-scale performance smoke

- 显式 `experiment.paper_scale_override` + generator `--mode paper --allow-paper-scale`：
  8 players 仍使用 paper prompt/tool/lifecycle；这是容量/性能测试，不是论文 4-player
  replication 声明。
- 5 分钟真实 Minecraft + DeepSeek 全栈：8/8 runtime_ready，9 Pi runners（8+Observer），
  263 model request/262 response，273/266 tools，Observer reports 2，0 infrastructure_error；
  status budget_exhausted（5min wall）。
- 性能：model p50 1.64s / p95 5.83s / max 18.19s；tool p50 207ms / p95 30.67s /
  max 65.20s；bot health p50 1.60ms；supervisor RSS ~310 MiB；容器 CPU peak ~193%、
  memory peak ~1.40 GiB；ledger +4.7 MB、run dir +68.7 MB / 5min。
- 链路发现：provider latency 是模型链主因；tool 长尾来自 Minecraft program；运行中
  player1 停止后 heartbeat watcher 仍反复记录 timeout（已修复为 finished 后退出）。
- 报告：[EIGHT_AGENT_PAPER_PERF_2026-10-23.md](EIGHT_AGENT_PAPER_PERF_2026-10-23.md)


## 2026-10-23 8-agent fixes / optimization

- 修复 runner 停止后 `_heartbeat_watch` 仍反复记录 `runner_heartbeat_timeout`
  的问题（finished 后直接退出）；新增回归测试。
- 修复 shutdown 时 Observer 等在途 `budget.pending` 永不结算：新增
  `Budget.recover_all_pending()`，按 unknown usage 保守计费并清空，新增测试。
- Pi model proxy 复用每个 runner 的 `httpx.Client`/连接池（含 UDS forwarder），
  减少每轮模型调用的连接握手开销。
- server 增加 `spigot.yml`（`moved-too-quickly-multiplier=100`），降低 8-agent
  并发下 Mineflayer 移动被 `invalid_player_movement` kick 的概率；镜像已重建。
- 8-agent 2 分钟复测：8/8 ready、9 runners、157/153 model、181/176 tools、
  **0 bot kick、0 heartbeat timeout、pending {}**、0 infrastructure_error；
  recheck model p50 1.31s / p95 4.17s / max 7.84s（此前 5min run 为 1.64/5.83/18.19，
  非受控对比）。
- 全量 Python：**762 passed, 3 skipped, 734 subtests**。


## 2026-10-23 max_run_bytes 调整

- `storage.max_run_bytes` 默认从 24 GiB 提升到 **100 GiB**
  (`107374182400` bytes，约 107 GB)。
- 同步更新所有 `configs/*.yaml`、`Recorder.storage_status` fallback 和
  `runners._PI_BUDGET_DEFAULT_GLOBAL_BYTES`。
- `max_artifact_bytes`/`pi_artifact_max_bytes` 仍为 16 GiB/4 GB，低于新 run cap。
- 长程检查报告已注明：100 GiB 下按 8-agent 5min 粗估约 100h 余量，但真实长时
  storage 曲线仍是 54h 前的必测项。


## 2026-10-23 Pi session cap / auto-rotation

- `storage.pi_session_max_bytes`: **2,000,000,000 bytes (2 GB)**；
  `storage.pi_session_rotation_limit`: **3**（0–100，0 表示不自动轮转）。
- 80/90/95% 阈值发 `pi_session_alert`；每 60s 发 `pi_session_sample`
  （bytes/limit/percent/growth/rotations）。
- 95% 在 Pi RPC 进程内执行有界 rotation：`new_session` + 归档旧 JSONL +
  compaction handoff prompt + 重新发 prompt；不重启 Pi 进程，不丢 workspace/memory。
- rotation 失败（abort timeout/new_session/archive）会记录
  `pi_session_rotation_failed` 并降级；达到 rotation limit 记录
  `pi_session_rotation_exhausted` 后按 infra_failed 结束该 agent。
- Observer 目前仍是 2GB hard-stop + alerts，不含 in-run rotation。
- 新增 e2e：真实 Pi CLI + fake upstream，强制小 cap 触发 rotation，验证
  `pi_session_rotation_started`/`pi_session_rotated`、旧 session 归档和继续执行。


## 2026-10-23 Supervisor 持久化修复

- 18:57 run 提前结束根因：DSH 子进程 scope 收到客户端 SIGTERM，连带杀掉 wrapper/Supervisor；
  不是模型/预算/wall/server 崩溃。
- `run-paper.sh` 新增 `PAPER_SUPERVISOR_CONTAINER=1`：在 detached Docker 容器中运行
  Supervisor，Docker daemon 托管，不再受 DSH/SSH 客户端断开影响。
- 新增 `scripts/container_supervisor.sh`：容器内运行 CLI，结束后 save/stop server 并
  `docker compose down`，记录 `SUPERVISOR_CONTAINER_EXIT`。
- 2 分钟验证：wrapper 退出后容器继续运行，4/4 ready，运行结束 cleanup rc=0，栈已清理。
- 说明文档：[PERSISTENT_SUPERVISOR.md](PERSISTENT_SUPERVISOR.md)
