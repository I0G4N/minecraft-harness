# dsh-mc-agent 与本项目对比：可吸纳能力清单

- 对比对象：<https://github.com/blueziii/dsh-mc-agent>
- 本地克隆：`reference/dsh-mc-agent`（main，commit `0a3320d`，2026-10-25）
- 本项目：`minecraft-harness/`（8-agent paper-aligned 实验框架）
- 结论先行：**两者不是同一层工具，不能整体替换；但 dsh-mc-agent 的“动作调度 / 寻路补丁 / 取消语义 / 崩溃边界 / 测试档案”非常适合被吸纳到我们的 bot worker 内部。**
- 验证状态：本次是源码/文档静态对比，未运行 dsh-mc-agent 的 DSH 宿主或 43 项自检；所有“收益”是设计推断，落地前需要用小规模 A/B 和 20 分钟诊断验证。
- 许可：dsh-mc-agent 为 **MIT**（whale-craft derivative）；若复制/改写其代码，保留 `LICENSE` 与来源说明。

---

## 1. 两者定位不同

| 维度 | 本项目 | dsh-mc-agent |
| --- | --- | --- |
| 核心目标 | 多 Agent、可复现、可审计的 Minecraft 实验 | 给 DSH 里的 AI 一副“能玩 Minecraft 的身体” |
| 模型层 | Pi runtime / 自研编程 Agent，生成并执行 JS 程序 | DSH 宿主 + 40 个 `mc_*` 工具调用 |
| 游戏控制 | Agent 直接拿 `bot` API 写异步 JS | 模型调用高层工具，工具内部操作 bot |
| 多 Agent | Supervisor、8 个独立 runner/worker、共享账本 | 一个 DSH 会话里的一只 bot |
| 实验证据 | SQLite ledger、报告、任务成功判定、8-agent 稳态 | 任务台账/自检/踩坑档案，不是实验评估框架 |
| 部署 | Docker Compose、per-execution runsc、身份代理、viewer | DSH profile bundle，本地/宿主进程 |
| 相同基础 | mineflayer、mineflayer-pathfinder、Node 22、Minecraft 1.20.4 | 同左 |

所以：

- **不能直接安装 dsh-mc-agent 到本项目**：它依赖 DSH 的 `@deepseek-ai/dsh-*`、bundle/plugin、host session、`agent.steer` 等，我们的 Supervisor/Pi runtime 不提供这些。
- **可以吸收它的底层能力**：动作调度、寻路/移动语义、取消与清理、异常边界、测试用例和 bug 档案。

---

## 2. 它最值得吸收的部分

### P0：动作调度器 + 移动代次 + 控制位租约

对应文件：

- [action-scheduler.mjs](reference/dsh-mc-agent/src/action-scheduler.mjs#L1-L120)
- [core.mjs](reference/dsh-mc-agent/src/core.mjs#L487-L533)（`_moveGen`、`interruptMove`、`#beginMove`、`#stopPathing`、`holdControl`）
- [踩坑档案 #9](reference/dsh-mc-agent/踩坑档案.md#L180-L201)（两份移动共用一个 pathfinder）
- [踩坑档案 #36](reference/dsh-mc-agent/踩坑档案.md#L733-L743)（领号、认中断、控制位租约）

它解决的核心问题：

- 一个 bot 只有一个 `bot.pathfinder`、一套 `controlState`、一个挖掘/交互状态；
- 两次移动或“移动 + 采集”并发时，后结束的一方会把前一方的路径/控制位一起停掉；
- 直接重试会让模型误判“路不通”，实际上是被另一路动作打断。

它的做法：

- 动作按资源登记：`movement` / `interaction` / `inventory` / `combat`；
- 每个动作有 `actionId`、父动作、优先级、可抢占边界；
- 移动有 `_moveGen` 代次，`finally` 先核对“还是不是我”，避免旧动作清理新动作；
- 控制位有租约，不允许收尾方松开别人刚按下的键；
- `stopPathing()` 同时 `setGoal(null)` + `stop()`，因为 pathfinder 的 `stop()` 只是置标志；
- 超时/取消时显式 `cancelCollect`，因为 `withTimeout` 只是“不等了”，不会停止底层任务。

**为什么它和我们的 nonfinite 问题强相关：**

我们的 worker 当前是：

- [worker.js](bot_runtime/src/worker.js#L62-L83)：清理只按 `goalOwner` 停 pathfinder，没有移动代次；
- [worker.js](bot_runtime/src/worker.js#L500-L527)：`leftoverPolicy=allow` 时，paper 程序正常返回**不清原生动作**，旧异步动作可以继续动；
- [worker.js](bot_runtime/src/worker.js#L246-L283)：给 agent 暴露了 `bot.pathfinder`，但没有资源协调；
- `configs/minecraft-paper-aligned.yaml` 同时是 `paper_single_program: true` + `leftover_policy: allow`。

这会产生与 dsh-mc-agent bug #9/#36 同构的场景：

1. 程序 A 启动 `pathfinder.goto` 或 `dig` 后返回；
2. paper 语义保留 leftover，原生动作继续；
3. 程序 B 又启动移动/挖掘；
4. 两个动作共享同一个 pathfinder、控制位和 `bot.entity`；
5. Mineflayer 客户端物理状态被并发写入/停止，最终出现 nonfinite position/yaw/pitch；
6. 我们现在的 guard 只能等 30 秒后重启 worker。

**建议**：把 action scheduler 降维实现成 worker 内部模块（不改 agent API）：

- 资源粒度先只做 `movement`、`interaction`、`inventory`、`combat`；
- 默认 `enforce` 或先 `observe-only` 采集冲突数据；
- 单飞 `pathfinder.goto`，第二个请求排队/返回 busy，而不是互相 stop；
- 引入 `moveGen` 与 `controlLease`；
- 显式取消 collect/dig，而不是只 race timeout；
- 对 paper leftover：保留后台任务，但**原生动作必须有 owner 和 lease**，旧 scope 的收尾不能停新 owner 的动作。

预期收益：直接命中当前“worker 重启 241 次/4 轮”的主要并发来源，且不需要改 paper 的 agent 编程接口。

**风险**：我们允许 leftover 跨程序存活；调度器不能简单地把 leftover 全部杀掉。建议先以 `observe-only` 跑 20 分钟，记录冲突数和动作 owner，再切 `enforce`。

---

### P0：mineflayer-pathfinder 2.4.5 的两个 vendor 补丁

对应文件：

- [PATHFINDER.md](reference/dsh-mc-agent/PATHFINDER.md#L73-L97)（补丁表、上游复核）
- [踩坑档案 #4](reference/dsh-mc-agent/踩坑档案.md#L81-L106)（1 格台阶/墙角卡死）

它打的两个补丁：

1. `lib/physics.js`：`collides()` / `simulateStraight()`，贴墙穿模不再被判定为“能直走”；
2. `lib/movements.js`：`getMoveDiagonal` 禁止斜切拐角；上游 master 用 `if (y === 1) return` 修了同类问题，但 npm 上的 2.4.5 没带。

本项目当前依赖：

- `mineflayer@4.35.0`、`mineflayer-pathfinder@2.4.5`（[package.json](bot_runtime/package.json)）
- 当前仓库构建路径里**没有**看到 pathfinder 的 vendored patch 或 postinstall 校验脚本；因此从构建配置看，运行的是 npm 2.4.5 原版，dsh-mc-agent 记录的两个拐角/穿模问题应仍在。由于本地 `bot_runtime/node_modules` 未安装，无法逐行核对打包镜像内的最终文件；如果镜像构建阶段有外部 patch，需要以镜像内文件为准。

**建议**：把这两个补丁作为 `bot_runtime/patches/` + `postinstall` 校验引入，或升级到包含上游修复的 pathfinder commit。先只打补丁，不升级 mineflayer 大版本。补丁是 MIT 项目 derivative，保留来源说明。

预期收益：降低“卡住→反复 pathfinder→客户端状态异常”的概率；成本低、可单独回归。

**注意**：dsh-mc-agent 还调了 `thinkTimeout=8000`、`tickTimeout=40`、`maxDropDown=3`、`liquidCost=4`、`digCost=4`、`placeCost=12` 等参数（见 PATHFINDER.md）。这些应按我们的实验场景做 A/B，不要一次全改。

---

### P0/P1：显式取消与“超时 ≠ 失败”的分类

对应文件：

- [踩坑档案 #3](reference/dsh-mc-agent/踩坑档案.md#L62-L77)（dig 超时不停后台任务 → 物理冻住）
- [task-runner.mjs](reference/dsh-mc-agent/src/task-runner.mjs#L1-L110)（失败分类：`budget` / `timeout` / `preempted` 可重试）
- [task-report.mjs](reference/dsh-mc-agent/src/task-report.mjs)

关键教训：

> `withTimeout` 只是“不再等”，不是“取消”；被它包住的第三方长任务要显式取消。

我们的 worker 目前对 `bot.dig` / `pathfinder` 的长期动作主要依赖 deadline 和 guard；paper leftover 还会保留原生动作。这与 #3 的形态相似。

**建议**：

- 对每次 execution 记录它启动的 native operation（pathfinder/goto、dig、place、collect）；
- 显式 cancel/timeout/abort 时，调用 `stopPathing`、`stopDigging`、清控制位；
- 正常返回时按 paper 语义保留，但要由调度器管理 owner/lease，不能变成无人管的并发写；
- 把 `timeout` / `preempted` / `budget` 与 `failed` 分开记录，避免把“这一轮时间到了”当成“任务失败”。

---

### P1：pathfinder `unhandledRejection` 分类边界

对应文件：

- [crash-guard.mjs](reference/dsh-mc-agent/src/crash-guard.mjs#L1-L90)

它解决的问题：

- `mineflayer-pathfinder` 的 `PathStopped` / `NoPath` / `GoalChanged` / `Timeout` 是正常语义，但派生 promise 链（`Promise.race`、collectBlock 内部）可能留下未处理拒绝；
- DSH 宿主把任何未处理拒绝当致命 → 整个应用闪退；
- 它只吞这四个白名单，其他错误仍然上报/崩溃。

我们的 worker 当前：

```js
process.on('uncaughtException', ... exit(1))
process.on('unhandledRejection', ... exit(1))
```

[worker.js](bot_runtime/src/worker.js#L561-L562)

这在“agent 程序/插件后台 promise 留下未处理拒绝”时会变成 hard worker exit。当前 4 轮退出里仍以 `NONFINITE` 为主，但这个改动很便宜，能减少一类假崩溃。

**建议**：在 worker 中增加明确的白名单异常处理，保留 stack/event 证据，其他异常继续 fatal；不要把 guard 写成静默吞错。

---

### P1：任务台账 / 阶段进度 / 证据式成功

对应文件：

- [task-runner.mjs](reference/dsh-mc-agent/src/task-runner.mjs#L1-L120)
- [task-report.mjs](reference/dsh-mc-agent/src/task-report.mjs)
- [插件说明.md](reference/dsh-mc-agent/插件说明.md)

它把“任务”从模型上下文搬到了磁盘：

- `taskId` / `goal` / `phase` / `steps` / `progress` / `checkpoint` / `lastError`；
- 失败分类和下一步建议；
- 每步状态、重试上限、epoch（防止异步回来后写错一代计划）；
- 成功要有证据（回读方块/背包、伤害记录等）。

**适配建议**：不要把 `mc_task` 作为新工具直接暴露给我们的 paper agent（会改变论文接口和评测口径）；可以借鉴其**数据模型/失败分类/证据断言**，用于：

- agent 程序执行记录；
- 长任务的阶段进度；
- evaluation 的“失败原因”维度；
- 恢复/重试提示。

---

### P1/P2：世界观察 / 地图 / 地形

对应文件：

- `src/world-scan.mjs`、`src/spatial-map.mjs`、`src/terrain.mjs`、`src/map-render.mjs`
- `mc_scan` / `mc_map` 工具说明

它提供的结构化感知：

- 方块状态（作物 age、矿石）；
- 地形分类、可站立点、洞穴/竖井；
- 地图/竖列图，减少“看着地图撞墙”；
- 寻路代价与地形选择。

**适配建议**：作为**内部诊断/辅助**先验证，不要直接作为 paper agent 新工具；否则会引入 paper 对齐偏差。它可能通过减少盲目 dig/pathfinder 间接降低 nonfinite。

---

### P2：测试与 Bug 档案

这是该仓库最被低估的资产：

- `selfcheck.mjs` 4,228 行，43 项自检；
- `tools/check-*.mjs` 74 个检查脚本；
- [踩坑档案.md](reference/dsh-mc-agent/踩坑档案.md) 记录了 151 个真 bug 的“现象 → 根因 → 修法”；
- 其中 #2/#3/#4/#9/#11/#36/#78/#96 与我们的 bot/worker 直接相关。

**建议**：挑与 worker 相关的用例移植成我们的 protocol/contract 测试，不搬 DSH 宿主测试：

- pathfinder 拐角/1 格台阶；
- 双移动互相 stop；
- dig timeout 后后台任务；
- y 目标在空中；
- `stop()` vs `setGoal(null)`；
- 控制位租约；
- 超时/取消的如实返回。

---

## 3. 不建议直接吸收的部分

| 能力 | 原因 |
| --- | --- |
| DSH bundle / profile / cordis patch / `@deepseek-ai/dsh-*` | 依赖 DSH 宿主；本项目 Supervisor/Pi runtime 不提供 |
| 40 个 `mc_*` 工具接口 | 会改变 paper agent 的编程接口和评测口径；只能内部借鉴实现 |
| 自主唤醒 / watchdog / `agent.steer` | 针对 DSH 单会话的“叫醒 AI”，我们的 agent turn 由 Supervisor/Pi 管理 |
| 自动吃喝/战斗/交易/钓鱼/种田等玩法层 | 与论文任务和 8-agent 稳态目标无关，增加行为面 |
| 直接升级 mineflayer 到 4.37/4.39 | 可能改变 paper 行为；先补 pathfinder 补丁和做 A/B |
| 整个 `worker-pool` / 地图渲染 | 主要解决单进程地图渲染阻塞；我们的 viewer 是独立 child，优先级低 |

---

## 4. 和当前 nonfinite 残留问题的关系

最终 4 轮（`p8-f1-02` / `p8-f2-02` / `p8-f3-01` / `p8-f4-01`）：

- 0 server kick、0 DecoderException、0 invalid movement、0 SIGABRT；
- 但仍有 **241 次 runtime restart**，退出原因主要是 `NONFINITE`；
- 当前 guard 是“发现已坏 → 30 秒观察 → hard restart”，不是“阻止并发写入导致坏状态”。

dsh-mc-agent 的经验正好补在“上游”：

1. 动作调度/移动代次，避免两个 pathfinder/控制位互相踩；
2. 显式取消，避免 timeout 后底层任务继续；
3. pathfinder vendor 补丁，避免拐角/穿模误判导致反复重算；
4. 未处理拒绝分类，避免正常 pathfinder 语义被当成 worker 致命错误。

这四个是**下一步最值得做的 P0**；它们都不需要改变 paper agent 的可见接口。

---

## 5. 建议的落地顺序

### 阶段 1：低风险、可独立回归（1–2 天）

1. 引入 pathfinder 2.4.5 两个 vendor 补丁 + postinstall 校验；
2. 在 worker 增加 `PathStopped/NoPath/GoalChanged/Timeout` 白名单，其他异常仍 fatal；
3. 在 guard 第一次检测到 nonfinite 时，不只有“等 30 秒”，还主动 `stopPathing` + 清控制位 + 暂停当前 scope 的 native 动作，再观察能否恢复。

### 阶段 2：核心修复（2–4 天）

4. 在 worker 内部实现轻量 action coordinator：
   - movement 单飞；
   - `moveGen` + `controlLease`；
   - 资源冲突排队/返回 busy；
   - 显式 `cancelNativeWork`；
5. 先 `observe-only` 跑 20 分钟诊断，记录冲突；再 `enforce` 跑 20 分钟；
6. 若 restart 显著下降，再跑 4×1h。

### 阶段 3：增强（可选）

7. 借鉴 task-runner 的失败分类/阶段进度/epoch；
8. 选做结构化世界观察（内部，不暴露 paper 工具）；
9. 移植相关回归测试。

---

## 6. 判断标准

阶段 1/2 完成后，用同一套指标判断是否值得保留：

- runtime restart / 20min（当前 4 轮平均约 60/小时）；
- `NONFINITE_BOT_STATE_OBSERVED` 次数；
- server `moved_too_quickly` / `lost_connection`；
- 0 kick / 0 SIGABRT / 0 cleanup；
- viewer 自动重连次数；
- 4 轮 wall 完成率；
- 对 paper 任务指标（不是只追求“不重启”）的影响。

---

## 7. 一句话总结

**dsh-mc-agent 不是我们的替代品，而是我们 worker 层缺失的“身体协调层”参考实现。最该吸收的不是 40 个工具，而是：动作调度器、移动代次/控制位租约、显式取消、pathfinder 补丁、异常分类和那 151 条真机 bug 档案。**
