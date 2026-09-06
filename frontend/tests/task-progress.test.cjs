// 只运行任务表格渲染函数，验证分区列、空值兼容和日志摘要转义，不依赖真实登录。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../src/app.js'), 'utf8');
const context = vm.createContext({
  state: { taskZone: 'running', selectedTaskId: '' },
  escapeHtml: (v) => String(v).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  escapeAttr: (v) => String(v).replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
  taskStateText: (v) => v, formatDate: (v) => v || '-',
});
vm.runInContext(source.slice(source.indexOf('function renderTaskZoneTable('), source.indexOf('function taskToDraft(')), context);
const task = { task_id: '1', state: 'running', requirement: {}, duration_seconds: 125,
  progress: { text: 'Epoch 2/10', remaining_seconds: 120, scope: 'task' } };

test('执行区增加当前进度和时间补充信息', () => {
  context.state.taskZone = 'running';
  const html = context.renderTaskZoneTable([task]);
  assert.match(html, /<th>当前进度<\/th>/);
  assert.match(html, /Epoch 2\/10/);
  assert.match(html, /预估剩余时间：约 2 分/);
  assert.doesNotMatch(html, /运行时长/);
});
test('等待区保留原有列和时间', () => {
  context.state.taskZone = 'wait';
  const html = context.renderTaskZoneTable([task]);
  assert.doesNotMatch(html, /当前进度|预估剩余时间|Epoch 2\/10|运行时长/);
});
test('历史区只补运行时长，旧记录缺值显示未知', () => {
  context.state.taskZone = 'history';
  const html = context.renderTaskZoneTable([task]);
  assert.match(html, /运行时长：2 分 5 秒/);
  assert.doesNotMatch(html, /当前进度|预估剩余时间/);
  assert.match(context.renderTaskTimes({}), /运行时长：未知/);
  assert.equal(context.formatTaskDuration(0), '0 秒');
});
test('当前阶段预估带范围，日志摘要转义且陈旧值不继续倒计时', () => {
  context.state.taskZone = 'running';
  const item = { ...task, progress: { ...task.progress, text: '<img onerror=x>', scope: 'stage' } };
  assert.match(context.renderTaskTimes(item), /本阶段 约 2 分/);
  assert.doesNotMatch(context.renderTaskProgress(item), /<img/);
  item.progress.stale = true;
  item.progress.reason = '进度长时间未更新';
  assert.match(context.renderTaskTimes(item), /进度长时间未更新/);
  assert.doesNotMatch(context.renderTaskTimes(item), /约 2 分/);
});
test('停止中的执行区任务不显示旧进度和剩余时间', () => {
  context.state.taskZone = 'running';
  const item = { ...task, state: 'cancelling' };
  assert.equal(context.renderTaskProgress(item), '停止中');
  assert.doesNotMatch(context.renderTaskTimes(item), /约 2 分/);
});

test('缺少完整周期时明确显示粗估，完整周期估计不显示本阶段', () => {
  context.state.taskZone = 'running';
  const item = { ...task, progress: { ...task.progress, estimate_kind: 'rough' } };
  assert.match(context.renderTaskTimes(item), /粗估 约 2 分/);
  assert.doesNotMatch(context.renderTaskTimes(task), /本阶段|粗估/);
});

test('执行区补读显示百分比和字节量，完成后恢复训练进度', () => {
  context.state.taskZone = 'running';
  const item = { ...task, progress: { text: '日志读取中', catchup: {
    percent: 25, read_bytes: 2097152, total_bytes: 8388608,
  } } };
  const html = context.renderTaskZoneTable([item]);
  assert.match(html, /日志补读 25.0%/);
  assert.match(html, /<progress max="100" value="25" aria-label="日志补读进度">/);
  assert.match(html, /已读 2.0 \/ 8.0 MiB/);
  assert.doesNotMatch(context.renderTaskProgress(task), /<progress/);
  for (const zone of ['wait', 'history']) {
    context.state.taskZone = zone;
    assert.doesNotMatch(context.renderTaskZoneTable([item]), /日志补读|<progress/);
  }
});

test('补读条的非法数值不会进入 HTML 属性', () => {
  const item = { ...task, progress: { catchup: {
    percent: '" onmouseover="x', read_bytes: '<img>', total_bytes: Infinity,
  } } };
  const html = context.renderTaskProgress(item);
  assert.match(html, /value="0"/);
  assert.doesNotMatch(html, /onmouseover|<img>|Infinity|NaN/);
});

test('进度事件仅刷新执行区，状态变动仍刷新相关分区', () => {
  const scheduled = [];
  const ctx = vm.createContext({
    state: { user: {}, page: 'tasks', taskZone: 'wait', taskZoneCursors: {} },
    normalizeTaskZone: (zone) => zone,
    scheduleTaskRealtimeRefresh: (zone) => scheduled.push(zone),
  });
  vm.runInContext(source.slice(source.indexOf('function taskCursorKey('),
    source.indexOf('function scheduleTaskRealtimeRefresh(')), ctx);
  const cursor = { zones: {
    wait: { count: 1, max_task_id: 1, max_event_id: 1 },
    running: { count: 1, max_task_id: 2, max_event_id: 2, progress_version: 1 },
    history: { count: 1, max_task_id: 3, max_event_id: 3 },
  } };
  ctx.refreshTasksFromEvent(cursor);
  assert.deepEqual(scheduled.splice(0), ['wait']);
  cursor.zones.running.progress_version++;
  ctx.refreshTasksFromEvent(cursor);
  assert.deepEqual(scheduled.splice(0), []);
  ctx.state.taskZone = 'history';
  cursor.zones.running.progress_version++;
  ctx.refreshTasksFromEvent(cursor);
  assert.deepEqual(scheduled.splice(0), []);
  ctx.state.taskZone = 'running';
  cursor.zones.running.progress_version++;
  ctx.refreshTasksFromEvent(cursor);
  assert.deepEqual(scheduled.splice(0), ['running']);
  cursor.zones.running.max_event_id++;
  ctx.refreshTasksFromEvent(cursor);
  assert.deepEqual(scheduled.splice(0), ['running']);
});

test('仪表盘忽略纯进度通知，保留任务状态通知', () => {
  let listener, refreshes = 0;
  const ctx = vm.createContext({
    state: { user: {}, token: 'x', apiBase: '/api', page: 'dashboard' },
    isPresenterUser: () => false,
    autoRefreshDashboard: () => refreshes++,
    EventSource: class {
      addEventListener(event, fn) { listener = fn; }
    },
  });
  vm.runInContext(source.slice(source.indexOf('function updateTaskEventStream('),
    source.indexOf('function changedTaskZones(')), ctx);
  ctx.updateTaskEventStream();
  const cursor = { zones: {
    wait: { count: 1 }, running: { count: 2, progress_version: 1 }, history: { count: 3 },
  } };
  listener({ data: JSON.stringify(cursor) });
  assert.equal(refreshes, 1);
  cursor.zones.running.progress_version++;
  listener({ data: JSON.stringify(cursor) });
  assert.equal(refreshes, 1);
  cursor.zones.running.count--;
  cursor.zones.history.count++;
  listener({ data: JSON.stringify(cursor) });
  assert.equal(refreshes, 2);
});

test('总览 GPU 显示剩余占用时间、未知、可使用及外部占用', () => {
  const ctx = vm.createContext({
    formatTaskDuration: context.formatTaskDuration,
    escapeHtml: context.escapeHtml, escapeAttr: context.escapeAttr,
    schedulableGpus: (node) => node.gpus,
    speed: () => '-', miniMetric: () => '', nodeStateText: (v) => v,
    percent: (v) => `${v}%`, formatMb: (v) => String(v),
    vramUsedMb: () => 1000, vramUsedPercent: () => 10, metricBar: () => '',
  });
  vm.runInContext(source.slice(source.indexOf('function renderNodeCard('), source.indexOf('function renderPresenter(')), ctx);
  const node = { state: 'online', scheduling_enabled: true, gpus: [] };
  const occupied = { scheduled_occupied: true, remaining_occupancy_seconds: 120 };
  assert.equal(ctx.gpuOccupancyText(occupied, node), '预计剩余占用时间：约 2 分');
  for (const value of [null, undefined, 0, NaN]) {
    assert.equal(ctx.gpuOccupancyText({ ...occupied, remaining_occupancy_seconds: value }, node), '剩余占用时间未知');
  }
  assert.equal(ctx.gpuOccupancyText({ occupancy_status: 'available' }, node), '可使用');
  assert.equal(ctx.gpuOccupancyText({ occupancy_status: 'external' }, node), '外部占用');
  assert.equal(ctx.gpuOccupancyText({}, { ...node, state: 'offline' }), '不可调度');
  assert.equal(ctx.gpuOccupancyText({}, node), '可用状态未知');
  node.gpus = [{ ...occupied, gpu_index: 0, gpu_usage: 98, total_vram_mb: 16000 }];
  assert.match(ctx.renderNodeCard(node), /调度占用 是[\s\S]*预计剩余占用时间：约 2 分[\s\S]*gpu-bars/);
});
