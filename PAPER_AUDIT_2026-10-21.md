# 论文细节二次审计（2026-10-21）

审计对象：arXiv **2610.22319v1**《A team of Gemini 4 Argon agents beats Minecraft》全文（HTML 正文，
本地分析副本 `runs/_analysis/paper.txt`，649 行），对照本仓库 `experiments/minecraft_harness` 当前实现
（branch `harness_embodiment_v1`，Python 347 / Node 51 项测试通过）。

分类：✅ 一致 ｜ ⚠️ 语义不同（会改变实验行为/可比性） ｜ ❌ 未实现 ｜ ➕ 超出论文边界（工程增强或额外能力） ｜ ❓ 无法从论文确认。

## 0. 结论摘要

- 论文的核心闭环（四个通用 coding agent + 第五个 Observer、Mineflayer 文本程序、游戏聊天/共享笔记、
  服务端日志为唯一成功权威、每 5 分钟存档、Easy/生存/随机世界/禁用作弊、无人类操作输入）在本框架中
  是**有对应实现**的。
- 但有 **2 个 P0 级语义差异**会直接改变实验环境：**残留异步代码/超时语义**、**并发提交与显式 jobs**。
  这两点使 paper_aligned 不能被称为"逐行为复现"。
- 有 **一组 P1 缺失**：Observer 报告结构、in-game days（论文 ~81 天）、图 4 的 13 步阶段表（2026-10-21
  已在观测台实现动态近似，含证据与精度标注）、图 13 的行为分类（已实现确定性近似）、获胜后继续运行的
  全会话口径、web search 默认关闭、程序输出保留边界、Node 内置模块范围、每个 agent 独立云机器。
- 有 **一组 ➕ 工程增强**超出论文描述：确定性评测/证据封存、预算与成本上限、worker 重启/watchdog/
  进程隔离、身份代理与原生沙箱、checkpoint/rollback、额外玩家工具（mc_submit/mc_wait/mc_jobs/
  events_read/history_read/artifact_read、自动 before/after 快照）、实时第一视角直播、多任务/多 trial 等。
  其中一部分只影响可观测性（可接受），另一部分会改变 agent 可见信息或调度（需要在论文对齐声明中降级）。
- 另有 **2 处文档与实现不一致**必须修正：`PAPER_ALIGNMENT.md` 声称 Observer 报告包含"进度摘要+已核实
  事件+发现"，但 `observer_report` schema 只有 `findings`；`PAPER_ALIGNMENT.md` 对超时残留代码的
  描述弱于论文实际行为（论文 §4 明确把 timed-out 程序的残留代码作为机制统计）。

## 1. 一致项（有对应实现，不展开）

| 论文要求 | 证据 | 框架对应 |
| --- | --- | --- |
| Minecraft Java 1.20.4、survival、Easy、随机世界、禁用作弊、无人类输入 | §2 p94 | `runtime.mode=minecraft`，paper 模板 `seed: null`、`require_fresh_world: true`；`environment.py` 校验版本/难度/allow-commands/在线模式等 |
| 四个玩家 + 第五个 Observer；Observer 不玩、独立见证 | §2 p88, §2.3 p112 | `agents.count=4` + `observer.mode=llm`（paper_aligned 强制）；Observer 无游戏/玩家/预算工具 |
| 玩家用 JS 程序操作 Mineflayer + pathfinder，无预置游戏技能/课程/工具库 | §2 p88-98, §7 p282 | `mc_execute` + bot/pathfinder；`capabilities.js` 只有库模块，无游戏技能；测试夹具与玩家工具隔离 |
| 一次程序/一次返回；默认 60s 超时；globals 跨程序保留 | §2.1 p104 | `program_timeout_default_s: 60`；VM context 的 `globals` 持久（直到 reset） |
| 玩家通过游戏聊天与共享笔记协作；笔记作者写、队友读 | §2 p92, §2.1 p104, Fig.2 | `game_chat` + `shared_memory`；`memory_*` 工具，team 笔记作者写、他人读 |
| 服务端聊天/死亡/advancement 是权威证据；不信任玩家返回值 | §5 p270 | `server_evidence` 收集 stats/advancements/log；确定性 evaluator；报告区分 claim/verified |
| 每 5 分钟存档；失败后从最新存档重启 | §2.1 p104 | paper 模板强制 `save_interval_s: 300`；`managed_container` 恢复路径 |
| 完整记录程序、消息与游戏事件 | §5 p270, §9 p317 | events 账本 + artifact（源码/结果/日志哈希）；服务器证据快照 |
| Observer 检查 claim、报告 specification gaming/伪造、只在环境故障时介入、不改策略 | §2.3 p112 | observer prompt + 只读证据 + supervisor 校验的 `infra_issue`；确定性评分不被模型覆盖 |
| 研究者可实时看 dashboard、暂停/停止，但不干预 | §9 p317 | 观测台（总览/事件/程序/聊天/证据/Observer/资源/直播）和 pause/stop 控制 |
| 费用按 list price + token counters 估算；区分玩家/Observer、成功前/全会话 | §6 p274 | usage-v2（role/purpose/cached/reasoning/unknown）；报告分组与窗口 |

## 2. P0：会改变实验语义的差异

### 2.1 超时/程序结束后的残留异步代码：论文允许继续运行，本框架强制清除并重启 worker

- 论文：§2.1（p104）"waits up to a timeout that the agent chooses (60 seconds by default)"；§4（p256）
  明确统计"**a counter that makes leftover code from a timed-out program stop itself**, referenced in
  **1,519 programs**"，以及"**146 pieces of state and code alive between programs**, including 42 helper
  functions: a background loop that keeps a bot afloat in water, timed walking, item counting…"。
  即：程序超时/返回后，其未完成的异步代码在**同一 bot 进程**里继续运行，玩家可以依赖它、并自己写计数器
  让它停止。
- 本框架：`worker.js` 在每个前台程序结束后调用 `cleanup(scope)`，清掉该程序的 timer/listener/action
  (`bot_runtime/src/worker.js:27-43`)，随后若仍有 `unfinishedActions` 或 bot 仍在移动，则
  `needs_reset: true`、`reset_reason: unfinished_async_action|unfinished_movement`
  (`worker.js:235-252`)；runtime 在 `finish(..., reset=true)` 里 `killWorker()`，**globals 全部丢失**
  (`server.js:442-459`)；程序超时同样 `finish(..., reset=true, reset_reason='timeout')`
  (`server.js:423`)。当前 prompt 因此额外要求 "Always await movement ... unfinished async work forces
  a runtime reset"（`agents/prompts.py:41-46`），这是为框架语义打的补丁，而不是论文行为。
- 影响：论文的"长驻代码/后台循环/超时残留"策略在 paper_aligned 下基本不可用（除非改用 `jobs.start`
  显式注册，见 2.2）；每次 restart 还会丢 globals、断开 live viewer、增加 bot 重连。我们的真实 run8
  在 17 分钟内出现 **30 次** worker reset（每约 4 秒一次），就是这一语义与 agent 行为交互的结果。
- 结论：**P0，和论文语义不同**。若目标是"复现论文环境"，需要兼容模式；若坚持安全重置，则必须把
  paper_aligned 定位为"重新设计的严格化环境"，不能声称行为对齐。

### 2.2 并发提交与显式 jobs：论文是单程序 CLI，本框架提供并行执行/后台任务 API

- 论文：§2.1（p104）"sends a JavaScript program ... waits up to a timeout ... prints what the program
  logged and returned"；§2（p88）"each player had a program running about 40% of the time"，描述的是
  **一次一个程序**的同步循环；背景工作是程序留下的异步代码（2.1），不是额外的提交 API。
- 本框架：paper_aligned 玩家额外获得 `mc_submit`（不等待提交）、`mc_wait`、`mc_jobs`、`mc_job_cancel`
  (`agents/tools.py:64-73`；`core.py:52` 只在 lifecycle==paper_aligned 时启用)，以及 JS 内的
  `jobs.start(fn,{ttl_ms})/status/cancel/list`，上限 `max_background_jobs: 8`、TTL 36h
  (`bot_runtime/src/worker.js:78-106`；paper 模板 runtime 段)。prompt 主动教模型使用这些 API
  (`prompts.py:20-27`)。
- 影响：允许**同时运行多个程序**、把背景逻辑显式注册为 jobs，改变了论文的单线程编程节奏、40% 运行
  占比、故障恢复与协作时序；这是"实现超过论文边界"的典型项。
- 结论：**P0，超出论文边界**。忠实复现应关闭 `mc_submit`/`mc_jobs`/`jobs.start`（仅保留 `mc_execute`
  同步），或者把"显式 jobs"明确写成 framework 增强而非 paper 对齐。

## 3. P1：未实现或实现不完整

### 3.1 Observer 报告结构缺 2/3

- 论文：§2.3（p112）Observer "published a live report ... with a **summary of progress**, a **log of
  verified events** and its **findings about the infrastructure**"。
- 本框架：`observer_report` schema 只有 `findings`（每条 `status/claim/evidence_refs`）
  (`observer/agent.py:71-75`)；prompt 却声称报告含"进度摘要、已核实事件、发现"（`prompts.py` observer
  段），`PAPER_ALIGNMENT.md` 第 84 行也这样写——**文档/提示词/schema 三者不一致**。infra findings
  只能混在 findings 里，没有独立字段；`infra_issue` 只有 `reconnect/runtime_recovery/server_recovery`
  (`observer/agent.py:30`)，论文所说的 **broken tool / quota ceiling** 没有对应类型（预算由 supervisor
  控制，不允许 Observer 提高）。
- 建议：扩展 schema（`progress_summary`、`verified_events`、`infrastructure_findings`）或修正 prompt/
  文档，二选一；如果要忠实对齐，建议扩展 schema 并同步观测台展示。
- 结论：**P1，未实现 + 文档不一致**。

### 3.2 In-game days（论文 ~81 天）没有计算

- 论文：§3（p116）"spans about **81 in-game days**"。
- 本框架：报告有 `wall_time_s`、`server_stopped_time_s`，没有 in-game day 字段；世界 `level.dat` 的
  DayTime 未采集（`observer/report.py`、`analysis.py` 无 `in_game_day`）。
- 结论：**P1，缺失指标**。

### 3.3 图 4 的“13 个依赖步骤”时间线（2026-10-21 已部分实现）

观测台「论文图表」新增 Fig.1/4 十三步进度：混合 advancement（含成就时间）、world statistics
（首次出现快照时间为观测上界）、服务端日志 advancement 标题；按四个维度分栏显示首达时间与玩家。
仍缺：十水晶步骤无对应证据（unavailable）、论文原始 13 步 taxonomy 的精确判定规则、玩家角色摘要。


- 论文：§3 Fig.4（p120）"thirteen dependent steps from initial spawn to the Ender Dragon, each with
  the time the team first reached it and the player who did"。
- 本框架：阶段来自 advancement（世界文件/server log 的 generic `stages` 表，
  `observer/analysis.py:218-257`），没有论文的 13 步 taxonomy、首达时间/玩家归属的专用表；部分步骤
  （如末地水晶、12 眼、坐标定位）不是 advancement，无法从现表恢复。图 3 的 6 次 restart 标记也没有
  restart count/list（只有 `server_stopped_time_s`，`report.py:325`）。
- 结论：**P1，分析产物未实现/不完整**。

### 3.4 图 13 的聊天行为分类与论文不同（2026-10-21 已提供近似动态图）

观测台新增 Fig.13 行为与死因：论文五类行为（specialization/shared memory/own tools/looking out/
corrections）用本项目的确定性正则近似，并同时保留本项目词法标签；死因来自服务端规范化日志。
规则仍不是论文原始模式，页面与 DASHBOARD.md 都标注为近似。


- 论文：§4 Fig.13（p244）"messages that match simple patterns for each behavior"：specialization、
  shared memory、own tools、looking out for each other、corrections。
- 本框架：报告使用 `warning/rescue/shared_storage/lesson/coordinates` 五类（`analysis.py:423`），
  与论文的五种行为不是同一 taxonomy（warning≈looking out、lesson≈corrections 部分重叠，缺
  specialization/tools）。`PAPER_ALIGNMENT.md` 已注明"分类规则由本项目定义"，但若论文对齐需要更接近
  论文 Figure 13，应另设版本化规则。
- 结论：**P1，分析口径不同**（不影响执行，影响可比较性）。

### 3.5 获胜后继续运行（全会话）默认不采集

- 论文：§6（p274）区分 "up to the win"（$520）与 "whole session, including the Observer agent and
  the day of activity after the win"（$810）。
- 本框架：paper 模板 `experiment.stop_on_success: true`（`configs/minecraft-paper-aligned.yaml:9`），
  到 advancement 成功即收尾；wall 上限 2160 min（36h）也短于"27h14m + 一天"的全会话。
- 结论：**P1，默认只复现赛前口径**。若要全会话口径，需要 `stop_on_success: false` + 更长 wall/预算，
  并同时报告 pre-win/whole-session 两个窗口。

### 3.6 Web search 默认关闭

- 论文：§2.1（p102）每个 agent 都有 "a code workspace with version control, a shell, file editing
  and **web search**"；同时 §2.1 说玩家程序本身没有网络请求。
- 本框架：paper 模板 `web.enabled: false`（`configs/minecraft-paper-aligned.yaml:55-57`）；只有操作者
  配置了搜索服务才启用 `web_search/web_fetch`（`agents/tools.py:75-78`）。默认比论文少一个编码工具。
- 结论：**P1，默认缺失（可配置）**。

### 3.7 程序输出保留/截断比论文严格

- 论文：§2.1（p104）"prints what the program logged and returned"；§5（p270）"keeps every program
  with its output"。
- 本框架：bot runtime 每程序日志上限 128 条/256 KiB（`MAX_PROGRAM_LOGS`、`MAX_PROGRAM_LOG_BYTES`，
  `server.js:561-562`）；模型可见 tool result 上限 16000 字符（`config.py:18`、`agents/core.py:140`）；
  超出部分只可通过 `artifact_read` 读自己的原始产物，并标记 `evidence_incomplete`。论文未说原实现有同等
  截断；这会让模型看到的输出与论文不同。
- 结论：**P1，不同/更严格**（可通过配置提高上限，但"完整输出"语义仍未完全等价）。

### 3.8 Node 内置模块范围比论文窄

- 论文：§2.1（p104）程序 "can load the built-in modules of Node.js and the installed Mineflayer
  packages, but cannot start processes or write files"。
- 本框架：paper 白名单只有 assert/buffer/crypto/events/path/querystring/string_decoder/url/util/zlib +
  vec3/minecraft-data/mineflayer/mineflayer-pathfinder（`bot_runtime/src/capabilities.js`）；fs/os/stream/
  http 等不可用。fs 写入由原生沙箱另行禁止，因此这里 strict 于论文可能允许的读取面。
- 结论：**P1，更严格**（安全上合理，但论文对齐声明中应写明）。

### 3.9 每个 agent 独立云机器没有对应实现

- 论文：§2.1（p102）、§9（p313）"Each of the five agents ... on its own cloud machine" / "The agents
  themselves ran on dedicated cloud machines"。
- 本框架：五个 agent 是同一个 supervisor Python 进程里的 asyncio 循环；只有 bot runtime/worker 可远程
  容器化，模型调用共享事件循环与内存。故障隔离、并发性能与论文不同。
- 结论：**P1，架构差异**（`PAPER_ALIGNMENT.md` 只写"本地隔离容器，不宣称云机器相同"，应更明确写
  "agent 本身不独立"）。

### 3.10 程序超时上限 180s

- 论文：§2（p88）"a time limit chosen by the agent"，默认 60s，未给上限。
- 本框架：`program_timeout_max_s` 默认 180（`config.py:23`），paper 模板未覆盖；agent 选择被截到 180s。
- 结论：**P1，边界更窄**；对中位 33.5s 影响小，但会阻止论文中可能存在的长程序。

## 4. ➕ 超出论文边界（工程增强/额外能力）

只影响观察/安全、不改变论文语义的记为 ➕(观察/安全)；会改变 agent 可见信息或调度的记为 ➕(行为风险)。

| 项 | 实现 | 边界判断 |
| --- | --- | --- |
| 确定性 evaluator（多任务 observe_only/obtain_logs/craft_table/iron/kill_dragon 与证据封存） | `evaluation/scoring.py:15`；kill_dragon 用 world advancement delta（`scoring.py:273`） | ➕：论文只用 server log 的 [Free the End] advancement 判成功；本框架证据通道是 world 文件快照而非 log 字符串，且评测更"强"（带 baseline/player scope）。不改变游戏，但证据口径不同，报告应注明 |
| 预算/成本/墙钟/调用数上限、max_request_bytes、max_tool_result_chars、maxLogs 等 | `budget.py`、`config.py`、runtime env | ➕(行为风险)：论文没有预算上限；上限会截断长实验，成本/输出边界也会改变模型可用信息 |
| worker reset、watchdog、RSS 上限、recovery 上限 20 次 | `server.js`、`watchdog.js`、`supervisor.py` | ➕(行为风险)：见 2.1；论文未描述这些故障域；recovery 次数上限也是论文没有的停止条件 |
| 原生沙箱/身份代理/模块白名单/只读容器 | `capabilities.js`、`deployment.py`、paper 模板 `workspace.network none` | ➕(安全)：比论文 §9 的文字更强；但 3.8 的模块面反而更窄 |
| 额外玩家工具：`mc_status/mc_cancel/mc_pending`、`events_read`、`history_read`、`artifact_read`、自动 before/after 快照 | `agents/tools.py:34-63`；worker snapshot | ➕(行为风险)：论文玩家只有 CLI+编码工具+记忆+聊天；`events_read`/history/artifact 是额外信息通道，`before/after` 自动注入也比论文"程序打印什么"更多 |
| `mc_submit/mc_wait/mc_jobs/jobs.*` | `tools.py:64-73`、`worker.js:78-106` | ➕(行为风险)：见 2.2，最明显超出论文的执行模型 |
| checkpoint/rollback/restart 世界恢复、冻结、操作员控制 | `world_checkpoints.py`、`supervisor.py` | ➕(行为/安全)：论文只描述"失败后从最新存档重启"；rollback 是额外能力，可能改变世界轨迹 |
| 实时第一视角直播（prismarine-viewer）、观测台 8 个视图 | bot runtime viewer、`dashboard.py`/`static/` | ➕(观察)：论文只有 dashboard 观察/暂停/停止；直播是额外观察能力，也是我们最近为人类观看加的功能，不应计入论文方法 |
| 多任务/多 trial/prepare-trials/评测报告、上下文确定性 handoff、memory CAS、事件/证据哈希 | `trials.py`、`observer/`、`memory/` | ➕(工程/观察)：论文是单 run、无这些设施；对可复现性有益，但与论文"同一 harness"不同 |
| 程序 API 的 `jobs` helper、`isCancelled`、`globals`、`console`、`require` | `worker.js:218-237` | 部分一致：论文示例显式用了 `isCancelled()`；`jobs` 是额外 helper；`globals`/console/require 论文有对应描述 |
| prompt 额外指令：never replay uncertain action、evidence refs/epoch、deterministic evaluator、await async、tool results untrusted、artifact_read 等 | `agents/prompts.py:6-19,41-58` | ➕(行为风险)：论文的通用要求是观察/验证/工具/记忆/背景工作/诚实/不利用漏洞；本框架额外加入了一批机制性约束，会改变 agent 行为；论文对齐应区分"通用+环境"与"框架补充" |

## 5. 文档/实现不一致点（需要修正）

1. `PAPER_ALIGNMENT.md:84` 与 observer prompt 说报告含"进度摘要+已核实事件+发现"，但
   `observer_report` 只接受 `findings`（3.1）。要么扩 schema，要么改文字。
2. `PAPER_ALIGNMENT.md:60-61` 对"残留代码/超时"的描述是"显式注册的后台任务"和"无法确认旧代码停止时
   重建容器"；论文 §4 明确说 timed-out 程序的残留代码继续运行（1,519 programs 使用停止计数器），
   这不只是"未公开实现细节"，而是论文公开描述过的机制。应把这处升级为"已知 P0 差异"。
3. `configs/minecraft-paper-aligned.yaml:7` 的目标段没有论文 §2 的完整 objective 段：
   论文 p96 "This is the only criterion. Any means of meeting it within the rules of survival mode is
   permitted, and the players decide how to share the work." 当前模板只保留第一句 + "Decide together
   how to share the work"，少了"唯一标准/允许生存模式内任何手段"这层边界。
4. `PAPER_ALIGNMENT.md:43` 把图 1/4 的阶段时间线概括为"服务端 advancement 与记录时间线"，但图 4 是
   13 个手工定义的依赖步骤（含非 advancement 步骤），实现只覆盖 generic advancement（3.3）。

## 6. 建议的修复顺序

**先决策（P0，需你确认方向）**

1. **残留代码兼容模式**：为 `paper_aligned` 增加 `runtime.leftover_policy: reset|paper`：
   - `reset`：保留现安全语义（当前默认）；
   - `paper`：超时/返回后不 kill worker，保留 timer/异步代码与 globals，允许下一程序继续使用；
     需要重新设计取消、租约和故障恢复，风险高。若不做，论文对齐声明必须写"环境语义不同"。
2. **关闭/降级并发工具**：paper 复现默认禁用 `mc_submit/mc_wait/mc_jobs/jobs.start`（仅 `mc_execute`），
   或把它们标记为 framework-only 能力，不进入论文口径。

**P1（低风险、可尽快做）**

3. 扩展 `observer_report`：增加 `progress_summary`、`verified_events`、`infrastructure_findings`（或最小
   增加 `progress_summary` + `infrastructure_findings`），同步观测台与测试；`infra_issue` 增加
   `broken_tool`/`quota` 的**报告**类型（是否允许 supervisor 修复由现有权限决定；预算仍不可由 Observer 提高）。
4. 修正 `PAPER_ALIGNMENT.md` 的 3 处文字，并把本文档加入链接。
5. paper 模板目标段恢复论文完整 objective 文本；把 `stop_on_success` 与"全会话口径"写成注释/可选项。
6. 增加 in-game days、restart count、13 步阶段表、图 13 行为分类的版本化分析（可先只做数据字段，不覆盖
   现有关键指标）。
7. 提高 paper 模式程序日志/输出上限（如 `MAX_PROGRAM_LOGS=1024`、`MAX_PROGRAM_LOG_BYTES=4MiB`），并在
   report 中标注 `evidence_incomplete`。
8. 若操作者提供搜索服务，paper 模板启用 `web_search`；否则在 manifest/报告里标 `web_search_unconfigured`。
9. 在 `PAPER_ALIGNMENT.md` 明确"agent 本身不独立于 supervisor 进程、模型不是 Gemini 4 Argon、单 run
   无法复现论文统计"三条边界。

## 7. 复现方式

```bash
# 论文正文（v1 HTML，分析副本不入库）
curl -sSL https://arxiv.org/html/2610.22319v1 -o paper.html
# 本次审计对照的代码
git -C /ssd_data/caizihao/minecraft/zkdx rev-parse --abbrev-ref HEAD
```

---

## 2026-10-21 二次复核更新

第二轮复核（含运行中修复）见 [PAPER_ALIGNMENT.md](PAPER_ALIGNMENT.md) 的「2026-10-21 二次复核」以及
`runs/_analysis/paper_delta_2026-10-21.md`。已关闭/缓解的工程项：Observer transient model retry、
malformed model response retryable、server restart 前日志 drain、paper bot `MAX_TIMEOUT_MS` 同步、长程
retry/recovery/storage 参数显式化、figures API TTL。仍开放的核心语义项：leftover async/timeout 语义、
paper_aligned 默认异步工具集、Observer schema/report、成功时间日志佐证、全会话窗口、model/host/context
差异，以及 in-game days/Fig.12/server-command audit 等数据项。

> 2026-10-21 修复更新：leftover-code 兼容模式、paper_single_program、Observer 完整报告、in-game days、
> success log/server command audit、whole-session 模板、session-loss recovery 已实现；见
> [PAPER_ALIGNMENT.md](PAPER_ALIGNMENT.md#2026-10-21-深夜修复结果覆盖上述历史缺口)。仍开放的语义项见该节。

