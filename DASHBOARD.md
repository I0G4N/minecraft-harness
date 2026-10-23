# 实验观测台

实验观测台把实验运行、事件、程序、后台任务、聊天、Observer、证据和费用放在同一个本地页面。可以查看已结束的运行，也可以观察仍在工作的 Supervisor。打开页面只读取已有数据，不会启动 Minecraft、调用模型或重放游戏程序。

## 打开已有实验

在 `experiments/minecraft_harness` 下执行：

```bash
# 选择 runs 的直接子目录；顶部下拉框切换实验。
uv run mc-harness serve runs --port 8899

# 也可以只开放一个运行目录。
uv run mc-harness serve runs/<run-id> --port 8899
```

浏览器打开 [本地实验观测台](http://127.0.0.1:8899/)。带 `?run=<目录名>` 的地址可以直接选择某次运行，例如 [paper 生命周期记录](http://127.0.0.1:8899/?run=paper-lifecycle-20261021-123443)。服务只监听 `127.0.0.1`；结束查看时在服务终端按 Ctrl+C，不会停止被观察的实验。

多实验模式仅发现显式指定目录的直接子目录，不递归扫描磁盘，不接受跳出该目录的 run 参数。数据来自每次运行的 `manifest.json`、`events.sqlite3`、内容寻址产物和已有报告；浏览不会重新生成报告。旧运行缺少某类记录时，页面显示未知、暂无或读取范围，不补造历史数据。

## 七个视图

| 视图 | 用途 |
| --- | --- |
| 实验总览 | 运行状态、诊断标记、事件与模型调用、参与者、最近动态和数据范围 |
| 事件时间线 | 在服务端按参与者、类别、级别和关键词筛选，再加载更早事件；点击记录查看原始详情和产物 |
| 程序与任务 | 前台程序与跨程序后台 jobs 分开查看；保留执行 ID、源码、回执、错误、代际和相关事件 |
| 协作聊天 | 区分服务端证据与客户端观察；显示显式的去重范围和固定规则标签 |
| 证据与评估 | 确定性评分、阶段证据、首次成功前与完整会话窗口、采集缺口及玩家统计 |
| Observer 审阅 | 审阅记录、发现和证据引用；Observer 是建议来源，不替代 Evaluator 的成功判定 |
| 资源与费用 | 已记录内存采样、预算和存储；按参与者、角色与用途查看模型费用 |

页面顶部的“暂停刷新”只停止页面轮询，实验继续运行；“立即刷新”重新读取。事件筛选和分页不会发起控制请求。手机窄屏通过左上角按钮打开导航，宽表格可在表格内部横向滚动。

详情中的长 JSON、源码和报告使用有界分页。产物按 SHA256 校验，完整报告可下载 JSON 或 Markdown；不存在或被修改的文件会明确报错，不以预览冒充完整证据。

## 如何理解数据

- `diagnostic_pass` 和“诊断运行”表示 fake/synthetic 机制检查，不表示真实 Minecraft 任务成功。程序正常结束也不等于任务通关。
- 资源曲线使用已持久化的采样，并把最新状态单点与历史窗口区分。新增运行默认每 10 秒登记一次资源事件（`supervision.resource_sample_interval_s`），资源约束检查默认每 5 秒进行；旧运行没有的历史不会被补造。图表保留最多 600 点，可向前加载更早窗口或返回最新，历史窗口不混入最新状态。
- jobs 的状态是最后记录的状态，不是当前进程存活保证；任务创建代际与最新事件代际分别保留。
- 服务端聊天优先；客户端向不同接收者重复投递时，有限窗口的去重采用明确标注的规则，不能据此证明消息独立发生的次数或协作因果关系。
- 费用的已知部分与未知调用分别保留。未知 token/费用显示未知；缓存输入和推理 token 的包含关系沿用原始计量，不重复相加。
- 统计窗口与报告范围均来自原账本。首次验证成功记录和当前世界分支评分是不同数据，回滚后不能仅凭历史成功记录推断当前成功。

## 实验控制

侧栏“实验控制”先选择暂停、冻结、恢复、检查点或停止，再明确确认一次；只有确认后才提交 POST。请求需要本地页面的操作者 token 和同源检查，随后进入原有持久控制队列，返回 `control_id`。排队成功不等于 Supervisor 已执行成功，应继续核对命令结果。

控制只适用于仍由 Supervisor 持有的活动实验。已退出的实验可以继续浏览；要重新启动恢复流程，使用 `uv run mc-harness resume runs/<run-id>`。页面不会自行启动实验。CLI、观测台和实验目录属于操作者侧，不提供给玩家代码或公开网络。

## 本机可浏览的离线数据

| 目录 | 内容与范围 |
| --- | --- |
| `runs/paper-lifecycle-20261021-123443` | 4,299 条事件、四玩家与 Observer、123 条审阅、223 次交接、4 个后台任务；适合查看各视图 |
| `runs/paper-hour-final` | 165,419 条事件、12,992 次模型记账；适合检查大账本筛选、分页和程序回执 |

以上都是已有离线 fake 数据，没有 Vanilla 或付费模型调用。原冻结一小时脚本的收尾超时结果仍为 failed；看板读取该账本不改变这一资格，见 [原始限定说明](validation/2026-10-21-paper-hour-final-qualification.json)。这些运行目录是本机产物，不随 Git 分发；换一台机器时选择自己的运行目录。

## 看板验收

真实浏览器验收入口：

```bash
uv run python scripts/dashboard_acceptance.py \
  --output validation/dashboard/acceptance.json
```

验收使用已有离线账本启动临时本地监听器，实际 Chrome 完成导航、筛选、分页、详情、实验切换和窄屏截图；控制确认及不可信文本只在独立临时 fixture 上检查，不向原运行提交控制。结束后比对原 DB、manifest 和报告 SHA256，关闭临时浏览器与监听器。它不会重新执行实验。

浏览器驱动使用 Node.js 22 自带 WebSocket/fetch 和本机 Chrome DevTools Protocol，不增加产品依赖。可通过 `DASHBOARD_CHROME=/path/to/chrome` 指定浏览器。仅需 HTTP 检查时加 `--skip-browser`；这不等于浏览器交互验收。

2026-10-21 最终验收已完成：

| 原始证据 | 实际结果 |
| --- | --- |
| [完整独立验收](validation/dashboard/acceptance.json) | 61 项检查通过；35 次真实 HTTP 请求，原 DB、manifest、报告 SHA256 不变；12 个应用/验收文件的起止哈希一致 |
| [真实浏览器子结果](validation/dashboard/acceptance-browser.json) | HeadlessChrome 156，36 项浏览器检查通过；无 Runtime/console 错误、无失败 HTTP/网络请求 |
| 735 条临时资源采样 | 跨过 600 点上限继续向前加载，时间窗口与游标准确，历史不混入最新状态，返回最新恢复正确窗口 |
| 控制与不可信文本 | 原运行零 POST；临时 fixture 仅确认后提交一次 pause；含 HTML 的消息作为文本显示，没有执行 |
| 桌面与窄屏 | 1440 与 390 像素实际截图；总览、事件、详情没有页面横向溢出 |

最终截图：[桌面总览](validation/dashboard/acceptance-browser/desktop-overview.png)、[程序源码与回执](validation/dashboard/acceptance-browser/desktop-program-detail.png)、[Observer](validation/dashboard/acceptance-browser/desktop-observer.png)、[资源费用](validation/dashboard/acceptance-browser/desktop-resources.png)、[手机总览](validation/dashboard/acceptance-browser/mobile-overview.png)、[手机事件](validation/dashboard/acceptance-browser/mobile-events.png)、[手机详情](validation/dashboard/acceptance-browser/mobile-event-detail.png)。查看过实际截图后完成验收，未修改产品代码来配合测试。

最终结果使用 `acceptance.json` 和 `acceptance-browser/`。目录内 `acceptance-initial`、`acceptance-v2/v3/v4`、`live-service` 等保留为中间或旧版记录，不能代替最终截图和源码哈希。初次 full Chrome 的本机 loopback 导航超时及驱动动画等待问题也未被抹去。原始证据保存在 [validation/dashboard](validation/dashboard/)；[数据源清单](validation/dashboard/data-sources.json)记录实际只读发现的数量和范围。

## 实时第一视角（可选）

实验观测台新增「实时第一视角」页，把每个玩家 bot 的真实游戏画面以浏览器实时渲染的方式并排展示（基于
`prismarine-viewer`）。这是渲染视图，不是视频录像，也不能替代服务器证据或确定性评分。

启用步骤：

1. 构建包含 viewer 依赖的 bot 镜像（`bot_runtime/package.json` 已声明 `prismarine-viewer` 与
   `@napi-rs/canvas`；Dockerfile 会在 `npm ci` 后运行 `scripts/canvas-shim.mjs`）。
2. 给每个 bot 设置 `VIEWER_PORT=<端口>`（可选 `VIEWER_FIRST_PERSON=false`、`VIEWER_BIND=127.0.0.1`）。
   默认只绑定回环地址；在 Compose 中若需发布端口，则设 `VIEWER_BIND=0.0.0.0` 并显式映射端口。
3. 在实验配置写入 `runtime.viewer_ports: {player1: 3105}`；冻结的 manifest 会记录端口，观测台据此生成直播卡片。
4. 打开观测台「实时第一视角」页（`#live`）。

非本机访问需要 SSH 隧道（示例，工作台端口以实际启动参数为准）：

```bash
ssh -N -L 8896:127.0.0.1:8896 -L 3105:127.0.0.1:3105 <user>@<server>
# 浏览器打开 http://127.0.0.1:8896/?run=<run>&#35;live
```

说明：viewer 画面只在 bot 在线时可用；连接中断会显示空白。多个玩家按 `runtime.viewer_ports` 逐项增加隧道端口。

## Minecraft GUI 主题

观测台整体采用 Minecraft 物品栏/书册风格：泥土与橡木背景、物品槽导航、石质按钮、像素字体、末影/红石/金锭
图标，以及图表的 Minecraft 书页边框。主题完全由前端 CSS 实现，不改变任何 API、数据语义或实验行为。

- 方块/物品纹理：**Pixel Perfection Legacy 13.21.3**（XSSheep / Nova_Wostra，**CC BY-SA 4.0**），
  抽取的 40 个 dashboard 装饰文件位于 `src/minecraft_harness/static/ui-assets/`，由本机 `/ui-assets/` 提供；
  游戏实验本身不加载资源包。
- 像素字体：**Cubic 11 / Fusion Pixel Font**（TakWolf，**SIL OFL 1.1**），WOFF2 位于
  `src/minecraft_harness/static/ui-assets/fonts/Cubic_11.woff2`。
- 完整来源、版本、许可与抽取清单：
  [ui-assets/ATTRIBUTION.md](src/minecraft_harness/static/ui-assets/ATTRIBUTION.md) 与
  [ui-assets/OFL.md](src/minecraft_harness/static/ui-assets/OFL.md)。
- 响应式布局、键盘导航、跳过链接、操作员控制弹窗和所有只读边界保持不变；纹理仅作装饰（alt 为空、
  `aria-hidden`），不承载数据。

### 布局与交互

- **分组导航**：侧栏分为「运行」「证据与审计」「观察工具」三组，九个页面各自有对应物品图标；
- **运行 HUD**：顶栏下方粘性显示状态、运行时长、提交数、模型调用/费用、Observer 报告数，并提供「实验控制」和「实时视角」快捷入口；
- **快速入口**：总览页在指标下方提供事件、程序、证据、聊天、图表、直播的快捷按钮；
- **侧栏收起**：桌面端可用顶栏按钮收起侧栏（状态记在 localStorage），≤980px 自动变为抽屉导航；
- **数据表**：长表在限高滚动区内吸顶表头，筛选栏与分页保持可见；
- **证据阶段列表**：默认只渲染 20 项，按需「展开更多阶段」，避免一次加载上百条推进记录；
- **专注模式**：论文图表页提供「⛶ 专注」按钮，隐藏导航与页面外壳，只保留图表；`Esc` 退出；
- **存档世界地图**：图表页新增「存档世界 · 地图」，从 run 的 `server.world_dir` 只读解析 level.dat/Anvil region，按存档指纹缓存 1024×640 的 160×160 textured 3D voxel diorama（另有 full-world LOD/top/iso 样式）；仅覆盖 overworld spawn/base，不是真 3D 网格；
- **响应式**：820px 宽度下已验证 HUD、两列指标、表单向导和抽屉导航可用。

## 论文图表的动态回放

观测台新增「论文图表」页（`#figures`），把论文中的图 1/3/4/5/6/8/12/13/14 按本 run 的账本与服务端证据
重建为可交互、可播放的视图。时间是相对首个程序的秒数；切到该页后随观测台轮询更新，具备「播放/暂停」
「拖动时间」「跟随最新」控制。

视觉上向论文图表靠拢：米白画布、柔和的维度色块、论文玩家的蓝/橙/绿/紫角色色、像素物品图标与像素头像、
轻量虚线/圆点标记，以及图下方的 "Figure n | ..." 说明。所有文字都用普通 HTML/CSS 渲染（时间线、进度、
末地面板均为 DOM 元素，而不是缩放 SVG 文本），字体更清晰；条形图和标记用 CSS 定位，便于交互与响应式。

**材质包**：物品图标与玩家头像来自 **Pixel Perfection Legacy 13.21.3**（作者 XSSheep / Nova_Wostra，
**CC BY-SA 4.0**，Modrinth 项目 `pixel-perfection-legacy`）。只抽取图表所需的 14 个物品/方块贴图和 4 个
玩家头部贴图，以 `<img>` 形式由本机 dashboard 提供（`/figure-assets/`），不进入游戏运行时、agent 工具或
评分证据；游戏实验本身不使用资源包。来源、版本、归档 SHA-256 与许可见
[`static/figure-assets/ATTRIBUTION.md`](src/minecraft_harness/static/figure-assets/ATTRIBUTION.md)。

| 页签 | 对应论文 | 内容 |
| --- | --- | --- |
| Fig.3 运行时间线 | 图 3 | 每位玩家一条泳道：服务器聊天（圆点）、成就（菱形）、死亡（红色刻度）、程序条；底部为程序数柱状图；Overworld/Nether/Stronghold/The End 维度底色；服务器重启/恢复与运行时重置虚线；点击消息/死亡/成就可打开证据事件 |
| Fig.1/4 十三步进度 | 图 1、4 | 玩家卡片（程序/消息/死亡数）+ 论文的 13 个依赖步骤，按维度分栏；已到达步骤显示时间、玩家、精度（日志/成就/快照上界/起始前已达成）；死亡带 |
| Fig.5/6/8/14 聊天回放 | 图 5、6、8、14 | 按玩家分泳道的聊天点图 + 可回放消息列表；可按玩家过滤、点击证据编号打开原始事件 |
| Fig.12 末地战斗 | 图 12 | 水晶轮盘与龙血曲线；当前 harness 尚未采集该观测通道，页面会明确显示"未采集"，待 kill_dragon 观测通道接入后填充 |
| Fig.13 行为与死因 | 图 13 | 论文五类聊天行为的确定性近似统计（可多标签）+ 服务端死亡原因条形图；另列本项目词法标签 |

数据口径：

- 只读 `events.sqlite3`、世界快照与规范化服务端日志制品；不信任玩家/聊天中的成功声明。
- 13 步检测混合 advancement（成就时间优先）、world statistics（`crafted`/`picked_up`，首次出现快照时间是
  观测上界）、服务端日志 advancement 标题；**十水晶**步骤没有 advancement/统计对应，固定标为 unavailable。
- 「学术上界」：快照派生步骤的时间是"观测到的上界"，不是实际发生时刻；悬停/详情会显示精度和证据引用。
- 图 12 的龙血/水晶需要玩家程序观测通道；本框架尚未提供，页面保留空状态。
- 不生成论文中的 3D 世界渲染图（本框架没有世界渲染器）；实时画面可在「实时第一视角」查看。
- In-game days（论文图 3 的 ~81 天）尚未采集；需要 `level.dat` DayTime。
- 行为分类是确定性正则近似（论文也未公开其原始模式代码），不能当作因果或意图判断。
- 数据有界：程序/消息/死亡/成就各最多 4000 条，超出部分在页面底部显示省略数。

本地 API：`GET /api/figures?run=<run>`（仅本机操作者访问）。测试：`tests/test_figures.py`。



## 2026-10-23 世界渲染缓存位置

Dashboard 的 `/api/world-image` 与 `/api/world-meta` 把 PNG/JSON 缓存写到
`<run_dir>/renders/`，不再写入 `<run_dir>/reports/`。报告目录由 Supervisor
独占，避免 Dashboard 容器用户与 Supervisor 用户不同导致最终报告
`PermissionError`。旧 run 的渲染缓存可保留在 `reports/`，不影响读取。

## 2026-10-23 Viewer 断线重连页

Caddy 的 `/viewer/playerN/` 现在：
- `reverse_proxy` 带 `lb_try_duration 6s`，worker 短暂重启时先重试；
- 后端仍不可用时返回 **503 HTML 自动刷新页**（每 2s reload），
  不再直接显示 502。worker 恢复后浏览器自动回到直播页。
