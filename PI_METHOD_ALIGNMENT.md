# Prompt / 工具 / Harness 对齐：论文目标配置与 Pi 方法借鉴

日期：2026-10-22。对象：`four-agent-paper-20261022-final11` 之后的**下一次**正式 run；
当前已暂停的运行不重启、不改配置。

> 2026-10-22 晚更新：玩家 agent runtime 已直接采用真实 Pi coding-agent CLI（RPC 模式），
> 见第 9 节；第 1–8 节保留为设计过程、内部机制和稳定性修复记录。

## 0. 结论

论文（arXiv:2610.22319v1）公开的是**方法类别**，不是原始提示词、工具 JSON schema、压缩算法或
harness 源码。因此：

- 可以做到：模型可见的 prompt / 工具 / 环境说明按论文 §2.1–2.3 的类别和边界实现；
- 不能声称：逐字复刻作者 prompt/harness；
- 内部 harness 可以借鉴 Pi 的成熟设计，但**不能把 Pi 的工程术语、恢复规则、artifact/evidence
  机制继续暴露给玩家模型**——那会重新偏离论文的“通用 coding agent + 单程序 CLI”。

一句话目标配置：

> 四个玩家是通用 coding agent：有代码工作区、shell、文件编辑、web search、私有/团队持久记忆；
> 游戏只通过单程序 CLI；上下文过长时正常压缩；Observer 是同一 coding agent 的只读变体；
> harness 负责长程持久化、恢复、证据和预算，但对模型隐藏这些工程层。

## 1. Pi 的可借鉴点

参考（外部资料，按 2026-10-22 可访问版本）：

- Pi Agent Harness（`earendil-works/pi`）：minimal core、session tree、context reconstruction、
  compaction、branch summary、extension/skill/package。[仓库](https://github.com/earendil-works/pi)
- Pi Durable：对话/任务/文档的 durable runtime，强调“意图—执行—结果”效果三明治、崩溃后
  精确续跑、不重放不确定的副作用。
  [README](https://github.com/earendil-works/pi/tree/main/packages/durable)
- Pi 的 Code Mode / MCP 渐进披露：工具按需加载，不把所有工具说明塞进上下文；
  Code Mode 在沙箱里编排工具，只把有用结果送回模型。
  [InfoQ 报道](https://www.infoq.cn/article/hQOHf1ZXoBs7uMpWJQss)
- Piagent Context Engine / Team memory：本地 FTS5 索引、排序检索、语义压缩保留
  goal/decisions/changed files/verification/blockers；团队记忆从失败中形成 lesson。
  [Context Engine](https://piagent.io.vn/en/context-engine)
- `pi-agentos`：把角色策略（tools/deny-tools/skills/rules/checklists/cwd）作为可加载 profile，
  runtime 只做最小核心。[仓库](https://github.com/ch0udry/pi-agentos)

只借鉴**机制**，不照搬产品功能。论文没有要求 session 分支、MCP、扩展包或子 agent，因此这些不进入
玩家可见面；其中“持久 session / 上下文投影 / 压缩 / 文件跟踪 / 记忆检索 / 效果三明治”进入内部实现。

## 2. 论文公开的模型可见面

论文 §2.1：每个 agent 是同一个通用 coding agent，工具有代码工作区（版本控制）、shell、文件编辑、
web search、私有+团队持久记忆；上下文过长时像软件工作一样压缩。

论文 §2.2：指令 = 通用部分 + 简短环境说明 + 目标。
通用部分要求：行动前观察、行动后验证；新工具先在短任务上验证；手工操作受限时构建/改进自己的工具；
把机制、策略、workaround 记入记忆，避免同一个障碍遇到两次；后台任务运行时继续工作；诚实报告；
追求目标而不是利用环境/评测漏洞。
环境说明：如何用 bot 跑代码、聊天如何工作、服务端在哪里记录、Mineflayer 文档在哪里；**不含游戏策略**。
另有一句队友列表。

论文 §2.3：Observer 是同一 coding agent 的 Observer 变体；核对玩家 claim、报告 specification gaming
和伪造结果、不改变游戏、只在环境故障时介入、从不介入策略；发布 progress summary / verified events /
infrastructure findings 的 live report。

游戏接口：只能通过命令行客户端。它把一段 JavaScript 程序发给玩家自己的 Mineflayer bot，等待 agent
自选 timeout（默认 60 秒），打印程序 logs 和返回值。程序是 async function body，接收 `bot`，
pathfinding 已加载，无额外 helper；可 require Node 内置模块和已安装的 Mineflayer 包；不能起进程、
不能写文件；global 上的值跨程序保留。聊天到达每个 bot，并出现在每个玩家下一个程序的输出里。
服务端记录聊天/死亡/advancement，保存每个程序和输出，每 5 分钟存世界；失败后从最近存档恢复。

## 3. 对齐映射

| 维度 | 论文要求 | 当前差距 | 本次实现 |
| --- | --- | --- | --- |
| 玩家 prompt | 通用指令 + 环境说明 + 目标 + 队友；无策略、无 harness 细则 | v3 暴露了 artifact/history/evidence/context 恢复/超时/残留等 harness 规则 | `paper-framework-v4`：只保留论文类别；版本哈希进 manifest，prompt 正文不显示内部版本/工具恢复规则 |
| Observer prompt | §2.3 四类语义 + 三段式报告 | 混入本 harness 的只读边界、报告 schema、基础设施去重流程 | v4 只写角色语义；schema 放在工具定义，不写进 prompt |
| 工具面 | 代码工作区、shell、文件编辑、web search、私有/团队记忆；游戏只 CLI | paper 模式仍可能暴露 `mc_*`/history/artifact/events；描述含 harness 术语 | paper profile 强制最小工具集：workspace_read/write/list/remove、shell、memory_read/write/list/search、web_search/fetch；无 `mc_*`/history/artifact/events |
| 游戏 CLI | 单程序、logs+returned、聊天进下一程序输出、globals 跨程序 | 已实现，但依赖多个 flag | paper profile 自动采用 CLI/单程序/tight 接口，flag 只作 legacy 兼容 |
| 上下文压缩 | 像软件工作一样压缩；agent 被要求把 WIP/next steps 写进记忆 | 自研确定性 archive + handoff；字段偏 harness | Pi 风格 context projection：压缩条目保留 goal/WIP/next steps/decisions/verified facts/failed attempts/files read+modified/blockers/pending；保留最近 tail；原始消息仍归档 |
| 记忆 | 私有+团队持久 note | read/write/list，长会话检索成本高 | 增加 FTS5 索引 + ranked `memory_search`（owner/权限不变，只返回最新 revision 的 snippet） |
| 恢复 | 失败后从最近存档恢复；程序/输出全记录 | 恢复消息是两条泛化 user message | branch/recovery summary：明确原因、丢失的 globals、未决执行、下一步；不重放不确定副作用 |
| 证据/预算 | 服务端日志权威、完整程序记录、成本 | 已有 ledger/evaluator/budget | 保留；不把这些规则写进玩家 prompt |
| 模型/主机 | Gemini 4 Argon、各自 cloud machine | DeepSeek flash、同机容器 | 明确列为 deviation，不在本轮改变 |

## 4. Harness 内部：Pi 风格 context / memory

### 4.1 Context projection（新增 `agents/context.py`）

- 持久化 context journal（append-only JSONL）：`compaction`、`branch_summary`、`recovery` 条目；
  原始 messages 仍在 checkpoint/artifact/ledger。
- 压缩条目字段：
  - `objective`
  - `work_in_progress` / `next_steps`
  - `decisions`
  - `verified_facts` / `failed_attempts`
  - `files_read` / `files_modified`
  - `blockers`
  - `pending_executions` / `pending_jobs`
  - `world_epoch` / `runtime_epoch` / `handoff_ref`
- 压缩时模型仍可先写自己的 memory，再产出 handoff；handoff schema 增加 WIP/next steps/decisions/
  files/blockers，旧字段保留为兼容。
- tail 选择仍在 assistant 边界切，保持 tool-call/result 配对；目标约 60% context。
- 文件跟踪从工具调用提取：`workspace_read/write/remove`、`mc exec --file <path>`；不解析任意 shell。

### 4.2 Branch/recovery summary

- runner 重启、world/runtime reset、session loss 时，用结构化 summary 取代泛化 “resumed” 文本：
  reason、epoch 变化、globals 可能丢失、未决 execution/job、下一步建议、明确“旧观察需重新验证”。
- 记录为 `context_branch_summary` 事件，供 dashboard/证据审计。

### 4.3 Tool intent journal（效果三明治）

- 每次工具调用先写 `tool_intent`（tool、call_id、参数哈希、幂等类别），执行后写既有 `tool_result`。
- 重启时 `pending_tools` 只记录 `tool_effect_unknown`，**绝不重放**；读类工具可以安全重试，写/游戏类
  不可重放。这样与 Pi Durable 的“意图—执行—结果”一致，但不改变玩家可见接口。

### 4.4 Memory 检索

- `MemoryStore` 增加 FTS5（不可用时 LIKE fallback）索引，写 note 时更新；启动时按最新 revision 重建。
- `memory_search(query, scope=all|private|team, author, limit)`：
  - 私有 note 只允许 owner；团队 note 可跨作者；只返回最新 revision；返回 snippet + rank。
  - 不改变 CAS、quota、evidence_refs 字段和权限。
- paper 玩家工具面包含 `memory_search`；这是论文“持久记忆”的实现方式，不引入游戏策略。

## 5. 长程稳定性（与本轮并行修复）

| 优先级 | 问题 | 修复 |
| --- | --- | --- |
| P0 | 单个玩家 model 非重试错误终止全 run | `supervision.agent_failure_policy=isolate`：标记 `agent_degraded`，其他玩家继续；全部玩家失败才结束；`fail_run` 保留旧行为 |
| P0 | paper 模式 Observer 失败直接 `model_failed` | `observer_failure_policy=degrade`：记录 `observer_degraded`，run 继续并报告证据降级；`fail_run` 保留旧行为 |
| P1 | runner 重启加载被编辑过的新源码，与 Supervisor 不一致 | 启动时冻结 `run_dir/source-snapshot/src`，容器/process runner 都从 snapshot 加载并记录哈希 |
| P1 | Compose 里 bot/identity/launcher `restart: 'no'`，崩溃不恢复 | 改为 `restart: unless-stopped`；server 仍由 Supervisor 管理，避免自动重启与恢复存档竞态 |
| P1 | web search/fetch 不计入预算 | `budgets.max_web_calls` + `Budget` 计数/事件；超限返回 unavailable，不终止 agent |
| P1 | pause 消耗 wall budget | `Budget.pause()/resume()` 冻结 deadline；暂停时长不计入实验 wall clock（与论文“服务器重启计入 27h”区分） |
| P2 | runner RPC 同步调用无超时 | `runtime.runner_rpc_timeout_s`，RPC client 默认超时 |
| P2 | 1 MB request ceiling 小于 worst-case context | 默认 4 MB + 配置期校验 `context*3+64KiB` |
| P2 | 旧 workspace 容器残留 | 保留 Supervisor 已有的 owned cleanup；新增独立清理脚本/文档，不在本轮自动删除其他 run |

## 6. 明确不做 / 仍为偏差

- 不更换模型（Gemini 4 Argon 不可得），不模拟 5 台 cloud machine；报告继续列 deviation。
- 不引入 Pi 的 extension/package/MCP/code-mode 作为模型可见功能；只借鉴内部 session/context/memory 机制。
- 不提供游戏策略、教程、技能库或预侦察地图。
- 不把 Pi 的 session 树/分支 UI 暴露给玩家；玩家只看到压缩后的 handoff 和正常工具。
- 当前 paused 的 `final11` 不重启、不迁移 runner mode；新机制只对下一次新 run 生效。

## 7. 验收

- `paper-framework-v4` prompt 不含 `artifact`、`history_read`、`context_compacted`、`mc_pending`、
  `evidence_ref`、`leftover` 等 harness 术语；包含论文 §2.2 的通用要求和 §2.1 的 CLI/chat/记忆/存档说明。
- paper profile 的工具名集合精确等于最小集合；`mc_execute`/`mc_*`/`events_read`/`history_read`/
  `artifact_read` 不出现；游戏只能通过 `shell` 内的 `mc exec --file`。
- 压缩条目包含 WIP/next steps/files/blockers；archive 仍可恢复；tool-call 配对不被破坏。
- `memory_search` 权限/最新 revision/排序测试通过。
- 单个玩家 model 失败/崩溃后 run 继续；Observer 失败后 run 继续且 `observer_degraded=true`。
- source snapshot 在同 run 重启后哈希不变；RPC 超时、web cap、pause 冻结 wall clock 测试通过。
- 全量 Python/Node 测试通过；不启动实验、不重启 `final11`。

## 8. 实现状态（2026-10-22 晚）

已落在工作树，未提交、未启动新实验；`final11` 仍为 `reasoning_paused_world_running`，manifest
mtime 与 18:33 一致，`runner_*` 事件为 0。

| 工作包 | 文件 | 验收 |
| --- | --- | --- |
| paper 可见 prompt/工具面 v4 | `agents/prompts.py`、`agents/tools.py`、`tests/test_paper_interface.py` | 65 passed（含 paper_agents/multi_agent） |
| Pi 风格 context/记忆/工具 journal | `agents/context.py`（新）、`agents/core.py`、`agents/loop.py`、`memory/store.py`、`runner.py`、`runners.py` | 82 passed（定向）；全量通过 |
| 长程稳定性 | `supervisor.py`、`budget.py`、`runner_rpc.py`、`agents/web.py`、`deployment_generation.py`、`infra/compose*.yaml`、`tests/test_stability.py` | 62 passed（定向）；RPC 3.12 回归 23 passed |
| source snapshot + runner 预算接线 | `runners.py`、`runner.py`、`tests/test_source_snapshot.py`、`tests/test_runner_entry.py` | 77 passed（-k not container）；3 个 Docker gate 跳过 |
| **Pi RPC 玩家/Observer runtime** | `pi_runner.py`、`pi_observer_runner.py`、`pi_model_proxy.py`、`pi_tool_bridge.py`、`pi_observer_bridge.py`、`pi_security.py`、`runners.py`、`config.py`、`supervisor.py`、`pi_runtime/extensions/*.js`、`tests/test_pi_*.py`、`test_observer_rpc.py` | Pi/Observer 定向 **196 passed / 119 subtests**（memory/web、retention、Observer bridge/RPC、host e2e）；paper-runner config 已切 `runner_runtime: pi` + container |
| 全量回归（Pi 接线后） | 宿主 `.test-venv`（3.12） | **757 passed, 3 skipped, 734 subtests**；Pi/Observer 定向 196 passed / 119 subtests |

验收命令（宿主，不启动实验 / 不启动 Docker）：

```bash
cd /ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness
PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
  .test-venv/bin/python -m pytest -q          # 757 passed, 3 skipped, 734 subtests
PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
  .test-venv/bin/python -m pytest -q \
  tests/test_pi_model_proxy.py tests/test_pi_runner_manager.py \
  tests/test_pi_runner.py tests/test_pi_e2e.py   # 37 passed / 28 subtests（e2e 4 passed）
npm test --prefix bot_runtime                 # 51 passed（上一窗口）
```

已知剩余边界（下一轮正式 run 前应记录为 deviation）：

- 全量 suite 已恢复绿色：`test_pi_runner_manager.py` 的旧断言已更新为
  paper-runner=pi / whole-session=python；当前 757 passed, 3 skipped, 734 subtests。
- 模型/主机仍是 DeepSeek `deepseek-flash` + 同机容器，不是 Gemini 4 Argon + 5 台 cloud machine。
- Docker-gated runner 容器测试（3 个）本窗口仍未执行（skipped，未启动 Docker）；source snapshot
  的容器挂载由单元测试覆盖，正式 run 前应在专用窗口跑 gated 测试。
- Pi 的 container-gated e2e（Pi CLI 在 runner 容器内、`/proc` 边界、Node 是否在镜像内）本窗口
  未执行（禁止启动 Docker）；仅历史一次性 smoke + `tests/test_pi_runner_manager.py` 的
  mount/command 单元断言覆盖。`run-paper.sh --prepare` 已新增无启动镜像 probe（要求两个 Pi
  extension；检查 Node≥22、Pi CLI、两扩展、Python imports；`docker run --rm --network none`；
  Docker 不可用则报告并拒绝 `--start`），但它不替代真实 container Pi/Compose 短 smoke；
  正式 run 前必须在专用 Docker 窗口跑一次并记录 image IDs。
- **Pi `/proc` 凭据（已缓解，accepted residual）**：runner 启动设置 `PR_SET_DUMPABLE=0`，container
  runner 不再使用 `--init`，同 uid 的 Pi 子进程无法读取 `/proc/<runner>/environ` 中的 model
  key/RPC token（已实测 `PermissionError`）。Pi 子进程 env 仍保留 bridge token（只授权
  memory/web/observer 工具）与 player `MC_AGENT_TOKEN`（游戏/CLI 必需）；模型可读取自身 env
  并用 bash 直接调用 loopback bridge，从而绕过 Pi 的 `tool_intent/tool_result` 投影
  （ServiceHub 仍记录 memory/reference 事件）。该偏差 accepted，**不宣称完整工具事件审计**；
  后续可评估 bridge Unix socket/每调用 side channel 等方案。
- Pi 的 `web_search`/`web_fetch`/`memory_*` 是 extension+bridge（非 built-in）；team memory
  仍有共享 rw 文件路径，但 memory 工具写入中央 MemoryStore；`model.extra_body` 已作为 Pi
  `samplingParams` 转发；`max_turns`
  约束 continuation prompts 而非 Pi 内部 turns；“不确定动作不重放”靠 prompt+`mc` CLI 约定，
  不是 harness 强制。
- runner 模式的 Observer 没有主动 wake RPC，但 `ObserverAgent.IMPORTANT_KINDS` 已包含
  `agent_degraded` / `agent_task_crashed` / `agent_task_cancelled`；`_wait_for_review` 每
  ≤1 s 轮询 ledger，实测重要事件约 1.001 s 内唤醒下一次 review，非重要事件不会提前唤醒。
  这是证据时效的保证，不再是“按 interval 评审”的差异。
- Pi RPC runtime 见第 9 节：默认 python，`runtime.runner_runtime: pi` 才启用；paper run
  必须用 container 模式，web_search/web_fetch 与 memory_* 由 extension+bridge 提供；
  旧 `/proc` model-key/RPC-token 暴露已缓解（`PR_SET_DUMPABLE=0` + 无 `--init`）；主要
  剩余 deviation 是 Pi 子进程 env 里的 bridge token / player `MC_AGENT_TOKEN`（游戏必需）
  以及 bridge token 直调绕过 tool 账本（accepted，见 §9.4 item 7/11-12）。
- 论文未公开原始 prompt/compactor/tool schema，v4 仍是按类别独立撰写，不能声明逐字复刻。
- `final11` 是 inprocess 模式；新机制只对下一次新建 run 生效，不在原 run 上迁移 runner mode。
  `configs/minecraft-paper-runner-20261022.yaml` 已切 `runner_runtime: pi`（next run 默认），
  `minecraft-paper-whole-session.yaml` 仍是 process+python 回退；`final11` 保持不变。


## 9. Pi RPC runtime（`runtime.runner_runtime=pi`，2026-10-22 晚）

### 9.1 架构

- 玩家 agent 不再由 Python `AgentLoop/AgentCore` 驱动，而是由真实 Pi coding-agent CLI
  1.4.0（`pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js`，
  Node ≥ 22）以 `--mode rpc` 长驻子进程运行；Python 侧只剩 `pi_runner.py` 控制进程。
- `pi_runner.py`：读同一套 `HARNESS_RUNNER_*` env/spec；用 paper v4 玩家 prompt
  （`paper_aligned` 时 `paper_player_prompt`）加一段 Pi 环境附录生成 system prompt；写
  `PI_CODING_AGENT_DIR/models.json`（本地 `mcproxy` provider）、`mc` wrapper
  （`python -m minecraft_harness.program_cli`）与 session 目录；启动 Pi（cwd=自己的
  workspace，`--tools read,write,edit,bash --session-dir ... [--continue]`），把 Pi JSONL
  事件投影成 ledger 事件，并实现 `runner.ready/heartbeat/finished`。
- `pi_model_proxy.py`：每个 runner 一个 loopback OpenAI-compatible proxy（随机 127.0.0.1
  端口）。每次 model attempt 先 `budget.wait_admission` → `budget.validate_request` →
  `budget.reserve(agent, purpose=agent_decision, role=player)`，转发上游 SSE/JSON 并原样
  回传，最后 `budget.settle` 恰好一次，并写 `model_request`/`model_response` + bounded
  request/response artifact。budget 拒绝时返回 OpenAI-style 429 而不调用上游；上游凭据只
  存在 runner 进程（proxy 读取），不写 models.json、不传给 Pi 子进程。
- Supervisor / ledger（单写者 SQLite）/ budget / memory service / gateway / workspace shell
  broker / evaluator / server control 仍全部是 Python；`runner_runtime=pi` 时玩家和
  Observer 的 agent loop 都由 Pi 运行（Observer 走 `pi_observer_runner.py` + 只读
  observer tool bridge/RPC；Python ObserverAgent 只保留给 `runner_runtime=python`/
  inprocess 回退）。
- 源码冻结：Pi player 也从 `run_dir/source-snapshot/src` 加载 harness（process 用
  PYTHONPATH，container 挂载 snapshot），live 源码变更不影响重启后的 runner；Pi bundle
  本身不在 source snapshot 内（pinned `pi_runtime` tree，容器以 ro 挂载）。

### 9.2 如何选择

```yaml
runtime:
  runner_mode: container        # 论文 run 推荐；process 仅用于本机 e2e
  runner_runtime: pi            # 默认 python
  pi_cli: pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js  # 默认
  pi_context_window: 64000      # Pi 内部 compaction 的上下文窗口（tokens）
```

默认仍是 `runner_runtime: python`；`configs/minecraft-paper-runner-20261022.yaml`（下一轮
wrapper 默认）已显式切到 `runtime.runner_runtime: pi` + `runner_mode: container`，
`minecraft-paper-whole-session.yaml` 保留 process+python 回退。`final11` 保持 inprocess
且不重启/不迁移。

### 9.3 模型可见面映射

- prompt：paper v4 player prompt（目标 / general instructions / environment / teammates）
  加 Pi 附录（workspace、`/reference`、私有/团队记忆路径、游戏只能 `mc exec --file`、
  built-in tools 说明）。
- tools：Pi built-in `read`/`write`/`edit`/`bash`，加 extension `minecraft-tools.js`
  注册的 `memory_read/write/list/search` 与 `web_search/web_fetch`；游戏只通过 bash 执行
  `mc exec --file`。
- memory：`memory_*` 经 loopback tool bridge → ServiceHub → 中央 `MemoryStore`（CAS/权限/
  FTS/ledger 证据）；同时保留 file-based 私有目录 `<workspace>/memory/private` 和
  `<team_memory_root>` 供模型直接读写。Python runtime 的 memory 工具/索引不变。
- web：`web_search`/`web_fetch` 经 ServiceHub `web.search`/`web.fetch` 使用配置的 provider，
  受 `budgets.max_web_calls` 和 team-wide `reference_access` 约束；web 结果是 untrusted。
- context：Pi 自己的 session/compaction；Python context engine 只服务 python runtime。
- artifact / history / evidence 等 harness 术语不出现在 Pi 的 prompt/tools 中。

### 9.4 与 python runtime 的差异 / 下一轮 run 前要记录的 deviation

1. **Pi web/memory 工具是 extension，不是 built-in**：通过 loopback tool bridge 调
   ServiceHub；bridge token 会被 Pi bash 环境看到（只授权这六个工具），且模型可绕过 Pi
   的 tool_intent/tool_result 投影直接调用 bridge（P2-3）。
2. **Observer 现在也用 Pi**（`runner_runtime=pi`）：`pi_observer_runner.py` 使用 paper
   Observer prompt、只读 `records_read/evidence_read/observer_report/infra_issue` 工具和
   `observer.report` RPC；无 bash/mc/memory/web/team-memory/broker。仍保留 Python
   ObserverAgent 作为 `runner_runtime=python` 回退。
3. **`model.extra_body` 转发**：Pi 的 `models.json` 会把
   `spec.pi_model.extra_body`（paper config 的 `thinking: disabled`）写入
   `samplingParams`，由 Pi 合并到每次 OpenAI-compatible 请求；proxy 不另行改写上游
   body。若上游仍忽略/拒绝该字段，以 proxy artifact 中的实际请求为准。
4. **`max_turns` 语义**：Pi 的 max_turns 约束 runner 向 Pi 发送 initial/continuation prompt
   的次数（每次 `agent_settled` 后一轮），不是 Pi 内部 model/tool turn 数。
5. **不确定动作不重放靠 prompt/CLI 约定，不是 harness 强制**：`mc` CLI 在提交结果未知时
   返回 `status: unknown` + “do not resubmit” reason；`RESUME_PROMPT`/`CONTINUATION_PROMPT`
   要求先验证实际状态、不重放；但 Pi runtime 没有 Python runtime 的 pending-execution
   journal / `tool_effect_unknown` 强制机制，Pi session `--continue` 的恢复语义由 Pi 决定。
6. **process 模式不适合 Pi**：Pi bash 直接在 runner 进程所在 host/namespace 执行；process
   模式下同 uid 可读 host run_dir/ledger/其他 agent state。paper run 必须用
   `runner_mode: container`。
7. **/proc 凭据暴露已修复**：`pi_runner`/`pi_observer_runner`/Python runner 启动时设置
   Linux `PR_SET_DUMPABLE=0`；Pi 子进程（同 uid）无法读取 runner 的
   `/proc/<pid>/environ`，model key/RPC token 不再暴露。Pi 子进程 env 仍有 bridge token
   （只授权 memory/web 或 observer 工具）和 player `MC_AGENT_TOKEN`（游戏必需）；这是
   剩余的最小权限面，不再包含上游 model key/RPC token。
8. **容器 Pi e2e 仅有先前窗口的一次性 smoke，不是本窗口 gated suite**：先前窗口用真实
   `minecraft-harness-workspace:local` + `--read-only` + `--cap-drop=ALL` + host uid
   `--user` 跑通 `pi_runner`（Pi extension 加载、built-in bash 写 proof、2 次
   model_request/reserve/settle、runner.ready/finished；Node/Python/Pi CLI/扩展和
   workspace/state 挂载）。**本窗口未启动 Docker，3 个 Docker-gated 测试为 skipped**，
   不能把历史 smoke 当成本窗口验证；compose 全栈/4-agent container 长测仍未跑。
   `run-paper.sh --prepare` 的新无启动镜像 probe 只覆盖 image/Node/Pi CLI/两个 extension/
   Python imports，不能替代真实 container Pi 短 smoke——正式 run 前必须在专用 Docker
   窗口补跑一次并记录真实 image IDs。
9. **Pi session/artifact 增长未做长时间测量**：perf audit 测得约 4.63 KB/turn session、
   3 artifacts/14.9 KB/turn；带 100 KB 工具输出时 session 可达 114 KB/turn。54h/30k calls
   下需要监控 run_dir 磁盘和 session 文件，必要时在 run 前设置磁盘告警/保留策略。
10. **真实 DeepSeek/Pi 短 smoke 已通过**：一次真实 provider 调用（文本）与一次真实
    tool-call smoke（4 次 bash tool，全部 is_error=false，proof 文件写入，4 次
    reserve/settle）均成功；fake SSE 测试之外的 provider 兼容性风险降低，但仍需
    container/compose 全栈短 smoke。
11. **active session 停止不自动轮转（P2-4，accepted residual）**：达到
    `storage.pi_session_max_bytes` 的活跃 session clean abort + 事件后**不会在进程内自动
    归档/重启**；player 映射 `infra_failed`/`agent_degraded`，Observer 映射
    `observer_degraded`，run 继续但已失去该 agent，且**没有操作者告警阈值**。54h run 需
    人工监控；自动轮转/告警属后续改进。
12. **run-wide retention（P1-5 已修）**：artifact 会计已移到受信 ServiceHub：
    `<run_dir>/pi-budget.sqlite3`（0600，WAL，不挂载）保存每 agent used/metadata/retained/
    pressure，`pi.budget.take/charge_metadata/snapshot` 走身份绑定的 RPC；每个 take 在
    `BEGIN IMMEDIATE` 事务中同时检查 per-agent 与 global cap，30 并发 take 不超卖。
    Runner 侧 `ArtifactBudget` 在有 RPC 时完全不读/不写 model-writable marker；伪造/删除
    `/state/pi-artifact-budget.json` 不能重置 usage；RPC 不可用时 fail-closed，不是本地回退。

### 9.5 验收（本轮，宿主 .test-venv 3.12，无 Docker / 无外部网络）

```bash
cd /ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness
PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
  .test-venv/bin/python -m pytest -q \
  tests/test_pi_model_proxy.py tests/test_pi_runner_manager.py \
  tests/test_pi_runner.py tests/test_pi_e2e.py tests/test_pi_tools.py \
  tests/test_servicehub_web.py
# Pi/Observer 定向：196 passed, 119 subtests passed
```

`tests/test_pi_e2e.py`（新增，独立验证）：真实 `python -m minecraft_harness.pi_runner` +
真实 Pi CLI 1.4.0 + 真实 `RpcServer` + 127.0.0.1 fake OpenAI SSE upstream；验证 paper v4
prompt、built-in bash 写 proof 文件、model/tool 事件入 ledger、每次 model attempt
reserve/settle 恰好一次、ready/heartbeat/finished、SIGTERM 干净收尾、`--continue` session
续跑、observer role fail-closed、model key/RPC token 不出现在 ledger/artifact/输出/Pi
子进程 env。

## 10. 2026-10-23 鲁棒性补强（审计后）

- **Pi upstream 失败 fail-fast**：`pi_model_proxy` 记录连续 upstream 错误；4xx（auth/bad request）
  立即标记 fatal，连续错误达到 `max_model_failures` 时 `pi_runner` 结束该玩家为
  `model_failed`，不再无限 continuation。HTTP 错误响应无 completion 时 settle 零用量，
  不再把整次 reservation 当 unknown 计费。
- **pause/admission 有界**：`budget.wait_admission` 等待 30s、检测客户端断开；超时返回 503，
  不 reserve，避免暂停期间断开的请求在 resume 后仍发起模型调用。
- **no-progress 上限**：Pi continuation 使用 `agents.max_no_progress_turns`；连续无成功
  tool result 的 settled 轮次达到上限后结束为 `stalled`。
- **容器所有权**：runner 容器使用 run_dir 的 host uid:gid（`runtime.runner_container_user`
  可覆盖），配合 `--cap-drop=ALL` 仍能读写自己的 workspace/state；frozen source 统一
  0644/0755，避免 cap-drop 容器无法读取 agent 创建的 0600 文件。
- **Pi runtime 冻结哈希**：`source-snapshot.json` 增加 Pi CLI bundle、extension、package/
  lock 哈希；重启校验不一致即 fail closed，和 Python source snapshot 同一模式。
- **wrapper 启动链路与 preflight**：`--prepare` 只做无副作用 preflight；`--start` 执行
  `compose_up → wait_world → prepare_world_marker → preflight_world → wait_bot_health →
  Supervisor`。preflight 拒绝运行中的旧栈、残留 compose 网络、占用端口、已绑定世界、
  非空 run_dir、未接受 EULA、缺失 model key；不会自动 stop final11 或旧 run。Pi runtime
  还必须存在**两个** extension（`minecraft-tools.js` + `observer-tools.js`）；随后做一次
  无启动容器镜像 probe（`docker run --rm --network none`，检查 Node≥22、Pi CLI、两个
  extension、Python imports），Docker daemon 不可用或 probe 失败即拒绝启动（只报告，
  不修改/启动实验栈）。
- **审计文档**：[LONGRUN_ROBUSTNESS_AUDIT_2026-10-23.md](LONGRUN_ROBUSTNESS_AUDIT_2026-10-23.md)、
  [CHAIN_PERFORMANCE_AUDIT_2026-10-23.md](CHAIN_PERFORMANCE_AUDIT_2026-10-23.md)。

## 11. 2026-10-23 目标收口（三项 + 安全审计）

- **`/proc` 凭据暴露**：Linux runner（`pi_runner`/`pi_observer_runner`/`runner.py`）启动即
  `PR_SET_DUMPABLE=0`；容器 runner 不再使用 `--init`，hardened Python runner 是 PID 1，
  不存在持有 `HARNESS_RUNNER_TOKEN`/model key 的未加固 tini。实测 `docker run` + host uid +
  cap-drop 容器内同 uid 读 `/proc/1/environ` 得 `PermissionError`。Pi 子进程仍持有
  bridge token（只授权固定工具）与 player `MC_AGENT_TOKEN`（游戏必需），不再持有上游 key/
  RPC token。
- **Pi session/artifact retention**：`storage.pi_session_max_bytes`（默认 1 GB）在 runner
  启动时剪枝最旧完整 session，活跃 session 超限则原子归档并 fresh start（recovery prompt）；
  运行中每 ~30 s 检查，超限 clean abort + `pi_session_size_limit` 事件。共享
  `ArtifactBudget`（默认 4 GB）覆盖 model request/response、tool result、compaction；超限
  只写 metadata-only artifact（合法 ref + hash/size/head/tail），并写一次
  `pi_artifact_retention` 事件。paper-runner 配置显式 1 GB/8 GB。
- **Observer 不再固定 Python**：`runner_runtime=pi` 时 Observer 走
  `pi_observer_runner.py`：paper Observer prompt、read/write + 只读
  `records_read/evidence_read/observer_report/infra_issue`，无 bash/mc/memory/web/
  team-memory/broker；ServiceHub 提供 `observer.records_read/evidence_read/report`
  （角色/agent 双重绑定、operator 事件脱敏、artifact grant、报告 schema/证据引用校验、
  advisory-only），active 轮询 ledger important event 或 interval。
- **安全审计**：[AUDIT_2026-10-23_OBSERVER_SECURITY.md](AUDIT_2026-10-23_OBSERVER_SECURITY.md)
  的 F1–F6 全部修复：容器 PID1 env、artifact grant 所有权、viewed-event 证据绑定、报告
  key 注入、bridge 错误泄漏、infra_issue issue_id/kind。全量 **757 passed, 3 skipped,
  734 subtests**；Pi/Observer 定向 **196 passed / 119 subtests**。完整 4-agent compose
  长测仍待专用 Docker 窗口。

## 12. 2026-10-23 P1 审计收口

- **P1-1 Pi runtime hash**：`_pi_runtime_hashes()` 现在递归哈希整个 pinned `pi_runtime` 树
  （CLI shim、`cli-runtime.js`/bundle chunks、node_modules、两个 extension、package/lock），
  manifest 记录 file_count/files/sha256；启动/重启任何文件变化 fail closed。探针验证修改
  bundle chunk 会改变 sha256。
- **P1-2 mid-stream SSE**：proxy 在 stream 已开始后遇到上游错误不再写第二个 HTTP 502，
  不发送 chunked 终止符，只记录一个 `model_error` + 一次 UNKNOWN settle，无 false
  `model_response`/null artifact；正常流仍只终止一次。
- **P1-3 egress**：`runtime.runner_egress`（默认 pi+container=`model_only`）让 runner 容器
  `--network none`；Supervisor 在 host 启动 UDS `PiModelForwarder`（仅 `/v1/chat/completions`、
  capability token、provider key 只在 host/Supervisor env），PiModelProxy 通过
  `httpx.HTTPTransport(uds=...)` 访问；Pi 子进程/PID1 无 provider key。Pi bash 不再有任意
  外网；仍可访问 runner-local proxy/bridge（预算/权限仍在 host/ServiceHub 侧）。已用
  `--network none` + UDS 挂载 + DNS 阻断的短容器探针验证基础行为；完整 Docker 全栈仍需
  专用窗口。
- **P1-4 / P1-5 run-wide retention**：artifact 会计由受信 ServiceHub
  `<run_dir>/pi-budget.sqlite3` 集中控制（0600，WAL，不挂载；per-agent + `max_run_bytes`
  global cap；每个 take 是 `BEGIN IMMEDIATE` 原子事务；伪造/删除 model-writable marker 不能
  重置 usage；RPC 不可用 fail-closed）。玩家/Observer 关闭时执行 session retention 并 flush；
  90% artifact/session pressure 事件；paper 配置每 agent 6 GB artifacts + 1 GB session +
  1 GB archive，5 agents session/archive 共 10 GB + artifacts global cap < 51.5 GB run cap。
- **P2**：wrapper preflight 要求两个 Pi extension 并在 compose 前做 no-start image probe；
  docs 统一到当前 full suite **757 passed, 3 skipped, 734 subtests**（3 个 Docker-gated
  skip）；bridge/MC token、active-session 无自动轮转、完整容器 smoke 未执行均明确记录为
  residual。

## 13. 2026-10-23 真实烟测 + Pi 本地凭据隔离（residual #2）

- **Bridge token**：不再进入 Pi 环境；`pi_runner`/`pi_observer_runner` 通过 pipe FD +
  `pass_fds` 传一次，extension 加载时读取并关闭 FD，deletes `HARNESS_PI_BRIDGE_TOKEN_FD`。
- **本地 model proxy**：Pi CLI models.json 的 provider apiKey 是随机 per-run loopback
  credential（不是上游 key）；PiModelProxy 对 `/v1/chat/completions` 和 `/v1/models` 校验
  Bearer，缺失/错误 → 401 且不 reserve/不调用上游；首个认证请求后删除 models.json 和 auth
  store，记录 `pi_cli_auth_redacted`；真实 Pi 不会 per-call 重读该文件。
- **MC token**：`MC_AGENT_TOKEN`/`MC_SUBMISSION_AUTHORITY`/secret 命名的 CLI env 不再进入
  Pi env；写入 `<state>/bin/mc-env`（0600），只有 `mc` wrapper source 到 program_cli 子进程。
  非秘密 `MC_RUNTIME_URL/SOCKET/AGENT_ID` 保留。
- **真实 smoke**（见 [REAL_SMOKE_2026-10-23.md](REAL_SMOKE_2026-10-23.md)）：
  - host process：DeepSeek + Pi，**15 model requests / 15 responses / 15 tools**，status
    completed；env 无任何 token/key；models.json 已删；mc-env 0600；run dir 无 API key。
  - container：`runner_mode=container` + `model_only` + `--network none` + host UDS
    forwarder，**13 model requests / 13 responses / 15 tools**，status completed；
    manifest `provider_credential_in_runner=false`、`runner_pid=1`；无 token/key。
- **残余（同 uid 边界）**：Linux 在 execve 重置 dumpable，Pi child `/proc/<pid>/environ`
  仍可读，但其中只有路径/非秘密 routing 和已关闭 FD 编号；`mc-env` 同 uid 可读（mc 必需，
  own-game scope）；loopback proxy/bridge 仍网络可达，但没有上述随机 credential 时 401。


## 14. 2026-10-23 /proc 与 loopback 可达性收口

- **Pi `/proc/<pid>/environ`**：Linux 在 `execve` 重置 dumpable，pre-exec prctl 无效。新增
  `pi_runtime/prctl_guard.c`（编译为 `prctl_guard.so`）作为 LD_PRELOAD constructor，在 Pi
  exec 后设置 `PR_SET_DUMPABLE=0`；player/observer Pi 子进程及其 bash/tool 后代均不可被
  同 uid 读取 `/proc/<Pi>/environ`。实测模型 bash `cat /proc/$pi_pid/environ` 得到
  `Permission denied`；真实 smoke 的 proc-env.txt 记录 `PROC_DENIED`。
- **Loopback proxy/bridge**：guard 同时 interpose `connect(2)`，通过继承的 `FD_CLOEXEC` pipe
  把 `(pid, source_port)` 报给 runner 的 `LoopbackPeerRegistry`；PiModelProxy /
  PiToolBridge / PiObserverBridge 只接受 `allowed_peer_pid == Pi PID` 的连接，其他连接在
  解析 HTTP 前关闭（没有 401/403 响应）。bash 即使拿到 loopback URL 也只能得到
  `BRIDGE_REPLY b''`（连接被关闭），不能直接调用 memory/web/bridge 或本地模型代理。
- **动态多 agent smoke**：真实 DeepSeek + Pi：
  - host N=1：bash 检查得到 `PROC_DENIED`、bridge 返回空/关闭；env 无 token/key。
  - container N=2（`model_only` + `--network none` + UDS forwarder）：两名 agent 均为
    `PROC_DENIED`、bridge `b''`，无 token/key，24 model request/response、30 tools，
    runner containers 清理干净。证明动态 N=2 exact-N 路径可用，未跑完整 Minecraft 世界。
- `.so` 是 x86_64 glibc 构建产物，容器挂载整棵 pi_runtime；`RunnerManager` 与
  `run-paper.sh` 现在会在 Linux 上 fail closed 如果 guard 缺失。


## 15. 2026-10-23 8-agent paper-scale 性能烟测

- 论文协议固定 4 玩家；为容量测试新增显式 `experiment.paper_scale_override: true`
  （generator `--mode paper --allow-paper-scale`），保留 paper prompt/tool/lifecycle
  语义，但不是论文 replication 声明。
- 8 players + Observer、真实 Minecraft/DeepSeek 5 分钟：8/8 runtime_ready，263/262
  model calls，273/266 tools，Observer reports 2，0 infrastructure_error；status
  budget_exhausted（wall）。
- 链路：model p50 1.64s / p95 5.83s；tool p50 207ms / p95 30.67s；bot health
  p50 1.60ms；supervisor RSS ~310MiB；容器 CPU peak ~193%；run dir +68.7MB/5min。
  详见 [EIGHT_AGENT_PAPER_PERF_2026-10-23.md](EIGHT_AGENT_PAPER_PERF_2026-10-23.md)。


## 16. 2026-10-23 修复与性能优化

- heartbeat watcher 在 agent finished 后退出；budget shutdown 清空在途 pending。
- Pi model proxy 复用 httpx.Client/连接池；server 增加 spigot 移动容忍配置。
- 8-agent 2 分钟复测 0 heartbeat timeout / 0 bot kick / pending 空；全量
  Python 757 passed / 3 skipped。
