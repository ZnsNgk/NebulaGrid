// 使用真实页面渲染、按钮事件和异步加载，所有业务请求由夹具替代，不接触真实用户。
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const source = fs.readFileSync(path.join(__dirname, '../src/app.js'), 'utf8');
const styles = fs.readFileSync(path.join(__dirname, '../src/styles.css'), 'utf8');
let browser;
before(async () => { browser = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }); });
after(async () => { await browser?.close(); });

async function openUsage(t, role = 'student', empty = false) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  page.setDefaultTimeout(5000);
  t.after(() => page.close());
  await page.route('http://nebulagrid.test/**', (route) => route.request().url().endsWith('/load_page.png')
    ? route.fulfill({ contentType: 'image/png', body: fs.readFileSync(path.join(__dirname, '../load_page.png')) })
    : route.fulfill({ contentType: 'text/html', body: '<meta name="viewport" content="width=device-width, initial-scale=1"><div id="app"></div>' }));
  await page.goto('http://nebulagrid.test/#/usage');
  await page.addStyleTag({ content: styles });
  await page.addScriptTag({ content: source.slice(0, source.lastIndexOf('loadMe().then(refreshPage)')) });
  await page.evaluate(async ({ role, empty }) => {
    state.user = { id: 1, username: 'tester', role, permissions: role === 'viewer' ? ['presenter:read'] : ['dashboard:read', 'usage:read'] };
    loadMe = async () => {};
    const total = { submitted: 12, executed: 9, succeeded: 6, failed: 2, cancelled: 1, running: 1,
      waiting: 2, other: 0, runtime_seconds: 7200, gpu_hours: 4, unknown_duration_tasks: 0, unknown_gpu_tasks: 0 };
    const users = [{ id: 1, username: 'tester', real_name: '测试学生', role: 'student', ...total, occupied_seconds: 10800 }];
    const tasks = { personal: total, summary: total, users: role === 'student' ? [] : users, generated_at: '2026-09-14T12:00:00+08:00' };
    window.usagePaths = [];
    window.failUsage = false;
    window.slowUsage = false;
    api = async (url) => {
      window.usagePaths.push(url);
      if (window.failUsage) throw new Error('模拟统计读取失败');
      if (url === '/usage/tasks') return { data: tasks };
      if (!url.startsWith('/usage/nodes?')) throw new Error(`非预期请求 ${url}`);
      const days = Number(new URLSearchParams(url.split('?')[1]).get('days'));
      if (window.slowUsage && days === 30) await new Promise((resolve) => { window.releaseUsage = resolve; });
      const daily = Array.from({ length: days }, (_, i) => ({ date: new Date(Date.UTC(2026, 8, 14 - days + 1 + i)).toISOString().slice(0, 10),
        gpu_usage_percent: i % 2 ? 42.5 : null, cpu_usage_percent: 20, occupied_seconds: 7200,
        own_occupied_seconds: 3600, occupancy_percent: 25, metric_hours: 12, expected_metric_hours: 24 }));
      return { data: { days, generated_at: tasks.generated_at, start_at: '2026-09-08T00:00:00+08:00',
        metrics_end_at: '2026-09-14T12:00:00+08:00', own_occupied_seconds: 10800, users, metrics_status: 'not_configured', nodes: empty ? [] : [
          { id: 1, name: 'GPU-01', daily, users: days === 7 ? users : [], occupied_seconds: 14400, average_daily_seconds: 3600,
            own_occupied_seconds: 7200, gpu_usage_percent: null, cpu_usage_percent: 20, occupancy_percent: 25 },
          { id: 2, name: '<img src=x onerror=alert(1)>', daily, users: [], occupied_seconds: 7200, average_daily_seconds: 1800,
            own_occupied_seconds: 3600, gpu_usage_percent: 42.5, cpu_usage_percent: 20, occupancy_percent: 20 },
        ] } };
    };
    if (role !== 'viewer') await loadUsageData();
    render();
  }, { role, empty });
  return page;
}

test('学生入口、两个页签、周期切换和节点每日明细', async (t) => {
  const page = await openUsage(t);
  assert.equal(await page.locator('[data-nav="usage"]').count(), 1);
  assert.match(await page.locator('main').innerText(), /我的任务/);
  assert.doesNotMatch(await page.locator('main').innerText(), /任务明细/);
  await page.click('[data-usage-tab="nodes"]');
  await page.waitForSelector('[data-usage-node="1"]');
  assert.match(await page.locator('main').innerText(), /无数据/);
  assert.equal(await page.locator('[data-usage-node-users]').count(), 0);
  assert.equal(await page.locator('[data-usage-period-users]').count(), 0);
  assert.equal(await page.locator('.usage-chart-column').count(), 7);
  await page.click('[data-usage-days="30"]');
  await page.waitForFunction(() => state.data.usageNodes?.days === 30);
  assert.equal(await page.locator('.usage-chart-column').count(), 30);
  await page.click('[data-usage-node="2"]');
  assert.match(await page.locator('main').innerText(), /<img src=x onerror=alert\(1\)> · 每日明细/);
  assert.equal(await page.locator('main img').count(), 0);
});

test('导师和管理员显示相应用户汇总，展示者没有用量入口', async (t) => {
  for (const role of ['mentor', 'admin']) {
    const page = await openUsage(t, role);
    assert.match(await page.locator('main').innerText(), role === 'admin' ? /所有用户汇总/ : /本人及名下学生汇总/);
    await page.click('[data-usage-tab="nodes"]');
    await page.waitForSelector('[data-usage-node="1"]');
    assert.match(await page.locator('main').innerText(), /本期提交任务/);
  }
  const page = await openUsage(t, 'viewer');
  assert.equal(await page.locator('[data-nav="usage"]').count(), 0);
});

test('失败重试、空节点和快速切换只保留最新周期', async (t) => {
  const page = await openUsage(t);
  await page.evaluate(() => { window.failUsage = true; });
  await page.click('[data-usage-tab="nodes"]');
  await page.waitForSelector('[role="alert"]');
  assert.match(await page.locator('[role="alert"]').innerText(), /模拟统计读取失败/);
  await page.evaluate(() => { window.failUsage = false; });
  await page.click('[role="alert"] [data-action="refresh"]');
  await page.waitForSelector('[data-usage-node="1"]');
  await page.evaluate(async () => {
    window.slowUsage = true;
    state.usageDays = 30;
    window.oldUsageRequest = loadUsageData();
    state.usageDays = 90;
    await loadUsageData();
    window.releaseUsage();
    await window.oldUsageRequest;
    render();
  });
  assert.equal(await page.evaluate(() => state.data.usageNodes.days), 90);
  const emptyPage = await openUsage(t, 'student', true);
  await emptyPage.click('[data-usage-tab="nodes"]');
  await emptyPage.waitForFunction(() => state.data.usageNodes);
  assert.match(await emptyPage.locator('main').innerText(), /暂无可见计算节点/);
});

test('历史查询超时与授权失败分别提示，继续显示任务占用', async (t) => {
  const page = await openUsage(t);
  await page.click('[data-usage-tab="nodes"]');
  await page.waitForSelector('[data-usage-node="1"]');
  for (const [status, message] of [['timeout', '小时汇总读取超时'], ['access_denied', '监控数据读取权限不足'],
                                 ['query_error', '历史监控查询被数据服务拒绝']]) {
    await page.evaluate((status) => { state.data.usageNodes.metrics_status = status; render(); }, status);
    const text = await page.locator('main').innerText();
    assert.ok(text.includes(message));
    assert.ok(text.includes('我的服务器占用'));
    assert.ok(text.includes('3 小时'));
    assert.ok(!text.includes('监控服务暂不可用'));
  }
});

test('桌面及窄屏统计布局保持页面宽度', async (t) => {
  const page = await openUsage(t, 'admin');
  if (process.env.USAGE_SCREENSHOT_DIR) {
    fs.mkdirSync(process.env.USAGE_SCREENSHOT_DIR, { recursive: true });
    await page.screenshot({ path: path.join(process.env.USAGE_SCREENSHOT_DIR, 'usage-tasks.png'), fullPage: true });
  }
  await page.click('[data-usage-tab="nodes"]');
  await page.waitForSelector('[data-usage-node="1"]');
  if (process.env.USAGE_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.USAGE_SCREENSHOT_DIR, 'usage-nodes.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  // 等布局及合成帧稳定后截图，避免缩放后的旧桌面背景图层残留在长截图中。
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  if (process.env.USAGE_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.USAGE_SCREENSHOT_DIR, 'usage-mobile.png'), fullPage: true });
});

// 补算缺口与离线零值必须区分，同时向用户说明当前小时未计入。
test('小时补算进度、零值和统计截止提示', async (t) => {
  const page = await openUsage(t);
  await page.click('[data-usage-tab="nodes"]');
  await page.waitForSelector('[data-usage-node="1"]');
  await page.evaluate(() => {
    state.data.usageNodes.metrics_status = 'building';
    state.data.usageNodes.nodes[0].daily[0].gpu_usage_percent = 0;
    render();
  });
  const text = await page.locator('main').innerText();
  assert.match(text, /部分小时汇总缺失/);
  assert.match(text, /不计当前未结束的小时/);
  assert.match(text, /已汇总 \/ 应汇总小时/);
  assert.ok(text.includes('12 / 24'));
  assert.ok(text.includes('0.0%'));
  assert.ok(text.includes('无数据'));
});

test('服务重启缺一小时仍显示日均，并明确排除缺失小时', async (t) => {
  const page = await openUsage(t);
  await page.click('[data-usage-tab="nodes"]');
  await page.waitForSelector('[data-usage-node="1"]');
  await page.evaluate(() => {
    state.data.usageNodes.metrics_status = 'building';
    Object.assign(state.data.usageNodes.nodes[0].daily[0], {
      metric_hours: 23, expected_metric_hours: 24, cpu_usage_percent: 46, gpu_usage_percent: 69,
    });
    render();
  });
  const text = await page.locator('main').innerText();
  assert.match(text, /缺失小时已排除/);
  const row = page.locator('.usage-table tbody tr').filter({ hasText: '2026-09-08' });
  assert.equal(await row.count(), 1);
  assert.match(await row.innerText(), /23 \/ 24/);
  assert.match(await row.innerText(), /46.0%/);
  assert.match(await row.innerText(), /69.0%/);
  assert.doesNotMatch(await row.innerText(), /无数据/);
});

test('导师与管理员查看本期卡时、节点用户、零时长过滤及空周期', async (t) => {
  for (const role of ['mentor', 'admin']) {
    const page = await openUsage(t, role);
    await page.click('[data-usage-tab="nodes"]');
    await page.waitForSelector('[data-usage-node-users]');
    const period = page.locator('[data-usage-period-users]');
    assert.match(await period.innerText(), /GPU 占用卡时（可见节点）/);
    assert.match(await period.locator('tbody tr').first().innerText(), /4.00/);
    const detail = page.locator('[data-usage-node-users]');
    assert.match(await detail.innerText(), /GPU-01 · 用户用量/);
    assert.match(await detail.innerText(), /3 小时/);
    assert.match(await detail.innerText(), /4.00/);
    await page.evaluate(() => {
      state.data.usageNodes.nodes[0].users.push(
        { username: 'zero', real_name: '零时长用户', role: 'student', occupied_seconds: 0, gpu_hours: 0 },
        { username: 'cpu', real_name: '<img src=x onerror=alert(1)>', role: 'student', occupied_seconds: 3600, gpu_hours: 0 },
      );
      render();
    });
    assert.equal(await detail.locator('tbody tr').count(), 2);
    assert.doesNotMatch(await detail.innerText(), /零时长用户/);
    assert.match(await detail.innerText(), /1 小时/);
    assert.match(await detail.innerText(), /0.00/);
    assert.equal(await detail.locator('img').count(), 0);
    await page.click('[data-usage-node="2"]');
    assert.equal(await detail.locator('tbody tr').count(), 0);
    assert.match(await detail.innerText(), /该周期内暂无可查看的用户占用记录/);
    await page.click('[data-usage-node="1"]');
    assert.equal(await detail.locator('tbody tr').count(), 2);
    await page.click('[data-usage-days="30"]');
    await page.waitForFunction(() => state.data.usageNodes?.days === 30);
    assert.equal(await detail.locator('tbody tr').count(), 0);
    assert.match(await detail.innerText(), /近 30 天/);
  }
});
