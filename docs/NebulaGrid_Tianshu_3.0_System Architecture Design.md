# NebulaGrid（天枢）3.0 系统架构设计书

> 项目全称：天枢 NebulaGrid 分布式 GPU 任务调度与资源管理平台  
> 文档类型：系统架构设计书  
> 推荐技术栈：Python + FastAPI + PostgreSQL + InfluxDB + Redis + SQLAlchemy + Alembic  
> 适用阶段：3.0 纯 B/S 重构设计、编码、联调与部署  
> 版本：V1.0  
> 日期：2026-05-18

---

## 1. 编写目的

本文档基于《NebulaGrid（天枢）3.0 需求规格说明书》整理，用于指导 NebulaGrid 3.0 的系统设计与工程实现。与需求分析书不同，本文档不再重复“系统应该具备哪些功能”，而是回答以下问题：

1. 系统采用什么总体架构；
2. 后端、前端、数据库、调度器、节点监控器、任务执行器如何拆分；
3. 任务、节点、GPU、环境、文件、日志、审计等核心对象如何建模；
4. 调度器如何保证资源分配一致性；
5. 远程任务如何启动、停止、恢复和守护；
6. 环境包安装、编译安装作业与普通训练任务如何隔离；
7. 系统如何部署、测试、迁移与演进。

NebulaGrid 3.0 的核心目标是：**彻底抛弃 2.0 的 C/S 架构与临时 Web 嵌入模式，构建一个以数据库为单一事实来源、以浏览器为统一入口、以服务层为业务边界、以调度器为资源决策核心的纯 B/S 系统。**

---

## 2. 架构目标与设计原则

### 2.1 架构目标

NebulaGrid 3.0 需要满足以下架构目标：

| 目标 | 说明 |
|---|---|
| 纯 B/S | 用户、导师、管理员、展示大屏均通过浏览器访问，不再依赖桌面客户端。 |
| 统一状态源 | 任务、节点、GPU、环境、日志索引、审计等状态以数据库为准，避免内存列表、JSON 文件、页面状态三套数据不一致。 |
| 可恢复 | master 重启、SSH 中断、节点掉线后，系统可以根据数据库记录、远程 PID 文件和节点状态恢复或标记任务。 |
| 可审计 | 任务提交、停止、节点强制下线、环境安装、文件操作、用户管理等关键行为均写入审计日志。 |
| 可扩展 | 后续可扩展排队策略、GPU 复用策略、导师配额、项目组、节点标签、消息通知、API token 等能力。 |
| 轻量部署 | 不强制引入 Kubernetes、Slurm 或 Docker，优先适配实验室现有多台 GPU 工作站。 |
| 安全边界明确 | 所有文件路径、环境路径、任务命令、日志读取和管理操作均通过服务层校验，禁止前端字段直接转为系统命令。 |

### 2.2 设计原则

1. **数据库优先，而不是全局变量优先。** 任务状态、节点状态、GPU 分配、安装作业、审计日志等均应落库。
2. **服务层封装业务规则。** API 层只做参数接收和响应，权限、状态迁移、路径解析、调度决策放入 services。
3. **调度器单独运行。** 调度器不应写在 Web 请求生命周期内，也不应依赖前端刷新触发。
4. **任务执行与任务调度解耦。** 调度器负责“选任务、选节点、锁资源”，执行器负责“SSH 启动、日志采集、进程回收”。
5. **普通任务与环境维护作业分离。** whl 安装、源码包导入、编译安装进入 `env_install_jobs`，不进入普通任务队列。
6. **GPU 绑定以检测为主、强制隔离为辅。** 在不使用容器的前提下，`CUDA_VISIBLE_DEVICES` 不能构成强隔离，因此必须增加运行时守护检测。
7. **所有危险操作必须可追踪。** 强制下线节点、终止任务、删除用户、删除环境、覆盖文件等操作必须写审计日志。

---

## 3. 技术选型

### 3.1 总体技术栈

| 层级 | 推荐技术 | 说明 |
|---|---|---|
| 后端语言 | Python 3.11+ | 与现有 DDLTM 代码技术栈一致，便于复用 SSH、任务调度和脚本能力。 |
| Web 框架 | FastAPI | 适合 REST API、WebSocket、Pydantic 数据校验和自动 API 文档。 |
| ASGI 服务器 | Uvicorn + Gunicorn | 开发期用 Uvicorn，生产期用 Gunicorn 管理多个 Uvicorn worker。 |
| ORM | SQLAlchemy 2.x | 支持 ORM/Core、事务、关系建模和异步使用。 |
| 数据库迁移 | Alembic | 管理数据库结构演进。 |
| 主数据库 | PostgreSQL | 用于任务队列、状态机、审计日志、权限数据、节点/GPU 清单和当前调度状态。 |
| 时序数据库 | InfluxDB 2.x | 用于节点 CPU/GPU、内存/显存、上传/下载、GPU 调用进程数等历史监控指标。 |
| 缓存/消息 | Redis | 用于短期缓存、WebSocket 广播、后台事件通知、可选轻量分布式锁。 |
| SSH 执行 | AsyncSSH / Paramiko | 推荐新代码优先 AsyncSSH；若兼容性优先，可先使用 Paramiko。 |
| 前端 | Vue 3 + Vite + TypeScript | 适合管理后台、仪表盘和大屏展示。若团队更熟 React，也可替换为 React。 |
| UI 组件 | Naive UI / Ant Design Vue | 适合表格、表单、弹窗、权限管理和后台页面。 |
| 日志 | structlog / logging + RotatingFileHandler | 后端服务日志与任务日志分离。 |
| 进程管理 | systemd / supervisor | 管理 API、scheduler、monitor、executor、worker 等后台进程。 |

### 3.2 为什么选择 FastAPI

NebulaGrid 3.0 需要大量结构化 API、权限校验、任务状态流和日志流。FastAPI 的优势是：

1. 与 Python 类型注解和 Pydantic 结合紧密，便于定义请求/响应模型；
2. 原生支持异步接口和 WebSocket；
3. 自动生成 OpenAPI 文档，便于前后端联调；
4. 对后台服务拆分友好，适合构建 API Server + Scheduler + Worker 的架构。

### 3.3 为什么选择 PostgreSQL

任务调度系统的关键是状态一致性。PostgreSQL 相比 SQLite/纯 JSON 文件更适合作为 3.0 主数据库：

1. 支持事务和行级锁，便于保证任务领取和 GPU 分配的原子性；
2. 支持 JSONB 字段，便于保存任务扩展参数、审计详情和少量结构化配置；
3. 支持复杂查询、分页、索引和统计，适合历史任务、审计日志和管理员后台；
4. 后续如果调度器需要多实例或高可靠，可以基于数据库锁继续演进。

### 3.4 为什么选择 InfluxDB

节点监控属于持续写入的时间序列数据，和任务、用户、权限这类关系型状态不同。CPU/GPU 使用率、可用内存/显存、上传/下载速率、GPU 调用进程数等指标需要高频写入、按时间窗口查询和后续可视化，因此 3.0 将历史监控指标保存到 InfluxDB：

1. PostgreSQL 只保存节点/GPU 清单、调度状态和业务事件，避免监控点持续写入拖慢事务表；
2. InfluxDB 保存 `node_metrics` 和 `gpu_metrics` measurement，便于按节点、GPU、时间范围查询历史曲线；
3. 后续可直接接入 Grafana 或自建大屏展示趋势、峰值和异常节点。

### 3.5 Redis 的定位

Redis 不作为主状态存储，只用于以下场景：

1. WebSocket/SSE 状态广播；
2. 实时页面的短期状态缓存；
3. 在线用户会话缓存；
4. 调度器心跳；
5. 轻量事件队列，例如“任务状态变化后通知日志页面刷新”。

如果 MVP 阶段希望减少组件数量，可以先不部署 Redis，改用数据库轮询与进程内广播；但正式部署建议引入 Redis。

---

## 4. 总体架构

### 4.1 逻辑架构

```mermaid
flowchart TB
    U[用户浏览器/导师/管理员/展示大屏] -->|HTTPS| FE[前端 Web 应用]
    FE -->|REST API| API[FastAPI API Server]
    FE -->|WebSocket/SSE| RT[实时推送服务]

    API --> AUTH[认证与 RBAC 权限服务]
    API --> TASK[任务服务 TaskService]
    API --> NODE[节点服务 NodeService]
    API --> ENV[环境服务 EnvService]
    API --> FILE[文件服务 FileService]
    API --> LOG[日志服务 LogService]
    API --> AUDIT[审计服务 AuditService]

    TASK --> DB[(PostgreSQL)]
    NODE --> DB
    ENV --> DB
    FILE --> DB
    LOG --> DB
    AUDIT --> DB

    SCH[Scheduler 调度器] --> DB
    MON[Node Monitor 节点监控器] --> DB
    EXE[Executor 任务执行器] --> DB
    GUARD[TaskRuntimeGuard 守护线程] --> DB
    ENVW[EnvInstallWorker 环境安装作业器] --> DB

    RT --> REDIS[(Redis)]
    SCH --> REDIS
    MON --> REDIS
    EXE --> REDIS
    GUARD --> REDIS

    MON -->|SSH| N1[计算节点 A]
    EXE -->|SSH| N1
    GUARD -->|SSH/nvidia-smi| N1
    ENVW -->|SSH/SFTP| N1

    MON -->|SSH| N2[计算节点 B]
    EXE -->|SSH| N2
    GUARD -->|SSH/nvidia-smi| N2
    ENVW -->|SSH/SFTP| N2
```

### 4.2 物理部署架构

最小部署模式：

```text
主控节点 master
├── nginx                         # HTTPS、静态资源、反向代理
├── nebulagrid-api.service         # FastAPI API Server
├── nebulagrid-scheduler.service   # 调度器，单实例
├── nebulagrid-monitor.service     # 节点监控器
├── nebulagrid-executor.service    # 任务执行器
├── nebulagrid-guard.service       # 运行中任务 GPU 守护检测
├── nebulagrid-envworker.service   # 环境包安装与编译安装作业器
├── postgresql.service             # 主数据库
├── redis.service                  # 实时事件与缓存
├── /home/ddltm/data/                         # NFS 共享：用户 home、任务日志、环境安装日志、运行时文件
├── /home/ddltm/data/user/<user_name>/        # master 子账户 home，只在主节点创建对应 Linux 账户
└── /home/ddltm/envs/                         # NFS 共享：miniconda、用户环境、节点监控/远端执行代码

计算节点 node-01/node-02/...
├── SSH Server
├── NVIDIA Driver / CUDA runtime
├── ddltm                          # 与 master 同名、同密码、同 UID、同 GID 的主账户
├── /home/ddltm/data                          # 通过 NFS 挂载 master 的 /home/ddltm/data，包含用户 home、日志、运行时文件
└── /home/ddltm/envs                          # 通过 NFS 挂载 master 的 /home/ddltm/envs，包含环境与 runner/monitor/env_installer
```

部署约定：master 作为 NFS server，共享 `/home/ddltm/data` 和 `/home/ddltm/envs` 到所有计算节点。`/home/ddltm/data/user/<user_name>` 是平台用户在 master 上的 home 目录；`/home/ddltm/data/logs` 存放任务日志和环境安装日志；`/home/ddltm/envs` 存放统一 miniconda、用户环境目录以及 `nebulagrid_remote` 节点监控/远端执行代码。master 与所有计算节点必须创建同名、同密码、同 UID、同 GID 的主账户，例如 `ddltm`，NebulaGrid 服务、SSH 控制命令和远端 runner 均默认以该主账户运行。平台为学生和导师创建的 Linux 子账户只存在于 master，用于用户 SSH 登录主节点和访问自己的 home；管理员用户复用部署前已存在的主账户，不额外创建管理员子账户。计算节点不创建这些子账户，避免节点侧账户同步和 UID 漂移。主账户必须能对 `/home/ddltm/data/user/<user_name>` 下的文件执行必要的增删查改，系统再通过 PathResolver、RBAC 和审计限制普通用户的可见范围。

中期可演进为：数据库独立部署，Redis 独立部署，API 多实例，Scheduler/Monitor/Executor 保持单实例或通过 leader election 控制。

### 4.3 运行时进程划分

| 进程 | 是否必须 | 职责 | 是否允许多实例 |
|---|---:|---|---:|
| API Server | 是 | 提供 REST/WebSocket、鉴权、服务入口 | 可多实例 |
| Scheduler | 是 | 扫描等待任务、分配节点/GPU、写 allocation | MVP 单实例 |
| Node Monitor | 是 | 采集节点 CPU/RAM/网络/GPU 状态，维护 watchdog | 可按节点分片，MVP 单实例 |
| Executor | 是 | 远程启动任务、采集日志、处理返回码 | 可多实例，但需要任务领取锁 |
| Runtime Guard | 是 | 检测运行任务实际 PID/GPU 使用，处理 alloc_error | 可多实例，但按任务加锁 |
| EnvInstallWorker | 是 | 处理 whl/源码包/编译安装作业 | 可多实例，但同一环境需加锁 |
| LogStreamer | 可选 | tail 任务日志并推送给 WebSocket | 可并入 API |

---

## 5. 后端工程结构

推荐目录结构如下：

```text
nebulagrid/
├── backend/
│   ├── app/
│   │   ├── main.py                    # FastAPI 入口
│   │   ├── api/                       # 路由层
│   │   │   ├── auth.py
│   │   │   ├── users.py
│   │   │   ├── nodes.py
│   │   │   ├── tasks.py
│   │   │   ├── envs.py
│   │   │   ├── files.py
│   │   │   ├── logs.py
│   │   │   ├── dashboard.py
│   │   │   └── admin.py
│   │   ├── core/
│   │   │   ├── config.py              # 配置读取
│   │   │   ├── security.py            # 密码、JWT/session、CSRF
│   │   │   ├── rbac.py                # 角色权限判断
│   │   │   ├── errors.py              # 统一错误码
│   │   │   └── path_resolver.py       # 虚拟路径到真实路径解析
│   │   ├── db/
│   │   │   ├── base.py
│   │   │   ├── session.py
│   │   │   ├── models/
│   │   │   └── migrations/
│   │   ├── schemas/                   # Pydantic 请求/响应模型
│   │   ├── services/                  # 业务服务层
│   │   │   ├── auth_service.py
│   │   │   ├── user_service.py
│   │   │   ├── node_service.py
│   │   │   ├── task_service.py
│   │   │   ├── scheduler_service.py
│   │   │   ├── executor_service.py
│   │   │   ├── env_service.py
│   │   │   ├── file_service.py
│   │   │   ├── log_service.py
│   │   │   └── audit_service.py
│   │   ├── workers/
│   │   │   ├── scheduler.py
│   │   │   ├── node_monitor.py
│   │   │   ├── task_executor.py
│   │   │   ├── runtime_guard.py
│   │   │   ├── env_install_worker.py
│   │   │   └── log_streamer.py
│   │   ├── remote/
│   │   │   ├── runner.py              # 下发到计算节点的任务启动脚本
│   │   │   ├── monitor.py             # 下发到计算节点的监控脚本
│   │   │   └── env_installer.py       # 远端环境安装辅助脚本
│   │   └── utils/
│   ├── tests/
│   ├── pyproject.toml
│   └── alembic.ini
├── frontend/
├── deploy/
│   ├── nginx.conf
│   ├── systemd/
│   └── docker-compose.dev.yml         # 仅用于开发数据库/Redis，不要求计算节点安装 Docker
└── docs/
```

注意：这里的 Docker Compose 仅用于开发环境快速启动 PostgreSQL/Redis，不代表计算节点必须安装 Docker。

---

## 6. 数据架构设计

### 6.1 核心实体关系

```mermaid
erDiagram
    users ||--o{ user_sessions : has
    users ||--o{ tasks : submits
    users ||--o{ environments : owns
    users ||--o{ env_install_jobs : starts
    users ||--o{ audit_logs : acts

    nodes ||--o{ gpu_devices : contains
    nodes ||--o{ tasks : runs
    nodes ||--o{ env_install_jobs : compiles_on

    tasks ||--o{ task_allocations : allocates
    tasks ||--o{ task_events : has
    tasks ||--o{ task_runtime_guard_events : guarded_by
    gpu_devices ||--o{ task_allocations : allocated_to

    environments ||--o{ tasks : used_by
    environments ||--o{ env_install_jobs : modified_by
    env_install_jobs ||--o{ env_package_manifests : produces
```

### 6.2 主要数据表

#### 6.2.1 users

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint / uuid | 内部主键 |
| campus_id | varchar | 校园统一认证号，管理员/展示者可为空 |
| name | varchar unique | 登录名，一般为姓名首字母缩写 |
| real_name | varchar | 真实姓名 |
| role | enum | student/supervisor/admin/visual |
| password_hash | varchar | 密码哈希，禁止明文保存 |
| supervisor1_id | fk users.id | 第一导师 |
| supervisor2_id | fk users.id | 第二导师 |
| avatar_path | varchar | 头像路径 |
| home_path | varchar | 用户真实 home 路径，固定映射为 `/home/ddltm/data/user/<user_name>`，仅管理员可见 |
| linux_account_name | varchar | master 上对应的 Linux 子账户名；该账户只在 master 创建，用于用户 SSH 登录 |
| linux_uid | int nullable | master 子账户 UID；用于审计和排障，不要求同步到计算节点 |
| linux_gid | int nullable | master 子账户 GID；用于审计和排障，不要求同步到计算节点 |
| state | enum | enabled/disabled |
| created_at | timestamp | 创建时间 |
| updated_at | timestamp | 更新时间 |

#### 6.2.2 nodes

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint / uuid | 节点主键 |
| name | varchar unique | 节点显示名称 |
| host | varchar | IP 或 hostname，仅管理员可见 |
| ssh_port | int | 默认 22 |
| ssh_username | varchar | master 登录该节点使用的主账户，默认 `ddltm` |
| ssh_uid | int | 该主账户在节点上的 UID，必须与 master 主账户一致 |
| ssh_gid | int | 该主账户在节点上的 GID，必须与 master 主账户一致 |
| ownership | enum | public/private |
| owner_user_id | fk users.id | 私人节点所有人 |
| open_to_group | bool | 私人节点是否开放给同导师组使用 |
| max_link_mbps | int | 与主节点最大连接速度 |
| state | enum | online/offline/disabled/maintenance |
| schedulable | bool | 是否允许调度 |
| last_heartbeat_at | timestamp | 最近心跳时间 |
| watchdog_seconds | int | watchdog 计数或等价时间差 |
| created_at | timestamp | 创建时间 |

#### 6.2.3 gpu_devices

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint / uuid | GPU 主键 |
| node_id | fk nodes.id | 所属节点 |
| gpu_index | int | nvidia-smi 顺序编号 |
| gpu_uuid | varchar | GPU UUID，强烈建议保存 |
| model_name | varchar | RTX4090/A6000/H100 等 |
| enabled | bool | 是否可被分配 |
| used_by_scheduler | bool | 是否被非复用任务占用 |
| running_task_count | int | 当前任务数 |
| last_mem_free_mb | int | 最近可用显存 |
| last_mem_total_mb | int | 总显存 |
| last_utilization | float | GPU 使用率 |

建议以 `gpu_uuid` 作为运行时守护检测的核心依据，`gpu_index` 只作为显示和 `CUDA_VISIBLE_DEVICES` 设置使用。

#### 6.2.4 tasks

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint / uuid | 内部主键 |
| task_no | varchar unique | 对用户展示的任务号，如 2026051809201250 |
| user_id | fk users.id | 提交人 |
| env_id | fk environments.id | 目标环境 |
| description | text | 任务描述 |
| workdir_virtual | varchar | 用户看到的项目路径 |
| workdir_real | varchar | 解析后的真实路径 |
| command | text | 用户命令主体 |
| generated_command | text | 系统生成后的完整命令，可管理员查看 |
| need_gpus | int | 所需 GPU 数 |
| gpu_type | jsonb | GPU 型号约束 |
| target_node_id | fk nodes.id nullable | 指定节点 |
| prev_task_id | fk tasks.id nullable | 前驱任务 |
| is_urgent | bool | 紧急任务 |
| is_reuse_gpu | bool | 是否允许 GPU 复用 |
| is_onhold | bool | 是否挂起 |
| state | enum | wait/dispatching/starting/running/succeeded/failed/cancelled/offline/alloc_error/lost |
| last_block_reason | varchar | 最近阻塞原因 |
| exit_code | int nullable | 进程返回码 |
| remote_root_pid | int nullable | 远端根进程 PID |
| remote_pgid | int nullable | 远端进程组 ID |
| log_path | varchar | 日志路径 |
| created_at | timestamp | 提交时间 |
| started_at | timestamp | 开始时间 |
| ended_at | timestamp | 结束时间 |

#### 6.2.5 task_allocations

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint / uuid | 主键 |
| task_id | fk tasks.id | 任务 |
| node_id | fk nodes.id | 分配节点 |
| gpu_id | fk gpu_devices.id nullable | 分配 GPU；CPU-only 可为空 |
| gpu_index | int nullable | 分配时的 index 快照 |
| gpu_uuid | varchar nullable | 分配时的 UUID 快照 |
| allocation_type | enum | exclusive/reuse/cpu |
| state | enum | allocated/released/violated |
| created_at | timestamp | 分配时间 |
| released_at | timestamp | 释放时间 |

#### 6.2.6 task_events

事件流用于审计任务生命周期和调试异常。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint / uuid | 主键 |
| task_id | fk tasks.id | 任务 |
| event_type | varchar | created/allocated/started/log_ready/finished/failed/cancelled/offline/alloc_error 等 |
| message | text | 事件说明 |
| detail | jsonb | 结构化详情 |
| created_at | timestamp | 时间 |

#### 6.2.7 task_runtime_guard_events

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint / uuid | 主键 |
| task_id | fk tasks.id | 任务 |
| node_id | fk nodes.id | 节点 |
| allowed_gpu_uuids | jsonb | 分配 GPU UUID 列表 |
| observed_gpu_uuids | jsonb | 观测 GPU UUID 列表 |
| observed_pids | jsonb | 观测到的违规 PID |
| violation_count | int | 连续违规次数 |
| action | enum | observe/warn/terminate/kill |
| message | text | 说明 |
| created_at | timestamp | 时间 |

#### 6.2.8 environments

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint / uuid | 主键 |
| user_id | fk users.id | 所有人 |
| name | varchar | 环境名 |
| description | text | 描述 |
| env_path | varchar | 真实路径 |
| python_path | varchar | 目标 python 路径 |
| source_type | enum | registered/conda_pack/imported |
| state | enum | creating/importing/available/error/disabled |
| created_at | timestamp | 创建时间 |
| updated_at | timestamp | 更新时间 |

#### 6.2.9 env_install_jobs

用于 whl 安装、压缩包导入和编译安装。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint / uuid | 主键 |
| job_no | varchar unique | 作业编号 |
| user_id | fk users.id | 操作用户 |
| env_id | fk environments.id | 目标环境 |
| package_name | varchar | 包名或文件名 |
| package_path | varchar | 上传包路径 |
| package_sha256 | varchar | 文件哈希 |
| package_type | enum | wheel/archive/source_dir |
| install_mode | enum | pip_install/direct_copy/compile_install |
| target_node_id | fk nodes.id nullable | 编译安装节点 |
| visible_gpu_indices | jsonb | 编译安装可见 GPU |
| state | enum | pending/running/succeeded/failed/cancelled |
| command | text | 实际安装命令 |
| remote_pid | int nullable | 远端 PID |
| log_path | varchar | 安装日志 |
| return_code | int nullable | 返回码 |
| before_freeze | text | 安装前 pip freeze |
| after_freeze | text | 安装后 pip freeze |
| created_at | timestamp | 创建时间 |
| started_at | timestamp | 开始时间 |
| ended_at | timestamp | 结束时间 |

#### 6.2.10 audit_logs

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint / uuid | 主键 |
| actor_user_id | fk users.id nullable | 操作者 |
| action | varchar | 操作类型 |
| target_type | varchar | users/tasks/nodes/envs/files/system |
| target_id | varchar | 目标 ID |
| ip | varchar | 操作 IP |
| user_agent | text | 浏览器 UA |
| result | enum | success/failed/denied |
| detail | jsonb | 详情 |
| created_at | timestamp | 时间 |

---

## 7. 权限架构

### 7.1 RBAC 模型

系统采用 RBAC + 数据可见性约束的混合模型。

| 角色 | 数据范围 | 操作能力 |
|---|---|---|
| student | 自己的任务、文件、环境；公共节点；可用私人节点 | 提交任务、停止/删除自己的任务、管理自己的文件和环境 |
| supervisor | 自己及学生的数据 | 查看学生任务/文件/环境；管理学生账户；默认不能停止学生任务，可配置扩展 |
| admin | 全部数据 | 管理用户、节点、任务、文件、环境、系统配置、强制下线、审计查看 |
| visual | 公开汇总数据 | 只读展示，不能执行任何修改操作 |

### 7.2 权限校验位置

权限必须在后端服务层校验，不能只依赖前端隐藏按钮。

```text
API Router
  -> Depends(get_current_user)
  -> Pydantic schema validate
  -> Service method
       -> RBAC check
       -> Data scope check
       -> PathResolver check
       -> Business state check
       -> DB transaction
  -> Response
```

### 7.3 数据可见性规则

1. 学生只能看到自己的任务、环境、文件、日志。
2. 导师可以看到自己学生的任务、环境、文件、日志，但默认无权停止学生正在运行的任务。
3. 管理员可以看到全部任务、环境、文件、日志和节点敏感字段。
4. 展示者只能看到汇总指标和脱敏节点状态，不显示 IP、SSH 用户名、真实路径、命令全文和用户隐私字段。

---

## 8. API 架构

### 8.1 API 风格

采用 REST API + WebSocket/SSE：

- REST API：用于登录、查询、提交、修改、删除、停止等确定性操作；
- WebSocket/SSE：用于节点状态、任务状态、任务日志、展示大屏实时更新。

通用响应格式：

```json
{
  "ok": true,
  "code": "OK",
  "message": "success",
  "data": {},
  "request_id": "20260518-abcdef"
}
```

错误响应格式：

```json
{
  "ok": false,
  "code": "FORBIDDEN",
  "message": "无权访问该资源",
  "data": null,
  "request_id": "20260518-abcdef"
}
```

### 8.2 主要 API 分组

#### Auth

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/auth/login | 登录 |
| POST | /api/auth/logout | 退出 |
| GET | /api/auth/me | 当前用户 |
| POST | /api/auth/change-password | 修改密码 |

#### Tasks

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/tasks | 查询任务列表，支持分页/筛选 |
| POST | /api/tasks | 提交任务 |
| GET | /api/tasks/{task_id} | 查看任务详情 |
| POST | /api/tasks/{task_id}/hold | 挂起任务 |
| POST | /api/tasks/{task_id}/resume | 取消挂起 |
| POST | /api/tasks/{task_id}/stop | 停止任务 |
| DELETE | /api/tasks/{task_id} | 删除等待任务或隐藏历史任务 |
| GET | /api/tasks/{task_id}/events | 任务事件流 |
| GET | /api/tasks/{task_id}/allocations | 分配详情 |

#### Nodes

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/nodes | 节点列表 |
| POST | /api/nodes | 新增节点，管理员 |
| GET | /api/nodes/{node_id} | 节点详情 |
| PATCH | /api/nodes/{node_id} | 修改节点 |
| POST | /api/nodes/{node_id}/disable | 禁用调度 |
| POST | /api/nodes/{node_id}/enable | 启用调度 |
| POST | /api/nodes/{node_id}/reconnect | 重新连接 |
| POST | /api/nodes/{node_id}/force-offline | 强制下线 |
| GET | /api/nodes/{node_id}/metrics | 指标历史 |

#### Environments

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/envs | 环境列表 |
| POST | /api/envs/register | 登记已有环境 |
| POST | /api/envs/import-conda-pack | 上传 conda-pack 环境 |
| GET | /api/envs/{env_id} | 环境详情 |
| DELETE | /api/envs/{env_id} | 删除环境 |
| POST | /api/envs/{env_id}/packages/install | 上传并安装 whl 或压缩包 |
| POST | /api/envs/{env_id}/packages/compile-install | 指定节点/GPU 编译安装 |
| GET | /api/envs/{env_id}/install-jobs | 安装作业列表 |
| GET | /api/env-install-jobs/{job_id}/log | 安装日志 |
| POST | /api/env-install-jobs/{job_id}/cancel | 中止安装作业 |

#### Files

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/files/list | 文件列表 |
| POST | /api/files/upload | 上传 |
| GET | /api/files/download | 下载 |
| POST | /api/files/mkdir | 创建目录 |
| POST | /api/files/archive | 打包文件夹 |
| POST | /api/files/extract | 解压 zip/tar |
| GET | /api/files/preview | 文本/图片/音视频预览 |
| DELETE | /api/files | 删除 |

#### Logs

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/logs/tasks/{task_id}/tail | 获取日志尾部 |
| GET | /api/logs/tasks/{task_id}/download | 下载完整日志 |
| WS/SSE | /api/ws/tasks/{task_id}/log | 实时日志 |

---

## 9. 任务调度架构

### 9.1 任务状态机

```mermaid
stateDiagram-v2
    [*] --> wait: 用户提交
    wait --> on_hold: 用户挂起
    on_hold --> wait: 恢复
    wait --> dispatching: 调度器分配资源
    dispatching --> starting: 执行器准备远程启动
    starting --> running: 进程启动/首条日志
    starting --> failed: 启动失败
    dispatching --> offline: 节点掉线
    running --> succeeded: 返回码 0
    running --> failed: 返回码非 0
    running --> cancelled: 用户/管理员中止
    running --> offline: 节点掉线或 SSH 不可恢复
    running --> alloc_error: 守护线程检测到越权 GPU 使用
    running --> lost: master 重启后无法确认
    succeeded --> [*]
    failed --> [*]
    cancelled --> [*]
    offline --> [*]
    alloc_error --> [*]
    lost --> [*]
```

建议后端枚举与前端显示如下：

| 后端状态 | 前端中文 | 说明 |
|---|---|---|
| wait | 等待中 | 等待调度 |
| on_hold | 已挂起 | 不参与调度 |
| dispatching | 分配中 | 已锁定资源，等待执行器领取 |
| starting | 启动中 | SSH 连接、环境激活、命令启动 |
| running | 运行中 | 远端进程正在运行 |
| succeeded | 已完成 | 返回码 0 |
| failed | 运行错误 | 用户代码或启动命令返回非 0 |
| cancelled | 已中止 | 用户或管理员主动停止 |
| offline | 节点掉线 | 节点异常导致中断 |
| alloc_error | 调度错误 | 任务使用了未分配 GPU 或 CPU-only 任务使用 GPU |
| lost | 状态未知 | master 重启或 SSH 异常后无法确认 |

### 9.2 调度器职责

调度器只做资源决策，不直接处理前端请求。主要职责：

1. 周期性扫描 `tasks.state = wait` 且 `is_onhold = false` 的任务；
2. 按紧急程度、优先级、提交时间排序；
3. 检查用户状态、前驱任务、可见节点、节点状态、GPU 型号、复用策略；
4. 在数据库事务中锁定任务和候选 GPU；
5. 写入 `task_allocations`；
6. 更新任务状态为 `dispatching`；
7. 发布任务分配事件，通知 Executor 启动任务。

### 9.3 调度事务边界

调度器必须保证以下动作原子化：

```text
BEGIN;
1. 锁定待调度任务；
2. 检查任务仍为 wait；
3. 锁定候选节点/GPU 记录；
4. 再次检查 GPU 可用性；
5. 写入 task_allocations；
6. 更新 gpu_devices.used_by_scheduler/running_task_count；
7. 更新 tasks.state = dispatching；
8. 写入 task_events.allocated；
COMMIT;
```

如果任何一步失败，必须回滚，不允许出现“任务已进入运行区但 GPU 未占用”或“GPU 已占用但任务未启动”的状态。

### 9.4 调度算法草案

```python
def scheduler_loop():
    while True:
        with db.transaction() as tx:
            task = task_repo.pick_next_waiting_task_for_update(tx)
            if task is None:
                sleep(config.scheduler_interval_seconds)
                continue

            if user_is_disabled(task.user_id):
                task_repo.set_block_reason(task.id, "USER_DISABLED")
                continue

            if not dependency_satisfied(task.prev_task_id):
                task_repo.set_block_reason(task.id, "WAITING_DEPENDENCY")
                continue

            candidates = node_repo.visible_nodes_for_user(task.user_id)
            candidates = filter_online_and_schedulable(candidates)
            candidates = filter_target_node(candidates, task.target_node_id)
            selected = select_resources(candidates, task.requirements)

            if selected is None:
                task_repo.set_block_reason(task.id, "RESOURCE_UNAVAILABLE")
                continue

            allocation_repo.create(task.id, selected)
            gpu_repo.mark_allocated(selected)
            task_repo.set_state(task.id, "dispatching")
            event_repo.add(task.id, "allocated", detail=selected.to_json())

        executor_queue.publish(task.id)
```

### 9.5 GPU 选择策略

GPU 选择应分为硬约束和软评分。

硬约束：

1. 节点 online；
2. 节点 schedulable；
3. 用户对该节点有权限；
4. GPU enabled；
5. GPU 型号符合 `gpu_type`；
6. 非复用任务要求 `used_by_scheduler = false` 且显存占用低于阈值；
7. 复用任务要求 `running_task_count < max_reuse_tasks` 且可用显存比例大于阈值。

软评分：

1. 优先选择空闲显存更多的 GPU；
2. 优先选择任务数更少的 GPU；
3. 优先选择网络速度更高的节点；
4. 指定节点优先；
5. 同等条件下可轮询分配，避免总是压到第一张卡。

---

## 10. 任务执行架构

### 10.1 执行器职责

Executor 负责把已分配任务真正变成远端进程：

1. 领取 `dispatching` 任务；
2. 通过 SSH 连接目标节点；
3. 确认环境存在、工作目录存在；
4. 生成受控命令包装器；
5. 设置 `CUDA_VISIBLE_DEVICES`、`CUDA_DEVICE_ORDER`、`PYTHONUNBUFFERED` 等环境变量；
6. 启动远端进程组；
7. 记录 root_pid、pgid、启动时间；
8. 将 stdout/stderr 写入日志；
9. 根据返回码更新任务状态；
10. 释放 GPU/CPU 资源。

### 10.2 远端 runner 设计

不建议直接通过 `ssh.exec_command("source ... && python train.py")` 裸跑任务。推荐在计算节点放置受控 runner：

```text
/home/ddltm/envs/nebulagrid_remote/runner.py
```

runner 输入 JSON：

```json
{
  "task_id": "2026051809201250",
  "env_activate": "source /home/ddltm/envs/miniconda3/bin/activate && conda activate torch201",
  "workdir": "/home/ddltm/data/user/xz/project1",
  "command": "python train.py --config model.yaml",
  "cuda_visible_devices": "0,2",
  "log_path": "/home/ddltm/data/logs/task_logs/2026051809201250.log",
  "pid_file": "/home/ddltm/data/runtime/2026051809201250.pid"
}
```

runner 输出元信息：

```json
{
  "task_id": "2026051809201250",
  "root_pid": 12345,
  "pgid": 12345,
  "started_at": "2026-05-18T10:10:10+08:00"
}
```

runner 应使用进程组启动任务，便于停止整个任务树：

```python
proc = subprocess.Popen(
    command,
    shell=True,
    cwd=workdir,
    stdout=log_file,
    stderr=subprocess.STDOUT,
    env=env,
    preexec_fn=os.setsid,
)
```

### 10.3 停止任务

停止任务时，系统应：

1. 查询 `tasks.remote_pgid`；
2. SSH 到目标节点；
3. 执行 `kill -TERM -<pgid>`；
4. 等待 `kill_grace_seconds`；
5. 若仍存在，执行 `kill -KILL -<pgid>`；
6. 更新任务状态为 `cancelled` 或具体错误状态；
7. 释放资源；
8. 写入 `task_events` 和 `audit_logs`。

不要只关闭 SSH 连接，因为关闭 SSH 不等于远端 Python 训练进程一定退出。

### 10.4 master 重启后的任务恢复

master 启动时应执行恢复扫描：

| 任务状态 | 恢复策略 |
|---|---|
| wait/on_hold | 保持不变 |
| dispatching | 若未启动，释放 allocation 并回到 wait；若远端已启动，转 running |
| starting | 检查 pid_file 和远端进程，存在则 running，否则 failed/offline |
| running | 检查远端 pgid，存在则继续 running 并恢复守护；不存在则根据返回码或日志标记 lost/failed |
| env_install_jobs.running | 检查远端安装 PID，存在则继续 running，否则 failed/lost |

---

## 11. 运行中任务守护架构

### 11.1 设计背景

用户可能在代码或命令中重新设置 `CUDA_VISIBLE_DEVICES`，导致任务实际运行到未分配 GPU 上。由于 3.0 不强制使用 Docker/cgroup 进行硬隔离，因此必须增加运行时守护检测。

该守护模块命名为：`TaskRuntimeGuard`。

### 11.2 监控对象

TaskRuntimeGuard 只监控普通训练任务，不监控环境包安装/编译安装作业。每个运行中任务需要具备：

1. task_id；
2. node_id；
3. remote_root_pid；
4. remote_pgid；
5. allowed_gpu_indices；
6. allowed_gpu_uuids；
7. 是否 CPU-only。

### 11.3 检测方法

守护线程周期性执行：

1. SSH 到任务所在节点；
2. 获取该任务进程组内所有 PID；
3. 执行 `nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader,nounits`；
4. 筛选属于该任务进程树的 PID；
5. 对比这些 PID 实际使用的 GPU UUID 是否都在 allowed_gpu_uuids 中；
6. 若 CPU-only 任务出现任何 GPU 使用，也视为违规；
7. 连续违规达到阈值后中止任务。

### 11.4 判定规则

```python
def detect_gpu_violation(task, observed):
    allowed = set(task.allowed_gpu_uuids)
    used = set(observed.gpu_uuids_by_task_process_tree)

    if task.need_gpus == 0 and used:
        return True, "CPU_ONLY_TASK_USED_GPU"

    if not used.issubset(allowed):
        return True, "GPU_OUT_OF_ALLOCATION"

    return False, None
```

为降低误判，建议配置：

| 配置 | 默认值 | 说明 |
|---|---:|---|
| task_guard_enabled | true | 是否启用守护检测 |
| task_guard_interval_seconds | 5 | 检测间隔 |
| task_guard_startup_grace_seconds | 10 | 任务启动宽限期 |
| task_guard_violation_confirm_count | 2 | 连续违规多少次才处理 |
| task_guard_kill_grace_seconds | 10 | SIGTERM 后等待时间 |
| task_guard_cpu_only_policy | forbid_gpu | CPU-only 是否禁止使用 GPU |

### 11.5 违规处理

确认违规后：

1. 追加任务日志：`Program stopped because it used GPUs outside allocation.`；
2. 写入 `task_runtime_guard_events`；
3. 写入 `task_events: alloc_error`；
4. 写入 `audit_logs`；
5. 发送 SIGTERM/SIGKILL 停止进程组；
6. 更新 `tasks.state = alloc_error`；
7. 释放 `task_allocations` 和 GPU 资源；
8. 前端显示“调度错误”。

### 11.6 注意事项

1. 不要根据全节点 GPU 使用率误杀任务，必须先映射到任务进程树。
2. 不要只检查环境变量，用户可以在代码运行后动态修改。
3. GPU index 可能因驱动或节点重启变化，长期判断应以 GPU UUID 为准。
4. 对复用 GPU 任务，允许多个任务使用同一个被分配 GPU，但仍禁止使用未分配 GPU。
5. 对启动阶段的短暂 nvidia-smi 波动设置宽限期。

---

## 12. 环境管理与包安装架构

### 12.1 环境管理边界

NebulaGrid 管理环境的目标不是替代 conda，而是提供统一 Web 入口：

1. 登记已有环境；
2. 导入 conda-pack 环境；
3. 上传 whl 包安装；
4. 上传压缩 Python 包安装或导入；
5. 在指定节点/GPU 上进行编译安装；
6. 查看安装日志、pip freeze 差异和安装清单。

### 12.2 whl 安装流程

```mermaid
sequenceDiagram
    participant U as 用户
    participant API as API Server
    participant ENV as EnvService
    participant W as EnvInstallWorker
    participant DB as PostgreSQL

    U->>API: 上传 .whl 并选择环境
    API->>ENV: 校验权限/环境/文件类型
    ENV->>DB: 创建 env_install_jobs(pending)
    ENV->>W: 发布安装事件
    W->>DB: job -> running
    W->>W: env_python -m pip install --no-index package.whl
    W->>W: pip check / pip freeze diff
    W->>DB: job -> succeeded/failed
    API-->>U: 前端轮询或 WebSocket 显示状态
```

### 12.3 压缩 Python 包安装流程

系统对 `.zip/.tar/.tar.gz/.tgz` 执行以下流程：

1. 保存上传包并计算 SHA256；
2. 解压到隔离临时目录；
3. 检查路径安全：禁止绝对路径、`..`、软链接逃逸、超大文件；
4. 判断包结构：
   - 若存在 `pyproject.toml/setup.py/setup.cfg`，执行 `pip install --no-index <unpacked_path>`；
   - 若为纯 Python 包目录，复制到目标环境 `site-packages`；
5. 写入安装清单 `env_package_manifests`；
6. 执行 `pip check` 与可选 import test；
7. 更新安装作业状态。

### 12.4 编译安装作业

有些 whl 或源码包需要在目标节点编译，甚至需要 CUDA/GPU 环境。此时用户可以选择：

1. 目标环境；
2. 目标节点；
3. 可见 GPU；
4. 安装命令模式。

编译安装作业具有以下特点：

1. 不进入普通 `tasks` 表；
2. 不经过 Scheduler；
3. 不占用 `gpu_devices.used_by_scheduler` 或 `running_task_count`；
4. 进入 `env_install_jobs`；
5. 管理员可查看、中止和审计；
6. 前端提示该操作可能影响正在运行任务。

### 12.5 环境锁

同一个环境不应同时执行多个修改操作。建议实现环境级锁：

```text
env_id = 10
running install job exists -> reject new install job or queue it
```

规则：

1. 同一环境只能有一个 running/pending 的安装作业；
2. 运行中的环境安装作业不阻止该环境已有任务继续运行，但前端应提示风险；
3. 若环境处于 importing/error/disabled，不允许提交普通任务；
4. 若环境处于 installing，可配置是否允许提交新任务，默认不允许。

---

## 13. 文件与路径架构

### 13.1 虚拟路径模型

前端永远展示虚拟路径，例如：

```text
/workspace/project/train.py
/home/ddltm/envs/miniconda3/envs/torch201
/logs/2026051809201250.log
```

后端通过 `PathResolver` 转为真实路径：

```python
def resolve_virtual_path(user, virtual_path, mode):
    normalized = normalize(virtual_path)
    if contains_forbidden_pattern(normalized):
        raise SecurityError("非法路径")

    allowed_roots = get_allowed_roots(user, mode)
    real_path = map_virtual_to_real(user, normalized)

    if not is_inside_any(real_path, allowed_roots):
        raise SecurityError("路径越权")

    return real_path
```

### 13.2 文件操作安全要求

1. 禁止前端提交真实绝对路径直接访问；
2. 禁止 `..` 路径穿越；
3. 解压前先扫描文件清单；
4. 禁止符号链接逃逸；
5. 大文件上传分片；
6. 下载接口必须鉴权；
7. 文本预览限制最大大小；
8. 图片/音视频预览只读；
9. 删除、覆盖、解压等危险操作写审计日志。

---

## 14. 节点监控架构

### 14.1 监控方式

MVP 阶段采用 master 通过 SSH 启动远端监控脚本的方式，避免在每个计算节点长期安装复杂 agent。

监控脚本采集：

1. CPU 使用率；
2. 可用内存；
3. 网络上传/下载；
4. GPU 使用率；
5. GPU 总显存/已用/可用；
6. GPU UUID、型号、index；
7. 当前 compute processes；
8. 监控脚本时间戳。

### 14.2 watchdog

节点 watchdog 规则：

| 条件 | 处理 |
|---|---|
| 正常收到状态 | node.state = online，更新 last_heartbeat_at |
| 超过阈值未收到状态 | node.state = offline |
| 节点 offline | CPU/GPU 调度占用释放或按任务恢复策略处理 |
| 管理员禁用 | node.state = disabled，不参与调度 |
| 管理员维护 | node.state = maintenance，不参与普通调度 |

### 14.3 节点状态与任务状态关系

节点 offline 后，不应简单清空所有任务，而应执行恢复流程：

1. 查询该节点 running/starting/dispatching 任务；
2. 尝试 SSH 重连；
3. 若无法连接，任务标记 offline；
4. 释放 allocation；
5. 写入事件和审计；
6. 前端提示用户任务因节点掉线中止。

---

## 15. 日志架构

### 15.1 日志分类

| 日志类型 | 存储位置 | 用途 |
|---|---|---|
| 服务日志 | /var/log/nebulagrid/ | API、scheduler、monitor 等服务自身日志 |
| 任务日志 | /home/ddltm/data/logs/task_logs/<task_no>.log | 用户训练 stdout/stderr，master 与计算节点通过 NFS 共享 |
| 环境安装日志 | /home/ddltm/data/logs/env_install_logs/<job_no>.log | whl/源码包/编译安装日志 |
| 审计日志 | PostgreSQL audit_logs | 谁在何时做了什么 |
| 任务事件 | PostgreSQL task_events | 任务状态机事件 |
| 节点/GPU 历史指标 | InfluxDB node_metrics/gpu_metrics | CPU/GPU 使用率、内存/显存、上传/下载、GPU 调用进程数 |

### 15.2 日志读取策略

1. 等待任务：显示“任务尚未运行，暂无日志”；
2. dispatching/starting：显示“环境启动中，暂无日志”；
3. running：默认 tail 最后 N KB，支持自动刷新；
4. 终态任务：支持完整查看和下载；
5. 超大日志：分页或按 offset 读取；
6. 日志接口必须检查权限。

### 15.3 WebSocket 日志流

```mermaid
sequenceDiagram
    participant UI as 前端日志页面
    participant API as WebSocket API
    participant LOG as LogStreamer
    participant FS as 日志文件

    UI->>API: subscribe task_id
    API->>API: 权限校验
    API->>LOG: 创建 tail 订阅
    loop every interval
        LOG->>FS: read appended bytes
        LOG-->>API: log chunk
        API-->>UI: push chunk
    end
    LOG-->>API: task ended
    API-->>UI: ended event
```

---

## 16. 前端架构

### 16.1 页面结构

```text
frontend/src/
├── api/                # API client
├── components/         # 通用组件
├── layouts/            # 管理后台布局/展示大屏布局
├── pages/
│   ├── dashboard/
│   ├── visual/
│   ├── nodes/
│   ├── tasks/
│   ├── logs/
│   ├── files/
│   ├── envs/
│   ├── users/
│   └── admin/
├── router/
├── store/
├── types/
└── utils/
```

### 16.2 前端状态管理

前端不应自行推断任务终态，应以后端返回为准。建议：

1. 任务列表使用分页 API；
2. 节点卡片使用 WebSocket/SSE 更新；
3. 日志页建立单独日志订阅；
4. 用户权限由 `/api/auth/me` 返回；
5. 路由守卫只做页面级限制，真正权限由后端控制。

### 16.3 关键页面

| 页面 | 关键能力 |
|---|---|
| 仪表盘 | 节点总览、GPU 总览、任务统计、最近错误 |
| 任务列表 | wait/running/history 统一列表，支持状态筛选 |
| 提交任务 | 选择环境、路径、命令、GPU 数量、型号、节点、前驱、复用、紧急 |
| 任务详情 | 状态机、分配 GPU、日志、事件流、停止按钮 |
| 节点管理 | 节点状态、GPU 卡片、启停调度、强制下线 |
| 环境管理 | 环境列表、conda-pack 导入、whl 安装、压缩包导入、编译安装 |
| 文件管理 | 上传、下载、预览、打包、解包 |
| 用户管理 | 账号创建、停用、导师关系、登录设备 |
| 审计日志 | 管理员查看关键操作 |
| 展示大屏 | 只读脱敏展示 |

---

## 17. 安全架构

### 17.1 认证与会话

推荐两种方案：

1. **Session Cookie 模式**：适合实验室内网 Web 系统，服务端可主动注销会话；
2. **JWT + Refresh Token 模式**：适合未来开放 API 或移动端。

MVP 推荐 Session Cookie + CSRF 防护，原因是管理后台操作多，主动失效和审计更方便。

### 17.2 命令安全

用户任务命令本身具有执行能力，不能完全当作普通文本处理。需要通过制度和技术边界共同控制：

1. 用户只能在自己的工作目录运行，真实目录为 `/home/ddltm/data/user/<user_name>`；
2. 系统统一拼接环境激活和 CUDA 前缀；
3. 任务命令保存原文和最终命令；
4. 管理员可查看最终命令；
5. 禁止前端传入真实系统路径绕过 PathResolver；
6. 对危险命令可增加提示或黑名单，但不能依赖黑名单保证安全；
7. 计算节点上的任务以跨节点一致的主账户运行，不能把计算节点 Unix 账户当作用户隔离边界；
8. 后续如需要强隔离，可引入容器、cgroup 或独立 Unix 用户。

### 17.3 文件安全

文件上传与解压是高风险功能。必须：

1. 限制单文件大小；
2. 限制总配额；
3. 解压前检查路径；
4. 禁止解压软链接逃逸；
5. 预览文本时限制最大读取量；
6. 删除操作二次确认；
7. 写审计日志。

### 17.4 环境包安全

whl 和源码包本质上可以执行任意安装脚本。因此：

1. 用户只能安装到自己的环境；
2. 安装作业必须写日志；
3. 管理员可禁用普通用户编译安装；
4. 编译安装默认提示可能影响节点负载；
5. 同一环境安装需加锁；
6. 生产环境不建议自动安装来自未知来源的包，至少要求用户确认风险。

---

## 18. 配置架构

建议使用 YAML + 环境变量覆盖。

```yaml
app:
  name: NebulaGrid
  env: production
  secret_key: change-me
  base_url: https://nebulagrid.local

database:
  url: postgresql+psycopg://nebulagrid:password@127.0.0.1:5432/nebulagrid

redis:
  url: redis://127.0.0.1:6379/0

storage:
  nfs_data_root: /home/ddltm/data
  nfs_env_root: /home/ddltm/envs
  user_home_root: /home/ddltm/data/user
  user_home_template: /home/ddltm/data/user/{user_name}
  task_log_root: /home/ddltm/data/logs/task_logs
  env_package_root: /home/ddltm/envs/packages
  env_install_log_root: /home/ddltm/data/logs/env_install_logs
  runtime_root: /home/ddltm/data/runtime
  miniconda_root: /home/ddltm/envs/miniconda3
  user_env_root: /home/ddltm/envs/miniconda3/envs
  remote_code_root: /home/ddltm/envs/nebulagrid_remote

metrics:
  backend: influxdb
  url: http://127.0.0.1:8086
  org: nebulagrid
  bucket: nebulagrid_metrics
  measurements:
    - node_metrics
    - gpu_metrics

accounts:
  main_user: ddltm
  main_group: ddltm
  require_same_uid_gid_on_nodes: true
  create_child_accounts_on_master_only: true
  child_home_template: /home/ddltm/data/user/{user_name}

scheduler:
  enabled: true
  interval_seconds: 2
  max_dispatch_per_round: 4
  default_gpu_free_mem_ratio_for_reuse: 0.4
  max_tasks_per_reuse_gpu: 5
  exclusive_gpu_max_mem_util: 0.2

executor:
  ssh_connect_timeout_seconds: 10
  kill_grace_seconds: 10
  ssh_username: ddltm
  remote_runner_path: /home/ddltm/envs/nebulagrid_remote/runner.py

monitor:
  interval_seconds: 5
  watchdog_offline_seconds: 600

runtime_guard:
  enabled: true
  interval_seconds: 5
  startup_grace_seconds: 10
  violation_confirm_count: 2
  kill_grace_seconds: 10
  cpu_only_policy: forbid_gpu

env_install:
  allow_compile_install_for_students: true
  max_upload_size_mb: 2048
  one_job_per_env: true
  default_pip_no_index: true

logs:
  tail_default_kb: 256
  tail_max_kb: 4096
```

---

## 19. 部署架构

### 19.1 系统服务

建议在 master 上创建以下 systemd 服务：

```text
nebulagrid-api.service
nebulagrid-scheduler.service
nebulagrid-monitor.service
nebulagrid-executor.service
nebulagrid-runtime-guard.service
nebulagrid-env-worker.service
```

### 19.2 Nginx 反向代理

Nginx 负责：

1. HTTPS；
2. 静态前端文件；
3. API 反向代理；
4. WebSocket 代理；
5. 上传大小限制。

### 19.3 计算节点准备

每个计算节点需要：

1. 开启 SSH；
2. 创建与 master 完全一致的主账户，例如 `ddltm`，要求用户名、密码、UID 和 GID 一致；
3. master 可使用该主账户免密或密码登录；
4. 不在计算节点创建平台用户子账户；子账户只存在于 master，用于用户 SSH 到主节点；
5. 安装 NVIDIA 驱动；
6. `nvidia-smi` 可用；
7. 通过 NFS 挂载 master 共享的 `/home/ddltm/data`，用于访问用户 home、任务日志、环境安装日志、运行时文件、miniconda、用户环境和节点监控/远端执行代码；
8. `/home/ddltm/data` 在 master 和所有计算节点上的路径必须一致；
9. `runner.py`、`monitor.py`、`env_installer.py` 由 master 统一维护在 `/home/ddltm/envs/nebulagrid_remote/`，计算节点通过 NFS 读取并以主账户执行。

---

## 20. 测试架构

### 20.1 单元测试

重点测试：

1. RBAC 权限判断；
2. PathResolver；
3. 任务状态机合法迁移；
4. GPU 选择算法；
5. 环境包类型判断；
6. 解压安全检查；
7. 日志读取权限。

### 20.2 集成测试

重点测试：

1. 提交 CPU 任务；
2. 提交单 GPU 任务；
3. 提交多 GPU 任务；
4. GPU 型号筛选；
5. GPU 复用；
6. 前驱任务；
7. 用户中止任务；
8. 节点掉线；
9. master 重启恢复；
10. whl 安装；
11. 压缩包导入；
12. 编译安装作业；
13. alloc_error 触发。

### 20.3 alloc_error 测试用例

测试脚本示例：

```python
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "1"
import torch
x = torch.randn(1024, 1024, device="cuda:0")
while True:
    y = x @ x
```

测试步骤：

1. 系统分配 GPU 0；
2. 用户代码强行改到其他 GPU；
3. TaskRuntimeGuard 检测到实际 GPU UUID 不属于 allocation；
4. 连续违规达到阈值；
5. 系统停止任务；
6. 状态变为 `alloc_error`；
7. 前端显示“调度错误”；
8. GPU 资源释放；
9. 审计日志记录违规 PID 和实际 GPU。

### 20.4 验收标准

| 模块 | 验收标准 |
|---|---|
| 登录权限 | 四类角色均能进入对应页面，越权 API 被拒绝 |
| 节点监控 | 节点状态、CPU/RAM/GPU/显存可持续刷新 |
| 任务调度 | wait -> dispatching -> running -> terminal 状态正确 |
| GPU 分配 | 不重复分配，不越权可见，复用策略生效 |
| 任务停止 | 能停止整个远端进程组，无残留子进程 |
| 日志查看 | 运行中 tail，历史可下载，权限正确 |
| 环境安装 | whl、压缩包、编译安装均可记录日志和状态 |
| 守护检测 | 越权 GPU 使用会进入 alloc_error |
| 节点异常 | offline 后任务状态、资源释放、审计正确 |
| 审计 | 关键操作均可追踪到用户、IP、时间、目标和结果 |

---

## 21. 迁移策略

### 21.1 从 2.0 迁移的原则

1. 不继续把 3.0 功能堆到 2.0 的全局列表架构中；
2. 先建立数据库模型，再写迁移脚本；
3. 旧的 `wait_task.json/exec_task.json/hist_task.json` 可一次性导入；
4. 旧字段 `slaver` 迁移为 `node`；
5. 旧状态 `accp/err/term/offline_error` 映射到 `succeeded/failed/cancelled/offline`；
6. 旧任务日志保留原路径或建立索引映射；
7. 旧环境列表导入为 `environments.source_type = registered`。

### 21.2 状态映射

| 2.0 状态 | 3.0 状态 |
|---|---|
| wait | wait |
| pexec | starting 或 dispatching |
| exec | running |
| accp | succeeded |
| err | failed |
| term | cancelled |
| offline_error | offline |
| alloc_error | alloc_error |

---

## 22. 开发阶段划分

### 22.1 MVP 阶段

1. 用户登录与 RBAC；
2. 节点登记与监控；
3. 任务提交、调度、执行、停止；
4. 日志查看；
5. 基础文件管理；
6. 基础环境登记；
7. 管理员节点管理；
8. 展示大屏只读页面。

### 22.2 V1.1 阶段

1. conda-pack 环境导入；
2. whl 安装；
3. 压缩 Python 包导入；
4. 编译安装作业；
5. TaskRuntimeGuard 与 alloc_error；
6. 审计日志完整化。

### 22.3 V1.2 阶段

1. 基于 InfluxDB 的历史指标图表和 Grafana 大屏；
2. 用户配额；
3. 导师组资源策略；
4. 任务模板；
5. 消息通知；
6. API token；
7. 更细粒度的环境锁和安装回滚。

### 22.4 V2.0 可能演进

1. 容器化执行隔离；
2. cgroup 级别 GPU/CPU 限制；
3. 多 master 高可用；
4. 与 Slurm/Kubernetes 适配；
5. 项目组/课题组维度资源配额；
6. 训练结果元数据管理。

---

## 23. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 不使用容器导致隔离弱 | 用户可影响同节点其他任务 | PathResolver、GPU Guard、审计、制度约束，后续引入 cgroup/容器 |
| 编译安装作业不经过调度 | 可能影响正在运行任务 | 前端提示、管理员配置、安装作业审计、允许管理员中止 |
| SSH 中断但任务仍在运行 | 状态不一致 | runner 写 pid/pgid，恢复扫描确认远端进程 |
| GPU index 变化 | 分配错误或误判 | 使用 GPU UUID 作为守护判断依据 |
| 日志过大 | Web 页面卡顿 | tail 读取、分页、下载完整日志 |
| master 重启 | running 任务状态丢失 | 数据库持久化、pid_file、恢复扫描 |
| 多调度器重复派发 | 资源冲突 | MVP 单调度器；未来使用 DB 锁或 leader election |
| 环境安装污染 | 任务环境不可用 | 环境锁、pip freeze diff、安装日志、失败状态 |

---

## 24. 结论

NebulaGrid 3.0 应定位为一次完整重构，而不是对 DDLTM 2.0 的页面化改造。推荐架构为：

```text
FastAPI API Server
+ PostgreSQL 单一事实来源
+ InfluxDB 节点/GPU 历史监控
+ Redis 实时事件与缓存
+ Scheduler 单实例调度
+ Executor SSH 远程执行
+ Node Monitor 节点监控
+ TaskRuntimeGuard GPU 分配守护
+ EnvInstallWorker 环境维护作业
+ Vue 3 管理后台与展示大屏
```

该架构能够覆盖当前需求分析书中提出的角色管理、节点监控、任务调度、环境管理、文件管理、日志查看、用户状态、管理员后台、环境包安装和调度错误守护等需求，并为后续横向项目、软著、论文工程系统展示和实验室长期运维留下扩展空间。


