# 论文方法对齐与验证

日期：2026-10-21。分支：`harness_embodiment_v1`。依据：[A team of Gemini 4 Argon agents beats Minecraft，v1](https://arxiv.org/pdf/2610.22319v1)。本文是对公开方法的独立实现说明。

## 本轮实现

提供 `experiment.profile: strict | paper_aligned`。旧配置使用 strict；运行的原配置继续冻结，恢复不会默默切换模式。paper_aligned 使用 v3 执行协议、显式后台任务、独立 Observer 和受限编程容器。FakeModel 或 fake 游戏始终标记 diagnostic，不产生能力成功结论。

| 机制 | 实现与入口 | 验收证据 |
| --- | --- | --- |
| 实际世界与试验身份 | `environment.py` 解析有界 NBT，校验实际 seed、版本、生存/Easy、权限、空玩家进度及世界 origin/run 绑定；`deployment.py` 核验实际镜像、网络和代理配置；`prepare-trials` 只准备新目录，不接受 EULA | `tests/test_environment.py`、`tests/test_deployment.py`、`tests/test_paper_infrastructure.py` |
| 跨程序任务 | `jobs.start(fn,{ttl_ms}) / status / cancel / list`，最多 8 个活跃任务；期限不超过剩余 run，取消有 1 秒协作窗口，无法确认则重建 worker | Node paper 生命周期测试、`validation/2026-10-21-paper-lifecycle-soak.json` |
| 异步与 CLI | `mc_submit / mc_wait` 与同步 `mc_execute` 共用提交账本；shell 中 `mc` 通过带玩家认证的 Unix broker，不需要工作区联网 | `tests/test_paper_runtime.py`、`scripts/workspace_smoke.py` |
| 独立代码工作区 | 每人独立容器、Git、rg、Python、Node、锁定的本地参考文档；只挂载自身 workspace、只读文档和玩家 socket；Observer 无玩家 socket | `workspaces.py`、真实 Docker workspace smoke |
| 任务连续性 | 共用 AgentCore；paper 模式在上下文阈值前进行有预算的交接；原始历史、产物和未决任务可分页读取，失败摘要明确不完整 | `tests/test_paper_agents.py`、四玩家与 Observer 联调 |
| 独立 Observer | 同 AgentCore/模型配置，独立上下文和工作区；周期与故障唤醒、证据引用、修复请求去重；Supervisor 独立探测后才允许恢复 | `tests/test_paper_supervisor.py` |
| 执行权限 | 控制面、launcher、生成代码分离；独立 PID/容器、只读文件系统、原生 seccomp 拒绝写文件及创建进程；无宿主网桥地址的 isolated 前端只连接 worker 和固定身份代理 | `infra/test_sandbox.py`、`validation/2026-10-21-remote-worker.json`、`validation/2026-10-21-network-boundary.json` |
| 通信消融 | 在身份代理协议层阻断双向聊天包，包含直接访问 `_client` 的旁路；共享记忆继续按作者授权 | `bot_runtime/test/identity-proxy.test.js`、隔离协议联调 |
| 证据与费用 | 原始日志分块、轮转尾部、缺口和午夜时间；成功前/全会话窗口、玩家活动、死因、阶段与代码/记忆分析；缓存/推理 token、角色及用途分账 | `tests/test_paper_evidence.py`、`validation/paper-evidence-scale.json` |
| 操作者观察 | 本地仪表板展示时间线、程序、回执、证据、预算和 Observer；带本地操作者 token 的控制请求仍进入持久队列 | `dashboard.py`、真实 HTTP 边界测试 |

## 正文逐节追踪

| 论文位置 | 对应要求与解释 | 本项目处理 |
| --- | --- | --- |
| §1，p2 | 对等团队长期编程协作 | 不设中央游戏规划器；人数仍支持 1/2/4，paper 配置为四人 |
| §2，p2–4 | 文本程序接口、原生游戏状态与寻路 | 保留 Mineflayer/pathfinder；不添加人写的游戏技能，不把代码感知包装成像素控制 |
| §2.1，p3–4 | 通用工作区、工具、记忆和长驻代码 | 编程容器、版本控制、搜索适配、globals 与显式 jobs；通用模块权限有版本化清单 |
| §2.2，p4 | 观察、验证、短任务试验、经验与诚实报告 | `agents/prompts.py` 将通用原则、环境说明、目标/队友分开；提示词和 schema 哈希进 manifest |
| §2.3，p4 | 独立观察和基础设施问题处理 | LLM Observer；报告不能判成功，不能直接操作游戏或提高预算 |
| §3，p4–12 | 运行过程可重建，时间包含基础设施停顿 | 以首个实际程序开始为实验时钟；停服时间单列；事件和阶段保留证据位置 |
| §4，p12–13 | 分工、工具、记忆、救援和纠错是观察结果 | 可分析消息与复用，不能将论文中的策略代码装给玩家；模式匹配不等于因果证明 |
| §5，p14 | 完整程序/输出、消息和服务端记录用于核验 | 不以玩家返回值评分；原始日志有哈希和字节引用，输出截断/缺失显式标记 |
| §6，p14 | 玩家/Observer 与成功前/全会话费用有不同口径 | usage-v2 区分缓存、推理、角色和用途；不重复加总子集，不猜测未知供应商字段 |
| §7，p14 | 区别于人写游戏工具、课程和技能库 | 不引入相关文章的攻略或技能作为默认玩家能力；离线测试夹具与玩家工具分开 |
| §8，p15 | 感知、难度、先验、故障与单次运行的限制 | 报告保留模型、世界、规则、依赖和偏差；工程通过不证明通关成功率 |
| §9，p15 | 自身角色、权限边界与实时监督 | 生成代码无管理凭据/世界卷；身份代理限制用户名；操作者可暂停或停止 |

## 图表与代码逐项追踪

| 项目 | 支持的记录或分析 | 约束 |
| --- | --- | --- |
| 图 1、4：阶段时间线 | 服务端 advancement 与记录时间线 | 没有证据的阶段为未知，不补出全部阶段；论文图 4 的 13 步手工 taxonomy/首达玩家尚未专门实现（见审计 §3.3） |
| 图 2：系统结构 | 玩家、模型、工具、执行、世界、记忆、Observer 的权限与数据流 | 部署使用本地隔离容器，不宣称原始云机器配置相同 |
| 图 3：完整运行 | 按玩家的程序、消息、死亡、重启与耗时窗口 | 客户端广播副本不当成多条发送消息 |
| 图 5：团队形成 | 带发送者、来源时间和证据位置的聊天 | 角色由玩家协商，不从示例预置 |
| 图 6：经验修正 | 聊天多标签、记忆版本与引用 | “经验”分类是版本化分析规则 |
| 图 7、p9 代码：定位 | 原生实体状态、计时与自写 JS 能力保留 | 不提供三角定位函数或预侦察地图 |
| 图 8：救援 | 消息、死亡及来源时间可联合查询 | 不自动下发救援计划或推断未观测物品转移 |
| 图 9：世界结构 | 原始证据与只读存档引用 | 已实现只读 160×160 textured 3D mesh（以及 full-world LOD/top/iso）；不影响程序行动闭环 |
| 图 10、11：失败后的策略 | 记忆与自建源码可持续修改 | 不硬编码末地规则或水晶策略 |
| 图 12：实体/战斗曲线 | Agent 记录的状态作为其观测保留 | 不伪装成独立服务端真值，不要求输出未采集的 HP/水晶曲线 |
| 图 13：消息与死因 | 多标签消息分析、服务端死因分类、成功前窗口 | 分类规则由本项目定义，论文未提供原始分类程序 |
| 图 14：纠正错误声明 | Observer verified/claim/insufficient/contradicted 与记录引用 | LLM 判断为辅助报告，不覆盖确定性评分 |

## 明确的工程选择与未知项

- 没有获得原作者完整 harness、完整提示词、压缩算法、取消协议或全部统计程序；本项目对应部分是重新设计。
- 作者分区共享笔记继续采用作者写、队友读；论文没有要求共同写同一文件。
- `runtime.leftover_policy=allow` 时 bot runtime 不再在程序返回/超时时清理 timer/listener/action 或重建 worker；论文 §4 的残留代码、停止计数器和 globals 可在跨程序保留。strict 默认仍是安全重置；paper compose 已设置 `LEFTOVER_POLICY=allow`。循环型 CPU 死循环（包括 await 后）由 loop-deadline instrumentation 中断并保留 globals；非循环超长 CPU 仍有 abort grace 兜底。
- 超时程序在 allow 模式下被 detach：supervisor 记录 `timed_out`，旧代码继续在同一 worker 中运行，下一程序可继续执行；globals 不丢失。strict 模式仍保持旧的安全重置语义。
- paper 模板现设置 `agents.paper_single_program: true`，模型 schema 只暴露 `mc_execute`，不再暴露 mc_submit/mc_wait/mc_jobs/mc_job_cancel/mc_status/mc_cancel/mc_pending；显式 jobs API 仅作为非 paper profile 的能力保留。论文的“单程序 CLI”接口因此成为 paper 默认。
- 外部搜索通过操作者配置的服务提供；未配置时 manifest 明示 `web_search_unconfigured`。游戏执行网络只接身份代理。
- 实际世界身份和 seed 保留在操作者侧；Observer 只看到脱敏记录，不能由该记录取得私有产物。新 run、trial 和世界数据目录默认仅操作者可访问，已有目录权限由操作者维护。
- 原生模块白名单不是安全边界。可信 launcher 持有 Docker 管理权限；它不运行玩家代码，其管理接口不能对外公开。
- 聊天关闭仍允许物品、建筑和移动等游戏内物理协作，不能称为“完全无通信”。
- 同一 seed 不保证模型和多玩家调度轨迹相同；每个 trial 独立世界、记忆和工作区，预算范围明确为每个 trial。

## 当前验证边界

本轮已运行真实 Docker worker、真实编码工作区和真实 Mineflayer 协议栈；游戏服务器使用标明为 synthetic 的协议夹具，未启动 Vanilla、未调用付费模型。完整测试和较长运行以 `validation/` 原始 JSON、日志及源码哈希为准。

冻结源码的一小时运行完成了 12,992 次调用尝试与三次故障恢复，但原仪器脚本超过 45 秒收尾期限，原始结果仍为 failed。完整账本对照确认内存追踪使收尾耗时从约 5.05 秒增至 40.88 秒；修正仪器后，同冻结应用代码的收尾补测通过，未重跑整小时。各层结果分别见 [最终验收摘要](validation/2026-10-21-paper-final-summary.json)。

宿主 Mihomo 的本地地址集合未自动包含新建 Docker 网段。`scripts/proxy_network_probe.py` 已用“原先失败 → 两个自建地址临时豁免后成功 → 撤销后重新失败”确认原因。完整隔离协议试验也只临时加入本次自建 peer 地址，全部撤销。正式部署仍需管理员让代理正确识别实验网络；本项目不持久修改宿主代理策略。

Vanilla 的真实短任务、小时/日尺度运行和真实模型费用均等待相应环境条件。Minecraft EULA 尚未获得明确接受；预置试验目录与构建镜像不会接受 EULA。

## 2026-10-21 修复复核

本轮把真实模型测试暴露的问题与论文细节重新对齐，代码和回归已更新：

| 修订 | 内容 | 位置与证据 |
| --- | --- | --- |
| 提示词 v2 | 玩家补齐论文 §2.2 的通用要求：短任务先验证新工具、构建并复用工具、把机制/坐标/策略/workaround 写进记忆、诚实报告、不利用环境或评测漏洞；环境说明补充代码接口、聊天投递、服务端日志以及 `/workspace`、`/reference`。Observer 对齐 §2.3：逐条核对玩家主张、报告 specification gaming 与伪造结果、只在环境故障时介入，每轮产出 `observer_report` **findings**（论文要求的进度摘要/已核实事件/基础设施发现尚未结构化，见审计 §3.1）。 | `src/minecraft_harness/agents/prompts.py`（版本 `paper-framework-v2`） |
| Observer 节奏 | 自身 LLM 记账事件不计入证据；一次评审有界合并多个证据页；评审间隔为硬下限；证据预算按 Observer 上下文自适应，避免压缩抖动。修复前单玩家 380 秒触发 190 次调用（占 72% 成本、0 条报告）；修复后 8 分钟 25 次调用、11 条报告。 | `src/minecraft_harness/observer/agent.py`、`tests/test_observer_operator_boundary.py` |
| 报告兜底 | 有新证据但本轮未写报告时，补报调用使用强制 `tool_choice` 指定 `observer_report`；模型客户端与 AgentCore 增加可选 `tool_choice` 透传。 | `observer/agent.py`、`models/client.py`、`agents/core.py`、`tests/test_models.py` |
| 未知用量 | 未知 token/费用按 admission 时最坏预留计入保守成本并标记 unknown，不再冻结后续付费调用；长程网络失败或停机取消不会终止实验。 | `budget.py`、`tests/test_budget_longrun.py`、`tests/test_core.py`、`tests/test_paper_evidence.py` |
| 配置与状态 | 启动前校验 `model.max_request_bytes` ≥ players/observer `context_chars`；`max_turns` 结束记为 `turns_exhausted`；`max_input_tokens` 的字节/token 单位修正。 | `config.py`、`agents/loop.py`、`supervisor.py`、`tests/test_longrun_config.py` |
| 收尾竞态 | 操作员停止、墙钟上限或成功收尾时，Observer 因 `budget.stop_requested()` 自然退出不再被误判为 unexpected（此前会让 paper_aligned 运行以 `infra_failed` 结束）；`agent_outcomes` 对被取消任务记录异常类型而不是空字符串。 | `supervisor.py`；`paper_lifecycle_soak` 连续 25 次通过 |
| 论文测量参数 | paper 模板 `context_chars` 调至 160000 并注明依据（论文玩家平均每次调用约 56k 输入 token）；Observer 与玩家共享同一模型/上下文口径（论文同为 coding agent），可用 `observer.context_chars` 单独放大。 | `configs/minecraft-paper-aligned.yaml` |
| 真实 Minecraft 单 Agent 验证 | EULA 接受后在本机 Mojang 1.20.4/Easy 服务端用真实 Mineflayer + DeepSeek 跑通 `obtain_logs`（stats 原木 delta=3）与 `craft_table`（stats 工作台 delta=6），报告 token/费用在修复收尾竞态后为已知值；4 Agent paper_aligned 与日尺度未验证。 | `validation/2026-10-21-real-minecraft/` |

验证：仓库内 Python **347 项通过**、Node **51 项通过**；`--network none` 离线 smoke 独立运行通过；真实 DeepSeek 1 Agent + LLM Observer 8 分钟运行 106 次调用、11 条报告、成本约 $0.24，未知用量发生后继续到墙钟上限；加入强制补报与补报重试后的 4 分钟复核为 66 次调用、10 条报告、2 次补报失败、成本约 $0.13。真实 Minecraft 1 Agent（strict，真实服务器 stats 证据）已在上表通过；4 Agent paper_aligned、付费模型能力实验与日尺度运行仍未验收；原冻结一小时 failed 结论保持不变。

## 2026-10-21 二次深入审计

按 arXiv 2610.22319v1 全文逐节复核后，发现的 P0/P1 差异、未实现项和超出论文边界的能力，已单独整理为
[PAPER_AUDIT_2026-10-21.md](PAPER_AUDIT_2026-10-21.md)。要点：

- **P0（会改变实验语义）**：① 论文允许 timed-out 程序的残留代码继续运行（§4，1,519 programs 使用停止
  计数器；146 个跨程序存活状态），本框架在程序返回/超时后清理并重建 worker、丢失 globals；② 本框架
  paper_aligned 提供 `mc_submit/mc_wait/mc_jobs/jobs.start` 并发能力，而论文玩家接口是单程序 CLI。
- **P1（缺失/不完整）**：Observer 报告只有 findings（论文要求 progress summary + verified events +
  infrastructure findings）；无 in-game days（论文 ~81 天）；图 4 的 13 步表、图 13 的行为分类未按论文
  taxonomy 实现；获胜后全会话默认不采集（`stop_on_success: true`）；web search 默认关闭；程序输出与
  Node 内置模块范围比论文更严；每个 agent 独立云机器未实现；程序超时上限 180s。
- **➕ 超出论文边界**：确定性 evaluator/证据封存、预算与成本上限、worker reset/watchdog、原生沙箱/
  身份代理、checkpoint/rollback、额外玩家工具与自动快照、实时第一视角直播、多任务/多 trial 等。其中
  观察性功能可保留，但改变 agent 可见信息/调度的项不应算作论文方法。

下一步修复建议与优先级见审计文档 §6；P0 需要先决定是"实现论文残留代码兼容模式"还是"接受严格化差异
并在对外表述中降级"。

## 2026-10-21 论文图表动态可视化

观测台新增「论文图表」页，按论文图 1/3/4/5/6/8/12/13/14 的口径重建本 run 的数据：

- **已实现/有数据**：Fig.3 运行时间线（玩家泳道、聊天/成就/死亡、程序柱状图、维度底色、服务器重启与
  runtime reset 标记）；Fig.1/4 十三步进度（混合 advancement、world statistics、服务端日志；快照派生的
  时间明确标为观测上界）；Fig.5/6/8/14 聊天回放（含证据跳转）；Fig.13 行为与死因（行为为确定性正则近似）。
- **不完整/缺失**：Fig.12 末地战斗需要玩家程序的 dragon health/crystals 观测通道（当前空状态）；13 步中的
  "Ten crystals" 没有 advancement/统计对应，固定 unavailable；in-game days（论文 ~81 天）需要 level.dat
  DayTime；论文的完整 3D 世界渲染只做到 160×160 mesh/full-world LOD，不是全精度全景。
- 数据来源与边界见 [DASHBOARD.md](DASHBOARD.md#论文图表的动态回放)；回归测试 `tests/test_figures.py`，
  Python 347 项 / Node 51 项通过。

## 2026-10-21 二次复核：仍未对齐的论文设计

对论文全文、当前代码、配置和真实 run 证据做了第二轮逐项复核。完整 delta 报告在
`runs/_analysis/paper_delta_2026-10-21.md`；下面是需要带进实验记录/论文表述的结论。

### P0：会阻止"精确复现"声明

1. **残留代码/超时语义**：论文 §4 依赖 timed-out 程序留下的跨程序代码、计数器和停止条件（1,519 个
   program 使用 stop counter，146 个跨程序状态存活）；当前实现程序返回/超时后清理 timer/listener/action，
   无法确认旧代码停止时重建 worker 并丢失 globals。这对应用户可选择的"严格安全"再设计，但没有
   leftover-code 兼容开关。
2. **异步工具集默认开启**：`paper_aligned` 无条件启用 `mc_submit/mc_wait/mc_jobs/jobs.start`、额外
   `mc_status/mc_cancel/mc_pending`、`events_read` 及异步 jobs；paper_single_program 已从 schema 移除这些，`history_read/artifact_read` 仍用于上下文交接；论文 §2.1 的玩家接口是一个
   同步 CLI 程序，打印 logged + returned。没有关闭这些能力的 profile/config。

### P1：实验设计/指标差异

- **成功时间**：当前以 world advancement/stats 快照作为成功上界（最多晚一个 save interval），不是论文的
  服务端日志 `[Free the End]` 行时间；最终报告应同时给出服务端日志佐证。
- **信息通道**：function-call 工具返回结构化 metadata，并在程序前后自动采集快照；论文程序只返回日志/
  返回值；聊天现在可按 `collaboration.chat_delivery: program_output` 进入下一个程序输出，仍保留 durable event 证据。
- **程序限制**：128 条日志/256 KiB 日志/256 KiB 返回值、16k 字符工具结果、Node 内置模块白名单（10 个）
  与 4 个游戏包均比论文更窄；`program_timeout_max_s` 已从 180 提升到 600 并同步 compose bot
  `MAX_TIMEOUT_MS`，但仍不等于论文的"由 agent 选择"。
- **Observer**：`observer_report` 现支持 progress_summary、verified_events、infrastructure_findings，保留 findings；基础设施 issue 现支持 broken_tool/quota_ceiling；report 输出新增 `observer_review` 汇总，Markdown 中包含最新报告。
- **主机/部署**：模型已明确使用 DeepSeek `deepseek-flash`（不是论文的 Gemini 4 Argon，这是本项目选择而非待修 bug）；五个 agent loop 在同一 Supervisor 进程，不是各自云机器；使用自研 handoff compaction。
- **全会话窗口**：默认 paper 模板仍是预胜 36h；新增 [minecraft-paper-whole-session.yaml](configs/minecraft-paper-whole-session.yaml)（`stop_on_success: false` + 54h wall + 30k calls）用于采集胜利后 day-after 和两种费用口径。
- **框架安全截断**：`max_no_progress_turns=120`（论文未描述 stall cutoff；30 对长程过紧）、`max_turns`、max_model/event_failures、Observer 必需性、
  budget 上限、checkpoint/pause/stop 都不是论文描述的实验设计；其中一部分为长程稳定性所必需，但必须在
  方法/附录声明。
- **其他**：web_search 默认关闭；in-game days 现从最新 server save 的 level.dat 读取；服务端 `[Free the End]` 日志行和 server-command 行进入最终 report（`success_log`/`server_command_audit`）；Fig.12 末地战斗/水晶通道仍缺；真实 run 的 restart 标记混合 server restart 与 runtime reset；13-step taxonomy/Fig.13 分类仍是近似。

### 已完成的工程加固（不改变上述语义差）

- Observer 现在对可重试的 transient model error 做 bounded retry（`observer/agent.py:_review_model_call`），
  不再因一次 5xx/timeout 终止 paper_aligned run；malformed JSON/无效 tool-calling response 在 model client
  标记为 retryable；HTTP 408/409/425/429/5xx 可重试。
- `_recover_runtime` 在重启 managed server 前先 drain `latest.log` 尾部，减少 server restart 导致的
  `server_log_gap`/权威死亡与聊天计数缺失。
- paper/longrun 模板增加 20 次连续可重试模型失败、180s 模型超时、60 次 recovery、32/48GiB 产物/run
  配额、2GiB 磁盘保留；paper bot compose 的 `MAX_TIMEOUT_MS` 与 600s 模板同步。
- 观测台 `/api/figures` 增加 10s 服务端 TTL + 前端 15s 节流，避免长账本下重复扫描服务端日志/快照。

### 决策点

在任何付费/长程 paper run 前需要明确：**A）实现 paper-mode 的 leftover-code 兼容、单程序同步工具集、
Observer schema/报告、成功时间日志佐证等，然后声明更接近复现；B）保持当前严格化实现，在报告/论文中
逐项列出上表差异并降级"复现"表述。** 当前代码没有默认宣称精确复现。

### 2026-10-21 深夜修复结果（覆盖上述历史缺口）

已实现并通过 347 项 Python + 51 项 Node 测试：

- `runtime.leftover_policy: allow` + bot `LEFTOVER_POLICY=allow`：正常返回和超时都保留 globals/timers/listeners/异步动作；超时执行被 detach，下一程序可运行。循环型 CPU 死循环由 loop instrumentation 在下一迭代中断并保留 globals；非循环超长 CPU 仍有 fallback reset。
- `agents.paper_single_program: true`：paper 模板只暴露同步 `mc_execute`，async submit/wait/jobs/status/cancel/pending/events_read 全部从 schema 隐藏。
- `collaboration.chat_delivery: program_output`：paper bot 把未读聊天 prepend 到下一程序日志；AgentLoop 不再把 chat 作为独立 user event 注入。
- `paper_tight_interface: true`：从模型 schema 隐藏 `history_read/artifact_read`，并从 `mc_execute` 工具结果剥离自动 before/after 快照；服务端证据仍完整保存。上下文目标从 160k 提升到 224k chars（约 56k tokens，按 4 chars/token 估算）。
- Node 模块面扩大：paper 模式允许只读 fs/os/stream/timers 等内置模块和 prismarine/minecraft-protocol 包；bot 侧增加只读 `fs` Proxy（写操作返回 EPERM），真正的边界仍是远程 native sandbox/只读容器。输出上限提高到 16k 条日志 / 16 MiB 日志 / 8 MiB result。
- CPU 死循环：worker 对程序做 **loop-deadline instrumentation**（vendored acorn 8，MIT）：在 while/for/do/for-of/for-in 循环体插入 `__mcWindowCheck()`；`await` 之后的 CPU 循环在下一次迭代检查截止时间并抛可捕获的 `ERR_EXECUTION_DEADLINE`，保留 globals/timers，不 reset worker。同步运行仍叠加 V8 vm timeout。Node 测试覆盖 `await fake.wait(); globals.marker="kept"; while(true){}` → timed_out 且下一程序读到 marker、无 runtime_reset。非循环的单个超长 CPU 计算仍只能由 supervisor abort grace 兜底；这是有限的残余边界。
- 13 步顺序修正为论文 Figure 4 的 Ender pearls 在 Blaze rods 之前；`ten_crystals` 可由玩家程序的 `crystals:0` 观测命中；Fig.13 使用论文的五个消息类别（Warnings/Rescues/Shared chests/Lessons/Exact coordinates）与六组死因（Monsters/The dragon/Drowning/Falls and the void/Lava and fire/Other）。
- 新增只读存档 3D 渲染：`world_mesh.py` 从 Anvil 数据构建 voxel 体（**160×160×64**），仅发出可见面，透视相机 + Pixel Perfection 16×16 材质采样 + **real per-pixel z-buffer**（替换 painter 深度排序），生成 1024×640 的 textured 3D mesh PNG；另有 full-world LOD 模式和 top/iso 兜底。Dashboard「存档世界 · 地图」默认 160×160 mesh，`/api/world-image?run=…&style=mesh` 按 world fingerprint 缓存，并带单飞锁。
- Sandbox：**gVisor `runsc` 已安装并配置到 Docker**（`/usr/local/bin/runsc`，`/etc/docker/daemon.json` 注册 runtimes，live-restore 开启），paper launcher 默认 `WORKER_SANDBOX_RUNTIME=runsc`；worker 镜像在 gVisor 下 smoke 通过。模块白名单和只读 fs Proxy 仍只是纵深防御，真正边界是 gVisor/只读容器/seccomp/网络隔离。见 `infra/SANDBOX.md`。
- 模型：paper/whole-session 模板默认已改为 DeepSeek `deepseek-flash`（`https://api.deepseek.com`，`DEEPSEEK_API_KEY`，thinking disabled，价格按 0.30/0.006/1.20 估算），不再使用 Gemini 占位符。
- Observer: 报告增加 progress summary/verified events/infrastructure findings；新增 broken_tool/quota_ceiling issue kind；最终 report 含 observer_review。
- 数据项：最新 server save 的 `level.dat` 产生 `in_game_days`；report 增加 `success_log`（[Free the End] 服务端日志行，仍以 advancement 快照为成功权威）和 `server_command_audit`。
- 长程：`recover_session_loss: true` 允许 bot runtime 重启后重建 session 并记录显式 `runtime_session_lost`；recovery 窗口提高到 8 次/约 6 分钟；pre-restart log drain；paper bot 输出上限提升到 4096 logs / 4MiB logs / 2MiB result。
- 全会话模板：`configs/minecraft-paper-whole-session.yaml`。
- 仍明确不声称：paper-tight 已在 paper 模板隐藏 history/artifact/自动快照；Node 模块/输出仍比论文环境更窄；模型明确使用 DeepSeek deepseek-flash（本项目选择，不是论文 Gemini）；五个 loop 在同一 Supervisor 进程；13-step/Fig.13 是论文图重建的确定性规则；Fig.12 是 player-observed 非服务端战斗真值。

**这些新语义必须先在 1–2 小时真实/fake staging 中验证，尤其是 loop-deadline instrumentation、CPU/session-loss recovery、managed server restart、paper-tight 接口和扩大后的 Node 输出。**

仍明确不声称：完整世界 1:1 全精度 mesh（默认 160×160；full-world 是 LOD）；DeepSeek 与 Gemini 4 Argon 完全同模型；上下文 token 与论文完全一致；非循环超长 CPU 仍会 reset；13-step/Fig.13 是论文图重建的确定性规则，不是作者原程序。
