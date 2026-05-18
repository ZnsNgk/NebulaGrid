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
    manual: null,
    envResult: null,
  },
};

const roleLabels = {
  student: "学生",
  mentor: "导师",
  admin: "管理员",
  viewer: "展示用户",
};

const pages = [
  { id: "dashboard", label: "总览", icon: "OV", permission: "dashboard:read" },
  { id: "tasks", label: "任务管理", icon: "TM", permission: "tasks:read" },
  { id: "files", label: "文件管理", icon: "FM", permission: "files:read" },
  { id: "envs", label: "环境管理", icon: "EM", permission: "envs:read" },
  { id: "manual", label: "使用手册", icon: "MD" },
  { id: "account", label: "账号管理", icon: "AC" },
  { id: "students", label: "学生管理", icon: "ST", roles: ["mentor"], permission: "users:read" },
  { id: "admin", label: "管理员后台", icon: "AD", roles: ["admin"], permission: "admin:settings:read" },
];

const demoManual = `# NebulaGrid（天枢）3.0 系统架构设计书

> 当前演示模式使用架构书作为使用手册占位。真实部署时页面会从后端读取 docs 目录中的 Markdown。

## 1. 使用入口

左侧导航按用户角色展示。学生可以使用任务、文件、环境和手册；导师额外拥有学生管理；管理员额外拥有管理员后台。

## 2. 节点监控

总览页面展示计算节点、CPU/GPU 使用率、可用内存/显存、上传下载和 GPU 调用进程数。历史指标写入 InfluxDB。

## 3. 任务与文件

任务管理用于提交训练命令、查看状态和日志；文件管理用于浏览平台开放的工作目录并预览文本文件。

## 4. 环境管理

环境管理用于登记用户环境，并为后续包安装、编译安装和环境检测流程预留入口。
`;

const demoStore = {
  token: "demo-token",
  users: [
    {
      id: 1,
      username: "admin",
      real_name: "演示管理员",
      role: "admin",
      state: "enabled",
      permissions: ["*"],
      home_path: "/home/ddltm/data/user/1",
      created_at: new Date().toISOString(),
    },
    {
      id: 2,
      username: "mentor",
      real_name: "演示导师",
      role: "mentor",
      state: "enabled",
      permissions: ["dashboard:read", "nodes:read", "tasks:read", "tasks:create", "files:read", "files:write", "envs:read", "envs:write", "users:read", "users:create_student"],
      home_path: "/home/ddltm/data/user/2",
      created_at: new Date().toISOString(),
    },
    {
      id: 3,
      username: "student",
      real_name: "演示学生",
      role: "student",
      state: "enabled",
      permissions: ["dashboard:read", "nodes:read", "tasks:read", "tasks:create", "files:read", "files:write", "envs:read", "envs:write"],
      home_path: "/home/ddltm/data/user/3",
      created_at: new Date().toISOString(),
    },
  ],
  currentUser: null,
  nodes: [
    {
      id: 1,
      name: "node-a",
      ip: "192.168.1.21",
      ssh_user: "ddltm",
      state: "online",
      scheduling_enabled: true,
      cpu_usage: 36,
      avail_ram_mb: 118784,
      upload_mbps: 12,
      download_mbps: 28,
      metric_collected_at: new Date().toISOString(),
      gpus: [
        { id: 1, gpu_index: 0, model: "NVIDIA A100", total_vram_mb: 40960, free_vram_mb: 31500, gpu_usage: 22, process_count: 1, schedulable: true },
        { id: 2, gpu_index: 1, model: "NVIDIA A100", total_vram_mb: 40960, free_vram_mb: 40220, gpu_usage: 0, process_count: 0, schedulable: true },
      ],
    },
    {
      id: 2,
      name: "node-b",
      ip: "192.168.1.22",
      ssh_user: "ddltm",
      state: "offline",
      scheduling_enabled: false,
      cpu_usage: null,
      avail_ram_mb: null,
      upload_mbps: null,
      download_mbps: null,
      gpus: [
        { id: 3, gpu_index: 0, model: "RTX 4090", total_vram_mb: 24576, free_vram_mb: null, gpu_usage: null, process_count: null, schedulable: false },
      ],
    },
  ],
  tasks: [],
  envs: [
    {
      id: 1,
      owner_user_id: 1,
      name: "torch-cu121",
      path: "/home/ddltm/envs/user_envs/1/torch-cu121",
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
    { name: "configs", path: "/workspace/configs", type: "directory", size_bytes: 0, modified_at: new Date().toISOString() },
  ],
  settings: [
    { key: "scheduler.enabled", value: "true", updated_by: null, updated_at: null },
    { key: "monitor.interval_seconds", value: "5", updated_by: null, updated_at: null },
    { key: "metrics.backend", value: "influxdb", updated_by: null, updated_at: null },
  ],
  auditLogs: [],
};

function can(permission) {
  if (!permission) return true;
  if (!state.user) return false;
  return state.user.permissions.includes("*") || state.user.permissions.includes(permission);
}

function hasRole(roles) {
  return !roles || roles.includes(state.user?.role);
}

function visiblePages() {
  return pages.filter((page) => hasRole(page.roles) && can(page.permission));
}

function currentPageMeta() {
  return pages.find((page) => page.id === state.page) || pages[0];
}

function ensureVisiblePage() {
  if (!state.user) return;
  const visible = visiblePages();
  if (!visible.some((page) => page.id === state.page)) {
    state.page = visible[0]?.id || "dashboard";
    location.hash = `/${state.page}`;
  }
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
  const body = options.body && !(options.body instanceof FormData) ? JSON.parse(options.body) : null;
  await new Promise((resolve) => window.setTimeout(resolve, 120));
  if (path === "/health") return ok({ service: "NebulaGrid", status: "ok", environment: "demo", version: "demo" });
  if (path === "/auth/login") return ok(loginDemoUser(body?.identity || "admin"));
  if (path === "/auth/me") return ok(demoStore.currentUser || demoStore.users[0]);
  if (path === "/dashboard/summary") return ok(buildDemoSummary());
  if (path === "/nodes") return ok(demoStore.nodes);
  if (path === "/admin/nodes" && method === "POST") return ok(addDemoNode(body));
  if (path.includes("/reconnect")) return ok(updateDemoNode(path.split("/")[3], "reconnecting"));
  if (path.includes("/force-offline")) return ok(updateDemoNode(path.split("/")[3], "manual_offline"));
  if (path === "/tasks" && method === "GET") return ok({ items: demoStore.tasks, total: demoStore.tasks.length, page: 1, page_size: 20 });
  if (path === "/tasks" && method === "POST") return ok(addDemoTask(body));
  if (path.includes("/cancel")) return ok(updateDemoTask(path.split("/")[2], "cancelled"));
  if (path.includes("/resubmit")) return ok(resubmitDemoTask(path.split("/")[2]));
  if (path.includes("/log")) return `[${path.split("/")[2]}] demo log\ntraining loss=0.024\ncompleted`;
  if (path === "/envs") return ok(demoStore.envs);
  if (path === "/envs/upload-pack" && method === "POST") return ok(addDemoEnv(body));
  if (path.endsWith("/test")) return ok({ status: "ok", message: "demo environment is ready", python: "3.11" });
  if (path.startsWith("/files/list")) return ok({ path: state.data.files.path || "/workspace", items: demoStore.files });
  if (path.startsWith("/files/preview")) return ok({ path: decodeURIComponent(path.split("path=")[1] || "/workspace/train.py"), content_type: "text/plain", content: "print('hello NebulaGrid')\n", truncated: false });
  if (path === "/users") return ok(demoStore.users);
  if (path === "/users" && method === "POST") return ok(addDemoUser(body));
  if (path === "/admin/settings" && method === "GET") return ok(demoStore.settings);
  if (path === "/admin/settings" && method === "PATCH") return ok(updateDemoSettings(body.values));
  if (path === "/admin/audit-logs") return ok({ items: demoStore.auditLogs, total: demoStore.auditLogs.length, page: 1, page_size: 20 });
  if (path === "/manual/current") return ok({ title: `${roleName(demoStore.currentUser?.role || "admin")}使用手册`, role: demoStore.currentUser?.role || "admin", source_path: "docs/NebulaGrid_Tianshu_3.0_System Architecture Design.md", content: demoManual });
  return ok({});
}

function ok(data) {
  return { ok: true, code: "OK", message: "success", data, request_id: `demo-${Date.now()}` };
}

function loginDemoUser(identity) {
  const lowered = String(identity || "admin").toLowerCase();
  demoStore.currentUser = demoStore.users.find((user) => user.username === lowered || user.role === lowered) || demoStore.users[0];
  return { access_token: demoStore.token, token_type: "bearer", user: demoStore.currentUser };
}

function buildDemoSummary() {
  const totalGpus = demoStore.nodes.reduce((sum, node) => sum + node.gpus.length, 0);
  const freeGpus = demoStore.nodes.flatMap((node) => node.gpus).filter((gpu) => gpu.schedulable && Number(gpu.process_count || 0) === 0).length;
  return {
    nodes_total: demoStore.nodes.length,
    nodes_online: demoStore.nodes.filter((node) => node.state === "online").length,
    gpus_total: totalGpus,
    gpus_available: freeGpus,
    tasks_waiting: demoStore.tasks.filter((task) => ["wait", "on_hold"].includes(task.state)).length,
    tasks_running: demoStore.tasks.filter((task) => task.state === "running").length,
    tasks_finished_today: demoStore.tasks.filter((task) => ["succeeded", "failed", "cancelled"].includes(task.state)).length,
    viewer_role: demoStore.currentUser?.role || "admin",
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
    cpu_usage: null,
    avail_ram_mb: null,
    upload_mbps: null,
    download_mbps: null,
    gpus: (payload.gpu_models || []).map((model, index) => ({ id: Date.now() + index, gpu_index: index, model, total_vram_mb: 0, schedulable: true })),
  };
  demoStore.nodes.push(node);
  pushAudit("node.create", "node", node.name);
  return node;
}

function updateDemoNode(nodeId, nextState) {
  const node = demoStore.nodes.find((item) => String(item.id) === String(nodeId));
  if (node) {
    node.state = nextState;
    node.scheduling_enabled = nextState === "online";
  }
  pushAudit(`node.${nextState}`, "node", String(nodeId));
  return node || {};
}

function addDemoTask(payload) {
  const id = demoStore.tasks.length + 1;
  const task = {
    id,
    task_id: `NG-${String(id).padStart(6, "0")}`,
    user_id: demoStore.currentUser?.id || 1,
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
  return task || {};
}

function resubmitDemoTask(taskId) {
  const task = demoStore.tasks.find((item) => item.task_id === taskId);
  if (!task) return {};
  return addDemoTask({ ...task, on_hold: false });
}

function addDemoEnv(payload) {
  const env = {
    id: demoStore.envs.length + 1,
    owner_user_id: demoStore.currentUser?.id || 1,
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
    id: Math.max(...demoStore.users.map((item) => item.id)) + 1,
    username: payload.username,
    real_name: payload.real_name,
    role: payload.role,
    state: payload.state,
    permissions: payload.role === "student" ? demoStore.users.find((item) => item.role === "student").permissions : [],
    home_path: `/home/ddltm/data/user/${demoStore.users.length + 1}`,
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
      item.updated_by = demoStore.currentUser?.id || 1;
      item.updated_at = nowText();
    } else {
      demoStore.settings.push({ key, value, updated_by: demoStore.currentUser?.id || 1, updated_at: nowText() });
    }
  });
  pushAudit("settings.update", "settings", Object.keys(values).join(","));
  return demoStore.settings;
}

function pushAudit(action, targetType, targetId) {
  demoStore.auditLogs.unshift({
    id: demoStore.auditLogs.length + 1,
    actor_user_id: demoStore.currentUser?.id || 1,
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

async function enterDemo(role = "admin") {
  state.demo = true;
  localStorage.setItem("ng_demo_mode", "1");
  const payload = await demoApi("/auth/login", { method: "POST", body: JSON.stringify({ identity: role, password: "demo" }) });
  state.token = payload.data.access_token;
  state.user = payload.data.user;
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
  ensureVisiblePage();
}

async function refreshPage() {
  await loadMe();
  ensureVisiblePage();
  const loaders = {
    dashboard: async () => {
      state.data.dashboard = (await api("/dashboard/summary")).data;
      if (can("nodes:read")) state.data.nodes = (await api("/nodes")).data;
    },
    tasks: async () => {
      state.data.tasks = (await api("/tasks")).data;
      if (can("envs:read")) state.data.envs = (await api("/envs")).data;
    },
    files: async () => {
      state.data.files = (await api(`/files/list?path=${encodeURIComponent(state.data.files.path || "/workspace")}`)).data;
    },
    envs: async () => {
      state.data.envs = (await api("/envs")).data;
    },
    manual: async () => {
      state.data.manual = (await api("/manual/current")).data;
    },
    account: async () => {
      if (can("users:read")) state.data.users = (await api("/users")).data;
    },
    students: async () => {
      state.data.users = (await api("/users")).data;
    },
    admin: async () => {
      state.data.nodes = (await api("/nodes")).data;
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

async function reconnectNode(nodeId) {
  await api(`/admin/nodes/${nodeId}/reconnect`, { method: "POST" });
  await refreshPage();
}

async function forceOfflineNode(nodeId) {
  await api(`/admin/nodes/${nodeId}/force-offline`, { method: "POST" });
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
  const log = await api(`/tasks/${taskId}/log?tail=200KB`);
  state.drawer = { title: `任务日志 ${taskId}`, body: `<pre class="drawer-log">${escapeHtml(log)}</pre>` };
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

async function testEnv(envId) {
  const payload = await api(`/envs/${envId}/test`, { method: "POST" });
  state.drawer = { title: `环境检测 #${envId}`, body: `<pre class="drawer-log">${escapeHtml(JSON.stringify(payload.data, null, 2))}</pre>` };
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

async function submitUser(event, fixedRole = null) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/users", {
    method: "POST",
    body: JSON.stringify({
      username: formValue(form, "username"),
      real_name: formValue(form, "real_name"),
      role: fixedRole || formValue(form, "role"),
      state: formValue(form, "state") || "enabled",
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
  const nav = visiblePages().map((page) => `
    <button class="nav-item ${state.page === page.id ? "active" : ""}" data-nav="${page.id}">
      <span>${page.icon}</span><b>${page.label}</b>
    </button>
  `).join("");
  const meta = currentPageMeta();
  return `
    <div class="layout">
      <aside class="sidebar">
        <div class="brand">
          <div class="brand-logo">NG</div>
          <div>
            <strong>NebulaGrid</strong>
            <small>天枢 3.0 控制台</small>
          </div>
        </div>
        <nav>${nav}</nav>
        <section class="user-card">
          <strong>${escapeHtml(state.user?.real_name || "未登录")}</strong>
          <span>${escapeHtml(state.user?.username || "")} · ${roleName(state.user?.role)}</span>
          <button class="secondary" data-action="logout">退出登录</button>
        </section>
      </aside>
      <main class="workspace">
        <header class="topbar">
          <div>
            <p>${state.demo ? "演示模式" : "在线模式"} · ${escapeHtml(state.apiBase)}</p>
            <h1>${meta.label}</h1>
          </div>
          <div class="top-actions">
            ${state.demo ? renderDemoRoleButtons() : ""}
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

function renderDemoRoleButtons() {
  return `
    <div class="segmented" aria-label="演示角色">
      ${["student", "mentor", "admin"].map((role) => `
        <button class="${state.user?.role === role ? "active" : ""}" data-demo-role="${role}">${roleName(role)}</button>
      `).join("")}
    </div>
  `;
}

function renderLogin() {
  return `
    <main class="login-page">
      <section class="login-copy">
        <p>NebulaGrid 3.0</p>
        <h1>面向实验室多机多卡的任务调度与资源管理</h1>
        <div class="login-stats">
          <span>节点监控</span>
          <span>任务队列</span>
          <span>文件工作区</span>
          <span>环境维护</span>
        </div>
      </section>
      <section class="login-card">
        <h2>登录控制台</h2>
        <form id="loginForm" class="form-stack">
          <label>API 地址<input name="apiBase" value="${escapeAttr(state.apiBase)}"></label>
          <label>账号<input name="identity" autocomplete="username" value="admin"></label>
          <label>密码<input name="password" type="password" autocomplete="current-password" value="admin123"></label>
          <button type="submit">登录</button>
          <button type="button" class="secondary" data-action="demo">进入演示模式</button>
        </form>
      </section>
    </main>
  `;
}

function renderDashboard() {
  const summary = state.data.dashboard || {};
  const metrics = [
    ["节点总数", summary.nodes_total ?? "-"],
    ["在线节点", summary.nodes_online ?? "-"],
    ["GPU 总数", summary.gpus_total ?? "-"],
    ["可用 GPU", summary.gpus_available ?? "-"],
    ["运行任务", summary.tasks_running ?? "-"],
  ];
  return shell(`
    <section class="metrics">${metrics.map(([label, value]) => `<article><span>${label}</span><strong>${value}</strong></article>`).join("")}</section>
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>计算节点监控</h2>
          <span>master 节点不会显示在这里；这里只展示计算节点的实时快照。</span>
        </div>
      </div>
      ${state.data.nodes.length ? `<div class="node-grid">${state.data.nodes.map(renderNodeCard).join("")}</div>` : renderEmpty("暂无计算节点")}
    </section>
  `);
}

function renderNodeCard(node) {
  return `
    <article class="node-card">
      <div class="node-head">
        <div>
          <h3>${escapeHtml(node.name)}</h3>
          <p>${escapeHtml(node.ip)} · ${escapeHtml(node.ssh_user)}</p>
        </div>
        <span class="status ${node.state}">${stateText(node.state)}</span>
      </div>
      <div class="node-stats">
        ${miniMetric("CPU", percent(node.cpu_usage))}
        ${miniMetric("可用内存", formatMb(node.avail_ram_mb))}
        ${miniMetric("上传", speed(node.upload_mbps))}
        ${miniMetric("下载", speed(node.download_mbps))}
      </div>
      <div class="gpu-list">
        ${(node.gpus || []).map((gpu) => `
          <div class="gpu-row">
            <div>
              <strong>GPU ${gpu.gpu_index}</strong>
              <span>${escapeHtml(gpu.model || "Unknown")}</span>
            </div>
            <div class="gpu-metrics">
              ${bar("使用率", gpu.gpu_usage)}
              ${bar("显存", gpu.total_vram_mb && gpu.free_vram_mb !== null && gpu.free_vram_mb !== undefined ? Math.round((1 - gpu.free_vram_mb / gpu.total_vram_mb) * 100) : null)}
              <span>可用显存 ${formatMb(gpu.free_vram_mb)}</span>
              <span>调用进程 ${emptyDash(gpu.process_count)}</span>
            </div>
          </div>
        `).join("")}
      </div>
    </article>
  `;
}

function renderTasks() {
  const tasks = state.data.tasks.items || [];
  const envOptions = state.data.envs.map((env) => `<option value="${env.id}">${escapeHtml(env.name)}</option>`).join("");
  return shell(`
    <section class="panel">
      <div class="panel-head"><div><h2>提交训练任务</h2><span>沿用 2.0 的任务入口，但按 3.0 API 保存为结构化任务。</span></div></div>
      <form id="taskForm" class="form-grid task-form">
        <label class="wide">任务描述<input name="description" placeholder="ResNet 训练 / 参数搜索 / 数据预处理"></label>
        <label>运行环境<select name="env_id"><option value="">不指定</option>${envOptions}</select></label>
        <label>优先级<input name="priority" type="number" min="0" max="100" value="0"></label>
        <label class="wide">工作目录<input name="workdir" value="/workspace"></label>
        <label>GPU 数量<input name="need_gpus" type="number" min="0" max="16" value="1"></label>
        <label>GPU 型号<input name="gpu_types" placeholder="A100,4090"></label>
        <label class="check"><input name="allow_gpu_reuse" type="checkbox">允许复用 GPU</label>
        <label class="check"><input name="on_hold" type="checkbox">先挂起</label>
        <label class="full-row">执行命令<textarea name="command" placeholder="python train.py --config configs/default.yaml" required></textarea></label>
        <div class="form-actions"><button type="submit">提交任务</button></div>
      </form>
    </section>
    <section class="panel">
      <div class="panel-head"><div><h2>任务列表</h2><span>等待、运行和历史任务统一展示。</span></div></div>
      ${tasks.length ? renderTable(["任务", "状态", "资源", "命令", "操作"], tasks.map((task) => [
        `<strong>${escapeHtml(task.task_id)}</strong><br><span class="muted">${escapeHtml(task.description || "无描述")}</span>`,
        `<span class="status ${task.state}">${stateText(task.state)}</span><br><span class="muted">${formatDate(task.created_at)}</span>`,
        `GPU ${task.requirement?.need_gpus ?? 0}<br><span class="muted">${escapeHtml((task.requirement?.gpu_types || []).join(", ") || "不限型号")}</span>`,
        `<code>${escapeHtml(task.command)}</code>`,
        `<button class="small secondary" data-log="${escapeAttr(task.task_id)}">日志</button><button class="small secondary" data-resubmit="${escapeAttr(task.task_id)}">重提</button><button class="small danger" data-cancel="${escapeAttr(task.task_id)}">取消</button>`,
      ])) : renderEmpty("暂无任务")}
    </section>
  `);
}

function renderFiles() {
  const files = state.data.files.items || [];
  return shell(`
    <section class="panel">
      <div class="panel-head"><div><h2>工作区文件</h2><span>文件接口会限制在后端允许开放的路径内。</span></div></div>
      <form id="fileForm" class="path-bar">
        <input name="path" value="${escapeAttr(state.data.files.path || "/workspace")}">
        <button type="submit">打开路径</button>
      </form>
      ${files.length ? renderTable(["名称", "类型", "大小", "修改时间", "操作"], files.map((item) => [
        `<strong>${escapeHtml(item.name)}</strong><br><span class="muted">${escapeHtml(item.path)}</span>`,
        item.type === "directory" ? "目录" : "文件",
        formatBytes(item.size_bytes),
        formatDate(item.modified_at),
        item.type === "directory"
          ? `<button class="small secondary" data-open-path="${escapeAttr(item.path)}">进入</button>`
          : `<button class="small secondary" data-preview="${escapeAttr(item.path)}">预览</button>`,
      ])) : renderEmpty("当前目录为空")}
    </section>
  `);
}

function renderEnvs() {
  const envs = state.data.envs || [];
  return shell(`
    <section class="panel">
      <div class="panel-head"><div><h2>登记环境</h2><span>当前表单登记已有 conda 环境，后续环境包安装会接入同一页面。</span></div></div>
      <form id="envForm" class="form-grid">
        <label>环境名称<input name="name" placeholder="torch-cu121" required></label>
        <label class="wide">环境路径<input name="path" placeholder="/home/ddltm/envs/user_envs/1/torch-cu121" required></label>
        <label>Python 版本<input name="python_version" placeholder="3.11"></label>
        <label class="full-row">说明<input name="description" placeholder="PyTorch CUDA 12.1"></label>
        <div class="form-actions"><button type="submit">登记环境</button></div>
      </form>
    </section>
    <section class="panel">
      <div class="panel-head"><div><h2>环境列表</h2><span>可对环境执行连通性检测。</span></div></div>
      ${envs.length ? renderTable(["名称", "状态", "路径", "版本", "操作"], envs.map((env) => [
        `<strong>${escapeHtml(env.name)}</strong><br><span class="muted">${escapeHtml(env.description || "")}</span>`,
        `<span class="status ${env.state}">${stateText(env.state)}</span>`,
        `<code>${escapeHtml(env.path)}</code>`,
        escapeHtml(env.python_version || "-"),
        `<button class="small secondary" data-test-env="${env.id}">检测</button>`,
      ])) : renderEmpty("暂无环境")}
    </section>
  `);
}

function renderManual() {
  const manual = state.data.manual;
  return shell(`
    <section class="panel manual-shell">
      <div class="panel-head">
        <div>
          <h2>${escapeHtml(manual?.title || "使用手册")}</h2>
          <span>${escapeHtml(manual?.source_path || "docs/NebulaGrid_Tianshu_3.0_System Architecture Design.md")} · ${roleName(manual?.role || state.user?.role)}</span>
        </div>
      </div>
      <article class="markdown-body">${manual ? renderMarkdown(manual.content) : renderEmpty("手册加载中")}</article>
    </section>
  `);
}

function renderAccount() {
  const permissions = state.user?.permissions || [];
  const users = state.data.users || [];
  return shell(`
    <section class="split">
      <article class="panel">
        <div class="panel-head"><div><h2>当前账号</h2><span>账号身份决定左侧可见页面和可执行操作。</span></div></div>
        <dl class="kv">
          <dt>姓名</dt><dd>${escapeHtml(state.user?.real_name || "-")}</dd>
          <dt>用户名</dt><dd>${escapeHtml(state.user?.username || "-")}</dd>
          <dt>角色</dt><dd>${roleName(state.user?.role)}</dd>
          <dt>状态</dt><dd>${stateText(state.user?.state || "enabled")}</dd>
        </dl>
      </article>
      <article class="panel">
        <div class="panel-head"><div><h2>权限</h2><span>后端 RBAC 会再次校验所有请求。</span></div></div>
        <div class="permission-list">${permissions.map((permission) => `<span>${escapeHtml(permission)}</span>`).join("")}</div>
      </article>
    </section>
    ${state.user?.role === "admin" ? `
      <section class="panel">
        <div class="panel-head"><div><h2>创建账号</h2><span>管理员可创建学生、导师、管理员或展示账号。</span></div></div>
        <form id="userForm" class="form-grid">
          <label>用户名<input name="username" required></label>
          <label>姓名<input name="real_name" required></label>
          <label>角色<select name="role"><option value="student">学生</option><option value="mentor">导师</option><option value="admin">管理员</option><option value="viewer">展示用户</option></select></label>
          <label>状态<select name="state"><option value="enabled">启用</option><option value="disabled">禁用</option></select></label>
          <label>初始密码<input name="password" type="password" minlength="8" required></label>
          <div class="form-actions"><button type="submit">创建账号</button></div>
        </form>
      </section>
      <section class="panel">
        <div class="panel-head"><div><h2>账号列表</h2><span>用户 home 目录会映射到 /home/ddltm/data/user/&lt;user_id&gt;。</span></div></div>
        ${users.length ? renderUserTable(users) : renderEmpty("暂无账号")}
      </section>
    ` : ""}
  `);
}

function renderStudents() {
  const students = (state.data.users || []).filter((user) => user.role === "student");
  return shell(`
    <section class="panel">
      <div class="panel-head"><div><h2>创建学生账号</h2><span>导师账号只能创建学生，不能创建导师或管理员。</span></div></div>
      <form id="studentForm" class="form-grid">
        <label>用户名<input name="username" required></label>
        <label>姓名<input name="real_name" required></label>
        <label>初始密码<input name="password" type="password" minlength="8" required></label>
        <input type="hidden" name="state" value="enabled">
        <div class="form-actions"><button type="submit">创建学生</button></div>
      </form>
    </section>
    <section class="panel">
      <div class="panel-head"><div><h2>学生列表</h2><span>后续可在这里接入导师-学生绑定关系。</span></div></div>
      ${students.length ? renderUserTable(students) : renderEmpty("暂无学生账号")}
    </section>
  `);
}

function renderAdmin() {
  const nodes = state.data.nodes || [];
  const settings = state.data.settings || [];
  const auditItems = state.data.auditLogs.items || [];
  return shell(`
    <section class="panel">
      <div class="panel-head"><div><h2>登记计算节点</h2><span>只登记计算节点，master/control-plane 节点会被后端拒绝或过滤。</span></div></div>
      <form id="nodeForm" class="form-grid">
        <label>节点名称<input name="name" placeholder="node-a" required></label>
        <label>IP 地址<input name="ip" placeholder="192.168.1.21" required></label>
        <label>SSH 用户<input name="ssh_user" value="ddltm"></label>
        <label class="wide">GPU 型号列表<input name="gpu_models" placeholder="A100,A100"></label>
        <div class="form-actions"><button type="submit">登记节点</button></div>
      </form>
    </section>
    <section class="panel">
      <div class="panel-head"><div><h2>节点运维</h2><span>重新连接或强制下线会写入审计日志。</span></div></div>
      ${nodes.length ? renderTable(["节点", "状态", "资源", "操作"], nodes.map((node) => [
        `<strong>${escapeHtml(node.name)}</strong><br><span class="muted">${escapeHtml(node.ip)}</span>`,
        `<span class="status ${node.state}">${stateText(node.state)}</span>`,
        `${(node.gpus || []).length} GPU<br><span class="muted">调度 ${node.scheduling_enabled ? "开启" : "关闭"}</span>`,
        `<button class="small secondary" data-reconnect-node="${node.id}">重连</button><button class="small danger" data-offline-node="${node.id}">下线</button>`,
      ])) : renderEmpty("暂无节点")}
    </section>
    <section class="split">
      <article class="panel">
        <div class="panel-head"><div><h2>系统设置</h2><span>保存后立即写入后端设置存储。</span></div></div>
        <form id="settingForm" class="form-grid compact-form">
          <label>键<input name="key" placeholder="scheduler.enabled" required></label>
          <label>值<input name="value" placeholder="true" required></label>
          <div class="form-actions"><button type="submit">保存</button></div>
        </form>
        ${settings.length ? renderTable(["键", "值"], settings.map((item) => [escapeHtml(item.key), `<code>${escapeHtml(item.value)}</code>`])) : renderEmpty("暂无设置")}
      </article>
      <article class="panel">
        <div class="panel-head"><div><h2>审计日志</h2><span>关键管理动作会留痕。</span></div></div>
        ${auditItems.length ? renderTable(["动作", "对象", "时间"], auditItems.map((item) => [
          escapeHtml(item.action),
          `${escapeHtml(item.target_type)} #${escapeHtml(item.target_id)}`,
          formatDate(item.created_at),
        ])) : renderEmpty("暂无审计日志")}
      </article>
    </section>
  `);
}

function renderUserTable(users) {
  return renderTable(["账号", "角色", "状态", "Home"], users.map((user) => [
    `<strong>${escapeHtml(user.real_name)}</strong><br><span class="muted">${escapeHtml(user.username)}</span>`,
    roleName(user.role),
    stateText(user.state),
    `<code>${escapeHtml(user.home_path || "-")}</code>`,
  ]));
}

function renderTable(headers, rows) {
  return `
    <div class="table-wrap">
      <table>
        <thead><tr>${headers.map((header) => `<th>${header}</th>`).join("")}</tr></thead>
        <tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody>
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

function renderEmpty(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}

function miniMetric(label, value) {
  return `<span><b>${label}</b><strong>${value}</strong></span>`;
}

function bar(label, value) {
  const safe = value === null || value === undefined ? null : Math.max(0, Math.min(100, Number(value) || 0));
  return `
    <label class="bar-label">
      <span>${label}</span><b>${safe === null ? "-" : `${safe}%`}</b>
      <i><em style="width:${safe || 0}%"></em></i>
    </label>
  `;
}

function renderMarkdown(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const html = [];
  let inCode = false;
  let codeLines = [];
  let inList = false;

  const closeList = () => {
    if (inList) {
      html.push("</ul>");
      inList = false;
    }
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim().startsWith("```")) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        codeLines = [];
        inCode = false;
      } else {
        closeList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }
    if (!line.trim()) {
      closeList();
      continue;
    }
    if (line.trim().startsWith("|")) {
      closeList();
      const tableLines = [];
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        tableLines.push(lines[index]);
        index += 1;
      }
      index -= 1;
      html.push(renderMarkdownTable(tableLines));
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = Math.min(heading[1].length + 1, 5);
      html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    const list = line.match(/^\s*[-*]\s+(.+)$/);
    if (list) {
      if (!inList) {
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${inlineMarkdown(list[1])}</li>`);
      continue;
    }
    if (line.startsWith(">")) {
      closeList();
      html.push(`<blockquote>${inlineMarkdown(line.replace(/^>\s?/, ""))}</blockquote>`);
      continue;
    }
    closeList();
    html.push(`<p>${inlineMarkdown(line)}</p>`);
  }
  closeList();
  return html.join("");
}

function renderMarkdownTable(lines) {
  const rows = lines
    .map((line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => inlineMarkdown(cell.trim())))
    .filter((row) => !row.every((cell) => /^:?-{3,}:?$/.test(cell)));
  if (!rows.length) return "";
  const [head, ...body] = rows;
  return `
    <div class="markdown-table">
      <table>
        <thead><tr>${head.map((cell) => `<th>${cell}</th>`).join("")}</tr></thead>
        <tbody>${body.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
    </div>
  `;
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function roleName(role) {
  return roleLabels[role] || role || "-";
}

function stateText(value) {
  const map = {
    online: "在线",
    offline: "离线",
    manual_offline: "手动下线",
    reconnecting: "重连中",
    wait: "等待",
    on_hold: "挂起",
    running: "运行中",
    succeeded: "完成",
    failed: "失败",
    cancelled: "已取消",
    available: "可用",
    registered: "已登记",
    enabled: "启用",
    disabled: "禁用",
  };
  return map[value] || value || "-";
}

function percent(value) {
  return value === null || value === undefined ? "-" : `${value}%`;
}

function speed(value) {
  return value === null || value === undefined ? "-" : `${value} Mbps`;
}

function formatMb(value) {
  if (value === null || value === undefined) return "-";
  if (value >= 1024) return `${(value / 1024).toFixed(1)} GB`;
  return `${value} MB`;
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (size >= 1024 ** 3) return `${(size / 1024 ** 3).toFixed(1)} GB`;
  if (size >= 1024 ** 2) return `${(size / 1024 ** 2).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function emptyDash(value) {
  return value === null || value === undefined ? "-" : value;
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
    tasks: renderTasks,
    files: renderFiles,
    envs: renderEnvs,
    manual: renderManual,
    account: renderAccount,
    students: renderStudents,
    admin: renderAdmin,
  };
  ensureVisiblePage();
  document.querySelector("#app").innerHTML = state.user ? (renderers[state.page] || renderDashboard)() : renderLogin();
  bindEvents();
}

function bindEvents() {
  document.querySelector("#loginForm")?.addEventListener("submit", (event) => run(() => login(event), "登录成功"));
  document.querySelector("#nodeForm")?.addEventListener("submit", (event) => run(() => submitNode(event), "节点已登记"));
  document.querySelector("#taskForm")?.addEventListener("submit", (event) => run(() => submitTask(event), "任务已提交"));
  document.querySelector("#envForm")?.addEventListener("submit", (event) => run(() => submitEnv(event), "环境已登记"));
  document.querySelector("#userForm")?.addEventListener("submit", (event) => run(() => submitUser(event), "账号已创建"));
  document.querySelector("#studentForm")?.addEventListener("submit", (event) => run(() => submitUser(event, "student"), "学生已创建"));
  document.querySelector("#settingForm")?.addEventListener("submit", (event) => run(() => updateSetting(event), "设置已保存"));
  document.querySelector("#fileForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    run(() => openPath(formValue(event.currentTarget, "path")));
  });
  document.querySelectorAll("[data-nav]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.nav)));
  document.querySelector("[data-action='logout']")?.addEventListener("click", () => run(logout));
  document.querySelector("[data-action='refresh']")?.addEventListener("click", () => run(refreshPage, "已刷新"));
  document.querySelector("[data-action='demo']")?.addEventListener("click", () => run(() => enterDemo("admin"), "已进入演示模式"));
  document.querySelector("[data-action='close-drawer']")?.addEventListener("click", () => {
    state.drawer = null;
    render();
  });
  document.querySelectorAll("[data-demo-role]").forEach((button) => button.addEventListener("click", () => run(() => enterDemo(button.dataset.demoRole), `已切换为${roleName(button.dataset.demoRole)}`)));
  document.querySelectorAll("[data-cancel]").forEach((button) => button.addEventListener("click", () => run(() => cancelTask(button.dataset.cancel), "任务已取消")));
  document.querySelectorAll("[data-resubmit]").forEach((button) => button.addEventListener("click", () => run(() => resubmitTask(button.dataset.resubmit), "任务已重新提交")));
  document.querySelectorAll("[data-log]").forEach((button) => button.addEventListener("click", () => run(() => showTaskLog(button.dataset.log))));
  document.querySelectorAll("[data-open-path]").forEach((button) => button.addEventListener("click", () => run(() => openPath(button.dataset.openPath))));
  document.querySelectorAll("[data-preview]").forEach((button) => button.addEventListener("click", () => run(() => previewFile(button.dataset.preview))));
  document.querySelectorAll("[data-test-env]").forEach((button) => button.addEventListener("click", () => run(() => testEnv(button.dataset.testEnv))));
  document.querySelectorAll("[data-reconnect-node]").forEach((button) => button.addEventListener("click", () => run(() => reconnectNode(button.dataset.reconnectNode), "已提交重连")));
  document.querySelectorAll("[data-offline-node]").forEach((button) => button.addEventListener("click", () => run(() => forceOfflineNode(button.dataset.offlineNode), "已强制下线")));
}

window.addEventListener("hashchange", () => {
  const page = location.hash.replace("#/", "") || "dashboard";
  if (pages.some((item) => item.id === page)) {
    state.page = page;
    run(refreshPage);
  }
});

if (state.demo && !demoStore.currentUser) {
  demoStore.currentUser = demoStore.users[0];
  state.user = demoStore.currentUser;
}

loadMe().then(refreshPage).catch(() => null).finally(render);
