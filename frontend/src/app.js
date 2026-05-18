const state = {
  apiBase: localStorage.getItem("ng_api_base") || `${location.origin}/api`,
  token: localStorage.getItem("ng_token") || "",
  user: null,
  page: location.hash.replace("#/", "") || "dashboard",
  toast: null,
  loading: false,
  demo: localStorage.getItem("ng_demo_mode") === "1",
  drawer: null,
  data: {
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

const pages = [
  { id: "dashboard", label: "总览", icon: "⌂", permission: "dashboard:read" },
  { id: "nodes", label: "节点", icon: "▦", permission: "nodes:read" },
  { id: "tasks", label: "任务", icon: "▶", permission: "tasks:read" },
  { id: "envs", label: "环境", icon: "◇", permission: "envs:read" },
  { id: "files", label: "文件", icon: "▤", permission: "files:read" },
  { id: "users", label: "用户", icon: "◉", permission: "users:read" },
  { id: "admin", label: "系统", icon: "⚙", permission: "admin:settings:read" },
];

const demoStore = {
  token: "demo-token",
  user: {
    id: 1,
    username: "admin",
    real_name: "演示管理员",
    role: "admin",
    state: "enabled",
    permissions: ["*"],
  },
  nodes: [
    {
      id: 1,
      name: "node-a",
      ip: "192.168.1.21",
      ssh_user: "ddltm",
      state: "online",
      scheduling_enabled: true,
      gpus: [
        { id: 1, gpu_index: 0, model: "A100", total_vram_mb: 40960 },
        { id: 2, gpu_index: 1, model: "A100", total_vram_mb: 40960 },
      ],
    },
  ],
  tasks: [],
  envs: [
    {
      id: 1,
      owner_user_id: 1,
      name: "torch-cu121",
      path: "/data/envs/user_envs/1/torch-cu121",
      description: "PyTorch CUDA 12.1",
      source_type: "registered",
      state: "available",
      python_version: "3.11",
      size_bytes: 0,
      created_at: new Date().toISOString(),
    },
  ],
  files: [
    { name: "project", path: "/workspace/project", type: "directory", size_bytes: 0, modified_at: new Date().toISOString() },
    { name: "train.py", path: "/workspace/train.py", type: "file", size_bytes: 2048, modified_at: new Date().toISOString() },
  ],
  users: [],
  settings: [
    { key: "scheduler.enabled", value: "true", updated_by: null, updated_at: null },
    { key: "uploads.max_size_mb", value: "1024", updated_by: null, updated_at: null },
  ],
  auditLogs: [],
};

function can(permission) {
  if (!state.user) return false;
  return state.user.permissions.includes("*") || state.user.permissions.includes(permission);
}

function nowText() {
  return new Date().toISOString();
}

function showToast(text, type = "info") {
  state.toast = { text, type };
  render();
  window.setTimeout(() => {
    if (state.toast?.text === text) {
      state.toast = null;
      render();
    }
  }, 2800);
}

async function run(action, successText) {
  state.loading = true;
  render();
  try {
    await action();
    if (successText) showToast(successText, "success");
  } catch (error) {
    showToast(error.message || "操作失败", "error");
  } finally {
    state.loading = false;
    render();
  }
}

async function api(path, options = {}) {
  if (state.demo) return demoApi(path, options);
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(`${state.apiBase.replace(/\/$/, "")}${path}`, { ...options, headers });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof payload === "string" ? payload : payload.message;
    throw new Error(message || `HTTP ${response.status}`);
  }
  return payload;
}

async function demoApi(path, options = {}) {
  const method = options.method || "GET";
  const body = options.body ? JSON.parse(options.body) : null;
  await new Promise((resolve) => window.setTimeout(resolve, 120));
  if (path === "/health") return ok({ service: "NebulaGrid", status: "ok", environment: "demo", version: "demo" });
  if (path === "/auth/login") return ok({ access_token: demoStore.token, token_type: "bearer", user: demoStore.user });
  if (path === "/auth/me") return ok(demoStore.user);
  if (path === "/dashboard/summary") return ok(buildDemoSummary());
  if (path === "/nodes") return ok(demoStore.nodes);
  if (path === "/admin/nodes" && method === "POST") return ok(addDemoNode(body));
  if (path === "/tasks" && method === "GET") return ok({ items: demoStore.tasks, total: demoStore.tasks.length, page: 1, page_size: 20 });
  if (path === "/tasks" && method === "POST") return ok(addDemoTask(body));
  if (path.includes("/cancel")) return ok(updateDemoTask(path.split("/")[2], "cancelled"));
  if (path.includes("/resubmit")) return ok(resubmitDemoTask(path.split("/")[2]));
  if (path.includes("/log")) return `[${path.split("/")[2]}] demo log\ntraining loss=0.024\ncompleted`;
  if (path === "/envs") return ok(demoStore.envs);
  if (path === "/envs/upload-pack" && method === "POST") return ok(addDemoEnv(body));
  if (path.startsWith("/files/list")) return ok({ path: "/workspace", items: demoStore.files });
  if (path.startsWith("/files/preview")) return ok({ path: "/workspace/train.py", content_type: "text/plain", content: "print('hello NebulaGrid')\n", truncated: false });
  if (path === "/users") return ok([demoStore.user, ...demoStore.users]);
  if (path === "/users" && method === "POST") return ok(addDemoUser(body));
  if (path === "/admin/settings" && method === "GET") return ok(demoStore.settings);
  if (path === "/admin/settings" && method === "PATCH") return ok(updateDemoSettings(body.values));
  if (path === "/admin/audit-logs") return ok({ items: demoStore.auditLogs, total: demoStore.auditLogs.length, page: 1, page_size: 20 });
  return ok({});
}

function ok(data) {
  return { ok: true, code: "OK", message: "success", data, request_id: `demo-${Date.now()}` };
}

function buildDemoSummary() {
  const totalGpus = demoStore.nodes.reduce((sum, node) => sum + node.gpus.length, 0);
  return {
    nodes_total: demoStore.nodes.length,
    nodes_online: demoStore.nodes.filter((node) => node.state === "online").length,
    gpus_total: totalGpus,
    gpus_available: totalGpus,
    tasks_waiting: demoStore.tasks.filter((task) => ["wait", "on_hold"].includes(task.state)).length,
    tasks_running: demoStore.tasks.filter((task) => task.state === "running").length,
    tasks_finished_today: demoStore.tasks.filter((task) => ["succeeded", "failed", "cancelled"].includes(task.state)).length,
    viewer_role: "admin",
  };
}

function addDemoNode(payload) {
  const node = {
    id: demoStore.nodes.length + 1,
    name: payload.name,
    ip: payload.ip,
    ssh_user: payload.ssh_user || "ddltm",
    state: "offline",
    scheduling_enabled: false,
    gpus: (payload.gpu_models || []).map((model, index) => ({ id: Date.now() + index, gpu_index: index, model, total_vram_mb: 0 })),
  };
  demoStore.nodes.push(node);
  pushAudit("node.create", "node", node.name);
  return node;
}

function addDemoTask(payload) {
  const id = demoStore.tasks.length + 1;
  const task = {
    id,
    task_id: `NG-${String(id).padStart(6, "0")}`,
    user_id: 1,
    description: payload.description || "",
    env_id: payload.env_id,
    workdir: payload.workdir,
    command: payload.command,
    state: payload.on_hold ? "on_hold" : "wait",
    priority: payload.priority || 0,
    on_hold: Boolean(payload.on_hold),
    created_at: nowText(),
    requirement: payload.requirement,
  };
  demoStore.tasks.unshift(task);
  pushAudit("task.create", "task", task.task_id);
  return task;
}

function updateDemoTask(taskId, stateName) {
  const task = demoStore.tasks.find((item) => item.task_id === taskId);
  if (task) task.state = stateName;
  pushAudit("task.cancel", "task", taskId);
  return task;
}

function resubmitDemoTask(taskId) {
  const task = demoStore.tasks.find((item) => item.task_id === taskId);
  if (!task) return {};
  return addDemoTask({ ...task, on_hold: false });
}

function addDemoEnv(payload) {
  const env = {
    id: demoStore.envs.length + 1,
    owner_user_id: 1,
    name: payload.name,
    path: payload.path,
    description: payload.description || "",
    source_type: "registered",
    state: "registered",
    python_version: payload.python_version,
    size_bytes: 0,
    created_at: nowText(),
  };
  demoStore.envs.push(env);
  pushAudit("env.create", "env", String(env.id));
  return env;
}

function addDemoUser(payload) {
  const user = {
    id: demoStore.users.length + 2,
    username: payload.username,
    real_name: payload.real_name,
    role: payload.role,
    state: payload.state,
    home_path: `/data/user/${demoStore.users.length + 2}`,
    created_at: nowText(),
  };
  demoStore.users.push(user);
  pushAudit("user.create", "user", user.username);
  return user;
}

function updateDemoSettings(values) {
  Object.entries(values).forEach(([key, value]) => {
    const item = demoStore.settings.find((setting) => setting.key === key);
    if (item) {
      item.value = value;
      item.updated_by = 1;
      item.updated_at = nowText();
    } else {
      demoStore.settings.push({ key, value, updated_by: 1, updated_at: nowText() });
    }
  });
  pushAudit("settings.update", "settings", Object.keys(values).join(","));
  return demoStore.settings;
}

function pushAudit(action, targetType, targetId) {
  demoStore.auditLogs.unshift({
    id: demoStore.auditLogs.length + 1,
    actor_user_id: 1,
    action,
    target_type: targetType,
    target_id: targetId,
    ip: "demo",
    result: "success",
    created_at: nowText(),
    detail_json: {},
  });
}

function formValue(form, name) {
  return String(new FormData(form).get(name) || "").trim();
}

function parseList(value) {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

async function login(event) {
  event.preventDefault();
  const form = event.currentTarget;
  state.apiBase = formValue(form, "apiBase").replace(/\/$/, "");
  localStorage.setItem("ng_api_base", state.apiBase);
  const payload = await api("/auth/login", {
    method: "POST",
    body: JSON.stringify({ identity: formValue(form, "identity"), password: formValue(form, "password") }),
  });
  state.token = payload.data.access_token;
  state.user = payload.data.user;
  localStorage.setItem("ng_token", state.token);
  await refreshPage();
}

async function enterDemo() {
  state.demo = true;
  localStorage.setItem("ng_demo_mode", "1");
  state.token = demoStore.token;
  state.user = demoStore.user;
  await refreshPage();
}

async function logout() {
  state.token = "";
  state.user = null;
  state.demo = false;
  localStorage.removeItem("ng_token");
  localStorage.removeItem("ng_demo_mode");
  render();
}

async function loadMe() {
  if (!state.token && !state.demo) return;
  const payload = await api("/auth/me");
  state.user = payload.data;
}

async function refreshPage() {
  await loadMe();
  const loaders = {
    dashboard: async () => {
      state.data.dashboard = (await api("/dashboard/summary")).data;
    },
    nodes: async () => {
      state.data.nodes = (await api("/nodes")).data;
    },
    tasks: async () => {
      state.data.tasks = (await api("/tasks")).data;
    },
    envs: async () => {
      state.data.envs = (await api("/envs")).data;
    },
    files: async () => {
      state.data.files = (await api(`/files/list?path=${encodeURIComponent(state.data.files.path || "/workspace")}`)).data;
    },
    users: async () => {
      state.data.users = (await api("/users")).data;
    },
    admin: async () => {
      state.data.settings = (await api("/admin/settings")).data;
      state.data.auditLogs = (await api("/admin/audit-logs")).data;
    },
  };
  await loaders[state.page]?.();
}

function navigate(page) {
  state.page = page;
  location.hash = `/${page}`;
  state.drawer = null;
  run(refreshPage);
}

async function submitNode(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/admin/nodes", {
    method: "POST",
    body: JSON.stringify({
      name: formValue(form, "name"),
      ip: formValue(form, "ip"),
      ssh_user: formValue(form, "ssh_user") || "ddltm",
      gpu_models: parseList(formValue(form, "gpu_models")),
    }),
  });
  form.reset();
  await refreshPage();
}

async function submitTask(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/tasks", {
    method: "POST",
    body: JSON.stringify({
      description: formValue(form, "description"),
      env_id: formValue(form, "env_id") ? Number(formValue(form, "env_id")) : null,
      workdir: formValue(form, "workdir") || "/workspace",
      command: formValue(form, "command"),
      priority: Number(formValue(form, "priority") || 0),
      on_hold: form.elements.on_hold.checked,
      requirement: {
        need_gpus: Number(formValue(form, "need_gpus") || 0),
        gpu_types: parseList(formValue(form, "gpu_types")),
        allow_gpu_reuse: form.elements.allow_gpu_reuse.checked,
      },
    }),
  });
  form.reset();
  await refreshPage();
}

async function cancelTask(taskId) {
  await api(`/tasks/${taskId}/cancel`, { method: "POST" });
  await refreshPage();
}

async function resubmitTask(taskId) {
  await api(`/tasks/${taskId}/resubmit`, { method: "POST" });
  await refreshPage();
}

async function showTaskLog(taskId) {
  state.data.taskLog = await api(`/tasks/${taskId}/log?tail=200KB`);
  state.drawer = { title: `任务日志 ${taskId}`, body: `<pre class="drawer-log">${escapeHtml(state.data.taskLog)}</pre>` };
}

async function submitEnv(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/envs/upload-pack", {
    method: "POST",
    body: JSON.stringify({
      name: formValue(form, "name"),
      path: formValue(form, "path"),
      description: formValue(form, "description"),
      python_version: formValue(form, "python_version") || null,
    }),
  });
  form.reset();
  await refreshPage();
}

async function openPath(path) {
  state.data.files.path = path;
  state.data.preview = null;
  await refreshPage();
}

async function previewFile(path) {
  const payload = await api(`/files/preview?path=${encodeURIComponent(path)}`);
  state.data.preview = payload.data;
  state.drawer = { title: `文件预览 ${path}`, body: `<pre class="drawer-log">${escapeHtml(payload.data.content)}</pre>` };
}

async function submitUser(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/users", {
    method: "POST",
    body: JSON.stringify({
      username: formValue(form, "username"),
      real_name: formValue(form, "real_name"),
      role: formValue(form, "role"),
      state: formValue(form, "state"),
      password: formValue(form, "password"),
    }),
  });
  form.reset();
  await refreshPage();
}

async function updateSetting(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/admin/settings", {
    method: "PATCH",
    body: JSON.stringify({ values: { [formValue(form, "key")]: formValue(form, "value") } }),
  });
  form.reset();
  await refreshPage();
}

function shell(content) {
  const nav = pages
    .filter((page) => can(page.permission))
    .map((page) => `
      <button class="nav-item ${state.page === page.id ? "active" : ""}" data-nav="${page.id}">
        <span>${page.icon}</span>${page.label}
      </button>
    `)
    .join("");
  return `
    <div class="layout">
      <aside class="sidebar">
        <div class="brand">
          <div class="brand-logo">NG</div>
          <div>
            <strong>NebulaGrid</strong>
            <small>实验室 GPU 调度平台</small>
          </div>
        </div>
        <nav>${nav}</nav>
        <section class="user-card">
          <strong>${escapeHtml(state.user?.real_name || "未登录")}</strong>
          <span>${escapeHtml(state.user?.username || "")} · ${escapeHtml(state.user?.role || "")}</span>
          <button class="secondary" data-action="logout">退出登录</button>
        </section>
      </aside>
      <main class="workspace">
        <header class="topbar">
          <div>
            <p>${state.demo ? "演示模式" : "在线模式"} · ${escapeHtml(state.apiBase)}</p>
            <h1>${pages.find((page) => page.id === state.page)?.label || "总览"}</h1>
          </div>
          <div class="top-actions">
            <button class="secondary" data-action="refresh">刷新</button>
          </div>
        </header>
        ${content}
      </main>
      ${state.drawer ? renderDrawer() : ""}
      ${state.toast ? `<div class="toast ${state.toast.type}">${escapeHtml(state.toast.text)}</div>` : ""}
      ${state.loading ? `<div class="loading">正在处理...</div>` : ""}
    </div>
  `;
}

function renderLogin() {
  return `
    <main class="login-page">
      <section class="login-copy">
        <p>NebulaGrid 3.0</p>
        <h1>分布式 GPU 任务调度与实验资源管理</h1>
        <div class="login-stats">
          <span>节点监控</span>
          <span>任务队列</span>
          <span>环境管理</span>
          <span>审计追踪</span>
        </div>
      </section>
      <section class="login-card">
        <h2>登录控制台</h2>
        <form id="loginForm" class="form-stack">
          <label>API 地址<input name="apiBase" value="${escapeAttr(state.apiBase)}" /></label>
          <label>账号<input name="identity" value="admin" autocomplete="username" /></label>
          <label>密码<input name="password" type="password" value="admin123" autocomplete="current-password" /></label>
          <button type="submit">登录</button>
        </form>
        <button class="secondary full" data-action="demo">进入演示模式</button>
      </section>
      ${state.toast ? `<div class="toast ${state.toast.type}">${escapeHtml(state.toast.text)}</div>` : ""}
    </main>
  `;
}

function renderDashboard() {
  const d = state.data.dashboard || {};
  const cards = [
    ["在线节点", `${d.nodes_online ?? 0}/${d.nodes_total ?? 0}`],
    ["可用 GPU", `${d.gpus_available ?? 0}/${d.gpus_total ?? 0}`],
    ["等待任务", d.tasks_waiting ?? 0],
    ["运行任务", d.tasks_running ?? 0],
    ["今日结束", d.tasks_finished_today ?? 0],
  ];
  return shell(`
    <section class="metrics">${cards.map(([label, value]) => `<article><span>${label}</span><strong>${value}</strong></article>`).join("")}</section>
    <section class="split">
      <article class="panel">
        <h2>快速开始</h2>
        <div class="quick-grid">
          <button data-nav="nodes">登记计算节点</button>
          <button data-nav="envs">登记 Python 环境</button>
          <button data-nav="tasks">提交训练任务</button>
          <button data-nav="files">浏览工作目录</button>
        </div>
      </article>
      <article class="panel">
        <h2>当前登录</h2>
        <dl class="kv">
          <dt>用户</dt><dd>${escapeHtml(state.user?.real_name || "-")}</dd>
          <dt>角色</dt><dd>${badge(state.user?.role || "-")}</dd>
          <dt>状态</dt><dd>${badge(state.user?.state || "-")}</dd>
        </dl>
      </article>
    </section>
  `);
}

function renderNodes() {
  const rows = state.data.nodes.map((node) => [
    node.name,
    node.ip,
    node.ssh_user,
    badge(node.state),
    node.scheduling_enabled ? badge("调度开启") : badge("调度关闭"),
    node.gpus?.map((gpu) => `${gpu.gpu_index}:${gpu.model}`).join(", ") || "-",
  ]);
  return shell(`
    <section class="panel">
      <div class="panel-head"><h2>新增计算节点</h2><span>登记 SSH 地址和 GPU 型号，后续监控器会采集真实状态。</span></div>
      <form id="nodeForm" class="form-grid">
        <label>节点名<input name="name" placeholder="node-a" required /></label>
        <label>IP 地址<input name="ip" placeholder="192.168.1.21" required /></label>
        <label>SSH 用户<input name="ssh_user" value="ddltm" /></label>
        <label>GPU 型号<input name="gpu_models" placeholder="A100,A100" /></label>
        <button type="submit">登记节点</button>
      </form>
    </section>
    <section class="panel">${renderTable(["节点", "IP", "SSH", "状态", "调度", "GPU"], rows)}</section>
  `);
}

function renderTasks() {
  const rows = state.data.tasks.items.map((task) => [
    task.task_id,
    badge(task.state),
    task.description || "-",
    task.workdir,
    task.command,
    `<button class="small" data-log="${task.task_id}">日志</button><button class="small secondary" data-resubmit="${task.task_id}">重提</button><button class="small danger" data-cancel="${task.task_id}">取消</button>`,
  ]);
  return shell(`
    <section class="panel">
      <div class="panel-head"><h2>提交任务</h2><span>填写命令、工作目录和 GPU 需求后进入等待队列。</span></div>
      <form id="taskForm" class="form-grid task-form">
        <label>任务描述<input name="description" placeholder="baseline train" /></label>
        <label>环境 ID<input name="env_id" type="number" /></label>
        <label>工作目录<input name="workdir" value="/workspace" /></label>
        <label>优先级<input name="priority" type="number" value="0" /></label>
        <label>GPU 数<input name="need_gpus" type="number" value="1" /></label>
        <label>GPU 类型<input name="gpu_types" placeholder="A100,RTX4090" /></label>
        <label class="check"><input name="on_hold" type="checkbox" /> 挂起提交</label>
        <label class="check"><input name="allow_gpu_reuse" type="checkbox" /> 允许复用</label>
        <label class="wide">执行命令<textarea name="command" placeholder="python train.py --epochs 10" required></textarea></label>
        <button type="submit">提交到队列</button>
      </form>
    </section>
    <section class="panel">${renderTable(["任务 ID", "状态", "描述", "工作目录", "命令", "操作"], rows)}</section>
  `);
}

function renderEnvs() {
  const rows = state.data.envs.map((env) => [env.id, env.name, env.path, badge(env.state), env.python_version || "-", env.description || "-"]);
  return shell(`
    <section class="panel">
      <div class="panel-head"><h2>登记环境</h2><span>登记已有环境或 conda-pack 导入结果，供任务提交时选择。</span></div>
      <form id="envForm" class="form-grid">
        <label>环境名<input name="name" placeholder="torch-cu121" required /></label>
        <label>路径<input name="path" placeholder="/data/envs/user_envs/1/torch-cu121" required /></label>
        <label>Python 版本<input name="python_version" placeholder="3.11" /></label>
        <label>说明<input name="description" placeholder="PyTorch CUDA 12.1" /></label>
        <button type="submit">登记环境</button>
      </form>
    </section>
    <section class="panel">${renderTable(["ID", "环境", "路径", "状态", "Python", "说明"], rows)}</section>
  `);
}

function renderFiles() {
  const rows = state.data.files.items.map((item) => [
    item.type === "directory" ? "目录" : "文件",
    item.name,
    item.path,
    item.size_bytes,
    item.type === "directory" ? `<button class="small" data-open-path="${item.path}">打开</button>` : `<button class="small" data-preview="${item.path}">预览</button>`,
  ]);
  return shell(`
    <section class="panel">
      <div class="panel-head"><h2>文件浏览</h2><span>所有路径都会通过后端 PathResolver 校验。</span></div>
      <form id="fileForm" class="path-bar">
        <input name="path" value="${escapeAttr(state.data.files.path || "/workspace")}" />
        <button type="submit">打开</button>
      </form>
      ${renderTable(["类型", "名称", "路径", "大小", "操作"], rows)}
    </section>
  `);
}

function renderUsers() {
  const rows = state.data.users.map((user) => [user.id, user.username, user.real_name, badge(user.role), badge(user.state), user.home_path || "-"]);
  return shell(`
    <section class="panel">
      <div class="panel-head"><h2>创建用户</h2><span>管理员可创建任意角色；导师只能创建学生。</span></div>
      <form id="userForm" class="form-grid">
        <label>用户名<input name="username" required /></label>
        <label>姓名<input name="real_name" required /></label>
        <label>角色<select name="role"><option value="student">学生</option><option value="mentor">导师</option><option value="admin">管理员</option><option value="viewer">展示者</option></select></label>
        <label>状态<select name="state"><option value="enabled">启用</option><option value="disabled">禁用</option></select></label>
        <label>初始密码<input name="password" type="password" minlength="8" required /></label>
        <button type="submit">创建用户</button>
      </form>
    </section>
    <section class="panel">${renderTable(["ID", "用户名", "姓名", "角色", "状态", "Home"], rows)}</section>
  `);
}

function renderAdmin() {
  const settingRows = state.data.settings.map((item) => [item.key, item.value, item.updated_by || "-", item.updated_at || "-"]);
  const auditRows = state.data.auditLogs.items.map((item) => [item.created_at, item.actor_user_id, item.action, `${item.target_type}:${item.target_id}`, badge(item.result)]);
  return shell(`
    <section class="panel">
      <div class="panel-head"><h2>系统设置</h2><span>修改调度、上传等运行参数。</span></div>
      <form id="settingForm" class="path-bar">
        <input name="key" placeholder="scheduler.enabled" required />
        <input name="value" placeholder="true" required />
        <button type="submit">保存</button>
      </form>
      ${renderTable(["Key", "Value", "修改人", "修改时间"], settingRows)}
    </section>
    <section class="panel">
      <div class="panel-head"><h2>审计日志</h2><span>关键操作会记录在这里。</span></div>
      ${renderTable(["时间", "操作者", "动作", "目标", "结果"], auditRows)}
    </section>
  `);
}

function renderTable(headers, rows) {
  if (!rows.length) return `<div class="empty">暂无数据</div>`;
  return `
    <div class="table-wrap">
      <table>
        <thead><tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr></thead>
        <tbody>
          ${rows.map((row) => `<tr>${row.map((cell) => `<td>${cellHtml(cell)}</td>`).join("")}</tr>`).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderDrawer() {
  return `
    <aside class="drawer">
      <div class="drawer-head">
        <h2>${escapeHtml(state.drawer.title)}</h2>
        <button class="secondary" data-action="close-drawer">关闭</button>
      </div>
      ${state.drawer.body}
    </aside>
  `;
}

function badge(value) {
  return `<span class="badge">${escapeHtml(String(value))}</span>`;
}

function cellHtml(value) {
  const text = String(value);
  if (text.includes("data-") || text.includes("class=\"badge\"")) return text;
  return escapeHtml(text);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

function render() {
  const renderers = {
    dashboard: renderDashboard,
    nodes: renderNodes,
    tasks: renderTasks,
    envs: renderEnvs,
    files: renderFiles,
    users: renderUsers,
    admin: renderAdmin,
  };
  document.querySelector("#app").innerHTML = state.user ? renderers[state.page]() : renderLogin();
  bindEvents();
}

function bindEvents() {
  document.querySelector("#loginForm")?.addEventListener("submit", (event) => run(() => login(event), "登录成功"));
  document.querySelector("#nodeForm")?.addEventListener("submit", (event) => run(() => submitNode(event), "节点已登记"));
  document.querySelector("#taskForm")?.addEventListener("submit", (event) => run(() => submitTask(event), "任务已提交"));
  document.querySelector("#envForm")?.addEventListener("submit", (event) => run(() => submitEnv(event), "环境已登记"));
  document.querySelector("#userForm")?.addEventListener("submit", (event) => run(() => submitUser(event), "用户已创建"));
  document.querySelector("#settingForm")?.addEventListener("submit", (event) => run(() => updateSetting(event), "设置已保存"));
  document.querySelector("#fileForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    run(() => openPath(formValue(event.currentTarget, "path")));
  });
  document.querySelectorAll("[data-nav]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.nav)));
  document.querySelector("[data-action='logout']")?.addEventListener("click", () => run(logout));
  document.querySelector("[data-action='refresh']")?.addEventListener("click", () => run(refreshPage, "已刷新"));
  document.querySelector("[data-action='demo']")?.addEventListener("click", () => run(enterDemo, "已进入演示模式"));
  document.querySelector("[data-action='close-drawer']")?.addEventListener("click", () => {
    state.drawer = null;
    render();
  });
  document.querySelectorAll("[data-cancel]").forEach((button) => button.addEventListener("click", () => run(() => cancelTask(button.dataset.cancel), "任务已取消")));
  document.querySelectorAll("[data-resubmit]").forEach((button) => button.addEventListener("click", () => run(() => resubmitTask(button.dataset.resubmit), "任务已重新提交")));
  document.querySelectorAll("[data-log]").forEach((button) => button.addEventListener("click", () => run(() => showTaskLog(button.dataset.log))));
  document.querySelectorAll("[data-open-path]").forEach((button) => button.addEventListener("click", () => run(() => openPath(button.dataset.openPath))));
  document.querySelectorAll("[data-preview]").forEach((button) => button.addEventListener("click", () => run(() => previewFile(button.dataset.preview))));
}

window.addEventListener("hashchange", () => {
  const page = location.hash.replace("#/", "") || "dashboard";
  if (pages.some((item) => item.id === page)) {
    state.page = page;
    run(refreshPage);
  }
});

loadMe().then(refreshPage).catch(() => null).finally(render);
