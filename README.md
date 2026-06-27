# NebulaGrid（天枢）3.0 部署文档

本文档根据 `docs/` 目录中的需求规格说明书与系统架构设计书整理，用于指导 NebulaGrid 3.0 在实验室主控节点和计算节点上的部署、配置、启动、验证与日常运维。

> 当前仓库已经具备 FastAPI、前端控制台、数据库模型、任务数据库 CRUD、调度器 allocation 事务、任务执行 worker、节点监控 worker 和远端 runner 骨架。真实 GPU 集群上线前仍需按本文档完成 PostgreSQL/InfluxDB/Redis、NFS、SSH key、conda 路径和 systemd 服务联调。

## 1. 部署目标

NebulaGrid 3.0 是纯 B/S 架构的分布式 GPU 任务调度与实验资源管理平台。部署后应满足：

- 用户通过浏览器访问统一入口；如确需 SSH，仅登录 master 上的个人子账户，不直接登录计算节点。
- master 主控节点运行 Web/API、调度器、节点监控、任务执行器、运行守护、环境安装 worker、数据库、时序监控库和缓存。
- master 与计算节点通过 NFS 共享 `/home/ddltm/data`、`/home/ddltm/envs` 和 `/home/ddltm/shared`：`/home/ddltm/data/user/<user_name>` 作为平台用户 home 目录，`/home/ddltm/data/logs` 存放任务与环境日志，`/home/ddltm/envs` 存放 miniconda、用户环境和节点监控/远端执行代码，`/home/ddltm/shared` 作为所有用户可查看并互相复制文件的共享 SSD 目录。
- master 使用统一主账户运行平台与远端 SSH 控制，例如 `ddltm`；所有计算节点必须创建同名、同密码、同 UID、同 GID 的主账户，避免 NFS 权限和远端执行身份不一致。
- 平台为用户创建的 Linux 子账户只存在于 master，用于用户 SSH 登录主节点和访问自己的 home；计算节点不创建这些子账户，任务由主账户通过受控 runner 在计算节点启动。
- PostgreSQL 作为任务、节点、GPU、用户、审计和事件的单一事实来源。
- InfluxDB 保存节点 CPU/GPU、内存/显存、上传/下载等历史监控指标，便于后续接 Grafana 等可视化工具。
- Redis 用于实时事件、日志流、缓存或后续异步任务协调。
- Nginx 负责 HTTPS、前端静态文件、API 反向代理和 WebSocket/SSE 代理。

## 2. 推荐拓扑

```text
用户浏览器 / 展示大屏
        |
        | HTTPS
        v
[Nginx on master]
        |
        | HTTP / WebSocket / SSE
        v
[NebulaGrid Web/API 主控节点]
├── API Server：鉴权、接口、WebSocket/SSE、审计入口
├── Scheduler：等待任务扫描与资源分配，保持单实例
├── Monitor：节点状态采集与 watchdog
├── Executor：通过 SSH 启动、停止和恢复任务
├── Runtime Guard：运行中任务 GPU 分配一致性检测
├── Env Worker：环境导入、whl/源码包/编译安装作业
├── PostgreSQL：用户、节点、GPU、任务、事件、审计
├── InfluxDB：节点/GPU 历史监控指标
├── Redis：实时事件、缓存、日志流辅助
└── NFS Storage：/home/ddltm/data + /home/ddltm/envs + /home/ddltm/shared（用户 home、日志、运行时文件、环境、节点监控代码、共享文件夹）
        |
        | SSH 控制命令 + NFS 共享文件
        v
[计算节点 A/B/C...]：运行用户训练命令，返回状态与日志
```

最小可用部署可以把 PostgreSQL、InfluxDB、Redis、API 和所有后台 worker 放在同一台 master 上。后续如需提高可靠性，可将数据库独立部署，但调度器仍应保持单实例，避免重复派发任务。

## 3. 机器准备

### 3.1 master 主控节点

建议系统：

- Linux 服务器，推荐 Ubuntu Server 22.04 LTS 或兼容发行版。
- Python 3.11+。
- Node.js 20+，用于前端构建。
- PostgreSQL 14+。
- InfluxDB 2.x。
- Redis 6+。
- Nginx。
- systemd。
- 可访问所有计算节点的网络。
- 作为 NFS server 共享 `/home/ddltm/data`、`/home/ddltm/envs` 和 `/home/ddltm/shared`，并具备读写用户 home、任务日志、环境目录、远端脚本目录和共享文件夹的权限。
- 创建平台主账户，例如 `ddltm`，该账户在 master 和所有计算节点上的用户名、密码、UID、GID 必须一致；NebulaGrid 服务、SSH 控制命令和远端任务 runner 默认使用该主账户。
- master 上的平台子账户由系统按用户名创建，只存在于 master，home 目录统一映射为 `/home/ddltm/data/user/<user_name>`；主账户需要能对这些 home 目录执行增删查改，以便文件管理、任务准备、日志归档和管理员运维。

建议系统用户：

```bash
sudo useradd --create-home --shell /bin/bash ddltm
sudo mkdir -p /home/ddltm/master /etc/nebulagrid /var/log/nebulagrid
sudo mkdir -p /home/ddltm/data/user /home/ddltm/data/logs/task_logs /home/ddltm/data/logs/env_install_logs /home/ddltm/data/runtime /home/ddltm/data/backups
sudo mkdir -p /home/ddltm/envs/miniconda3 /home/ddltm/envs/user_envs /home/ddltm/envs/nebulagrid_remote /home/ddltm/envs/packages
sudo mkdir -p /home/ddltm/shared
sudo chown -R ddltm:ddltm /home/ddltm/master /home/ddltm/data /home/ddltm/envs /home/ddltm/shared /var/log/nebulagrid
sudo chmod 750 /etc/nebulagrid /home/ddltm/data /home/ddltm/envs /var/log/nebulagrid
sudo chmod 775 /home/ddltm/shared
```

### 3.2 计算节点

每个计算节点需要满足：

- SSH 服务可用，master 能连接。
- 优先使用主账户 SSH key 登录；如使用密码或密钥文件，凭据只允许 master 主账户或受控服务读取。
- 已创建与 master 完全一致的主账户，例如 `ddltm`，包括用户名、密码、UID 和 GID。该约束用于保证 NFS 文件属主、远端进程属主和 SSH 执行身份一致。
- 不需要、也不应在计算节点创建平台子账户；用户 SSH 入口只在 master，计算节点只接受主账户的受控 SSH 执行。
- NVIDIA 驱动已安装，`nvidia-smi` 可用。
- 已挂载 master 通过 NFS 共享的 `/home/ddltm/data`、`/home/ddltm/envs` 和 `/home/ddltm/shared`，且挂载路径与 master 保持一致。
- `/home/ddltm/data/user/<user_name>` 下可访问对应用户 home，`/home/ddltm/data/logs` 下可访问任务日志和环境安装日志，`/home/ddltm/data/runtime` 下可访问运行时文件。
- `/home/ddltm/envs` 下可访问 miniconda、用户环境目录和 `nebulagrid_remote` 节点监控/远端执行代码。

计算节点预检查示例：

```bash
ssh node-a 'hostname && nvidia-smi && python3 --version'
ssh node-a 'id ddltm && mount | grep -E " /home/ddltm/data "'
ssh node-a 'test -x /home/ddltm/envs/miniconda3/bin/python'
ssh node-a 'test -f /home/ddltm/envs/nebulagrid_remote/runner.py && test -f /home/ddltm/envs/nebulagrid_remote/monitor.py'
```

## 4. 目录规划

### 4.1 代码目录

推荐将仓库部署到：

```text
/home/ddltm/master
```

对应当前仓库结构：

```text
backend/
frontend/
deploy/
scripts/
docs/
```

### 4.2 配置目录

```text
/etc/nebulagrid/config.yaml
/etc/nebulagrid/secrets.env
```

`config.yaml` 保存非敏感配置，`secrets.env` 保存数据库密码、SECRET_KEY、SSH 凭据路径等敏感配置。敏感文件权限建议：

```bash
sudo chown root:ddltm /etc/nebulagrid/secrets.env
sudo chmod 640 /etc/nebulagrid/secrets.env
```

### 4.3 数据与日志目录

```text
/home/ddltm/data/                     # 通过 NFS 共享到所有计算节点
├── user/                   # 平台用户 home 根目录，子账户 home 为 /home/ddltm/data/user/<user_name>
├── logs/
│   ├── task_logs/          # 任务 stdout/stderr 日志
│   └── env_install_logs/   # 环境安装日志
├── runtime/                # pid、pgid、runner 状态等运行时文件
└── backups/                # 数据库、配置、用户数据和日志备份

/home/ddltm/envs/           # 通过 NFS 共享到所有计算节点
├── miniconda3/             # 统一 miniconda 安装目录
├── user_envs/              # 用户 conda-pack 或登记环境
├── packages/               # 上传的环境包、whl、源码包
└── nebulagrid_remote/      # runner.py、monitor.py、env_installer.py 等节点侧代码

/var/log/nebulagrid/        # API、scheduler、monitor 等 master 服务日志
```

注意：

- `/home/ddltm/data` 必须在 master 和所有计算节点保持相同挂载路径，避免用户 home、任务 workdir、日志路径或环境路径在节点侧失效。
- 用户子账户只在 master 存在，home 目录为 `/home/ddltm/data/user/<user_name>`。计算节点侧所有任务进程以主账户运行，必须通过 PathResolver 和审计约束访问范围，不能依赖计算节点本地 Unix 子账户隔离。
- 用户文件、任务日志、环境目录和数据库备份应分别设计备份策略，不建议混在同一个备份包中。
- 解压、导入环境包时必须先进入隔离临时目录，完成路径安全检查后再移动到目标目录。
- `/home/ddltm/envs/nebulagrid_remote` 由 master 统一维护，计算节点只执行该目录中的受控 runner、monitor 和 env_installer。

## 5. 依赖安装

### 5.1 系统依赖

Ubuntu 示例：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip postgresql redis-server nginx git
```

如果 master 不联网，应提前准备离线包、APT 本地源或内部镜像。

### 5.2 后端依赖

仓库实现后，建议使用虚拟环境隔离后端依赖：

```bash
cd /home/ddltm/master/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

如果后端采用 `pyproject.toml`、`requirements.txt` 或其他包管理工具，应以实际项目文件为准。

### 5.3 前端依赖与构建

仓库实现后，前端建议构建为静态文件，由 Nginx 托管：

```bash
cd /home/ddltm/master/frontend
npm ci
npm run build
```

构建产物路径按前端框架确定，常见为 `dist/` 或 `build/`。

## 6. 数据库、InfluxDB 与 Redis

### 6.1 PostgreSQL 初始化

示例：

```bash
sudo -u postgres psql
```

```sql
CREATE USER nebulagrid WITH PASSWORD 'change-this-password';
CREATE DATABASE nebulagrid OWNER nebulagrid;
GRANT ALL PRIVILEGES ON DATABASE nebulagrid TO nebulagrid;
```

生产环境请使用强密码，并避免把真实密码写入 Git 仓库。

### 6.2 数据库迁移

后端实现迁移工具后执行：

```bash
cd /home/ddltm/master/backend
source .venv/bin/activate
alembic upgrade head
```

如果项目采用其他迁移命令，以实际实现为准。迁移前建议先备份数据库。

### 6.3 Redis

本机 Redis 示例：

```bash
sudo systemctl enable --now redis-server
redis-cli ping
```

期望返回：

```text
PONG
```

### 6.4 InfluxDB

InfluxDB 保存节点/GPU 历史监控指标，PostgreSQL 只保存节点和 GPU 清单、调度状态及业务事件。初始化示例：

```bash
influx setup \
  --org nebulagrid \
  --bucket nebulagrid_metrics \
  --username nebulagrid \
  --password 'change-this-influx-password' \
  --token 'change-this-influx-token' \
  --force
```

## 7. 配置文件

推荐使用 YAML，并允许环境变量覆盖。

示例 `config.yaml`：

```yaml
app:
  name: NebulaGrid
  env: production
  base_url: https://nebulagrid.local

database:
  url: postgresql+psycopg://nebulagrid:${NEBULAGRID_DB_PASSWORD}@127.0.0.1:5432/nebulagrid

redis:
  url: redis://127.0.0.1:6379/0

influxdb:
  url: http://127.0.0.1:8086
  org: nebulagrid
  bucket: nebulagrid_metrics
  token: ${NEBULAGRID_INFLUXDB_TOKEN}
  latest_range: 30m

storage:
  nfs_data_root: /home/ddltm/data
  user_home_root: /home/ddltm/data/user
  user_home_template: /home/ddltm/data/user/{user_name}
  shared_folder_root: /home/ddltm/shared
  task_log_root: /home/ddltm/data/logs/task_logs
  env_package_root: /home/ddltm/envs/packages
  env_install_log_root: /home/ddltm/data/logs/env_install_logs
  runtime_root: /home/ddltm/data/runtime
  miniconda_root: /home/ddltm/envs/miniconda3
  user_env_root: /home/ddltm/envs/user_envs
  remote_code_root: /home/ddltm/envs/nebulagrid_remote

accounts:
  main_user: ddltm
  main_group: ddltm
  require_same_uid_gid_on_nodes: true
  create_child_accounts_on_master_only: true
  child_home_template: /home/ddltm/data/user/{user_name}

scheduler:
  enabled: true
  interval_seconds: 1
  max_dispatch_per_round: 1
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
  reconnect_attempts: 3
  watchdog_timeout_seconds: 600

file_operations:
  worker_threads: 2

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

示例 `secrets.env`：

```bash
NEBULAGRID_SECRET_KEY=replace-with-a-long-random-secret
NEBULAGRID_DB_PASSWORD=replace-with-db-password
NEBULAGRID_INFLUXDB_URL=http://127.0.0.1:8086
NEBULAGRID_INFLUXDB_ORG=nebulagrid
NEBULAGRID_INFLUXDB_BUCKET=nebulagrid_metrics
NEBULAGRID_INFLUXDB_TOKEN=replace-with-influx-token
NEBULAGRID_INFLUXDB_LATEST_RANGE=30m
NEBULAGRID_CONFIG=/etc/nebulagrid/config.yaml
```

安全要求：

- `secret_key` 必须使用高强度随机值。
- 不要把真实 `.env`、密钥、数据库密码提交到 Git。
- 节点 IP、SSH 用户名、真实系统路径只应对管理员可见；普通用户只知道自己的 master 子账户和虚拟路径。

## 8. systemd 服务

架构设计建议在 master 上拆分以下服务：

```text
nebulagrid-api.service
nebulagrid-scheduler.service
nebulagrid-monitor.service
nebulagrid-executor.service
nebulagrid-runtime-guard.service
nebulagrid-env-worker.service
```

### 8.1 API 服务示例

`/etc/systemd/system/nebulagrid-api.service`：

```ini
[Unit]
Description=NebulaGrid API Server
After=network.target postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
Type=simple
User=ddltm
Group=ddltm
WorkingDirectory=/home/ddltm/master/backend
EnvironmentFile=/etc/nebulagrid/secrets.env
ExecStart=/home/ddltm/master/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=on-failure
RestartSec=5
RuntimeDirectory=nebulagrid
LogsDirectory=nebulagrid

[Install]
WantedBy=multi-user.target
```

### 8.2 后台 worker 服务模板

以调度器为例，实际模块路径按实现调整：

```ini
[Unit]
Description=NebulaGrid Scheduler
After=network.target postgresql.service redis-server.service nebulagrid-api.service
Wants=postgresql.service redis-server.service

[Service]
Type=simple
User=ddltm
Group=ddltm
WorkingDirectory=/home/ddltm/master/backend
EnvironmentFile=/etc/nebulagrid/secrets.env
ExecStart=/home/ddltm/master/backend/.venv/bin/python -m app.workers.scheduler
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

其他 worker 可复用该模板，将 `Description` 和 `ExecStart` 替换为：

```text
app.workers.node_monitor
app.workers.task_executor
app.workers.runtime_guard
app.workers.env_install_worker
app.workers.log_streamer
```

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nebulagrid-api
sudo systemctl enable --now nebulagrid-scheduler
sudo systemctl enable --now nebulagrid-monitor
sudo systemctl enable --now nebulagrid-executor
sudo systemctl enable --now nebulagrid-runtime-guard
sudo systemctl enable --now nebulagrid-env-worker
```

查看状态：

```bash
systemctl status nebulagrid-api
journalctl -u nebulagrid-api -f
```

## 9. Nginx 反向代理

Nginx 负责 HTTPS、静态前端文件、API 反向代理、WebSocket/SSE 代理和上传大小限制。

示例配置：

```nginx
server {
    listen 80;
    server_name nebulagrid.local;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name nebulagrid.local;

    ssl_certificate /etc/letsencrypt/live/nebulagrid.local/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nebulagrid.local/privkey.pem;

    client_max_body_size 2048m;

    root /home/ddltm/master/frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000/ws/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }

    location /events/ {
        proxy_pass http://127.0.0.1:8000/events/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

启用配置：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 10. 初始化管理员与节点登记

项目实现初始化脚本后，建议提供一次性管理员创建命令：

```bash
cd /home/ddltm/master
sudo -u ddltm backend/.venv/bin/python scripts/init_admin.py
```

节点登记建议由管理员后台完成，至少录入：

- 节点名称。
- host/IP。
- SSH 端口和主账户 SSH 用户，默认应为跨节点一致的 `ddltm`。
- 公共/私人归属。
- 私人节点所有人和开放范围。
- GPU 可调度开关：按 `nvidia-smi` 顺序填写 0/1，亮机卡或维护卡填 0。
- watchdog 超时时间和是否允许调度。

GPU index、GPU UUID、型号和显存由节点监控通过 `nvidia-smi` 自动扫描并写入数据库；硬件数量变化时下一轮监控会同步更新清单。GPU UUID 建议作为运行时守护检测的核心依据，GPU index 只用于显示和 `CUDA_VISIBLE_DEVICES`。

## 11. 上线验证

### 11.1 master 检查

```bash
systemctl status postgresql
systemctl status redis-server
systemctl status nginx
systemctl status nebulagrid-api
curl -f http://127.0.0.1:8000/api/health
```

### 11.2 Web 检查

- 浏览器访问 `https://nebulagrid.local`。
- 管理员可登录。
- `/api/auth/me` 能返回当前用户和权限。
- 展示大屏只显示脱敏信息。

### 11.3 节点检查

- 管理员后台能看到节点在线。
- 节点 CPU、内存、网络、GPU 利用率和显存占用能刷新。
- watchdog 超时逻辑可在测试节点上验证。
- 节点 IP、SSH 用户等敏感字段只对管理员可见。

### 11.4 任务检查

建议按顺序验证：

1. 提交 CPU-only 测试任务。
2. 提交 `need_gpus=1` 的 GPU 测试任务。
3. 指定 GPU 型号或指定节点提交任务；只指定型号时应在所有可见节点中匹配该型号，只指定节点时只在该节点内调度，两者同时指定时只在指定节点内匹配指定型号。
4. 查看运行中日志 tail。
5. 停止自己的运行任务。
6. 查看任务事件流和审计记录。
7. 重启 API 或 master 后执行恢复扫描，确认不重复派发任务。

## 12. 运维与备份

### 12.1 日志位置

| 类型 | 位置 | 说明 |
|---|---|---|
| 服务日志 | `/var/log/nebulagrid/` 或 `journalctl` | API、scheduler、monitor 等服务自身日志 |
| 任务日志 | `/home/ddltm/data/logs/task_logs/<task_no>.log` | 用户训练 stdout/stderr，通过 NFS 对 master 和计算节点可见 |
| 环境操作日志 | `/home/ddltm/data/logs/env_install_logs/env-<env_id>-<env_name>.log` | 环境导入、复制、修复、检测、安装包、删除包和删除环境日志 |
| 审计日志 | PostgreSQL `audit_logs` | 谁在何时做了什么 |
| 任务事件 | PostgreSQL `task_events` | 任务状态机事件 |
| 节点/GPU 历史指标 | InfluxDB `node_metrics` / `gpu_metrics` measurements | CPU/GPU 使用率、内存/显存、上传/下载、GPU 调用进程数 |

### 12.2 数据库备份

每日备份示例：

```bash
sudo mkdir -p /home/ddltm/data/backups/db
sudo -u postgres pg_dump nebulagrid | gzip > /home/ddltm/data/backups/db/nebulagrid-$(date +%F).sql.gz
```

建议：

- 至少保留最近 7 天每日备份。
- 每月保留一个归档版本。
- 恢复演练应定期执行，不能只备份不验证。

### 12.3 配置备份

每次修改配置前备份：

```bash
sudo mkdir -p /home/ddltm/data/backups/config
sudo cp /etc/nebulagrid/config.yaml /home/ddltm/data/backups/config/config-$(date +%F-%H%M%S).yaml
```

管理员后台后续可提供配置 diff 和回滚能力。

### 12.4 文件与环境备份

建议独立备份：

```text
/home/ddltm/data/user
/home/ddltm/envs/packages
/home/ddltm/data/logs/task_logs
/home/ddltm/data/logs/env_install_logs
/home/ddltm/envs/miniconda3
/home/ddltm/envs/user_envs
/home/ddltm/envs/nebulagrid_remote
```

不要把用户文件、任务日志和数据库 dump 混为一个不可拆分的大备份包。

## 13. 故障处理

### 13.1 master 重启

master 或服务重启后必须执行恢复扫描：

- 查询 `dispatching`、`starting`、`running` 任务。
- 根据远端 PID/PGID 和 runtime 文件确认进程是否仍存在。
- 可以确认仍在运行的任务保持 running。
- 无法确认的任务标记为 `lost` 或 `offline`，并写入任务事件与审计。
- 不得重复派发已经可能在远端运行的任务。

### 13.2 节点 offline

处理流程：

1. watchdog 超时后节点进入 `offline`。
2. 查询该节点 `running`、`starting`、`dispatching` 任务。
3. 尝试 SSH 重连。
4. 无法连接时，任务标记 `offline`。
5. 释放调度占用或按恢复策略处理。
6. 写入 `task_events` 和 `audit_logs`。

### 13.3 SSH 启动失败

- 任务应进入 `failed` 或 `offline`。
- 错误原因需要对管理员可见。
- 已占用 GPU 资源必须释放。
- 失败事件必须写入任务事件表。

### 13.4 GPU 越权使用

Runtime Guard 应检查任务进程树实际使用的 GPU UUID：

- 普通任务使用未分配 GPU 时，按配置警告或中止。
- CPU-only 任务调用 CUDA 时，默认按 `cpu_only_policy: forbid_gpu` 中止。
- 中止时停止远端进程组，任务标记为 `alloc_error`。
- 记录审计日志和任务事件。

### 13.5 环境安装失败

- 同一环境同一时间只允许一个安装作业。
- 安装日志必须保存。
- 失败时保留失败原因和可回滚信息。
- 生产环境不建议自动安装未知来源包，至少要求用户确认风险。

### 13.6 环境导入、复制和路径修复

- 环境管理页面进入时会执行 `conda env list --json` 同步当前 `miniconda3/envs` 下的环境，`base` 环境不展示。
- 用户导入 zip 环境包时，系统先解压到运行时临时目录，再复制到 `NEBULAGRID_CONDA_ENV_ROOT/<env_name>`，状态依次为 `导入中 -> 修复中 -> 测试中 -> 可用`。
- 用户可以对可用环境创建副本。副本复制到 `NEBULAGRID_CONDA_ENV_ROOT/<new_env_name>`，状态依次为 `复制中 -> 修复中 -> 测试中 -> 可用`，副本所有者为创建用户。
- 路径修复会扫描环境内所有文本文件，提取旧环境前缀并替换为新路径，覆盖 `pip` shebang、`.pth`、包配置、metadata 和 conda 记录；含空字节或明显二进制的文件会跳过。
- 修复阶段会整理权限：目录 `755`，普通文件 `644`，`bin`/`Scripts` 下入口文件 `755`。若 API 以 root 运行，会把环境属主调整为 `NEBULAGRID_MAIN_LINUX_USER`。
- 环境检测会通过 `conda activate <env_name>` 激活环境后运行 `remote/env_probe.py`，采集 Python、PyTorch、TensorFlow、CUDA/cuDNN 和包列表。
- 管理员可删除所有环境；普通用户只能删除自己导入或复制的环境。删除会同时删除数据库记录和 `miniconda3/envs/<env_name>` 下的对应目录。
- 每个环境对应一个落盘日志文件：`/home/ddltm/data/logs/env_install_logs/env-<env_id>-<env_name>.log`。管理员可查看全部环境日志，普通用户只能查看自己的环境日志。

## 14. 安全基线

- 后端服务层必须执行 RBAC 和资源归属校验，前端隐藏按钮不能作为权限依据。
- 文件访问必须经过 PathResolver，禁止用户传入真实绝对路径绕过权限。
- 文件列表、预览、上传、创建、保存、复制、移动、删除、打包和解压统一进入专用文件线程池，避免 NFS 或共享盘 IO 占用 API 默认请求线程；生产环境可通过 `NEBULAGRID_FILE_OPERATION_WORKER_THREADS` 控制并发。
- 上传和解压必须限制大小、类型、路径穿越、软链接逃逸和临时目录污染。
- 用户命令保存原文和系统生成后的最终命令，管理员可审计。
- 任务启动必须统一拼接环境激活和 CUDA 绑定前缀。
- 普通用户可以通过系统创建的 master 子账户 SSH 到主节点，但不得获得主账户权限，也不得直接 SSH 到计算节点。
- 所有任务、节点、用户、文件、环境和配置写操作必须写入审计日志。
- Nginx 和后端均应设置上传大小限制，避免大文件绕过应用限制。

## 15. 发布与回滚建议

推荐发布流程：

1. 停止调度器，避免发布期间派发新任务。
2. 备份数据库与配置文件。
3. 拉取或解压新版本到新的 release 目录。
4. 安装后端依赖并构建前端。
5. 执行数据库迁移。
6. 切换 `/home/ddltm/master` 指向新版本。
7. 重启 API 和后台 worker。
8. 验证健康检查、登录、节点状态和任务提交。
9. 恢复调度器。

回滚原则：

- 代码回滚必须确认数据库迁移是否兼容。
- 如果迁移不可逆，应先从备份恢复数据库，再切回旧版本代码。
- 回滚后必须执行 master 恢复扫描，避免任务状态分裂。

## 16. 最小验收清单

上线前至少确认：

- [ ] HTTPS 可访问，Nginx 代理 API 和 WebSocket/SSE 正常。
- [ ] PostgreSQL、InfluxDB、Redis、API、scheduler、monitor、executor、runtime guard、env worker 均可启动。
- [ ] 管理员账号可登录，普通用户、导师、展示者权限边界正确。
- [ ] 至少一个计算节点在线，`nvidia-smi` 采集正常。
- [ ] master 与计算节点均能通过相同路径访问 `/home/ddltm/data`，NFS 挂载、主账户 UID/GID、权限和读写测试正常。
- [ ] 平台用户的 master 子账户 home 均映射到 `/home/ddltm/data/user/<user_name>`，主账户可对其文件执行必要的增删查改，计算节点不存在这些子账户。
- [ ] GPU 任务可提交、调度、运行、停止、释放资源。
- [ ] 日志 tail、完整查看和下载按权限工作。
- [ ] 文件路径越权返回 403 或 404，不泄露真实路径。
- [ ] 环境登记或环境包导入流程可记录日志和状态。
- [ ] master 重启后不会重复派发任务。
- [ ] 节点 offline 后任务状态、资源释放和审计记录正确。
- [ ] 数据库、配置、用户文件和任务日志均有备份策略。
