const state = {
  apiBase: localStorage.getItem("ng_api_base") || "http://127.0.0.1:8000/api",
  token: localStorage.getItem("ng_token") || "",
  user: null,
  view: "dashboard",
  message: "",
  data: {
    health: null,
    dashboard: null,
    nodes: [],
    tasks: { items: [], total: 0, page: 1, page_size: 20 },
    envs: [],
    files: { path: "/workspace", items: [] },
    preview: null,
    users: [],
    settings: [],
    auditLogs: { items: [], total: 0, page: 1, page_size: 20 },
    taskLog: "",
  },
};

const navItems = [
  { id: "dashboard", label: "仪表盘", permission: "dashboard:read" },
  { id: "nodes", label: "节点", permission: "nodes:read" },
  { id: "tasks", label: "任务", permission: "tasks:read" },
  { id: "envs", label: "环境", permission: "envs:read" },
  { id: "files", label: "文件", permission: "files:read" },
  { id: "users", label: "用户", permission: "users:read" },
  { id: "admin", label: "管理", permission: "admin:settings:read" },
];

function can(permission) {
  if (!state.user) return false;
  return state.user.permissions.includes("*") || state.user.permissions.includes(permission);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(`${state.apiBase}${path}`, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof payload === "string" ? payload : payload.message;
    throw new Error(message || `HTTP ${response.status}`);
  }
  return payload;
}

function notify(message) {
  state.message = message;
  render();
}

async function run(action, successMessage) {
  try {
    await action();
    if (successMessage) notify(successMessage);
    render();
  } catch (error) {
    notify(error.message);
  }
}

async function loadHealth() {
  const payload = await api("/health");
  state.data.health = payload.data;
}

async function login(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  state.apiBase = String(form.get("apiBase") || "").replace(/\/$/, "");
  localStorage.setItem("ng_api_base", state.apiBase);
  const payload = await api("/auth/login", {
    method: "POST",
    body: JSON.stringify({ identity: form.get("identity"), password: form.get("password") }),
  });
  state.token = payload.data.access_token;
  state.user = payload.data.user;
  localStorage.setItem("ng_token", state.token);
  await refreshCurrentView();
}

async function logout() {
  state.token = "";
  state.user = null;
  localStorage.removeItem("ng_token");
  render();
}

async function loadMe() {
  if (!state.token) return;
  const payload = await api("/auth/me");
  state.user = payload.data;
}

async function loadDashboard() {
  const payload = await api("/dashboard/summary");
  state.data.dashboard = payload.data;
}

async function loadNodes() {
  const payload = await api("/nodes");
  state.data.nodes = payload.data;
}

async function loadTasks() {
  const payload = await api("/tasks");
  state.data.tasks = payload.data;
}

async function loadEnvs() {
  const payload = await api("/envs");
  state.data.envs = payload.data;
}

async function loadFiles(path = state.data.files.path || "/workspace") {
  const payload = await api(`/files/list?path=${encodeURIComponent(path)}`);
  state.data.files = payload.data;
}

async function loadUsers() {
  const payload = await api("/users");
  state.data.users = payload.data;
}

async function loadAdmin() {
  const [settings, auditLogs] = await Promise.all([api("/admin/settings"), api("/admin/audit-logs")]);
  state.data.settings = settings.data;
  state.data.auditLogs = auditLogs.data;
}

async function refreshCurrentView() {
  await loadMe();
  const loaders = {
    dashboard: loadDashboard,
    nodes: loadNodes,
    tasks: loadTasks,
    envs: loadEnvs,
    files: () => loadFiles(),
    users: loadUsers,
    admin: loadAdmin,
  };
  if (loaders[state.view]) await loaders[state.view]();
}

function setView(view) {
  state.view = view;
  run(refreshCurrentView);
}

function fieldValue(form, name) {
  return String(new FormData(form).get(name) || "").trim();
}

async function submitNode(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const gpuModels = fieldValue(form, "gpu_models").split(",").map((item) => item.trim()).filter(Boolean);
  await api("/admin/nodes", {
    method: "POST",
    body: JSON.stringify({
      name: fieldValue(form, "name"),
      ip: fieldValue(form, "ip"),
      ssh_user: fieldValue(form, "ssh_user") || "ddltm",
      gpu_models: gpuModels,
    }),
  });
  form.reset();
  await loadNodes();
}

async function submitTask(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/tasks", {
    method: "POST",
    body: JSON.stringify({
      description: fieldValue(form, "description"),
      env_id: fieldValue(form, "env_id") ? Number(fieldValue(form, "env_id")) : null,
      workdir: fieldValue(form, "workdir") || "/workspace",
      command: fieldValue(form, "command"),
      priority: Number(fieldValue(form, "priority") || 0),
      on_hold: form.elements.on_hold.checked,
      requirement: {
        need_gpus: Number(fieldValue(form, "need_gpus") || 0),
        gpu_types: fieldValue(form, "gpu_types").split(",").map((item) => item.trim()).filter(Boolean),
        allow_gpu_reuse: form.elements.allow_gpu_reuse.checked,
      },
    }),
  });
  form.reset();
  await loadTasks();
  await loadDashboard();
}

async function cancelTask(taskId) {
  await api(`/tasks/${taskId}/cancel`, { method: "POST" });
  await loadTasks();
}

async function resubmitTask(taskId) {
  await api(`/tasks/${taskId}/resubmit`, { method: "POST" });
  await loadTasks();
}

async function loadTaskLog(taskId) {
  state.data.taskLog = await api(`/tasks/${taskId}/log?tail=200KB`);
}

async function submitEnv(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/envs/upload-pack", {
    method: "POST",
    body: JSON.stringify({
      name: fieldValue(form, "name"),
      path: fieldValue(form, "path"),
      description: fieldValue(form, "description"),
      python_version: fieldValue(form, "python_version") || null,
    }),
  });
  form.reset();
  await loadEnvs();
}

async function previewFile(path) {
  const payload = await api(`/files/preview?path=${encodeURIComponent(path)}`);
  state.data.preview = payload.data;
}

async function submitUser(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/users", {
    method: "POST",
    body: JSON.stringify({
      username: fieldValue(form, "username"),
      real_name: fieldValue(form, "real_name"),
      role: fieldValue(form, "role"),
      password: fieldValue(form, "password"),
      state: fieldValue(form, "state"),
    }),
  });
  form.reset();
  await loadUsers();
}

async function updateSetting(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/admin/settings", {
    method: "PATCH",
    body: JSON.stringify({ values: { [fieldValue(form, "key")]: fieldValue(form, "value") } }),
  });
  form.reset();
  await loadAdmin();
}

function renderShell(content) {
  const nav = navItems
    .filter((item) => can(item.permission))
    .map((item) => `<button class="${state.view === item.id ? "active" : ""}" data-view="${item.id}">${item.label}</button>`)
    .join("");
  return `
    <div class="app-shell">
      <aside class="sidebar">
        <div class="brand">
          <div class="brand-mark">NG</div>
          <div><strong>NebulaGrid</strong><span>GPU 调度控制台</span></div>
        </div>
        <nav>${nav}</nav>
        <div class="session">
          <span>${state.user?.real_name || ""}</span>
          <small>${state.user?.role || ""}</small>
          <button data-action="logout">退出</button>
        </div>
      </aside>
      <main>
        <header class="topbar">
          <div>
            <h1>${navItems.find((item) => item.id === state.view)?.label || "控制台"}</h1>
            <p>${state.apiBase}</p>
          </div>
          <button data-action="refresh">刷新</button>
        </header>
        ${state.message ? `<div class="notice">${state.message}</div>` : ""}
        ${content}
      </main>
    </div>
  `;
}

function renderLogin() {
  return `
    <main class="login-screen">
      <section class="login-panel">
        <div>
          <p class="eyebrow">NebulaGrid Console</p>
          <h1>登录测试控制台</h1>
        </div>
        ${state.message ? `<div class="notice">${state.message}</div>` : ""}
        <form id="loginForm" class="stack">
          <label>API 地址<input name="apiBase" value="${state.apiBase}" /></label>
          <label>账号<input name="identity" value="admin" autocomplete="username" /></label>
          <label>密码<input name="password" type="password" value="admin123" autocomplete="current-password" /></label>
          <button type="submit">登录</button>
        </form>
        <button class="ghost" data-action="health">检查健康状态</button>
        ${state.data.health ? `<pre>${escapeHtml(JSON.stringify(state.data.health, null, 2))}</pre>` : ""}
      </section>
    </main>
  `;
}

function renderDashboard() {
  const data = state.data.dashboard || {};
  const cards = [
    ["节点", `${data.nodes_online ?? 0}/${data.nodes_total ?? 0}`],
    ["GPU", `${data.gpus_available ?? 0}/${data.gpus_total ?? 0}`],
    ["等待任务", data.tasks_waiting ?? 0],
    ["运行任务", data.tasks_running ?? 0],
    ["今日结束", data.tasks_finished_today ?? 0],
  ];
  return renderShell(`
    <section class="metric-grid">${cards.map(([label, value]) => `<article><span>${label}</span><strong>${value}</strong></article>`).join("")}</section>
    <section class="panel"><h2>当前用户</h2><pre>${escapeHtml(JSON.stringify(state.user, null, 2))}</pre></section>
  `);
}

function renderNodes() {
  return renderShell(`
    <section class="panel">
      <h2>登记节点</h2>
      <form id="nodeForm" class="grid-form">
        <label>名称<input name="name" placeholder="node-a" required /></label>
        <label>IP<input name="ip" placeholder="192.168.1.21" required /></label>
        <label>SSH 用户<input name="ssh_user" value="ddltm" /></label>
        <label>GPU 型号<input name="gpu_models" placeholder="A100,A100" /></label>
        <button type="submit">新增节点</button>
      </form>
    </section>
    <section class="panel"><h2>节点列表</h2>${table(["名称", "IP", "状态", "调度", "GPU"], state.data.nodes.map((node) => [
      node.name, node.ip, badge(node.state), node.scheduling_enabled ? "启用" : "关闭", node.gpus.map((gpu) => gpu.model).join(", ") || "-"
    ]))}</section>
  `);
}

function renderTasks() {
  return renderShell(`
    <section class="panel">
      <h2>提交任务</h2>
      <form id="taskForm" class="grid-form wide">
        <label>描述<input name="description" placeholder="baseline train" /></label>
        <label>环境 ID<input name="env_id" type="number" /></label>
        <label>工作目录<input name="workdir" value="/workspace" /></label>
        <label>优先级<input name="priority" type="number" value="0" /></label>
        <label>GPU 数<input name="need_gpus" type="number" value="1" /></label>
        <label>GPU 类型<input name="gpu_types" placeholder="A100,RTX4090" /></label>
        <label class="check"><input name="on_hold" type="checkbox" /> 挂起提交</label>
        <label class="check"><input name="allow_gpu_reuse" type="checkbox" /> 允许复用</label>
        <label class="span-2">命令<textarea name="command" required placeholder="python train.py"></textarea></label>
        <button type="submit">提交任务</button>
      </form>
    </section>
    <section class="panel">
      <h2>任务列表</h2>
      ${table(["ID", "状态", "描述", "工作目录", "命令", "操作"], state.data.tasks.items.map((task) => [
        task.task_id, badge(task.state), task.description || "-", task.workdir, task.command,
        `<button data-task-log="${task.task_id}">日志</button><button data-task-cancel="${task.task_id}">取消</button><button data-task-resubmit="${task.task_id}">重提</button>`
      ]))}
      ${state.data.taskLog ? `<pre class="log">${escapeHtml(state.data.taskLog)}</pre>` : ""}
    </section>
  `);
}

function renderEnvs() {
  return renderShell(`
    <section class="panel">
      <h2>登记环境</h2>
      <form id="envForm" class="grid-form">
        <label>名称<input name="name" placeholder="torch-cu121" required /></label>
        <label>路径<input name="path" placeholder="/data/envs/user_envs/1/torch" required /></label>
        <label>Python<input name="python_version" placeholder="3.11" /></label>
        <label>说明<input name="description" /></label>
        <button type="submit">登记环境</button>
      </form>
    </section>
    <section class="panel"><h2>环境列表</h2>${table(["ID", "名称", "路径", "状态", "Python"], state.data.envs.map((env) => [
      env.id, env.name, env.path, badge(env.state), env.python_version || "-"
    ]))}</section>
  `);
}

function renderFiles() {
  const rows = state.data.files.items.map((item) => [
    item.type === "directory" ? "目录" : "文件",
    item.name,
    item.size_bytes,
    item.type === "directory" ? `<button data-file-open="${item.path}">打开</button>` : `<button data-file-preview="${item.path}">预览</button>`,
  ]);
  return renderShell(`
    <section class="panel">
      <h2>文件浏览</h2>
      <form id="filePathForm" class="inline-form">
        <input name="path" value="${state.data.files.path || "/workspace"}" />
        <button type="submit">打开路径</button>
      </form>
      ${table(["类型", "名称", "大小", "操作"], rows)}
      ${state.data.preview ? `<pre>${escapeHtml(state.data.preview.content)}</pre>` : ""}
    </section>
  `);
}

function renderUsers() {
  return renderShell(`
    <section class="panel">
      <h2>创建用户</h2>
      <form id="userForm" class="grid-form">
        <label>用户名<input name="username" required /></label>
        <label>姓名<input name="real_name" required /></label>
        <label>角色<select name="role"><option value="student">student</option><option value="mentor">mentor</option><option value="admin">admin</option><option value="viewer">viewer</option></select></label>
        <label>状态<select name="state"><option value="enabled">enabled</option><option value="disabled">disabled</option></select></label>
        <label>密码<input name="password" type="password" required minlength="8" /></label>
        <button type="submit">创建用户</button>
      </form>
    </section>
    <section class="panel"><h2>用户列表</h2>${table(["ID", "用户名", "姓名", "角色", "状态", "Home"], state.data.users.map((user) => [
      user.id, user.username, user.real_name, user.role, badge(user.state), user.home_path
    ]))}</section>
  `);
}

function renderAdmin() {
  return renderShell(`
    <section class="panel">
      <h2>系统设置</h2>
      <form id="settingForm" class="inline-form">
        <input name="key" placeholder="scheduler.enabled" required />
        <input name="value" placeholder="true" required />
        <button type="submit">保存</button>
      </form>
      ${table(["Key", "Value", "Updated By"], state.data.settings.map((item) => [item.key, item.value, item.updated_by || "-"]))}
    </section>
    <section class="panel"><h2>审计日志</h2>${table(["时间", "操作者", "动作", "目标", "结果"], state.data.auditLogs.items.map((item) => [
      item.created_at, item.actor_user_id, item.action, `${item.target_type}:${item.target_id}`, badge(item.result)
    ]))}</section>
  `);
}

function render() {
  const views = {
    dashboard: renderDashboard,
    nodes: renderNodes,
    tasks: renderTasks,
    envs: renderEnvs,
    files: renderFiles,
    users: renderUsers,
    admin: renderAdmin,
  };
  document.querySelector("#app").innerHTML = state.user ? views[state.view]() : renderLogin();
  bindEvents();
}

function bindEvents() {
  document.querySelector("#loginForm")?.addEventListener("submit", (event) => run(() => login(event), "登录成功"));
  document.querySelector("#nodeForm")?.addEventListener("submit", (event) => run(() => submitNode(event), "节点已登记"));
  document.querySelector("#taskForm")?.addEventListener("submit", (event) => run(() => submitTask(event), "任务已提交"));
  document.querySelector("#envForm")?.addEventListener("submit", (event) => run(() => submitEnv(event), "环境已登记"));
  document.querySelector("#userForm")?.addEventListener("submit", (event) => run(() => submitUser(event), "用户已创建"));
  document.querySelector("#settingForm")?.addEventListener("submit", (event) => run(() => updateSetting(event), "设置已保存"));
  document.querySelector("#filePathForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    run(() => loadFiles(fieldValue(event.currentTarget, "path")));
  });
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  document.querySelector("[data-action='logout']")?.addEventListener("click", () => run(logout));
  document.querySelector("[data-action='refresh']")?.addEventListener("click", () => run(refreshCurrentView, "已刷新"));
  document.querySelector("[data-action='health']")?.addEventListener("click", () => run(loadHealth, "健康检查完成"));
  document.querySelectorAll("[data-task-cancel]").forEach((button) => button.addEventListener("click", () => run(() => cancelTask(button.dataset.taskCancel), "任务已取消")));
  document.querySelectorAll("[data-task-resubmit]").forEach((button) => button.addEventListener("click", () => run(() => resubmitTask(button.dataset.taskResubmit), "任务已重提")));
  document.querySelectorAll("[data-task-log]").forEach((button) => button.addEventListener("click", () => run(() => loadTaskLog(button.dataset.taskLog))));
  document.querySelectorAll("[data-file-open]").forEach((button) => button.addEventListener("click", () => run(() => loadFiles(button.dataset.fileOpen))));
  document.querySelectorAll("[data-file-preview]").forEach((button) => button.addEventListener("click", () => run(() => previewFile(button.dataset.filePreview))));
}

function table(headers, rows) {
  if (!rows.length) return `<div class="empty">暂无数据</div>`;
  return `
    <div class="table-wrap">
      <table>
        <thead><tr>${headers.map((header) => `<th>${header}</th>`).join("")}</tr></thead>
        <tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${cellHtml(cell)}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
    </div>
  `;
}

function cellHtml(value) {
  const text = String(value);
  if (text.includes("data-task-") || text.includes("data-file-") || text.includes("class=\"badge\"")) return text;
  return escapeHtml(text);
}

function badge(value) {
  return `<span class="badge">${escapeHtml(String(value))}</span>`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

loadMe().then(refreshCurrentView).catch(() => render());
