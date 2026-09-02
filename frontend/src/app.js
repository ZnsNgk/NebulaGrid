const state = {
  apiBase: localStorage.getItem("ng_api_base") || `${location.origin}/api`,
  token: localStorage.getItem("ng_token") || "",
  deviceId: getOrCreateDeviceId(),
  user: null,
  page: location.hash.replace("#/", "") || "dashboard",
  taskZone: normalizeTaskZone(localStorage.getItem("ng_task_zone") || "wait"),
  selectedTaskId: "",
  taskHistoryAllLoaded: false,
  taskPredecessorOptions: [],
  taskPredecessorLoading: false,
  taskPredecessorRequestKey: "",
  taskListLoading: false,
  taskRequestSeq: 0,
  taskRealtimeTimer: null,
  taskRealtimeZone: "",
  taskRealtimePendingZone: "",
  taskZoneCursors: {},
  taskFormDraft: null,
  taskEventsSource: null,
  taskRealtimeBusy: false,
  taskLog: {
    taskId: "",
    text: "",
    command: "",
    refreshSeconds: Number(localStorage.getItem("ng_task_log_refresh_seconds") || 2),
    paused: false,
    busy: false,
    timer: null,
    lastRefreshAt: null,
    error: "",
  },
  adminMenu: localStorage.getItem("ng_admin_menu") || "overview",
  adminSettingKey: "",
  adminNodeEditId: null,
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
  presenterRefreshSeconds: Number(localStorage.getItem("ng_presenter_refresh_seconds") || 5),
  presenterHistoryHours: Number(localStorage.getItem("ng_presenter_history_hours") || 1),
  autoRefreshTimer: null,
  autoRefreshBusy: false,
  sessionRefreshTimer: null,
  sessionRefreshBusy: false,
  fileJobRefreshTimer: null,
  fileJobRefreshBusy: false,
  envRefreshTimer: null,
  envRefreshBusy: false,
  authWatchTimer: null,
  authWatchBusy: false,
  lastDashboardRefreshAt: null,
  lastPresenterRefreshAt: null,
  drawer: null,
  fileTargetPicker: null,
  envCompilePicker: null,
  envPackageInstall: null,
  envPackageDelete: null,
  fileViewScope: "own",
  data: {
    dashboard: null,
    presenter: null,
    nodes: [],
    tasks: { items: [], total: 0, page: 1, page_size: 100 },
    taskZones: {
      wait: { items: [], total: 0, page: 1, page_size: 100 },
      running: { items: [], total: 0, page: 1, page_size: 100 },
      history: { items: [], total: 0, page: 1, page_size: 100 },
    },
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
    nodeOwnerUsers: [],
    runtimeConfig: { shared_folder_root: "/home/ddltm/shared" },
  },
};

const LOGIN_DEVICE_REFRESH_MS = 3000;
const AUTH_WATCH_MS = 3000;
const FILE_JOB_REFRESH_MS = 2000;
const TASK_LOG_TAIL_BYTES = "200KB";
const TASK_LOG_TAIL_LINES = 200;
const PRESENTER_HISTORY_OPTIONS = [1, 3, 6, 12, 24];
const gatewayErrorStatuses = new Set([502, 503, 504]);

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
  "samba account is unavailable": "当前账号不支持 Samba 服务",
  "samba enable requires current password": "开启 Samba 需要当前密码",
  "samba account command failed": "Samba 账号命令执行失败",
  "samba account command unavailable": "Samba 账号命令不可用",
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
  "student file scope requires mentor role": "只有导师可以查看学生文件",
  "mentor can only view assigned student files": "导师只能查看自己名下学生的文件",
  "path is outside shared folder": "路径超出共享文件夹",
  "mentor can only reset assigned student passwords": "导师只能重置自己名下学生的密码",
  "permission required: admin:login:read": "只有管理员可以查看登录管理",
  "permission required: admin:login:write": "只有管理员可以下线登录设备",
  "session not found": "登录设备不存在或已失效",
  "target already exists": "目标已存在",
  "path not found": "路径不存在",
  "path is outside assigned student home": "路径超出学生文件目录",
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
  "package name is required": "请选择需要删除的包",
  "package not found": "包不存在，请刷新后重试",
  "protected package cannot be deleted": "环境默认包不能删除",
  "environment package operation is running": "该环境正在进行操作，请稍后",
  "node name already exists": "节点名称已存在",
  "node owner not found": "节点所有人不存在，请刷新用户列表后重试",
  "gpu_count must match gpu_models length": "GPU 数量必须与 GPU 型号列表条数一致",
  "gpu schedulable flags must be 0 or 1": "GPU 可用性列表只能填写 0 或 1",
  "command must be a single line": "单个任务的执行命令不允许换行",
  "batch commands are empty": "批量命令中没有可提交的有效行",
  "running task cannot be edited": "运行中的任务不能修改",
  "running task cannot be deleted": "运行中的任务不能删除",
  "only waiting task can be held": "只有等待区任务可以挂起",
  "only waiting or held task can toggle hold": "只有等待区或挂起任务可以切换挂起状态",
  "predecessor task not found": "前驱任务不存在或不可见",
};

const roleLabels = {
  student: "学生",
  mentor: "导师",
  admin: "管理员",
  viewer: "展示用户",
};

const pages = [
  { id: "dashboard", label: "总览", icon: "📊", permission: "dashboard:read" },
  { id: "tasks", label: "任务管理", icon: "✅", permission: "tasks:read" },
  { id: "files", label: "文件管理", icon: "📁", permission: "files:read" },
  { id: "envs", label: "环境管理", icon: "🧪", permission: "envs:read" },
  { id: "manual", label: "使用手册", icon: "📖" },
  { id: "account", label: "账号管理", icon: "👤" },
  { id: "students", label: "学生管理", icon: "🎓", roles: ["mentor"], permission: "users:read" },
  { id: "admin", label: "管理员后台", icon: "⚙️", roles: ["admin"], permission: "admin:settings:read" },
];

const protectedEnvPackageNames = new Set([
  "python",
  "pip",
  "setuptools",
  "wheel",
  "conda",
  "conda-package-handling",
  "conda-package-streaming",
  "openssl",
  "sqlite",
  "tk",
  "xz",
  "zlib",
  "libffi",
  "ncurses",
  "readline",
  "ca-certificates",
  "certifi",
]);

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
  if (isPresenterUser()) return [];
  return pages.filter((page) => hasRole(page.roles) && can(page.permission));
}

function currentPageMeta() {
  return pages.find((page) => page.id === state.page) || pages[0];
}

function ensureVisiblePage() {
  if (!state.user) return;
  if (isPresenterUser()) {
    state.page = "presenter";
    if (location.hash !== "#/presenter") {
      history.replaceState(null, "", "#/presenter");
    }
    return;
  }
  const visible = visiblePages();
  if (!visible.some((page) => page.id === state.page)) {
    state.page = visible[0]?.id || "dashboard";
    location.hash = `/${state.page}`;
  }
}

function isPresenterUser() {
  return state.user?.role === "viewer";
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
  let response;
  try {
    response = await fetch(`${state.apiBase.replace(/\/$/, "")}${path}`, { ...options, headers });
  } catch (error) {
    throw new Error(normalizeNetworkError(error));
  }
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
  if (gatewayErrorStatuses.has(status)) {
    return "后端 API 正在启动或暂时不可达，请稍后再登录；如果持续出现，请检查 nebulagrid-api 服务和 nginx 反向代理。";
  }
  if (status === 413) {
    return "上传文件过大：请通过scp上传。";
  }
  if (isHtmlErrorPage(message)) {
    return `后端服务返回 HTTP ${status}，请稍后重试或检查服务日志。`;
  }
  return normalizeErrorMessage(message);
}

function normalizeErrorMessage(message) {
  const text = String(message || "操作失败").trim();
  if (isNetworkErrorText(text)) {
    return "无法连接后端 API，请确认服务已启动且 API 地址正确。";
  }
  if (isHtmlErrorPage(text)) {
    return "后端服务暂时不可用，请稍后重试或检查 nginx/API 服务状态。";
  }
  if (text.includes("413 Request Entity Too Large")) {
    return "上传文件过大：请通过scp上传。";
  }
  return errorMessageMap[text] || text;
}

function normalizeNetworkError(error) {
  // 浏览器在 API 进程未监听端口、地址填错或跨域被拦截时只暴露 TypeError，这里统一给出可排查的中文提示。
  const message = error?.message || "";
  if (isNetworkErrorText(message)) {
    return "无法连接后端 API，请确认服务已启动且 API 地址正确。";
  }
  return normalizeErrorMessage(message);
}

function isNetworkErrorText(message) {
  return /failed to fetch|networkerror|load failed|fetch/i.test(String(message || ""));
}

function isHtmlErrorPage(message) {
  // nginx 的 502/503 默认响应是 HTML；登录页只需要展示故障含义，不应暴露整页网关错误源码。
  const text = String(message || "").trim().toLowerCase();
  return text.startsWith("<!doctype html") || text.startsWith("<html") || (text.includes("<html") && text.includes("</html>"));
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
  state.envCompilePicker = null;
  stopTaskLogRefreshTimer();
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
  return value.split(/[\n,，]/).map((item) => item.trim()).filter(Boolean);
}

function parseGpuSchedulableFlags(value) {
  return parseList(value).map((item) => {
    if (item === "0" || item.toLowerCase() === "false") return 0;
    if (item === "1" || item.toLowerCase() === "true") return 1;
    throw new Error("GPU 可用性列表只能填写 0 或 1");
  });
}

function parseGpuComputeCapabilityOverrides(value) {
  const items = String(value || "").replace(/，/g, ",").replace(/\r/g, "").split(/[\n,]/).map((item) => item.trim());
  while (items.length && !items[items.length - 1]) items.pop();
  items.forEach((item) => {
    if (item && !/^\d+\.\d+$/.test(item)) throw new Error("GPU 算力必须填写为 major.minor，例如 8.9；空行表示自动探测");
  });
  return items;
}

function checkedValues(containerId, root = document) {
  const container = root.querySelector?.(`#${containerId}`);
  if (!container) return [];
  return Array.from(container.querySelectorAll("input[type='checkbox']:checked")).map((input) => input.value);
}

function uniqueNumbers(values) {
  const result = [];
  values.forEach((value) => {
    const number = Number(value);
    if (Number.isFinite(number) && number > 0 && !result.includes(number)) result.push(number);
  });
  return result;
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
  state.page = isPresenterUser() ? "presenter" : "dashboard";
  state.drawer = null;
  state.envCompilePicker = null;
  const nextHash = `#/${state.page}`;
  if (location.hash !== nextHash) {
    history.replaceState(null, "", nextHash);
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
  const [userPayload, configPayload] = await Promise.all([
    api("/auth/me", { method: "POST" }),
    api("/runtime-config"),
  ]);
  state.user = userPayload.data;
  state.data.runtimeConfig = configPayload.data;
  if (isStudentFileView() && !canViewStudentFiles()) state.fileViewScope = "own";
  ensureVisiblePage();
}

async function refreshPage() {
  await loadMe();
  ensureVisiblePage();
  if (isPresenterUser()) {
    await loadPresenterDashboard();
    return;
  }
  const loaders = {
    dashboard: async () => {
      state.data.dashboard = (await api("/dashboard/summary")).data;
      if (can("nodes:read")) state.data.nodes = (await api("/nodes")).data;
      state.lastDashboardRefreshAt = new Date();
    },
    tasks: async () => {
      await loadTasksPageData({ includeLookups: true });
    },
    files: async () => {
      state.data.files = (await api(`/files/list?${fileQuery(state.data.files.path || "/")}`)).data;
      state.data.fileJob = (await api("/files/jobs/latest")).data;
      updateFileJobTimer();
    },
    envs: async () => {
      state.data.envs = (await api("/envs")).data;
      updateEnvRefreshTimer();
    },
    manual: async () => {
      state.data.manual = (await api("/manual/current")).data;
    },
    account: async () => {
      state.user = (await api("/auth/samba/status", { method: "POST" })).data;
      state.data.sessions = (await api("/auth/sessions/list", { method: "POST" })).data;
    },
    students: async () => {
      const filters = cleanUserFilters({ ...state.userFilters, role: "student" });
      state.data.users = (await api("/users/list", { method: "POST", body: JSON.stringify(filters) })).data;
    },
    admin: async () => {
      state.data.nodes = (await api("/nodes")).data;
      state.data.users = (await api("/users/list", { method: "POST", body: JSON.stringify(cleanUserFilters(state.userFilters)) })).data;
      state.data.nodeOwnerUsers = state.adminMenu === "nodes"
        ? (await api("/users/list", { method: "POST", body: JSON.stringify({}) })).data
        : state.data.users;
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

async function loadPresenterDashboard() {
  const hours = PRESENTER_HISTORY_OPTIONS.includes(state.presenterHistoryHours) ? state.presenterHistoryHours : 1;
  state.data.presenter = (await api(`/dashboard/presenter?hours=${hours}`)).data;
  state.lastPresenterRefreshAt = new Date();
}

function emptyTaskList() {
  return { items: [], total: 0, page: 1, page_size: 100 };
}

function normalizeTaskZone(zone) {
  if (["running", "exec"].includes(zone)) return "running";
  if (["history", "hist", "finished"].includes(zone)) return "history";
  return "wait";
}

function cachedTaskList(zone) {
  return state.data.taskZones[normalizeTaskZone(zone)] || emptyTaskList();
}

async function loadTasksPageData({ includeLookups = false } = {}) {
  const zone = normalizeTaskZone(state.taskZone);
  const page = state.page;
  const allHistory = zone === "history" && state.taskHistoryAllLoaded;
  const requestSeq = ++state.taskRequestSeq;
  const params = new URLSearchParams({
    state: zone,
    page_size: "100",
  });
  if (allHistory) params.set("all_history", "true");
  const tasksRequest = api(`/tasks?${params.toString()}`);
  const lookupRequests = [];
  if (includeLookups && can("envs:read")) {
    lookupRequests.push(api("/envs").then((payload) => { state.data.envs = payload.data; }));
  }
  if (includeLookups && can("nodes:read")) {
    lookupRequests.push(api("/nodes").then((payload) => { state.data.nodes = payload.data; }));
  }
  const [tasksPayload] = await Promise.all([tasksRequest, ...lookupRequests]);
  if (
    requestSeq !== state.taskRequestSeq ||
    page !== state.page ||
    zone !== normalizeTaskZone(state.taskZone) ||
    allHistory !== (zone === "history" && state.taskHistoryAllLoaded)
  ) {
    return false;
  }
  state.data.tasks = tasksPayload.data;
  state.data.taskZones[zone] = tasksPayload.data;
  if (!state.data.tasks.items.some((task) => task.task_id === state.selectedTaskId)) state.selectedTaskId = "";
  return true;
}

function navigate(page) {
  const previousPage = state.page;
  state.page = page;
  if (page !== previousPage && ["admin", "students"].includes(page)) {
    state.userFilters = { user_id: "", keyword: "", role: "", state: "" };
  }
  location.hash = `/${page}`;
  state.drawer = null;
  state.envCompilePicker = null;
  stopTaskLogRefreshTimer();
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

function setPresenterRefreshSeconds(value) {
  const seconds = Math.max(0, Math.min(3600, Number(value) || 0));
  state.presenterRefreshSeconds = seconds;
  localStorage.setItem("ng_presenter_refresh_seconds", String(seconds));
  updateRealtimeTimers();
  render();
}

function setPresenterHistoryHours(value) {
  const hours = Number(value);
  state.presenterHistoryHours = PRESENTER_HISTORY_OPTIONS.includes(hours) ? hours : 1;
  localStorage.setItem("ng_presenter_history_hours", String(state.presenterHistoryHours));
  run(loadPresenterDashboard);
}

function updateRealtimeTimers() {
  updateAutoRefreshTimer();
  updateTaskEventStream();
  updateSessionRefreshTimer();
  updateFileJobTimer();
  updateEnvRefreshTimer();
  updateAuthWatchTimer();
  updateTaskLogRefreshTimer();
}

function updateTaskEventStream() {
  if (state.taskEventsSource) {
    state.taskEventsSource.close();
    state.taskEventsSource = null;
  }
  if (!state.user || !state.token || isPresenterUser() || typeof EventSource === "undefined") return;
  const base = state.apiBase.replace(/\/$/, "");
  const source = new EventSource(`${base}/tasks/events?token=${encodeURIComponent(state.token)}`);
  source.addEventListener("tasks", (event) => {
    const cursor = parseTaskEventCursor(event.data);
    if (state.page === "tasks") {
      refreshTasksFromEvent(cursor);
    } else if (state.page === "dashboard") {
      autoRefreshDashboard();
    }
  });
  source.onerror = () => {
    if (!state.token) source.close();
  };
  state.taskEventsSource = source;
}

function parseTaskEventCursor(data) {
  try {
    return JSON.parse(data || "{}");
  } catch (error) {
    return {};
  }
}

function taskCursorKey(cursor) {
  if (!cursor) return "";
  return [cursor.count ?? cursor.total ?? 0, cursor.max_task_id ?? 0, cursor.max_event_id ?? 0].join(":");
}

function changedTaskZones(cursor) {
  const zones = cursor?.zones || {};
  const result = [];
  ["wait", "running", "history"].forEach((zone) => {
    const next = zones[zone] || cursor;
    const nextKey = taskCursorKey(next);
    const previousKey = state.taskZoneCursors[zone];
    state.taskZoneCursors[zone] = nextKey;
    if (previousKey && previousKey !== nextKey) result.push(zone);
  });
  return result;
}

function refreshTasksFromEvent(cursor) {
  if (!state.user || state.page !== "tasks") return;
  const zone = normalizeTaskZone(state.taskZone);
  changedTaskZones(cursor);
  scheduleTaskRealtimeRefresh(zone);
}

function scheduleTaskRealtimeRefresh(zone) {
  if (state.taskRealtimeTimer && state.taskRealtimeZone === zone) return;
  if (state.taskRealtimeTimer) window.clearTimeout(state.taskRealtimeTimer);
  state.taskRealtimeZone = zone;
  state.taskRealtimeTimer = window.setTimeout(() => {
    state.taskRealtimeTimer = null;
    runTaskRealtimeRefresh(zone);
  }, 300);
}

async function runTaskRealtimeRefresh(zone) {
  if (!state.user || state.page !== "tasks" || normalizeTaskZone(state.taskZone) !== zone) return;
  if (state.taskRealtimeBusy) {
    state.taskRealtimePendingZone = zone;
    return;
  }
  state.taskRealtimeBusy = true;
  try {
    await loadTasksPageData();
    render();
  } catch (error) {
    if (!isAuthExpiredMessage(error.message)) console.warn("task realtime refresh failed", error);
  } finally {
    state.taskRealtimeBusy = false;
    const pendingZone = state.taskRealtimePendingZone;
    state.taskRealtimePendingZone = "";
    if (pendingZone && normalizeTaskZone(state.taskZone) === pendingZone) scheduleTaskRealtimeRefresh(pendingZone);
  }
}

function updateAutoRefreshTimer() {
  if (state.autoRefreshTimer) {
    window.clearInterval(state.autoRefreshTimer);
    state.autoRefreshTimer = null;
  }
  const seconds = state.page === "presenter" ? state.presenterRefreshSeconds : state.autoRefreshSeconds;
  if (!state.user || !["dashboard", "presenter"].includes(state.page) || seconds <= 0) return;
  state.autoRefreshTimer = window.setInterval(autoRefreshDashboard, seconds * 1000);
}

async function autoRefreshDashboard() {
  if (!state.user || !["dashboard", "presenter"].includes(state.page) || state.autoRefreshBusy) return;
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

function updateEnvRefreshTimer() {
  if (state.envRefreshTimer) {
    window.clearInterval(state.envRefreshTimer);
    state.envRefreshTimer = null;
  }
  if (!state.user || state.page !== "envs" || !hasActiveEnvImport()) return;
  state.envRefreshTimer = window.setInterval(refreshEnvsLive, 2000);
}

function hasActiveEnvImport() {
  return (state.data.envs || []).some((env) => ["copying", "importing", "fixing", "testing"].includes(env.state));
}

async function refreshEnvsLive() {
  if (!state.user || state.page !== "envs" || state.envRefreshBusy) return;
  state.envRefreshBusy = true;
  try {
    state.data.envs = (await api("/envs")).data;
    render();
  } catch (error) {
    if (!isAuthExpiredMessage(error.message)) console.warn("env refresh failed", error);
  } finally {
    state.envRefreshBusy = false;
    updateEnvRefreshTimer();
  }
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

function isStudentFileView() {
  return state.fileViewScope === "students";
}

function isSharedFileView() {
  return state.fileViewScope === "shared";
}

function isReadOnlyFileView() {
  return isStudentFileView() || isSharedFileView();
}

function canViewStudentFiles() {
  return state.user?.role === "mentor";
}

function fileQuery(path, scope = state.fileViewScope) {
  const params = new URLSearchParams({ path: path || "/" });
  if (scope === "students" || scope === "shared") params.set("scope", scope);
  return params.toString();
}

function requireOwnFileViewForWrite() {
  if (isStudentFileView()) throw new Error("学生文件视图仅支持查看");
  if (isSharedFileView()) throw new Error("共享文件夹视图仅支持查看和复制到我的文件夹");
}

function displayFilePath(path) {
  if (!isReadOnlyFileView()) return path || "/";
  const currentPath = state.data.files.path || "/";
  const currentDisplayPath = state.data.files.display_path || currentPath;
  const normalizedPath = normalizeClientPath(path || "/");
  const normalizedCurrent = normalizeClientPath(currentPath);
  if (normalizedPath === normalizedCurrent) return currentDisplayPath;
  if (normalizedCurrent !== "/" && normalizedPath.startsWith(`${normalizedCurrent}/`)) {
    return `${currentDisplayPath}${normalizedPath.slice(normalizedCurrent.length)}`;
  }
  return normalizedPath;
}

async function watchCurrentSession() {
  if (!state.user || state.authWatchBusy) return;
  state.authWatchBusy = true;
  try {
    const payload = await api("/auth/me", { method: "POST" });
    state.user = payload.data;
    if (isStudentFileView() && !canViewStudentFiles()) state.fileViewScope = "own";
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
  const formData = new FormData(form);
  const nodeId = state.adminNodeEditId;
  const checkedOwnerIds = formData.getAll("owner_user_ids").map((value) => Number(value)).filter(Boolean);
  const manualOwnerIds = parseList(formValue(form, "owner_user_ids_manual")).map((value) => Number(value)).filter(Boolean);
  const payload = {
    name: formValue(form, "name"),
    ip: formValue(form, "ip"),
    ssh_user: formValue(form, "ssh_user") || "ddltm",
    max_speed_mbps: formValue(form, "max_speed_mbps") ? Number(formValue(form, "max_speed_mbps")) : null,
    gpu_schedulable_flags: parseGpuSchedulableFlags(formValue(form, "gpu_schedulable_flags")),
    gpu_compute_capability_overrides: parseGpuComputeCapabilityOverrides(formValue(form, "gpu_compute_capability_overrides")),
    owner_user_ids: uniqueNumbers([...checkedOwnerIds, ...manualOwnerIds]),
    access_scope: formValue(form, "access_scope") || "public",
    sharing_scope: formValue(form, "sharing_scope") || "public",
  };
  await api(nodeId ? `/admin/nodes/${nodeId}` : "/admin/nodes", {
    method: nodeId ? "PUT" : "POST",
    body: JSON.stringify(payload),
  });
  state.adminNodeEditId = null;
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

async function deleteNode(nodeId) {
  const node = (state.data.nodes || []).find((item) => String(item.id) === String(nodeId));
  if (!window.confirm(`确认删除节点 ${node?.name || nodeId}？\n该操作会删除节点和 GPU 清单，并清理调度引用。`)) return;
  await api(`/admin/nodes/${nodeId}`, { method: "DELETE" });
  if (String(state.adminNodeEditId || "") === String(nodeId)) state.adminNodeEditId = null;
  await refreshPage();
  showToast("节点已删除", "success");
}

async function submitTask(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const mode = form.dataset.taskFormMode || "add";
  const payload = taskPayloadFromForm(form, mode);
  const endpoint = mode === "batch" ? "/tasks/batch" : (mode === "edit" ? `/tasks/${encodeURIComponent(formValue(form, "task_id"))}` : "/tasks");
  await api(endpoint, {
    method: mode === "edit" ? "PATCH" : "POST",
    body: JSON.stringify(payload),
  });
  state.drawer = null;
  state.taskFormDraft = null;
  state.taskPredecessorLoading = false;
  state.taskPredecessorRequestKey = "";
  await refreshPage();
}

async function cancelTask(taskId) {
  if (!window.confirm(`确认中止任务 ${taskId}？`)) return;
  await api(`/tasks/${taskId}/cancel`, { method: "POST" });
  await refreshPage();
}

async function resubmitTask(taskId) {
  if (!window.confirm(`确认重新提交任务 ${taskId}？\n系统会生成一个新任务 ID。`)) return;
  await api(`/tasks/${taskId}/resubmit`, { method: "POST" });
  await refreshPage();
}

async function showTaskLog(taskId) {
  stopTaskLogRefreshTimer();
  const task = findTaskById(taskId) || (await api(`/tasks/${taskId}`)).data;
  state.taskLog = {
    ...state.taskLog,
    taskId,
    text: "正在读取日志...",
    command: buildTaskLogCommand(task.log_path),
    paused: false,
    busy: false,
    lastRefreshAt: null,
    error: "",
  };
  state.drawer = { type: "task-log", title: `任务日志 ${taskId}` };
  render();
  await refreshTaskLogDrawer({ force: true });
  updateTaskLogRefreshTimer();
}

function findTaskById(taskId) {
  return (state.data.tasks.items || []).find((task) => task.task_id === taskId) || null;
}

function buildTaskLogCommand(logPath) {
  const path = logPath || "<log_path>";
  return `tail -f -n ${TASK_LOG_TAIL_LINES} ${shellQuote(path)}`;
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\"'\"'")}'`;
}

function stopTaskLogRefreshTimer() {
  if (!state.taskLog.timer) return;
  window.clearInterval(state.taskLog.timer);
  state.taskLog.timer = null;
}

function updateTaskLogRefreshTimer() {
  stopTaskLogRefreshTimer();
  if (!state.user || state.drawer?.type !== "task-log" || !state.taskLog.taskId) return;
  if (state.taskLog.paused || state.taskLog.refreshSeconds <= 0) return;
  state.taskLog.timer = window.setInterval(() => refreshTaskLogDrawer(), state.taskLog.refreshSeconds * 1000);
}

async function refreshTaskLogDrawer({ force = false } = {}) {
  if (!state.taskLog.taskId || state.taskLog.busy) return;
  if (!force && (state.taskLog.paused || state.drawer?.type !== "task-log")) return;
  const logElement = document.querySelector("[data-preserve-scroll='task-log']");
  const shouldStickToBottom = !logElement || logElement.scrollTop + logElement.clientHeight >= logElement.scrollHeight - 24;
  state.taskLog.busy = true;
  try {
    const log = await api(`/tasks/${state.taskLog.taskId}/log?tail=${encodeURIComponent(TASK_LOG_TAIL_BYTES)}`);
    state.taskLog.text = log;
    state.taskLog.error = "";
    state.taskLog.lastRefreshAt = new Date();
  } catch (error) {
    state.taskLog.error = error.message || "日志刷新失败";
  } finally {
    state.taskLog.busy = false;
    if (state.drawer?.type === "task-log") {
      render();
      if (shouldStickToBottom) scrollTaskLogToBottom();
    }
  }
}

function scrollTaskLogToBottom() {
  const logElement = document.querySelector("[data-preserve-scroll='task-log']");
  if (logElement) logElement.scrollTop = logElement.scrollHeight;
}

function setTaskLogRefreshSeconds(value) {
  const seconds = Math.max(0, Math.min(3600, Number(value) || 0));
  state.taskLog.refreshSeconds = seconds;
  localStorage.setItem("ng_task_log_refresh_seconds", String(seconds));
  updateTaskLogRefreshTimer();
  render();
}

function toggleTaskLogRefresh() {
  if (state.taskLog.paused || state.taskLog.refreshSeconds <= 0) {
    state.taskLog.paused = false;
    if (state.taskLog.refreshSeconds <= 0) {
      state.taskLog.refreshSeconds = 2;
      localStorage.setItem("ng_task_log_refresh_seconds", String(state.taskLog.refreshSeconds));
    }
  } else {
    state.taskLog.paused = true;
  }
  updateTaskLogRefreshTimer();
  render();
}

async function copyTaskLogCommand() {
  const text = state.taskLog.command || "";
  if (!text) return;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
  } else {
    const input = document.createElement("textarea");
    input.value = text;
    input.style.position = "fixed";
    input.style.left = "-9999px";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }
  showToast("日志命令已复制", "success");
}

async function openTaskForm(mode) {
  const selected = selectedTask();
  if (mode === "edit" && !selected) throw new Error("请选择任务");
  const requestKey = `${mode}:${selected?.task_id || ""}:${Date.now()}`;
  state.taskFormDraft = taskToDraft(mode, selected);
  state.taskPredecessorLoading = true;
  state.taskPredecessorRequestKey = requestKey;
  state.drawer = {
    type: "task-form",
    title: mode === "batch" ? "批量添加任务" : (mode === "edit" ? `修改任务 ${selected.task_id}` : "添加任务"),
    body: renderTaskForm(mode, selected),
  };
  render();
  loadTaskPredecessorOptions(selected?.task_id || "")
    .then(() => {
      if (state.taskPredecessorRequestKey !== requestKey) return;
      if (state.drawer?.type !== "task-form") return;
      const draft = syncTaskFormDraftFromDom() || state.taskFormDraft || taskToDraft(mode, selected);
      state.taskPredecessorLoading = false;
      state.taskFormDraft = draft;
      state.drawer.body = renderTaskForm(draft.mode, selectedTask());
      render();
    })
    .catch((error) => {
      if (state.taskPredecessorRequestKey !== requestKey) return;
      state.taskPredecessorLoading = false;
      if (state.drawer?.type === "task-form") render();
      showToast(error.message || "加载前驱任务失败", "error");
    });
}

async function loadTaskPredecessorOptions(excludedTaskId = "") {
  const [wait, running] = await Promise.all([
    api("/tasks?state=wait&page_size=200"),
    api("/tasks?state=running&page_size=200"),
  ]);
  const seen = new Set();
  state.taskPredecessorOptions = [...(wait.data.items || []), ...(running.data.items || [])]
    .filter((task) => task.task_id !== excludedTaskId)
    .filter((task) => {
      if (seen.has(task.task_id)) return false;
      seen.add(task.task_id);
      return true;
    });
}

function taskPayloadFromForm(form, mode) {
  const nodeId = formValue(form, "node_id");
  const common = {
    description: formValue(form, "description"),
    env_id: formValue(form, "env_id") ? Number(formValue(form, "env_id")) : null,
    workdir: formValue(form, "workdir") || "/",
    priority: 0,
    urgent: form.elements.urgent.checked,
    on_hold: form.elements.on_hold.checked,
    predecessor_task_id: formValue(form, "predecessor_task_id") || null,
    requirement: {
      need_gpus: Number(formValue(form, "need_gpus") || 1),
      node_id: nodeId ? Number(nodeId) : null,
      gpu_types: checkedValues("taskGpuTypeOptions", form),
      allow_gpu_reuse: form.elements.allow_gpu_reuse.checked,
    },
  };
  if (mode === "batch") return { ...common, commands: form.elements.commands.value };
  return { ...common, command: formValue(form, "command") };
}

async function holdSelectedTask() {
  const task = requireSelectedTask();
  await api(`/tasks/${task.task_id}/hold`, { method: "POST" });
  await refreshPage();
}

async function deleteSelectedTask() {
  const task = requireSelectedTask();
  const preview = (await api(`/tasks/${task.task_id}/delete-preview`)).data;
  const successors = preview.successors || [];
  let deleteSuccessors = false;
  if (successors.length) {
    deleteSuccessors = window.confirm(`任务 ${task.task_id} 有以下后继任务：\n${successors.join(", ")}\n\n点击“确定”将同时删除当前任务和所有后继任务；点击“取消”后可选择只删除当前任务。`);
    if (!deleteSuccessors && !window.confirm(`只删除当前任务 ${task.task_id}？后继任务会保留，但前驱关系会被清理。`)) return;
  } else if (!window.confirm(`确认删除任务 ${task.task_id}？`)) {
    return;
  }
  await api(`/tasks/${task.task_id}?delete_successors=${deleteSuccessors ? "true" : "false"}`, { method: "DELETE" });
  state.selectedTaskId = "";
  await refreshPage();
}

async function loadAllHistoryTasks() {
  state.taskZone = "history";
  state.selectedTaskId = "";
  state.taskHistoryAllLoaded = true;
  state.taskListLoading = true;
  state.data.tasks = cachedTaskList("history");
  localStorage.setItem("ng_task_zone", "history");
  render();
  try {
    await loadTasksPageData();
  } finally {
    if (normalizeTaskZone(state.taskZone) === "history") state.taskListLoading = false;
  }
  render();
}

async function openTaskWorkdirPicker() {
  const draft = syncTaskFormDraftFromDom();
  const current = draft?.workdir || "/";
  state.fileTargetPicker = {
    mode: "task-workdir",
    sourcePath: "",
    currentPath: current,
    items: [],
  };
  if (state.drawer && draft) state.drawer.body = renderTaskForm(draft.mode, selectedTask());
  await loadFileTargetPickerPath(current);
  render();
}

function syncTaskFormDraftFromDom() {
  const form = document.querySelector("#taskForm");
  if (!form) return state.taskFormDraft;
  state.taskFormDraft = captureTaskFormDraft(form);
  return state.taskFormDraft;
}

async function importEnvs() {
  state.fileTargetPicker = {
    mode: "env-import",
    sourcePath: "",
    currentPath: "/",
    items: [],
  };
  await loadFileTargetPickerPath("/");
  render();
}

async function testEnv(envId) {
  const payload = await api(`/envs/${envId}/test`, { method: "POST" });
  state.drawer = { title: `环境检测 #${envId}`, body: renderEnvTestResult(payload.data) };
}

async function showEnvLog(envId) {
  const env = (state.data.envs || []).find((item) => String(item.id) === String(envId));
  const log = await api(`/envs/${envId}/log`);
  state.drawer = { title: `环境日志 ${env?.name || `#${envId}`}`, body: renderEnvOperationLog(log) };
}

async function cloneEnv(envId) {
  const env = (state.data.envs || []).find((item) => String(item.id) === String(envId));
  const sourceName = env?.name || `#${envId}`;
  const suggested = env?.name ? `${env.name}_copy` : "";
  const name = window.prompt(`请输入 ${sourceName} 的新环境名`, suggested);
  if (name === null) return;
  const cleaned = name.trim();
  if (!cleaned) {
    showToast("请输入新环境名", "error");
    return;
  }
  await api(`/envs/${envId}/clone`, { method: "POST", body: JSON.stringify({ name: cleaned }) });
  showToast("已开始创建环境副本", "success");
  await refreshPage();
}

async function deleteEnv(envId) {
  await api(`/envs/${envId}`, { method: "DELETE" });
  await refreshPage();
}

function confirmDeleteEnv(envId) {
  const env = (state.data.envs || []).find((item) => String(item.id) === String(envId));
  const name = env?.name || `#${envId}`;
  const path = env?.path || "";
  return window.confirm(`确认删除环境 ${name}？\n\n该操作会同时删除 miniconda/envs 下的对应环境文件夹。\n${path}`);
}

async function showEnvPackageDrawer(envId, mode) {
  const env = (state.data.envs || []).find((item) => String(item.id) === String(envId));
  if (mode !== "install") {
    const payload = await api(`/envs/${envId}/test`, { method: "POST" });
    state.envPackageInstall = null;
    state.envPackageDelete = {
      envId: String(envId),
      packages: payload.data?.packages || [],
      packageCount: payload.data?.package_count || 0,
      selectedPackageNames: [],
    };
    state.drawer = {
      title: `删除包 · ${env?.name || `#${envId}`}`,
      body: renderEnvPackageDeletePanel(),
    };
    render();
    return;
  }
  const payload = await api(`/envs/${envId}/test`, { method: "POST" });
  state.envPackageDelete = null;
  state.envPackageInstall = {
    envId: String(envId),
    method: "conda",
    pipMode: "wheel",
    batch: false,
    folderCommand: "pip",
    installStatus: "ready",
    compileTarget: null,
    gpuVisibility: "default",
    visibleGpuIndices: [],
    packagePath: "",
    folderPath: "",
    requirementsPath: "",
    packages: payload.data?.packages || [],
    packageCount: payload.data?.package_count || 0,
  };
  state.drawer = {
    title: `安装包 · ${env?.name || `#${envId}`}`,
    body: renderEnvPackageInstallPanel(),
  };
  render();
}

function refreshEnvPackageInstallFromForm() {
  if (!state.envPackageInstall) return;
  const form = document.querySelector("#envPackageInstallForm");
  if (!form) return;
  const data = new FormData(form);
  state.envPackageInstall.method = data.get("method") || "conda";
  state.envPackageInstall.pipMode = data.get("pip_mode") || "wheel";
  state.envPackageInstall.batch = data.get("batch") === "on";
  state.envPackageInstall.folderCommand = data.get("folder_command") || "pip";
}

async function openEnvPackagePicker(field, kind, extensions = "") {
  if (isEnvPackageInstalling()) return;
  refreshEnvPackageInstallFromForm();
  state.fileTargetPicker = {
    mode: "env-package",
    sourcePath: "",
    currentPath: "/",
    items: [],
    targetField: field,
    selectKind: kind,
    extensions: extensions.split(",").map((item) => item.trim().toLowerCase()).filter(Boolean),
  };
  await loadFileTargetPickerPath("/");
  render();
}

async function openEnvCompilePicker() {
  if (isEnvPackageInstalling()) return;
  refreshEnvPackageInstallFromForm();
  const panel = state.envPackageInstall;
  if (!panel) return;
  state.envCompilePicker = {
    loading: true,
    targets: [],
    selectedTargetId: panel.compileTarget?.id || "",
    gpuVisibility: panel.gpuVisibility || "default",
    visibleGpuIndices: [...(panel.visibleGpuIndices || [])],
  };
  render();
  try {
    const payload = await api("/envs/compile-targets");
    state.envCompilePicker.targets = payload.data || [];
    if (!state.envCompilePicker.selectedTargetId && state.envCompilePicker.targets.length) {
      state.envCompilePicker.selectedTargetId = state.envCompilePicker.targets[0].id;
    }
  } finally {
    if (state.envCompilePicker) state.envCompilePicker.loading = false;
    render();
  }
}

function closeEnvCompilePicker() {
  state.envCompilePicker = null;
  render();
}

function selectEnvCompileTarget(targetId) {
  if (!state.envCompilePicker) return;
  state.envCompilePicker.selectedTargetId = targetId;
  state.envCompilePicker.gpuVisibility = "default";
  state.envCompilePicker.visibleGpuIndices = [];
  render();
}

function setCompileGpuMode(mode) {
  if (!state.envCompilePicker) return;
  state.envCompilePicker.gpuVisibility = mode;
  if (mode !== "gpu") state.envCompilePicker.visibleGpuIndices = [];
  render();
}

function toggleCompileGpu(index, checked) {
  if (!state.envCompilePicker) return;
  const current = new Set(state.envCompilePicker.visibleGpuIndices || []);
  if (checked) current.add(Number(index));
  else current.delete(Number(index));
  state.envCompilePicker.visibleGpuIndices = Array.from(current).sort((a, b) => a - b);
  state.envCompilePicker.gpuVisibility = state.envCompilePicker.visibleGpuIndices.length ? "gpu" : "default";
  render();
}

function confirmEnvCompilePicker() {
  const picker = state.envCompilePicker;
  const panel = state.envPackageInstall;
  if (!picker || !panel) return;
  const target = (picker.targets || []).find((item) => item.id === picker.selectedTargetId);
  if (!target) throw new Error("请选择编译节点");
  if (picker.gpuVisibility === "gpu" && !(picker.visibleGpuIndices || []).length) throw new Error("请选择可见 GPU");
  panel.compileTarget = target;
  panel.gpuVisibility = picker.gpuVisibility || "default";
  panel.visibleGpuIndices = panel.gpuVisibility === "gpu" ? [...(picker.visibleGpuIndices || [])] : [];
  state.envCompilePicker = null;
  if (state.drawer) state.drawer.body = renderEnvPackageInstallPanel();
  render();
}

function clearEnvCompileTarget() {
  if (!state.envPackageInstall || isEnvPackageInstalling()) return;
  state.envPackageInstall.compileTarget = null;
  state.envPackageInstall.gpuVisibility = "default";
  state.envPackageInstall.visibleGpuIndices = [];
  if (state.drawer) state.drawer.body = renderEnvPackageInstallPanel();
  render();
}

async function submitEnvPackageInstall(event) {
  event.preventDefault();
  refreshEnvPackageInstallFromForm();
  const panel = state.envPackageInstall;
  if (!panel) return;
  if (panel.installStatus === "installing") return;
  const body = {
    method: panel.method,
    pip_mode: panel.method === "pip" ? panel.pipMode : null,
    package_path: panel.packagePath || null,
    folder_path: panel.folderPath || null,
    requirements_path: panel.requirementsPath || null,
    batch: Boolean(panel.batch),
    folder_command: panel.folderCommand || "pip",
    compile_on_master: Boolean(panel.compileTarget?.is_master),
    target_node_id: panel.compileTarget?.node_id || null,
    gpu_visibility: panel.gpuVisibility || "default",
    visible_gpu_indices: panel.gpuVisibility === "gpu" ? (panel.visibleGpuIndices || []) : [],
  };
  panel.installStatus = "installing";
  if (state.drawer) state.drawer.body = renderEnvPackageInstallPanel();
  render();
  try {
    const result = (await api(`/envs/${panel.envId}/packages/install`, { method: "POST", body: JSON.stringify(body) })).data;
    showToast("安装作业已加入后台队列", "success");
    state.envPackageInstall = null;
    state.drawer = { title: `安装作业 #${result.id}`, body: renderEnvPackageInstallResult(result) };
    render();
  } catch (error) {
    panel.installStatus = "ready";
    if (state.drawer) state.drawer.body = renderEnvPackageInstallPanel();
    render();
    throw error;
  }
}

function isEnvPackageInstalling() {
  return state.envPackageInstall?.installStatus === "installing";
}

function refreshEnvPackageDeleteFromForm() {
  if (!state.envPackageDelete) return;
  state.envPackageDelete.selectedPackageNames = Array.from(document.querySelectorAll("[data-delete-package-select]:checked")).map((item) => item.value);
}

async function submitEnvPackageDelete(event) {
  event.preventDefault();
  refreshEnvPackageDeleteFromForm();
  const panel = state.envPackageDelete;
  if (!panel || !panel.selectedPackageNames.length) throw new Error("请选择需要删除的包");
  const body = { package_names: panel.selectedPackageNames };
  const preview = (await api(`/envs/${panel.envId}/packages/delete-preview`, { method: "POST", body: JSON.stringify(body) })).data;
  if (!window.confirm(preview.prompt || "确认删除选中的包？")) return;
  const result = (await api(`/envs/${panel.envId}/packages/delete`, { method: "POST", body: JSON.stringify(body) })).data;
  showToast(result.ok ? "删除包命令执行完成" : "删除包命令执行失败，请查看输出和日志", result.ok ? "success" : "error");
  state.drawer = { title: `删除结果 · ${result.env_name}`, body: renderEnvPackageDeleteResult(result) };
  render();
}

async function openPath(path, scope = state.fileViewScope) {
  state.fileViewScope = scope || "own";
  state.data.files.path = path;
  state.data.preview = null;
  state.data.selectedFilePath = "";
  await refreshPage();
}

async function previewFile(path) {
  const payload = await api(`/files/preview?${fileQuery(path)}`);
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

async function toggleStudentFileView() {
  await openPath("/", isStudentFileView() ? "own" : "students");
}

async function toggleSharedFileView() {
  await openPath("/", isSharedFileView() ? "own" : "shared");
}

async function createFolderFromPrompt() {
  requireOwnFileViewForWrite();
  const name = prompt("新建文件夹名称");
  if (!name) return;
  await api("/files/mkdir", { method: "POST", body: JSON.stringify({ path: joinPath(state.data.files.path || "/", name) }) });
  await refreshPage();
}

async function createFileFromPrompt() {
  requireOwnFileViewForWrite();
  const name = prompt("新建文件名称");
  if (!name) return;
  const path = joinPath(state.data.files.path || "/", name);
  await api("/files/create", { method: "POST", body: JSON.stringify({ path, content: "" }) });
  await refreshPage();
  await previewFile(path);
}

async function renameSelectedPath() {
  requireOwnFileViewForWrite();
  const source = requireSelectedPath();
  const name = prompt("重命名为", baseName(source));
  if (!name || name === baseName(source)) return;
  const target = joinPath(parentPath(source), name);
  await api("/files/rename", { method: "POST", body: JSON.stringify({ path: source, target_path: target }) });
  await refreshPage();
  state.data.selectedFilePath = target;
}

async function copySelectedPath() {
  requireOwnFileViewForWrite();
  await openFileTargetPicker("copy");
}

async function copySelectedPathToShared() {
  if (!isSharedFileView()) requireOwnFileViewForWrite();
  await openFileTargetPicker("copy-to-shared");
}

async function copySelectedPathToOwn() {
  if (!isSharedFileView()) throw new Error("请先切换到共享文件夹");
  await openFileTargetPicker("copy-from-shared");
}

async function moveSelectedPath() {
  requireOwnFileViewForWrite();
  await openFileTargetPicker("move");
}

async function archiveSelectedFolder() {
  requireOwnFileViewForWrite();
  const source = requireSelectedPath();
  const item = currentSelectedFileItem();
  if (item?.type !== "directory") throw new Error("请选择文件夹");
  const targetPath = joinPath(parentPath(source), `${baseName(source)}_${Date.now()}.zip`);
  const payload = await api("/files/archive", { method: "POST", body: JSON.stringify({ path: source, target_path: targetPath }) });
  state.data.fileJob = payload.data;
  updateFileJobTimer();
}

async function extractSelectedZip() {
  requireOwnFileViewForWrite();
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
  const targetScope = mode === "copy-to-shared" ? "shared" : "own";
  state.fileTargetPicker = {
    mode,
    sourcePath: source,
    sourceScope: state.fileViewScope,
    targetScope,
    listScope: targetScope,
    currentPath: mode === "copy" || mode === "move" || mode === "extract" ? startPath : "/",
    items: [],
  };
  await loadFileTargetPickerPath(state.fileTargetPicker.currentPath);
  render();
}

async function loadFileTargetPickerPath(path) {
  if (!state.fileTargetPicker) return;
  const payload = await api(`/files/list?${fileQuery(path || "/", state.fileTargetPicker.listScope || "own")}`);
  state.fileTargetPicker.currentPath = payload.data.path || "/";
  const items = payload.data.items || [];
  if (state.fileTargetPicker.mode === "env-import") {
    state.fileTargetPicker.items = items.filter((item) => item.type === "directory" || (item.type === "file" && item.path.toLowerCase().endsWith(".zip")));
  } else if (state.fileTargetPicker.mode === "env-package") {
    const extensions = state.fileTargetPicker.extensions || [];
    state.fileTargetPicker.items = items.filter((item) => item.type === "directory" || (state.fileTargetPicker.selectKind === "file" && item.type === "file" && (!extensions.length || extensions.some((suffix) => item.path.toLowerCase().endsWith(suffix)))));
  } else {
    state.fileTargetPicker.items = items.filter((item) => item.type === "directory");
  }
  if (state.fileTargetPicker.mode === "env-import" || (state.fileTargetPicker.mode === "env-package" && state.fileTargetPicker.selectKind === "file")) state.fileTargetPicker.sourcePath = "";
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
  if (picker.mode === "env-import") {
    if (!picker.sourcePath) throw new Error("请选择环境 zip 包");
    if (!confirm(`确认导入环境包 ${picker.sourcePath}？`)) return;
    const selectedPath = picker.sourcePath;
    state.fileTargetPicker = null;
    render();
    await api("/envs/import-archive", { method: "POST", body: JSON.stringify({ path: selectedPath }) });
    showToast("已开始导入环境", "success");
    await refreshPage();
    return;
  }
  if (picker.mode === "env-package") {
    const selectedPath = picker.selectKind === "directory" ? picker.currentPath : picker.sourcePath;
    if (!selectedPath) throw new Error("请选择安装文件或文件夹");
    if (state.envPackageInstall && picker.targetField) state.envPackageInstall[picker.targetField] = selectedPath;
    state.fileTargetPicker = null;
    if (state.drawer) state.drawer.body = renderEnvPackageInstallPanel();
    render();
    return;
  }
  if (picker.mode === "task-workdir") {
    const selectedPath = picker.currentPath || "/";
    const draft = state.taskFormDraft || syncTaskFormDraftFromDom() || taskToDraft("add");
    draft.workdir = selectedPath;
    state.taskFormDraft = draft;
    if (state.drawer) state.drawer.body = renderTaskForm(draft.mode, selectedTask());
    state.fileTargetPicker = null;
    render();
    return;
  }
  if (picker.mode === "extract") {
    const payload = await api("/files/extract", { method: "POST", body: JSON.stringify({ path: picker.sourcePath, target_path: picker.currentPath }) });
    state.data.fileJob = payload.data;
    state.fileTargetPicker = null;
    updateFileJobTimer();
    return;
  }
  const targetPath = buildPickedTargetPath(picker.sourcePath, picker.currentPath, picker.mode, true);
  const endpoint = picker.mode === "move" ? "/files/move" : "/files/copy";
  const body = {
    path: picker.sourcePath,
    target_path: targetPath,
    scope: picker.sourceScope || "own",
    target_scope: picker.targetScope || "own",
  };
  await api(endpoint, { method: "POST", body: JSON.stringify(body) });
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
  if (isDirectory && !isCopyBetweenFileScopes(mode) && isSameOrChildPath(targetFolder, sourcePath)) {
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

function isCopyBetweenFileScopes(mode) {
  return mode === "copy-to-shared" || mode === "copy-from-shared";
}

async function deleteSelectedPath() {
  requireOwnFileViewForWrite();
  const source = requireSelectedPath();
  if (!confirm(`确认删除 ${source}？`)) return;
  await api(`/files?path=${encodeURIComponent(source)}`, { method: "DELETE" });
  state.data.preview = null;
  state.data.selectedFilePath = "";
  await refreshPage();
}

async function uploadCurrentFile(event) {
  requireOwnFileViewForWrite();
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
  requireOwnFileViewForWrite();
  const preview = state.data.preview;
  if (!preview?.path || preview.encoding !== "text") throw new Error("当前文件不可保存");
  await api("/files/save", { method: "POST", body: JSON.stringify({ path: preview.path, content }) });
  await previewFile(preview.path);
}

async function grantCurrentFileExecutePermission() {
  requireOwnFileViewForWrite();
  const preview = state.data.preview;
  if (!preview?.path) throw new Error("请先打开需要授权的文件");
  const payload = await api("/files/permissions/execute", { method: "POST", body: JSON.stringify({ path: preview.path }) });
  state.data.preview = { ...preview, ...payload.data };
  render();
}

async function downloadSelectedPath() {
  const source = requireSelectedPath();
  const selectedItem = currentSelectedFileItem();
  if (isReadOnlyFileView() && selectedItem?.type === "directory") throw new Error("只读视图中的文件夹请进入查看或使用复制按钮");
  if (selectedItem?.type === "directory") {
    await archiveSelectedFolder();
    return;
  }
  await downloadPath(source);
}

async function downloadPath(source) {
  const response = await fetch(`${state.apiBase.replace(/\/$/, "")}/files/download?${fileQuery(source)}`, {
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
  if (mode === "env-import") return "导入环境";
  if (mode === "env-package") return "选择安装资源";
  if (mode === "task-workdir") return "选择项目路径";
  if (mode === "extract") return "解压到";
  if (mode === "copy-to-shared") return "复制到共享文件夹";
  if (mode === "copy-from-shared") return "复制到我的文件夹";
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

async function toggleCurrentSamba(enabled, currentPassword = "") {
  if (enabled && !currentPassword) {
    throw new Error("开启 Samba 需要当前密码");
  }
  const payload = await api("/auth/samba/update", {
    method: "POST",
    body: JSON.stringify({
      enabled,
      ...(enabled ? { current_password: currentPassword } : {}),
    }),
  });
  state.user = payload.data;
  render();
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

async function switchTaskZone(zone) {
  const nextZone = normalizeTaskZone(zone);
  if (normalizeTaskZone(state.taskZone) === nextZone && !state.taskListLoading) return;
  state.taskZone = nextZone;
  state.selectedTaskId = "";
  state.taskListLoading = true;
  state.data.tasks = cachedTaskList(nextZone);
  if (nextZone !== "history") state.taskHistoryAllLoaded = false;
  localStorage.setItem("ng_task_zone", nextZone);
  render();
  try {
    await loadTasksPageData();
  } catch (error) {
    showToast(error.message || "切换任务区失败", "error");
  } finally {
    if (normalizeTaskZone(state.taskZone) === nextZone) state.taskListLoading = false;
  }
  render();
}

function selectTask(taskId) {
  state.selectedTaskId = taskId;
  render();
}

function selectedTask() {
  return (state.data.tasks.items || []).find((task) => task.task_id === state.selectedTaskId) || null;
}

function requireSelectedTask() {
  const task = selectedTask();
  if (!task) throw new Error("请选择任务");
  return task;
}

async function handleTaskAction(action) {
  if (action === "add") return openTaskForm("add");
  if (action === "batch") return openTaskForm("batch");
  if (action === "edit") return openTaskForm("edit");
  if (action === "hold") return holdSelectedTask();
  if (action === "delete") return deleteSelectedTask();
  if (action === "cancel") return cancelTask(requireSelectedTask().task_id);
  if (action === "resubmit") return resubmitTask(requireSelectedTask().task_id);
  if (action === "historyAll") return loadAllHistoryTasks();
  if (action === "log") return showTaskLog(requireSelectedTask().task_id);
  return null;
}

function updateTaskGpuTypeOptions() {
  const form = document.querySelector("#taskForm");
  const container = document.querySelector("#taskGpuTypeOptions");
  if (!form || !container) return;
  container.innerHTML = renderTaskGpuTypeOptions(
    formValue(form, "node_id"),
    checkedValues("taskGpuTypeOptions", form),
    formValue(form, "env_id"),
  );
}

function switchAdminMenu(menu) {
  state.adminMenu = menu;
  if (menu !== "nodes") state.adminNodeEditId = null;
  localStorage.setItem("ng_admin_menu", menu);
  updateRealtimeTimers();
  if (["audit", "nodes"].includes(menu)) {
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
  const key = formValue(form, "key");
  await api("/admin/settings", {
    method: "PATCH",
    body: JSON.stringify({ values: { [key]: formValue(form, "value") } }),
  });
  state.adminSettingKey = key;
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
          <div class="brand-logo app-logo app-logo-sidebar"><img src="./load_page.png" alt="NebulaGrid 标志"></div>
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
      ${state.envCompilePicker ? renderEnvCompilePicker() : ""}
      ${state.toast ? `<div class="toast ${state.toast.type}">${escapeHtml(state.toast.text)}</div>` : ""}
      ${state.loading ? `<div class="loading">正在处理...</div>` : ""}
    </div>
  `;
}

function renderLogin() {
  return `
    <main class="login-page">
      <section class="login-copy">
        <div class="login-mark" aria-hidden="true">
          <img src="./load_page.png" alt="">
        </div>
        <div class="login-copy-text">
          <p>NebulaGrid 3.0</p>
          <h1>天枢 3.0</h1>
          <h2>多GPU集群任务管理调度与资源管理平台</h2>
          <div class="login-stats">
            <span>节点监控</span>
            <span>任务队列</span>
            <span>文件工作区</span>
            <span>环境维护</span>
          </div>
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
          <span>只展示计算节点的实时快照。${state.lastDashboardRefreshAt ? ` 上次刷新：${formatTime(state.lastDashboardRefreshAt)}` : ""}</span>
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
  const gpus = schedulableGpus(node);
  const bandwidth = speed(node.network_bandwidth_mbps ?? node.max_speed_mbps);
  return `
    <article class="node-card">
      <div class="node-head">
        <div>
          <h3>${escapeHtml(node.name)}</h3>
          <p>可用带宽 ${bandwidth} · ${gpus.length} GPUs</p>
        </div>
        <span class="status ${node.state}">${nodeStateText(node.state)}</span>
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

function renderPresenter() {
  const payload = state.data.presenter || {};
  const summary = payload.summary || {};
  const nodes = payload.nodes || [];
  const metrics = [
    ["节点数量", summary.nodes_total ?? "-"],
    ["在线节点", summary.nodes_online ?? "-"],
    ["GPU 总数", summary.gpus_total ?? "-"],
    ["等待任务", summary.tasks_waiting ?? "-"],
    ["运行任务", summary.tasks_running ?? "-"],
    ["历史任务", summary.tasks_history_total ?? "-"],
  ];
  return `
    <div class="presenter-layout">
      <main class="presenter-workspace">
        <header class="presenter-topbar">
          <div>
            <p>NebulaGrid 展示者视图</p>
            <h1>集群运行状态</h1>
          </div>
          <div class="presenter-controls">
            <span>${state.lastPresenterRefreshAt ? `更新 ${formatTime(state.lastPresenterRefreshAt)}` : "等待刷新"}</span>
            <div class="presenter-range" aria-label="历史数据范围">
              ${PRESENTER_HISTORY_OPTIONS.map((hours) => `
                <button class="secondary ${state.presenterHistoryHours === hours ? "active" : ""}" type="button" data-presenter-hours="${hours}">${hours}h</button>
              `).join("")}
            </div>
            <label class="refresh-control presenter-refresh">刷新
              <input name="presenter_refresh_seconds" type="number" min="0" max="3600" step="1" value="${state.presenterRefreshSeconds}">
              <span>秒</span>
            </label>
            <button class="secondary" data-action="refresh">刷新</button>
          </div>
        </header>
        <section class="presenter-metrics">
          ${metrics.map(([label, value]) => `
            <article>
              <span>${escapeHtml(label)}</span>
              <strong>${escapeHtml(value)}</strong>
            </article>
          `).join("")}
        </section>
        <section class="presenter-node-wall" data-preserve-scroll="presenter-node-wall">
          ${nodes.length ? nodes.map(renderPresenterNode).join("") : renderEmpty("暂无计算节点")}
        </section>
      </main>
      <button class="presenter-logout secondary" data-action="logout">退出登录</button>
      ${state.toast ? `<div class="toast ${state.toast.type}">${escapeHtml(state.toast.text)}</div>` : ""}
      ${state.loading ? `<div class="loading">正在处理...</div>` : ""}
    </div>
  `;
}

function renderPresenterNode(node) {
  const gpus = schedulableGpus(node);
  return `
    <article class="presenter-node">
      <div class="presenter-node-title">
        <div>
          <h2>${escapeHtml(node.name)}</h2>
          <span>${escapeHtml(gpus.length)} GPU · ${node.scheduling_enabled ? "调度开启" : "调度关闭"}</span>
        </div>
        <span class="status ${node.state}">${nodeStateText(node.state)}</span>
      </div>
      <div class="presenter-node-row ${gpus.length ? "has-gpu" : "no-gpu"}">
        ${presenterMetricCard("CPU 使用率", percent(node.cpu_usage), node.history?.cpu_usage || [], "percent", "cpu", node.cpu_usage)}
        ${presenterMetricCard("可用内存", formatMb(node.avail_ram_mb), node.history?.avail_ram_mb || [], "memory", "memory", node.avail_ram_mb)}
        <div class="presenter-network-stack">
          ${presenterMetricCard("网络接收", speed(node.download_mbps), node.history?.download_mbps || [], "dynamic", "network", node.download_mbps)}
          ${presenterMetricCard("网络发送", speed(node.upload_mbps), node.history?.upload_mbps || [], "dynamic", "network", node.upload_mbps)}
        </div>
        ${gpus.length ? `
          ${renderPresenterGpuMetricPanel("GPU 使用率", gpus, "usage")}
          ${renderPresenterGpuMetricPanel("可用 GPU 显存", gpus, "memory")}
        ` : `<section class="presenter-gpu-panel">${renderEmpty("暂无 GPU")}</section>`}
      </div>
    </article>
  `;
}

function renderPresenterGpuMetricPanel(label, gpus = [], kind = "usage") {
  return `
    <section class="presenter-gpu-panel ${kind}">
      <div class="presenter-gpu-panel-head">
        <span>${escapeHtml(label)}</span>
        <small>${kind === "usage" ? "当前使用率" : "当前可用显存"}</small>
      </div>
      ${gpus.length ? `
        <div class="presenter-gpu-grid">
          ${gpus.map((gpu, index) => {
            const tone = presenterGpuTone(index, gpu);
            const free = Number(gpu.free_vram_mb || 0);
            const total = Number(gpu.total_vram_mb || 0);
            const value = kind === "usage" ? percent(gpu.gpu_usage) : formatMb(free);
            const detail = kind === "usage"
              ? `${escapeHtml(gpu.process_count ?? 0)} 进程 · ${gpu.scheduled_occupied ? "调度占用" : "空闲"}`
              : `${formatMb(free)} / ${formatMb(total)} · ${gpu.scheduled_occupied ? "调度占用" : "空闲"}`;
            const points = kind === "usage" ? gpu.history?.gpu_usage || [] : gpu.history?.free_vram_mb || [];
            const mode = kind === "usage" ? "percent" : "memory";
            return `
              <div class="presenter-gpu-card ${kind} ${tone} ${gpu.scheduled_occupied ? "occupied" : ""}" title="${escapeAttr(gpu.model || "Unknown")}">
                <div class="presenter-gpu-card-head">
                  <div>
                    <b>GPU ${escapeHtml(gpu.gpu_index)}</b>
                    <span>${escapeHtml(gpu.model || "Unknown")}</span>
                  </div>
                  <strong>${value}</strong>
                </div>
                ${renderSparkline(points, mode, tone, kind === "usage" ? gpu.gpu_usage : free)}
                <small>${detail}</small>
              </div>
            `;
          }).join("")}
        </div>
      ` : `<div class="presenter-panel-empty">暂无 GPU</div>`}
    </section>
  `;
}

function presenterMetricCard(label, value, points, mode, tone = "default", currentValue = null) {
  return `
    <section class="presenter-metric-card ${tone}">
      <div class="presenter-metric-head">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(value)}</strong>
      </div>
      ${renderSparkline(points, mode, tone, currentValue)}
    </section>
  `;
}

function renderSparkline(points = [], mode = "dynamic", tone = "", currentValue = null) {
  const endMs = state.lastPresenterRefreshAt?.getTime?.() || Date.now();
  const hours = PRESENTER_HISTORY_OPTIONS.includes(state.presenterHistoryHours) ? state.presenterHistoryHours : 1;
  const startMs = endMs - hours * 60 * 60 * 1000;
  const samples = normalizeTimelinePoints(points, startMs, endMs, currentValue);
  if (!samples.length) return `<div class="presenter-sparkline ${tone} empty"></div>`;
  const values = samples.map((point) => point.value);
  const maxValue = mode === "percent" ? 100 : Math.max(1, ...values);
  const chartPoints = samples.map((point) => {
    const x = Math.max(0, Math.min(100, ((point.time - startMs) / (endMs - startMs)) * 100));
    const y = 40 - Math.max(0, Math.min(1, point.value / maxValue)) * 36;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });
  const areaPoints = `0,42 ${chartPoints.join(" ")} 100,42`;
  return `
    <div class="presenter-sparkline ${tone}">
      <svg viewBox="0 0 100 42" preserveAspectRatio="none" aria-hidden="true" focusable="false">
        <polygon points="${areaPoints}"></polygon>
        <polyline points="${chartPoints.join(" ")}"></polyline>
      </svg>
    </div>
  `;
}

function normalizeTimelinePoints(points = [], startMs, endMs, currentValue = null) {
  const samples = (points || [])
    .map((point) => {
      const time = Date.parse(point.time || "");
      const value = Number(point.value);
      return Number.isFinite(time) && Number.isFinite(value) ? { time, value } : null;
    })
    .filter((point) => point && point.time >= startMs && point.time <= endMs)
    .sort((left, right) => left.time - right.time);
  const latestValue = Number(currentValue);
  if (Number.isFinite(latestValue)) {
    const last = samples[samples.length - 1];
    if (last && Math.abs(last.time - endMs) > 1000) {
      samples.push({ time: endMs, value: latestValue });
    }
  }
  return samples;
}

function presenterGpuTone(index, gpu = {}) {
  if (Number(gpu.gpu_usage || 0) >= 85) return "hot";
  if (gpu.scheduled_occupied) return "busy";
  return index % 2 === 0 ? "cool" : "calm";
}

function renderTasks() {
  const tasks = state.data.tasks.items || [];
  const zones = [
    ["wait", "等待区"],
    ["running", "执行区"],
    ["history", "历史区"],
  ];
  const selected = selectedTask();
  return shell(`
    <section class="panel task-board">
      <div class="task-board-stack">
        <div class="task-board-head">
          <div>
            <h2>任务管理</h2>
            <span>${selected ? `已选择 ${escapeHtml(selected.task_id)}` : "请选择一条任务后执行管理操作"}</span>
          </div>
          <div class="task-action-bar">
            ${renderTaskActionButton("add", "添加任务")}
            ${renderTaskActionButton("batch", "批量添加")}
            ${renderTaskActionButton("edit", "修改选中任务")}
            ${renderTaskActionButton("hold", "挂起/取消挂起选中任务")}
            ${renderTaskActionButton("delete", "删除选中任务", "danger")}
            ${renderTaskActionButton("cancel", "中止选中任务", "danger")}
            ${renderTaskActionButton("resubmit", "重新提交")}
            ${renderTaskActionButton("historyAll", state.taskHistoryAllLoaded ? "已显示全部历史任务" : "查看所有历史任务")}
            ${renderTaskActionButton("log", "查看任务日志")}
          </div>
        </div>
        <div class="task-zone-rail task-zone-tabs">
          ${zones.map(([id, label]) => `<button class="secondary ${state.taskZone === id ? "active" : ""}" data-task-zone="${id}">${label}</button>`).join("")}
        </div>
        <div class="task-zone-content">
          <div class="task-list-summary">
            <strong>${zones.find(([id]) => id === state.taskZone)?.[1] || "当前分区"}</strong>
            <span>${state.taskListLoading ? "加载中..." : `共 ${state.data.tasks.total || tasks.length} 条`}</span>
          </div>
          ${state.taskListLoading ? renderEmpty("正在加载任务...") : (tasks.length ? renderTaskZoneTable(tasks) : renderEmpty(`${zones.find(([id]) => id === state.taskZone)?.[1] || "当前分区"}暂无任务`))}
        </div>
      </div>
    </section>
  `);
}

function renderTaskZoneTable(tasks) {
  const preserveKey = `task-list-${state.taskZone}`;
  const headers = ["", "状态", "任务ID", "环境", "路径", "命令", "节点", "GPU数", "GPU型号", "前驱", "紧急", "复用", "所有人", "时间"];
  const rows = tasks.map((task) => {
    const selected = state.selectedTaskId === task.task_id;
    const cells = [
      `<input type="radio" name="task_selection" value="${escapeAttr(task.task_id)}" ${selected ? "checked" : ""} data-select-task="${escapeAttr(task.task_id)}">`,
      `<span class="status ${task.state}">${taskStateText(task.state)}</span>`,
      `<strong>${escapeHtml(task.task_id)}</strong>${task.description ? `<br><span class="muted">${escapeHtml(task.description)}</span>` : ""}`,
      escapeHtml(task.env_name || "-"),
      `<code>${escapeHtml(task.workdir || "/")}</code>`,
      `<code class="task-command">${escapeHtml(task.command)}</code>${task.last_block_reason ? `<br><span class="muted">${escapeHtml(task.last_block_reason)}</span>` : ""}`,
      escapeHtml(task.node_name || (task.requirement?.node_id ? `#${task.requirement.node_id}` : "<任意>")),
      escapeHtml(task.requirement?.need_gpus ?? 0),
      escapeHtml(taskGpuModelText(task)),
      escapeHtml(task.predecessor_task_no || "(无)"),
      escapeHtml(task.urgent ? "是" : "否"),
      escapeHtml(task.requirement?.allow_gpu_reuse ? "是" : "否"),
      `${escapeHtml(task.owner_name || task.owner_username || "-")}`,
      renderTaskTimes(task),
    ];
    return `<tr class="task-row ${selected ? "selected" : ""}" data-task-row="${escapeAttr(task.task_id)}" title="双击查看日志">${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`;
  });
  return `
    <div class="table-wrap" data-preserve-scroll="${escapeAttr(preserveKey)}">
      <table>
        <thead><tr>${headers.map((header) => `<th>${header}</th>`).join("")}</tr></thead>
        <tbody>${rows.join("")}</tbody>
      </table>
    </div>
  `;
}

function renderTaskActionButton(action, label, tone = "secondary") {
  const disabled = isTaskActionDisabled(action);
  return `<button class="${tone}" data-task-action="${action}" ${disabled ? "disabled" : ""}>${label}</button>`;
}

function isTaskActionDisabled(action) {
  if (["add", "batch"].includes(action)) return !can("tasks:create");
  const zone = state.taskZone;
  if (action === "historyAll") return zone !== "history" || state.taskHistoryAllLoaded;
  const needsSelection = ["edit", "hold", "delete", "cancel", "resubmit", "log"].includes(action);
  if (needsSelection && !selectedTask()) return true;
  if (zone === "wait") return ["cancel", "resubmit"].includes(action);
  if (zone === "running") return ["edit", "hold", "delete", "resubmit"].includes(action);
  if (zone === "history") return ["hold", "cancel"].includes(action);
  return false;
}

function taskGpuModelText(task) {
  if (task.gpu_indices?.length) {
    const models = task.gpu_models?.length ? ` · ${task.gpu_models.join(", ")}` : "";
    return `GPU ${task.gpu_indices.join(", GPU ")}${models}`;
  }
  const requested = task.requirement?.gpu_types || [];
  return requested.length ? requested.join(", ") : "不限型号";
}

function renderTaskTimes(task) {
  return `
    <span class="muted">提交：${formatDate(task.created_at)}</span><br>
    <span class="muted">执行：${formatDate(task.started_at)}</span><br>
    <span class="muted">结束：${formatDate(task.finished_at)}</span>
  `;
}

function taskToDraft(mode, task = null) {
  return {
    mode,
    task_id: task?.task_id || "",
    description: task?.description || "",
    env_id: task?.env_id || "",
    workdir: task?.workdir || "/",
    command: task?.command || "",
    commands: "",
    node_id: task?.requirement?.node_id || "",
    need_gpus: task?.requirement?.need_gpus ?? 1,
    gpu_types: task?.requirement?.gpu_types || [],
    predecessor_task_id: task?.predecessor_task_id || "",
    urgent: Boolean(task?.urgent),
    allow_gpu_reuse: Boolean(task?.requirement?.allow_gpu_reuse),
    on_hold: mode === "batch" || Boolean(task?.on_hold),
  };
}

function captureTaskFormDraft(form) {
  return {
    mode: form.dataset.taskFormMode || "add",
    task_id: formValue(form, "task_id"),
    description: formValue(form, "description"),
    env_id: formValue(form, "env_id"),
    workdir: formValue(form, "workdir") || "/",
    command: form.elements.command?.value || "",
    commands: form.elements.commands?.value || "",
    node_id: formValue(form, "node_id"),
    need_gpus: formValue(form, "need_gpus") || 1,
    gpu_types: checkedValues("taskGpuTypeOptions", form),
    predecessor_task_id: formValue(form, "predecessor_task_id"),
    urgent: form.elements.urgent?.checked || false,
    allow_gpu_reuse: form.elements.allow_gpu_reuse?.checked || false,
    on_hold: form.elements.on_hold?.checked || false,
  };
}

function renderTaskForm(mode, task = null) {
  const draft = state.taskFormDraft || taskToDraft(mode, task);
  const isBatch = mode === "batch";
  const isEdit = mode === "edit";
  const envOptions = renderTaskEnvOptions(draft.env_id);
  const nodeOptions = renderTaskNodeOptions(draft.node_id);
  const gpuOptions = renderTaskGpuTypeOptions(draft.node_id || "", draft.gpu_types || [], draft.env_id || "");
  const predecessorOptions = renderTaskPredecessorOptions(draft.predecessor_task_id || "");
  const workdir = draft.workdir || "/";
  return `
    <form method="post" id="taskForm" class="task-edit-form" data-task-form-mode="${escapeAttr(mode)}">
      ${isEdit ? `<input type="hidden" name="task_id" value="${escapeAttr(draft.task_id)}">` : ""}
      <label>选择环境<select name="env_id" data-task-env-select><option value="">不指定</option>${envOptions}</select></label>
      <label>项目路径
        <input type="hidden" name="workdir" value="${escapeAttr(workdir)}">
        <span class="path-pick">
          <code id="taskWorkdirPreview">${escapeHtml(workdir)}</code>
          <button type="button" class="secondary" data-task-pick-workdir>选择文件夹</button>
        </span>
      </label>
      <label class="full-row">${isBatch ? "执行命令（一行一个）" : "执行命令"}
        ${isBatch
          ? `<textarea name="commands" required placeholder="python train.py --config /home/ddltm/data/user/${escapeAttr(state.user?.username || "user")}/project/config.yaml&#10;# 空行和注释会被忽略">${escapeHtml(draft.commands || "")}</textarea>`
          : `<input name="command" required value="${escapeAttr(draft.command || "")}" placeholder="python example.py --config ${escapeAttr(taskHomeHint())}/project/config.yaml">`}
        <span class="muted">用户文件夹绝对路径：${escapeHtml(taskHomeHint())}</span>
        <span class="muted">共享文件夹绝对路径：${escapeHtml(taskSharedHint())}</span>
      </label>
      <label>指定计算节点<select name="node_id" data-task-node-select><option value="">${escapeHtml("<任意>")}</option>${nodeOptions}</select></label>
      <label>需求的 GPU 数量<input name="need_gpus" type="number" min="0" max="16" value="${escapeAttr(draft.need_gpus ?? 1)}"></label>
      <div class="full-row task-gpu-picker">
        <span>指定 GPU 型号</span>
        <div id="taskGpuTypeOptions" class="task-check-grid">${gpuOptions}</div>
      </div>
      <label>前驱任务<select name="predecessor_task_id"><option value="">无</option>${predecessorOptions}</select></label>
      <label>任务描述<input name="description" value="${escapeAttr(draft.description || "")}" placeholder="可选"></label>
      <label class="check"><input name="urgent" type="checkbox" ${draft.urgent ? "checked" : ""}>紧急任务</label>
      <label class="check"><input name="allow_gpu_reuse" type="checkbox" ${draft.allow_gpu_reuse ? "checked" : ""}>复用 GPU</label>
      <label class="check"><input name="on_hold" type="checkbox" ${draft.on_hold ? "checked" : ""}>挂起任务</label>
      <div class="form-actions full-row">
        <button type="submit">${isEdit ? "提交修改" : "添加"}</button>
      </div>
    </form>
  `;
}

function renderTaskEnvOptions(selectedId = "") {
  return (state.data.envs || [])
    .filter(isEnvUsable)
    .map((env) => `<option value="${env.id}" ${String(selectedId || "") === String(env.id) ? "selected" : ""}>${escapeHtml(env.name)}</option>`)
    .join("");
}

function renderTaskNodeOptions(selectedId = "") {
  return (state.data.nodes || [])
    .map((node) => `<option value="${node.id}" ${String(selectedId || "") === String(node.id) ? "selected" : ""}>${escapeHtml(node.name)}</option>`)
    .join("");
}

function renderTaskGpuTypeOptions(nodeId = "", selectedTypes = [], envId = "") {
  const selected = new Set((selectedTypes || []).map(String));
  const nodes = nodeId ? (state.data.nodes || []).filter((node) => String(node.id) === String(nodeId)) : (state.data.nodes || []);
  const env = (state.data.envs || []).find((item) => String(item.id) === String(envId)) || null;
  const models = new Map();
  nodes.forEach((node) => {
    (node.gpus || [])
      .filter((gpu) => gpu.schedulable)
      .forEach((gpu) => {
        const model = gpu.model || "Unknown";
        const item = models.get(model) || { total: 0, free: 0, compatibilities: [] };
        item.total += 1;
        if (!gpu.scheduled_occupied) item.free += 1;
        item.compatibilities.push(pytorchGpuCompatibility(env, gpu));
        models.set(model, item);
      });
  });
  return models.size
    ? Array.from(models.entries())
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([model, count]) => {
        // 同型号物理卡继续汇总；如果状态不一致则展示较保守的等级，避免把潜在风险标成原生支持。
        const compatibility = summarizeGpuCompatibilities(count.compatibilities);
        const compatibilityMeta = gpuCompatibilityMeta(compatibility);
        const compatibilityBadge = compatibilityMeta
          ? `<span class="task-gpu-card__status ${compatibilityMeta.className}">${compatibilityMeta.label}</span>`
          : "";
        return `
          <label class="task-gpu-card${compatibilityMeta ? ` ${compatibilityMeta.cardClass}` : ""}">
            <input type="checkbox" value="${escapeAttr(model)}" ${selected.has(String(model)) ? "checked" : ""}>
            <span class="task-gpu-card__body">
              <strong class="task-gpu-card__title">${escapeHtml(model)}</strong>
              <span class="task-gpu-card__availability">${escapeHtml(count.free)} / ${escapeHtml(count.total)} 可用</span>
              ${compatibilityBadge}
            </span>
          </label>
        `;
      }).join("")
    : `<span class="muted">暂无可选 GPU 型号</span>`;
}

function renderTaskPredecessorOptions(selectedId = "") {
  if (state.taskPredecessorLoading) {
    return `<option value="${escapeAttr(selectedId || "")}" ${selectedId ? "selected" : ""}>加载中...</option>`;
  }
  return (state.taskPredecessorOptions || [])
    .map((task) => `<option value="${escapeAttr(task.task_id)}" ${String(selectedId || "") === String(task.task_id) ? "selected" : ""}>${escapeHtml(task.task_id)} · ${escapeHtml(task.description || taskStateText(task.state))}</option>`)
    .join("");
}

function taskHomeHint() {
  if (state.user?.home_path) return state.user.home_path;
  if (state.user?.role === "admin") return "/home/ddltm";
  return `/home/ddltm/data/user/${state.user?.username || "user"}`;
}

function pytorchGpuCompatibility(env, gpu) {
  const capability = parseGpuComputeCapability(gpu?.compute_capability);
  if (!env?.pytorch_version || !capability) return "unknown";
  const targets = (env.pytorch_arch_list || [])
    .map(parseTorchSmTarget)
    .filter(Boolean);
  if (targets.some((target) => target.major === capability.major && target.minor === capability.minor)) {
    return "native_supported";
  }
  // 普通 cubin 只在同一主版本内向更高次版本兼容；a/f 等专用后缀不能套用该规则。
  if (targets.some((target) => !target.suffix && target.major === capability.major && target.minor <= capability.minor)) {
    return "same_major_compatible";
  }
  // compute_xx/PTX 即使存在也不参与产品兼容判断，避免把理论可 JIT 误报为可用。
  return "unsupported";
}

function parseTorchSmTarget(value) {
  const match = String(value || "").trim().toLowerCase().match(/^sm_(\d+)([a-z]*)$/);
  if (!match) return null;
  const architecture = Number(match[1]);
  return {
    major: Math.floor(architecture / 10),
    minor: architecture % 10,
    suffix: match[2],
  };
}

function parseGpuComputeCapability(value) {
  const match = String(value || "").trim().match(/^(\d+)\.(\d+)$/);
  return match ? { major: Number(match[1]), minor: Number(match[2]) } : null;
}

function summarizeGpuCompatibilities(statuses = []) {
  if (!statuses.length) return "unknown";
  return ["unsupported", "unknown", "same_major_compatible", "native_supported"]
    .find((status) => statuses.includes(status)) || "unknown";
}

function gpuCompatibilityMeta(status) {
  return {
    native_supported: { label: "原生支持", className: "is-native", cardClass: "compat-native" },
    same_major_compatible: { label: "同主版本兼容", className: "is-compatible", cardClass: "compat-same-major" },
    unsupported: { label: "不支持", className: "is-unsupported", cardClass: "compat-unsupported" },
  }[status] || null;
}

function taskSharedHint() {
  return state.data.runtimeConfig?.shared_folder_root || "/home/ddltm/shared";
}

function schedulableGpus(node) {
  return (node?.gpus || []).filter((gpu) => gpu.schedulable);
}

function renderFiles() {
  const files = state.data.files.items || [];
  const currentPath = state.data.files.path || "/";
  const currentDisplayPath = state.data.files.display_path || currentPath;
  const selected = state.data.selectedFilePath;
  const preview = state.data.preview;
  const studentView = isStudentFileView();
  const sharedView = isSharedFileView();
  const readOnlyView = isReadOnlyFileView();
  const studentFilesButton = canViewStudentFiles()
    ? `<button class="secondary ${studentView ? "active" : ""}" data-file-students>${studentView ? "查看我的文件" : "查看学生文件"}</button>`
    : "";
  const sharedFilesButton = `<button class="secondary ${sharedView ? "active" : ""}" data-file-shared>${sharedView ? "查看我的文件" : "共享文件夹"}</button>`;
  return shell(`
    <section class="file-manager">
      <aside class="file-sidebar-panel">
        <div class="file-nav-row">
          <button class="secondary" data-file-root>根目录</button>
          <button class="secondary" data-file-up>上级</button>
          <button class="secondary" data-action="refresh">刷新</button>
          ${studentFilesButton}
          ${sharedFilesButton}
        </div>
        <div class="file-path">${escapeHtml(currentDisplayPath)}</div>
        <div class="file-list" role="list" data-preserve-scroll="file-list">
          ${files.length ? files.map((item) => `
            <div class="file-row ${selected === item.path ? "active" : ""}" data-select-file="${escapeAttr(item.path)}" data-file-kind="${escapeAttr(item.type)}">
              <span class="file-glyph">${fileIcon(item)}</span>
              <span class="file-name">${escapeHtml(item.name)}</span>
              <span class="file-type">${item.type === "directory" ? "dir" : formatBytes(item.size_bytes)}</span>
              ${item.type === "directory" ? `<button class="small secondary file-enter" data-open-path="${escapeAttr(item.path)}">进入</button>` : ""}
            </div>
          `).join("") : `<div class="file-empty">当前目录为空</div>`}
        </div>
        ${readOnlyView ? "" : `<div class="file-actions-grid">
          <button class="secondary" data-file-new-folder>新建文件夹</button>
          <button class="secondary" data-file-new-file>新建文件</button>
          <button class="secondary" data-file-rename>重命名</button>
          <button class="secondary" data-file-copy>复制到</button>
          <button class="secondary" data-file-copy-shared>复制到共享文件夹</button>
          <button class="secondary" data-file-move>移动到</button>
          <button class="secondary" data-file-archive>打包成 zip</button>
          <button class="secondary" data-file-extract>解压压缩包</button>
          <button class="danger" data-file-delete>删除</button>
        </div>`}
        ${readOnlyView ? `<div class="file-transfer-row">${sharedView ? `<button type="button" class="secondary" data-file-copy-own>复制到我的文件夹</button>` : ""}<button type="button" class="secondary" data-file-download>下载选中</button></div>` : `<form id="fileUploadForm" class="file-upload-form">
          <input name="file" type="file">
          <div class="file-transfer-row">
            <button type="submit">上传到当前目录</button>
            <button type="button" class="secondary" data-file-download>下载选中</button>
          </div>
        </form>`}
      </aside>
      <section class="file-editor-panel">
        <div class="file-editor-toolbar">
          <span class="status">${preview?.path ? escapeHtml(displayFilePath(preview.path)) : "未打开文件"}</span>
          <button data-file-save ${preview?.encoding === "text" && !readOnlyView ? "" : "disabled"}>保存</button>
        </div>
        ${renderFilePreview(preview, readOnlyView)}
        ${renderFilePermissionPanel(preview, readOnlyView)}
        ${renderFileJobProgress(state.data.fileJob)}
      </section>
    </section>
  `);
}

function renderFilePreview(preview, readOnly = false) {
  if (!preview) {
    return `<textarea class="file-editor" disabled placeholder="${readOnly ? "选择左侧文件后可在这里预览" : "选择左侧文本文件后可在这里预览或编辑"}"></textarea>`;
  }
  if (preview.encoding === "text") {
    return `<div class="file-editor-shell"><textarea id="fileEditor" class="file-editor" spellcheck="false" ${readOnly ? "readonly" : ""}>${escapeHtml(preview.content || "")}</textarea>${preview.truncated ? `<p class="file-note">文件较大，仅显示前 ${formatBytes(preview.content.length)}。</p>` : ""}</div>`;
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

function renderFilePermissionPanel(preview, readOnly = false) {
  const disabled = !preview?.path || readOnly || preview.main_user_can_execute;
  const mode = preview?.mode_octal || "----";
  const mainUser = preview?.main_user || "主账户";
  const mainStatus = preview?.main_user_can_execute ? "可执行" : "未授权";
  const bitText = preview?.path
    ? `属主 ${preview.owner_executable ? "x" : "-"} · 用户组 ${preview.group_executable ? "x" : "-"} · 其他 ${preview.other_executable ? "x" : "-"}`
    : "未打开文件";
  return `
    <div class="file-permission-panel">
      <div class="file-permission-head">
        <strong>文件权限</strong>
        <code>${escapeHtml(mode)}</code>
      </div>
      <div class="file-permission-grid">
        <span>执行位</span>
        <strong>${escapeHtml(bitText)}</strong>
        <span>${escapeHtml(mainUser)}</span>
        <strong>${escapeHtml(mainStatus)}</strong>
      </div>
      <button class="secondary" data-file-grant-exec ${disabled ? "disabled" : ""}>授予执行权限</button>
    </div>
  `;
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
        <span>${fileJobStateText(job.state)} · ${progress}%</span>
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
  const items = picker.items || [];
  const action = fileTargetActionText(picker.mode);
  const isEnvImport = picker.mode === "env-import";
  const isEnvPackage = picker.mode === "env-package";
  const isTaskWorkdir = picker.mode === "task-workdir";
  const targetPath = isTaskWorkdir ? picker.currentPath : (isEnvImport ? picker.sourcePath : (isEnvPackage ? (picker.selectKind === "directory" ? picker.currentPath : picker.sourcePath) : buildPickedTargetPath(picker.sourcePath, picker.currentPath, picker.mode)));
  const invalidTarget = !targetPath;
  const fileGlyph = isEnvImport ? "ZIP" : "PKG";
  const filePickerHint = picker.selectKind === "directory" ? "当前目录会作为选中的文件夹" : "请选择安装文件";
  return `
    <div class="modal-backdrop">
      <section class="file-picker-modal" role="dialog" aria-modal="true" aria-labelledby="filePickerTitle">
        <div class="file-picker-head">
          <div>
            <h2 id="filePickerTitle">${action}</h2>
            <span>${escapeHtml(isTaskWorkdir ? "请选择任务项目文件夹" : (isEnvImport ? "请选择用户根目录下的环境 zip 包" : (isEnvPackage ? filePickerHint : baseName(picker.sourcePath))))}</span>
          </div>
          <button class="secondary" data-file-picker-close>关闭</button>
        </div>
        <div class="file-picker-current">
          <span>${isEnvImport || isEnvPackage || isTaskWorkdir ? "当前目录" : "目标目录"}</span>
          <strong>${escapeHtml(picker.currentPath)}</strong>
        </div>
        <div class="file-picker-nav">
          <button class="secondary" data-file-picker-root>根目录</button>
          <button class="secondary" data-file-picker-up>上级</button>
        </div>
        <div class="file-picker-list" data-preserve-scroll="file-picker-list">
          ${items.length ? items.map((item) => item.type === "directory" ? `
            <button class="file-picker-row" data-file-picker-open="${escapeAttr(item.path)}">
              <span class="file-glyph">DIR</span>
              <span>${escapeHtml(item.name)}</span>
            </button>
          ` : `
            <button class="file-picker-row ${picker.sourcePath === item.path ? "active" : ""}" data-file-picker-select="${escapeAttr(item.path)}">
              <span class="file-glyph">${fileGlyph}</span>
              <span>${escapeHtml(item.name)}</span>
            </button>
          `).join("") : `<div class="file-empty">${isTaskWorkdir ? "当前目录下没有子文件夹" : (isEnvImport ? "当前目录下没有 zip 包或子文件夹" : (isEnvPackage ? "当前目录下没有可选择项目" : "当前目录下没有子文件夹"))}</div>`}
        </div>
        <div class="file-picker-target">
          <span>${isTaskWorkdir ? "选中项目路径" : (isEnvPackage && picker.selectKind === "directory" ? "选中文件夹" : (isEnvImport || isEnvPackage ? "已选择" : "将生成"))}</span>
          <code>${escapeHtml(targetPath || (isTaskWorkdir ? "请选择项目文件夹" : (isEnvImport ? "请选择 zip 包" : (isEnvPackage ? "请选择文件或文件夹" : "不能选择当前目标目录"))))}</code>
        </div>
        <div class="file-picker-actions">
          <button class="secondary" data-file-picker-close>取消</button>
          <button data-file-picker-confirm ${invalidTarget ? "disabled" : ""}>${isTaskWorkdir ? "使用此路径" : (isEnvImport ? "导入此环境" : (isEnvPackage ? "确认选择" : "选择此文件夹"))}</button>
        </div>
      </section>
    </div>
  `;
}

function renderEnvs() {
  const envs = state.data.envs || [];
  return shell(`
    <section class="panel">
      <div class="panel-head">
        <div><h2>环境管理</h2><span>刷新读取数据库中的环境；导入会扫描 conda envs 目录并写入数据库，base 环境不显示。</span></div>
        <div class="top-actions">
          <button class="secondary" data-action="refresh">刷新环境列表</button>
          <button data-import-envs ${can("envs:write") ? "" : "disabled"}>导入环境</button>
        </div>
      </div>
      ${envs.length ? renderTable(["名称", "来源", "状态", "路径", "版本", "操作"], envs.map((env) => [
        renderEnvName(env),
        escapeHtml(envSourceText(env.source_type)),
        `<span class="status ${env.state}">${envStateText(env.state)}</span>`,
        `<code>${escapeHtml(env.path)}</code>`,
        renderEnvVersion(env),
        renderEnvActions(env),
      ])) : renderEmpty("暂无环境")}
    </section>
  `);
}

function renderEnvVersion(env) {
  const torchLine = env.pytorch_version
    ? `<br><span class="muted">PyTorch ${escapeHtml(env.pytorch_version)} · CUDA ${escapeHtml(env.pytorch_cuda_version || "-")}</span>`
    : "";
  const archLine = (env.pytorch_arch_list || []).length
    ? `<br><span class="muted">${escapeHtml(env.pytorch_arch_list.join(", "))}</span>`
    : "";
  return `${escapeHtml(env.python_version || "-")}${torchLine}${archLine}`;
}

function renderEnvActions(env) {
  const usable = isEnvUsable(env);
  const testAttrs = usable ? "" : "disabled title=\"环境尚不可用\"";
  const logAttrs = canViewEnvLog(env) ? "" : "disabled title=\"无日志查看权限\"";
  const cloneAttrs = can("envs:write") && usable ? "" : "disabled title=\"环境尚不可用或无创建权限\"";
  const modifyAttrs = can("envs:write") && env.can_modify && usable ? "" : "disabled title=\"无修改权限或环境尚不可用\"";
  const deleteAttrs = canDeleteEnv(env) && !isEnvBusy(env) ? "" : "disabled title=\"无删除权限或环境正在处理\"";
  return `
    <button class="small secondary" data-test-env="${env.id}" ${testAttrs}>检测</button>
    <button class="small secondary" data-env-log="${env.id}" ${logAttrs}>查看日志</button>
    <button class="small secondary" data-clone-env="${env.id}" ${cloneAttrs}>创建副本</button>
    <button class="small secondary" data-install-package-env="${env.id}" ${modifyAttrs}>安装包</button>
    <button class="small secondary" data-delete-package-env="${env.id}" ${modifyAttrs}>删除包</button>
    <button class="small danger" data-delete-env="${env.id}" ${deleteAttrs}>删除环境</button>
  `;
}

function renderEnvName(env) {
  const ownerLine = env.owner_name ? `<br><span class="muted">所有人：${escapeHtml(env.owner_name)}</span>` : "";
  const descriptionLine = env.description ? `<br><span class="muted">${escapeHtml(env.description)}</span>` : "";
  return `<strong>${escapeHtml(env.name)}</strong>${ownerLine}${descriptionLine}`;
}

function canDeleteEnv(env) {
  return state.user?.role === "admin" || Boolean(env.can_modify);
}

function canViewEnvLog(env) {
  return state.user?.role === "admin" || String(env?.owner_user_id) === String(state.user?.id);
}

function isEnvUsable(env) {
  return ["available", "registered"].includes(env?.state);
}

function isEnvBusy(env) {
  return ["copying", "importing", "fixing", "testing", "installing"].includes(env?.state);
}

function envSourceText(value) {
  const map = {
    system_imported: "系统导入",
    user_imported: "用户导入",
    user_clone: "环境副本",
    conda_pack: "conda-pack",
    registered: "登记",
  };
  return map[value] || value || "-";
}

function renderEnvPackageInstallPanel() {
  const panel = state.envPackageInstall || {};
  const packages = panel.packages || [];
  const topPackages = packages.slice(0, 300);
  const method = panel.method || "conda";
  const pipMode = panel.pipMode || "wheel";
  const installing = panel.installStatus === "installing";
  return `
    <div class="env-package-panel">
      <form id="envPackageInstallForm" class="env-package-form">
        <div class="env-install-warning">系统不会处理依赖，请自行解决依赖。</div>
        <div class="env-install-state ${installing ? "installing" : "ready"}">
          <span class="status ${installing ? "installing" : "available"}">${installing ? "安装中" : "就绪"}</span>
          <strong>${installing ? "安装命令正在目标环境中执行，请不要关闭此页面。" : "选择安装方式和安装资源后即可执行安装。"}</strong>
        </div>
        <section class="env-install-methods">
          <label><input type="radio" name="method" value="conda" ${method === "conda" ? "checked" : ""} ${installing ? "disabled" : ""}> 安装 conda 包</label>
          <label><input type="radio" name="method" value="pip" ${method === "pip" ? "checked" : ""} ${installing ? "disabled" : ""}> pip 包</label>
        </section>
        ${method === "conda" ? `
        <section class="env-install-section" data-install-section="conda">
          <h3>安装 conda 包</h3>
          <p class="muted">选择离线 .tar.bz2 包，系统执行 <code>conda install --offline</code>。</p>
          ${renderPathPick("Conda 包", panel.packagePath, "packagePath", "file", ".tar.bz2")}
        </section>
        ` : ""}
        ${method === "pip" ? `
        <section class="env-install-section" data-install-section="pip">
          <h3>pip 包</h3>
          <div class="env-install-methods compact">
            <label><input type="radio" name="pip_mode" value="wheel" ${pipMode !== "folder" ? "checked" : ""} ${installing ? "disabled" : ""}> 安装 whl 包</label>
            <label><input type="radio" name="pip_mode" value="folder" ${pipMode === "folder" ? "checked" : ""} ${installing ? "disabled" : ""}> 安装文件夹</label>
          </div>
          ${pipMode !== "folder" ? `
          <div class="env-install-subsection">
            <label class="checkline"><input type="checkbox" name="batch" ${panel.batch ? "checked" : ""} ${installing ? "disabled" : ""}> 批量安装模式</label>
            <p class="muted">单包模式执行 <code>pip install --no-index xxx.whl</code>；批量模式执行 <code>pip install --no-index --find-links=&lt;folder&gt; -r requirements.txt</code>。</p>
            ${panel.batch ? `
              ${renderPathPick("包所在文件夹", panel.folderPath, "folderPath", "directory")}
              ${renderPathPick("requirements.txt", panel.requirementsPath, "requirementsPath", "file", ".txt")}
            ` : renderPathPick("whl 包", panel.packagePath, "packagePath", "file", ".whl")}
          </div>
          ` : `
          <div class="env-install-subsection">
            <p class="muted">选择源码目录，默认执行 <code>pip install .</code>，也可选择 <code>python setup.py install</code>。</p>
            ${renderPathPick("目标文件夹", panel.folderPath, "folderPath", "directory")}
            <label>安装命令
              <select name="folder_command" ${installing ? "disabled" : ""}>
                <option value="pip" ${panel.folderCommand !== "setup_py" ? "selected" : ""}>pip install .</option>
                <option value="setup_py" ${panel.folderCommand === "setup_py" ? "selected" : ""}>python setup.py install</option>
              </select>
            </label>
          </div>
          `}
        </section>
        ` : ""}
        <section class="env-install-section">
          <h3>指定节点编译</h3>
          <div class="compile-target-summary">${renderCompileTargetSummary(panel)}</div>
          <div class="compile-target-actions">
            <button type="button" class="secondary" data-open-env-compile ${installing ? "disabled" : ""}>在指定节点</button>
            ${panel.compileTarget ? `<button type="button" class="secondary" data-clear-env-compile ${installing ? "disabled" : ""}>清除</button>` : ""}
          </div>
        </section>
        <div class="form-actions">
          <button type="submit" ${installing ? "disabled" : ""}>${installing ? "安装中" : "执行安装"}</button>
        </div>
      </form>
      <aside class="env-package-list">
        <div class="panel-head compact"><div><h3>现有包</h3><span>共 ${escapeHtml(panel.packageCount || packages.length)} 个，显示前 ${escapeHtml(topPackages.length)} 个</span></div></div>
        ${topPackages.length ? renderTable(["包名", "版本", "来源"], topPackages.map((item) => [
          escapeHtml(item.name || "-"),
          escapeHtml(item.version || "-"),
          renderPackageSource(item.source),
        ])) : renderEmpty("暂无包信息")}
      </aside>
    </div>
  `;
}

function renderCompileTargetSummary(panel = {}) {
  const target = panel.compileTarget;
  if (!target) return `<span class="muted">未指定时在主节点按默认 CUDA 可见性执行。</span>`;
  const mode = panel.gpuVisibility || "default";
  const gpuText = mode === "cpu"
    ? "CPU"
    : (mode === "gpu" ? `GPU ${formatGpuIndices(panel.visibleGpuIndices || [])}` : "默认");
  return `
    <dl class="kv compact-kv">
      <dt>节点</dt><dd>${escapeHtml(target.name || "-")}<span class="muted"> ${escapeHtml(target.ip || "")}</span></dd>
      <dt>GPU</dt><dd>${escapeHtml(gpuText)}</dd>
    </dl>
  `;
}

function renderEnvCompilePicker() {
  const picker = state.envCompilePicker || {};
  const targets = picker.targets || [];
  const selected = targets.find((item) => item.id === picker.selectedTargetId) || targets[0] || null;
  const gpus = selected?.gpus || [];
  const selectedGpuIndices = new Set((picker.visibleGpuIndices || []).map(Number));
  return `
    <div class="modal-backdrop">
      <section class="compile-picker-modal" role="dialog" aria-modal="true" aria-labelledby="compilePickerTitle">
        <div class="file-picker-head">
          <div>
            <h2 id="compilePickerTitle">在指定节点执行安装</h2>
            <span>节点信息为本次打开弹窗时实时探测。</span>
          </div>
          <button class="secondary" data-compile-picker-close>关闭</button>
        </div>
        ${picker.loading ? renderEmpty("正在探测节点编译环境...") : `
          <div class="compile-picker-grid">
            <div class="compile-target-list">
              ${targets.length ? targets.map((target) => renderCompileTargetOption(target, selected?.id)).join("") : renderEmpty("暂无可见节点")}
            </div>
            <div class="compile-gpu-panel">
              ${selected ? `
                <div class="compile-target-detail">
                  <h3>${escapeHtml(selected.name)}</h3>
                  <span class="muted">${escapeHtml(selected.ip || "-")} · ${escapeHtml(selected.is_master ? "主节点" : nodeStateText(selected.state))}</span>
                </div>
                ${renderCompilerGrid(selected.compilers || {})}
                <div class="compile-gpu-modes">
                  <button type="button" class="secondary ${picker.gpuVisibility === "default" ? "active" : ""}" data-compile-gpu-mode="default">默认</button>
                  <button type="button" class="secondary ${picker.gpuVisibility === "cpu" ? "active" : ""}" data-compile-gpu-mode="cpu">CPU</button>
                </div>
                <div class="compile-gpu-list">
                  ${gpus.length ? gpus.map((gpu) => `
                    <label class="compile-gpu-option">
                      <input type="checkbox" value="${escapeAttr(gpu.index)}" data-compile-gpu-index="${escapeAttr(gpu.index)}" ${selectedGpuIndices.has(Number(gpu.index)) ? "checked" : ""}>
                      <span>GPU ${escapeHtml(gpu.index)} · ${escapeHtml(gpu.model || "-")}</span>
                      <small>${escapeHtml(gpu.total_vram_mb ? `${gpu.total_vram_mb} MB` : "")}</small>
                    </label>
                  `).join("") : renderEmpty("该节点未探测到 GPU")}
                </div>
                ${selected.error ? `<div class="env-test-error">${escapeHtml(selected.error)}</div>` : ""}
              ` : renderEmpty("请选择节点")}
            </div>
          </div>
        `}
        <div class="file-picker-actions">
          <button class="secondary" data-compile-picker-refresh ${picker.loading ? "disabled" : ""}>重新探测</button>
          <button data-compile-picker-confirm ${picker.loading || !selected ? "disabled" : ""}>确认</button>
        </div>
      </section>
    </div>
  `;
}

function renderCompileTargetOption(target, selectedId) {
  const compilers = target.compilers || {};
  const compilerText = ["gcc", "g++", "clang", "nvcc"].map((name) => `${name}: ${compilers[name] || "未安装"}`).join(" / ");
  return `
    <label class="compile-target-option ${target.id === selectedId ? "active" : ""}">
      <input type="radio" name="compile_target" value="${escapeAttr(target.id)}" data-compile-target-select ${target.id === selectedId ? "checked" : ""}>
      <span><strong>${escapeHtml(target.name)}</strong><small>${escapeHtml(target.ip || "-")}</small></span>
      <em>${escapeHtml(compilerText)}</em>
    </label>
  `;
}

function renderCompilerGrid(compilers = {}) {
  return `
    <div class="compiler-grid">
      ${["gcc", "g++", "clang", "nvcc"].map((name) => `
        <span><b>${escapeHtml(name)}</b><strong>${escapeHtml(compilers[name] || "未安装")}</strong></span>
      `).join("")}
    </div>
  `;
}

function formatGpuIndices(indices = []) {
  return indices.length ? indices.join(", ") : "-";
}

function renderPathPick(label, value, field, kind, extensions = "") {
  const disabled = state.envPackageInstall?.installStatus === "installing";
  return `
    <label class="path-pick-label">${escapeHtml(label)}</label>
    <div class="path-pick">
      <code>${escapeHtml(value || "未选择")}</code>
      <button type="button" class="secondary" data-env-package-pick="${field}" data-pick-kind="${kind}" data-pick-ext="${escapeAttr(extensions)}" ${disabled ? "disabled" : ""}>选择</button>
    </div>
  `;
}

function renderEnvPackageInstallResult(result = {}) {
  const isJob = Boolean(result.status) && result.ok === undefined;
  const statusText = isJob ? envInstallJobStatusLabel(result.status) : (result.ok ? "成功" : "失败");
  const statusClass = isJob ? envInstallJobStatusClass(result.status) : (result.ok ? "available" : "failed");
  return `
    <div class="env-install-result">
      <dl class="kv">
        <dt>状态</dt><dd><span class="status ${statusClass}">${escapeHtml(statusText)}</span></dd>
        <dt>环境</dt><dd>${escapeHtml(result.env_name || (result.env_id ? `#${result.env_id}` : "-"))}</dd>
        <dt>方式</dt><dd>${escapeHtml(result.method || result.mode || "-")}</dd>
        ${isJob ? `<dt>作业</dt><dd>#${escapeHtml(result.id || "-")}</dd>` : ""}
        <dt>返回码</dt><dd>${escapeHtml(result.return_code ?? "-")}</dd>
        <dt>日志</dt><dd><code>${escapeHtml(result.log_path || "-")}</code></dd>
      </dl>
      ${isJob ? "" : `
        <h3>命令</h3>
        <pre class="drawer-log">${escapeHtml(result.command || "")}</pre>
        <h3>stdout</h3>
        <pre class="drawer-log">${escapeHtml(result.stdout || "")}</pre>
        <h3>stderr</h3>
        <pre class="drawer-log">${escapeHtml(result.stderr || "")}</pre>
      `}
    </div>
  `;
}

function envInstallJobStatusLabel(status) {
  return ({ queued: "排队中", running: "安装中", succeeded: "成功", failed: "失败", cancelled: "已取消" })[status] || status || "-";
}

function envInstallJobStatusClass(status) {
  if (status === "succeeded") return "available";
  if (["failed", "cancelled"].includes(status)) return "failed";
  return "running";
}

function renderEnvPackageDeletePanel() {
  const panel = state.envPackageDelete || {};
  const packages = panel.packages || [];
  const selected = new Set(panel.selectedPackageNames || []);
  const topPackages = packages.slice(0, 500);
  const deletableCount = packages.filter((item) => !isProtectedEnvPackage(item)).length;
  return `
    <form id="envPackageDeleteForm" class="env-package-delete-panel">
      <div class="env-install-warning">删除包不会自动修复依赖关系，请确认没有任务依赖这些包。</div>
      <div class="panel-head compact">
        <div><h3>现有包</h3><span>共 ${escapeHtml(panel.packageCount || packages.length)} 个，显示前 ${escapeHtml(topPackages.length)} 个；可删除 ${escapeHtml(deletableCount)} 个</span></div>
      </div>
      <div class="env-package-delete-list" data-preserve-scroll="env-package-delete-list">
        ${topPackages.length ? renderTable(["选择", "包名", "版本", "来源", "状态"], topPackages.map((item) => {
        const protectedPackage = isProtectedEnvPackage(item);
        return [
          `<input type="checkbox" value="${escapeAttr(item.name || "")}" data-delete-package-select ${selected.has(item.name) ? "checked" : ""} ${protectedPackage ? "disabled" : ""}>`,
          `<strong>${escapeHtml(item.name || "-")}</strong>`,
          escapeHtml(item.version || "-"),
          renderPackageSource(item.source),
          protectedPackage ? `<span class="status disabled">环境默认包</span>` : `<span class="status available">可删除</span>`,
        ];
        })) : renderEmpty("暂无包信息")}
      </div>
      <div class="package-delete-actions">
        <span class="muted">已选择 ${escapeHtml(selected.size)} 个包</span>
        <button type="submit" class="danger" ${selected.size ? "" : "disabled"}>删除选中包</button>
      </div>
    </form>
  `;
}

function renderEnvPackageDeleteResult(result = {}) {
  const packages = result.packages || [];
  return `
    <div class="env-install-result">
      <dl class="kv">
        <dt>状态</dt><dd><span class="status ${result.ok ? "available" : "failed"}">${result.ok ? "成功" : "失败"}</span></dd>
        <dt>环境</dt><dd>${escapeHtml(result.env_name || "-")}</dd>
        <dt>返回码</dt><dd>${escapeHtml(result.return_code ?? "-")}</dd>
        <dt>日志</dt><dd><code>${escapeHtml(result.log_path || "-")}</code></dd>
      </dl>
      <h3>删除包</h3>
      ${packages.length ? renderTable(["包名", "版本", "来源"], packages.map((item) => [
        escapeHtml(item.name || "-"),
        escapeHtml(item.version || "-"),
        renderPackageSource(item.source),
      ])) : renderEmpty("无")}
      <h3>命令</h3>
      <pre class="drawer-log">${escapeHtml((result.commands || []).join("\n"))}</pre>
      <h3>stdout</h3>
      <pre class="drawer-log">${escapeHtml(result.stdout || "")}</pre>
      <h3>stderr</h3>
      <pre class="drawer-log">${escapeHtml(result.stderr || "")}</pre>
    </div>
  `;
}

function renderEnvOperationLog(logText = "") {
  const lines = String(logText || "").split(/\r?\n/).filter((line) => line.trim());
  if (!lines.length) return renderEmpty("暂无环境日志");
  return `
    <div class="env-log-viewer">
      ${lines.map((line) => {
        const entry = parseEnvLogLine(line);
        return entry ? renderEnvLogEntry(entry) : `<pre class="drawer-log">${escapeHtml(unescapeLogText(line))}</pre>`;
      }).join("")}
    </div>
  `;
}

function parseEnvLogLine(line) {
  try {
    const entry = JSON.parse(line);
    return entry && typeof entry === "object" ? entry : null;
  } catch {
    return null;
  }
}

function renderEnvLogEntry(entry) {
  const title = entry.message || entry.action || "日志";
  const meta = [
    entry.time,
    entry.env_name ? `env=${entry.env_name}` : "",
    entry.method ? `method=${entry.method}` : "",
    entry.user_id !== undefined ? `user=${entry.user_id}` : "",
    entry.return_code !== undefined ? `code=${entry.return_code}` : "",
  ].filter(Boolean).join(" · ");
  const detailRows = Object.entries(entry)
    .filter(([key]) => !["time", "env_id", "env_name", "action", "message", "method", "user_id", "return_code"].includes(key))
    .map(([key, value]) => renderEnvLogDetail(key, value))
    .join("");
  return `
    <article class="env-log-entry">
      <div class="env-log-entry-head">
        <strong>${escapeHtml(unescapeLogText(title))}</strong>
        <span>${escapeHtml(unescapeLogText(meta))}</span>
      </div>
      ${detailRows ? `<div class="env-log-details">${detailRows}</div>` : ""}
    </article>
  `;
}

function renderEnvLogDetail(key, value) {
  if (value === null || value === undefined || value === "") return "";
  const text = typeof value === "string" ? unescapeLogText(value) : JSON.stringify(value, null, 2);
  return `
    <div class="env-log-detail">
      <span>${escapeHtml(key)}</span>
      <pre>${escapeHtml(text)}</pre>
    </div>
  `;
}

function unescapeLogText(value) {
  return String(value ?? "").replaceAll("\\r\\n", "\n").replaceAll("\\n", "\n").replaceAll("\\t", "\t");
}

function renderPackageSource(source) {
  const normalized = source === "pip" ? "pip" : "conda";
  return `<span class="package-source ${normalized}">${normalized}</span>`;
}

function isProtectedEnvPackage(item = {}) {
  return Boolean(item.protected) || protectedEnvPackageNames.has(normalizePackageName(item.name || ""));
}

function normalizePackageName(name) {
  return String(name || "").trim().toLowerCase().replace(/[-_.]+/g, "-");
}

function renderEnvTestResult(result = {}) {
  const packages = result.packages || [];
  const topPackages = packages.slice(0, 300);
  return `
    <div class="env-test">
      ${result.ok ? "" : `<div class="env-test-error">${escapeHtml(result.error || "检测失败")}</div>`}
      <dl class="kv env-kv">
        <dt>环境</dt><dd><strong>${escapeHtml(result.env_name || "-")}</strong></dd>
        <dt>路径</dt><dd><code>${escapeHtml(result.env_path || "-")}</code></dd>
        <dt>Python</dt><dd>${escapeHtml(result.python_version || "-")}</dd>
        <dt>解释器</dt><dd><code>${escapeHtml(result.python_executable || "-")}</code></dd>
      </dl>
      <div class="framework-grid">
        ${renderFrameworkCard("PyTorch", result.pytorch)}
        ${renderFrameworkCard("TensorFlow", result.tensorflow)}
      </div>
      <div class="panel-head compact">
        <div><h3>包列表</h3><span>共 ${escapeHtml(result.package_count ?? packages.length)} 个包${packages.length > topPackages.length ? "，当前仅展示前 300 个" : ""}</span></div>
      </div>
      ${topPackages.length ? renderTable(["包名", "版本", "来源"], topPackages.map((item) => [
        escapeHtml(item.name || "-"),
        escapeHtml(item.version || "-"),
        renderPackageSource(item.source),
      ])) : renderEmpty("暂无包信息")}
    </div>
  `;
}

function renderFrameworkCard(name, info = {}) {
  const installed = Boolean(info?.installed);
  return `
    <article class="framework-card ${installed ? "installed" : "missing"}">
      <div class="framework-head">
        <h3>${escapeHtml(name)}</h3>
        <span class="status ${installed ? "available" : "disabled"}">${installed ? "已安装" : "未安装"}</span>
      </div>
      <dl class="kv">
        <dt>版本</dt><dd>${escapeHtml(info?.version || "-")}</dd>
        <dt>CUDA</dt><dd>${escapeHtml(info?.cuda || "-")}</dd>
        <dt>cuDNN</dt><dd>${escapeHtml(info?.cudnn ?? "-")}</dd>
        <dt>GPU</dt><dd>${info?.cuda_available === null || info?.cuda_available === undefined ? "-" : (info.cuda_available ? `可用，${escapeHtml(info.gpu_count ?? 0)} 张` : "不可用")}</dd>
        ${(info?.arch_list || []).length ? `<dt>GPU 架构</dt><dd>${escapeHtml(info.arch_list.join(", "))}</dd>` : ""}
      </dl>
      ${info?.error ? `<p class="framework-error">${escapeHtml(info.error)}</p>` : ""}
    </article>
  `;
}

function renderManual() {
  const manual = state.data.manual;
  const rendered = manual ? renderMarkdownDocument(manual.content) : { html: renderEmpty("手册加载中"), toc: [] };
  return shell(`
    <section class="panel manual-shell">
      <div class="panel-head">
        <div>
          <h2>${escapeHtml(manual?.title || "使用手册")}</h2>
          <span>${escapeHtml(manual?.source_path || "guides/student_manual.md")} · ${roleName(manual?.role || state.user?.role)}</span>
        </div>
      </div>
      <div class="manual-layout">
        <aside class="manual-toc" aria-label="使用手册目录">
          <strong>目录</strong>
          ${rendered.toc.length
            ? rendered.toc.map((item) => `<button type="button" class="manual-toc-item depth-${item.level}" data-manual-target="${escapeAttr(item.id)}">${escapeHtml(item.text)}</button>`).join("")
            : `<span class="muted">暂无目录</span>`}
        </aside>
        <article class="markdown-body">${rendered.html}</article>
      </div>
    </section>
  `);
}

function renderAccount() {
  const permissions = state.user?.permissions || [];
  const sessions = state.data.sessions || [];
  const sambaStatus = state.user?.samba_status || "disabled";
  return shell(`
    <section class="split">
      <article class="panel">
        <div class="panel-head"><div><h2>当前账号</h2><span>这里只展示和管理自己的登录身份。</span></div></div>
        <dl class="kv">
          <dt>姓名</dt><dd>${escapeHtml(state.user?.real_name || "-")}</dd>
          <dt>用户名</dt><dd>${escapeHtml(state.user?.username || "-")}</dd>
          <dt>角色</dt><dd>${roleName(state.user?.role)}</dd>
          <dt>状态</dt><dd>${userStateText(state.user?.state || "enabled")}</dd>
          <dt>SSH 账户</dt><dd><code>${escapeHtml(accountNameForUser(state.user))}</code></dd>
          <dt>Samba 服务</dt>
          <dd>
            <div class="samba-control">
              <label class="check"><input type="checkbox" data-toggle-samba ${state.user?.samba_enabled ? "checked" : ""}>开启</label>
              <span class="status ${sambaStatusClass(sambaStatus)}">${sambaStatusText(state.user)}</span>
            </div>
            <input name="samba_current_password" type="password" autocomplete="current-password" placeholder="开启时输入当前密码">
            ${state.user?.samba_last_error ? `<span class="muted">${escapeHtml(state.user.samba_last_error)}</span>` : ""}
          </dd>
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
  const editingNode = nodes.find((node) => node.id === state.adminNodeEditId) || null;
  const formTitle = editingNode ? "修改节点" : "新增节点";
  const selectedOwners = editingNode?.owner_user_ids || [];
  const gpuFlags = nodeGpuSchedulableFlagsText(editingNode);
  const gpuComputeCapabilityOverrides = nodeGpuComputeCapabilityOverridesText(editingNode);
  return `
    <section class="node-management-grid">
      <article class="panel admin-card node-list-card">
        <div class="panel-head"><div><h2>节点列表</h2><span>所有计算节点集中展示；修改、强制下线、重连和删除都会写入审计日志。</span></div></div>
        ${nodes.length ? renderTable(["节点", "SSH", "带宽", "GPU", "所有人", "使用权 / 共享", "状态", "操作"], nodes.map((node) => [
          `<strong>${escapeHtml(node.name)}</strong><br><span class="muted">${escapeHtml(node.ip)}</span>`,
          escapeHtml(node.ssh_user || "-"),
          speed(node.max_speed_mbps),
          `${schedulableGpus(node).length} / ${(node.gpus || []).length} 可调度<br><span class="muted">${escapeHtml(nodeGpuModelsText(node))}</span>`,
          escapeHtml(nodeOwnerNames(node)),
          `${nodeAccessScopeText(node.access_scope)}<br><span class="muted">${nodeSharingScopeText(node.sharing_scope)}</span>`,
          `<span class="status ${node.state}">${nodeStateText(node.state)}</span><br><span class="muted">调度 ${node.scheduling_enabled ? "开启" : "关闭"}</span>`,
          `<button class="small secondary" data-edit-node="${node.id}">修改</button><button class="small danger" data-offline-node="${node.id}">强制下线</button><button class="small secondary" data-reconnect-node="${node.id}">重连</button><button class="small danger" data-delete-node="${node.id}">删除</button>`,
        ])) : renderEmpty("暂无节点")}
      </article>
      <article class="panel admin-card node-form-card">
        <div class="panel-head"><div><h2>${formTitle}</h2><span>GPU 数量、型号和算力由节点监控自动扫描；管理员可维护调度开关并按 index 覆盖算力。</span></div></div>
        <form method="post" id="nodeForm" class="form-grid compact-form">
          <label>节点名称<input name="name" value="${escapeAttr(editingNode?.name || "")}" placeholder="node-a" required></label>
          <label>IP 地址<input name="ip" value="${escapeAttr(editingNode?.ip || "")}" placeholder="192.168.1.21" required></label>
          <label>SSH 用户<input name="ssh_user" value="${escapeAttr(editingNode?.ssh_user || "ddltm")}" required></label>
          <label>与主节点最大连接带宽（Mbps）<input name="max_speed_mbps" type="number" min="1" value="${escapeAttr(editingNode?.max_speed_mbps || "")}" placeholder="10000"></label>
          <label>使用权<select name="access_scope">${renderSelectOptions([["public", "公开"], ["private", "私有"]], editingNode?.access_scope || "public")}</select></label>
          <label>共享<select name="sharing_scope">${renderSelectOptions([["none", "不共享"], ["group", "组内共享"], ["public", "公开共享"]], editingNode?.sharing_scope || "public")}</select></label>
          <label class="full-row">GPU 可用性列表<textarea name="gpu_schedulable_flags" placeholder="按 nvidia-smi 顺序填写，一行一个：1 可调度，0 不调度&#10;1&#10;1&#10;0">${escapeHtml(gpuFlags)}</textarea></label>
          <label class="full-row">GPU 算力覆盖列表<textarea name="gpu_compute_capability_overrides" placeholder="按 nvidia-smi index 一行一个；留空使用自动探测值&#10;8.9&#10;&#10;7.5">${escapeHtml(gpuComputeCapabilityOverrides)}</textarea><span class="muted">格式为 major.minor，例如 RTX 4090 常见为 8.9。中间空行会保留对应 GPU index。</span></label>
          ${editingNode ? `<div class="full-row">${renderNodeGpuInventory(editingNode)}</div>` : ""}
          <div class="full-row">${renderNodeOwnerSelector(selectedOwners)}</div>
          <div class="form-actions full-row">
            <button type="submit">${editingNode ? "提交修改" : "提交新增"}</button>
            ${editingNode ? `<button type="button" class="secondary" data-cancel-node-edit>取消修改</button>` : ""}
          </div>
        </form>
      </article>
    </section>
  `;
}

function renderNodeOwnerSelector(selectedIds = []) {
  const users = nodeOwnerCandidates();
  const selected = new Set(selectedIds.map((id) => String(id)));
  const summary = selected.size ? `已选择 ${selected.size} 人` : "未选择所有人";
  return `
    <div class="node-owner-picker">
      <div class="node-owner-head">
        <span>所有人</span>
        <strong data-owner-summary>${summary}</strong>
      </div>
      <div class="owner-search-row">
        <input type="search" data-owner-search placeholder="搜索统一识别码 / 用户名 / 姓名">
        <button type="button" class="secondary" data-owner-search-button>搜索</button>
      </div>
      <input name="owner_user_ids_manual" value="${escapeAttr(selectedIds.join(","))}" placeholder="也可直接填写所有人 ID，如 1001,1002">
      <div id="nodeOwnerOptions" class="multi-select-options">
        ${users.length ? users.map((user) => `
          <label class="check" data-owner-option="${escapeAttr(`${user.id} ${user.username} ${user.real_name}`.toLowerCase())}">
            <input type="checkbox" name="owner_user_ids" value="${user.id}" ${selected.has(String(user.id)) ? "checked" : ""}>
            <span>#${user.id} ${escapeHtml(user.real_name)}（${escapeHtml(user.username)}，${roleName(user.role)}）</span>
          </label>
        `).join("") : `<div class="empty compact-empty">暂无可选用户，请刷新后台数据</div>`}
      </div>
    </div>
  `;
}

function nodeOwnerCandidates() {
  return (state.data.nodeOwnerUsers || []).length ? state.data.nodeOwnerUsers : (state.data.users || []);
}

function filterNodeOwnerOptions(keyword) {
  const text = String(keyword || "").trim().toLowerCase();
  document.querySelectorAll("[data-owner-option]").forEach((item) => {
    item.hidden = text ? !item.dataset.ownerOption.includes(text) : false;
  });
}

function searchNodeOwners() {
  filterNodeOwnerOptions(document.querySelector("[data-owner-search]")?.value || "");
}

function updateNodeOwnerSummary() {
  const manualIds = parseList(document.querySelector("[name='owner_user_ids_manual']")?.value || "");
  const count = uniqueNumbers([...checkedValues("nodeOwnerOptions"), ...manualIds]).length;
  const summary = document.querySelector("[data-owner-summary]");
  if (summary) summary.textContent = count ? `已选择 ${count} 人` : "未选择所有人";
}

function nodeOwnerNames(node) {
  const ids = node.owner_user_ids || [];
  if (!ids.length) return "未指定";
  const users = nodeOwnerCandidates();
  return ids.map((id) => {
    const user = users.find((item) => String(item.id) === String(id));
    return user ? `${user.real_name}(${user.username})` : `#${id}`;
  }).join("，");
}

function nodeGpuModelsText(node) {
  const models = (node.gpus || []).map((gpu) => {
    const label = gpu.model || "Unknown";
    return gpu.schedulable ? label : `${label}(禁用)`;
  });
  return models.length ? models.join("，") : "-";
}

function nodeGpuSchedulableFlagsText(node) {
  if (!node) return "";
  const flags = node.gpu_schedulable_flags || [];
  const gpuCount = (node.gpus || []).length;
  const length = Math.max(flags.length, gpuCount);
  return Array.from({ length }, (_, index) => Number(flags[index] || 0) ? "1" : "0").join("\n");
}

function nodeGpuComputeCapabilityOverridesText(node) {
  if (!node) return "";
  const overrides = node.gpu_compute_capability_overrides || [];
  const gpuCount = (node.gpus || []).length;
  const length = Math.max(overrides.length, gpuCount);
  return Array.from({ length }, (_, index) => String(overrides[index] || "")).join("\n");
}

function renderNodeGpuInventory(node) {
  const gpus = node.gpus || [];
  return `
    <div class="node-gpu-inventory">
      <span>扫描到的 GPU</span>
      ${gpus.length ? gpus.map((gpu) => `
        <div>
          <code>GPU ${escapeHtml(gpu.gpu_index)}</code>
          <span>${escapeHtml(gpu.model || "Unknown")}</span>
          <small>算力 ${escapeHtml(gpu.compute_capability || "未探测")}${gpu.compute_capability && gpu.detected_compute_capability && gpu.compute_capability !== gpu.detected_compute_capability ? `（探测 ${escapeHtml(gpu.detected_compute_capability)}）` : ""}</small>
          <strong>${gpu.schedulable ? "可调度" : "不调度"}</strong>
        </div>
      `).join("") : `<small class="muted">等待节点监控扫描</small>`}
    </div>
  `;
}

function nodeAccessScopeText(value) {
  return value === "private" ? "私有" : "公开";
}

function nodeSharingScopeText(value) {
  const map = { none: "不共享", group: "组内共享", public: "公开共享" };
  return map[value] || "公开共享";
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
    `<strong>#${escapeHtml(user.id)} ${escapeHtml(user.real_name)}</strong><br><span class="muted">${escapeHtml(user.username)} · ${userStateText(user.state)}</span>`,
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
      <div class="panel-head compact"><div><h3>#${escapeHtml(item.id)} ${escapeHtml(item.real_name)}</h3><span>${escapeHtml(item.username)} · ${roleName(item.role)} · ${userStateText(item.state)}</span></div></div>
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
    `<span class="status ${sessionStatusClass(session)}">${session.current ? "当前会话 · " : ""}${sessionStateText(session)}</span>`,
    session.session_state === "online" ? `<button class="small danger" data-admin-offline-session="${session.id}">下线</button>` : "-",
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

function settingOptionLabel(item) {
  return item.description ? `${item.key} - ${item.description}` : item.key;
}

function selectedAdminSetting(settings) {
  if (!settings.length) return null;
  return settings.find((item) => item.key === state.adminSettingKey) || settings[0];
}

function renderSettingValueControl(item) {
  if (!item) return "";
  const options = item.options || [];
  if (options.length) {
    const optionPairs = options.map((option) => [option.value, option.label]);
    if (item.value && !optionPairs.some(([value]) => String(value) === String(item.value))) {
      optionPairs.push([item.value, item.value]);
    }
    return `<select name="value">${renderSelectOptions(optionPairs, item.value)}</select>`;
  }
  const type = ["integer", "number"].includes(item.value_type) ? "number" : "text";
  const stepAttr = type === "number" ? ` step="${item.value_type === "number" ? "0.1" : "1"}"` : "";
  return `<input name="value" type="${type}"${stepAttr} value="${escapeAttr(item.value || "")}" required>`;
}

function renderAdminSettings(settings) {
  const selected = selectedAdminSetting(settings);
  const options = settings.map((item) => [item.key, settingOptionLabel(item)]);
  return `
    <section class="panel admin-card">
      <div class="panel-head"><div><h2>系统设置</h2><span>所有可管理配置都已落库；选择键后按类型修改值。</span></div></div>
      <form method="post" id="settingForm" class="form-grid compact-form">
        <label class="full-row">配置键<select name="key" id="settingKey" required>${renderSelectOptions(options, selected?.key || "")}</select></label>
        <label>配置值${renderSettingValueControl(selected)}</label>
        <label class="full-row">说明<input value="${escapeAttr(selected?.description || "")}" readonly></label>
        <div class="form-actions"><button type="submit">保存</button></div>
      </form>
      <div class="settings-table">
        ${settings.length ? renderTable(["键", "说明", "值"], settings.map((item) => [
          `<code>${escapeHtml(item.key)}</code>`,
          `<span class="setting-description">${escapeHtml(item.description || "-")}</span>`,
          `<code>${escapeHtml(item.value)}</code>`,
        ])) : renderEmpty("暂无设置")}
      </div>
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
      userStateText(user.state),
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
    `<span class="status ${sessionStatusClass(session)}">${session.current ? "当前会话 · " : ""}${sessionStateText(session)}</span>`,
    session.session_state === "online" ? `<button class="small danger" data-offline-session="${session.id}">下线</button>` : "-",
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

function sambaStatusText(user) {
  const status = user?.samba_status || "disabled";
  return user?.samba_status_label || {
    enabled: "已启用",
    disabled: "已禁用",
    failed: "失败",
    pending: "未执行",
  }[status] || "未知";
}

function sambaStatusClass(status) {
  if (status === "enabled") return "enabled";
  if (status === "pending") return "pending";
  if (status === "failed") return "failed";
  return "disabled";
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
    <aside class="drawer" data-preserve-scroll="drawer">
      <div class="drawer-head">
        <h2>${escapeHtml(state.drawer.title)}</h2>
        <button class="secondary" data-action="close-drawer">关闭</button>
      </div>
      ${state.drawer.type === "task-log" ? renderTaskLogDrawer() : state.drawer.body}
    </aside>
  `;
}

function renderTaskLogDrawer() {
  const status = state.taskLog.paused || state.taskLog.refreshSeconds <= 0
    ? "已暂停"
    : `每 ${state.taskLog.refreshSeconds} 秒刷新`;
  const refreshedAt = state.taskLog.lastRefreshAt ? `上次刷新 ${formatTime(state.taskLog.lastRefreshAt)}` : "等待首次刷新";
  return `
    <div class="task-log-panel">
      <div class="task-log-toolbar">
        <button class="secondary" data-task-log-toggle>${state.taskLog.paused || state.taskLog.refreshSeconds <= 0 ? "恢复刷新" : "暂停刷新"}</button>
        <button class="secondary" data-task-log-refresh>立即刷新</button>
        <label class="refresh-control">刷新间隔
          <input name="task_log_refresh_seconds" type="number" min="0" max="3600" step="1" value="${state.taskLog.refreshSeconds}">
          <span>秒，0 为暂停</span>
        </label>
      </div>
      <div class="task-log-command">
        <code>${escapeHtml(state.taskLog.command)}</code>
        <button class="secondary" data-task-log-copy>复制</button>
      </div>
      <div class="task-log-status">
        <span>${escapeHtml(status)}</span>
        <span>${escapeHtml(refreshedAt)}</span>
        ${state.taskLog.busy ? "<span>刷新中...</span>" : ""}
        ${state.taskLog.error ? `<span class="error-text">${escapeHtml(state.taskLog.error)}</span>` : ""}
      </div>
      <pre class="drawer-log task-log-content" data-preserve-scroll="task-log">${escapeHtml(normalizeTerminalLog(state.taskLog.text))}</pre>
    </div>
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
  return renderMarkdownDocument(markdown).html;
}

function renderMarkdownDocument(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const html = [];
  const toc = [];
  const headingIds = new Map();
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
      const headingText = stripInlineMarkdown(heading[2]);
      const id = uniqueMarkdownHeadingId(headingText, headingIds);
      toc.push({ id, text: headingText, level: heading[1].length });
      html.push(`<h${level} id="${escapeAttr(id)}">${inlineMarkdown(heading[2])}</h${level}>`);
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
  return { html: html.join(""), toc };
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

function stripInlineMarkdown(value) {
  return String(value || "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .trim();
}

function uniqueMarkdownHeadingId(text, usedIds) {
  const fallback = "section";
  const base = String(text || "")
    .toLowerCase()
    .replace(/[^a-z0-9_\u4e00-\u9fff]+/g, "-")
    .replace(/^-+|-+$/g, "") || fallback;
  const count = usedIds.get(base) || 0;
  usedIds.set(base, count + 1);
  return count ? `${base}-${count + 1}` : base;
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

const nodeStateLabels = {
  online: "在线",
  offline: "离线",
  manual_offline: "手动下线",
  reconnecting: "重连中",
  node_lost: "节点丢失",
};

const taskStateLabels = {
  wait: "等待",
  on_hold: "挂起",
  preparing: "准备中",
  dispatching: "派发中",
  starting: "启动中",
  running: "运行中",
  succeeded: "完成",
  failed: "失败",
  cancelled: "已取消",
  alloc_error: "调度错误",
  dependency_failed: "依赖失败",
  offline: "节点掉线",
  offline_error: "节点掉线",
  node_lost: "节点丢失",
};

const envStateLabels = {
  available: "可用",
  registered: "已登记",
  copying: "复制中",
  importing: "导入中",
  fixing: "修复中",
  testing: "测试中",
  installing: "安装中",
  detected: "自动发现",
  error: "错误",
};

const userStateLabels = {
  enabled: "启用",
  disabled: "停用",
};

const loginSessionStateLabels = {
  online: "在线",
  offline: "已下线",
};

const fileJobStateLabels = {
  pending: "等待中",
  running: "运行中",
  succeeded: "完成",
  failed: "失败",
  cancelled: "已取消",
};

function labelFrom(map, value) {
  return map[value] || value || "-";
}

function nodeStateText(value) {
  return labelFrom(nodeStateLabels, value);
}

function taskStateText(value) {
  return labelFrom(taskStateLabels, value);
}

function envStateText(value) {
  return labelFrom(envStateLabels, value);
}

function userStateText(value) {
  return labelFrom(userStateLabels, value);
}

function fileJobStateText(value) {
  return labelFrom(fileJobStateLabels, value);
}

function sessionStateText(session) {
  return session?.status_label || labelFrom(loginSessionStateLabels, session?.session_state);
}

function sessionStatusClass(session) {
  return session?.status_category || (session?.session_state === "online" ? "online" : "offline");
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

function normalizeTerminalLog(value) {
  const text = String(value ?? "").replace(/\r\n/g, "\n");
  const lines = [];
  let line = "";
  let cursor = 0;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === "\x1b") {
      const match = text.slice(index).match(/^\x1b\[([0-?]*)([ -/]*)([@-~])/);
      if (match) {
        // tqdm 和终端进度条常用 ANSI K 清除当前行尾，这里按终端语义折叠展示文本。
        if (match[3] === "K") line = line.slice(0, cursor);
        index += match[0].length - 1;
      }
      continue;
    }
    if (char === "\r") {
      cursor = 0;
      continue;
    }
    if (char === "\n") {
      lines.push(line);
      line = "";
      cursor = 0;
      continue;
    }
    if (char === "\b") {
      cursor = Math.max(0, cursor - 1);
      line = line.slice(0, cursor) + line.slice(cursor + 1);
      continue;
    }
    if (char < " " && char !== "\t") continue;
    if (cursor < line.length) {
      line = line.slice(0, cursor) + char + line.slice(cursor + 1);
    } else {
      line = line.padEnd(cursor, " ") + char;
    }
    cursor += 1;
  }
  lines.push(line);
  return lines.join("\n").replace(/\n{4,}/g, "\n\n\n");
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
  const scrollPositions = capturePreservedScrollPositions();
  document.querySelector("#app").innerHTML = state.user
    ? (isPresenterUser() ? renderPresenter() : (renderers[state.page] || renderDashboard)())
    : renderLogin();
  bindEvents();
  restorePreservedScrollPositions(scrollPositions);
}

function capturePreservedScrollPositions() {
  return Array.from(document.querySelectorAll("[data-preserve-scroll]")).reduce((positions, element) => {
    positions[element.dataset.preserveScroll] = { top: element.scrollTop, left: element.scrollLeft };
    return positions;
  }, {});
}

function restorePreservedScrollPositions(positions) {
  document.querySelectorAll("[data-preserve-scroll]").forEach((element) => {
    const position = positions[element.dataset.preserveScroll];
    if (!position) return;
    element.scrollTop = position.top;
    element.scrollLeft = position.left;
  });
}

function bindEvents() {
  document.querySelector("#loginForm")?.addEventListener("submit", (event) => run(() => login(event), "登录成功"));
  document.querySelector("#nodeForm")?.addEventListener("submit", (event) => run(() => submitNode(event), "节点已保存"));
  document.querySelector("#taskForm")?.addEventListener("submit", (event) => run(() => submitTask(event), "任务已提交"));
  document.querySelector("#profileForm")?.addEventListener("submit", (event) => run(() => submitProfile(event), "资料已保存"));
  document.querySelector("#passwordForm")?.addEventListener("submit", (event) => run(() => changePassword(event), "密码已更新"));
  document.querySelector("#userForm")?.addEventListener("submit", (event) => run(() => submitUser(event), "账号已创建"));
  document.querySelector("#studentForm")?.addEventListener("submit", (event) => run(() => submitUser(event, "student"), "学生已创建"));
  document.querySelector("#settingForm")?.addEventListener("submit", (event) => run(() => updateSetting(event), "设置已保存"));
  document.querySelector("#settingKey")?.addEventListener("change", (event) => {
    state.adminSettingKey = event.currentTarget.value;
    render();
  });
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
  document.querySelector("[data-file-students]")?.addEventListener("click", () => run(toggleStudentFileView));
  document.querySelector("[data-file-shared]")?.addEventListener("click", () => run(toggleSharedFileView));
  document.querySelector("[data-file-new-folder]")?.addEventListener("click", () => run(createFolderFromPrompt, "文件夹已创建"));
  document.querySelector("[data-file-new-file]")?.addEventListener("click", () => run(createFileFromPrompt, "文件已创建"));
  document.querySelector("[data-file-rename]")?.addEventListener("click", () => run(renameSelectedPath, "已重命名"));
  document.querySelector("[data-file-copy]")?.addEventListener("click", () => run(copySelectedPath));
  document.querySelector("[data-file-copy-shared]")?.addEventListener("click", () => run(copySelectedPathToShared));
  document.querySelector("[data-file-copy-own]")?.addEventListener("click", () => run(copySelectedPathToOwn));
  document.querySelector("[data-file-move]")?.addEventListener("click", () => run(moveSelectedPath));
  document.querySelector("[data-file-archive]")?.addEventListener("click", () => run(archiveSelectedFolder, "已开始打包"));
  document.querySelector("[data-file-extract]")?.addEventListener("click", () => run(extractSelectedZip));
  document.querySelector("[data-file-delete]")?.addEventListener("click", () => run(deleteSelectedPath, "已删除"));
  document.querySelector("[data-file-download]")?.addEventListener("click", () => run(downloadSelectedPath));
  document.querySelector("[data-file-save]")?.addEventListener("click", () => {
    const content = document.querySelector("#fileEditor")?.value ?? "";
    run(() => saveCurrentFile(content), "文件已保存");
  });
  document.querySelector("[data-file-grant-exec]")?.addEventListener("click", () => run(grantCurrentFileExecutePermission, "执行权限已更新"));
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
    const successText = state.fileTargetPicker?.mode === "env-import"
      ? ""
      : (["env-package", "task-workdir"].includes(state.fileTargetPicker?.mode) ? "" : (state.fileTargetPicker?.mode === "move" ? "已移动" : (state.fileTargetPicker?.mode === "extract" ? "已开始解压" : "已复制")));
    run(confirmFileTargetPicker, successText);
  });
  document.querySelectorAll("[data-file-picker-open]").forEach((button) => {
    button.addEventListener("click", () => run(() => navigateFileTargetPicker(button.dataset.filePickerOpen)));
  });
  document.querySelectorAll("[data-file-picker-select]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!state.fileTargetPicker) return;
      state.fileTargetPicker.sourcePath = button.dataset.filePickerSelect;
      render();
    });
  });
  document.querySelectorAll("[data-nav]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.nav)));
  bindManualTocEvents();
  document.querySelector("[data-action='logout']")?.addEventListener("click", () => run(logout));
  document.querySelectorAll("[data-action='refresh']").forEach((button) => button.addEventListener("click", () => run(refreshPage, "已刷新")));
  document.querySelector("[name='dashboard_refresh_seconds']")?.addEventListener("change", (event) => setDashboardRefreshSeconds(event.currentTarget.value));
  document.querySelector("[name='presenter_refresh_seconds']")?.addEventListener("change", (event) => setPresenterRefreshSeconds(event.currentTarget.value));
  document.querySelectorAll("[data-presenter-hours]").forEach((button) => {
    button.addEventListener("click", () => setPresenterHistoryHours(button.dataset.presenterHours));
  });
  document.querySelector("[data-action='close-drawer']")?.addEventListener("click", () => {
    if (isEnvPackageInstalling() && !window.confirm("安装正在执行，关闭页面可能导致你无法看到实时结果。确认关闭吗？")) return;
    stopTaskLogRefreshTimer();
    state.drawer = null;
    state.taskFormDraft = null;
    state.taskPredecessorLoading = false;
    state.taskPredecessorRequestKey = "";
    render();
  });
  document.querySelector("[data-task-log-toggle]")?.addEventListener("click", toggleTaskLogRefresh);
  document.querySelector("[data-task-log-refresh]")?.addEventListener("click", () => run(() => refreshTaskLogDrawer({ force: true })));
  document.querySelector("[data-task-log-copy]")?.addEventListener("click", () => run(copyTaskLogCommand));
  document.querySelector("[name='task_log_refresh_seconds']")?.addEventListener("change", (event) => setTaskLogRefreshSeconds(event.currentTarget.value));
  document.querySelectorAll("[data-task-zone]").forEach((button) => button.addEventListener("click", () => switchTaskZone(button.dataset.taskZone)));
  document.querySelectorAll("[data-select-task]").forEach((input) => input.addEventListener("change", () => selectTask(input.value)));
  document.querySelectorAll("[data-task-row]").forEach((row) => row.addEventListener("click", () => selectTask(row.dataset.taskRow)));
  document.querySelectorAll("[data-task-row]").forEach((row) => row.addEventListener("dblclick", () => run(() => showTaskLog(row.dataset.taskRow))));
  document.querySelectorAll("[data-task-action]").forEach((button) => button.addEventListener("click", () => run(() => handleTaskAction(button.dataset.taskAction))));
  document.querySelector("[data-task-pick-workdir]")?.addEventListener("click", (event) => {
    event.preventDefault();
    openTaskWorkdirPicker().catch((error) => showToast(error.message || "打开文件夹选择器失败", "error"));
  });
  document.querySelector("[data-task-node-select]")?.addEventListener("change", updateTaskGpuTypeOptions);
  document.querySelector("[data-task-env-select]")?.addEventListener("change", updateTaskGpuTypeOptions);
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
  document.querySelectorAll("[data-env-log]").forEach((button) => button.addEventListener("click", () => run(() => showEnvLog(button.dataset.envLog))));
  document.querySelectorAll("[data-clone-env]").forEach((button) => button.addEventListener("click", () => run(() => cloneEnv(button.dataset.cloneEnv))));
  document.querySelector("[data-import-envs]")?.addEventListener("click", () => run(importEnvs));
  document.querySelectorAll("[data-install-package-env]").forEach((button) => button.addEventListener("click", () => run(() => showEnvPackageDrawer(button.dataset.installPackageEnv, "install"))));
  document.querySelectorAll("[data-delete-package-env]").forEach((button) => button.addEventListener("click", () => run(() => showEnvPackageDrawer(button.dataset.deletePackageEnv, "delete"))));
  document.querySelector("#envPackageInstallForm")?.addEventListener("submit", (event) => run(() => submitEnvPackageInstall(event)));
  document.querySelector("#envPackageInstallForm")?.addEventListener("change", () => {
    refreshEnvPackageInstallFromForm();
    if (state.drawer) state.drawer.body = renderEnvPackageInstallPanel();
    render();
  });
  document.querySelectorAll("[data-env-package-pick]").forEach((button) => {
    button.addEventListener("click", () => run(() => openEnvPackagePicker(button.dataset.envPackagePick, button.dataset.pickKind, button.dataset.pickExt || "")));
  });
  document.querySelector("[data-open-env-compile]")?.addEventListener("click", () => run(openEnvCompilePicker));
  document.querySelector("[data-clear-env-compile]")?.addEventListener("click", clearEnvCompileTarget);
  document.querySelector("[data-compile-picker-close]")?.addEventListener("click", closeEnvCompilePicker);
  document.querySelector("[data-compile-picker-refresh]")?.addEventListener("click", () => run(openEnvCompilePicker));
  document.querySelector("[data-compile-picker-confirm]")?.addEventListener("click", () => run(confirmEnvCompilePicker));
  document.querySelectorAll("[data-compile-target-select]").forEach((input) => {
    input.addEventListener("change", () => selectEnvCompileTarget(input.value));
  });
  document.querySelectorAll("[data-compile-gpu-mode]").forEach((button) => {
    button.addEventListener("click", () => setCompileGpuMode(button.dataset.compileGpuMode));
  });
  document.querySelectorAll("[data-compile-gpu-index]").forEach((input) => {
    input.addEventListener("change", () => toggleCompileGpu(input.value, input.checked));
  });
  document.querySelector("#envPackageDeleteForm")?.addEventListener("submit", (event) => run(() => submitEnvPackageDelete(event)));
  document.querySelectorAll("[data-delete-package-select]").forEach((input) => {
    input.addEventListener("change", () => {
      refreshEnvPackageDeleteFromForm();
      if (state.drawer) state.drawer.body = renderEnvPackageDeletePanel();
      render();
    });
  });
  document.querySelectorAll("[data-delete-env]").forEach((button) => button.addEventListener("click", () => {
    if (!confirmDeleteEnv(button.dataset.deleteEnv)) return;
    run(() => deleteEnv(button.dataset.deleteEnv), "环境已删除");
  }));
  document.querySelectorAll("[data-edit-user]").forEach((button) => button.addEventListener("click", () => fillUserEditForm(button.dataset.editUser)));
  document.querySelectorAll("[data-reset-user]").forEach((button) => button.addEventListener("click", () => fillPasswordResetForm(button.dataset.resetUser)));
  document.querySelectorAll("[data-delete-user]").forEach((button) => button.addEventListener("click", () => run(() => deleteUser(button.dataset.deleteUser), "账号已删除")));
  document.querySelectorAll("[data-toggle-user]").forEach((button) => button.addEventListener("click", () => run(() => toggleUserState(button.dataset.toggleUser, button.dataset.nextState), button.dataset.nextState === "enabled" ? "账号已启用" : "账号已停用")));
  document.querySelector("[data-toggle-samba]")?.addEventListener("change", (event) => {
    const enabled = Boolean(event.currentTarget.checked);
    const passwordInput = document.querySelector("[name='samba_current_password']");
    const currentPassword = passwordInput ? passwordInput.value.trim() : "";
    run(() => toggleCurrentSamba(enabled, currentPassword), enabled ? "Samba 服务已启用" : "Samba 服务已禁用");
  });
  document.querySelectorAll("select[name='supervisor_ids']").forEach((select) => select.addEventListener("change", () => enforceSupervisorLimit(select)));
  bindSessionEvents();
  document.querySelectorAll("[data-reconnect-node]").forEach((button) => button.addEventListener("click", () => run(() => reconnectNode(button.dataset.reconnectNode), "已提交重连")));
  document.querySelectorAll("[data-offline-node]").forEach((button) => button.addEventListener("click", () => run(() => forceOfflineNode(button.dataset.offlineNode), "已强制下线")));
  document.querySelectorAll("[data-edit-node]").forEach((button) => button.addEventListener("click", () => {
    state.adminNodeEditId = Number(button.dataset.editNode);
    render();
  }));
  document.querySelectorAll("[data-delete-node]").forEach((button) => button.addEventListener("click", () => run(() => deleteNode(button.dataset.deleteNode))));
  document.querySelector("[data-cancel-node-edit]")?.addEventListener("click", () => {
    state.adminNodeEditId = null;
    render();
  });
  document.querySelector("[data-owner-search-button]")?.addEventListener("click", searchNodeOwners);
  document.querySelector("[data-owner-search]")?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    searchNodeOwners();
  });
  document.querySelectorAll("#nodeOwnerOptions input[type='checkbox']").forEach((input) => input.addEventListener("change", updateNodeOwnerSummary));
  document.querySelector("[name='owner_user_ids_manual']")?.addEventListener("input", updateNodeOwnerSummary);
}

function bindManualTocEvents() {
  const manualBody = document.querySelector(".manual-layout .markdown-body");
  const manualToc = document.querySelector(".manual-toc");
  if (!manualBody || !manualToc) return;
  const updateActive = () => updateManualTocActive(manualBody, manualToc);
  document.querySelectorAll("[data-manual-target]").forEach((button) => {
    button.addEventListener("click", () => {
      document.getElementById(button.dataset.manualTarget)?.scrollIntoView({ behavior: "smooth", block: "start" });
      window.setTimeout(updateActive, 260);
    });
  });
  manualBody.addEventListener("scroll", updateActive, { passive: true });
  updateActive();
}

function updateManualTocActive(manualBody, manualToc) {
  const headings = Array.from(manualBody.querySelectorAll("h2[id], h3[id], h4[id], h5[id]"));
  if (!headings.length) return;
  const bodyTop = manualBody.getBoundingClientRect().top;
  const threshold = bodyTop + 28;
  let activeId = headings[0].id;
  for (const heading of headings) {
    if (heading.getBoundingClientRect().top <= threshold) activeId = heading.id;
    else break;
  }
  let activeButton = null;
  manualToc.querySelectorAll("[data-manual-target]").forEach((button) => {
    const isActive = button.dataset.manualTarget === activeId;
    button.classList.toggle("active", isActive);
    if (isActive) activeButton = button;
  });
  if (!activeButton) return;
  const padding = 12;
  const buttonTop = activeButton.offsetTop;
  const buttonBottom = buttonTop + activeButton.offsetHeight;
  const visibleTop = manualToc.scrollTop;
  const visibleBottom = visibleTop + manualToc.clientHeight;
  if (buttonTop < visibleTop + padding) {
    manualToc.scrollTo({ top: Math.max(0, buttonTop - padding), behavior: "smooth" });
  } else if (buttonBottom > visibleBottom - padding) {
    manualToc.scrollTo({ top: buttonBottom - manualToc.clientHeight + padding, behavior: "smooth" });
  }
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
  if (page === "presenter" || pages.some((item) => item.id === page)) {
    state.page = page;
    updateRealtimeTimers();
    run(refreshPage);
  }
});

window.addEventListener("beforeunload", (event) => {
  if (!isEnvPackageInstalling()) return;
  event.preventDefault();
  event.returnValue = "";
});

loadMe().then(refreshPage).catch(() => null).finally(() => {
  updateRealtimeTimers();
  render();
});
