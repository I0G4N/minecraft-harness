# 端到端链路审查（2026-10-23）

审查范围：`run-paper.sh` → Compose server/identity/launcher/bot → per-execution worker
sandbox → Pi runner / model forwarder → execution gateway / durable ledger → Observer /
report → Dashboard / Caddy / viewer → teardown / world save。

## 已确认正常的部分

- 4 players + Observer 的 paper_aligned 容器栈可启动、4/4 runtime_ready、Pi runner
  与 DeepSeek 调用正常。
- detach Supervisor 在 wrapper 退出后继续运行；1/1.5/2/5 分钟 run 的 wall 计时、
  world save、server stop、compose down 正常。
- session DELETE 收尾超时已修复（`36dc7b3`）；真实强制重试 run 出现 7 次
  `runtime_shutdown_retry` 后仍然 `rc=0`，无 `cleanup_failed`。
- Dashboard `/api/world-image`、`/api/world-meta`、`/api/live-report`、报告下载
  在 run 结束后可读；Caddy 的 LAN/Tailscale 入口和 `/viewer/playerN/` 均返回 200。
- 全量 Python 测试 **761 passed, 3 skipped, 734 subtests**（本次新增 UDS 测试后
  实际 763 passed）。

## 本轮新发现并修复

1. **Dashboard 渲染缓存与最终报告目录冲突**（`773ef62`）
   - 根因：`/api/world-image` 把 PNG/JSON 写进 `<run>/reports/`；Dashboard 容器
     以映射 UID 创建该目录，Supervisor 无写权限，`write_report` 的 `mkstemp`
     抛 PermissionError，完成 run 被覆盖成 `infra_failed`。
   - 修复：缓存改到 `<run>/renders/`；`write_report` 遇到 foreign reports 目录时
     重命名为 `.reports-foreign-<uuid>` 后重建并写入。
   - 验证：`verify-dash-1` 运行中请求 Dashboard 世界图，只生成 `renders/`；
     结束时报告写入成功、`budget_exhausted`、`rc=0`、无 `finalization_failed`。

2. **RUN_DIR 过长导致 broker Unix socket 启动失败**
   - 现象：run dir 名较长时，栈已经启动后才出现
     `OSError: AF_UNIX path too long`；`live-test-1` 之前的一次长路径 smoke
     就是这样失败的。
   - 修复：
     - `run-paper.sh` preflight 用 `realpath -m` 检查
       `<RUN_DIR>/broker/socket` 字节数 ≤ 107，超长时在 compose_up 之前失败；
     - `SubmissionBroker.start` 同样提前抛清晰 ValueError；
     - 新增 `tests/test_broker_paths.py`。
   - 验证：144-byte probe 的 `--prepare` 被 preflight 拒绝；正常路径可 bind/close。

3. **stale Supervisor / worker sandbox 预检缺失**
   - 现象：上次 run 的 exited `<project>-supervisor` 或 launcher 创建的
     `mc-worker-<project>-*` 残留会让下一次 `--start` 在栈启动后才失败，
     可能留下无人监管的运行栈。
   - 修复：preflight 提前拒绝 stale supervisor 容器和项目 worker sandbox；
     detach Supervisor `docker run` 失败时自动 save/stop/down 整个栈。
   - 验证：dummy supervisor/worker 容器存在时 `--prepare` 均在启动前
     返回 PREFLIGHT FAILED。

## 仍然存在、非本轮修复的问题

1. **收尾时的在途 model call 记为 unknown**
   - 近几次 run 的 `model_request` 比 `model_response` 多 1–4 个；预算把在途调用
     记为 `unknown_usage_calls`，因此 report 的 `input_tokens`/`output_tokens`/
     `estimated_cost_usd` 为 `null`。这是“不虚构 usage”的保守设计，不影响 run
     状态或证据；但论文表格里的 token/cost 会显示 unknown。若需要可计算的总成本，
     下一步应改为在 SIGTERM 前等待/优雅结束在途模型请求，而不是估算。
2. **startup/resume 初始 fence 仍是单次超时**
   - 正常 wall/success/operator stop 走 `gateway.close()`，已带收尾重试；
     启动/resume 时的初始 `gateway.fence()` 仍沿用普通 10s 请求超时。
     硬崩溃后 resume 若残留仍在 teardown 的 sandbox，理论上仍可能遇到同类超时。
     本次没有扩大修改范围；做 90 分钟硬崩溃演练时建议统一接入同样的 bounded retry。
3. **Viewer 直连端口不是入口**
   - `127.0.0.1:3105–3108/` 会返回 404/拒绝；必须走
     `8902/viewer/playerN/`（Caddy）。LAN 用 `192.168.1.237:8902`，Tailscale 用
     `100.65.35.7:8902`，公网客户端需要 SSH 隧道。
4. **Minecraft bot kick / death 属于实验行为**
   - 15 分钟 run 有 30 次 bot kick、6 次死亡；2 分钟 run 有 3–5 次 kick。
     runtime 会自动重启并恢复 session，不是当前 infra 故障；但会消耗 turn、
     造成进度回滚，属于论文对齐实验本身的观察项。
5. **Dashboard 写状态与历史报告不一致（仅旧 run）**
   - `live-test-1` 的 ledger 保留 `infra_failed` 作为本次故障记录；已用新 fallback
     手工补齐 `reports/report.*`，报告显示 `budget_exhausted`。新 run 不会再出现。

## 建议的 90 分钟启动前检查

- 使用新的 run dir（名字尽量短，满足 UDS 限制）；
- fresh world（`.harness-world.json` 无 run_id，playerdata/stats/advancements 为空）；
- 确认没有 `<project>-supervisor`、`mc-worker-*`、compose networks 残留；
- `PAPER_SUPERVISOR_CONTAINER=1`，detached Supervisor；
- 预算上限与 wall 明确（建议 wall=90min、max_cost=$5）；
- 启动后观察 `runtime_ready`、`bot_kicked`、`model_request/response`、`resource_sample`、
  `pi_session_sample` 和最终 `cleanup_failed/finalization_failed` 事件。

## 2026-10-23 21:xx 长程稳定性补充

- 修复 recovery/startup/operator fence 的单次 10s 缺口；统一使用
  `fence_with_cleanup()`，owned runtime 走短路径 + `_fence_owned_processes`
  兜底，避免已死 runtime 让恢复等待 60s。
- `_resource_guard` 增加 50/75/90% `storage_alert`。
- `run-paper.sh` 增加 DEST/H source drift preflight；实测发现 H `src` 落后会
  立即导致新 Supervisor `AttributeError`，已同步 H 并移除误拷文件。
- 真实 server crash 恢复：3 分钟 run 中 `docker kill -9` server，检测→重启→
  world_epoch=1→4/4 ready→wall 正常结束，`rc=0`，报告正常。
- 120s offline soak：16/16 checks passed，3 次 runtime SIGKILL 恢复，pause
  稳定，RSS/tracemalloc 达标。
- 输出证据：
  - `mc-runs/longrun-server-crash-2/`
  - `validation/supervisor-soak-20261023-213511-b.json`
