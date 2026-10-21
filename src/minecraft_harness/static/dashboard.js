/* Local operator interface. No generated text is interpreted as HTML. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="operator-token"]')?.content || '';
  const VIEWS = {
    overview: ['实验总览', '在同一处查看运行状态、协作过程与可核验证据。'],
    events: ['事件时间线', '按参与者、事件类别与级别，检索每一次实验活动。'],
    programs: ['程序与后台任务', '追踪提交、执行、回执与取消，回到原始源码。'],
    chat: ['协作聊天', '阅读参与者的协作过程，并区分服务端证据与客户端观察。'],
    evidence: ['证据与评估', '从可核验证据理解结果，保留未知与不完整的部分。'],
    observer: ['Observer 审阅', '独立模型的证据审阅、质疑与基础设施观察。'],
    resources: ['资源与费用', '查看资源趋势、预算使用与各类模型调用的费用。'],
    figures: ['论文图表', '按论文图 1/3/4/5/6/8/12/13/14 的口径动态回放本 run 的过程、阶段与证据。'],
    live: ['实时第一视角', '以浏览器实时渲染观看每个玩家 bot 在真实游戏中的第一视角画面。']
  };
  const PAGE_ICONS = {overview:'map.png', events:'clock.png', programs:'redstone.png', chat:'writable_book.png', evidence:'spyglass.png', observer:'ender_eye.png', resources:'gold_ingot.png', figures:'painting.png', live:'ender_pearl.png'};
  const LABELS = {
    running: '运行中', succeeded: '已完成', failed: '失败', timed_out: '超时', cancelled: '已取消',
    interrupted: '基础设施中断', invalidated: '已失效', rejected: '已拒绝', accepted: '已接收', submitted: '已提交',
    pending: '等待中', queued: '排队中', applying: '执行中', applied: '已执行', completed: '已完成',
    stopped: '已停止', stopping: '停止中', paused: '已暂停', frozen: '已冻结', success: '已核验成功',
    capability_success: '已核验成功', diagnostic_pass: '诊断通过', diagnostic_only: '诊断运行',
    budget_exhausted: '预算耗尽', infra_failed: '基础设施失败', model_failed: '模型失败',
    stalled: '无进展停止', unknown: '未知', observe_only: '仅观察', not_started: '尚未开始',
    not_succeeded: '尚未满足任务标准', insufficient_evidence: '证据不足', unsupported: '暂无可用评估规则',
    verified: '已验证', player_claim: '玩家声明', insufficient_evidence: '证据不足', contradicted: '相互矛盾',
    agent_decision: '玩家决策', observer_review: 'Observer 审阅', context_compaction: '上下文交接',
    player: '玩家', observer: 'Observer', server: '服务器', runtime: '运行时', harness: '框架',
    agent_claim: 'Agent 声明', client: '客户端', strict: '严格模式', paper_aligned: '论文对齐模式'
  };
  const EVENTS = {
    execution_started: '程序开始', execution_accepted: '程序已接收', execution_submitted: '程序已提交',
    execution_finished: '程序结束', execution_succeeded: '程序完成', execution_failed: '程序失败',
    execution_timed_out: '程序超时', execution_cancelled: '程序已取消', execution_interrupted: '程序中断',
    job_started: '后台任务开始', job_finished: '后台任务结束', job_cancelled: '后台任务已取消',
    model_admitted: '模型调用已准入', model_request: '模型请求', model_response: '模型返回', model_usage: '模型用量结算',
    context_compacted: '上下文完成交接', observer_report: 'Observer 提交审阅', observer_degraded: 'Observer 已降级',
    observer_outcome: 'Observer 状态变化', observer_infra_issue: 'Observer 基础设施请求',
    bot_chat: '游戏聊天', game_chat: '游戏聊天', server_chat: '服务器聊天',
    bot_death: '客户端死亡观察', server_player_death: '服务器死亡记录', server_log_segment: '服务端日志证据',
    server_advancement: '服务器进度记录', first_success_verified: '首次成功已核验',
    world_restored: '世界已恢复', runtime_reset: '运行时已重建',
    run_started: '实验开始', run_resumed: '实验恢复', run_finished: '实验结束', run_stopped: '实验停止',
    control_applied: '操作者指令已执行', control_failed: '操作者指令失败', control_requested: '操作者请求',
    checkpoint_saved: '检查点已保存', resource_sample: '资源采样', resource_guard_sample: '资源采样',
    effective_environment_verified: '实际环境已核验', deployment_provenance_verified: '部署来源已记录'
  };
  const pageState = () => ({items: [], cursor: null, hasMore: false, loaded: false, busy: false, revision: 0, queuedReset: false, scope: {}, completeness: {}, filters: {}});
  const state = {
    run: new URLSearchParams(location.search).get('run') || '', runs: [], overview: {}, status: {}, report: null, reportState: 'not_loaded',
    view: 'overview', programTab: 'programs', measurementWindow: 'full_session', generation: 0,
    poll: true, coreBusy: null, resourcesBusy: null, resourceItems: [], resourceCursor: null,
    resourceMore: false, resourceScope: {}, resourceCompleteness: {}, resourceLoaded: false, resourceHistory: false,
    pages: Object.fromEntries(['events', 'programs', 'jobs', 'chat', 'observer'].map(key => [key, pageState()])),
    drawerGeneration: 0, drawerFocus: null, pendingControl: null, controlCommand: null, lastError: '', stageLimit: 20, figuresAt: 0
  };
  const node = (tag, text, className) => {
    const result = document.createElement(tag);
    if (text !== undefined && text !== null) result.textContent = String(text);
    if (className) result.className = className;
    return result;
  };
  const obj = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const arr = value => Array.isArray(value) ? value : [];
  const num = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
  const label = value => LABELS[value] || (value == null || value === '' ? '未知' : String(value));
  const number = (value, digits = 0) => num(value) === null ? '未知' : value.toLocaleString('zh-CN', {maximumFractionDigits: digits});
  const money = value => num(value) === null ? '未知' : '$' + value.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 4});
  const short = (value, length = 120) => value == null ? '未知' : String(value).slice(0, length) + (String(value).length > length ? '…' : '');
  const jsonText = value => { try { return JSON.stringify(value, null, 2); } catch { return '无法显示该记录'; } };
  function time(value, full = false) {
    if (value == null || value === '') return '未知';
    const date = new Date(typeof value === 'number' && value < 1e11 ? value * 1000 : value);
    if (!Number.isFinite(date.getTime())) return short(value, 36);
    return date.toLocaleString('zh-CN', full ? {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false} : {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  }
  function duration(value) {
    if (num(value) === null) return '未知';
    const seconds = Math.max(0, value);
    if (seconds > 0 && seconds < .001) return '< 1 毫秒';
    if (seconds > 0 && seconds < 1) return number(seconds * 1000, 1) + ' 毫秒';
    if (seconds < 60) return number(seconds, seconds < 10 ? 1 : 0) + ' 秒';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' 分 ' + Math.floor(seconds % 60) + ' 秒';
    return Math.floor(seconds / 3600) + ' 小时 ' + Math.floor(seconds % 3600 / 60) + ' 分';
  }
  function bytes(value) {
    if (num(value) === null) return '未知';
    if (value >= 1024 ** 3) return number(value / 1024 ** 3, 2) + ' GiB';
    if (value >= 1024 ** 2) return number(value / 1024 ** 2, 1) + ' MiB';
    return number(value / 1024, 1) + ' KiB';
  }
  const percent = value => num(value) === null ? '未知' : number(value * 100, 1) + '%';
  function tone(value) {
    if (['failed', 'infra_failed', 'model_failed', 'contradicted', 'timed_out', 'error', 'bad', 'rejected'].includes(value)) return 'bad';
    if (['running', 'succeeded', 'success', 'capability_success', 'verified', 'applied', 'completed', 'good'].includes(value)) return 'good';
    if (['warning', 'paused', 'frozen', 'interrupted', 'budget_exhausted', 'player_claim', 'diagnostic_pass', 'insufficient_evidence', 'degraded'].includes(value)) return 'warning';
    return 'neutral';
  }
  function badge(text, style = 'neutral') { return node('span', text, 'badge ' + style); }
  function actorAvatar(actor) {
    const name = String(actor || 'system');
    const color = [...name].reduce((total, char) => total + char.charCodeAt(0), 0) % 4;
    return node('span', name === 'observer' ? 'OB' : name.replace(/^player/i, 'P').slice(0, 3).toUpperCase(), 'avatar color-' + color);
  }
  function actorCell(actor) {
    const cell = node('div', null, 'actor-cell');
    cell.append(actorAvatar(actor), node('span', actor || '系统', 'actor-label'));
    return cell;
  }
  function empty(container, title, description, columns) {
    const body = node('div', null, 'empty-state');
    body.append(node('div', '—', 'empty-symbol'), node('strong', title));
    if (description) body.append(node('p', description));
    if (columns) {
      const row = node('tr'), cell = node('td');
      cell.colSpan = columns; cell.append(body); container.replaceChildren(row); row.append(cell);
    } else container.replaceChildren(body);
  }
  function metric(container, title, value, unit, foot, accent = false) {
    const card = node('article', null, 'metric-card' + (accent ? ' accent' : ''));
    const heading = node('div', title, 'metric-label');
    const amount = node('div', value, 'metric-value' + (value === '未知' ? ' value-unknown' : ''));
    if (unit && value !== '未知') amount.append(node('small', unit));
    card.append(heading, amount, node('p', foot, 'metric-foot')); container.append(card);
  }
  function facts(container, entries, className = 'metadata-grid') {
    const list = node('dl', null, className);
    for (const [title, value] of entries) {
      const group = node('div'); group.append(node('dt', title), node('dd', value == null || value === '' ? '未知' : value)); list.append(group);
    }
    container.replaceChildren(...list.children);
  }
  function errorMessage(error) {
    if (error?.name === 'AbortError') return '读取超时，请重试或缩小筛选范围。';
    return short(error?.message || error || '请求失败', 240);
  }
  function reportError(error) {
    const message = errorMessage(error);
    $('error-message').textContent = message; $('error-banner').hidden = false; state.lastError = message;
  }
  let toastTimer;
  function toast(message) {
    $('toast').textContent = message; $('toast').hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
  }
  async function api(path, parameters = {}, options = {}) {
    const url = new URL(path, location.origin);
    if (state.run && !['/api/runs', '/api/control'].includes(path)) url.searchParams.set('run', state.run);
    for (const [key, value] of Object.entries(parameters)) if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, String(value));
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(url, {...options, signal: controller.signal, credentials: 'same-origin', cache: 'no-store'});
      const document = await response.json();
      if (!response.ok) { const error = new Error(document.error || '请求失败（HTTP ' + response.status + '）'); error.status = response.status; throw error; }
      return document;
    } finally { clearTimeout(timeout); }
  }
  function runInfo() {
    return {...(state.runs.find(run => run.id === state.run) || {}), ...obj(state.overview.run)};
  }
  function setConnection(kind, text) {
    $('connection').className = 'connection ' + kind;
    $('connection').lastElementChild.textContent = text;
  }
  async function loadRuns() {
    let cursor = null, catalog = null, first = null, seen = new Set(), discoveryTruncated = false, unavailable = 0;
    state.runs = [];
    do {
      catalog = await api('/api/runs', {cursor, limit: 100}); first = first || catalog;
      discoveryTruncated = discoveryTruncated || catalog.discovery_truncated === true;
      unavailable = Math.max(unavailable, num(catalog.unavailable_runs) ?? 0);
      for (const run of arr(catalog.runs)) if (!seen.has(run.id)) { seen.add(run.id); state.runs.push(run); }
      if (!catalog.has_more || catalog.next_cursor == null || String(catalog.next_cursor) === String(cursor)) break;
      cursor = catalog.next_cursor;
    } while (state.runs.length < 1000);
    state.runs = state.runs.slice(0, 1000);
    const issues = [];
    if (catalog?.has_more === true) issues.push('页面实验目录显示上限 1,000 项');
    if (discoveryTruncated) issues.push('目录扫描触及服务端时间或数量边界，发现结果可能不完整');
    if (unavailable) issues.push(number(unavailable) + ' 个实验目录暂不可读');
    state.catalogNotice = issues.join('；'); state.catalogTruncated = issues.length > 0;
    if (!state.runs.some(run => run.id === state.run)) state.run = first?.selected_run_id || state.runs[0]?.id || '';
    const select = $('run-select'); select.replaceChildren();
    for (const run of state.runs) {
      const title = run.name && run.name !== run.id ? run.name + ' · ' + run.id : run.id;
      const option = node('option', title + (run.active ? ' · 运行中' : '') + (run.diagnostic_only ? ' · 诊断' : '')); option.value = run.id; option.title = run.id; select.append(option);
    }
    if (!state.runs.length) { const option = node('option', '当前实验'); option.value = state.run; select.append(option); }
    select.value = state.run;
    select.title = state.run;
    if (state.catalogNotice) toast(state.catalogNotice + '。');
  }
  function renderChrome() {
    const overview = state.overview, info = runInfo();
    $('run-select').title = state.run;
    const status = typeof overview.status === 'string' ? overview.status : state.status.status;
    $('run-status').textContent = label(status); $('run-status').className = 'badge ' + tone(status);
    $('profile-badge').textContent = label(info.profile); $('profile-badge').hidden = !info.profile;
    $('diagnostic-badge').hidden = info.diagnostic_only !== true;
    $('nav-event-count').textContent = number(overview.counts?.events ?? state.status.event_count);
    $('last-updated').textContent = '同步于 ' + time(Date.now());
    if (state.poll) setConnection('online', '实时同步'); else setConnection('paused', '刷新已暂停');
    const high = overview.scope?.ledger_through_event_seq;
    const diagnostic = info.diagnostic_only ? '诊断数据不代表真实游戏能力。' : '';
    $('scope-text').textContent = diagnostic + '总览基于全账本聚合' + (high != null ? '（截至事件 #' + high + '）' : '') + '；列表有界分页。未知值不按零计算。' + (state.catalogNotice ? state.catalogNotice + '。' : '');
    for (const [id, format] of [['report-download-json', 'json'], ['report-download-md', 'md']]) { const url = new URL('/download/report.' + format, location.origin); if (state.run) url.searchParams.set('run', state.run); $(id).href = url; }
    document.title = (info.name || '实验') + ' · 实验观测台';
    populateHud();
    populateActors(); updateControlFeedback();
  }
  function populateHud() {
    const overview = state.overview, info = runInfo();
    const status = typeof overview.status === 'string' ? overview.status : state.status.status;
    const budget = obj(state.status.budget || overview.budget), costs = obj(overview.costs);
    const observer = obj(overview.observer), counts = obj(overview.counts);
    const set = (id, text, tone) => { const element = $(id); if (!element) return; element.textContent = text; element.className = 'hud-value' + (tone ? ' ' + tone : ''); };
    set('hud-status', label(status), tone(status));
    set('hud-time', duration(overview.active_wall_seconds ?? state.status.active_wall_seconds ?? state.report?.metrics?.wall_time_s));
    set('hud-programs', number(counts.programs) + ' 次提交');
    const calls = num(budget.calls), cost = num(costs.cost_usd) ?? num(budget.cost_usd);
    set('hud-model', number(calls) + ' 次 · ' + money(cost));
    const observerText = observer.degraded ? '已降级' : counts.observer_reports ? number(counts.observer_reports) + ' 份报告' : (observer.outcome?.status ? label(observer.outcome.status) : '等待审阅');
    set('hud-observer', observerText, observer.degraded ? 'bad' : counts.observer_reports ? 'good' : null);
  }

  function populateActors() {
    const actors = new Set(arr(state.overview.actors).map(value => value.actor_id).filter(Boolean));
    arr(runInfo().players).forEach(value => actors.add(value));
    for (const select of document.querySelectorAll('.actor-select')) {
      const selected = select.value;
      for (const option of [...select.options].slice(1)) option.remove();
      for (const actor of [...actors].sort()) { const option = node('option', actor); option.value = actor; select.append(option); }
      if (selected && !actors.has(selected)) { const option = node('option', selected); option.value = selected; select.append(option); }
      select.value = selected;
    }
  }
  async function loadCore() {
    const generation = state.generation;
    if (state.coreBusy === generation) return;
    state.coreBusy = generation;
    try {
      const results = await Promise.allSettled([api('/api/overview'), api('/api/status')]);
      if (generation !== state.generation) return;
      if (results[0].status === 'rejected' && results[1].status === 'rejected') throw results[0].reason;
      if (results[1].status === 'fulfilled') state.status = obj(results[1].value);
      if (results[0].status === 'fulfilled') state.overview = obj(results[0].value);
      else state.overview = {status: state.status.status, budget: state.status.budget, resources: state.status.resources, evaluation: state.status.evaluation, counts: {events: state.status.event_count}, costs: {cost_usd: null}};
      renderChrome(); renderOverview(); renderResources(); renderEvaluation(); renderObserverSummary();
      if (state.view === 'live') renderLive();
    } catch (error) { if (generation === state.generation) { setConnection('offline', '连接异常'); reportError(error); } }
    finally { if (state.coreBusy === generation) state.coreBusy = null; }
  }
  function renderOverview() {
    const o = state.overview, counts = obj(o.counts), info = runInfo(), costs = obj(o.costs);
    const metrics = $('overview-metrics'); metrics.replaceChildren();
    const elapsed = o.active_wall_seconds ?? state.status.active_wall_seconds ?? state.report?.metrics?.wall_time_s;
    metric(metrics, '实验运行时长', duration(elapsed), '', '实际运行时间 · 停服时间单独记录', true);
    metric(metrics, '程序提交', number(counts.programs), '次', '以持久提交账本为准');
    metric(metrics, '已结算模型费用', money(costs.cost_usd), '', num(costs.cost_usd) === null ? '已知部分 ' + money(costs.known_cost_usd) + ' · 总额仍未知' : '玩家、Observer 与交接调用合计');
    metric(metrics, '已记录事件', number(counts.events ?? state.status.event_count), '条', '全账本统计 · 列表按需分页');
    const health = $('health-items'); health.replaceChildren();
    const observer = obj(o.observer), active = state.status.supervisor_active;
    healthRow(health, 'Supervisor', active === true ? '在线' : active === false ? '已退出' : '未知', '持久控制队列由 Supervisor 处理', active === true ? 'good' : 'neutral');
    healthRow(health, '实验评估', evaluationTitle(o.evaluation), '确定性 Evaluator 的已记录结果', o.evaluation?.capability_success === true ? 'good' : 'neutral');
    healthRow(health, 'Observer', observer.degraded ? '已降级' : observer.outcome?.status ? label(observer.outcome.status) : counts.observer_reports > 0 ? '已提交审阅' : '尚无审阅', '独立审阅不替代成功判定', observer.degraded ? 'warning' : 'neutral');
    const gaps = Object.values(obj(o.evidence?.gaps)).reduce((total, value) => total + (num(value) || 0), 0);
    healthRow(health, '证据记录', gaps ? number(gaps) + ' 项缺口事件' : '完整性待核验', '没有缺口事件不等于证据完整', gaps ? 'warning' : 'neutral');
    $('health-indicator').style.backgroundColor = ['infra_failed', 'model_failed'].includes(o.status) ? '#b46d5d' : '#91a57e';
    renderActors();
    const recent = $('recent-activity'); recent.replaceChildren();
    for (const event of arr(o.recent_activity).slice(0, 5)) {
      const item = node('div', null, 'activity-item'); item.tabIndex = 0; item.setAttribute('role', 'button');
      const content = node('div'); content.append(node('div', eventName(event), 'activity-title'), node('div', (event.actor_id || '系统') + ' · ' + eventSummary(event), 'activity-description'));
      item.append(node('span', null, 'activity-bullet ' + (event.severity === 'error' ? 'bad' : event.severity === 'info' ? 'good' : '')), content, node('time', time(event.received_at), 'activity-time'));
      clickable(item, () => showEvent(event)); recent.append(item);
    }
    if (!recent.children.length) empty(recent, '暂无动态', '产生事件后，最近动态会显示在这里。');
    facts($('run-metadata'), [['运行名称', info.name || state.run], ['运行 ID', info.run_id], ['实验模式', label(info.profile)], ['开始记录', time(info.created_at, true)], ['世界 epoch', info.world_epoch ?? state.status.world_epoch], ['运行时 epoch', info.runtime_epoch ?? state.status.runtime_epoch], ['数据性质', info.diagnostic_only ? '合成／诊断数据' : info.diagnostic_only === false ? '真实环境运行' : '未知'], ['目标', info.goal || state.report?.task || '未提供目标']]);
    drawResourceCharts();
  }
  function healthRow(container, title, value, description, style) {
    const row = node('div', null, 'health-row'), copy = node('div', null, 'health-copy');
    copy.append(node('strong', title), node('small', description));
    row.append(node('span', style === 'good' ? '✓' : style === 'warning' ? '!' : '·', 'health-icon ' + style), copy, node('span', value, 'health-value ' + style)); container.append(row);
  }
  function renderActors() {
    const table = $('actors-table'); table.replaceChildren();
    const players = arr(state.report?.paper_metrics?.full_session?.players), groups = arr(state.overview.costs?.groups);
    const actors = arr(state.overview.actors).filter(actor => actor.actor_id && !['system', 'server', 'supervisor'].includes(actor.actor_id));
    for (const actor of actors.slice(0, 16)) {
      const row = node('tr'), player = players.find(value => value.actor_id === actor.actor_id), costGroups = groups.filter(value => value.actor_id === actor.actor_id);
      const programCount = player ? Object.values(obj(player.execution_status_counts)).reduce((sum, count) => sum + (num(count) || 0), 0) : null;
      const cell = node('td'); cell.append(actorCell(actor.actor_id));
      const statusCell = node('td'); statusCell.append(badge(actor.actor_id === 'observer' ? '审阅角色' : '已记录活动', 'neutral'));
      row.append(cell, node('td', number(programCount)), node('td', costGroups.length ? number(costGroups.reduce((sum, group) => sum + (num(group.calls) || 0), 0)) : '未知'), statusCell); table.append(row);
    }
    if (!table.children.length) empty(table, '尚无参与者活动', '参与者首次产生事件后会出现在这里。', 4);
  }
  function eventName(event) { return EVENTS[event.kind] || event.kind || '事件'; }
  function inner(event) { const payload = obj(event.payload); return Object.keys(obj(payload.payload)).length ? payload.payload : payload; }
  function eventSummary(event) {
    const p = inner(event), outer = obj(event.payload);
    const text = p.message ?? p.reason ?? p.error?.message ?? p.error ?? p.claim ?? p.command ?? p.status ?? p.tool ?? outer.tool;
    if (typeof text === 'string') return short(text, 170);
    if (event.kind === 'observer_report') return number(arr(p.findings).length) + ' 条审阅结论';
    if (event.kind === 'model_usage') return label(p.purpose) + ' · 输入 ' + number(p.normalized_usage?.input_tokens ?? p.usage?.input_tokens) + ' / 输出 ' + number(p.normalized_usage?.output_tokens ?? p.usage?.output_tokens);
    if (p.execution_id || event.execution_id) return short(p.execution_id || event.execution_id, 64);
    if (p.job_id || p.job?.job_id) return short(p.job_id || p.job.job_id, 64);
    if (outer.preview) return '内容较长，打开详情分页读取';
    const names = Object.keys(p).filter(key => !['operator_only', 'schema_version'].includes(key)).slice(0, 4);
    return names.length ? names.join(' · ') : '查看原始记录与关联证据';
  }
  function clickable(element, action) {
    element.addEventListener('click', action);
    element.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); action(); } });
  }
  function formFilters(name) {
    const form = $(name === 'jobs' ? 'programs-filter' : name + '-filter');
    return form ? Object.fromEntries(new FormData(form).entries()) : {};
  }
  function itemKey(name, item) {
    if (name === 'chat') return item.event_seq + ':' + (item.item_index ?? 0) + ':' + (item.actor_id || '');
    if (name === 'programs') return item.actor_id + ':' + (item.execution_id || item.request_id);
    if (name === 'jobs') return item.actor_id + ':' + item.job_id;
    return String(item.event_seq);
  }
  async function loadPage(name, {reset = false, refresh = false} = {}) {
    const page = state.pages[name];
    if (reset) page.revision++;
    if (page.busy) { if (reset) page.queuedReset = true; return; }
    const revision = page.revision; page.queuedReset = false;
    page.busy = true; const generation = state.generation;
    const parameters = {...page.filters, limit: name === 'observer' ? 30 : 50};
    if (name === 'events') {
      parameters.recent = 1;
      if (refresh && page.items.length) parameters.after = Math.max(...page.items.map(item => Number(item.event_seq) || 0));
      else if (!reset && page.cursor !== null) parameters.before = page.cursor;
    } else if (!reset && !refresh && page.cursor !== null) parameters.cursor = page.cursor;
    const button = $(name === 'jobs' ? 'programs-more' : name + '-more'); if (button) button.disabled = true;
    try {
      const result = await api('/api/' + name, parameters);
      if (generation !== state.generation || revision !== page.revision) return;
      const incoming = arr(result.items || result.events);
      if (reset) page.items = [];
      const merged = new Map();
      for (const item of [...incoming, ...page.items]) if (!merged.has(itemKey(name, item))) merged.set(itemKey(name, item), item);
      page.items = [...merged.values()];
      page.items.sort((first, second) => {
        if (name === 'programs') return Number(second.cursor ?? second.last_event_seq ?? 0) - Number(first.cursor ?? first.last_event_seq ?? 0);
        const order = (second.event_seq ?? second.last_event_seq ?? 0) - (first.event_seq ?? first.last_event_seq ?? 0);
        return order || (name === 'chat' ? (second.item_index ?? 0) - (first.item_index ?? 0) : 0);
      });
      if (page.items.length > 1000) { page.items.length = 1000; toast('浏览器保留最近 1,000 条记录；请缩小筛选范围查看更早数据。'); }
      if (!refresh || !page.loaded) { page.cursor = result.next_cursor ?? null; page.hasMore = result.has_more ?? incoming.length === 50; }
      page.scope = obj(result.scope); page.completeness = obj(result.completeness); page.loaded = true;
      renderPage(name);
    } catch (error) { if (generation === state.generation && revision === page.revision) reportError(error); }
    finally {
      page.busy = false;
      if (generation === state.generation && button && (!['programs', 'jobs'].includes(name) || state.programTab === name)) { button.disabled = !page.hasMore || page.items.length >= 1000; button.hidden = !page.hasMore; }
      if (generation === state.generation && page.queuedReset) { page.queuedReset = false; loadPage(name, {reset: true}); }
    }
  }
  function scopeLabel(page) {
    const parts = ['已加载 ' + number(page.items.length) + ' 条'];
    if (page.scope.ledger_through_event_seq != null) parts.push('账本截至 #' + page.scope.ledger_through_event_seq);
    if (page.completeness.history_complete === false) parts.push('历史范围受采集窗口限制');
    if (page.completeness.payload_previews) parts.push('长内容请打开详情');
    if (page.items.length >= 1000) parts.push('浏览器显示上限 1,000 条');
    return parts.join(' · ');
  }
  function renderPage(name) {
    if (name === 'events') renderEvents();
    else if (name === 'programs' || name === 'jobs') { if (state.programTab === name) renderPrograms(); }
    else if (name === 'chat') renderChat();
    else if (name === 'observer') renderObserver();
  }
  function renderEvents() {
    const page = state.pages.events, table = $('events-table'); table.replaceChildren();
    $('events-count').textContent = '事件记录 · ' + number(page.items.length) + ' 条已加载';
    $('events-scope').textContent = scopeLabel(page);
    for (const event of page.items) {
      const row = node('tr'); row.tabIndex = 0; row.setAttribute('aria-label', '#' + event.event_seq + ' ' + eventName(event));
      const clock = node('td'); clock.append(node('span', '#' + event.event_seq, 'seq-label'), node('time', time(event.received_at), 'time-label'));
      const actor = node('td'); actor.append(actorCell(event.actor_id));
      const kind = node('td'); kind.append(node('strong', eventName(event), 'event-kind'), node('small', event.kind, 'event-subtitle'));
      const summary = node('td'); summary.append(node('span', eventSummary(event), 'cell-preview'));
      const source = node('td'); source.append(badge(label(event.source), event.source === 'agent_claim' ? 'warning' : 'neutral'));
      row.append(clock, actor, kind, summary, source, node('td', '↗', 'row-arrow'));
      clickable(row, () => showEvent(event)); table.append(row);
    }
    if (!table.children.length) empty(table, '没有匹配的事件', '请调整参与者、类别、级别或搜索条件。筛选在服务端分页前执行。', 6);
  }
  function renderPrograms() {
    const jobs = state.programTab === 'jobs', page = state.pages[state.programTab];
    const head = node('tr');
    for (const text of jobs ? ['任务 / 创建程序', '参与者', '最近记录状态', '开始时间', '期限', ''] : ['程序 / 请求', '参与者', '状态', '提交时间', '执行耗时', '']) head.append(node('th', text));
    $('programs-head').replaceChildren(head); $('programs-table').replaceChildren();
    $('programs-caption').textContent = jobs ? '受监管后台任务 · 最近已记录状态' : '前台程序 · 持久提交账本';
    $('programs-scope').textContent = scopeLabel(page) + (jobs ? ' · 记录为运行中不保证当前仍存活' : '');
    $('program-count').textContent = state.overview.counts?.programs != null ? number(state.overview.counts.programs) : '';
    $('job-count').textContent = state.pages.jobs.loaded ? state.pages.jobs.items.length + '+' : '';
    for (const item of page.items) {
      const row = node('tr'); row.tabIndex = 0;
      const identifier = jobs ? item.job_id : item.execution_id || item.request_id;
      const id = node('td'); id.append(node('strong', short(identifier, 30), 'cell-id'), node('small', short(jobs ? item.execution_id : item.request_id, 34), 'event-subtitle')); id.title = identifier || '';
      const actor = node('td'); actor.append(actorCell(item.actor_id));
      const status = node('td'); status.append(badge(label(item.status), tone(item.status)));
      row.append(id, actor, status, node('td', time(jobs ? item.started_at : item.submitted_at || item.accepted_at, true), 'time-label'), node('td', jobs ? duration(num(item.ttl_ms) === null ? null : item.ttl_ms / 1000) : duration(num(item.duration_ms) === null ? null : item.duration_ms / 1000)), node('td', '↗', 'row-arrow'));
      row.setAttribute('aria-label', label(item.status) + ' ' + identifier);
      clickable(row, () => jobs ? showJob(item) : showProgram(item)); $('programs-table').append(row);
    }
    if (!$('programs-table').children.length) empty($('programs-table'), jobs ? '暂无匹配的后台任务' : '暂无匹配的程序', jobs ? '只有 Agent 显式登记的任务会在前台程序结束后继续运行。' : '程序提交后会显示状态、源码和原始回执。', 6);
    $('programs-more').hidden = !page.hasMore; $('programs-more').disabled = page.busy || !page.hasMore;
  }
  function renderChat() {
    const page = state.pages.chat, feed = $('chat-feed'); feed.replaceChildren();
    $('chat-scope').textContent = scopeLabel(page);
    $('chat-caption').textContent = page.completeness.history_complete === false ? '有界近期窗口 · 不代表完整聊天历史' : '逐条标明服务端核验或客户端投递';
    for (const item of page.items) {
      const message = node('article', null, 'chat-message'), content = node('div', null, 'chat-content');
      const meta = node('div', null, 'chat-meta'); meta.append(node('strong', item.actor_id || '未知发言者', 'actor-label'), node('time', time(item.source_time || item.received_at, true)));
      const verified = ['server_normalized', 'server_event'].includes(item.verification);
      meta.append(badge(verified ? '服务端核验' : '客户端观察', verified ? 'good' : 'neutral'));
      content.append(meta, node('p', item.message || '消息内容未知', 'chat-text'));
      const footer = node('div', null, 'chat-footer'); footer.append(node('span', '世界 epoch ' + (item.world_epoch ?? '未知')));
      if (item.duplicate_deliveries) footer.append(node('span', '合并 ' + item.duplicate_deliveries + ' 次重复投递'));
      const detail = node('button', '事件 #' + item.event_seq + ' ↗', 'text-button'); detail.addEventListener('click', () => showEventById(item.event_seq)); footer.append(detail);
      content.append(footer); message.append(actorAvatar(item.actor_id), content); feed.append(message);
    }
    if (!feed.children.length) empty(feed, '暂无匹配消息', '聊天通道关闭或没有聊天记录时，此处保持为空；不会生成示例对话。');
    $('chat-labels').replaceChildren(...['预警', '救援', '共享储物', '经验', '坐标'].map(text => node('span', text, 'tag')));
  }
  function evaluationTitle(evaluation) {
    if (!evaluation) return '尚无已记录评估';
    if (evaluation.diagnostic_only || evaluation.status === 'diagnostic_pass') return '诊断结果';
    if (evaluation.capability_success === true) return '任务成功已核验';
    return label(evaluation.status || 'unknown');
  }
  async function loadReport() {
    if (['loading', 'loaded', 'too_large'].includes(state.reportState)) return;
    state.reportState = 'loading'; const generation = state.generation;
    try {
      let offset = 0, text = '', result;
      do {
        result = await api('/api/report', {format: 'json', offset, limit: 64000});
        if (generation !== state.generation) return;
        if (result.available === false) { state.reportState = 'unavailable'; renderEvaluation(); return; }
        text += result.content || '';
        if (text.length > 512000) { state.reportState = 'too_large'; renderEvaluation(); return; }
        offset = result.next_offset;
      } while (offset !== null && offset !== undefined);
      state.report = obj(JSON.parse(text)); state.reportState = 'loaded';
      renderEvaluation(); renderActors(); renderResources();
    } catch (error) { if (generation === state.generation) { state.reportState = 'unavailable'; renderEvaluation(); reportError(error); } }
  }
  function measurement() { return state.report?.paper_metrics?.[state.measurementWindow] || null; }
  function renderEvaluation() {
    const evaluation = state.overview.evaluation || state.report?.evaluation || state.status.evaluation;
    const summary = $('evaluation-summary'); summary.replaceChildren();
    const verified = evaluation?.capability_success === true && !runInfo().diagnostic_only && !evaluation.diagnostic_only;
    const icon = node('div', verified ? '✓' : evaluation?.status === 'diagnostic_pass' || runInfo().diagnostic_only ? '◇' : '—', 'evaluation-icon');
    const copy = node('div'); copy.append(node('h2', evaluationTitle(evaluation)));
    const description = verified ? '已由确定性评估器基于服务端证据核验。首次成功时间为首次已核验快照的观测时间，是达到目标时间的上界。' : runInfo().diagnostic_only ? '本次使用合成运行时或诊断模型。诊断通过用于验证框架行为，不证明真实 Minecraft 任务能力。' : '任务结果只读取已持久化评估。Agent 或 Observer 的成功声明不能单独建立成功。';
    copy.append(node('p', description)); summary.append(icon, copy, badge(verified ? '已核验' : '保留未知', verified ? 'good' : 'neutral'));
    const criteria = $('evaluation-criteria'); criteria.replaceChildren();
    for (const criterion of arr(evaluation?.criteria)) {
      const row = node('div', null, 'criterion-row'), text = node('div');
      text.append(node('strong', criterion.name || '未命名标准'), node('p', '观测增量 ' + number(criterion.observed_delta) + ' / 要求增量 ' + number(criterion.required_delta) + ' · 方法 ' + (criterion.method || '未知')));
      row.append(text, badge(criterion.passed === true ? '已满足' : criterion.passed === false ? '尚未满足' : '未知', criterion.passed === true ? 'good' : 'neutral')); criteria.append(row);
    }
    if (evaluation?.reason) criteria.append(node('p', evaluation.reason, 'criteria-note'));
    for (const warning of arr(evaluation?.warnings).slice(0, 10)) criteria.append(node('p', typeof warning === 'string' ? warning : jsonText(warning), 'criteria-note'));
    if (arr(evaluation?.evidence_refs).length) { const refs = node('div', null, 'criterion-references'); arr(evaluation.evidence_refs).slice(0, 20).forEach(ref => refs.append(referenceButton(ref))); criteria.append(refs); }
    if (!criteria.children.length) empty(criteria, '尚无可显示的规则或证据', '诊断任务与未完成评估不会生成虚构的游戏进度。');
    const window = measurement(), full = state.report?.paper_metrics?.full_session;
    $('window-note').textContent = state.reportState === 'loaded' ? '统计来自已保存报告，非每次页面刷新重新评估。' : state.reportState === 'loading' ? '正在分页读取已保存报告…' : state.reportState === 'too_large' ? '报告超过页面解析上限，可打开原始报告分页查看。' : '尚无可用的已保存报告；不会自动生成或补齐统计。';
    const metrics = $('evidence-metrics'); metrics.replaceChildren();
    let stages = arr(state.report?.paper_metrics?.stage_timeline);
    if (state.measurementWindow === 'pre_first_success') stages = window ? stages.filter(stage => num(stage.source_time) === null || window.end_time != null && stage.source_time <= window.end_time) : [];
    const stageOmitted = state.report?.paper_metrics?.stage_timeline_omitted;
    const serverComplete = window?.server_evidence_complete ?? window?.complete;
    metric(metrics, '统计窗口时长', duration(window?.duration_s), '', '从首个可信程序开始计时');
    metric(metrics, '服务端死亡记录', number(serverComplete === true && window?.authoritative_server_counts ? window.authoritative_server_counts.death ?? 0 : null), '次', '证据缺失时不按零计算');
    metric(metrics, '报告中可见阶段', state.report && window ? number(stages.length) : '未知', '项', stageOmitted ? '报告另有 ' + number(stageOmitted) + ' 项省略，当前计数不是全量' : '只显示有直接证据的阶段');
    const timeline = $('stage-timeline'); timeline.replaceChildren();
    const visibleStages = Math.min(stages.length, Math.max(20, state.stageLimit));
    for (const stage of stages.slice(0, visibleStages)) {
      const item = node('div', null, 'stage-item'); item.tabIndex = 0; item.setAttribute('role', 'button');
      item.append(node('strong', stage.stage || '未知阶段'), node('p', (stage.actor_id || '未知参与者') + ' · ' + time(stage.source_time, true) + ' · 事件 #' + stage.event_seq));
      clickable(item, () => showEventById(stage.event_seq)); timeline.append(item);
    }
    if (!timeline.children.length) empty(timeline, '暂无可核验阶段', '没有直接证据的阶段保持未知，不使用模型叙述补齐。');
    if (stages.length > visibleStages) {
      const more = node('button', '展开更多阶段（剩余 ' + number(stages.length - visibleStages) + ' 项）', 'button button-secondary');
      more.type = 'button';
      more.addEventListener('click', () => { state.stageLimit = Math.min(stages.length, visibleStages + 40); renderEvaluation(); });
      timeline.append(more);
    }
    if (stages.length > visibleStages || stageOmitted) timeline.append(node('p', '页面显示 ' + Math.min(stages.length, visibleStages) + ' / ' + stages.length + ' 项可见阶段；报告另有 ' + number(stageOmitted ?? 0) + ' 项省略。', 'data-hint'));
    const integrity = $('evidence-integrity'); integrity.replaceChildren();
    integrityRow(integrity, '服务端日志覆盖', serverComplete === true ? '完整' : serverComplete === false ? '存在缺口' : '未知', '按已登记原始字节核验，不以事件名替代完整性。', serverComplete === true ? 'good' : 'warning');
    integrityRow(integrity, '统计窗口', state.measurementWindow === 'full_session' ? '完整会话' : '首次成功前', state.measurementWindow === 'pre_first_success' && !window ? '尚无已核验首次成功，窗口保持未知。' : '首次成功后继续运行时，两个窗口分别统计。', window ? 'neutral' : 'warning');
    const incomplete = obj(window?.incomplete || full?.incomplete || state.overview.evidence?.gaps);
    for (const [name, count] of Object.entries(incomplete).slice(0, 12)) integrityRow(integrity, short(name, 90), number(count), '原始记录中的完整性标记', 'warning');
    if (!window && !Object.keys(incomplete).length) integrityRow(integrity, '报告状态', state.reportState === 'loaded' ? '窗口未知' : '报告未就绪', '页面不触发评估或修改记录。', 'neutral');
    const table = $('evidence-players'); table.replaceChildren();
    for (const player of arr(window?.players)) {
      const row = node('tr'), actor = node('td'); actor.append(actorCell(player.actor_id));
      row.append(actor, node('td', duration(player.program_duration_s?.p50) + ' / ' + duration(player.program_duration_s?.p95)), node('td', percent(player.active_fraction) + ' / ' + percent(player.combined_active_fraction)), node('td', number(player.error_count) + ' / ' + number(player.timeout_count)), node('td', number(player.death_count_complete ? player.death_count : null))); table.append(row);
    }
    if (!table.children.length) empty(table, '当前统计窗口暂无报告数据', '打开原始报告可查看完整口径与限制。', 5);
  }
  function integrityRow(container, title, value, description, style) {
    const row = node('div', null, 'integrity-item'), text = node('div');
    text.append(node('strong', title), node('p', description)); row.append(text, badge(value, style)); container.append(row);
  }
  function renderObserverSummary() {
    const summary = $('observer-summary'), observer = obj(state.overview.observer), outcome = obj(observer.outcome || state.status.observer_outcome);
    summary.replaceChildren();
    const mark = node('div', '◎', 'observer-mark'), text = node('div');
    text.append(node('h2', observer.degraded ? 'Observer 已降级' : '独立、只读的实验审阅'), node('p', observer.degraded ? '观察循环出现异常。框架状态与审阅结果分别保留。' : '无游戏操作、玩家笔记写入或预算提升权限；基础设施请求由 Supervisor 受限处理。'));
    summary.append(mark, text, badge(outcome.status ? label(outcome.status) : number(state.overview.counts?.observer_reports) + ' 次审阅', observer.degraded ? 'warning' : 'neutral'));
  }
  function renderObserver() {
    renderObserverSummary(); const page = state.pages.observer, feed = $('observer-feed'); feed.replaceChildren();
    $('observer-scope').textContent = scopeLabel(page);
    for (const event of page.items) {
      const report = node('article', null, 'observer-report card'), heading = node('div', null, 'observer-report-head');
      const details = node('button', '查看事件 #' + event.event_seq + ' ↗', 'text-button'); details.addEventListener('click', () => showEvent(event));
      heading.append(node('span', time(event.received_at, true) + ' · 世界 epoch ' + (event.world_epoch ?? '未知')), details); report.append(heading);
      const findings = arr(event.findings || inner(event).findings);
      for (const finding of findings) {
        const row = node('div', null, 'finding'); row.append(badge(label(finding.status), tone(finding.status)), node('p', finding.claim || '未提供结论文本'));
        const refs = node('div'); arr(finding.evidence_refs).slice(0, 20).forEach(ref => refs.append(referenceButton(ref))); row.append(refs); report.append(row);
      }
      if (event.findings_preview || findings.some(finding => finding.preview === true)) report.append(node('p', '当前为有界结构化预览；完整结论与全部引用请打开原始事件分页读取。', 'data-hint'));
      if (!findings.length) report.append(node('p', '本次审阅没有可显示的结构化结论；可打开原始事件核对。', 'data-hint'));
      feed.append(report);
    }
    if (!feed.children.length) empty(feed, '暂无独立 Observer 审阅', '规则评估与 LLM Observer 是两个独立组件。未启用 LLM Observer 时不会生成模型审阅。');
  }
  async function loadResources(append = false, resetLatest = false) {
    if (state.resourceHistory && !append && !resetLatest) return;
    const generation = state.generation; if (state.resourcesBusy === generation) return; state.resourcesBusy = generation;
    updateResourcePager();
    try {
      const result = await api('/api/resources', {limit: 100, cursor: append ? state.resourceCursor : undefined});
      if (generation !== state.generation) return;
      const records = new Map();
      for (const item of [...arr(result.items), ...(resetLatest ? [] : state.resourceItems)]) records.set(String(item.event_seq ?? item.sampled_at), item);
      state.resourceItems = [...records.values()].sort((a, b) => new Date(a.sampled_at) - new Date(b.sampled_at));
      if (state.resourceItems.length > 600) state.resourceItems = append ? state.resourceItems.slice(0, 600) : state.resourceItems.slice(-600);
      if (append) state.resourceHistory = true;
      if (resetLatest) state.resourceHistory = false;
      if (append || resetLatest || !state.resourceLoaded) { state.resourceCursor = result.next_cursor ?? null; state.resourceMore = result.has_more === true; }
      state.resourceLoaded = true; state.resourceScope = obj(result.scope); state.resourceCompleteness = obj(result.completeness);
      if (!state.overview.resources && result.latest) state.overview.resources = result.latest;
      renderResources(); drawResourceCharts();
    } catch (error) { if (generation === state.generation) reportError(error); }
    finally { if (state.resourcesBusy === generation) state.resourcesBusy = null; updateResourcePager(); }
  }
  function updateResourcePager() {
    $('resources-more').disabled = state.resourcesBusy === state.generation || !state.resourceMore;
    $('resources-latest').disabled = state.resourcesBusy === state.generation || !state.resourceHistory;
    $('resource-paging-scope').textContent = (state.resourceHistory ? '历史窗口' : '最近采样') + ' · ' + state.resourceItems.length + ' 个持久采样 · 最多 600 点';
  }
  function renderResources() {
    const o = state.overview, budget = obj(o.budget || state.status.budget), resources = obj(o.resources || state.status.resources), costs = obj(o.costs), storage = obj(o.storage || state.status.storage);
    const metrics = $('resource-metrics'); metrics.replaceChildren();
    metric(metrics, 'Python 内存', bytes(resources.python_rss_bytes), '', resources.sampled_at ? '最近采样 ' + time(resources.sampled_at) : '尚无当前采样', true);
    metric(metrics, '模型调用', number(budget.calls), '次', '包括玩家、Observer 与上下文交接');
    metric(metrics, '已结算费用', money(costs.cost_usd), '', costs.unknown_cost_calls ? number(costs.unknown_cost_calls) + ' 次调用费用未知' : '按冻结价格表与实际 usage 结算');
    const pending = budget.pending && typeof budget.pending === 'object' ? Object.values(budget.pending) : null;
    const reserved = pending && pending.every(item => num(item.reserved_cost) !== null) ? pending.reduce((total, item) => total + item.reserved_cost, 0) : null;
    metric(metrics, '并发预留', number(pending ? pending.length : null), '次', '预留金额 ' + money(reserved));
    const bars = $('budget-bars'); bars.replaceChildren();
    const limits = obj(o.budget_limits || budget.limits || state.status.budget_limits);
    budgetBar(bars, '模型调用', budget.calls, limits.total_model_calls, '次');
    budgetBar(bars, '已知费用', costs.known_cost_usd, limits.max_cost_usd, 'USD', '未知费用不视为零；预留与已结算分开。');
    budgetBar(bars, '有效运行时长', o.active_wall_seconds, num(limits.wall_time_minutes) === null ? null : limits.wall_time_minutes * 60, '秒');
    const unknown = num(budget.unknown_usage_calls);
    bars.append(node('p', '用量未知的调用：' + number(unknown) + ' 次。' + (Object.keys(limits).length ? '' : '当前接口未提供预算上限，页面不会推断剩余额度。'), 'data-hint'));
    const table = $('cost-table'); table.replaceChildren();
    const groups = arr(costs.groups).length ? costs.groups : arr(state.report?.paper_metrics?.full_session?.costs?.groups);
    for (const group of groups) {
      const row = node('tr'), actor = node('td'); actor.append(node('strong', group.actor_id || '未知参与者', 'actor-label'), node('small', label(group.role) + ' · ' + label(group.purpose), 'event-subtitle'));
      const cost = node('td', money(group.cost_usd ?? group.estimated_cost_usd));
      if (group.unknown_cost_calls) cost.append(node('small', number(group.unknown_cost_calls) + ' 次未知', 'event-subtitle'));
      row.append(actor, node('td', number(group.calls)), node('td', number(group.input_tokens) + ' / ' + number(group.cached_input_tokens)), node('td', number(group.output_tokens) + ' / ' + number(group.reasoning_tokens)), cost); table.append(row);
    }
    if (!table.children.length) empty(table, '暂无已结算用量', '模型调用完成结算后，按角色和用途显示。未知费用不会显示为免费。', 5);
    facts($('storage-metadata'), [['运行目录', state.status.run_dir || state.run], ['运行目录已测量', bytes(storage.measured_run_bytes ?? storage.total_bytes ?? storage.bytes ?? resources.run_bytes)], ['原始产物', bytes(storage.artifact_bytes)], ['工作区', bytes(storage.workspace_bytes ?? resources.workspace_bytes)], ['磁盘可用', bytes(storage.free_disk_bytes ?? storage.disk_free_bytes ?? resources.disk_free_bytes)], ['输入 token（累计）', number(budget.input_tokens)], ['输出 token（累计）', number(budget.output_tokens)], ['资源历史', state.resourceItems.length ? number(state.resourceItems.length) + ' 个持久采样' : '暂无持久时间序列']]);
    drawResourceCharts();
    updateResourcePager();
  }
  function budgetBar(container, title, value, maximum, unit, foot) {
    const group = node('div', null, 'budget-item'), heading = node('div', null, 'budget-label');
    const display = unit === 'USD' ? money(value) : number(value);
    heading.append(node('span', title), node('span', display + (num(maximum) !== null ? ' / ' + (unit === 'USD' ? money(maximum) : number(maximum)) : ' / 未知上限')));
    group.append(heading);
    if (num(value) !== null && num(maximum) !== null && maximum > 0) {
      const track = node('div', null, 'progress-track'), fraction = value / maximum, fill = node('div', null, 'progress-fill' + (fraction >= 1 ? ' bad' : fraction > .8 ? ' warning' : ''));
      fill.style.width = Math.min(100, Math.max(0, fraction * 100)) + '%'; track.append(fill); group.append(track);
    }
    if (foot) group.append(node('p', foot, 'budget-detail')); container.append(group);
  }
  function resourceSeries() {
    const records = [...state.resourceItems];
    const latest = state.overview.resources;
    if (!state.resourceHistory && latest?.sampled_at && !records.some(record => record.sampled_at === latest.sampled_at)) records.push(latest);
    return records.filter(record => num(record.python_rss_bytes) !== null && Number.isFinite(new Date(record.sampled_at).getTime())).sort((a, b) => new Date(a.sampled_at) - new Date(b.sampled_at)).slice(-600);
  }
  function drawResourceCharts() {
    const records = resourceSeries();
    drawChart($('overview-chart'), records); drawChart($('resource-chart'), records);
    $('overview-chart-empty').hidden = records.length > 0; $('resource-chart-empty').hidden = records.length > 0;
    const current = records.at(-1);
    $('activity-current').replaceChildren(node('span', current ? number(current.python_rss_bytes / 1048576, 1) : '—'), node('small', ' MiB'));
    $('activity-label').textContent = records.length ? (state.resourceHistory ? '历史窗口 · ' : '') + number(records.length) + ' 个采样' : '等待采样';
    const range = records.length ? time(records[0].sampled_at) + ' — ' + time(current.sampled_at) : '暂无采样';
    $('chart-range').textContent = range; $('resource-range').textContent = records.length ? time(records[0].sampled_at, true) + ' — ' + time(current.sampled_at, true) : '暂无采样';
    $('resource-chart-note').textContent = state.resourceCompleteness.history_available === false ? '仅存在最近状态，尚无持久化资源时间序列。单点不表示连续观测。' : state.resourceHistory ? '历史窗口不会混入新采样。继续向前加载时保留最早 600 点；返回最新可恢复实时图表。' : '当前显示 ' + records.length + ' 个已读取采样，最多保留 600 个点；采样之间不代表连续观测。';
  }
  function drawChart(canvas, records) {
    const bounds = canvas.getBoundingClientRect(); if (!bounds.width || !bounds.height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2); canvas.width = Math.round(bounds.width * ratio); canvas.height = Math.round(bounds.height * ratio);
    const context = canvas.getContext('2d'); if (!context) return; context.scale(ratio, ratio);
    const width = bounds.width, height = bounds.height, left = 43, right = width - 15, top = 17, bottom = height - 29;
    context.font = '10px "Minecraft Pixel", "Segoe UI", monospace';
    const values = records.map(record => record.python_rss_bytes / 1048576);
    let low = values.length ? Math.min(...values) : 0, high = values.length ? Math.max(...values) : 100;
    const margin = Math.max(1, (high - low) * .25); low = Math.max(0, low - margin); high += margin;
    context.font = '9px system-ui'; context.textBaseline = 'middle';
    for (let index = 0; index < 4; index++) {
      const y = top + (bottom - top) * index / 3;
      context.strokeStyle = '#c3c3c3'; context.lineWidth = 1; context.setLineDash([3, 4]); context.beginPath(); context.moveTo(left, y); context.lineTo(right, y); context.stroke();
      context.fillStyle = '#4f4f4f'; context.textAlign = 'right'; context.fillText(number(high - (high - low) * index / 3, 1), left - 9, y);
    }
    context.setLineDash([]); if (!records.length) return;
    const start = new Date(records[0].sampled_at).getTime(), end = new Date(records.at(-1).sampled_at).getTime();
    const points = records.map((record, index) => ({x: records.length === 1 ? (left + right) / 2 : left + (right - left) * ((new Date(record.sampled_at).getTime() - start) / Math.max(1, end - start)), y: top + (bottom - top) * (high - values[index]) / (high - low)}));
    if (points.length > 1) {
      const gradient = context.createLinearGradient(0, top, 0, bottom); gradient.addColorStop(0, '#86a66b29'); gradient.addColorStop(1, '#86a66b02');
      context.beginPath(); context.moveTo(points[0].x, bottom); points.forEach(point => context.lineTo(point.x, point.y)); context.lineTo(points.at(-1).x, bottom); context.closePath(); context.fillStyle = gradient; context.fill();
      context.beginPath(); points.forEach((point, index) => index ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y)); context.lineWidth = 1.7; context.strokeStyle = '#4f8a3d'; context.stroke();
    }
    const last = points.at(-1); context.fillStyle = '#4f8a3d'; context.beginPath(); context.arc(last.x, last.y, 3, 0, Math.PI * 2); context.fill();
    context.fillStyle = '#4f4f4f'; context.textAlign = 'left'; context.fillText(time(records[0].sampled_at), left, height - 9); context.textAlign = 'right'; context.fillText(time(records.at(-1).sampled_at), right, height - 9);
  }
  function openDrawer(title, eyebrow = 'EVIDENCE DETAIL') {
    state.drawerGeneration++; state.drawerFocus = document.activeElement;
    $('drawer-title').textContent = title; $('drawer-eyebrow').textContent = eyebrow;
    $('drawer-body').replaceChildren(); $('detail-drawer').hidden = false; $('drawer-backdrop').hidden = false;
    document.body.style.overflow = 'hidden'; $('drawer-close').focus();
    return state.drawerGeneration;
  }
  function closeDrawer() {
    state.drawerGeneration++; $('detail-drawer').hidden = true; $('drawer-backdrop').hidden = true;
    document.body.style.overflow = ''; state.drawerFocus?.focus?.();
  }
  function drawerSection(title) {
    const section = node('section', null, 'drawer-section'); if (title) section.append(node('h3', title)); $('drawer-body').append(section); return section;
  }
  function rawDetails(value, title = '原始结构化记录') {
    const details = node('details', null, 'raw-details'); details.append(node('summary', title), node('pre', jsonText(value))); return details;
  }
  function collectHashes(value) {
    const hashes = new Set(), pending = [value]; let count = 0;
    while (pending.length && count++ < 1000 && hashes.size < 16) {
      const item = pending.pop(); if (!item || typeof item !== 'object') continue;
      for (const [key, child] of Object.entries(item)) {
        if (['sha256', 'hash', 'artifact_sha256', 'source_hash', 'raw_sha256'].includes(key) && typeof child === 'string' && /^[a-f0-9]{64}$/.test(child)) hashes.add(child);
        else if (child && typeof child === 'object') pending.push(child);
      }
    }
    return [...hashes];
  }
  async function showEventById(sequence) {
    if (!/^\d+$/.test(String(sequence))) { toast('此引用没有可读取的事件编号。'); return; }
    const generation = openDrawer('事件 #' + sequence);
    drawerSection().append(node('p', '正在读取原始事件…', 'data-hint'));
    try { const detail = await api('/api/event/' + sequence); if (generation === state.drawerGeneration) renderEventDetail(detail.event, detail, generation); }
    catch (error) { if (generation === state.drawerGeneration) $('drawer-body').replaceChildren(node('p', errorMessage(error), 'data-hint')); }
  }
  async function showEvent(event) {
    const generation = openDrawer(eventName(event)); renderEventDetail(event, null, generation);
    if (event.event_seq == null) return;
    try { const detail = await api('/api/event/' + event.event_seq); if (generation === state.drawerGeneration) renderEventDetail(detail.event || event, detail, generation); }
    catch (error) { if (generation === state.drawerGeneration) drawerSection().append(node('p', '原始分页暂不可用：' + errorMessage(error), 'data-hint')); }
  }
  function renderEventDetail(event, detail, generation) {
    $('drawer-body').replaceChildren(); $('drawer-title').textContent = eventName(event);
    const section = drawerSection('事件来源'), list = node('dl', null, 'detail-facts');
    facts(list, [['事件编号', '#' + event.event_seq], ['事件类型', event.kind], ['参与者', event.actor_id || '系统'], ['来源', label(event.source)], ['接收时间', time(event.received_at, true)], ['执行 ID', event.execution_id], ['世界 epoch', event.world_epoch], ['运行时 epoch', event.runtime_epoch]], 'detail-facts'); section.append(list);
    drawerSection('内容摘要').append(node('p', eventSummary(event), 'detail-summary'));
    const hashes = [...new Set([...arr(detail?.artifact_refs), ...collectHashes(event.payload)])];
    if (hashes.length) { const artifacts = drawerSection('关联源码与产物'); hashes.slice(0, 16).forEach((hash, index) => artifacts.append(artifactCard(hash, '产物 ' + (index + 1), generation))); }
    const raw = drawerSection('原始内容');
    if (detail?.payload_page) raw.append(payloadPager(event.event_seq, detail.payload_page, generation));
    else raw.append(rawDetails(event.payload, event.payload_truncated ? '原始内容预览（已截断）' : '原始结构化内容'));
  }
  function payloadPager(sequence, firstPage, generation) {
    const details = node('details', null, 'raw-details'), title = node('summary', '原始 payload · 分页读取'); details.append(title);
    const content = node('pre', firstPage.content || ''), controls = node('div', null, 'artifact-tools'), status = node('span');
    const back = node('button', '上一页', 'button button-secondary'), next = node('button', '下一页', 'button button-secondary');
    let page = firstPage, offsets = [];
    const render = () => { content.textContent = page.content || ''; status.textContent = '字符偏移 ' + page.offset + ' / ' + (page.total_characters ?? '未知'); back.disabled = !offsets.length; next.disabled = page.next_offset == null; };
    async function read(offset, direction) {
      back.disabled = next.disabled = true;
      try { const result = await api('/api/event/' + sequence, {offset, limit: 32000}); if (generation !== state.drawerGeneration) return; if (direction === 'next') offsets.push(page.offset); page = result.payload_page; render(); }
      catch (error) { status.textContent = errorMessage(error); back.disabled = !offsets.length; next.disabled = page.next_offset == null; }
    }
    back.addEventListener('click', () => read(offsets.pop(), 'back')); next.addEventListener('click', () => read(page.next_offset, 'next'));
    const buttons = node('div'); buttons.append(back, next); controls.append(status, buttons); details.append(content, controls); render(); return details;
  }
  function artifactCard(hash, title, generation, autoOpen = false) {
    const card = node('section', null, 'artifact-card'); card.append(node('h4', title), node('p', hash, 'artifact-hash'));
    const open = node('button', '读取内容', 'button button-secondary'), content = node('pre', '', 'artifact-content'); content.hidden = true;
    const status = node('p', '按原始产物分页读取；页面不执行源码。', 'artifact-status'), tools = node('div', null, 'artifact-tools'); tools.hidden = true;
    const back = node('button', '上一页', 'button button-secondary'), next = node('button', '下一页', 'button button-secondary'), wrap = node('button', '自动换行', 'button button-quiet'), position = node('span');
    let page = null, history = [];
    async function read(offset, direction) {
      open.disabled = back.disabled = next.disabled = true; status.textContent = '正在校验并读取产物…';
      try {
        const result = await api('/api/artifact/' + encodeURIComponent(hash), {offset, limit: 16000}); if (generation !== state.drawerGeneration) return;
        if (direction === 'next' && page) history.push(page.offset);
        page = result; content.textContent = result.content || ''; content.hidden = false; tools.hidden = false; open.hidden = true;
        position.textContent = '字节偏移 ' + number(result.offset); back.disabled = !history.length; next.disabled = result.next_offset == null;
        status.textContent = result.next_offset == null ? '当前页已到达文件末尾。' : '还有后续内容，点击下一页读取。';
      } catch (error) { status.textContent = errorMessage(error); open.disabled = false; back.disabled = !history.length; next.disabled = !page || page.next_offset == null; }
    }
    open.addEventListener('click', () => read(0)); back.addEventListener('click', () => read(history.pop(), 'back')); next.addEventListener('click', () => read(page.next_offset, 'next'));
    wrap.addEventListener('click', () => { content.classList.toggle('wrap'); wrap.setAttribute('aria-pressed', String(content.classList.contains('wrap'))); });
    const buttons = node('div'); buttons.append(back, next, wrap); tools.append(position, buttons); card.append(open, content, tools, status);
    if (autoOpen) read(0); return card;
  }
  async function showProgram(item) {
    const generation = openDrawer('程序 ' + short(item.execution_id || item.request_id, 26), 'PROGRAM & RECEIPT');
    drawerSection().append(node('p', '正在读取程序与回执…', 'data-hint'));
    try {
      const result = await api('/api/programs/' + encodeURIComponent(item.execution_id || 'request:' + item.request_id), {actor: item.actor_id});
      if (generation !== state.drawerGeneration) return;
      $('drawer-body').replaceChildren(); const program = result.program || item;
      const heading = drawerSection('执行记录'), list = node('dl', null, 'detail-facts');
      facts(list, [['执行 ID', program.execution_id], ['请求 ID', program.request_id], ['参与者', program.actor_id], ['状态', label(program.status)], ['提交时间', time(program.submitted_at || program.accepted_at, true)], ['执行耗时', duration(num(program.duration_ms) === null ? null : program.duration_ms / 1000)], ['世界 epoch', program.world_epoch], ['运行时 epoch', program.runtime_epoch]], 'detail-facts'); heading.append(list);
      const receipts = drawerSection('程序回执');
      const receipt = obj(result.receipt), error = receipt.error || program.error;
      if (error) receipts.append(node('p', typeof error === 'string' ? error : error.message || jsonText(error), 'detail-summary'));
      else receipts.append(node('p', '回执状态：' + label(program.status) + '。执行完成不代表任务目标已经达成。', 'detail-summary'));
      const hashes = [...new Set([...collectHashes(result.source_refs), ...collectHashes(program.source_ref)])];
      if (hashes.length) { const sources = drawerSection('保存的源码'); hashes.forEach((hash, index) => sources.append(artifactCard(hash, '源码 ' + (index + 1), generation, index === 0))); }
      receipts.append(rawDetails(receipt, '原始回执、执行前后观察与清理结果'));
      const events = drawerSection('关联事件'), feed = node('div', null, 'detail-events');
      for (const event of arr(result.events)) { const row = node('div', null, 'detail-event'); row.tabIndex = 0; row.setAttribute('role', 'button'); row.append(node('strong', eventName(event)), node('span', '#' + event.event_seq + ' · ' + time(event.received_at))); clickable(row, () => showEvent(event)); feed.append(row); }
      events.append(feed); if (!feed.children.length) events.append(node('p', '暂无关联事件。', 'data-hint'));
      if (result.has_more_events) events.append(node('p', '关联事件超过本页上限，可按执行 ID 在事件时间线检索。', 'data-hint'));
    } catch (error) { if (generation === state.drawerGeneration) $('drawer-body').replaceChildren(node('p', errorMessage(error), 'data-hint')); }
  }
  function showJob(job) {
    openDrawer('后台任务 ' + short(job.job_id, 24), 'SUPERVISED BACKGROUND JOB');
    const section = drawerSection('最近已记录状态'), list = node('dl', null, 'detail-facts');
    facts(list, [['任务 ID', job.job_id], ['创建程序', job.execution_id], ['参与者', job.actor_id], ['记录状态', label(job.status)], ['开始时间', time(job.started_at, true)], ['结束时间', time(job.finished_at, true)], ['存活期限', duration(num(job.ttl_ms) === null ? null : job.ttl_ms / 1000)], ['取消原因', job.cancel_reason]], 'detail-facts'); section.append(list);
    drawerSection().append(node('p', '此视图反映最近的持久任务事件。「运行中」不等于实时存活保证，恢复后不自动重放任务。', 'detail-summary'));
    if (job.last_event_seq != null) drawerSection('关联证据').append(referenceButton({event_seq: job.last_event_seq}));
    drawerSection().append(rawDetails(job));
  }
  function referenceButton(ref) {
    const sequence = typeof ref === 'number' ? ref : obj(ref).event_seq;
    const hash = typeof ref === 'string' && /^[a-f0-9]{64}$/.test(ref) ? ref : obj(ref).sha256 || obj(ref).hash || obj(ref).artifact_sha256;
    const button = node('button', sequence != null ? '事件 #' + sequence + ' ↗' : hash ? '产物 ' + short(hash, 12) + ' ↗' : '证据引用', 'reference-button');
    button.addEventListener('click', () => {
      if (sequence != null) showEventById(sequence);
      else if (hash && /^[a-f0-9]{64}$/.test(hash)) { const generation = openDrawer('证据产物', 'IMMUTABLE ARTIFACT'); drawerSection().append(artifactCard(hash, '原始证据', generation, true)); }
      else toast('此引用没有可读取的事件号或 SHA256。');
    }); return button;
  }
  async function showReport() {
    const generation = openDrawer('已保存实验报告', 'REPORT · READ ONLY');
    const section = drawerSection(); section.append(node('p', '此处读取已保存的报告，不重新运行评估。字符偏移分页，未知值与报告限制保留原样。', 'data-hint'));
    const content = node('pre', '', 'artifact-content wrap'), controls = node('div', null, 'artifact-tools'), position = node('span');
    const back = node('button', '上一页', 'button button-secondary'), next = node('button', '下一页', 'button button-secondary'), buttons = node('div');
    buttons.append(back, next); controls.append(position, buttons); section.append(content, controls);
    let page = null, history = [];
    async function read(offset, direction) {
      back.disabled = next.disabled = true; position.textContent = '正在读取…';
      try {
        const result = await api('/api/report', {format: 'markdown', offset, limit: 16000}); if (generation !== state.drawerGeneration) return;
        if (result.available === false) { content.textContent = '尚无已保存报告。'; position.textContent = '报告未就绪'; return; }
        if (direction === 'next' && page) history.push(page.offset);
        page = result; content.textContent = result.content; position.textContent = '字符偏移 ' + result.offset + ' · 文件 ' + bytes(result.size_bytes); back.disabled = !history.length; next.disabled = result.next_offset == null;
      } catch (error) { position.textContent = errorMessage(error); }
    }
    back.addEventListener('click', () => read(history.pop(), 'back')); next.addEventListener('click', () => read(page.next_offset, 'next')); read(0);
  }
  function renderLive() {
    const grid = $('live-grid'), note = $('live-note');
    if (!grid) return;
    grid.replaceChildren();
    const ports = Object.entries(obj(state.status.viewers));
    const host = location.hostname || '127.0.0.1';
    if (!ports.length) {
      note.hidden = false;
      note.textContent = '当前运行未配置 runtime.viewer_ports，因此没有可播放的第一视角。启用方式：给对应 bot 设置 VIEWER_PORT=<端口>，并在实验配置中写入 runtime.viewer_ports: {player1: <端口>}。';
      return;
    }
    const tunnels = ports.map(([, port]) => '-L ' + port + ':127.0.0.1:' + port).join(' ');
    note.hidden = false;
    note.textContent = '浏览器会直接连接下列端口；非本机访问时请为工作台和每个 Viewer 端口建立 SSH 隧道（示例）：ssh -N -L 8896:127.0.0.1:8896 ' + tunnels + ' <user>@<server>。画面由 prismarine-viewer 在浏览器中实时渲染 bot 的真实游戏状态，不是视频录像。';
    for (const [agent, port] of ports) {
      const source = location.protocol + '//' + host + ':' + port + '/';
      const card = node('div', undefined, 'live-card');
      const head = node('div', undefined, 'live-head');
      const title = node('strong', agent);
      const hint = node('span', '端口 ' + port, 'subtle');
      const link = node('a', '新标签页打开 ↗', 'live-link');
      link.href = source;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      head.append(title, hint, link);
      const frame = document.createElement('iframe');
      frame.className = 'live-frame';
      frame.loading = 'lazy';
      frame.allowFullscreen = true;
      frame.title = agent + ' 第一视角';
      frame.src = source;
      card.append(head, frame);
      grid.append(card);
    }
  }

  // Paper-figure gallery v3: HTML/CSS figures matching the paper's visual language.
  const FIG_TABS = {timeline:'图 3 · 整个运行', progress:'图 1/4 · 十三步', chat:'图 5/6/8/14 · 聊天', dragon:'图 12 · 末地', behavior:'图 13 · 行为与死因'};
  const FIG_COLORS = ['#4f74b8', '#d97a26', '#2f8f63', '#a83f9b'];
  const FIG_DIMENSIONS = [['overworld', 'Overworld'], ['nether', 'Nether'], ['stronghold', 'Stronghold'], ['end', 'The End']];
  const FIG_MILESTONES = [
    ['nether_portal', 'Nether portal', 'nether_portal'],
    ['nether_fortress', 'Fortress', 'nether_fortress'],
    ['stronghold', 'Stronghold located', 'stronghold'],
    ['the_end', 'Into the End', 'the_end'],
    ['ender_dragon', 'Dragon defeated', 'ender_dragon']
  ];
  const FIG_BEHAVIORS = [['warnings', '提醒队友'], ['rescues', '物品救援'], ['shared_chests', '共享箱子'], ['lessons', '经验与提示'], ['coordinates', '精确坐标']];
  const FIG_PRECISION = {source_time:'日志时间', advancement_criteria:'成就时间', snapshot_observation_upper_bound:'快照上界', baseline:'起始前已达成', player_program_observation:'玩家程序观测'};
  const FIG_ASSETS = {
    stone_tools: 'stone_pickaxe.png', iron_tools: 'iron_pickaxe.png', bucket: 'bucket.png',
    flint_and_steel: 'flint_and_steel.png', nether_portal: 'obsidian.png', nether_fortress: 'nether_bricks.png',
    blaze_rods: 'blaze_rod.png', ender_pearls: 'ender_pearl.png', eyes_of_ender: 'ender_eye.png',
    stronghold: 'stone_bricks.png', the_end: 'end_stone.png', ten_crystals: 'end_crystal.png',
    ender_dragon: 'dragon_egg.png'
  };
  const fig = {data:null, loading:false, run:null, tab:'timeline', playing:false, t:0, generation:0, timer:null, follow:true, chatAgent:'all', playheadEl:null, overlayEl:null};
  const figTime = value => {
    const seconds = num(value);
    if (seconds === null) return '—';
    const total = Math.max(0, Math.floor(seconds));
    if (total >= 3600) return Math.floor(total / 3600) + 'h ' + String(Math.floor(total % 3600 / 60)).padStart(2, '0') + 'm';
    if (total >= 60) return Math.floor(total / 60) + 'm ' + String(total % 60).padStart(2, '0') + 's';
    return total + 's';
  };
  const figPlusTime = value => {
    const seconds = Math.max(0, Math.floor(num(value) || 0));
    return '+' + Math.floor(seconds / 3600) + 'h ' + String(Math.floor(seconds % 3600 / 60)).padStart(2, '0') + 'm ' + String(seconds % 60).padStart(2, '0') + 's';
  };
  const figAxis = value => {
    const seconds = num(value) || 0;
    const total = figDuration();
    if (total >= 3600) return Math.round(seconds / 3600) + ' h';
    if (total >= 600) return Math.round(seconds / 60) + ' min';
    return Math.round(seconds) + ' s';
  };
  const figDuration = () => Math.max(0, num(fig.data?.run?.duration_s) || 0);
  const figPct = value => Math.max(0, Math.min(100, (num(value) || 0) / Math.max(1, figDuration()) * 100));
  const figColor = agent => {
    const agents = arr(fig.data?.agents);
    const index = agents.findIndex(item => item.id === agent);
    return FIG_COLORS[(index < 0 ? 0 : index) % FIG_COLORS.length];
  };
  function figItem(key, size = 30, alt = null) {
    const file = FIG_ASSETS[key];
    if (!file) {
      const hand = node('span', '✋', 'fig-item-fallback');
      hand.style.width = size + 'px'; hand.style.height = size + 'px'; hand.style.fontSize = Math.round(size * 0.62) + 'px';
      return hand;
    }
    const image = document.createElement('img');
    image.className = 'fig-item';
    image.src = '/figure-assets/' + file;
    image.width = size; image.height = size;
    image.alt = alt || key;
    image.decoding = 'async';
    return image;
  }
  function figAvatar(agent, size = 30) {
    const agents = arr(fig.data?.agents);
    const index = Math.max(0, agents.findIndex(item => item.id === agent));
    const ring = node('span', null, 'fig-avatar-ring');
    ring.style.borderColor = figColor(agent);
    ring.style.width = (size + 8) + 'px';
    ring.style.height = (size + 8) + 'px';
    const image = document.createElement('img');
    image.className = 'fig-avatar-img';
    image.src = '/figure-assets/avatar_' + (index % 4) + '.png';
    image.width = size; image.height = size;
    image.alt = agent + ' avatar';
    ring.append(image);
    return ring;
  }
  function figHead(kicker, title, caption) {
    const header = node('div', null, 'fig-head');
    header.append(node('div', kicker, 'fig-kicker'), node('h3', title, 'fig-title'), node('p', caption, 'fig-caption'));
    return header;
  }
  function figCaption(text) { return node('p', text, 'fig-footnote'); }
  function figEmpty(title, description) {
    const wrap = node('div', null, 'fig-empty');
    wrap.append(node('strong', title), node('p', description));
    return wrap;
  }
  function figLegend(items) {
    const legend = $('fig-legend');
    if (!legend) return;
    legend.replaceChildren();
    for (const item of items) {
      if (!item) continue;
      const span = node('span');
      const swatch = node('i');
      swatch.style.background = item[1];
      span.append(swatch, node('span', item[0]));
      legend.append(span);
    }
  }
  function figPlayersLegend(agents) {
    const legend = $('fig-legend');
    if (!legend) return;
    legend.replaceChildren();
    legend.append(node('span', '首次到达的玩家', 'fig-legend-title'));
    for (const agent of agents) {
      const item = node('span', null, 'fig-legend-player');
      item.append(figAvatar(agent.id, 20), node('span', agent.id));
      item.style.color = agent.color || figColor(agent.id);
      legend.append(item);
    }
  }
  function figDetail(title, rows, evidenceSeq) {
    const detail = $('fig-detail');
    if (!detail) return;
    detail.hidden = false;
    detail.replaceChildren();
    const card = node('div', null, 'fig-detail-card');
    card.append(node('strong', title));
    for (const row of rows) if (row) card.append(node('p', row));
    const actions = node('div', null, 'fig-detail-actions');
    if (evidenceSeq != null) {
      const open = node('button', '打开证据事件 #' + evidenceSeq, 'button button-secondary');
      open.type = 'button';
      open.addEventListener('click', () => showEventById(evidenceSeq));
      actions.append(open);
    }
    const close = node('button', '关闭', 'button button-secondary');
    close.type = 'button';
    close.addEventListener('click', () => { detail.hidden = true; });
    actions.append(close);
    card.append(actions);
    detail.append(card);
  }
  function figBandGradient() {
    const bands = arr(fig.data?.dimension_bands);
    const colors = {overworld: '#f1f7ec', nether: '#fbefe6', stronghold: '#eef2f8', end: '#f5eefa'};
    if (!bands.length) return '#f8f5ee';
    const stops = [];
    for (const band of bands) {
      const color = colors[band.key] || '#f8f5ee';
      stops.push(color + ' ' + figPct(band.start) + '%', color + ' ' + figPct(band.end) + '%');
    }
    return 'linear-gradient(to right, ' + stops.join(', ') + ')';
  }
  function figUpdate() {
    if (!fig.data) return;
    const duration = figDuration();
    const clock = $('fig-clock'), seek = $('fig-seek');
    if (clock) clock.textContent = figTime(fig.t) + ' / ' + figTime(duration);
    if (seek) { seek.max = String(Math.max(0.01, duration)); seek.value = String(Math.max(0, Math.min(duration, fig.t))); }
    document.querySelectorAll('[data-fig-t]').forEach(element => {
      const moment = Number(element.dataset.figT);
      element.classList.toggle('future', Number.isFinite(moment) && moment > fig.t + 1e-6);
    });
    document.querySelectorAll('[data-chat-t]').forEach(element => {
      const moment = Number(element.dataset.chatT);
      element.classList.toggle('future', Number.isFinite(moment) && moment > fig.t + 1e-6);
    });
    document.querySelectorAll('[data-stage-key]').forEach(card => {
      const moment = Number(card.dataset.stageT);
      card.classList.toggle('reached', card.dataset.stageState === 'reached' && (!Number.isFinite(moment) || moment <= fig.t + 1e-6));
    });
    if (fig.playheadEl) fig.playheadEl.style.left = figPct(fig.t) + '%';
    document.querySelectorAll('[data-guide-t]').forEach(guide => {
      guide.classList.toggle('future', Number(guide.dataset.guideT) > fig.t);
    });
  }
  function figStop() {
    fig.playing = false;
    if (fig.timer) clearInterval(fig.timer);
    fig.timer = null;
    const button = $('fig-play');
    if (button) button.textContent = '▶';
  }
  function figStart() {
    if (!fig.data) return;
    const duration = figDuration();
    if (duration <= 0) return;
    if (fig.t >= duration - 0.001) fig.t = 0;
    fig.playing = true;
    fig.follow = false;
    const follow = $('fig-follow');
    if (follow) follow.checked = false;
    const started = performance.now(), begin = fig.t, rate = Math.max(0.5, duration / 30);
    if (fig.timer) clearInterval(fig.timer);
    fig.timer = setInterval(() => {
      fig.t = Math.min(duration, begin + (performance.now() - started) / 1000 * rate);
      figUpdate();
      if (fig.t >= duration) figStop();
    }, 80);
    const button = $('fig-play');
    if (button) button.textContent = '⏸';
  }
  async function loadFigures() {
    if (fig.loading) return;
    const generation = ++fig.generation;
    const run = state.run;
    fig.loading = true;
    try {
      const data = await api('/api/figures');
      if (generation !== fig.generation || run !== state.run) return;
      const sameRun = fig.run === run;
      fig.data = data;
      fig.run = run;
      if (!sameRun || fig.follow) fig.t = figDuration();
      fig.t = Math.max(0, Math.min(fig.t, figDuration()));
      renderFigures();
    } catch (error) {
      if (generation === fig.generation) { fig.data = null; renderFigures(error); }
    } finally {
      if (generation === fig.generation) fig.loading = false;
    }
  }
  function renderFigures(error) {
    document.querySelectorAll('[data-fig-tab]').forEach(button => {
      button.classList.toggle('active', button.dataset.figTab === fig.tab);
      button.setAttribute('aria-selected', String(button.dataset.figTab === fig.tab));
    });
    const body = $('fig-body'), meta = $('fig-meta'), status = $('fig-status'), notes = $('fig-notes');
    const controls = document.querySelector('.fig-controls');
    if (controls) controls.hidden = !fig.data;
    if (notes) notes.textContent = '';
    fig.playheadEl = null;
    if (error || !fig.data) {
      if (status) status.textContent = '无数据';
      if (meta) meta.textContent = error ? ('读取失败：' + errorMessage(error)) : '正在读取图表数据…';
      if (body) body.replaceChildren(figEmpty('暂无可绘制的图表数据', error ? errorMessage(error) : '当前 run 没有可用的账本或服务端证据。'));
      if (notes) notes.textContent = '图表只读取本机 run 目录，不连接外部服务。';
      return;
    }
    const data = fig.data, run = obj(data.run), agents = arr(data.agents);
    if (status) status.textContent = label(run.status);
    if (meta) meta.textContent = [run.name || run.id, label(run.task), label(run.profile), figTime(figDuration()), number(run.event_count) + ' 事件', agents.length + ' 玩家'].filter(Boolean).join('  ·  ');
    const seek = $('fig-seek'), follow = $('fig-follow');
    if (seek) seek.max = String(Math.max(0.01, figDuration()));
    if (follow) follow.checked = fig.follow;
    if (body) body.replaceChildren();
    if (fig.tab === 'timeline') renderFigTimeline();
    else if (fig.tab === 'progress') renderFigProgress();
    else if (fig.tab === 'chat') renderFigChat();
    else if (fig.tab === 'dragon') renderFigDragon();
    else if (fig.tab === 'world') renderFigWorld();
    else renderFigBehavior();
    const omitted = obj(data.omitted);
    const omittedText = Object.entries(omitted).filter(([, value]) => number(value) > 0).map(([key, value]) => key + ' 省略 ' + number(value)).join('，');
    if (notes) notes.textContent = omittedText ? ('数据边界：' + omittedText) : '';
    figUpdate();
  }
  function renderFigTimeline() {
    const body = $('fig-body'), data = fig.data;
    if (!body) return;
    const agents = arr(data.agents), duration = figDuration();
    const gradient = figBandGradient();
    const timeline = node('div', null, 'fig-timeline');
    timeline.append(figHead('FIG. 3 · THE WHOLE RUN', '整个运行：每一位玩家的活动', '圆点 = 游戏聊天 · 菱形 = 成就 · 红色刻度 = 死亡 · 浅蓝柱 = 每时段程序数 · 橙色虚线 = 服务器重启 · 点击标记查看证据'));
    const grid = node('div', null, 'fig-web-grid');
    grid.style.gridTemplateRows = '96px repeat(' + Math.max(1, agents.length) + ', 64px) 84px 36px';
    // annotation row
    grid.append(node('div', null, 'fig-web-gutter fig-web-anno-gutter'));
    const annotation = node('div', null, 'fig-web-anno');
    annotation.style.background = gradient;
    for (const band of arr(data.dimension_bands)) {
      const bandLabel = node('span', (band.label || band.key).toUpperCase(), 'fig-band-label ' + (band.key || ''));
      bandLabel.style.left = figPct(band.start) + '%';
      annotation.append(bandLabel);
    }
    const stages = Object.fromEntries(arr(data.stages).map(stage => [stage.key, stage]));
    const milestones = FIG_MILESTONES.map(([key, text, iconKey]) => ({key, text, iconKey, stage: stages[key]})).filter(item => item.stage && item.stage.t != null);
    const chosen = milestones.length ? milestones.slice(0, 4) : arr(data.stages).filter(stage => stage.t != null).slice(0, 3).map(stage => ({key: stage.key, text: stage.label, iconKey: stage.key, stage}));
    chosen.forEach((item, index) => {
      const chip = node('div', null, 'fig-milestone');
      const pct = figPct(item.stage.t);
      chip.style.left = pct + '%';
      chip.style.top = (26 + (index % 2) * 30) + 'px';
      if (pct > 72) chip.classList.add('right');
      if (pct < 12) chip.classList.add('left');
      chip.append(figItem(item.iconKey, 20), node('span', item.text), node('span', figPlusTime(item.stage.t), 'fig-milestone-time'));
      if (item.stage.evidence?.event_seq != null) {
        chip.style.cursor = 'pointer';
        chip.addEventListener('click', () => figDetail('阶段 · ' + item.text, [figPlusTime(item.stage.t) + ' · ' + (item.stage.agent || '未知玩家'), '来源：' + (item.stage.source || ''), '精度：' + (FIG_PRECISION[item.stage.precision] || item.stage.precision || '未知')], item.stage.evidence.event_seq));
      }
      annotation.append(chip);
    });
    grid.append(annotation);
    // player lanes
    for (const agent of agents) {
      const color = agent.color || figColor(agent.id);
      const label = node('div', null, 'fig-web-lane-label');
      label.append(figAvatar(agent.id, 30));
      const text = node('div', null, 'fig-web-lane-text');
      const name = node('strong', agent.id);
      name.style.color = color;
      text.append(name, node('small', number(agent.programs) + ' programs · ' + number(agent.messages) + ' messages · ' + number(agent.deaths) + ' deaths'));
      label.append(text);
      const track = node('div', null, 'fig-web-lane-track');
      track.style.background = gradient;
      for (const item of arr(data.chats).filter(entry => entry.agent === agent.id)) {
        const dot = node('button', null, 'fig-web-msg');
        dot.type = 'button';
        dot.style.left = figPct(item.t) + '%'; dot.style.background = color;
        dot.title = 'message · ' + figPlusTime(item.t) + ' · ' + (item.text || '').slice(0, 160);
        dot.dataset.figT = String(item.t ?? '');
        dot.addEventListener('click', () => figDetail('聊天 · ' + agent.id, [figPlusTime(item.t) + ' · ' + agent.id, item.text || '', (item.labels || []).join(', ')], item.evidence?.event_seq));
        track.append(dot);
      }
      for (const item of arr(data.advancements).filter(entry => entry.agent === agent.id)) {
        const diamond = node('span', null, 'fig-web-adv');
        diamond.style.left = figPct(item.t) + '%'; diamond.style.borderColor = color;
        diamond.title = 'advancement · ' + (item.title || '') + ' · ' + figPlusTime(item.t);
        diamond.dataset.figT = String(item.t ?? '');
        diamond.addEventListener('click', () => figDetail('成就 · ' + agent.id, [figPlusTime(item.t) + ' · ' + (item.title || '')], item.evidence?.event_seq));
        track.append(diamond);
      }
      for (const item of arr(data.deaths).filter(entry => entry.agent === agent.id)) {
        const tick = node('span', null, 'fig-web-death');
        tick.style.left = figPct(item.t) + '%';
        tick.title = 'death · ' + figPlusTime(item.t) + ' · ' + (item.cause || '');
        tick.dataset.figT = String(item.t ?? '');
        tick.addEventListener('click', () => figDetail('死亡 · ' + agent.id, [figPlusTime(item.t) + ' · ' + agent.id, item.text || '', '原因：' + (item.cause || '未知')], item.evidence?.event_seq));
        track.append(tick);
      }
      grid.append(label, track);
    }
    // program bars
    grid.append(node('div', 'Programs', 'fig-web-gutter fig-web-hist-label'));
    const hist = node('div', null, 'fig-web-hist');
    hist.style.background = gradient;
    const bars = node('div', null, 'fig-web-bars');
    const bins = duration >= 7200 ? Math.min(48, Math.max(8, Math.ceil(duration / 1800))) : Math.min(32, Math.max(6, Math.ceil(duration / 120)));
    const counts = Array.from({length: bins}, () => 0);
    for (const program of arr(data.programs)) counts[Math.min(bins - 1, Math.max(0, Math.floor((num(program.start) || 0) / Math.max(1, duration) * bins)))] += 1;
    const maximum = Math.max(1, ...counts);
    for (const count of counts) {
      const bar = node('span', null, 'fig-web-bar');
      bar.style.height = Math.max(2, count / maximum * 100) + '%';
      bar.title = count + ' programs';
      bars.append(bar);
    }
    hist.append(bars);
    grid.append(hist);
    // axis
    grid.append(node('div', null, 'fig-web-gutter'));
    const axis = node('div', null, 'fig-web-axis');
    for (let index = 0; index <= 6; index += 1) {
      axis.append(node('span', figAxis(duration * index / 6), 'fig-web-axis-label'));
    }
    grid.append(axis);
    // overlay: restart guides and playhead
    const overlay = node('div', null, 'fig-web-overlay');
    for (const item of chosen) {
      const guide = node('div', null, 'fig-web-ms-guide');
      guide.style.left = figPct(item.stage.t) + '%';
      overlay.append(guide);
    }
    for (const marker of arr(data.restarts)) {
      if (marker.kind === 'runtime_reset') continue;
      const guide = node('div', null, 'fig-web-guide');
      guide.style.left = figPct(marker.t) + '%';
      guide.dataset.guideT = String(marker.t ?? '');
      guide.title = marker.kind + ' @ ' + figTime(marker.t);
      overlay.append(guide);
    }
    const playhead = node('div', null, 'fig-web-playhead');
    playhead.style.left = figPct(fig.t) + '%';
    overlay.append(playhead);
    grid.append(overlay);
    fig.playheadEl = playhead;
    timeline.append(grid, figCaption('图 3 | 每位玩家一条泳道；时间为相对首个程序；数据只来自服务端日志与世界快照。'));
    body.append(timeline);
    figLegend([['消息', '#6f7680'], ['成就', '#6f7680'], ['死亡', '#d05b5b'], ['服务器重启', '#d9a441']]);
    figUpdate();
  }
  function renderFigProgress() {
    const body = $('fig-body'), data = fig.data;
    if (!body) return;
    const agents = arr(data.agents);
    const wrap = node('div', null, 'fig-progress-layout');
    const left = node('div', null, 'fig-progress-agents');
    for (const agent of agents) {
      const card = node('div', null, 'fig-agent');
      const title = node('div', null, 'fig-agent-name');
      title.append(node('span', agent.id));
      title.style.color = agent.color || figColor(agent.id);
      card.append(figAvatar(agent.id, 34), title, node('small', number(agent.programs) + ' programs · ' + number(agent.messages) + ' messages', ''));
      left.append(card);
    }
    const flow = node('div', null, 'fig-progress-flow');
    for (const [key, labelText] of FIG_DIMENSIONS) {
      const group = node('div', null, 'fig-dim-group ' + key);
      const title = node('div', null, 'fig-dim-title');
      title.append(node('span', labelText.toUpperCase()), node('span', '→', 'fig-dim-arrow'));
      group.append(title);
      if (key === 'overworld') {
        const start = node('div', null, 'fig-start-node');
        start.append(node('span', '+0h 00m 00s', 'fig-start-time'), node('strong', 'Bare hands'), node('span', 'start', 'fig-start-caption'));
        group.append(start);
      }
      const row = node('div', null, 'fig-stage-row');
      for (const stage of arr(data.stages).filter(item => item.dimension === key)) {
        const card = node('div', null, 'fig-stage-card ' + stage.state);
        card.dataset.stageKey = stage.key;
        card.dataset.stageState = stage.state;
        card.dataset.stageT = stage.t == null ? '' : String(stage.t);
        card.append(figItem(stage.key, 30, stage.label));
        const text = node('div', null, 'fig-stage-text');
        text.append(node('div', stage.label, 'fig-stage-name'));
        const meta = node('div', null, 'fig-stage-meta');
        if (stage.t != null) {
          const dot = node('span', null, 'fig-dot');
          dot.style.background = stage.agent ? figColor(stage.agent) : '#b8b2a8';
          meta.append(dot, node('span', figPlusTime(stage.t)));
        } else {
          meta.append(node('span', stage.state === 'unavailable' ? '不可得' : (obj(data.run).running ? '待观测' : '未到达')));
        }
        text.append(meta);
        card.append(text);
        if (stage.evidence?.event_seq != null) card.addEventListener('click', () => figDetail('阶段 · ' + stage.label, [stage.t == null ? '未观测' : (figPlusTime(stage.t) + ' · ' + (stage.agent || '未知玩家')), '来源：' + (stage.source || ''), '精度：' + (FIG_PRECISION[stage.precision] || stage.precision || '未知')], stage.evidence.event_seq));
        row.append(card);
      }
      group.append(row);
      flow.append(group);
    }
    wrap.append(left, flow);
    body.append(figHead('FIG. 1 / 4 · THE RUN, STEP BY STEP', '十三个依赖步骤', '按 Overworld → Nether → Stronghold → The End 分栏，自上而下为步骤顺序；点击步骤查看证据、时间与精度。'), wrap);
    if (arr(data.deaths).length) {
      const strip = node('div', null, 'fig-web-death-strip');
      strip.append(node('span', number(arr(data.deaths).length) + ' deaths', 'fig-death-label'));
      const track = node('div', null, 'fig-death-track');
      for (const death of arr(data.deaths)) {
        const tick = node('span', null, 'fig-web-death');
        tick.style.left = figPct(death.t) + '%';
        tick.title = 'death · ' + figPlusTime(death.t) + ' · ' + (death.cause || '');
        track.append(tick);
      }
      strip.append(track);
      const axis = node('div', null, 'fig-death-axis');
      for (let index = 0; index <= 6; index += 1) axis.append(node('span', figAxis(figDuration() * index / 6)));
      strip.append(axis);
      body.append(strip);
    }
    figPlayersLegend(agents);
    body.append(figCaption('图 1/4 | 时间为到达该阶段的服务端证据时间；快照派生值为观测上界；十水晶没有对应证据，标为不可得。横向四栏分别对应四个维度。'));
    figUpdate();
  }
  function renderFigChat() {
    const body = $('fig-body'), data = fig.data;
    if (!body) return;
    const all = arr(data.chats).filter(item => fig.chatAgent === 'all' || item.agent === fig.chatAgent);
    const head = figHead('FIG. 5 / 6 / 8 / 14 · CHAT', '协作聊天回放', '论文用聊天片段展示分工、经验与救援；这里是本 run 的服务端聊天记录，可按玩家过滤并回放。');
    if (!all.length) {
      body.append(head, figEmpty('本 run 暂无可回放的服务器聊天', '论文图 5/6/8/14 依赖服务端聊天记录；切换其它 run 或等聊天出现后再看。'));
      figLegend([]);
      return;
    }
    const filters = node('div', null, 'fig-chat-filters');
    const addFilter = (id, text) => {
      const button = node('button', text, 'fig-chat-filter' + (fig.chatAgent === id ? ' active' : ''));
      button.type = 'button';
      if (id !== 'all') button.style.color = figColor(id);
      button.addEventListener('click', () => { fig.chatAgent = id; renderFigChat(); });
      filters.append(button);
    };
    addFilter('all', 'ALL');
    for (const agent of arr(data.agents)) addFilter(agent.id, agent.id);
    const list = node('div', null, 'fig-chat-list');
    for (const item of all.slice().sort((a, b) => (a.t || 0) - (b.t || 0)).slice(-240)) {
      const row = node('div', null, 'fig-chat-row');
      row.dataset.chatT = String(item.t ?? '');
      if (num(item.t) !== null && item.t > fig.t + 1e-6) row.classList.add('future');
      row.append(figAvatar(item.agent, 32));
      const main = node('div', null, 'fig-chat-main');
      const meta = node('div', null, 'fig-chat-meta');
      const name = node('strong', item.agent || 'unknown');
      name.style.color = figColor(item.agent);
      meta.append(name, node('span', figPlusTime(item.t), 'fig-chat-time'));
      main.append(meta);
      const rawText = item.text || '';
      const bubbleText = rawText.replace(/^[A-Za-z0-9_]{1,16}:\s*/, '') || rawText;
      main.append(node('div', bubbleText, 'fig-chat-bubble'));
      if (arr(item.labels).length) {
        const chips = node('div', null, 'fig-chips');
        for (const tag of item.labels) chips.append(node('span', tag, 'fig-chip'));
        main.append(chips);
      }
      if (item.evidence?.event_seq != null) {
        const open = node('button', '证据 #' + item.evidence.event_seq, 'fig-evidence-link');
        open.type = 'button';
        open.addEventListener('click', () => showEventById(item.evidence.event_seq));
        main.append(open);
      }
      row.append(main);
      list.append(row);
    }
    body.append(head, filters, list);
    figLegend(arr(data.agents).map(agent => [agent.id, agent.color || figColor(agent.id)]));
    body.append(figCaption('图 5/6/8/14 | 消息文本与时间来自服务端日志；播放时超前的消息会淡化。'));
    figUpdate();
  }
  function figBars(container, title, entries, colors) {
    const card = node('div', null, 'fig-behavior-card');
    card.append(node('h3', title));
    if (!entries.length) card.append(node('p', '暂无可统计的数据。', 'fig-footnote'));
    const maximum = Math.max(1, ...entries.map(entry => num(entry.total) || 0));
    for (const entry of entries) {
      const row = node('div', null, 'fig-bar-row');
      row.append(node('span', entry.label, 'fig-bar-label'));
      const bar = node('div', null, 'fig-bar');
      for (const [name, count] of Object.entries(entry.parts || {})) {
        if (!count) continue;
        const segment = node('i');
        segment.style.width = (count / maximum * 100) + '%';
        segment.style.background = colors[name] || '#9aa0a8';
        segment.title = name + ': ' + count;
        bar.append(segment);
      }
      row.append(bar, node('span', number(entry.total), 'fig-bar-num'));
      card.append(row);
    }
    container.append(card);
  }
  function renderFigBehavior() {
    const body = $('fig-body'), data = fig.data;
    if (!body) return;
    const agents = arr(data.agents), behavior = obj(data.behavior), death = obj(data.death_causes);
    const colors = Object.fromEntries(agents.map(agent => [agent.id, agent.color || figColor(agent.id)]));
    const grid = node('div', null, 'fig-two-col');
    const behaviorEntries = FIG_BEHAVIORS.map(([key, labelText]) => {
      const parts = {};
      for (const agent of agents) parts[agent.id] = num(behavior.by_agent?.[agent.id]?.[key]) || 0;
      return {label: labelText, total: num(behavior.paper?.[key]) || 0, parts};
    });
    figBars(grid, '聊天行为 · messages matching patterns', behaviorEntries, colors);
    const causes = Object.entries(obj(death.causes)).sort((a, b) => (num(b[1]) || 0) - (num(a[1]) || 0)).slice(0, 10);
    const causeEntries = causes.map(([labelText, total]) => {
      const parts = {};
      for (const agent of agents) parts[agent.id] = num(death.by_agent?.[agent.id]?.[labelText]) || 0;
      return {label: labelText, total, parts};
    });
    figBars(grid, '死亡原因 · from the server log', causeEntries, colors);
    body.append(figHead('FIG. 13 · TEAM BEHAVIOR', '行为与死因', '左：论文五类聊天行为的确定性近似规则（可多标签）；右：服务端日志中的死亡原因。规则是近似，不等同于意图判断。'), grid);
    const project = obj(behavior.project_labels);
    if (Object.keys(project).length) {
      const chips = node('div', null, 'fig-chips');
      for (const [name, count] of Object.entries(project)) chips.append(node('span', name + ' × ' + number(count), 'fig-chip'));
      body.append(chips);
    }
    figLegend(agents.map(agent => [agent.id, agent.color || figColor(agent.id)]));
    body.append(figCaption('图 13 | 行为模式由本项目重新定义；死因来自规范化英文 Vanilla 死亡消息，原始日志保留。'));
    figUpdate();
  }
  function renderFigDragon() {
    const body = $('fig-body'), data = fig.data;
    if (!body) return;
    const dragon = obj(data.dragon);
    const head = figHead('FIG. 12 · THE END FIGHT', '末地战斗', '论文左图是十块水晶被谁、何时摧毁；右图是玩家程序读到的龙血变化。');
    if (!dragon.available) {
      const missing = node('div', null, 'fig-dragon-empty');
      const left = node('div', null, 'fig-dragon-panel');
      left.append(node('h3', 'Ten crystals, destroyed from pillars of dirt'));
      const wheel = node('div', null, 'fig-crystal-wheel');
      const center = node('span', null, 'fig-crystal-center');
      center.append(figItem('ender_dragon', 46));
      wheel.append(center);
      for (let index = 0; index < 10; index += 1) {
        const angle = (index / 10) * Math.PI * 2 - Math.PI / 2;
        const slot = node('span', null, 'fig-crystal-slot');
        slot.style.left = (50 + Math.cos(angle) * 38) + '%';
        slot.style.top = (50 + Math.sin(angle) * 38) + '%';
        slot.title = 'End crystal ' + (index + 1) + ' · awaiting observation';
        slot.append(figItem('ten_crystals', 22));
        wheel.append(slot);
      }
      left.append(wheel, node('p', '等待观测通道 · no crystal observations', 'fig-empty-note'));
      const right = node('div', null, 'fig-dragon-panel');
      right.append(node('h3', "The dragon's health, as the players' programs read it"));
      const plot = node('div', null, 'fig-health-empty');
      const yAxis = node('div', null, 'fig-health-y');
      for (const hp of [200, 150, 100, 50, 0]) yAxis.append(node('span', hp));
      const area = node('div', null, 'fig-health-area');
      area.append(node('span', '等待龙血观测数据', 'fig-health-watermark'));
      plot.append(yAxis, area);
      right.append(plot, node('p', '需要 kill_dragon 运行中的玩家程序观测记录', 'fig-empty-note'));
      missing.append(left, right);
      body.append(head, missing, figCaption('图 12 | harness 尚未采集末地水晶/龙血观测通道；页面只显示现有证据，不伪造数据。'));
      figLegend([]);
      return;
    }
    const health = arr(dragon.health), crystals = arr(dragon.crystals);
    const wrap = node('div', null, 'fig-dragon-live');
    const left = node('div', null, 'fig-dragon-panel');
    left.append(node('h3', 'Ten crystals, destroyed from pillars of dirt'));
    const wheel = node('div', null, 'fig-crystal-wheel');
    const center = node('span', null, 'fig-crystal-center');
    center.append(figItem('ender_dragon', 46));
    wheel.append(center);
    const latestCrystal = crystals.length ? crystals[crystals.length - 1] : null;
    const remaining = latestCrystal && Number.isFinite(Number(latestCrystal.count)) ? Math.max(0, Math.min(10, Number(latestCrystal.count))) : null;
    for (let index = 0; index < 10; index += 1) {
      const angle = (index / 10) * Math.PI * 2 - Math.PI / 2;
      const slot = node('span', null, 'fig-crystal-slot');
      slot.style.left = (50 + Math.cos(angle) * 38) + '%';
      slot.style.top = (50 + Math.sin(angle) * 38) + '%';
      if (remaining != null && index >= remaining) slot.classList.add('destroyed');
      slot.title = 'End crystal ' + (index + 1) + (remaining != null && index >= remaining ? ' · observed destroyed' : ' · observed present');
      slot.append(figItem('ten_crystals', 22));
      wheel.append(slot);
    }
    left.append(wheel);
    left.append(node('p', latestCrystal ? 'Latest player observation: ' + remaining + '/10 crystals at ' + figPlusTime(latestCrystal.t) : 'No crystal count observed yet.', 'fig-empty-note'));
    const right = node('div', null, 'fig-dragon-panel');
    right.append(node('h3', "The dragon's health, as the players' programs read it"));
    const plot = node('div', null, 'fig-health-empty');
    const yAxis = node('div', null, 'fig-health-y');
    for (const hp of [200, 150, 100, 50, 0]) yAxis.append(node('span', hp));
    const area = node('div', null, 'fig-health-area');
    const latestHealth = health.length ? health[health.length - 1] : null;
    if (!health.length) area.append(node('span', '等待龙血观测数据', 'fig-health-watermark'));
    for (const point of health) {
      const dot = node('span', null, 'fig-health-dot');
      const hp = Math.max(0, Math.min(200, Number(point.hp) || 0));
      dot.style.left = Math.max(0, Math.min(100, ((Number(point.t) || 0) / Math.max(1, Number(data.run?.duration_s) || 1)) * 100)) + '%';
      dot.style.bottom = (hp / 2) + '%';
      dot.title = (point.agent || 'unknown') + ' · ' + hp + ' HP · ' + figPlusTime(point.t);
      area.append(dot);
    }
    plot.append(yAxis, area);
    right.append(plot, node('p', latestHealth ? 'Latest player observation: ' + latestHealth.hp + ' HP at ' + figPlusTime(latestHealth.t) : '等待玩家程序的龙血观测 JSON 行（dragon_health/crystals）。', 'fig-empty-note'));
    wrap.append(left, right);
    body.append(head, wrap, figCaption('图 12 | 水晶与龙血来自玩家程序主动记录的 observation 行，是 agent 观测而非服务端真值；absence 不按 0 处理。'), figLegend([]));
  }

  function renderFigWorld() {
    const body = $('fig-body'), data = fig.data;
    if (!body) return;
    const world = obj(data.world);
    const head = figHead('SAVED WORLD · 3D MESH', '存档世界：160×160 3D mesh', '出生点/基地周围 160×160 方块的 textured 3D 网格，带 Pixel Perfection 材质与 per-pixel z-buffer；只读，不修改世界。');
    if (!world.available || !world.url) {
      body.append(head, figEmpty('没有可读取的服务端存档', '当前 run 没有 world_dir，或存档文件不可读。'));
      figLegend([]);
      return;
    }
    const card = node('div', null, 'fig-world-card');
    const image = node('img', null, 'fig-world-image');
    image.src = world.url; image.alt = 'Minecraft 存档出生点与基地地形图';
    image.loading = 'lazy';
    const caption = node('p', world.caption || '只读存档地形图', 'fig-caption');
    const method = node('p', world.method || '', 'fig-footnote');
    image.addEventListener('error', () => {
      card.replaceChildren(figEmpty('地形图暂不可用', '世界存档读取失败或当前 run 没有可读存档。'));
    });
    card.append(image, caption, method);
    body.append(head, card, figCaption('数据来源：overworld region 存档与 Pixel Perfection 材质；160×160 表面窗口，不是完整世界或三角网格全精度。'));
    figLegend([]);
  }

  function navigate(view) {
    if (!(view in VIEWS)) view = 'overview'; state.view = view;
    document.querySelectorAll('[data-panel]').forEach(panel => { panel.hidden = panel.dataset.panel !== view; });
    document.querySelectorAll('.nav-item').forEach(link => { const active = link.dataset.view === view; link.classList.toggle('active', active); if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current'); });
    $('page-title').textContent = VIEWS[view][0]; $('page-description').textContent = VIEWS[view][1]; closeNavigation();
    if (view !== 'figures' && document.body.classList.contains('fig-focus')) {
      document.body.classList.remove('fig-focus');
      const focusButton = $('fig-focus'); if (focusButton) focusButton.textContent = '⛶ 专注';
    }
    const pageIcon = $('page-icon'); if (pageIcon) pageIcon.style.backgroundImage = 'url(/ui-assets/' + (PAGE_ICONS[view] || 'map.png') + ')';
    if (view === 'events' && !state.pages.events.loaded) loadPage('events', {reset: true});
    if (view === 'programs') { renderPrograms(); if (!state.pages[state.programTab].loaded) loadPage(state.programTab, {reset: true}); }
    if (view === 'chat' && !state.pages.chat.loaded) loadPage('chat', {reset: true});
    if (view === 'observer' && !state.pages.observer.loaded) loadPage('observer', {reset: true});
    if (view === 'evidence') { renderEvaluation(); loadReport(); }
    if (view === 'figures') { if (!fig.data || fig.run !== state.run) { state.figuresAt = Date.now(); loadFigures(); } else renderFigures(); }
    else figStop();
    if (view === 'live') renderLive();
    if (view === 'resources' || view === 'overview') { if (!state.resourceLoaded) loadResources(); requestAnimationFrame(drawResourceCharts); }
  }
  function closeNavigation() { $('sidebar').classList.remove('open'); $('nav-backdrop').hidden = true; $('nav-toggle').setAttribute('aria-expanded', 'false'); }
  async function changeRun(value) {
    state.generation++; state.run = value; state.overview = {}; state.status = {}; state.report = null; state.reportState = 'not_loaded'; state.pendingControl = null;
    const url = new URL(location.href); url.searchParams.set('run', value); history.replaceState(null, '', url);
    state.pages = Object.fromEntries(['events', 'programs', 'jobs', 'chat', 'observer'].map(key => [key, pageState()]));
    state.resourceItems = []; state.resourceCursor = null; state.resourceMore = false; state.resourceLoaded = false; state.resourceHistory = false; state.resourceScope = {}; state.resourceCompleteness = {};
    figStop(); fig.data = null; fig.run = null; state.stageLimit = 20;
    closeDrawer(); $('control-dialog').close(); $('error-banner').hidden = true;
    for (const form of document.querySelectorAll('.filter-bar')) form.reset();
    renderOverview(); renderEvents(); renderPrograms(); renderChat(); renderObserver(); renderResources(); renderEvaluation();
    await loadCore(); navigate(state.view); toast('已切换查看：' + (runInfo().name || value));
  }
  async function refresh() {
    await loadCore();
    if (state.view === 'events' && $('events-follow').checked) await loadPage('events', {refresh: true});
    else if (state.view === 'programs') await loadPage(state.programTab, {refresh: true});
    else if (state.view === 'chat' || state.view === 'observer') await loadPage(state.view, {refresh: true});
    else if (state.view === 'overview' || state.view === 'resources') await loadResources();
    else if (state.view === 'evidence' && state.reportState === 'unavailable') { state.reportState = 'not_loaded'; await loadReport(); }
    else if (state.view === 'figures' && !fig.playing && Date.now() - state.figuresAt > 15000) { state.figuresAt = Date.now(); figStop(); await loadFigures(); }
  }
  const COMMANDS = {pause: ['暂停推理', '提交暂停请求。页面刷新暂停与这个实验控制操作相互独立。'], freeze: ['冻结世界', '通过 Supervisor 的既有流程暂停实验并冻结服务器。'], resume: ['恢复运行', '请求恢复实验；不会由页面重新提交或重放程序。'], checkpoint: ['保存检查点', '请求保存检查点和世界存档。'], stop: ['停止实验', '请求有序停止整个实验。再次运行需要遵循显式恢复流程。']};
  function openControl() {
    const available = token && token !== '__TOKEN__' && state.status.supervisor_active === true;
    state.controlCommand = null; $('control-options').hidden = false; $('control-confirmation').hidden = true;
    $('control-target').textContent = (runInfo().name || state.run || '当前实验') + ' · ' + label(state.overview.status || state.status.status);
    for (const button of document.querySelectorAll('[data-command]')) button.disabled = !available;
    $('control-feedback').textContent = !token || token === '__TOKEN__' ? '当前页面没有有效操作者令牌，控制操作不可用。' : state.status.supervisor_active !== true ? 'Supervisor 当前不在线，无法执行控制。此页面仍可用于读取历史记录。' : '';
    updateControlFeedback(); $('control-dialog').showModal();
  }
  function chooseControl(command) {
    if (!COMMANDS[command]) return; state.controlCommand = command;
    $('control-options').hidden = true; $('control-confirmation').hidden = false;
    $('control-warning').textContent = '即将对「' + (runInfo().name || state.run) + '」执行「' + COMMANDS[command][0] + '」。' + COMMANDS[command][1];
    $('control-confirm').textContent = '确认' + COMMANDS[command][0]; $('control-confirm').disabled = false;
    $('control-confirm').focus();
  }
  async function submitControl() {
    const command = state.controlCommand; if (!command || !COMMANDS[command]) return;
    const generation = state.generation; $('control-confirm').disabled = true;
    try {
      const result = await api('/api/control', {}, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Control-Token': token}, body: JSON.stringify({command, run: state.run || undefined})});
      if (generation !== state.generation) return;
      state.pendingControl = {id: result.control_id, command}; state.controlCommand = null;
      $('control-options').hidden = false; $('control-confirmation').hidden = true;
      $('control-feedback').textContent = '已入持久队列：' + COMMANDS[command][0] + '\n请求 ID：' + result.control_id + '\n已接收不等于已执行，等待 Supervisor 回执。';
      toast('请求已入队，等待 Supervisor 执行。'); await loadCore();
    } catch (error) { if (generation === state.generation) { $('control-feedback').textContent = '提交未被确认：' + errorMessage(error) + '\n请核对队列后再决定是否重试。'; $('control-confirm').disabled = false; } }
  }
  function updateControlFeedback() {
    if (!state.pendingControl) return;
    const control = arr(state.status.controls).find(value => value.control_id === state.pendingControl.id || value.id === state.pendingControl.id);
    if (!control) return;
    $('control-feedback').textContent = COMMANDS[state.pendingControl.command][0] + ' · ' + label(control.state || control.status) + '\n请求 ID：' + state.pendingControl.id + (control.result ? '\n执行回执：' + short(typeof control.result === 'string' ? control.result : jsonText(control.result), 500) : '\n等待 Supervisor 执行回执。');
  }
  function setup() {
    document.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => { location.hash = button.dataset.go; }));
    window.addEventListener('hashchange', () => navigate(location.hash.slice(1)));
    $('run-select').addEventListener('change', event => changeRun(event.target.value));
    const narrow = () => window.matchMedia('(max-width: 980px)').matches;
    const setSidebarCollapsed = collapsed => {
      document.body.classList.toggle('sidebar-collapsed', collapsed);
      try { localStorage.setItem('mc-sidebar-collapsed', collapsed ? '1' : '0'); } catch (error) { /* storage is optional */ }
      $('nav-toggle').setAttribute('aria-expanded', String(!collapsed));
    };
    const closeNavDrawer = () => { $('sidebar').classList.remove('open'); $('nav-backdrop').hidden = true; $('nav-toggle').setAttribute('aria-expanded', 'false'); };
    $('nav-toggle').addEventListener('click', () => {
      if (narrow()) {
        const open = $('sidebar').classList.toggle('open');
        $('nav-backdrop').hidden = !open;
        $('nav-toggle').setAttribute('aria-expanded', String(open));
      } else {
        setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'));
      }
    });
    if (!narrow()) {
      let saved = '0';
      try { saved = localStorage.getItem('mc-sidebar-collapsed') || '0'; } catch (error) { /* optional */ }
      setSidebarCollapsed(saved === '1');
    }
    window.addEventListener('resize', () => { if (!narrow()) closeNavDrawer(); });
    $('nav-backdrop').addEventListener('click', closeNavigation);
    $('poll-toggle').addEventListener('click', () => { state.poll = !state.poll; $('poll-toggle').setAttribute('aria-pressed', String(!state.poll)); $('poll-toggle').querySelector('span').textContent = state.poll ? '暂停刷新' : '继续刷新'; setConnection(state.poll ? 'online' : 'paused', state.poll ? '实时同步' : '刷新已暂停'); if (state.poll) refresh(); });
    $('refresh').addEventListener('click', refresh); $('error-dismiss').addEventListener('click', () => { $('error-banner').hidden = true; });
    $('hud-control').addEventListener('click', () => $('control-open').click());
    const setFigFocus = focused => {
      document.body.classList.toggle('fig-focus', focused);
      const button = $('fig-focus'); if (button) button.textContent = focused ? '⛶ 退出专注' : '⛶ 专注';
    };
    $('fig-focus').addEventListener('click', () => setFigFocus(!document.body.classList.contains('fig-focus')));
    document.querySelectorAll('[data-fig-tab]').forEach(button => button.addEventListener('click', () => { fig.tab = button.dataset.figTab; renderFigures(); }));
    $('fig-play').addEventListener('click', () => { if (fig.playing) figStop(); else figStart(); });
    $('fig-follow').addEventListener('change', event => { fig.follow = event.target.checked; if (fig.follow) { figStop(); fig.t = figDuration(); figUpdate(); } });
    $('fig-seek').addEventListener('input', event => { figStop(); fig.follow = false; $('fig-follow').checked = false; fig.t = Math.max(0, Math.min(figDuration(), Number(event.target.value) || 0)); figUpdate(); });
    for (const name of ['events', 'programs', 'chat']) {
      const form = $(name + '-filter');
      const apply = () => { const pageName = name === 'programs' ? state.programTab : name; state.pages[pageName].filters = formFilters(pageName); loadPage(pageName, {reset: true}); };
      form.addEventListener('submit', event => { event.preventDefault(); apply(); });
      form.addEventListener('reset', () => setTimeout(apply, 0));
    }
    for (const name of ['events', 'programs', 'chat', 'observer']) $(name + '-more').addEventListener('click', () => loadPage(name === 'programs' ? state.programTab : name));
    document.querySelectorAll('[data-program-tab]').forEach(button => button.addEventListener('click', () => {
      state.programTab = button.dataset.programTab;
      document.querySelectorAll('[data-program-tab]').forEach(tab => tab.setAttribute('aria-selected', String(tab === button)));
      state.pages[state.programTab].filters = formFilters(state.programTab); renderPrograms();
      loadPage(state.programTab, {reset: true});
    }));
    document.querySelectorAll('[data-window]').forEach(button => button.addEventListener('click', () => { state.measurementWindow = button.dataset.window; document.querySelectorAll('[data-window]').forEach(other => other.setAttribute('aria-pressed', String(other === button))); renderEvaluation(); }));
    $('report-open').addEventListener('click', showReport);
    $('resources-more').addEventListener('click', () => loadResources(true));
    $('resources-latest').addEventListener('click', () => loadResources(false, true));
    $('drawer-close').addEventListener('click', closeDrawer); $('drawer-backdrop').addEventListener('click', closeDrawer);
    $('detail-drawer').addEventListener('keydown', event => {
      if (event.key !== 'Tab') return;
      const focusable = [...$('detail-drawer').querySelectorAll('button:not(:disabled),a[href],summary,[tabindex="0"]')].filter(element => element.getClientRects().length);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    });
    $('control-open').addEventListener('click', openControl);
    document.querySelectorAll('[data-command]').forEach(button => button.addEventListener('click', () => chooseControl(button.dataset.command)));
    $('control-cancel').addEventListener('click', () => { state.controlCommand = null; $('control-options').hidden = false; $('control-confirmation').hidden = true; });
    $('control-confirm').addEventListener('click', submitControl);
    window.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        if (document.body.classList.contains('fig-focus')) setFigFocus(false);
        if (!$('detail-drawer').hidden) closeDrawer();
        closeNavigation();
      }
      if ((event.metaKey || event.ctrlKey) && /^[1-9]$/.test(event.key) && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { event.preventDefault(); location.hash = Object.keys(VIEWS)[Number(event.key) - 1]; }
    });
    if ('ResizeObserver' in window) { const resize = new ResizeObserver(() => requestAnimationFrame(drawResourceCharts)); resize.observe($('main')); }
    else window.addEventListener('resize', drawResourceCharts);
  }
  async function start() {
    setup();
    try { await loadRuns(); } catch (error) { reportError(error); }
    await loadCore(); navigate(location.hash.slice(1) || 'overview');
    setInterval(() => { if (state.poll && !document.hidden) refresh(); }, 5000);
    // Read reports once to enrich summaries, without triggering any evaluation.
    loadReport();
  }
  start();
})();
