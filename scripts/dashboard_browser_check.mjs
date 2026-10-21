/** Real Chrome interaction acceptance; launched by dashboard_acceptance.py. */
import {mkdir, writeFile} from 'node:fs/promises';
import {basename, dirname, join} from 'node:path';
import {openBrowser} from './browser_cdp.mjs';

const options = {};
for (let i = 2; i < process.argv.length; i += 2) {
  if (!process.argv[i].startsWith('--') || !process.argv[i + 1]) throw new Error('Use --name value arguments');
  options[process.argv[i].slice(2)] = process.argv[i + 1];
}
const output = options.output || 'validation/dashboard/acceptance-browser.json';
for (const key of ['url', 'fixture-url']) {
  const url = new URL(options[key]);
  if (!['127.0.0.1', 'localhost'].includes(url.hostname) || url.protocol !== 'http:') throw new Error('Only explicit local HTTP dashboards may be tested');
}
const checks = {}, failures = [], screenshots = [], layouts = [];
const check = (name, condition, detail) => {
  checks[name] = Boolean(condition);
  if (!condition) failures.push({check: name, detail});
};
await mkdir(dirname(output), {recursive: true});
const screenshotDirectory = join(dirname(output), basename(output, '.json'));
await mkdir(screenshotDirectory, {recursive: true});
const browser = await openBrowser();
const {client} = browser;
const evaluate = expression => client.evaluate(expression);
const wait = expression => client.waitFor(expression, 20000);

async function screenshot(name) {
  const path = join(screenshotDirectory, name + '.png');
  await client.screenshot(path);
  screenshots.push(path);
}

async function layout(name) {
  const dimensions = await evaluate(`({viewport:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth,height:innerHeight})`);
  layouts.push({name, ...dimensions});
  check(name + '_no_document_horizontal_overflow', dimensions.document <= dimensions.viewport + 1 && dimensions.body <= dimensions.viewport + 1, dimensions);
}

async function ready() {
  await wait(`document.querySelector('#overview-metrics')?.children.length > 0 && document.querySelector('#last-updated')?.textContent !== '尚未同步'`);
}

async function navigate(view) {
  const selector = `a[data-view="${view}"]`;
  if (await evaluate(`innerWidth < 900 && !document.querySelector('#sidebar').classList.contains('open')`)) {
    // The accessible toggle is authoritative; do not depend on CSS class names.
    if (await evaluate(`document.querySelector('#nav-toggle').getAttribute('aria-expanded') !== 'true'`)) await client.click('#nav-toggle');
    await wait(`Math.abs(document.querySelector('#sidebar').getBoundingClientRect().left) < 1`);
  }
  await client.click(selector);
  await wait(`!document.querySelector('#panel-${view}').hidden`);
  if (await evaluate('innerWidth < 720')) await wait(`document.querySelector('#sidebar').getBoundingClientRect().right <= 1`);
  check('navigation_' + view, await evaluate(`document.querySelectorAll('.view-panel:not([hidden])').length === 1 && document.querySelector('[data-view="${view}"]').getAttribute('aria-current') === 'page'`));
}

async function stopRefresh() {
  if (await evaluate(`document.querySelector('#poll-toggle').getAttribute('aria-pressed') !== 'true'`)) await client.click('#poll-toggle');
  check('pause_refresh_is_page_only', await evaluate(`document.querySelector('#poll-toggle').getAttribute('aria-pressed') === 'true'`));
}

try {
  await client.send('Page.navigate', {url: options.url + '/?run=' + encodeURIComponent(options.run)});
  await ready();
  await wait(`document.querySelector('#run-select').value === ${JSON.stringify(options.run)}`);
  await stopRefresh();
  check('diagnostic_scope_visible', await evaluate(`!document.querySelector('#diagnostic-badge').hidden && document.querySelector('#scope-text').textContent.length > 20`));
  check('offline_state_visible', await evaluate(`document.querySelector('#run-status').textContent.length > 0 && document.querySelector('#connection').textContent.length > 0`));
  await layout('desktop_overview');
  await screenshot('desktop-overview');

  await navigate('events');
  await wait(`document.querySelectorAll('#events-table tr').length > 0`);
  const firstCount = await evaluate(`document.querySelectorAll('#events-table tr').length`);
  await client.click('#events-more');
  await wait(`document.querySelectorAll('#events-table tr').length > ${firstCount}`);
  check('events_load_older', true);
  await client.fill('#events-filter [name=actor]', 'player1');
  await client.fill('#events-filter [name=category]', 'model');
  await client.click('#events-filter button[type=submit]');
  await wait(`document.querySelectorAll('#events-table tr').length > 0`);
  await wait(`Array.from(document.querySelectorAll('#events-table tr')).every(r => /player1|玩家\\s*1/i.test(r.children[1]?.textContent || ''))`);
  check('event_filters_applied_in_browser', true);
  await client.click('#events-table tr');
  await wait(`!document.querySelector('#detail-drawer').hidden && document.querySelector('#drawer-body').textContent.length > 30`);
  check('event_detail_opens', true);
  await client.click('#drawer-close');
  await wait(`document.querySelector('#detail-drawer').hidden`);

  await navigate('programs');
  await wait(`document.querySelectorAll('#programs-table tr').length > 0`);
  await client.click('#programs-table tr');
  await wait(`!document.querySelector('#detail-drawer').hidden && document.querySelector('#drawer-body').textContent.length > 50`);
  check('program_detail_opens', true);
  await screenshot('desktop-program-detail');
  await client.click('#drawer-close');
  await client.click('#job-tab');
  await wait(`document.querySelector('#job-tab').getAttribute('aria-selected') === 'true' && document.querySelectorAll('#programs-table tr').length === 4`);
  check('four_background_jobs_visible', true);

  await navigate('observer');
  await wait(`document.querySelector('#observer-feed').children.length > 0`);
  const observerCount = await evaluate(`document.querySelector('#observer-feed').children.length`);
  await client.click('#observer-more');
  await wait(`document.querySelector('#observer-feed').children.length > ${observerCount}`);
  check('observer_load_older', true);
  await screenshot('desktop-observer');

  await navigate('evidence');
  await wait(`document.querySelector('#evaluation-summary').textContent.length > 20`);
  check('evaluator_and_observer_boundary_visible', await evaluate(`document.querySelector('#panel-evidence').textContent.includes('Observer') && document.querySelector('#stage-timeline').textContent.length > 0`));
  await navigate('resources');
  await wait(`document.querySelectorAll('#cost-table tr').length > 0`);
  check('cost_role_purpose_groups_visible', await evaluate(`/observer/i.test(document.querySelector('#cost-table').textContent) && /压缩|compaction|上下文交接/.test(document.querySelector('#cost-table').textContent)`));
  await layout('desktop_resources');
  await screenshot('desktop-resources');

  await client.fill('#run-select', options['large-run']);
  await wait(`document.querySelector('#run-select').value === ${JSON.stringify(options['large-run'])}`);
  await navigate('overview');
  await wait(`/165[ ,，]?419/.test(document.querySelector('#overview-metrics').textContent)`);
  check('large_real_ledger_switch', true);
  await navigate('chat');
  await wait(`document.querySelector('#chat-feed').children.length > 0`);
  check('chat_source_boundary_visible', await evaluate(`document.querySelector('#chat-feed').textContent.includes('客户端') || document.querySelector('#chat-caption').textContent.includes('客户端')`));
  await navigate('overview');
  await client.viewport(390, 844);
  await wait(`innerWidth === 390`);
  await layout('mobile_overview');
  await screenshot('mobile-overview');
  await navigate('events');
  await wait(`document.querySelectorAll('#events-table tr').length > 0`);
  await layout('mobile_events');
  await screenshot('mobile-events');
  await client.click('#events-table tr');
  await wait(`!document.querySelector('#detail-drawer').hidden`);
  await layout('mobile_detail');
  await screenshot('mobile-event-detail');
  await client.click('#drawer-close');
  const originalPosts = client.events.filter(event => event.method === 'Network.requestWillBeSent' && event.params.request.method === 'POST');
  check('original_runs_browsed_without_mutation', originalPosts.length === 0, originalPosts.map(event => event.params.request.url));

  await client.viewport(1440, 1000);
  await client.send('Page.navigate', {url: options['fixture-url']});
  await ready();
  await stopRefresh();
  await navigate('resources');
  await wait(`/最近采样.*100 个持久采样/.test(document.querySelector('#resource-paging-scope').textContent)`);
  check('resource_latest_state_separate_from_history', await evaluate(`/512/.test(document.querySelector('#activity-current').textContent)`));
  const resourceRanges = [];
  for (let page = 0; page < 6; page++) {
    const previousRange = await evaluate(`document.querySelector('#resource-range').textContent`);
    await client.click('#resources-more');
    await wait(`document.querySelector('#resource-range').textContent !== ${JSON.stringify(previousRange)} && !document.querySelector('#resources-more').disabled`);
    resourceRanges.push(await evaluate(`({range:document.querySelector('#resource-range').textContent,scope:document.querySelector('#resource-paging-scope').textContent,current:document.querySelector('#activity-current').textContent})`));
  }
  check('resource_window_bounded_at_600_points', await evaluate(`/历史窗口.*600 个持久采样/.test(document.querySelector('#resource-paging-scope').textContent)`));
  const expectedHistory = await evaluate(`(() => {const start=Date.parse('2026-10-21T06:00:00Z');const stamp=i=>new Date(start+i*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});return stamp(35)+' — '+stamp(634)})()`);
  check('resource_history_cursor_moves_past_600_without_gaps', await evaluate(`document.querySelector('#resource-range').textContent === ${JSON.stringify(expectedHistory)}`), resourceRanges);
  check('resource_history_excludes_latest_state', await evaluate(`parseFloat(document.querySelector('#activity-current').textContent) < 100 && document.querySelector('#resource-chart-note').textContent.includes('不会混入新采样')`));
  await client.click('#resources-latest');
  await wait(`/最近采样.*100 个持久采样/.test(document.querySelector('#resource-paging-scope').textContent) && document.querySelector('#resources-latest').disabled`);
  check('resource_return_latest_restores_newest_window', await evaluate(`/512/.test(document.querySelector('#activity-current').textContent)`));
  await navigate('chat');
  await wait(`document.querySelector('#chat-feed').textContent.includes('<img')`);
  check('hostile_content_is_text_not_html', await evaluate(`!window.unsafeExecuted && !document.querySelector('img[src="x"]') && !Array.from(document.scripts).some(s => s.textContent.includes('bad</script>'))`));
  await client.click('#control-open');
  await wait(`document.querySelector('#control-dialog').open`);
  await client.click('[data-command="pause"]');
  await wait(`!document.querySelector('#control-confirmation').hidden`);
  check('control_requires_second_confirmation', !client.events.some(event => event.method === 'Network.requestWillBeSent' && event.params.request.method === 'POST'));
  await client.click('#control-confirm');
  await wait(`document.querySelector('#control-feedback').textContent.includes('fixture-control-one')`);
  const posts = client.events.filter(event => event.method === 'Network.requestWillBeSent' && event.params.request.method === 'POST');
  check('only_fixture_control_posted_once', posts.length === 1 && new URL(posts[0].params.request.url).origin === options['fixture-url']
      && JSON.parse(posts[0].params.request.postData).command === 'pause', posts.map(event => ({url: event.params.request.url, method: event.params.request.method})));
} catch (error) {
  failures.push({type: error.name, message: error.message});
  try {await screenshot('failure');} catch {}
} finally {
  const errors = client.events.filter(event => event.method === 'Runtime.exceptionThrown'
      || event.method === 'Runtime.consoleAPICalled' && event.params.type === 'error'
      || event.method === 'Log.entryAdded' && event.params.entry.level === 'error');
  const failedRequests = client.events.filter(event => event.method === 'Network.loadingFailed' && !event.params.canceled);
  const httpErrors = client.events.filter(event => event.method === 'Network.responseReceived' && event.params.response.status >= 400)
      .map(event => ({url: event.params.response.url, status: event.params.response.status}));
  check('no_browser_runtime_or_console_errors', errors.length === 0, errors);
  check('no_browser_network_failures', failedRequests.length === 0 && httpErrors.length === 0, {failedRequests, httpErrors});
  const result = {at: new Date().toISOString(), status: Object.values(checks).every(Boolean) && failures.length === 0 ? 'passed' : 'failed',
    checks, failures, browser: browser.version.Browser, node: process.version, screenshots, layouts,
    errors, failedRequests, httpErrors, actual_minecraft: false, paid_model: false, scope: 'Real local Chrome, existing offline ledgers, temporary confirmed-control fixture only'};
  await browser.close();
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify({status: result.status, checks: Object.keys(checks).length, failures: result.failures, output}));
  process.exitCode = result.status === 'passed' ? 0 : 1;
}
