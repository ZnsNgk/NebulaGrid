const state = {
  apiBase: localStorage.getItem("ng_api_base") || `${location.origin}/api`,
  token: localStorage.getItem("ng_token") || "",
  deviceId: getOrCreateDeviceId(),
  user: null,
  page: location.hash.replace("#/", "") || "dashboard",
  taskZone: localStorage.getItem("ng_task_zone") || "wait",
  adminMenu: localStorage.getItem("ng_admin_menu") || "overview",
  auditCategory: localStorage.getItem("ng_audit_category") || "all",
  auditPage: 1,
  auditPageSize: Number(localStorage.getItem("ng_audit_page_size") || 20),
  auditFilters: { keyword: "", action: "", start_time: "", end_time: "" },
  toast: null,
  loginError: null,
  loading: false,
  userFilters: { user_id: "", keyword: "", role: "", state: "" },
  loginFilters: { user_id: "", keyword: "" },
  autoRefreshSeconds: Number(localStorage.getItem("ng_dashboard_refresh_seconds") || 5),
  autoRefreshTimer: null,
  autoRefreshBusy: false,
  sessionRefreshTimer: null,
  sessionRefreshBusy: false,
  fileJobRefreshTimer: null,
  fileJobRefreshBusy: false,
  authWatchTimer: null,
  authWatchBusy: false,
  lastDashboardRefreshAt: null,
  drawer: null,
  fileTargetPicker: null,
  data: {
    dashboard: null,
    nodes: [],
    tasks: { items: [], total: 0, page: 1, page_size: 20 },
    envs: [],
    files: { path: "/", items: [] },
    preview: null,
    selectedFilePath: "",
    fileJob: null,
    users: [],
    mentors: [],
    settings: [],
    auditLogs: { items: [], total: 0, page: 1, page_size: 20 },
    manual: null,
    envResult: null,
    sessions: [],
    adminOnlineUsers: [],
    adminUserSessions: [],
  },
};

const LOGIN_DEVICE_REFRESH_MS = 3000;
const AUTH_WATCH_MS = 3000;
const FILE_JOB_REFRESH_MS = 2000;

const authExpiredMessages = new Set([
  "invalid token",
  "session offline",
  "unauthorized",
  "登录状态已失效，请重新登录",
  "当前登录设备已下线，请重新登录",
  "账号已停用，请重新登录",
]);

const errorMessageMap = {
  "invalid token": "登录状态已失效，请重新登录",
  "session offline": "当前登录设备已下线，请重新登录",
  unauthorized: "登录状态已失效，请重新登录",
  "invalid identity or password": "账号或密码错误",
  "current password is invalid": "当前密码错误",
  "last admin user cannot be deleted": "不能删除最后一个管理员账号",
  "last admin user cannot be disabled": "不能停用最后一个管理员账号",
  "last admin user cannot be downgraded": "不能降级最后一个管理员账号",
  "username already exists": "用户名已存在",
  "user id already exists": "统一识别码已存在",
  "user not found": "用户不存在",
  "only admin can change usernames": "只有管理员可以修改用户名",
  "student can have at most two supervisors": "每名学生最多只能选择两名导师",
  "supervisor must be mentor user": "所选导师账号无效",
  "mentor can only manage assigned student users": "导师只能管理自己名下的学生",
  "mentor can only reset assigned student passwords": "导师只能重置自己名下学生的密码",
  "permission required: admin:login:read": "只有管理员可以查看登录管理",
  "permission required: admin:login:write": "只有管理员可以下线登录设备",
  "session not found": "登录设备不存在或已失效",
  "target already exists": "目标已存在",
  "path not found": "路径不存在",
  "path is not a file": "请选择文件",
  "path is not a directory": "请选择目录",
  "parent directory does not exist": "父目录不存在",
  "refusing to operate on protected root": "不能操作受保护的根目录",
  "target_path is required": "请提供目标路径",
  "target cannot be inside source directory": "目标目录不能位于源目录内部",
  "file job already running": "当前已有打包或解压任务正在运行",
  "too many file jobs running": "当前服务器打包或解压任务较多，请稍后再试",
  "unsupported archive type": "请选择 zip/tar/tar.gz/tar.bz2/tar.xz 压缩包",
  "target path is not a directory": "请选择解压目标文件夹",
  "zip command failed": "zip 命令执行失败",
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

function getOrCreateDeviceId() {
  const key = "ng_device_id";
  let value = localStorage.getItem(key);
  if (!value) {
    const random = window.crypto?.randomUUID ? window.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    value = `web-${random}`;
    localStorage.setItem(key, value);
  }
  return value;
}

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
  window.setTimeout(() => {
    if (state.toast?.text === text) {
      state.toast = null;
      document.querySelector(".toast")?.remove();
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
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  headers["X-NG-Device-Id"] = state.deviceId;
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(`${state.apiBase.replace(/\/$/, "")}${path}`, { ...options, headers });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const rawMessage = typeof payload === "string" ? payload : (payload.code === "VALIDATION_ERROR" ? extractValidationMessage(payload) : payload.message);
    const message = normalizeHttpError(response.status, rawMessage || `HTTP ${response.status}`);
    if (response.status === 401 && path !== "/auth/login" && state.token) {
      forceLoginRedirect(message);
    }
    throw new Error(message);
  }
  return payload;
}

function normalizeHttpError(status, message) {
  if (status === 413) {
    return "上传文件过大：请通过scp上传。";
  }
  return normalizeErrorMessage(message);
}

function normalizeErrorMessage(message) {
  const text = String(message || "操作失败").trim();
  if (text.includes("413 Request Entity Too Large")) {
    return "上传文件过大：请通过scp上传。";
  }
  return errorMessageMap[text] || text;
}

function extractValidationMessage(payload) {
  const errors = payload?.data?.errors || [];
  if (!Array.isArray(errors) || !errors.length) return payload?.message || "request validation failed";
  const first = errors[0];
  const field = Array.isArray(first.loc) ? first.loc.filter((part) => part !== "body").join(".") : "";
  const labelMap = {
    user_id: "统一识别码",
    username: "用户名",
    real_name: "姓名",
    role: "角色",
    state: "状态",
    password: "密码",
    current_password: "当前密码",
    new_password: "新密码",
    session_id: "登录设备",
  };
  const label = labelMap[field] || field || "请求参数";
  return `${label}格式不正确或不能为空`;
}

function isAuthExpiredMessage(message) {
  return authExpiredMessages.has(String(message || "").trim());
}

function resetLocalLoginState(message = "") {
  state.token = "";
  state.user = null;
  state.data.sessions = [];
  state.drawer = null;
  if (message) state.loginError = message;
  localStorage.removeItem("ng_token");
  state.page = "dashboard";
  if (location.hash !== "#/dashboard") {
    history.replaceState(null, "", "#/dashboard");
  }
  updateRealtimeTimers();
}

function forceLoginRedirect(message = "登录状态已失效，请重新登录") {
  resetLocalLoginState(message);
  render();
}

function formValue(form, name) {
  return String(new FormData(form).get(name) || "").trim();
}

function cleanObject(values) {
  return Object.fromEntries(Object.entries(values).filter(([, value]) => value !== "" && value !== null && value !== undefined));
}

function auditLogPath(page = state.auditPage, pageSize = state.auditPageSize, category = state.auditCategory, filters = state.auditFilters) {
  const params = new URLSearchParams({
    page: String(Math.max(1, Number(page) || 1)),
    page_size: String(normalizeAuditPageSize(pageSize)),
    category: category || "all",
  });
  Object.entries(cleanObject(filters || {})).forEach(([key, value]) => {
    params.set(key, key.endsWith("_time") ? toIsoDateTime(value) : value);
  });
  return `/admin/audit-logs?${params.toString()}`;
}

function toIsoDateTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toISOString();
}

function normalizeAuditPageSize(value) {
  const pageSize = Number(value) || 20;
  return [10, 20, 50, 100].includes(pageSize) ? pageSize : 20;
}

function cleanUserFilters(filters) {
  const body = cleanObject(filters || {});
  if (body.user_id) body.user_id = Number(body.user_id);
  return body;
}

function parseList(value) {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

async function login(event) {
  event.preventDefault();
  const form = event.currentTarget;
  state.loginError = null;
  try {
    const payload = await api("/auth/login", {
      method: "POST",
      body: JSON.stringify({ identity: formValue(form, "identity"), password: formValue(form, "password") }),
    });
    state.token = payload.data.access_token;
    state.user = payload.data.user;
    localStorage.setItem("ng_token", state.token);
    navigateAfterLogin();
    await refreshPage();
    updateRealtimeTimers();
  } catch (error) {
    state.loginError = normalizeErrorMessage(error.message);
    throw error;
  }
}

function navigateAfterLogin() {
  state.page = "dashboard";
  state.drawer = null;
  if (location.hash !== "#/dashboard") {
    history.replaceState(null, "", "#/dashboard");
  }
}

async function logout() {
  try {
    if (state.token) await api("/auth/logout", { method: "POST" });
  } catch (error) {
    console.warn("logout request failed", error);
  }
  resetLocalLoginState();
  render();
}

async function loadMe() {
  if (!state.token) return;
  const payload = await api("/auth/me", { method: "POST" });
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
      state.lastDashboardRefreshAt = new Date();
    },
    tasks: async () => {
      state.data.tasks = (await api(`/tasks?state=${encodeURIComponent(state.taskZone)}`)).data;
      if (can("envs:read")) state.data.envs = (await api("/envs")).data;
    },
    files: async () => {
      state.data.files = (await api(`/files/list?path=${encodeURIComponent(state.data.files.path || "/")}`)).data;
      state.data.fileJob = (await api("/files/jobs/latest")).data;
      updateFileJobTimer();
    },
    envs: async () => {
      state.data.envs = (await api("/envs")).data;
    },
    manual: async () => {
      state.data.manual = (await api("/manual/current")).data;
    },
    account: async () => {
      state.data.sessions = (await api("/auth/sessions/list", { method: "POST" })).data;
    },
    students: async () => {
      const filters = cleanUserFilters({ ...state.userFilters, role: "student" });
      state.data.users = (await api("/users/list", { method: "POST", body: JSON.stringify(filters) })).data;
    },
    admin: async () => {
      state.data.nodes = (await api("/nodes")).data;
      state.data.users = (await api("/users/list", { method: "POST", body: JSON.stringify(cleanUserFilters(state.userFilters)) })).data;
      state.data.mentors = (await api("/users/list", { method: "POST", body: JSON.stringify({ role: "mentor" }) })).data;
      state.data.adminOnlineUsers = (await api("/admin/login-management/online-users", { method: "POST", body: JSON.stringify({}) })).data;
      const loginQuery = cleanObject(state.loginFilters);
      state.data.adminUserSessions = Object.keys(loginQuery).length
        ? (await api("/admin/login-management/user-sessions", { method: "POST", body: JSON.stringify(loginQuery) })).data
        : [];
      state.data.settings = (await api("/admin/settings")).data;
      const auditCategory = state.adminMenu === "audit" ? state.auditCategory || "all" : "all";
      const auditPage = state.adminMenu === "audit" ? state.auditPage || 1 : 1;
      const auditFilters = state.adminMenu === "audit" ? state.auditFilters : {};
      state.data.auditLogs = (await api(auditLogPath(auditPage, state.auditPageSize, auditCategory, auditFilters))).data;
    },
  };
  await loaders[state.page]?.();
}

function navigate(page) {
  const previousPage = state.page;
  state.page = page;
  if (page !== previousPage && ["admin", "students"].includes(page)) {
    state.userFilters = { user_id: "", keyword: "", role: "", state: "" };
  }
  location.hash = `/${page}`;
  state.drawer = null;
  updateRealtimeTimers();
  run(refreshPage);
}

function setDashboardRefreshSeconds(value) {
  const seconds = Math.max(0, Math.min(3600, Number(value) || 0));
  state.autoRefreshSeconds = seconds;
  localStorage.setItem("ng_dashboard_refresh_seconds", String(seconds));
  updateRealtimeTimers();
  render();
}

function updateRealtimeTimers() {
  updateAutoRefreshTimer();
  updateSessionRefreshTimer();
  updateFileJobTimer();
  updateAuthWatchTimer();
}

function updateAutoRefreshTimer() {
  if (state.autoRefreshTimer) {
    window.clearInterval(state.autoRefreshTimer);
    state.autoRefreshTimer = null;
  }
  if (!state.user || state.page !== "dashboard" || state.autoRefreshSeconds <= 0) return;
  state.autoRefreshTimer = window.setInterval(autoRefreshDashboard, state.autoRefreshSeconds * 1000);
}

async function autoRefreshDashboard() {
  if (!state.user || state.page !== "dashboard" || state.autoRefreshBusy) return;
  state.autoRefreshBusy = true;
  try {
    await refreshPage();
    render();
  } catch (error) {
    console.warn("dashboard auto refresh failed", error);
  } finally {
    state.autoRefreshBusy = false;
  }
}

async function refreshSessionsLive() {
  if (!state.user || state.sessionRefreshBusy) return;
  state.sessionRefreshBusy = true;
  try {
    if (state.page === "account") {
      state.data.sessions = (await api("/auth/sessions/list", { method: "POST" })).data;
      renderSessionPanelOnly();
    } else if (state.page === "admin" && state.adminMenu === "logins") {
      await refreshAdminLoginManagementData();
      renderAdminLoginManagementOnly();
    }
  } catch (error) {
    if (!isAuthExpiredMessage(error.message)) {
      console.warn("login device refresh failed", error);
    }
  } finally {
    state.sessionRefreshBusy = false;
  }
}

function updateSessionRefreshTimer() {
  if (state.sessionRefreshTimer) {
    window.clearInterval(state.sessionRefreshTimer);
    state.sessionRefreshTimer = null;
  }
  const needsSessionRefresh = state.user && (state.page === "account" || (state.page === "admin" && state.adminMenu === "logins"));
  if (!needsSessionRefresh) return;
  state.sessionRefreshTimer = window.setInterval(refreshSessionsLive, LOGIN_DEVICE_REFRESH_MS);
}

function updateFileJobTimer() {
  if (state.fileJobRefreshTimer) {
    window.clearInterval(state.fileJobRefreshTimer);
    state.fileJobRefreshTimer = null;
  }
  if (!state.user || state.page !== "files" || !fileJobIsActive(state.data.fileJob)) return;
  state.fileJobRefreshTimer = window.setInterval(refreshFileJobLive, FILE_JOB_REFRESH_MS);
}

async function refreshFileJobLive() {
  if (!state.user || state.page !== "files" || state.fileJobRefreshBusy) return;
  state.fileJobRefreshBusy = true;
  try {
    state.data.fileJob = (await api("/files/jobs/latest")).data;
    if (!fileJobIsActive(state.data.fileJob)) {
      renderFileJobProgressOnly();
      await refreshPage();
      render();
      return;
    }
    renderFileJobProgressOnly();
  } catch (error) {
    if (!isAuthExpiredMessage(error.message)) {
      console.warn("file job refresh failed", error);
    }
  } finally {
    state.fileJobRefreshBusy = false;
    updateFileJobTimer();
  }
}

function fileJobIsActive(job) {
  return job && ["pending", "running"].includes(job.state);
}

async function watchCurrentSession() {
  if (!state.user || state.authWatchBusy) return;
  state.authWatchBusy = true;
  try {
    const payload = await api("/auth/me", { method: "POST" });
    state.user = payload.data;
  } catch (error) {
    if (!isAuthExpiredMessage(error.message)) {
      console.warn("auth watch failed", error);
    }
  } finally {
    state.authWatchBusy = false;
  }
}

function updateAuthWatchTimer() {
  if (state.authWatchTimer) {
    window.clearInterval(state.authWatchTimer);
    state.authWatchTimer = null;
  }
  if (!state.user) return;
  state.authWatchTimer = window.setInterval(watchCurrentSession, AUTH_WATCH_MS);
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
      max_speed_mbps: formValue(form, "max_speed_mbps") ? Number(formValue(form, "max_speed_mbps")) : null,
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
      workdir: formValue(form, "workdir") || "/",
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
  await api("/envs/register", {
    method: "POST",
    body: JSON.stringify({
      name: formValue(form, "name"),
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

async function deleteEnv(envId) {
  await api(`/envs/${envId}`, { method: "DELETE" });
  await refreshPage();
}

async function openPath(path) {
  state.data.files.path = path;
  state.data.preview = null;
  state.data.selectedFilePath = "";
  await refreshPage();
}

async function previewFile(path) {
  const payload = await api(`/files/preview?path=${encodeURIComponent(path)}`);
  state.data.preview = payload.data;
  state.data.selectedFilePath = path;
}

function selectFile(path, kind = "") {
  state.data.selectedFilePath = path;
  if (kind === "directory") state.data.preview = null;
  render();
}

async function openParentPath() {
  await openPath(parentPath(state.data.files.path || "/"));
}

async function createFolderFromPrompt() {
  const name = prompt("新建文件夹名称");
  if (!name) return;
  await api("/files/mkdir", { method: "POST", body: JSON.stringify({ path: joinPath(state.data.files.path || "/", name) }) });
  await refreshPage();
}

async function createFileFromPrompt() {
  const name = prompt("新建文件名称");
  if (!name) return;
  const path = joinPath(state.data.files.path || "/", name);
  await api("/files/create", { method: "POST", body: JSON.stringify({ path, content: "" }) });
  await refreshPage();
  await previewFile(path);
}

async function renameSelectedPath() {
  const source = requireSelectedPath();
  const name = prompt("重命名为", baseName(source));
  if (!name || name === baseName(source)) return;
  const target = joinPath(parentPath(source), name);
  await api("/files/rename", { method: "POST", body: JSON.stringify({ path: source, target_path: target }) });
  await refreshPage();
  state.data.selectedFilePath = target;
}

async function copySelectedPath() {
  await openFileTargetPicker("copy");
}

async function moveSelectedPath() {
  await openFileTargetPicker("move");
}

async function archiveSelectedFolder() {
  const source = requireSelectedPath();
  const item = currentSelectedFileItem();
  if (item?.type !== "directory") throw new Error("请选择文件夹");
  const targetPath = joinPath(parentPath(source), `${baseName(source)}_${Date.now()}.zip`);
  const payload = await api("/files/archive", { method: "POST", body: JSON.stringify({ path: source, target_path: targetPath }) });
  state.data.fileJob = payload.data;
  updateFileJobTimer();
}

async function extractSelectedZip() {
  const source = requireSelectedPath();
  if (!isSupportedArchivePath(source)) throw new Error("请选择 zip/tar/tar.gz/tar.bz2/tar.xz 压缩包");
  await openFileTargetPicker("extract");
}

function isSupportedArchivePath(path) {
  const lowered = (path || "").toLowerCase();
  return [".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"].some((suffix) => lowered.endsWith(suffix));
}

async function openFileTargetPicker(mode) {
  const source = requireSelectedPath();
  const startPath = state.data.files.path || parentPath(source);
  state.fileTargetPicker = {
    mode,
    sourcePath: source,
    currentPath: startPath,
    items: [],
  };
  await loadFileTargetPickerPath(startPath);
  render();
}

async function loadFileTargetPickerPath(path) {
  if (!state.fileTargetPicker) return;
  const payload = await api(`/files/list?path=${encodeURIComponent(path || "/")}`);
  state.fileTargetPicker.currentPath = payload.data.path || "/";
  state.fileTargetPicker.items = (payload.data.items || []).filter((item) => item.type === "directory");
}

async function navigateFileTargetPicker(path) {
  await loadFileTargetPickerPath(path);
  render();
}

function closeFileTargetPicker() {
  state.fileTargetPicker = null;
  render();
}

async function confirmFileTargetPicker() {
  const picker = state.fileTargetPicker;
  if (!picker) return;
  if (picker.mode === "extract") {
    const payload = await api("/files/extract", { method: "POST", body: JSON.stringify({ path: picker.sourcePath, target_path: picker.currentPath }) });
    state.data.fileJob = payload.data;
    state.fileTargetPicker = null;
    updateFileJobTimer();
    return;
  }
  const targetPath = buildPickedTargetPath(picker.sourcePath, picker.currentPath, picker.mode, true);
  const endpoint = picker.mode === "copy" ? "/files/copy" : "/files/move";
  await api(endpoint, { method: "POST", body: JSON.stringify({ path: picker.sourcePath, target_path: targetPath }) });
  state.fileTargetPicker = null;
  if (picker.mode === "move") {
    state.data.preview = null;
    state.data.selectedFilePath = targetPath;
  }
  await refreshPage();
}

function buildPickedTargetPath(sourcePath, targetFolder, mode, strict = false) {
  if (mode === "extract") return normalizeClientPath(targetFolder);
  const sourceItem = currentSelectedFileItem();
  const isDirectory = sourceItem?.type === "directory";
  if (isDirectory && isSameOrChildPath(targetFolder, sourcePath)) {
    if (strict) throw new Error("不能选择自身或子目录");
    return "";
  }
  const sameFolder = normalizeClientPath(targetFolder) === normalizeClientPath(parentPath(sourcePath));
  if (mode === "copy" && sameFolder) return suggestCopyPath(sourcePath);
  const targetPath = joinPath(targetFolder, baseName(sourcePath));
  if (mode === "move" && normalizeClientPath(targetPath) === normalizeClientPath(sourcePath)) {
    if (strict) throw new Error("不能移动到原位置");
    return "";
  }
  return targetPath;
}

async function deleteSelectedPath() {
  const source = requireSelectedPath();
  if (!confirm(`确认删除 ${source}？`)) return;
  await api(`/files?path=${encodeURIComponent(source)}`, { method: "DELETE" });
  state.data.preview = null;
  state.data.selectedFilePath = "";
  await refreshPage();
}

async function uploadCurrentFile(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const file = form.elements.file?.files?.[0];
  if (!file) throw new Error("请选择要上传的文件");
  const body = new FormData();
  body.append("path", state.data.files.path || "/");
  body.append("file", file);
  await api("/files/upload", { method: "POST", body });
  form.reset();
  await refreshPage();
}

async function saveCurrentFile(content) {
  const preview = state.data.preview;
  if (!preview?.path || preview.encoding !== "text") throw new Error("当前文件不可保存");
  await api("/files/save", { method: "POST", body: JSON.stringify({ path: preview.path, content }) });
  await previewFile(preview.path);
}

async function downloadSelectedPath() {
  const source = requireSelectedPath();
  const selectedItem = currentSelectedFileItem();
  if (selectedItem?.type === "directory") {
    await archiveSelectedFolder();
    return;
  }
  await downloadPath(source);
}

async function downloadPath(source) {
  const response = await fetch(`${state.apiBase.replace(/\/$/, "")}/files/download?path=${encodeURIComponent(source)}`, {
    headers: {
      Authorization: `Bearer ${state.token}`,
      "X-NG-Device-Id": state.deviceId,
    },
  });
  if (!response.ok) throw new Error("下载失败");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = baseName(source);
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function requireSelectedPath() {
  if (!state.data.selectedFilePath) throw new Error("请先选择文件或目录");
  return state.data.selectedFilePath;
}

function currentSelectedFileItem() {
  const selected = state.data.selectedFilePath;
  return (state.data.files.items || []).find((item) => item.path === selected) || null;
}

function joinPath(directory, name) {
  const safeName = String(name || "").replaceAll("\\", "/").split("/").filter(Boolean).join("/");
  const base = String(directory || "/").replace(/\/+$/, "");
  return `${base || ""}/${safeName}`.replace(/\/+/g, "/");
}

function parentPath(path) {
  const parts = String(path || "/").replace(/\/+$/, "").split("/").filter(Boolean);
  if (parts.length <= 1) return "/";
  return `/${parts.slice(0, -1).join("/")}`;
}

function baseName(path) {
  return String(path || "").replace(/\/+$/, "").split("/").filter(Boolean).pop() || "";
}

function suggestCopyPath(path) {
  const name = baseName(path);
  const index = name.lastIndexOf(".");
  const copyName = index > 0 ? `${name.slice(0, index)}_copy${name.slice(index)}` : `${name}_copy`;
  return joinPath(parentPath(path), copyName);
}

function normalizeClientPath(path) {
  const normalized = `/${String(path || "/").replaceAll("\\", "/").split("/").filter(Boolean).join("/")}`;
  return normalized === "/" ? "/" : normalized.replace(/\/+$/, "");
}

function isSameOrChildPath(path, parent) {
  const normalizedPath = normalizeClientPath(path);
  const normalizedParent = normalizeClientPath(parent);
  return normalizedPath === normalizedParent || normalizedPath.startsWith(`${normalizedParent}/`);
}

function fileTargetActionText(mode) {
  if (mode === "extract") return "解压到";
  return mode === "move" ? "移动到" : "复制到";
}

function fileIcon(item) {
  if (item.type === "directory") return "DIR";
  const ext = baseName(item.path).split(".").pop().toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)) return "IMG";
  if (["mp4", "webm", "mov"].includes(ext)) return "VID";
  if (["mp3", "wav", "flac"].includes(ext)) return "AUD";
  return "TXT";
}

async function submitUser(event, fixedRole = null) {
  event.preventDefault();
  const form = event.currentTarget;
  const role = fixedRole || formValue(form, "role");
  const body = {
    ...(formValue(form, "user_id") ? { user_id: Number(formValue(form, "user_id")) } : {}),
    username: formValue(form, "username"),
    real_name: formValue(form, "real_name"),
    role,
    state: formValue(form, "state") || "enabled",
    password: formValue(form, "password"),
  };
  if (role === "student") body.supervisor_ids = selectedSupervisorIds(form);
  await api("/users/create", { method: "POST", body: JSON.stringify(body) });
  form.reset();
  await refreshPage();
}

async function submitProfile(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = await api("/auth/me/update", {
    method: "POST",
    body: JSON.stringify({ real_name: formValue(form, "real_name") }),
  });
  state.user = payload.data;
  await refreshPage();
}

async function changePassword(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/auth/password/change", {
    method: "POST",
    body: JSON.stringify({
      current_password: formValue(form, "current_password"),
      new_password: formValue(form, "new_password"),
    }),
  });
  form.reset();
}

async function submitUserUpdate(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const userId = formValue(form, "user_id");
  if (!userId) throw new Error("请先填写或选择用户 ID");
  const body = { user_id: Number(userId) };
  const username = formValue(form, "username");
  const realName = formValue(form, "real_name");
  const role = formValue(form, "role");
  const userState = formValue(form, "state");
  if (username) body.username = username;
  if (realName) body.real_name = realName;
  if (role) body.role = role;
  if (userState) body.state = userState;
  if (form.elements.supervisor_ids) body.supervisor_ids = selectedSupervisorIds(form);
  await api("/users/update", { method: "POST", body: JSON.stringify(body) });
  await refreshPage();
}

async function resetUserPassword(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const userId = formValue(form, "user_id");
  const password = formValue(form, "password");
  if (!userId) throw new Error("请先填写或选择用户 ID");
  await api("/users/password/reset", { method: "POST", body: JSON.stringify({ user_id: Number(userId), password }) });
  form.reset();
  await refreshPage();
}

function fillUserEditForm(userId) {
  const user = (state.data.users || []).find((item) => String(item.id) === String(userId));
  const form = document.querySelector("#userEditForm");
  if (!user || !form) return;
  form.elements.user_id.value = user.id;
  if (form.elements.username) form.elements.username.value = user.username || "";
  form.elements.real_name.value = user.real_name || "";
  form.elements.role.value = user.role || "student";
  form.elements.state.value = user.state || "enabled";
  if (form.elements.supervisor_ids) {
    const selected = new Set((user.supervisor_ids || []).map(String));
    Array.from(form.elements.supervisor_ids.options).forEach((option) => {
      option.selected = selected.has(String(option.value));
    });
  }
}

function fillPasswordResetForm(userId) {
  const form = document.querySelector("#passwordResetForm");
  if (!form) return;
  form.elements.user_id.value = userId;
  form.elements.password.focus();
}

function selectedSupervisorIds(form) {
  const field = form.elements.supervisor_ids;
  if (!field) return [];
  const values = Array.from(field.selectedOptions || []).map((option) => Number(option.value)).filter(Boolean);
  return values.slice(0, 2);
}

function enforceSupervisorLimit(select) {
  const selected = Array.from(select.selectedOptions || []);
  if (selected.length <= 2) return;
  selected.slice(2).forEach((option) => { option.selected = false; });
  showToast("每名学生最多只能选择两名导师", "error");
}

function switchTaskZone(zone) {
  state.taskZone = zone;
  localStorage.setItem("ng_task_zone", zone);
  run(refreshPage);
}

function switchAdminMenu(menu) {
  state.adminMenu = menu;
  localStorage.setItem("ng_admin_menu", menu);
  updateRealtimeTimers();
  if (menu === "audit") {
    run(refreshPage);
    return;
  }
  render();
}

function switchAuditCategory(category) {
  state.auditCategory = category || "all";
  state.auditPage = 1;
  localStorage.setItem("ng_audit_category", state.auditCategory);
  run(refreshPage);
}

function switchAuditPage(page) {
  state.auditPage = Math.max(1, Number(page) || 1);
  run(refreshPage);
}

function switchAuditPageSize(pageSize) {
  state.auditPageSize = normalizeAuditPageSize(pageSize);
  state.auditPage = 1;
  localStorage.setItem("ng_audit_page_size", String(state.auditPageSize));
  run(refreshPage);
}

function updateAuditFilters(event) {
  event.preventDefault();
  const form = event.currentTarget;
  state.auditFilters = {
    keyword: formValue(form, "keyword"),
    action: formValue(form, "action"),
    start_time: formValue(form, "start_time"),
    end_time: formValue(form, "end_time"),
  };
  state.auditPage = 1;
  run(refreshPage, "已查询审计日志");
}

function resetAuditFilters() {
  state.auditFilters = { keyword: "", action: "", start_time: "", end_time: "" };
  state.auditPage = 1;
  run(refreshPage, "已重置查询");
}

function jumpAuditPage(event) {
  event.preventDefault();
  const form = event.currentTarget;
  switchAuditPage(formValue(form, "page"));
}

function updateUserFilters(event) {
  event.preventDefault();
  const form = event.currentTarget;
  state.userFilters = {
    user_id: formValue(form, "user_id"),
    keyword: formValue(form, "keyword"),
    role: formValue(form, "role"),
    state: formValue(form, "state"),
  };
  run(refreshPage, "已查询");
}

function resetUserFilters() {
  state.userFilters = { user_id: "", keyword: "", role: "", state: "" };
  run(refreshPage, "已重置查询");
}

async function deleteUser(userId) {
  await api("/users/delete", { method: "POST", body: JSON.stringify({ user_id: Number(userId) }) });
  await refreshPage();
}

async function toggleUserState(userId, nextState) {
  await api("/users/update", { method: "POST", body: JSON.stringify({ user_id: Number(userId), state: nextState }) });
  await refreshPage();
}

async function refreshAdminLoginManagementData() {
  state.data.adminOnlineUsers = (await api("/admin/login-management/online-users", { method: "POST", body: JSON.stringify({}) })).data;
  const query = cleanObject(state.loginFilters);
  state.data.adminUserSessions = Object.keys(query).length
    ? (await api("/admin/login-management/user-sessions", { method: "POST", body: JSON.stringify(query) })).data
    : [];
}

function updateLoginFilters(event) {
  event.preventDefault();
  const form = event.currentTarget;
  state.loginFilters = {
    user_id: formValue(form, "user_id"),
    keyword: formValue(form, "keyword"),
  };
  run(async () => {
    await refreshAdminLoginManagementData();
    render();
  }, "已查询登录情况");
}

function resetLoginFilters() {
  state.loginFilters = { user_id: "", keyword: "" };
  run(async () => {
    await refreshAdminLoginManagementData();
    render();
  }, "已重置查询");
}

function viewUserLoginSessions(userId) {
  state.loginFilters = { user_id: String(userId), keyword: "" };
  run(async () => {
    await refreshAdminLoginManagementData();
    render();
  }, "已载入用户上线情况");
}

async function offlineAdminSession(sessionId) {
  const session = findAdminSession(sessionId);
  await api("/admin/login-management/offline-session", { method: "POST", body: JSON.stringify({ session_id: Number(sessionId) }) });
  if (session?.current) {
    forceLoginRedirect("当前登录设备已下线，请重新登录");
    return;
  }
  await refreshAdminLoginManagementData();
}

function findAdminSession(sessionId) {
  for (const item of state.data.adminUserSessions || []) {
    const found = (item.sessions || []).find((session) => String(session.id) === String(sessionId));
    if (found) return found;
  }
  return null;
}

async function offlineSession(sessionId) {
  const session = (state.data.sessions || []).find((item) => String(item.id) === String(sessionId));
  await api("/auth/sessions/offline", { method: "POST", body: JSON.stringify({ session_id: Number(sessionId) }) });
  if (session?.current) {
    forceLoginRedirect("当前登录设备已下线，请重新登录");
    return;
  }
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
            <p>在线控制台</p>
            <h1>${meta.label}</h1>
          </div>
          <div class="top-actions">
            <button class="secondary" data-action="refresh">刷新</button>
          </div>
        </header>
        ${content}
      </main>
      ${state.drawer ? renderDrawer() : ""}
      ${state.fileTargetPicker ? renderFileTargetPicker() : ""}
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
        ${state.loginError ? `<div class="login-error">${escapeHtml(state.loginError)}</div>` : ""}
        <form method="post" id="loginForm" class="form-stack">
          <label>账号<input name="identity" autocomplete="username" required></label>
          <label>密码<input name="password" type="password" autocomplete="current-password" required></label>
          <button type="submit">登录</button>
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
          <span>master 节点不会显示在这里；这里只展示计算节点的实时快照。${state.lastDashboardRefreshAt ? ` 上次刷新：${formatTime(state.lastDashboardRefreshAt)}` : ""}</span>
        </div>
        <label class="refresh-control">刷新间隔
          <input name="dashboard_refresh_seconds" type="number" min="0" max="3600" step="1" value="${state.autoRefreshSeconds}">
          <span>秒，0 为暂停</span>
        </label>
      </div>
      ${state.data.nodes.length ? `<div class="node-grid">${state.data.nodes.map(renderNodeCard).join("")}</div>` : renderEmpty("暂无计算节点")}
    </section>
  `);
}

function renderNodeCard(node) {
  const gpus = node.gpus || [];
  const bandwidth = speed(node.network_bandwidth_mbps ?? node.max_speed_mbps);
  return `
    <article class="node-card">
      <div class="node-head">
        <div>
          <h3>${escapeHtml(node.name)}</h3>
          <p>可用带宽 ${bandwidth} · ${gpus.length} GPUs</p>
        </div>
        <span class="status ${node.state}">${stateText(node.state)}</span>
      </div>
      <div class="node-stats">
        ${miniMetric("CPU", percent(node.cpu_usage))}
        ${miniMetric("可用内存", formatMb(node.avail_ram_mb))}
        ${miniMetric("上传", speed(node.upload_mbps))}
        ${miniMetric("下载", speed(node.download_mbps))}
      </div>
      ${gpus.length ? `
        <div class="gpu-list">
          ${gpus.map((gpu) => `
          <div class="gpu-row">
            <div class="gpu-title">
              <strong>GPU ${gpu.gpu_index}</strong>
              <span>${escapeHtml(gpu.model || "Unknown")}</span>
            </div>
            <div class="gpu-summary">
              ${percent(gpu.gpu_usage)} · 显存 ${formatMb(vramUsedMb(gpu))} / ${formatMb(gpu.total_vram_mb)} · 调度占用 ${gpu.scheduled_occupied ? "是" : "否"}
            </div>
            <div class="gpu-bars">
              ${metricBar("GPU 使用率", gpu.gpu_usage, percent(gpu.gpu_usage))}
              ${metricBar("显存使用量", vramUsedPercent(gpu), `${formatMb(vramUsedMb(gpu))} / ${formatMb(gpu.total_vram_mb)}`)}
            </div>
          </div>
          `).join("")}
        </div>
      ` : ""}
    </article>
  `;
}

function renderTasks() {
  const tasks = state.data.tasks.items || [];
  const envOptions = state.data.envs.map((env) => `<option value="${env.id}">${escapeHtml(env.name)}</option>`).join("");
  const zones = [
    ["wait", "等待区", "已提交但尚未执行的任务"],
    ["running", "运行区", "正在执行或准备执行的任务"],
    ["history", "历史区", "已完成、失败、取消或调度错误的任务"],
  ];
  return shell(`
    <section class="panel">
      <div class="panel-head"><div><h2>提交训练任务</h2><span>沿用 2.0 的任务入口，但按 3.0 API 保存为结构化任务。</span></div></div>
      <form method="post" id="taskForm" class="form-grid task-form">
        <label class="wide">任务描述<input name="description" placeholder="ResNet 训练 / 参数搜索 / 数据预处理"></label>
        <label>运行环境<select name="env_id"><option value="">不指定</option>${envOptions}</select></label>
        <label>优先级<input name="priority" type="number" min="0" max="100" value="0"></label>
        <label class="wide">工作目录<input name="workdir" value="/"></label>
        <label>GPU 数量<input name="need_gpus" type="number" min="0" max="16" value="1"></label>
        <label>GPU 型号<input name="gpu_types" placeholder="A100,4090"></label>
        <label class="check"><input name="allow_gpu_reuse" type="checkbox">允许复用 GPU</label>
        <label class="check"><input name="on_hold" type="checkbox">先挂起</label>
        <label class="full-row">执行命令<textarea name="command" placeholder="python train.py --config configs/default.yaml" required></textarea></label>
        <div class="form-actions"><button type="submit">提交任务</button></div>
      </form>
    </section>
    <section class="panel task-board">
      <div class="panel-head">
        <div>
          <h2>任务管理</h2>
        </div>
        <div class="task-tabs">
          ${zones.map(([id, label]) => `<button class="secondary ${state.taskZone === id ? "active" : ""}" data-task-zone="${id}">${label}</button>`).join("")}
        </div>
      </div>
      <div class="zone-help">${escapeHtml(zones.find(([id]) => id === state.taskZone)?.[2] || "")}</div>
      ${tasks.length ? renderTaskZoneTable(tasks) : renderEmpty(`${zones.find(([id]) => id === state.taskZone)?.[1] || "当前分区"}暂无任务`)}
    </section>
  `);
}

function renderTaskZoneTable(tasks) {
  return renderTable(["任务", "状态", "资源", "命令", "操作"], tasks.map((task) => [
    `<strong>${escapeHtml(task.task_id)}</strong><br><span class="muted">${escapeHtml(task.description || "无描述")}</span>`,
    `<span class="status ${task.state}">${stateText(task.state)}</span><br><span class="muted">${formatDate(task.created_at)}</span>`,
    `GPU ${task.requirement?.need_gpus ?? 0}<br><span class="muted">${escapeHtml((task.requirement?.gpu_types || []).join(", ") || "不限型号")}</span>`,
    `<code>${escapeHtml(task.command)}</code>`,
    `<button class="small secondary" data-log="${escapeAttr(task.task_id)}">日志</button><button class="small secondary" data-resubmit="${escapeAttr(task.task_id)}">重提</button>${["succeeded", "failed", "cancelled", "alloc_error", "offline_error", "node_lost"].includes(task.state) ? "" : `<button class="small danger" data-cancel="${escapeAttr(task.task_id)}">取消</button>`}`,
  ]));
}

function renderFiles() {
  const files = state.data.files.items || [];
  const currentPath = state.data.files.path || "/";
  const selected = state.data.selectedFilePath;
  const preview = state.data.preview;
  return shell(`
    <section class="file-manager">
      <aside class="file-sidebar-panel">
        <div class="file-nav-row">
          <button class="secondary" data-file-root>根目录</button>
          <button class="secondary" data-file-up>上级</button>
          <button class="secondary" data-action="refresh">刷新</button>
        </div>
        <div class="file-path">${escapeHtml(currentPath)}</div>
        <div class="file-list" role="list">
          ${files.length ? files.map((item) => `
            <div class="file-row ${selected === item.path ? "active" : ""}" data-select-file="${escapeAttr(item.path)}" data-file-kind="${escapeAttr(item.type)}">
              <span class="file-glyph">${fileIcon(item)}</span>
              <span class="file-name">${escapeHtml(item.name)}</span>
              <span class="file-type">${item.type === "directory" ? "dir" : formatBytes(item.size_bytes)}</span>
              ${item.type === "directory" ? `<button class="small secondary file-enter" data-open-path="${escapeAttr(item.path)}">进入</button>` : ""}
            </div>
          `).join("") : `<div class="file-empty">当前目录为空</div>`}
        </div>
        <div class="file-actions-grid">
          <button class="secondary" data-file-new-folder>新建文件夹</button>
          <button class="secondary" data-file-new-file>新建文件</button>
          <button class="secondary" data-file-rename>重命名</button>
          <button class="secondary" data-file-copy>复制到</button>
          <button class="secondary" data-file-move>移动到</button>
          <button class="secondary" data-file-archive>打包成 zip</button>
          <button class="secondary" data-file-extract>解压压缩包</button>
          <button class="danger" data-file-delete>删除</button>
        </div>
        <form id="fileUploadForm" class="file-upload-form">
          <input name="file" type="file">
          <div class="file-transfer-row">
            <button type="submit">上传到当前目录</button>
            <button type="button" class="secondary" data-file-download>下载选中</button>
          </div>
        </form>
      </aside>
      <section class="file-editor-panel">
        <div class="file-editor-toolbar">
          <span class="status">${preview?.path ? escapeHtml(preview.path) : "未打开文件"}</span>
          <button data-file-save ${preview?.encoding === "text" ? "" : "disabled"}>保存</button>
        </div>
        ${renderFilePreview(preview)}
        ${renderFileJobProgress(state.data.fileJob)}
      </section>
    </section>
  `);
}

function renderFilePreview(preview) {
  if (!preview) {
    return `<textarea class="file-editor" disabled placeholder="选择左侧文本文件后可在这里预览或编辑"></textarea>`;
  }
  if (preview.encoding === "text") {
    return `<div class="file-editor-shell"><textarea id="fileEditor" class="file-editor" spellcheck="false">${escapeHtml(preview.content || "")}</textarea>${preview.truncated ? `<p class="file-note">文件较大，仅显示前 ${formatBytes(preview.content.length)}。</p>` : ""}</div>`;
  }
  const source = `data:${preview.content_type};base64,${preview.content}`;
  if (preview.content_type.startsWith("image/")) {
    return `<div class="file-media-preview"><img src="${escapeAttr(source)}" alt="${escapeAttr(baseName(preview.path))}"></div>`;
  }
  if (preview.content_type.startsWith("video/")) {
    return `<div class="file-media-preview"><video controls src="${escapeAttr(source)}"></video></div>`;
  }
  if (preview.content_type.startsWith("audio/")) {
    return `<div class="file-media-preview compact"><audio controls src="${escapeAttr(source)}"></audio></div>`;
  }
  return `<div class="file-binary-preview"><strong>${escapeHtml(baseName(preview.path))}</strong><span>${escapeHtml(preview.content_type)} · ${formatBytes(preview.size_bytes)}</span></div>`;
}

function renderFileJobProgress(job) {
  if (!job) {
    return `<div class="file-job-progress" id="fileJobProgress"><span>暂无打包或解压任务</span></div>`;
  }
  const title = job.action === "extract" ? "解压" : "打包";
  const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
  return `
    <div class="file-job-progress ${job.state}" id="fileJobProgress">
      <div class="file-job-line">
        <strong>${title}：${escapeHtml(job.source_path)}</strong>
        <span>${stateText(job.state)} · ${progress}%</span>
      </div>
      <div class="file-job-bar"><i style="width:${progress}%"></i></div>
      <div class="file-job-detail">
        <span>${escapeHtml(job.current_file || job.message || "等待任务进度")}</span>
        <code>${escapeHtml(job.target_path)}</code>
      </div>
    </div>
  `;
}

function renderFileJobProgressOnly() {
  const node = document.querySelector("#fileJobProgress");
  if (!node || state.page !== "files") return;
  node.outerHTML = renderFileJobProgress(state.data.fileJob);
}

function renderFileTargetPicker() {
  const picker = state.fileTargetPicker;
  const folders = picker.items || [];
  const action = fileTargetActionText(picker.mode);
  const targetPath = buildPickedTargetPath(picker.sourcePath, picker.currentPath, picker.mode);
  const invalidTarget = !targetPath;
  return `
    <div class="modal-backdrop">
      <section class="file-picker-modal" role="dialog" aria-modal="true" aria-labelledby="filePickerTitle">
        <div class="file-picker-head">
          <div>
            <h2 id="filePickerTitle">${action}</h2>
            <span>${escapeHtml(baseName(picker.sourcePath))}</span>
          </div>
          <button class="secondary" data-file-picker-close>关闭</button>
        </div>
        <div class="file-picker-current">
          <span>目标目录</span>
          <strong>${escapeHtml(picker.currentPath)}</strong>
        </div>
        <div class="file-picker-nav">
          <button class="secondary" data-file-picker-root>根目录</button>
          <button class="secondary" data-file-picker-up>上级</button>
        </div>
        <div class="file-picker-list">
          ${folders.length ? folders.map((item) => `
            <button class="file-picker-row" data-file-picker-open="${escapeAttr(item.path)}">
              <span class="file-glyph">DIR</span>
              <span>${escapeHtml(item.name)}</span>
            </button>
          `).join("") : `<div class="file-empty">当前目录下没有子文件夹</div>`}
        </div>
        <div class="file-picker-target">
          <span>将生成</span>
          <code>${escapeHtml(targetPath || "不能选择当前目标目录")}</code>
        </div>
        <div class="file-picker-actions">
          <button class="secondary" data-file-picker-close>取消</button>
          <button data-file-picker-confirm ${invalidTarget ? "disabled" : ""}>选择此文件夹</button>
        </div>
      </section>
    </div>
  `;
}

function renderEnvs() {
  const envs = state.data.envs || [];
  return shell(`
    <section class="panel">
      <div class="panel-head"><div><h2>登记环境</h2><span>当前表单登记已有 conda 环境，后续环境包安装会接入同一页面。</span></div></div>
      <form method="post" id="envForm" class="form-grid">
        <label>环境名称<input name="name" placeholder="torch-cu121" required></label>
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
        `<button class="small secondary" data-test-env="${env.id}">检测</button>${env.can_modify ? `<button class="small danger" data-delete-env="${env.id}">删除</button>` : ""}`,
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
  const sessions = state.data.sessions || [];
  return shell(`
    <section class="split">
      <article class="panel">
        <div class="panel-head"><div><h2>当前账号</h2><span>这里只展示和管理自己的登录身份。</span></div></div>
        <dl class="kv">
          <dt>姓名</dt><dd>${escapeHtml(state.user?.real_name || "-")}</dd>
          <dt>用户名</dt><dd>${escapeHtml(state.user?.username || "-")}</dd>
          <dt>角色</dt><dd>${roleName(state.user?.role)}</dd>
          <dt>状态</dt><dd>${stateText(state.user?.state || "enabled")}</dd>
          <dt>SSH 账户</dt><dd><code>${escapeHtml(accountNameForUser(state.user))}</code></dd>
        </dl>
      </article>
      <article class="panel">
        <div class="panel-head"><div><h2>权限</h2><span>后端 RBAC 会再次校验所有请求。</span></div></div>
        <div class="permission-list">${permissions.map((permission) => `<span>${escapeHtml(permission)}</span>`).join("")}</div>
      </article>
    </section>
    <section class="split">
      <article class="panel">
        <div class="panel-head"><div><h2>修改资料</h2><span>用户名、角色和状态由管理员后台维护。</span></div></div>
        <form method="post" id="profileForm" class="form-grid compact-form">
          <label>姓名<input name="real_name" value="${escapeAttr(state.user?.real_name || "")}" required></label>
          <div class="form-actions"><button type="submit">保存资料</button></div>
        </form>
      </article>
      <article class="panel">
        <div class="panel-head"><div><h2>重设密码</h2><span>修改密码需要先输入当前密码。</span></div></div>
        <form method="post" id="passwordForm" class="form-grid compact-form">
          <label>当前密码<input name="current_password" type="password" required></label>
          <label>新密码<input name="new_password" type="password" minlength="8" required></label>
          <div class="form-actions"><button type="submit">更新密码</button></div>
        </form>
      </article>
    </section>
    ${renderSessionPanel(sessions)}
  `);
}

function renderStudents() {
  const students = (state.data.users || []).filter((user) => user.role === "student");
  return shell(`
    <section class="admin-grid">
      <article class="panel admin-card">
        <div class="panel-head"><div><h2>创建学生账号</h2><span>创建后会自动绑定到当前导师名下。</span></div></div>
        <form method="post" id="studentForm" class="form-grid compact-form">
          <label>统一识别码<input name="user_id" type="number" min="1" placeholder="留空自动分配"></label>
          <label>用户名<input name="username" required></label>
          <label>姓名<input name="real_name" required></label>
          <label>状态<select name="state"><option value="enabled">启用</option><option value="disabled">停用</option></select></label>
          <label>初始密码<input name="password" type="password" minlength="8" required></label>
          <div class="form-actions"><button type="submit">创建学生</button></div>
        </form>
      </article>
      <article class="panel admin-card">
        <div class="panel-head"><div><h2>编辑学生</h2><span>可从列表点“编辑”自动填入，导师只能管理自己名下学生。</span></div></div>
        <form method="post" id="userEditForm" class="form-grid compact-form">
          <label>统一识别码<input name="user_id" type="number" min="1" required></label>
          <label>姓名<input name="real_name" placeholder="留空则不改"></label>
          <label>状态<select name="state"><option value="">不修改</option><option value="enabled">启用</option><option value="disabled">停用</option></select></label>
          <input type="hidden" name="role" value="">
          <div class="form-actions"><button type="submit">保存修改</button></div>
        </form>
        <form method="post" id="passwordResetForm" class="form-grid compact-form reset-form">
          <label>统一识别码<input name="user_id" type="number" min="1" required></label>
          <label>新密码<input name="password" type="password" minlength="8" required></label>
          <div class="form-actions"><button type="submit" class="secondary">重置密码</button></div>
        </form>
      </article>
    </section>
    <section class="panel admin-card">
      <div class="panel-head"><div><h2>学生列表</h2><span>支持按统一识别码、用户名、姓名和状态查询，布局与管理员用户管理保持一致。</span></div></div>
      <form method="post" id="userSearchForm" class="form-grid compact-form search-form">
        <label>统一识别码<input name="user_id" type="number" min="1" value="${escapeAttr(state.userFilters.user_id || "")}" placeholder="精确查询用户 ID"></label>
        <label>关键词<input name="keyword" value="${escapeAttr(state.userFilters.keyword || "")}" placeholder="用户名 / 姓名"></label>
        <label>状态<select name="state">${renderSelectOptions([["", "全部状态"], ["enabled", "启用"], ["disabled", "停用"]], state.userFilters.state)}</select></label>
        <input type="hidden" name="role" value="student">
        <div class="form-actions"><button type="submit">查询</button><button type="button" class="secondary" data-action="reset-user-filters">重置</button></div>
      </form>
      ${students.length ? renderUserTable(students) : renderEmpty("暂无学生账号")}
    </section>
  `);
}

function renderAdmin() {
  const nodes = state.data.nodes || [];
  const users = state.data.users || [];
  const settings = state.data.settings || [];
  const auditItems = state.data.auditLogs.items || [];
  const onlineNodes = nodes.filter((node) => node.state === "online").length;
  const offlineNodes = nodes.filter((node) => ["offline", "manual_offline"].includes(node.state)).length;
  const menus = [
    ["overview", "总览"],
    ["nodes", "节点管理"],
    ["users", "用户管理"],
    ["logins", "登录管理"],
    ["settings", "系统设置"],
    ["audit", "审计日志"],
  ];
  return shell(`
    <section class="admin-head">
      <div class="admin-actions">
        <button data-action="refresh">刷新后台数据</button>
      </div>
    </section>
    <section class="admin-tabs">
      ${menus.map(([id, label]) => `<button class="secondary ${state.adminMenu === id ? "active" : ""}" data-admin-menu="${id}">${label}</button>`).join("")}
    </section>
    <section class="admin-stats">
      ${renderAdminStat("Web 用户", users.length)}
      ${renderAdminStat("在线用户", (state.data.adminOnlineUsers || []).length)}
      ${renderAdminStat("配置节点", nodes.length)}
      ${renderAdminStat("在线节点", onlineNodes)}
      ${renderAdminStat("下线节点", offlineNodes)}
    </section>
    ${renderAdminMenuContent(state.adminMenu, { nodes, users, settings, auditItems, onlineUsers: state.data.adminOnlineUsers || [], userSessions: state.data.adminUserSessions || [] })}
  `);
}

function renderAdminMenuContent(menu, data) {
  const selected = ["overview", "nodes", "users", "logins", "settings", "audit"].includes(menu) ? menu : "overview";
  if (selected === "nodes") return renderAdminNodes(data.nodes);
  if (selected === "users") return renderAdminUsers(data.users);
  if (selected === "logins") return renderAdminLoginManagement(data.onlineUsers, data.userSessions);
  if (selected === "settings") return renderAdminSettings(data.settings);
  if (selected === "audit") return renderAdminAudit(state.data.auditLogs || { items: data.auditItems, total: data.auditItems.length });
  return renderAdminOverview(data);
}

function renderAdminOverview({ nodes, users, auditItems }) {
  const recentAudits = auditItems.slice(0, 6);
  return `
    <section class="admin-grid">
      <article class="panel admin-card">
        <div class="panel-head"><div><h2>运行状态</h2><span>汇总当前后台可管理对象。</span></div></div>
        <dl class="kv">
          <dt>用户</dt><dd>${users.length} 个</dd>
          <dt>在线用户</dt><dd>${(state.data.adminOnlineUsers || []).length} 个</dd>
          <dt>节点</dt><dd>${nodes.length} 个</dd>
          <dt>在线节点</dt><dd>${nodes.filter((node) => node.state === "online").length} 个</dd>
          <dt>审计记录</dt><dd>${auditItems.length} 条</dd>
        </dl>
      </article>
      <article class="panel admin-card">
        <div class="panel-head"><div><h2>最近审计</h2><span>关键管理动作留痕。</span></div></div>
        ${recentAudits.length ? renderTable(["动作", "对象", "时间"], recentAudits.map((item) => [
          escapeHtml(item.action),
          `${escapeHtml(item.target_type)} #${escapeHtml(item.target_id)}`,
          formatDate(item.created_at),
        ])) : renderEmpty("暂无审计日志")}
      </article>
    </section>
  `;
}

function renderAdminNodes(nodes) {
  return `
    <section class="panel admin-card">
      <div class="panel-head"><div><h2>节点管理</h2><span>登记计算节点，重连或下线会写入审计日志。</span></div></div>
      <form method="post" id="nodeForm" class="form-grid">
        <label>节点名称<input name="name" placeholder="node-a" required></label>
        <label>IP 地址<input name="ip" placeholder="192.168.1.21" required></label>
        <label>SSH 用户<input name="ssh_user" value="ddltm"></label>
        <label>网络带宽 Mbps<input name="max_speed_mbps" type="number" min="1" placeholder="10000"></label>
        <label class="wide">GPU 型号列表<input name="gpu_models" placeholder="A100,A100"></label>
        <div class="form-actions"><button type="submit">登记节点</button></div>
      </form>
      ${nodes.length ? renderTable(["节点", "状态", "资源", "操作"], nodes.map((node) => [
        `<strong>${escapeHtml(node.name)}</strong><br><span class="muted">${escapeHtml(node.ip)}</span>`,
        `<span class="status ${node.state}">${stateText(node.state)}</span>`,
        `${(node.gpus || []).length} GPU<br><span class="muted">调度 ${node.scheduling_enabled ? "开启" : "关闭"}</span>`,
        `<button class="small secondary" data-reconnect-node="${node.id}">重连</button><button class="small danger" data-offline-node="${node.id}">下线</button>`,
      ])) : renderEmpty("暂无节点")}
    </section>
  `;
}

function renderAdminUsers(users) {
  return `
    <section class="admin-grid">
      <article class="panel admin-card">
        <div class="panel-head"><div><h2>创建账号</h2><span>管理员用 ddltm SSH，学生和导师创建独立账户。</span></div></div>
        <form method="post" id="userForm" class="form-grid compact-form">
          <label>统一识别码<input name="user_id" type="number" min="1" placeholder="留空自动分配"></label>
          <label>用户名<input name="username" required></label>
          <label>姓名<input name="real_name" required></label>
          <label>角色<select name="role"><option value="student">学生</option><option value="mentor">导师</option><option value="admin">管理员</option><option value="viewer">展示用户</option></select></label>
          <label>导师（学生角色有效，最多两名）<select name="supervisor_ids" multiple size="3">${renderSupervisorOptions()}</select></label>
          <label>状态<select name="state"><option value="enabled">启用</option><option value="disabled">停用</option></select></label>
          <label>初始密码<input name="password" type="password" minlength="8" required></label>
          <div class="form-actions"><button type="submit">创建账号</button></div>
        </form>
      </article>
      <article class="panel admin-card">
        <div class="panel-head"><div><h2>编辑账号</h2><span>可从用户列表点“编辑”自动填入。</span></div></div>
        <form method="post" id="userEditForm" class="form-grid compact-form">
          <label>统一识别码<input name="user_id" type="number" min="1" required></label>
          <label>用户名<input name="username" placeholder="留空则不改"></label>
          <label>姓名<input name="real_name" placeholder="留空则不改"></label>
          <label>角色<select name="role"><option value="">不修改</option><option value="student">学生</option><option value="mentor">导师</option><option value="admin">管理员</option><option value="viewer">展示用户</option></select></label>
          <label>导师（学生角色有效，最多两名）<select name="supervisor_ids" multiple size="3">${renderSupervisorOptions()}</select></label>
          <label>状态<select name="state"><option value="">不修改</option><option value="enabled">启用</option><option value="disabled">停用</option></select></label>
          <div class="form-actions"><button type="submit">保存修改</button></div>
        </form>
        <form method="post" id="passwordResetForm" class="form-grid compact-form reset-form">
          <label>统一识别码<input name="user_id" type="number" min="1" required></label>
          <label>新密码<input name="password" type="password" minlength="8" required></label>
          <div class="form-actions"><button type="submit" class="secondary">重置密码</button></div>
        </form>
      </article>
    </section>
    <section class="panel admin-card">
      <div class="panel-head"><div><h2>用户列表</h2><span>支持按统一识别码、用户名、姓名、角色和状态查询。</span></div></div>
      <form method="post" id="userSearchForm" class="form-grid compact-form search-form">
        <label>统一识别码<input name="user_id" type="number" min="1" value="${escapeAttr(state.userFilters.user_id || "")}" placeholder="精确查询用户 ID"></label>
        <label>关键词<input name="keyword" value="${escapeAttr(state.userFilters.keyword || "")}" placeholder="用户名 / 姓名"></label>
        <label>角色<select name="role">${renderSelectOptions([["", "全部角色"], ["student", "学生"], ["mentor", "导师"], ["admin", "管理员"], ["viewer", "展示用户"]], state.userFilters.role)}</select></label>
        <label>状态<select name="state">${renderSelectOptions([["", "全部状态"], ["enabled", "启用"], ["disabled", "停用"]], state.userFilters.state)}</select></label>
        <div class="form-actions"><button type="submit">查询</button><button type="button" class="secondary" data-action="reset-user-filters">重置</button></div>
      </form>
      ${users.length ? renderUserTable(users) : renderEmpty("暂无账号")}
    </section>
  `;
}

function renderAdminLoginManagement(onlineUsers = [], userSessions = []) {
  return `
    <section class="panel admin-card" id="adminLoginManagementPanel">
      <div class="panel-head"><div><h2>登录管理</h2><span>查看当前在线用户，按用户查询上线 IP、设备、浏览器和最后活跃时间；本面板每 3 秒局部刷新。</span></div></div>
      <div class="login-management-grid">
        <article>
          <h3>当前在线用户</h3>
          <div id="adminOnlineUsersBlock">${renderAdminOnlineUsersBlock(onlineUsers)}</div>
        </article>
        <article>
          <h3>查询用户上线情况</h3>
          <form method="post" id="loginSearchForm" class="form-grid compact-form search-form">
            <label>统一识别码<input name="user_id" type="number" min="1" value="${escapeAttr(state.loginFilters.user_id || "")}" placeholder="精确查询用户 ID"></label>
            <label>关键词<input name="keyword" value="${escapeAttr(state.loginFilters.keyword || "")}" placeholder="用户名 / 姓名"></label>
            <div class="form-actions"><button type="submit">查询</button><button type="button" class="secondary" data-action="reset-login-filters">重置</button></div>
          </form>
          <div id="adminUserSessionsBlock">${renderAdminUserSessions(userSessions)}</div>
        </article>
      </div>
    </section>
  `;
}

function renderAdminOnlineUsersBlock(onlineUsers = []) {
  return onlineUsers.length ? renderOnlineUsersTable(onlineUsers) : renderEmpty("暂无在线用户");
}

function renderOnlineUsersTable(onlineUsers) {
  return renderTable(["用户", "角色", "在线设备", "IP / 设备", "最后活跃", "操作"], onlineUsers.map((user) => [
    `<strong>#${escapeHtml(user.id)} ${escapeHtml(user.real_name)}</strong><br><span class="muted">${escapeHtml(user.username)} · ${stateText(user.state)}</span>`,
    roleName(user.role),
    `${escapeHtml(user.online_sessions || 0)} 台`,
    `<span class="muted">IP：${escapeHtml((user.login_ips || []).join("、") || "-")}</span><br><span class="muted">设备：${escapeHtml((user.login_devices || []).join("、") || "-")}</span>`,
    formatDate(user.last_seen_at),
    `<button class="small secondary" data-view-login-user="${user.id}">查看上线情况</button>`,
  ]));
}

function renderAdminUserSessions(items = []) {
  if (!state.loginFilters.user_id && !state.loginFilters.keyword) {
    return renderEmpty("请选择在线用户或输入统一识别码 / 关键词后查询");
  }
  if (!items.length) return renderEmpty("未找到匹配用户或该用户暂无登录记录");
  return items.map((item) => `
    <section class="sub-panel">
      <div class="panel-head compact"><div><h3>#${escapeHtml(item.id)} ${escapeHtml(item.real_name)}</h3><span>${escapeHtml(item.username)} · ${roleName(item.role)} · ${stateText(item.state)}</span></div></div>
      ${(item.sessions || []).length ? renderAdminSessionTable(item.sessions) : renderEmpty("该用户暂无登录记录")}
    </section>
  `).join("");
}

function renderAdminSessionTable(sessions) {
  return renderTable(["设备", "IP", "登录时间", "最后活跃", "状态", "操作"], sessions.map((session) => [
    `<strong>${escapeHtml(session.login_device || "unknown device")}</strong><br><span class="muted">${escapeHtml(session.user_agent || "-")}</span>`,
    escapeHtml(session.login_ip || "-"),
    formatDate(session.login_time),
    formatDate(session.last_seen_at),
    `<span class="status ${session.state === "online" ? "online" : "offline"}">${session.current ? "当前会话 · " : ""}${stateText(session.state)}</span>`,
    session.state === "online" ? `<button class="small danger" data-admin-offline-session="${session.id}">下线</button>` : "-",
  ]));
}

function renderAdminLoginManagementOnly() {
  if (state.page !== "admin" || state.adminMenu !== "logins") return;
  const onlineBlock = document.querySelector("#adminOnlineUsersBlock");
  if (onlineBlock) onlineBlock.innerHTML = renderAdminOnlineUsersBlock(state.data.adminOnlineUsers || []);
  const sessionsBlock = document.querySelector("#adminUserSessionsBlock");
  if (sessionsBlock) sessionsBlock.innerHTML = renderAdminUserSessions(state.data.adminUserSessions || []);
  bindAdminLoginEvents();
}

function renderAdminSettings(settings) {
  return `
    <section class="panel admin-card">
      <div class="panel-head"><div><h2>系统设置</h2><span>保存后立即写入后端设置存储。</span></div></div>
      <form method="post" id="settingForm" class="form-grid compact-form">
        <label>键<input name="key" placeholder="scheduler.enabled" required></label>
        <label>值<input name="value" placeholder="true" required></label>
        <div class="form-actions"><button type="submit">保存</button></div>
      </form>
      ${settings.length ? renderTable(["键", "值"], settings.map((item) => [escapeHtml(item.key), `<code>${escapeHtml(item.value)}</code>`])) : renderEmpty("暂无设置")}
    </section>
  `;
}

function renderAdminAudit(auditLogs) {
  const auditItems = auditLogs.items || [];
  const category = state.auditCategory || "all";
  const page = auditLogs.page || state.auditPage || 1;
  const pageSize = normalizeAuditPageSize(auditLogs.page_size || state.auditPageSize);
  const totalPages = Math.max(1, Math.ceil((auditLogs.total || 0) / pageSize));
  const filters = state.auditFilters || {};
  return `
    <section class="panel admin-card">
      <div class="panel-head"><div><h2>审计日志</h2><span>可按操作类型分类查看。</span></div></div>
      <section class="admin-tabs compact-tabs">
        ${auditCategories().map(([id, label]) => `<button class="secondary ${category === id ? "active" : ""}" data-audit-category="${id}">${label}</button>`).join("")}
      </section>
      <form method="post" id="auditSearchForm" class="form-grid compact-form search-form audit-search">
        <label class="wide">搜索<input name="keyword" value="${escapeAttr(filters.keyword || "")}" placeholder="动作 / 操作对象 / 操作者 (统一识别码, 用户名, 姓名) / 结果 / 详情"></label>
        <label>动作<input name="action" value="${escapeAttr(filters.action || "")}" placeholder="例如 user.update"></label>
        <br>
        <label>开始时间<input name="start_time" type="datetime-local" value="${escapeAttr(filters.start_time || "")}"></label>
        <label>结束时间<input name="end_time" type="datetime-local" value="${escapeAttr(filters.end_time || "")}"></label>
        <div class="form-actions"><button type="submit">查询</button><button type="button" class="secondary" data-action="reset-audit-filters">重置</button></div>
      </form>
      <div class="audit-summary">
        <p class="muted">当前分类 ${escapeHtml(auditCategoryName(category))}，共 ${escapeHtml(auditLogs.total || 0)} 条，第 ${escapeHtml(page)} / ${escapeHtml(totalPages)} 页。</p>
        <label class="page-size-control">每页
          <select name="audit_page_size">${[10, 20, 50, 100].map((size) => `<option value="${size}" ${size === pageSize ? "selected" : ""}>${size} 条</option>`).join("")}</select>
        </label>
      </div>
      ${auditItems.length ? renderTable(["分类", "动作", "对象", "操作者", "结果", "时间", "详情"], auditItems.map((item) => [
        escapeHtml(auditCategoryName(item.category || auditCategoryOf(item))),
        escapeHtml(item.action),
        `${escapeHtml(item.target_type)} #${escapeHtml(item.target_id)}`,
        item.actor_user_id ? `#${escapeHtml(item.actor_user_id)}` : "-",
        escapeHtml(item.result || "-"),
        formatDate(item.created_at),
        `<span class="muted">${escapeHtml(auditDetailText(item))}</span>`,
      ])) : renderEmpty("暂无审计日志")}
      <div class="pagination-controls">
        <button class="secondary" data-audit-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
        <form method="post" id="auditPageJumpForm" class="pager-jump">
          <label>跳转到<input name="page" type="number" min="1" max="${totalPages}" value="${escapeAttr(page)}"></label>
          <button type="submit" class="secondary">跳转</button>
        </form>
        <button class="secondary" data-audit-page="${page + 1}" ${page >= totalPages ? "disabled" : ""}>下一页</button>
      </div>
    </section>
  `;
}

function auditCategories() {
  return [
    ["all", "全部"],
    ["system", "系统操作"],
    ["user", "用户操作"],
    ["archive", "压缩文件"],
    ["file", "文件操作"],
    ["task", "任务操作"],
    ["env", "环境操作"],
    ["node", "节点操作"],
    ["other", "其他"],
  ];
}

function auditCategoryName(category) {
  return Object.fromEntries(auditCategories())[category] || "其他";
}

function auditCategoryOf(item) {
  const action = item.action || "";
  const targetType = item.target_type || "";
  if (targetType === "system" || targetType === "settings" || action.startsWith("settings.")) return "system";
  if (targetType === "user" || targetType === "login_session" || action.startsWith("user.") || action.startsWith("auth.")) return "user";
  if (["file.archive", "file.extract"].includes(action)) return "archive";
  if (targetType === "file") return "file";
  if (targetType === "task") return "task";
  if (["env", "env_package", "env_install_job"].includes(targetType) || action.startsWith("env.")) return "env";
  if (targetType === "node") return "node";
  return "other";
}

function auditDetailText(item) {
  const detail = item.detail_json || {};
  const pairs = Object.entries(detail).filter(([, value]) => value !== null && value !== undefined && value !== "");
  if (!pairs.length) return "-";
  return pairs.slice(0, 3).map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`).join("；");
}

function renderUserTable(users) {
  return renderTable(["统一识别码", "账号", "角色", "状态", "导师", "Linux", "Home", "操作"], users.map((user) => {
    const nextState = user.state === "enabled" ? "disabled" : "enabled";
    const toggleText = user.state === "enabled" ? "停用" : "启用";
    const supervisorText = user.role === "student" ? ((user.supervisor_names || []).length ? user.supervisor_names.map(escapeHtml).join("、") : "未绑定") : "-";
    const actions = [
      can("users:read") ? `<button class="small secondary" data-edit-user="${user.id}">编辑</button>` : "",
      can("users:read") ? `<button class="small secondary" data-reset-user="${user.id}">重置密码</button>` : "",
      can("users:read") ? `<button class="small ${user.state === "enabled" ? "danger" : "secondary"}" data-toggle-user="${user.id}" data-next-state="${nextState}">${toggleText}</button>` : "",
      can("users:delete") ? `<button class="small danger" data-delete-user="${user.id}">删除</button>` : "",
    ].filter(Boolean).join("");
    return [
      `<strong>#${escapeHtml(user.id)}</strong>`,
      `<strong>${escapeHtml(user.real_name)}</strong><br><span class="muted">${escapeHtml(user.username)}</span>`,
      roleName(user.role),
      stateText(user.state),
      supervisorText,
      `<code>${escapeHtml(user.linux_account_name || "-")}</code>`,
      `<code>${escapeHtml(user.home_path || "-")}</code>`,
      actions || "-",
    ];
  }));
}


function renderSessionTable(sessions) {
  return renderTable(["设备", "IP", "登录时间", "最后活跃", "状态", "操作"], sessions.map((session) => [
    `<strong>${escapeHtml(session.login_device || "unknown device")}</strong><br><span class="muted">${escapeHtml(session.user_agent || "-")}</span>`,
    escapeHtml(session.login_ip || "-"),
    formatDate(session.login_time),
    formatDate(session.last_seen_at),
    `<span class="status ${session.state === "online" ? "online" : "offline"}">${session.current ? "当前会话 · " : ""}${stateText(session.state)}</span>`,
    session.state === "online" ? `<button class="small danger" data-offline-session="${session.id}">下线</button>` : "-",
  ]));
}

function renderSessionPanel(sessions = []) {
  return `
    <section class="panel" id="loginSessionsPanel">
      <div class="panel-head"><div><h2>登录设备</h2><span>按设备/IP/浏览器合并显示登录状态，每 3 秒自动刷新；发现异常设备时可手动下线。</span></div></div>
      ${sessions.length ? renderSessionTable(sessions) : renderEmpty("暂无登录记录")}
    </section>
  `;
}

function renderSessionPanelOnly() {
  const panel = document.querySelector("#loginSessionsPanel");
  if (!panel || state.page !== "account") return;
  panel.outerHTML = renderSessionPanel(state.data.sessions || []);
  bindSessionEvents();
}

function renderSupervisorOptions(selected = []) {
  const selectedSet = new Set((selected || []).map(String));
  const mentors = (state.data.mentors || []).filter((user) => user.role === "mentor");
  return mentors.length
    ? mentors.map((mentor) => `<option value="${escapeAttr(mentor.id)}" ${selectedSet.has(String(mentor.id)) ? "selected" : ""}>#${escapeHtml(mentor.id)} ${escapeHtml(mentor.real_name)}（${escapeHtml(mentor.username)}）</option>`).join("")
    : `<option disabled>暂无导师账号</option>`;
}

function renderSelectOptions(options, selected) {
  return options.map(([value, label]) => `<option value="${escapeAttr(value)}" ${String(value) === String(selected || "") ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
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

function metricBar(label, value, text) {
  const safe = value === null || value === undefined ? null : Math.max(0, Math.min(100, Number(value) || 0));
  return `
    <label class="metric-bar">
      <span>${label}</span><b>${text}</b>
      <i><em style="width:${safe || 0}%"></em></i>
    </label>
  `;
}

function vramUsedMb(gpu) {
  if (!gpu?.total_vram_mb || gpu.free_vram_mb === null || gpu.free_vram_mb === undefined) return null;
  return Math.max(0, gpu.total_vram_mb - gpu.free_vram_mb);
}

function vramUsedPercent(gpu) {
  const used = vramUsedMb(gpu);
  if (used === null || !gpu?.total_vram_mb) return null;
  return Math.round((used / gpu.total_vram_mb) * 100);
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

function accountNameForUser(user) {
  if (!user) return "-";
  return user.linux_account_name || (user.role === "admin" ? "ddltm" : user.username);
}

function renderAdminStat(label, value) {
  return `<article><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></article>`;
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
    alloc_error: "调度错误",
    offline_error: "节点掉线",
    node_lost: "节点丢失",
    preparing: "准备中",
    dispatching: "派发中",
    available: "可用",
    registered: "已登记",
    enabled: "启用",
    disabled: "停用",
    pending: "等待中",
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

function formatTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
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
  document.querySelector("#profileForm")?.addEventListener("submit", (event) => run(() => submitProfile(event), "资料已保存"));
  document.querySelector("#passwordForm")?.addEventListener("submit", (event) => run(() => changePassword(event), "密码已更新"));
  document.querySelector("#userForm")?.addEventListener("submit", (event) => run(() => submitUser(event), "账号已创建"));
  document.querySelector("#studentForm")?.addEventListener("submit", (event) => run(() => submitUser(event, "student"), "学生已创建"));
  document.querySelector("#settingForm")?.addEventListener("submit", (event) => run(() => updateSetting(event), "设置已保存"));
  document.querySelector("#userEditForm")?.addEventListener("submit", (event) => run(() => submitUserUpdate(event), "账号已更新"));
  document.querySelector("#passwordResetForm")?.addEventListener("submit", (event) => run(() => resetUserPassword(event), "密码已重置"));
  document.querySelector("#userSearchForm")?.addEventListener("submit", (event) => updateUserFilters(event));
  document.querySelector("[data-action='reset-user-filters']")?.addEventListener("click", () => resetUserFilters());
  bindAdminLoginEvents();
  document.querySelector("#fileForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    run(() => openPath(formValue(event.currentTarget, "path")));
  });
  document.querySelector("#fileUploadForm")?.addEventListener("submit", (event) => run(() => uploadCurrentFile(event), "文件已上传"));
  document.querySelector("[data-file-root]")?.addEventListener("click", () => run(() => openPath("/")));
  document.querySelector("[data-file-up]")?.addEventListener("click", () => run(openParentPath));
  document.querySelector("[data-file-new-folder]")?.addEventListener("click", () => run(createFolderFromPrompt, "文件夹已创建"));
  document.querySelector("[data-file-new-file]")?.addEventListener("click", () => run(createFileFromPrompt, "文件已创建"));
  document.querySelector("[data-file-rename]")?.addEventListener("click", () => run(renameSelectedPath, "已重命名"));
  document.querySelector("[data-file-copy]")?.addEventListener("click", () => run(copySelectedPath));
  document.querySelector("[data-file-move]")?.addEventListener("click", () => run(moveSelectedPath));
  document.querySelector("[data-file-archive]")?.addEventListener("click", () => run(archiveSelectedFolder, "已开始打包"));
  document.querySelector("[data-file-extract]")?.addEventListener("click", () => run(extractSelectedZip));
  document.querySelector("[data-file-delete]")?.addEventListener("click", () => run(deleteSelectedPath, "已删除"));
  document.querySelector("[data-file-download]")?.addEventListener("click", () => run(downloadSelectedPath));
  document.querySelector("[data-file-save]")?.addEventListener("click", () => {
    const content = document.querySelector("#fileEditor")?.value ?? "";
    run(() => saveCurrentFile(content), "文件已保存");
  });
  document.querySelectorAll("[data-select-file]").forEach((button) => {
    button.addEventListener("click", () => {
      const path = button.dataset.selectFile;
      const kind = button.dataset.fileKind;
      if (kind === "directory") selectFile(path, kind);
      else run(() => previewFile(path));
    });
    button.addEventListener("dblclick", () => {
      const path = button.dataset.selectFile;
      const kind = button.dataset.fileKind;
      run(() => (kind === "directory" ? openPath(path) : previewFile(path)));
    });
  });
  document.querySelectorAll("[data-file-picker-close]").forEach((button) => button.addEventListener("click", closeFileTargetPicker));
  document.querySelector("[data-file-picker-root]")?.addEventListener("click", () => run(() => navigateFileTargetPicker("/")));
  document.querySelector("[data-file-picker-up]")?.addEventListener("click", () => run(() => navigateFileTargetPicker(parentPath(state.fileTargetPicker?.currentPath || "/"))));
  document.querySelector("[data-file-picker-confirm]")?.addEventListener("click", () => {
    const successText = state.fileTargetPicker?.mode === "move" ? "已移动" : (state.fileTargetPicker?.mode === "extract" ? "已开始解压" : "已复制");
    run(confirmFileTargetPicker, successText);
  });
  document.querySelectorAll("[data-file-picker-open]").forEach((button) => {
    button.addEventListener("click", () => run(() => navigateFileTargetPicker(button.dataset.filePickerOpen)));
  });
  document.querySelectorAll("[data-nav]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.nav)));
  document.querySelector("[data-action='logout']")?.addEventListener("click", () => run(logout));
  document.querySelectorAll("[data-action='refresh']").forEach((button) => button.addEventListener("click", () => run(refreshPage, "已刷新")));
  document.querySelector("[name='dashboard_refresh_seconds']")?.addEventListener("change", (event) => setDashboardRefreshSeconds(event.currentTarget.value));
  document.querySelector("[data-action='close-drawer']")?.addEventListener("click", () => {
    state.drawer = null;
    render();
  });
  document.querySelectorAll("[data-task-zone]").forEach((button) => button.addEventListener("click", () => switchTaskZone(button.dataset.taskZone)));
  document.querySelectorAll("[data-admin-menu]").forEach((button) => button.addEventListener("click", () => switchAdminMenu(button.dataset.adminMenu)));
  document.querySelectorAll("[data-audit-category]").forEach((button) => button.addEventListener("click", () => switchAuditCategory(button.dataset.auditCategory)));
  document.querySelectorAll("[data-audit-page]").forEach((button) => button.addEventListener("click", () => switchAuditPage(button.dataset.auditPage)));
  document.querySelector("#auditSearchForm")?.addEventListener("submit", (event) => updateAuditFilters(event));
  document.querySelector("[data-action='reset-audit-filters']")?.addEventListener("click", () => resetAuditFilters());
  document.querySelector("#auditPageJumpForm")?.addEventListener("submit", (event) => jumpAuditPage(event));
  document.querySelector("[name='audit_page_size']")?.addEventListener("change", (event) => switchAuditPageSize(event.currentTarget.value));
  document.querySelectorAll("[data-cancel]").forEach((button) => button.addEventListener("click", () => run(() => cancelTask(button.dataset.cancel), "任务已取消")));
  document.querySelectorAll("[data-resubmit]").forEach((button) => button.addEventListener("click", () => run(() => resubmitTask(button.dataset.resubmit), "任务已重新提交")));
  document.querySelectorAll("[data-log]").forEach((button) => button.addEventListener("click", () => run(() => showTaskLog(button.dataset.log))));
  document.querySelectorAll("[data-open-path]").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    run(() => openPath(button.dataset.openPath));
  }));
  document.querySelectorAll("[data-preview]").forEach((button) => button.addEventListener("click", () => run(() => previewFile(button.dataset.preview))));
  document.querySelectorAll("[data-test-env]").forEach((button) => button.addEventListener("click", () => run(() => testEnv(button.dataset.testEnv))));
  document.querySelectorAll("[data-delete-env]").forEach((button) => button.addEventListener("click", () => run(() => deleteEnv(button.dataset.deleteEnv), "环境已删除")));
  document.querySelectorAll("[data-edit-user]").forEach((button) => button.addEventListener("click", () => fillUserEditForm(button.dataset.editUser)));
  document.querySelectorAll("[data-reset-user]").forEach((button) => button.addEventListener("click", () => fillPasswordResetForm(button.dataset.resetUser)));
  document.querySelectorAll("[data-delete-user]").forEach((button) => button.addEventListener("click", () => run(() => deleteUser(button.dataset.deleteUser), "账号已删除")));
  document.querySelectorAll("[data-toggle-user]").forEach((button) => button.addEventListener("click", () => run(() => toggleUserState(button.dataset.toggleUser, button.dataset.nextState), button.dataset.nextState === "enabled" ? "账号已启用" : "账号已停用")));
  document.querySelectorAll("select[name='supervisor_ids']").forEach((select) => select.addEventListener("change", () => enforceSupervisorLimit(select)));
  bindSessionEvents();
  document.querySelectorAll("[data-reconnect-node]").forEach((button) => button.addEventListener("click", () => run(() => reconnectNode(button.dataset.reconnectNode), "已提交重连")));
  document.querySelectorAll("[data-offline-node]").forEach((button) => button.addEventListener("click", () => run(() => forceOfflineNode(button.dataset.offlineNode), "已强制下线")));
}

function bindAdminLoginEvents() {
  document.querySelector("#loginSearchForm")?.addEventListener("submit", (event) => updateLoginFilters(event));
  document.querySelector("[data-action='reset-login-filters']")?.addEventListener("click", () => resetLoginFilters());
  document.querySelectorAll("[data-view-login-user]").forEach((button) => button.addEventListener("click", () => viewUserLoginSessions(button.dataset.viewLoginUser)));
  document.querySelectorAll("[data-admin-offline-session]").forEach((button) => button.addEventListener("click", () => run(() => offlineAdminSession(button.dataset.adminOfflineSession), "设备已下线")));
}

function bindSessionEvents() {
  document.querySelectorAll("[data-offline-session]").forEach((button) => button.addEventListener("click", () => run(() => offlineSession(button.dataset.offlineSession), "设备已下线")));
}

window.addEventListener("hashchange", () => {
  const page = location.hash.replace("#/", "") || "dashboard";
  if (pages.some((item) => item.id === page)) {
    state.page = page;
    updateRealtimeTimers();
    run(refreshPage);
  }
});

loadMe().then(refreshPage).catch(() => null).finally(() => {
  updateRealtimeTimers();
  render();
});
