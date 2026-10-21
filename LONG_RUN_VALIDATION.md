# 长程稳定性验证记录

日期：2026-10-21。分支：`harness_embodiment_v1`。

本文前半部分保留第一轮长程加固的历史记录。当前新增能力、复验结果及宿主网络原因见文末 [paper alignment 2026-10-21](#paper-alignment-2026-10-21)。历史的 175/34 项、7 条告警和容器 blocked 不代表本轮最新状态。

本轮验证检查长程运行所需的恢复、资源约束与审计机制。真实 Vanilla 服务端与真实付费模型尚未联调；离线结果只建立机制正确性的证据，不建立 Minecraft 通关能力或 24 小时真实环境运行记录。

## 已覆盖的故障

| 故障或规模 | 期望行为与验证方式 |
| --- | --- |
| 普通重启、冻结/恢复、checkpoint | 保留原始评分基线及累计进度，runtime epoch 变化不能清零任务 |
| 回滚到旧 checkpoint | 用 source epoch 与保存位置截断旧分支；保留 checkpoint 已有进度，撤销被回滚的成功 |
| 世界恢复中途进程被杀 | 子进程真实 `os._exit(42)`；保留 journal 与完整备份，重启前验证并恢复原世界 |
| 世界已复制、账本未记分支 | 测试 receipt 写入前后与 journal 删除前后的真实进程退出；未提交操作回滚，已提交文件系统恢复保留 receipt 供账本修复 |
| 备份损坏、receipt ID 不一致 | 拒绝启动或确认；保留恢复证据，避免继续使用不确定世界 |
| 快照复制失败、磁盘不足 | 快照在隐藏目录构建，校验/fsync 后原子发布；空间预检失败不改源世界 |
| 日志轮转、截断、未完成行 | 持久字节游标按新字节归档；partial 行不提前计入聊天 |
| 证据提交后、cursor 提交前退出 | 重放时稳定 evidence ID 去重，登记表与账本中断的链接可恢复 |
| 增量活动评分 | 连续 59 个世界快照追加；每次最多加载两份新/保留证据，最多保留四份快照引用与八份解析缓存 |
| Docker CLI 超时或取消 | 杀死本地进程组；容器内 helper 自带 timeout；stop 必须确认容器已停止 |
| SIGTERM/SIGINT | 仅首次取消根任务，等待 Supervisor 清理后返回 143/130 |
| 不同实验共用世界 | 即使 bot URL 不同，同 data root/container 仍互斥；localhost 地址别名不能绕过锁 |
| 错误参数、无限重试配置 | NaN/inf/bool/负配额在启动前拒绝；模型内部重试只允许 0，每次 Agent 重试分别计预算 |

## 历史：第一轮四 Agent Supervisor soak

[最终原始记录](validation/2026-10-21-supervisor-soak-final.json) 由 [longrun_soak.py](scripts/longrun_soak.py) 使用真实 Node 子进程运行 300 秒，16 项检查全部通过。整个过程未启动 Minecraft，也没有外部模型请求。

- 5,027 次模型尝试全部持久结算；5,022 个唯一程序提交，无重复派发。持久提交状态为 5,012 次成功、8 次不确定后中断、2 次拒绝；不确定动作未重新派发。报告分别列出 submission 与 execution：共有 5,016 个实际执行 ID，其中 5,012 个成功、4 个中断，其余提交在获得执行 ID 前终止。
- 在第 75、150、225 秒主动杀死 Node，分别约 0.617、0.716、0.665 秒恢复；预算与请求序列保持连续。
- 两个暂停窗口调用数保持不变；停止前四个会话的事件全部 ACK，停止后没有存活子进程。
- Python 观测 RSS 峰值 60.10 MiB，预热后增长 2.81 MiB，最后一分钟波动范围 0.105 MiB；tracemalloc 净存活增长约 0.58 MiB。Node HTTP 父进程观测 RSS 峰值 83.34 MiB，不包含四个 worker 的 RSS 合计。

这是有界的离线机制压力测试，使用的控制 lease 与 pace 是测试参数，不能据此宣称完成日尺度运行或真实 Minecraft 任务。

该历史版本完整套件为 Python 175 项、Node 34 项、FIFO 管理协议 3 项通过；Python 完整套件耗时 37.849 秒，Node 2.660 秒。

## 尚未定位的资源告警

第一轮完整 Python 套件 175/175 断言通过，但 stderr 出现 **7 条 `ResourceWarning`**，提示未关闭的 socket/transport；该轮没有定位。后续已定位并修复 broker 启动/取消的监听 socket 路径，复验见文末；原始告警仍保留，不删除历史记录。

[独立 FD 检查](validation/gateway-fd.json) 使用 [gateway_fd_check.py](scripts/gateway_fd_check.py)：12 轮启动/取消/关闭后 FD 均为 7，8 轮 SIGKILL 恢复期间为 12–13、最终回到 7；最终 300 秒 soak 的 RSS 与事件缓存也未显示持续累积。这些有限观察不能证明所有资源路径绝无泄漏，告警不因断言通过而被忽略。

## 历史：第一轮十万事件账本规模测试

使用真实 SQLite 文件写入 100,000 条执行终态，每条包含约 1 KiB 诊断字段。账本文件为 **145,162,240 字节**。在同一运行中重新构建报告与活动评分器：

| 操作 | 时间 | tracemalloc 测得的 Python 分配峰值 |
| --- | ---: | ---: |
| 完整报告，精确去重 100,000 个执行 ID | 2.019005 秒 | 20,121 字节 |
| 从账本恢复活动评分器 | 0.379568 秒 | 20,901 字节 |

原始结果与源码哈希见 [evidence-scale.json](validation/evidence-scale.json)，由 [evidence_scale.py](scripts/evidence_scale.py) 实际重跑生成。这是当次本地主机的测量，不能外推吞吐保证。tracemalloc 不包含 SQLite/系统原生内存和磁盘缓存。此次报告前后进程 RSS 为 23,105,536 → 27,828,224 字节，随后恢复评分器 RSS 为 27,836,416 字节。重点是报告默认逐行读取，执行去重在 SQLite 磁盘临时聚合中完成，Python 不保留历史源码/模型内容；显式 `events=` 参数仍由调用者控制其内存规模。

临时 SQLite 连接使用显式 `close()`，不能依赖 `with connection` 或 GC。新增生命周期测试在禁用 GC 并强持有连接对象时，循环采集、评分、报告和 status，验证超过 200 个临时连接均显式关闭。

## 复验入口

```bash
uv run python -m unittest discover -s tests -v
npm --prefix bot_runtime test
python3 -m unittest discover -s infra -p 'test_*.py' -v
docker compose --env-file infra/.env.example -f infra/compose.yaml config --quiet
uv run python scripts/evidence_scale.py --output validation/evidence-scale.json
uv run python scripts/longrun_soak.py --duration-s 300 --trace-memory
```

`tests/test_evaluation.py`、`test_world_checkpoints.py`、`test_observer.py`、`test_server.py`、`test_longrun_config.py` 覆盖本记录中的证据、文件系统、进程、配置和控制故障。整体套件与有界 soak 的通过范围见本文件前述原始记录。

## 历史：Docker 部署联调的本机环境阻塞

[原始联调记录](validation/2026-10-21-docker-fake-smoke.json) 为 `blocked`：RPC health 在 20 秒总期限内不可达，测试自建容器与网络均已清理。没有启动 Minecraft 或调用外部模型。

当时的只读排查中，宿主到 bot IP 以及 bot 容器自身 health 可返回 200；relay 的 DNS、路由与 ARP 正常，容器间 TCP SYN 没有获得回应。该轮尚未证明原因；文末记录本轮通过临时自建地址豁免实际验证的代理网络原因和隔离协议链路，不以当时的推测替代新证据。

三个镜像构建成功、固定 TCP relay 的五项单元测试通过，均不能替代这项尚未通过的完整容器链路验收。外部 Doctor 在模型 admission 前用 5 秒总网络期限检查 v2 health/凭据，失败即停止；它仅发送 GET，不创建 session 或调用模型。

## 版本迁移与停止边界

旧 manifest 的配置先补齐当前默认值并记录迁移事件；没有绝对截止时间的旧 run 使用原创建时间加原墙钟预算推导期限。迁移不延长实验时间。缺少 run ID/epoch/证据截止位置的旧 checkpoint 无法建立可靠回滚分支，会在修改世界之前被拒绝。

总恢复 episode 默认最多 20 次，每个 episode 默认最多 3 次尝试；外部网关不可达且无法确认旧 worker 停止时，框架安全终止并留证据，需要容器运维恢复。`shutdown_timeout_s` 是等待 Agent 取消的限额，世界复制安全结束及服务端保存期限另算，完整退出可能超过 30 秒。

## 真实环境验收门槛

操作者接受 EULA 并配置实际模型后，使用 `configs/minecraft-longrun.yaml` 的副本进行独立验收：先短任务，再逐步延长运行窗口；检查模型实际用量、服务器保存确认、checkpoint 回滚证据、内存/磁盘趋势与所有清理结果。模板的价格是示例，必须换成实际冻结计价，预算不会替代供应商账户的硬性账单限制。

完整 Minecraft 通关与 24 小时真实世界实验不属于已验证结果。代码和部署镜像修改后应重建镜像，记录实际 image ID；历史镜像 ID 不能代表本轮代码。

## Paper alignment 2026-10-21

这一轮实现论文公开方法对应的实验机制，并独立验收；不是对作者未公开 harness 的逐行为复制。方法、图表和工程选择的追踪见 [PAPER_ALIGNMENT.md](PAPER_ALIGNMENT.md)。`experiment.profile: strict` 保留前台生命周期和已有配置语义；`paper_aligned` 增加 v3 显式后台 jobs、结构化交接、独立 LLM Observer、受限编码容器、真实环境核验及用途分账。两种 profile 均适用持久预算、世界恢复和最终证据校验，恢复不自动改 profile。

### 当前已完成的测试

| 证据 | 当次实际结果 | 能证明的范围 |
| --- | --- | --- |
| [最终完整 Python 日志](validation/paper-python-after-instrumentation-fix.log) | 276 项通过，26.229 秒；`-W always::ResourceWarning` 下告警/unclosed 均 0 | 仪器修正后完整 Python/Node 进程联调、预算、配置、世界、工作区、报告和控制断言；冻结前 [276 项、27.842 秒日志](validation/paper-python-tests.log)另保留 |
| [完整 Node 日志](validation/paper-node-runtime-tests.log) | 47 项通过，2.751 秒 | v3 foreground/jobs、协议消融、固定身份与生命周期；infra 原生边界/FIFO 另 4 项、1.116 秒通过 |
| [独立 worker](validation/2026-10-21-remote-worker.json) | 10 项检查通过 | 真实 Docker worker、跨程序 jobs、协作取消、原生写入/exec 拒绝、死循环重建、断连 fencing、launcher SIGKILL 与独立 guardian 清理 |
| [隔离协议联调](validation/2026-10-21-isolated-protocol.json) | 7 项检查通过，cleanup complete | 真实 Mineflayer synthetic chunk/inventory 解码、固定身份代理、双向聊天包关闭、截止后重连；服务端为 synthetic 协议夹具 |
| [paper 生命周期](validation/2026-10-21-paper-lifecycle-soak.json) | 20 秒、13 项检查通过，507 次 admission/settlement | 四玩家加 Observer、223 次完整交接/0 shutdown fallback、4 个跨程序 jobs、用途分账、无未知动作重派、停止清理 |
| [Agent smoke](validation/2026-10-21-paper-agent-smoke.json) | 12 秒、382 次模型尝试、3 次故障恢复，检查通过 | FakeModel 与真实 Node 子进程的 Supervisor 联调 |
| [信号原始日志](validation/paper-signal-tests.log) | SIGTERM 与 SIGKILL/resume 两项通过 | 真实信号、进程清理及未决动作不重放 |
| [编码工作区](validation/2026-10-21-workspace-smoke.json) | 9 项检查通过，拥有容器均清理 | 实际 network=none 编码容器通过 Unix CLI 进入持久网关，幂等/账本、开发工具/文档、外部玩家身份拒绝、Observer 隔离 |
| [工作区限制与恢复](validation/2026-10-21-workspace-limits-smoke.json) | 11 项检查通过 | 实际编码容器的文件/工作区限制、恢复 fencing；每文件 16 MiB 内核硬限额，聚合配额为 shell 运行时每秒检测，不能宣称内核聚合限额 |
| [部署溯源](validation/2026-10-21-deployment-projection.json) | 6 项检查通过 | 实际不可变 image ID、有界 own Compose inventory、非秘密字段白名单、operator-only 记录与清理 |

这些记录明确 `actual_minecraft=false` / `paid_model=false`，或声明 fake/synthetic。Native 或协议检查通过不等于 Vanilla 生存任务通过。`validation/paper-supervisor-smoke.json` 保留更早的失败尝试；后续通过记录为上述 Agent smoke，不能混同为同一次运行。

后续 tracemalloc 已定位 broker 启动取消时未关闭的监听 socket，保留[修复前追踪](validation/broker-warning-trace.log)、[修复后同一路径](validation/broker-warning-fixed-trace.log)和[取消/停止循环](validation/gateway-stop-cancellation-trace.log)。当前完整测试未出现历史告警；有限 FD、RSS 与测试结果仍不证明所有可能路径绝无泄漏。

### 新增证据与统计口径

[20 项专项测试](tests/test_paper_evidence.py) 与原有评分/报告/预算、paper Supervisor 和 safety 测试合计 80 项通过。覆盖：

- 日志在独立周期采集，不等待默认 300 秒世界保存。按文件代际和字节偏移归档原文、SHA256、行号、source/receive 时间、actor 与 world epoch。持久游标由锁串行更新，崩溃重放仍使用稳定 evidence ID。
- 轮转先排空旧 inode 的未读尾部；旧 inode 消失、截短或 anchor 改写时记录明确 gap。未完成行和超长碎片不伪装成完整消息；9000 行压力样本被拆成最多 4096 行的小段，所有 9000 条均归一化，未因数组上限丢弃后半段。
- 关闭时即便世界已冻结或最终保存失败，也独立按持久 `pending_bytes` 排空日志，每次最多 8 MiB，整体使用服务端控制期限。真正 EOF 的尾片段作为 raw/incomplete 归档；字节预算边界不误切正常行。期限或配额阻止排空时，报告保留积压数量并标记不完整。
- `HH:MM:SS` 日期使用文件 mtime 锚定并跟踪跨午夜，IANA 时区写入证据；默认 UTC。日期锚定是明确的推断，时钟倒退、DST 歧义、非法时间和 UTF-8、原文缺口均留标记。English Vanilla Java 之外的陌生消息保留原文，不猜测死亡或 advancement。
- 死亡、聊天和 advancement 只从登记服务端证据计入；客户端广播副本和 Agent claim 不重复计数。JSON 提供有界的事实引用样本和精确遗漏数，完整字节证据仍在登记产物中。
- 全会话与首次验证成功前两种窗口从**首个实际程序启动**计时；run 创建/启动时间另留 `ledger_wall_time_s`。首次成功记录持久保留，后续继续运行或当前世界回滚不覆写这一历史窗口；当前能力成功仍由最终世界分支重新评分。
- 每玩家记录执行终态、完成时长精确 p50/p95/p99、未知/跨窗口 censored 数、活动比例、错误/超时/基础设施中断与服务端死因。跨程序 job 活动和前台程序取区间并集，重叠时间不相加；未完成程序不混入完整时长分布。
- 聊天采用 `chat-rules-v1` 多标签：warning、rescue、shared_storage、lesson、coordinates。代码使用完整源码 hash 与有界的去首尾空白行 hash；记忆统计实际工具结果。它们是可重建的词法/工具观察，不证明协作意图、复制行为、记忆被采用或因果收益。
- 阶段只列明确的登记证据。服务端日志标题不冒充 canonical advancement ID；快照中最后已记录 criterion 时间或快照观察时间作保守上界，初始 baseline 已有进度不当作新阶段。不存在的阶段保持未知。
- `usage-v2` 保留缓存输入、非缓存输入、输出、推理和 visible output 以及 inclusion flags。标准 details 视为总量子集，显式独立量只加一次；未知计量/单价为 null，保留已知部分和未知调用数。缓存创建等未明确支持的语义不按普通输入静默估价。
- 每次 player/observer 的 decision/review/compaction 都占同一全局预算，使用 reservation ID 合并 admission/response/settlement；provider cost 与按冻结配置估算不相加。付费预算有未知历史用量或费用时停止继续 admission。无 call ID 的历史数据保留关联歧义标志。
- 输出截断、日志抑制及 `evidence_incomplete` 明确计入不完整标记；包含取消记录 `cleanup:null` 的回归。原生输出已截去的内容不能由报告恢复。

### 新版十万事件报告规模

[paper-evidence-scale.json](validation/paper-evidence-scale.json) 由更新后的 [evidence_scale.py](scripts/evidence_scale.py) 实际生成，保存 Recorder、Budget、usage、collector/logs、scoring、report/analysis 八个源码 SHA256；生成后核对与文件一致。测试 actor 为 player1，100,000 个唯一执行终态，每条附约 1 KiB 诊断字段，SQLite 账本 **144,842,752 字节**。

| 操作 | 时间 | tracemalloc 分配峰值 | 进程 RSS 前 → 后 |
| --- | ---: | ---: | ---: |
| 新版完整报告，精确 100,000 succeeded | 11.943319 秒 | 41,931 字节 | 28,667,904 → 35,655,680 字节 |
| 从账本恢复活动评分器 | 0.368676 秒 | 29,015 字节 | 35,655,680 → 35,655,680 字节 |

新增窗口、逐玩家分布、用途和复用分析使用临时磁盘 SQLite 聚合；默认逐行处理，不按事件保留 Python 源码/模型列表。tracemalloc 不包括 SQLite 原生内存；RSS 也只是当次宿主观察，不构成吞吐或最大内存保证。显式 `events=` 参数仍由调用方决定其物化规模。历史 2.019 秒报告对应较少指标的旧实现，不能与本次数字混成同一版本。

最后一次测量另保留 66 个应用/脚本/锁文件的起止 SHA256（包括 Supervisor 与 Collector），`source_freeze_unchanged=true`，source tree 为 `b8ac63f4e1c5e6213e7c158a9bcff02e7cf7af06c8aa2d671d2e20c01c8231d6`，与最终 hour 快照相同。

### 宿主网络原因与隔离链路

[proxy-network-probe.json](validation/2026-10-21-proxy-network-probe.json) 验证：新建 peer 原先 TCP 不通；仅临时豁免这两个自建 peer 地址后 TCP 通；撤销后再次不通。宿主 Mihomo 本地地址集合未覆盖新建 Docker 地址，不再停留于旧版本“怀疑防火墙”的描述。记录明确 `global_policies_modified=false`、`temporary_changes_removed=true`。

[隔离协议试验](validation/2026-10-21-isolated-protocol.json) 使用本次自建 front/back 网络和四个 peer 的临时豁免，验证真实 worker→固定身份代理→synthetic 游戏协议链路并完成清理。没有持久修改宿主代理规则；正式部署仍需管理员正确配置实验网络的代理策略。旧 Docker smoke 的 blocked 保留为历史结果，不能据此否认新链路通过，也不能把临时试验解释为生产网络已完成配置。

最终隔离试验的 front 使用实际 isolated 网络、没有宿主网关。独立 [network-boundary.json](validation/2026-10-21-network-boundary.json) 记录 isolated 没有宿主桥地址，对自建宿主服务/公网的负向探测均不能连接，清理完成。普通 internal 正向对照同样被现有宿主代理阻断，因此 `ordinary_comparison=inconclusive_existing_host_policy_may_block_control_case`；不据此推断普通 internal 在所有环境中的表现。

### 小时验证与未完成的真实环境验收

本地 11:52 启动的[历史一小时 soak](validation/2026-10-21-paper-hour-soak.json) 已完成 3600 秒：15 项检查全部通过，13,710 次模型尝试结算，state.status=stopped、finalization_failed=0；原始[进度日志](validation/paper-hour-soak.log)保留。Python 观测 RSS 峰值 54,136,832 字节、预热后增长 7,086,080 字节，Node 父进程峰值 96,997,376 字节；此次没有 tracemalloc 检查。

该进程存活期间 Python 与 Node 源码发生过修改，不能将其作为最终源码验收。早先担心的旧版本 `cleanup:null` finalization 错误实际没有出现，未虚构失败或改写通过状态。[只读限定记录](validation/2026-10-21-paper-hour-soak-qualification.json) 保存原 JSON 的 SHA256 `d7cc02213731c36958ab44fbd1f583142b7038695561398435f149ef2c5ee6db`、原始检查与 state 状态；账本及原报告不变。最终验收使用下述独立冻结运行。

最终源码已用 [frozen_soak.py](scripts/frozen_soak.py) 独立复制 `src`、Node `src`、scripts、package/uv 锁文件，以及私有的只读 npm 依赖树，`node_modules` 链接到该私有副本。bootstrap 移除原 editable 源码路径，在预检和退出时审计所有 `minecraft_harness` 模块来源，并从快照 gateway 路径推导 Node server；开始/结束分别校验原源码、快照、依赖和 bootstrap 哈希。只读文件权限用于防止意外修改，不是同 UID 恶意进程的隔离边界。

[frozen-runner-smoke.json](validation/frozen-runner-smoke.json) 已用真实快照完成 10 秒 runner 验收：16 项机制检查、9 项冻结检查全通过，126 次 fixture 模型调用结算，3 次 Node 故障恢复。未改写的[子进程原始结果](validation/frozen-runner-smoke.raw.json)、[实际 import 审计](validation/frozen-runner-smoke.imports.json)和[原始日志](validation/frozen-runner-smoke.log)分别保留；Python 实际模块均来自快照，Node 与 Mineflayer 均解析到快照/私有依赖，原/快照起止哈希一致。**这不是最终一小时结果**。

**原冻结一小时完整脚本验收为 failed，不能记为一小时通过。** pace=1 秒、sample=5 秒、tracemalloc 开启，运行窗口结束后，在最终导出与报告阶段超过原有 45 秒停止等待，抛出 `TimeoutError`。[子进程原始结果](validation/2026-10-21-paper-hour-final.raw.json) 保留 14/16 项机制检查通过，`duration_completed`、`stopped_cleanly` 为 false，result 缺失；[wrapper](validation/2026-10-21-paper-hour-final.json) 的 `frozen_child_exit_zero` 同样为 false。账本虽已登记 stopped，后台报告线程后来完成，也不能据此宣称 Supervisor 当次正常返回。

这一小时登记 165,419 条事件、12,992 次 admission/settlement，pending 为空，三次 Node 故障恢复、暂停、全 ACK 和无遗留进程检查通过。Python 采样 RSS 峰值 63,795,200 字节，预热后增长 5,795,840 字节；Node 峰值 84,185,088 字节，tracemalloc 净存活分配增长 1,022,125 字节。这些是当次已完成的观测项，不改变完整脚本失败状态。[运行日志](validation/2026-10-21-paper-hour-final.log)、[实际 import 审计](validation/2026-10-21-paper-hour-final.imports.json)及[运行时路径证据](validation/2026-10-21-paper-hour-final.live-runtime.json) 分别保留。起止源码、依赖和 bootstrap 哈希检查通过，实际 Python/Node 均来自独立快照。

### 一小时收尾超时定位与独立补证

保留原 JSON、DB、报告和 SHA256，使用**相同冻结框架、相同完整 165,419 条事件账本**在独立路径重放 `export_jsonl + write_report`，见 [性能原始记录](validation/finalization-benchmark/results.json)与 [profile 文本](validation/finalization-benchmark/trace-on-profiled.txt)。

| 补证 | 实际结果 | 限定 |
| --- | --- | --- |
| tracing off 完整导出/报告 | 1.820 + 3.232 = **5.052 秒** | 输出与原件逐字节一致 |
| tracing on 完整导出/报告 | 15.368 + 25.514 = **40.882 秒** | 相比 off 约 8.1 倍；profile 指向线性 JSON/事件处理开销；profile 本身也会改变耗时 |
| [原冻结框架外部 hook](validation/2026-10-21-paper-finalization-hook.json) | 20 秒、16/16 机制检查与 8/8 补证检查通过、312 次调用 | 使用原冻结框架和未修改的原冻结 soak 脚本；仅在 ending snapshot 已记录后、stop 前由外部 hook 关闭 tracing |
| [修正后未来脚本实际运行](validation/2026-10-21-corrected-soak-fixture.json) | 12 秒、16/16 检查通过、167 次调用 | `stopped_before_finalization=true`，三次故障恢复、正常返回、无子进程及 pending |

原一小时 run_finished 到报告完成约 48.3 秒；完整账本重放显示 tracing 对收尾开销的显著影响，解释原有 45 秒等待为何超时。随后只修改 [longrun_soak.py](scripts/longrun_soak.py) 的测量边界：ending snapshot/比较完成后停止 tracemalloc，再进入停止、导出与报告；异常清理前也停止 tracing。**没有扩大 45 秒等待，也没有修改框架 `src` 或 Node `src`。**外部 hook [源码](validation/frozen_finalization_hook.py)、[原始短测结果](validation/2026-10-21-paper-finalization-hook.raw.json)和审计记录保留；同一框架补证正常返回，且全程来源/依赖未变。

[只读限定说明](validation/2026-10-21-paper-hour-final-qualification.json) 汇总原失败与上述独立补证，确认原 DB/raw/报告 SHA256 不变、三轮完整重放输出均与原件相同。冻结框架 core 保持原版本，只有测试仪器脚本变化；未再运行第三次完整一小时。因此当前结论是**一小时运行观测加独立收尾补证**，不是将原一小时完整脚本标记通过。

36 小时 [paper 模板](configs/minecraft-paper-aligned.yaml) 与 24 小时 [strict 模板](configs/minecraft-longrun.yaml) 是配置上限，不是运行证据。EULA 尚未明确接受，本轮未启动 Vanilla；付费模型、实际单价/供应商用量、真实短任务与小时/日尺度 Minecraft 运行仍待独立验收。

复验入口（均在本目录）：

```bash
uv run python -m unittest discover -s tests -v
npm --prefix bot_runtime test
uv run python -m unittest discover -s infra -p 'test_*.py' -v
uv run python -m unittest discover -s tests -p 'test_paper_evidence.py' -v
uv run python scripts/evidence_scale.py --events 100000 --output validation/paper-evidence-scale.json
uv run python scripts/paper_lifecycle_soak.py --duration-s 20 --output validation/paper-lifecycle-rerun.json
uv run python scripts/workspace_smoke.py
node bot_runtime/scripts/remote-smoke.js
node bot_runtime/scripts/isolated-protocol-smoke.js
```

后三项使用真实隔离容器/协议夹具，所需镜像与宿主网络条件见 [infra/README.md](infra/README.md)；它们不会替代真实 Vanilla 或付费模型验收。

## 编码工作区磁盘边界补验

自动准备的容器使用 `--ulimit fsize=16777216:16777216`。这是进程创建／扩展单个文件的内核硬限制；工作区总大小与文件数仍采用 shell 前后及执行期间每 1 秒的监测检查，存在采样间隔内的超额窗口，不宣称文件系统总量硬配额。

[真实 Docker 负向记录](validation/2026-10-21-workspace-limits-smoke.json) 验证 17 MiB 写入在 16,777,216 字节返回 `EFBIG`（errno 27）；工作区总量超限后保留文件、停止整个编码容器并阻止后续 shell。删除选定超额文件后能够重启同一容器。长命令与脱离原进程组的子进程在监测超额后由容器停止统一回收；本次观测耗时 1.2 秒。测试未启动 Minecraft 或模型。

复验：`uv run python scripts/workspace_limits_smoke.py`。`workspace_remove` 仅删除选定普通文件或空目录，不递归删除源码树。自配外部 shell 容器须自行提供相同 `fsize` 限制，自动准备路径会在复用容器时核验该限制。

## 2026-10-21：论文尺度长程复核

本节是针对论文尺度（4 玩家 + Observer、27h14m、约 24k 次模型调用、2,783 程序、605 死亡、6 次服务器重启）的静态复核与可测规模压测，不声称已经完成一次 27 小时真实 Vanilla + 付费模型运行。

### 已跑的规模/故障证据

- **250,000 条事件账本**（`scripts/evidence_scale.py --events 250000`）：SQLite 362,266,624 字节；完整报告构建 35.2 秒，Python 分配峰值 43 KiB，RSS 28.4 → 36.3 MiB；活动评分恢复 1.19 秒。完整报告仍按行读取，不把全部历史载入 Python。
- **180 秒离线 Supervisor soak**（`scripts/longrun_soak.py --duration-s 180 --trace-memory`）：2,870 次模型尝试、4 个真实 Node worker、3 次主动杀死 worker 后恢复、2,870 次 admission 全部结算；Python RSS 约 61 MiB，预热后增长低于 64 MiB，最终内存窗口波动低于 16 MiB；16 项检查全部通过（完整 JSON 见 `validation/supervisor-soak-*.json`）。
- **真实短跑工件外推**：`run9` 400 次模型调用产生 36.1 MiB artifacts（约 90 KiB/call）。按论文约 24k calls 和更长上下文估算，模型/工具 artifacts 约 6–12 GiB；`configs/minecraft-longrun.yaml` 与 `configs/minecraft-paper-aligned.yaml` 的 artifacts/run 配额已提高到 32/48 GiB，min free disk 2 GiB。
  - 这不是磁盘容量保证：27h 期间仍应独立监控 `df`、run 目录增长、SQLite WAL 与 artifacts 文件数。

### 已加固的长程配置

`configs/minecraft-longrun.yaml` 和 `configs/minecraft-paper-aligned.yaml` 现在把长程容错参数显式化：

- `max_model_failures: 20`、`max_event_failures: 20`、指数退避上限 60 秒、模型单次 180 秒；每次重试仍单独 admission/settlement，未知计费按 admission 预留保守计入；
- `supervision.max_recoveries: 60`、`recovery_attempts: 5`，覆盖论文里 6 次服务器重启之外的长尾网络/服务器故障；
- `max_artifact_bytes: 32 GiB`、`max_run_bytes: 48 GiB`；paper 模板的默认 60s 程序上限保留，允许 agent 显式选择到 600s；
- 观测台图表轮询从 5 秒节流为最多每 15 秒重建一次，避免长账本下 `/api/figures` 反复扫描服务端日志证据。

### 仍未验证/需要操作者处理

- **managed_container 真实长跑路径**：本文件的真实 Vanilla 验证使用外部 server（harness 容器无 Docker 管理权）；论文尺度运行需要由受信 launcher/宿主机提供 `managed_container`、Docker 控制和服务端自动恢复。该路径未被一次 27h 真实运行验证。
- **bot runtime/Compose 容器生命周期**：Compose 故意使用 `restart: "no"` 防止越过围栏；长时间运行需要外部 process supervisor 或操作员在 bot/relay 容器退出时重启并重新建立会话，否则仅靠 gateway recovery 无法恢复已死容器。
- **模型供应方长时行为**：180 秒超时和 20 次连续重试覆盖短暂故障，但持续数十分钟的供应商故障会以 `model_failed` 结束；未知计费仍可能消耗预算。
- **Observer 必需性**：paper profile 下 Observer 的 model/infra failure 会使整个 run 失败；长时运行需监控 Observer 的 `observer_outcome`/`observer_degraded`。
- **真实 27h 资源曲线**：MC 服务端 chunk/内存、世界增长、宿主机磁盘、SSH 隧道、dashboard 连接数都仍应由操作者在真实运行中监控；当前只有短跑与离线规模证据。

## 2026-10-21 二次复核补充：真实短跑中的 worker reset 与服务器内存

- 真实 `run9`（DeepSeek + 真实 Vanilla，45m26s、400 calls、204 programs、3 deaths）没有 `infra_failed`、
  `model_error`、`agent_error`，400 次调用全部结算且 `unknown_usage_calls=0`；但发生了 16 次
  `runtime_reset`（9 次 `unfinished_async_action`、5 次 `timeout`、1 次 `unfinished_movement`、1 次
  session close）和 32 次 worker exit。所有 reset 都被 harness 自动恢复，程序/事件没有 replay；这说明
  长程中 worker 重启是**常态成本**，agent 必须在 recovery event 后重新观察并重建 globals。
- 同一 run9 结束时 Minecraft 服务端容器内存约 1.22 GiB / 2 GiB limit。长程必须监控 server RSS 与
  save/restart 行为；managed_container 的自动恢复路径仍未被 27h 真实运行验证。
- 这些是真实单机短跑证据，不是 27h 长程曲线；worker reset 频率、服务器内存增长、世界大小和模型供应商
  长时行为仍需在 staging/正式长跑中观测。

## 2026-10-21 二次修复：长程会话丢失与残留代码

- `runtime.recover_session_loss: true`（paper/longrun 模板）允许 bot runtime 重启后重建 session；旧 session
  丢失会被记录为 `runtime_session_lost` 并触发 world/runtime reset，而不是直接终止 run。仍要求外部
  process supervisor/操作员把 bot 容器重新拉起来；Compose 默认 `restart: "no"` 的安全选择未改变。
- recovery 窗口提升到 8 次、base 5s/max 120s，覆盖容器重启时间；pre-restart log drain 保留。
- bot runtime 的 `LEFTOVER_POLICY=allow` 在 paper compose 中启用；超时执行 detach 但 CPU 死循环会在
  5 秒 abort grace 后回退到 reset。
- paper bot 输出上限提高（4096 条日志 / 4 MiB 日志 / 2 MiB result）。
- 这些是机制修复，不是 27h 真实运行的验收；仍需按前述 staged validation 执行。



## 2026-10-21 OCI sandbox / world map 补充

- Worker launcher 支持 `WORKER_SANDBOX_RUNTIME=runsc`：gVisor（<https://gvisor.dev/>）作为可选 OCI runtime；
  默认仍为 runc，加上只读容器、seccomp、网络隔离。选择依据与替代方案（nsjail/bubblewrap）见
  [infra/SANDBOX.md](infra/SANDBOX.md)。
- `world_render.py` 新增 read-only Anvil/level.dat top-down 与等距 2.5D PNG；dashboard 通过
  `/api/world-image?run=…&style=iso` 按 world fingerprint 缓存。它不是三角网格 3D 渲染。

## 2026-10-21 性能压测与修复

性能审计见 `runs/_analysis/perf/REPORT.md` 与 `RUNBOOK.md`（250k 事件/24k submissions/100 snapshots/logs、100k/250k/500k/1M 事件 scale、90s 4-agent soak）。已修复：

- dashboard `/api/world-image` 单飞锁：4 并发冷请求由约 23–25s 降到约 2.3s（一次渲染）。
- `/api/figures` 响应 gzip + 30s TTL：1.27MB payload 压到约 16KB；不再每个 event 重建。
- dashboard `programs` 页缓存、`_cached` 单飞、accept queue 128；16 并发 overview/programs/figures p95 约 14–22ms（warm）。
- SQLite 增加 `events(execution_id,actor_id,event_seq)` 与 `submissions(json_extract(...execution_id))` 索引（新 run；旧 run 需重建/迁移）。
- Observer `seen_events` 从每事件 connect+commit 改为持久连接、每页 commit；5000 次 grant 从约 4.8s 降到 0.006s。
- resource guard 默认 O(1) 统计，完整 artifact 扫描每 300s 一次。
- 仍保留：report finalization 单线程、compaction handoff 额外模型调用、旧 run 无新索引。
