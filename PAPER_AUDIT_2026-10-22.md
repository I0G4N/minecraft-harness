# 论文细节对齐审计（2026-10-22，运行中复核）

审计对象：arXiv:2610.22319v1《A team of Gemini 4 Argon agents beats Minecraft》全文
（https://arxiv.org/html/2610.22319v1，2026-10-15），当前工作树
`/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness`，当前运行
`four-agent-paper-20261022-viewer4`。

本审计只读取运行中的 ledger、manifest、配置、prompt、worker 配置和论文正文，不修改运行中的实验。
当前实验保持运行，作为 Viewer/infra 的 soak 测试。

## 0. 结论摘要

当前实现已经覆盖论文的核心闭环：4 个通用 coding agent + 第 5 个 Observer、Mineflayer 文本程序、
游戏聊天与共享笔记、服务端存档/日志证据、Easy/生存/禁用作弊、5 分钟存档、单程序 schema、
in-game days、Observer 三段式报告字段、服务端 `[Free the End]` 旁证。

但仍存在 **一个当前运行实际生效的 P0 配置传播 bug**、**一个 P0 随机世界语义差异**，以及若干
P1/P2 差异。最关键的结论是：

> 当前正在运行的这个实验不能作为"随机世界 + 论文 leftover/chat 语义"的对齐 run 引用。
> 它可以继续作为 Viewer/infra soak 测试；下一次正式 run 需要应用下面的 P0 修复后再重启。

## 1. P0：当前运行没有真正收到 leftover/chat 配置

### 现象

配置/manifest 声明：

```text
runtime.leftover_policy: allow
collaboration.chat_delivery: program_output
```

bot 容器环境也有：

```text
LEFTOVER_POLICY=allow
CHAT_DELIVERY=program_output
```

但 server 端生成的 worker_config 在传到 launcher 时被白名单裁掉了。修复前
`bot_runtime/src/worker-host.js:118` 只保留：

```text
mode username version lifecycle maxBackgroundJobs remainingRunMs
runDeadlineAt maxLogs maxLogBytes maxResultBytes
```

实际 worker 收到的 `BOT_WORKER_CONFIG` 不含：

```text
leftoverPolicy
chatDelivery
maxLeftoverScopes
```

worker 因此回退为默认 `reset`/`events` 语义。

### 当前运行证据

`four-agent-paper-20261022-viewer4`（截至本审计）：

```text
events: 6599
runtime_reset: 16 次
  unfinished_movement: 15
  unfinished_async_action: 1
runtime_exit: 30 次
infrastructure_error: 0
```

如果 `leftover_policy=allow` 真正生效，正常返回或超时的程序不应因为残留 movement/async action
触发 worker reset；当前 16 次 reset 与论文 §4 的"timed-out program 的残留代码继续运行、
用 stop counter 自己停止"机制相反。

同时 `chat_delivery=program_output` 也未进入 worker，聊天不会被 prepend 到下一个程序的日志中。

### 修复状态

已修改源码（尚未重启当前 run）：

```text
bot_runtime/src/worker-host.js:118
  + leftoverPolicy
  + chatDelivery
  + maxLeftoverScopes
```

并已重新构建 `minecraft-harness-launcher:research`。当前已经运行的 worker 仍使用旧镜像和旧
`BOT_WORKER_CONFIG`，必须重启 bot/launcher（以及因此重启实验）才能生效。

## 2. P0：当前世界是固定 seed，不是论文的随机世界

论文 §2/§5 明确从 **randomly generated world** 开始。当前运行：

```text
config experiment.seed: null
run-paper.sh 在启动前写入 .harness-world.json: seed 49
infra/server-data/world/level.dat WorldGenSettings.seed: 49
manifest.effective_environment.deviations: ["fixed_seed", ...]
```

因此当前 run 是 **seed 49 的固定世界**，不是随机世界。`experiment.seed: null` 只是没有配置期望 seed，
并没有自动随机化；启动脚本把 49 写死。

影响：不能把当前 run 当作论文随机世界采样的实验；同 seed 也会让不同 trial 共享地形/结构先验。
下一次正式 run 应在世界生成前生成随机 seed，并写入 server 属性 + marker；或明确把它登记为
"fixed-seed ablation"，不要用论文随机世界口径。

## 3. P1：成功权威仍是 advancement 快照，不是论文的服务端日志行

论文 §5：

> The win is in the server log: bulicame has made the advancement [Free the End],
> 27 hours 14 minutes after the first program.

当前确定性 evaluator 对 `kill_dragon` 使用：

```text
src/minecraft_harness/evaluation/scoring.py:273
method = "new_kill_dragon_advancement"
```

成功来自 world advancement 快照的 delta；快照通常最多晚一个 save interval。报告另外实现了：

```text
observer/report.py:_server_log_success()
```

会从注册的服务端日志段查找 `[Free the End]` 行，但该字段只是**旁证**，不决定 evaluator 的
`status=succeeded`，也不决定 first-success 时间。

影响：成功时间与论文的 server-log 时间不完全同口径；如果快照成功但日志行缺失（或反之），当前
报告可能显示两个不同结论。下一轮应明确二选一：
- 以日志行为权威成功，advancement 仅交叉核对；或
- 明确写成"advancement snapshot evaluator，log line 为旁证"。

## 4. P1：模型和 agent 主机不是论文配置

论文 §2.1/§9：

- Gemini 4 Argon；
- 每个 agent 在自己的 cloud machine 上；
- 4 玩家 + Observer 共 5 个通用 coding agent。

当前：

```text
model: deepseek-flash (https://api.deepseek.com)
agents: 4 players + LLM Observer
runtime: 五个 agent 各自运行在独立 runner 进程或独立容器
         （runner_mode: process | container）+ 一个 Supervisor 控制面；
         仍不是五台 cloud machine
```

这是已经登记的项目选择，不是隐藏 bug；但在论文对比/复现声明中必须明确写成"不同模型、不同主机
架构"，不能声称与 Gemini/cloud-machine 结果同分布。

2026-10-22 晚已实现 Phase 1（独立 runner 进程）和 Phase 2（per-agent runner 容器），
详见 [ARCHITECTURE_MULTIPROCESS.md](ARCHITECTURE_MULTIPROCESS.md)；5 台云机器仍是后续
可选工作。

## 5. P1：单程序接口、jobs helper 与输出边界

### 5.1 `paper_single_program` 只隐藏了模型工具，没有移除 JS 里的 `jobs`

`agents/tools.py` 在 `paper_single_program=true` 时隐藏 `mc_submit/mc_wait/mc_jobs/mc_job_cancel`
以及 `mc_status/mc_cancel/mc_pending/events_read`，这部分是对的。

但每个程序仍以：

```text
async function(bot, require, console, globalThis, isCancelled, jobs)
```

方式执行（`bot_runtime/src/worker.js`）。`jobs.start/status/cancel/list` 仍能显式创建后台任务，
`manifest.background_jobs` 也仍为 `true`。论文玩家接口是**单程序 CLI**，没有这个 helper。

影响：模型如果知道 `jobs`，可以绕过"单程序"执行节奏；manifest 也错误地报告有 background jobs。
建议 paper 模式不传 `jobs`（或传 `undefined`），并从 manifest/health 中区分"运行时支持"与
"本 profile 暴露"。

### 5.2 输出/超时边界

论文：程序打印的日志和返回文本，完整保留；timeout 由 agent 选择（默认 60s，未给硬上限）。

当前：

```text
bot MAX_PROGRAM_LOGS=16384 / 16MiB
MAX_RESULT_BYTES=8MiB
agent max_tool_result_chars=128000 (paper-tight context cap 112000 chars)
program_timeout_max_s=600
```

`mc_execute` 的模型侧结果是结构化 JSON（status/execution_id/...），paper-tight 只是去掉了
before/after 快照，并没有变成论文的"日志 + returned value"纯文本；超长输出会被截断成 excerpt。

影响：长输出/长程序与论文不完全同信息口径；`600s` 仍然是硬上限。

## 6. P1：Web search 默认缺失

论文 §2.1 每个 agent 的 coding workspace 有 web search；论文同时说明玩家程序本身不发网络请求，
且 2,839 个程序没有引用 web source。

当前：

```text
config web.enabled: false
manifest deviations: web_search_unconfigured
```

所以当前 run 的 agent 工具面比论文少一个 web search。若操作者配置搜索服务，可开启；否则最终
报告必须写 `web_search_unconfigured`，不能把它当作完全相同的工具集。

## 7. P1：Observer 报告字段已实现，但 Dashboard 只展示 findings

JSON schema 和持久化已经包含论文要求的三段：

```text
observer_report:
  progress_summary
  verified_events
  infrastructure_findings
  findings
```

最终 Markdown 也会列出 progress summary。可是 Dashboard 的 Observer feed 目前只渲染
`findings`（`static/dashboard.js` 的 `renderObserver()`），没有展示 progress summary、
verified events 和 infrastructure findings。

这是观测层缺口，不改变实验语义；如果要声称"论文的 Observer live report 可被研究者阅读"，
需要补齐 UI。

## 8. P1/P2：论文图表重建的两处小偏差

### 8.1 Figure 4 的 Nether 顺序

论文 Figure 4 的可视顺序是：

```text
Nether fortress → Blaze rods
              ↘ Ender pearls
```

当前 `figures.py` 的 `STAGE_ORDER` 是：

```text
nether_fortress, ender_pearls, blaze_rods
```

即 Ender pearls 被排在 Blaze rods 前面；和论文图的从左到右顺序不一致。阶段集合和 13 步数量
是对的，仅显示顺序需要交换。

### 8.2 Figure 3 的 restart 标记混入 runtime reset

论文 Figure 3 的橙色 restart 标记是**世界服务器 restart**（论文共 6 次，约暂停 20 分钟）。
当前 `figures.py` 的 `restarts` 查询包含：

```text
server_stopped / server_started / world_restored
runtime_recovery_started / runtime_reset
```

因此当前 16 次 runtime reset 会被画成与服务器 restart 同色的标记，视觉上会夸大论文口径的
restart 数。应拆成 `server_restarts` 和 `runtime_resets` 两条序列。

## 9. P2：prompt 的额外行为指令

当前 player prompt 除了论文的通用原则/环境/目标外，还加入了：

- 记录 observation time、epoch、evidence refs；
- never replay uncertain action；
- deterministic evaluator 决定成功；
- "当程序能观测 Ender Dragon health 或 end crystals 时，记录 JSON（dragon_health/crystals）"；
- 大量 paper-tight/background/compaction 相关约束。

其中"记录 dragon JSON"是为了补充 Figure 12 的玩家观测通道，**不在论文原始通用指令中**，会改变
agent 的行为分布。Figure 12 因此是"玩家程序观测"，不是服务端真值；报告必须保留这个限定。
其余框架性指令也应在方法/附录中列为 framework additions。

## 10. P2：全会话窗口

论文 §6 同时报告：

- up to the win（$520）；
- whole session，包括 Observer 和赢后一天（$810）。

当前正式 run 使用：

```text
stop_on_success: true
wall_time_minutes: 2160
```

即只采 **pre-win** 窗口；仓库另有 `configs/minecraft-paper-whole-session.yaml`（`stop_on_success:false`，
54h/30k calls），但没有在当前 run 使用。报告应明确当前是 pre-win 口径，不能与论文的 whole-session
费用直接比较。

## 11. P2：其他配置差异

- `server.properties`: `allow-flight=true`。论文只说标准 survival/Easy/cheats disabled，没有说
  allow-flight；当前配置应作为未解释 deviation 或改为 false 并验证 agent 兼容。
- `max_no_progress_turns=120`、`max_model_failures`、`max_event_failures`、预算上限、recovery 上限
  都是论文没有的 framework safety bound。
- `max-players=4`，Observer 不加入游戏；与论文的 4 玩家一致。
- `online-mode=false` / `enforce-secure-profile=false` 是本地 offline bot 部署选择，不是论文明确值。
- 5 分钟存档、Easy、survival、allow-commands=false、无人类输入、服务端聊天/死亡/advancement 记录
  这些核心项是对齐的。

## 12. 已对齐/已验证项

- 4 玩家 + 独立 LLM Observer；Observer 不玩、无玩家 socket、报告 advisory；
- Mineflayer + pathfinder；无手写游戏技能/课程/工具库；
- 程序 body 可采用 bot、globals、console、require、isCancelled；globals 跨程序（设计上）；
- 游戏聊天、共享 memory、chat delivery 设计为 program_output；
- 服务端日志/advancement/存档作为证据，客户端 claim 不评分；
- 1.20.4、survival、Easy、allow-commands=false、无 op、4 玩家；
- save interval 300s；世界恢复/服务端重启路径；
- Observer report schema 已有 progress/verified/infrastructure/findings；
- in-game days 从 level.dat 计算；server command audit；
- Figure 13 的五类消息模式（Warnings to teammates / Rescues / Shared chests / Lessons and tips /
  Exact coordinates）与论文 bars.svg 的标签一致（这是对图 13 的确定性近似，不是语义意图分类）；
- Figure 12 的 crystals/dragon health 只在玩家程序主动记录 JSON 时出现，报告明确标注来源；
- 当前 run 无 `infrastructure_error`，Viewer 4 路端口/iframe 验证通过。

## 13. 建议的修复顺序

1. **下一次 run 前**：使用包含 `leftoverPolicy/chatDelivery/maxLeftoverScopes` 的新 launcher 镜像，
   重启 bot/launcher，让配置真正进入 worker；同时确认 worker 侧有 `leftoverPolicy=allow` 和
   `chatDelivery=program_output` 的 manifest/health 证据。
2. **随机世界**：生成随机 seed，或把当前固定 seed 记为 fixed-seed ablation；不要用 current run
   声称 random-world replication。
3. **成功权威**：决定 server-log authority 或明确 snapshot evaluator + log corroboration；
   在报告中统一 `first_success_elapsed_s` 的来源。
4. **图表**：交换 Figure 4 的 Ender pearls / Blaze rods 顺序；拆分 server restarts 和 runtime resets；
   补齐 Observer progress/verified/infrastructure UI。
5. **接口/边界**：paper 模式不暴露 `jobs`；明确输出截断、600s 上限、web search 缺失、prompt additions；
   在最终报告中逐项列出。
6. **模型/主机**：继续明确 DeepSeek vs Gemini、同机 runner 进程 vs 5 cloud machines 的边界；同机 runner 已实现，容器化/云机器仍未实现。

## 14. 当前测试状态

`four-agent-paper-20261022-viewer4` 仍在运行：

```text
supervisor_active: true
4 个玩家 bot / worker 在线（偶发 worker 重建）
4 个 Viewer 端口 3105-3108
infrastructure_error: 0
```

本审计不停止该 run；它是 Viewer/长程 infra 的 soak 测试。上述 P0/P1 应在下一次正式 paper run 前
处理，当前 run 的数据不应作为完整论文对齐证据引用。

## 2026-10-22 修复后状态（final4）

已完成并已进入 `four-agent-paper-20261022-final4` 的修复：

- worker 配置白名单已补齐 `leftoverPolicy` / `chatDelivery` / `maxLeftoverScopes` / `paperSingleProgram`；
  四个 worker 的 `BOT_WORKER_CONFIG` 均已验证包含这些字段。
- `paper_single_program=true` 时 JS 程序不再收到 `jobs` helper；模型 schema 仍只暴露同步程序工具。
- 随机世界：marker 为 `origin=new_random`，实际 level.dat seed 与 server.properties level-seed 一致；
  `effective_environment.deviations` 为空。
- `allow-flight=false`，server.properties 模板和实际运行数据均已修正。
- kill_dragon 成功权威改为服务端日志 `[Free the End]`；advancement 只作为存档信号，
  缺失日志行时 evaluator 返回 `insufficient_evidence`，不再单独认证成功。
- Figure 4 的 Nether 顺序改为 `Nether fortress → Blaze rods → Ender pearls`。
- Figure 3 的 `restarts` 只包含 server_stopped/server_started/world_restored；runtime reset 单独输出，
  不再混入论文的服务器 restart 标记。
- Observer 页面已展示 progress_summary / verified_events / infrastructure_findings。
- final config 使用 whole-session 窗口（`stop_on_success=false`、54h wall、30k calls），
  程序超时上限提高到 3600s。
- 增加 loopback-only Bing HTML search adapter（`mc-web-search`，127.0.0.1:8899），
  paper 模式已启用 `web_search`；manifest 的 `web_search_unconfigured` 消失。
- 第一视角：view distance 降到 2；worker 现在向 viewer 转发 blockUpdate；实体更新做 150ms 节流；
  解决近距离方块在破坏/放置后不更新的问题，并降低推流负载。
- 服务器内存上限从 2g 提高到 4g；实际检查约 1.18GiB / 4GiB。
- Docker worker/control/world 子网和身份/worker 静态 IP 保持显式固定；Caddy 连接方式未变。

`final4` 当前检查：

```text
status: running
supervisor_active: true
runtime_reset: 0
infrastructure_error: 0
viewers: 3105/3106/3107/3108 全部 200
manifest deviations: []
```

仍保留的已知边界（不属于本轮可自动对齐项）：

- 模型为 DeepSeek deepseek-flash，不是 Gemini 4 Argon；
- 五个 agent 已拆为独立 runner 进程或独立容器，但仍是同一台 host，不是五台 cloud machine；
- 程序输出仍受 16MiB/8MiB/128k 模型可见上限约束；
- ten crystals 仍依赖玩家程序主动记录 JSON，Figure 12 不是服务端真值；
- 当前 run 是单个随机世界 run，不能证明成功率或论文统计分布。

## final5：web search 改为 DeepSeek 官方 Search/Fetch

- 删除自建 Bing HTML adapter 的运行时依赖。
- `WebTools` 现在使用：
  - `POST https://api.deepseek.com/v1/tools/search`
  - `POST https://api.deepseek.com/v1/tools/fetch`
- 使用 `DEEPSEEK_API_KEY` Bearer 鉴权；默认 `count=10`、`need_content=false`。
- Search 返回真实 URL、title、snippet、summary、site/date/authority；Fetch 返回 Markdown 正文。
- Observer 已获得同一组 `web_search/web_fetch` 工具，并保留只读/无玩家 socket 的边界。
- `final5` manifest：
  - `origin=new_random`
  - `deviations=[]`
  - `background_jobs=false`
  - web provider=deepseek
- 实测 supervisor 侧：
  - search: ok, backend=deepseek-search
  - fetch: ok, backend=deepseek-fetch

注意：Search/Fetch 调用目前尚未进入模型预算计费；搜索服务按请求次数计费，正式长程 run 应单独记录请求次数和费用。
