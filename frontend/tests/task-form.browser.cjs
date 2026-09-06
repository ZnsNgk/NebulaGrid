// 浏览器回归测试：需要 Playwright 和 Chrome（或通过 CHROME_PATH 指定 Chromium）。
// 所有请求在浏览器内拦截，不连接真实后端、不创建真实任务；直接运行 node --test 本文件。
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const source = fs.readFileSync(process.env.NEBULAGRID_APP_SOURCE || path.join(__dirname, '../src/app.js'), 'utf8');
let browser;

before(async () => {
  browser = await chromium.launch(process.env.CHROME_PATH
    ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' });
});
after(async () => { await browser?.close(); });

async function openForm(t, mode, delayPredecessors = false) {
  const page = await browser.newPage();
  page.setDefaultTimeout(5000);
  t.after(() => page.close());
  await page.route('http://nebulagrid.test/**', (route) => route.fulfill({
    contentType: 'text/html', body: '<div id="app"></div>',
  }));
  await page.goto('http://nebulagrid.test/#/tasks');
  // 只替换自动登录入口，保留真实渲染、事件绑定、异步列表刷新和提交逻辑。
  await page.addScriptTag({ content: source.slice(0, source.lastIndexOf('loadMe().then(refreshPage)')) });
  await page.evaluate(({ mode, delayPredecessors }) => {
    state.user = { username: 'tester', role: 'student', permissions: ['*'] };
    state.data.envs = [{ id: 1, name: 'test-env', state: 'available' }];
    state.data.nodes = [{ id: 1, name: 'test-node', gpus: [{ model: 'Test GPU', schedulable: true }] }];
    state.data.tasks = { items: [{ task_id: 'task-1', state: 'wait', command: 'old command', requirement: {} }], total: 1 };
    state.selectedTaskId = 'task-1';
    window.listRefreshes = 0;
    window.submissions = [];
    window.failSubmit = false;
    window.predecessorGate = delayPredecessors ? new Promise((resolve) => { window.releasePredecessors = resolve; }) : Promise.resolve();
    api = async (url, options = {}) => {
      if (options.method === 'POST' || options.method === 'PATCH') {
        window.submissions.push({ url, ...options, body: JSON.parse(options.body) });
        if (window.failSubmit) throw new Error('模拟提交失败');
        return { data: {} };
      }
      if (url.startsWith('/files/list?')) return { data: { path: '/project', items: [] } };
      if (!url.startsWith('/tasks?')) throw new Error(`非预期请求：${url}`);
      if (url.includes('page_size=200')) {
        await window.predecessorGate;
        return { data: { items: [{ task_id: 'pre-1', state: 'wait' }] } };
      }
      window.listRefreshes++;
      return { data: { items: [], total: window.listRefreshes } };
    };
    // 提交成功后的页面加载与本回归无关，避免请求登录接口。
    refreshPage = async () => {};
    render();
    openTaskForm(mode);
  }, { mode, delayPredecessors });
  if (!delayPredecessors) await page.waitForFunction(() => !state.taskPredecessorLoading);
  return page;
}

async function fillForm(page, mode) {
  await page.selectOption('[name="env_id"]', '1');
  await page.selectOption('[name="node_id"]', '1');
  await page.fill('[name="need_gpus"]', '0');
  await page.check('#taskGpuTypeOptions input');
  await page.selectOption('[name="predecessor_task_id"]', 'pre-1');
  await page.fill('[name="description"]', '  保留首尾空格  ');
  await page.check('[name="urgent"]');
  await page.check('[name="allow_gpu_reuse"]');
  await page.locator('[name="on_hold"]').setChecked(mode !== 'batch');
  await page.fill(`[name="${mode === 'batch' ? 'commands' : 'command'}"]`,
    mode === 'batch' ? 'python a.py\n\n# 注释\npython b.py  ' : 'python train.py --name "测试"  ');
}

async function values(page) {
  return page.evaluate(() => {
    const form = document.querySelector('#taskForm');
    return Array.from(form.elements).map((input) => ({ name: input.name, value: input.value, checked: input.checked }));
  });
}

for (const mode of ['add', 'batch', 'edit']) {
  test(`${mode}：连续实时刷新保留所有字段、光标和提交数据`, async (t) => {
    const page = await openForm(t, mode);
    await fillForm(page, mode);
    const expected = await values(page);
    await page.evaluate(() => document.activeElement.setSelectionRange(2, 8, 'backward'));
    await page.evaluate(async () => {
      for (let i = 0; i < 3; i++) await runTaskRealtimeRefresh('wait');
    });
    assert.deepEqual(await values(page), expected);
    assert.deepEqual(await page.evaluate(() => [document.activeElement.name,
      document.activeElement.selectionStart, document.activeElement.selectionEnd, document.activeElement.selectionDirection]),
    [mode === 'batch' ? 'commands' : 'command', 2, 8, 'backward']);
    assert.equal(await page.evaluate(() => window.listRefreshes), 3);
    assert.match(await page.locator('.task-list-summary').innerText(), /共 3 条/);
    const payload = await page.evaluate((mode) => taskPayloadFromForm(document.querySelector('#taskForm'), mode), mode);
    await page.evaluate(() => { window.failSubmit = true; });
    await page.click('#taskForm button[type="submit"]');
    await page.waitForFunction(() => !state.loading);
    assert.deepEqual(await values(page), expected);
    await page.evaluate(() => { window.failSubmit = false; });
    await page.click('#taskForm button[type="submit"]');
    await page.waitForFunction(() => !state.drawer);
    const submissions = await page.evaluate(() => window.submissions);
    assert.equal(submissions.length, 2);
    assert.deepEqual(submissions[1].body, payload);
    assert.equal(submissions[1].url, mode === 'batch' ? '/tasks/batch' : mode === 'edit' ? '/tasks/task-1' : '/tasks');
    assert.equal(submissions[1].method, mode === 'edit' ? 'PATCH' : 'POST');
    assert.equal(await page.evaluate(() => state.taskFormDraft), null);
  });
}

test('前驱异步加载及清空字段后重绘保留尚未完成的输入', async (t) => {
  const page = await openForm(t, 'batch', true);
  await page.fill('[name="commands"]', '  python a.py\npython b.py  ');
  await page.fill('[name="need_gpus"]', '');
  const expected = await values(page);
  await page.evaluate(() => runTaskRealtimeRefresh('wait'));
  assert.deepEqual(await values(page), expected);
  await page.evaluate(() => window.releasePredecessors());
  await page.waitForFunction(() => !state.taskPredecessorLoading);
  assert.equal(await page.inputValue('[name="commands"]'), '  python a.py\npython b.py  ');
  assert.equal(await page.inputValue('[name="need_gpus"]'), '');
});

test('任务事件触发的请求尚未完成时继续输入，响应返回后仍保留最新内容', async (t) => {
  const page = await openForm(t, 'add');
  await page.evaluate(() => {
    const originalApi = api;
    api = async (...args) => {
      await new Promise((resolve) => { window.releaseTaskList = resolve; });
      return originalApi(...args);
    };
    refreshTasksFromEvent({ zones: { wait: { count: 2, max_event_id: 10 } } });
  });
  await page.waitForFunction(() => !!window.releaseTaskList);
  await fillForm(page, 'add');
  const expected = await values(page);
  await page.evaluate(() => window.releaseTaskList());
  await page.waitForFunction(() => !state.taskRealtimeBusy);
  assert.equal(await page.evaluate(() => window.listRefreshes), 1);
  assert.deepEqual(await values(page), expected);
});

test('目录选择期间刷新、确认目录及关闭重开不会覆盖或串用草稿', async (t) => {
  const page = await openForm(t, 'add');
  await fillForm(page, 'add');
  await page.evaluate(() => openTaskWorkdirPicker());
  await page.evaluate(() => runTaskRealtimeRefresh('wait'));
  await page.evaluate(() => { state.fileTargetPicker.currentPath = '/new/project'; });
  await page.locator('[data-file-picker-confirm]').click();
  assert.equal(await page.inputValue('[name="workdir"]'), '/new/project');
  assert.equal(await page.inputValue('[name="command"]'), 'python train.py --name "测试"  ');
  await page.click('[data-action="close-drawer"]');
  assert.equal(await page.evaluate(() => state.taskFormDraft), null);
  await page.evaluate(() => openTaskForm('batch'));
  assert.equal(await page.inputValue('[name="commands"]'), '');
  assert.equal(await page.inputValue('[name="workdir"]'), '/');
  assert.equal(await page.isChecked('[name="on_hold"]'), true);
});
