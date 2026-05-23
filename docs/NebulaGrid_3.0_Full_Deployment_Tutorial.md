# NebulaGrid 3.0 完整部署教程

本文是一份从零开始的部署教程，目标是让没有接触过 NebulaGrid 的同学也能把系统跑起来。教程覆盖主节点、计算节点、NFS、PostgreSQL、InfluxDB、Redis、Miniconda base 环境、后端依赖、数据库初始化、systemd 服务、Nginx、前端页面和基础验证。

> 重要说明：当前仓库已经具备 API、前端控制台、数据库表结构、任务数据库 CRUD、调度器 allocation 事务、任务执行 worker、真实节点监控 worker 和远端监控/任务 runner 脚本。调度器与执行器仍需要在真实 GPU 节点上完成 SSH、NFS、conda 环境和日志回收联调；环境安装 worker 的真实包安装闭环仍需继续完善。本教程用于部署当前代码并开始联调真实机器。

## 0. 部署拓扑

示例机器：

| 角色 | 示例主机名 | 示例 IP | 作用 |
|---|---|---|---|
| 主节点 | `master` | `192.168.1.10` | API、前端静态文件、PostgreSQL、InfluxDB、Redis、NFS server、worker |
| 计算节点 1 | `node-a` | `192.168.1.21` | GPU 任务执行 |
| 计算节点 2 | `node-b` | `192.168.1.22` | GPU 任务执行 |

请先把本文中的这些示例值替换成你的实际环境：

- `192.168.1.10`：主节点 IP。
- `192.168.1.0/24`：你的内网网段。
- `node-a`、`node-b`：计算节点主机名或 IP。
- `ddltm`：NebulaGrid 用来控制节点的统一 Linux 主账户。
- `replace-with-strong-password`：PostgreSQL 密码。
- `replace-with-random-secret`：后端 session secret。
- `<your-repo-url>`：你的代码仓库地址。如果没有 git 仓库，可以用 `rsync/scp` 拷贝代码目录。

## 1. 所有机器的基础要求

主节点和计算节点都建议使用 Ubuntu Server 22.04 LTS。

所有机器需要满足：

- 网络互通，主节点能访问计算节点的 SSH 端口。
- 所有机器都有同名 Linux 主账户，例如 `ddltm`。
- 所有机器的 `/home/ddltm/data`、`/home/ddltm/envs` 和 `/home/ddltm/shared` 路径一致。
- 计算节点已经安装 NVIDIA 驱动，并且 `nvidia-smi` 可以正常输出。

计算节点上验证 GPU：

```bash
nvidia-smi
```

## 2. 主节点安装系统软件

以下命令在主节点执行。

```bash
sudo apt update
sudo apt install -y \
  build-essential curl git rsync nginx \
  nfs-kernel-server postgresql postgresql-contrib redis-server \
  influxdb2 openssh-client samba
```

启动基础服务：

```bash
sudo systemctl enable --now postgresql
sudo systemctl enable --now influxdb
sudo systemctl enable --now redis-server
sudo systemctl enable --now nfs-kernel-server
sudo systemctl enable --now nginx
sudo systemctl enable --now smbd
```

检查服务：

```bash
systemctl status postgresql --no-pager
systemctl status influxdb --no-pager
systemctl status redis-server --no-pager
systemctl status nfs-kernel-server --no-pager
systemctl status nginx --no-pager
systemctl status smbd --no-pager
redis-cli ping
```

`redis-cli ping` 应返回：

```text
PONG
```

初始化 InfluxDB，用于保存节点 CPU/GPU、内存/显存、上传/下载等历史监控指标。请把 token 换成随机长字符串并保存好，后续会写入 `/etc/nebulagrid/backend.env`：

```bash
influx setup \
  --host-url http://127.0.0.1:8086 \
  --org nebulagrid \
  --bucket nebulagrid_metrics \
  --username nebulagrid \
  --password replace-with-influx-password \
  --token replace-with-influx-token \
  --force
```

## 3. 创建统一 Linux 主账户

以下命令需要在主节点和所有计算节点都执行。这里以 `ddltm` 为例。

```bash
sudo groupadd -g 2000 ddltm || true
sudo useradd -m -u 2000 -g 2000 -s /bin/bash ddltm || true
sudo passwd ddltm
```

为什么要固定 UID/GID：

- NFS 主要按 UID/GID 判断文件属主。
- 主节点和计算节点 UID/GID 不一致时，可能出现文件权限混乱。

验证：

```bash
id ddltm
```

期望看到 UID/GID 都是 `2000`。

## 4. 配置主节点 SSH 免密访问计算节点

以下命令在主节点执行。

为 `ddltm` 生成 SSH key：

```bash
sudo -u ddltm mkdir -p /home/ddltm/.ssh
sudo -u ddltm ssh-keygen -t ed25519 -f /home/ddltm/.ssh/id_ed25519 -N ""
```

把公钥复制到每台计算节点：

```bash
sudo -u ddltm ssh-copy-id ddltm@node-a
sudo -u ddltm ssh-copy-id ddltm@node-b
```

如果没有 DNS 主机名，就使用 IP：

```bash
sudo -u ddltm ssh-copy-id ddltm@192.168.1.21
```

验证：

```bash
sudo -u ddltm ssh ddltm@node-a 'hostname && id && nvidia-smi -L'
```

这一步必须无密码登录成功，否则后续 executor、monitor、env worker 无法控制计算节点。

## 5. 主节点配置 NFS 共享

以下命令在主节点执行。

创建 NebulaGrid 数据目录、环境目录和共享 SSD 目录。`/home/ddltm/data` 只放用户数据、任务日志、运行时文件和备份；`/home/ddltm/envs` 放 Miniconda、用户环境以及远端执行脚本，避免环境文件和用户数据混在同一个共享根下；`/home/ddltm/shared` 是所有用户可在 Web 文件管理中查看和互相复制文件的共享文件夹。

```bash
sudo mkdir -p /home/ddltm/data/user
sudo mkdir -p /home/ddltm/data/logs/task_logs
sudo mkdir -p /home/ddltm/data/logs/env_install_logs
sudo mkdir -p /home/ddltm/data/runtime
sudo mkdir -p /home/ddltm/data/backups
sudo mkdir -p /home/ddltm/envs/packages
sudo mkdir -p /home/ddltm/envs/miniconda3
sudo mkdir -p /home/ddltm/envs/miniconda3/envs
sudo mkdir -p /home/ddltm/envs/nebulagrid_remote
sudo mkdir -p /home/ddltm/shared
sudo chown -R ddltm:ddltm /home/ddltm/data
sudo chown -R ddltm:ddltm /home/ddltm/envs
sudo chown -R ddltm:ddltm /home/ddltm/shared
sudo chmod 750 /home/ddltm/data
sudo chmod 750 /home/ddltm/envs
sudo chmod 775 /home/ddltm/shared
```

写入 NFS export 配置。请把 `192.168.1.0/24` 替换成你的实际内网网段。

```bash
sudo tee /etc/exports.d/nebulagrid.exports >/dev/null <<'EOF'
/home/ddltm/data 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)
/home/ddltm/envs 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)
/home/ddltm/shared 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)
EOF
```

重新加载 NFS：

```bash
sudo exportfs -ra
sudo exportfs -v
sudo systemctl restart nfs-kernel-server
```

## 6. 计算节点挂载 NFS

以下命令在每台计算节点执行。

安装 NFS 客户端：

```bash
sudo apt update
sudo apt install -y nfs-common
```

创建挂载点：

```bash
sudo mkdir -p /home/ddltm/data
sudo mkdir -p /home/ddltm/envs
sudo mkdir -p /home/ddltm/shared
```

临时挂载测试。请把 `master` 替换成主节点主机名或 IP。

```bash
sudo mount -t nfs master:/home/ddltm/data /home/ddltm/data
sudo mount -t nfs master:/home/ddltm/envs /home/ddltm/envs
sudo mount -t nfs master:/home/ddltm/shared /home/ddltm/shared
```

如果使用 IP：

```bash
sudo mount -t nfs 192.168.1.10:/home/ddltm/data /home/ddltm/data
sudo mount -t nfs 192.168.1.10:/home/ddltm/envs /home/ddltm/envs
sudo mount -t nfs 192.168.1.10:/home/ddltm/shared /home/ddltm/shared
```

写入开机自动挂载：

```bash
echo 'master:/home/ddltm/data /home/ddltm/data nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
echo 'master:/home/ddltm/envs /home/ddltm/envs nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
echo 'master:/home/ddltm/shared /home/ddltm/shared nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
```

如果使用 IP：

```bash
echo '192.168.1.10:/home/ddltm/data /home/ddltm/data nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
echo '192.168.1.10:/home/ddltm/envs /home/ddltm/envs nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
echo '192.168.1.10:/home/ddltm/shared /home/ddltm/shared nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
```

验证 NFS 读写：

```bash
sudo -u ddltm touch /home/ddltm/data/runtime/nfs-test-$(hostname)
sudo -u ddltm touch /home/ddltm/envs/nfs-test-$(hostname)
sudo -u ddltm touch /home/ddltm/shared/nfs-test-$(hostname)
ls -l /home/ddltm/data/runtime/
ls -l /home/ddltm/envs/
ls -l /home/ddltm/shared/
```

主节点和计算节点都应该能看到同一批测试文件。

## 7. 主节点安装 Miniconda base 环境

以下命令在主节点执行。

下载并安装 Miniconda 到 NFS 目录：

```bash
cd /tmp
curl -fsSLO https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p /home/ddltm/envs/miniconda3
sudo chown -R ddltm:ddltm /home/ddltm/envs/miniconda3
```

升级 pip：

```bash
sudo -u ddltm /home/ddltm/envs/miniconda3/bin/python -m pip install -U pip setuptools wheel
```

验证：

```bash
sudo -u ddltm /home/ddltm/envs/miniconda3/bin/python --version
sudo -u ddltm /home/ddltm/envs/miniconda3/bin/pip --version
```

计算节点也应该能通过 NFS 访问同一路径：

```bash
sudo -u ddltm ssh ddltm@node-a '/home/ddltm/envs/miniconda3/bin/python --version'
```

## 8. 主节点创建 PostgreSQL 数据库

以下命令在主节点执行。

进入 PostgreSQL：

```bash
sudo -u postgres psql
```

创建用户和数据库。请替换密码。

```sql
CREATE USER nebulagrid WITH PASSWORD 'replace-with-strong-password';
CREATE DATABASE nebulagrid OWNER nebulagrid;
GRANT ALL PRIVILEGES ON DATABASE nebulagrid TO nebulagrid;
\q
```

验证连接：

```bash
psql 'postgresql://nebulagrid:replace-with-strong-password@127.0.0.1:5432/nebulagrid' -c 'select 1;'
```

如果系统提示 `psql` 不存在：

```bash
sudo apt install -y postgresql-client
```

## 9. 拷贝代码到主节点

系统根目录统一放在主账户下：

```text
/home/ddltm/master
```

这样代码更新、后端依赖安装和前端静态文件检查都可以由 `ddltm` 主账户完成，避免把业务代码放进系统目录后再反复处理属主和 sudo 权限。以下命令以 `ddltm` 身份执行。

先创建目录：

```bash
mkdir -p /home/ddltm/master
```

方式 A：用 git 拉取：

```bash
git clone <your-repo-url> /home/ddltm/master
```

方式 B：从本地电脑拷贝：

```bash
rsync -av --delete ./NebulaGrid/ ddltm@master:/home/ddltm/master/
```

方式 C：用 scp 拷贝压缩包：

```bash
tar czf nebulagrid.tar.gz NebulaGrid
scp nebulagrid.tar.gz ddltm@master:/tmp/
ssh ddltm@master
mkdir -p /home/ddltm/master
tar xzf /tmp/nebulagrid.tar.gz -C /home/ddltm/master --strip-components=1
```

确认目录结构：

```bash
ls -la /home/ddltm/master
ls -la /home/ddltm/master/backend
ls -la /home/ddltm/master/frontend
```

## 10. 安装后端 Python 包

以下命令在主节点以 `ddltm` 身份执行。

```bash
cd /home/ddltm/master/backend
/home/ddltm/envs/miniconda3/bin/python -m pip install -e .
```

如果你希望运行测试，也安装 dev 依赖：

```bash
/home/ddltm/envs/miniconda3/bin/python -m pip install -e ".[dev]"
```

验证关键包：

```bash
/home/ddltm/envs/miniconda3/bin/python - <<'PY'
import fastapi
import sqlalchemy
import psycopg
print("fastapi", fastapi.__version__)
print("sqlalchemy", sqlalchemy.__version__)
print("psycopg", psycopg.__version__)
PY
```

## 11. 创建后端环境变量文件

以下命令在主节点执行。

```bash
sudo mkdir -p /etc/nebulagrid
sudo tee /etc/nebulagrid/backend.env >/dev/null <<'EOF'
NEBULAGRID_APP_NAME=NebulaGrid
NEBULAGRID_ENV=production
NEBULAGRID_MANAGE_LINUX_ACCOUNTS=true
NEBULAGRID_MANAGE_SAMBA_ACCOUNTS=true
NEBULAGRID_DATABASE_URL=postgresql+psycopg://nebulagrid:replace-with-strong-password@127.0.0.1:5432/nebulagrid
NEBULAGRID_REDIS_URL=redis://127.0.0.1:6379/0
NEBULAGRID_INFLUXDB_URL=http://127.0.0.1:8086
NEBULAGRID_INFLUXDB_ORG=nebulagrid
NEBULAGRID_INFLUXDB_BUCKET=nebulagrid_metrics
NEBULAGRID_INFLUXDB_TOKEN=replace-with-influx-token
NEBULAGRID_INFLUXDB_LATEST_RANGE=30m
NEBULAGRID_INFLUXDB_PRESENTER_RANGE=30m
NEBULAGRID_INFLUXDB_PRESENTER_WINDOW=30s
NEBULAGRID_DATA_ROOT=/home/ddltm/data
NEBULAGRID_USER_HOME_ROOT=/home/ddltm/data/user
NEBULAGRID_SHARED_FOLDER_ROOT=/home/ddltm/shared
NEBULAGRID_VISIBLE_ROOTS=/home/ddltm/data/user,/home/ddltm/envs/miniconda3
NEBULAGRID_CONDA_ENV_ROOT=/home/ddltm/envs/miniconda3/envs
NEBULAGRID_TASK_LOG_ROOT=/home/ddltm/data/logs/task_logs
NEBULAGRID_ENV_PACKAGE_ROOT=/home/ddltm/envs/packages
NEBULAGRID_ENV_INSTALL_LOG_ROOT=/home/ddltm/data/logs/env_install_logs
NEBULAGRID_RUNTIME_ROOT=/home/ddltm/data/runtime
NEBULAGRID_REMOTE_CODE_ROOT=/home/ddltm/envs/nebulagrid_remote
NEBULAGRID_MINICONDA_PYTHON=/home/ddltm/envs/miniconda3/bin/python
NEBULAGRID_MAIN_LINUX_USER=ddltm
NEBULAGRID_SESSION_SECRET=replace-with-random-secret
NEBULAGRID_CORS_ORIGINS=http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1,http://localhost,null
NEBULAGRID_SCHEDULER_INTERVAL_SECONDS=5
NEBULAGRID_MONITOR_INTERVAL_SECONDS=5
NEBULAGRID_FILE_OPERATION_WORKER_THREADS=2
EOF
```

设置权限：

```bash
sudo chown root:ddltm /etc/nebulagrid/backend.env
sudo chmod 640 /etc/nebulagrid/backend.env
```

检查内容：

```bash
sudo grep -v SECRET /etc/nebulagrid/backend.env
```

## 12. 初始化数据库表和默认账号

以下命令在主节点以 `ddltm` 身份执行。

```bash
cd /home/ddltm/master/backend
bash -lc 'set -a; source /etc/nebulagrid/backend.env; set +a; /home/ddltm/envs/miniconda3/bin/python scripts/init_db.py'
```

初始化完成后会创建：

- 数据库表。
- 默认管理员账号：`admin`。
- 默认管理员密码：`admin123`。
- 基础 settings 项。

如果你是从旧版本升级，并且 PostgreSQL 中已经存在旧的监控表，请执行一次清理脚本。节点监控历史现在写入 InfluxDB，不再写入 PostgreSQL：

```bash
bash -lc 'set -a; source /etc/nebulagrid/backend.env; set +a; /home/ddltm/envs/miniconda3/bin/python scripts/drop_postgres_metrics_tables.py'
```

验证数据库表：

```bash
psql 'postgresql://nebulagrid:replace-with-strong-password@127.0.0.1:5432/nebulagrid' -c '\dt'
```

> 第一次登录后请尽快修改默认密码。当前初始化脚本用于 MVP 测试，正式上线前建议改成随机一次性密码。

## 13. 同步远端脚本到 NFS

以下命令在主节点以 `ddltm` 身份执行。远端脚本位于 `backend/app/remote/*.py`，使用同步工具可以避免新增脚本后只复制了部分文件：

```bash
cd /home/ddltm/master/backend
env $(cat /etc/nebulagrid/backend.env | xargs) \
  /home/ddltm/envs/miniconda3/bin/python scripts/sync_remote_scripts.py
```

如果你的计算节点不是通过 NFS 读取同一个 `/home/ddltm/envs/nebulagrid_remote`，或者需要把脚本主动推送到每个已登记节点，先完成节点登记，再执行：

```bash
cd /home/ddltm/master/backend
env $(cat /etc/nebulagrid/backend.env | xargs) \
  /home/ddltm/envs/miniconda3/bin/python scripts/sync_remote_scripts.py --all-db-nodes
```

也可以先 dry-run，确认脚本清单和目标节点：

```bash
env $(cat /etc/nebulagrid/backend.env | xargs) \
  /home/ddltm/envs/miniconda3/bin/python scripts/sync_remote_scripts.py --all-db-nodes --dry-run
```

同步完成后运行只读部署自检。该脚本会检查本机共享目录、远端脚本文件、数据库中的计算节点、SSH、NFS 路径、主账户身份和 `nvidia-smi`，不会创建、删除或修改本机和计算节点文件：

```bash
env $(cat /etc/nebulagrid/backend.env | xargs) \
  /home/ddltm/envs/miniconda3/bin/python scripts/deployment_self_check.py
```

在计算节点上验证：

```bash
sudo -u ddltm ssh ddltm@node-a '/home/ddltm/envs/miniconda3/bin/python /home/ddltm/envs/nebulagrid_remote/monitor.py'
```

期望输出 JSON，例如：

```json
{"gpus": []}
```

如果计算节点有 NVIDIA GPU，会输出 GPU 列表。

## 14. 创建 systemd 后端服务

以下命令在主节点执行。

### 14.1 API 服务

```bash
sudo tee /etc/systemd/system/nebulagrid-api.service >/dev/null <<'EOF'
[Unit]
Description=NebulaGrid FastAPI API Server
After=network.target postgresql.service redis-server.service

[Service]
User=ddltm
Group=ddltm
WorkingDirectory=/home/ddltm/master/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/home/ddltm/envs/miniconda3/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

### 14.2 调度器

```bash
sudo tee /etc/systemd/system/nebulagrid-scheduler.service >/dev/null <<'EOF'
[Unit]
Description=NebulaGrid Scheduler
After=network.target postgresql.service redis-server.service nebulagrid-api.service

[Service]
User=ddltm
Group=ddltm
WorkingDirectory=/home/ddltm/master/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/home/ddltm/envs/miniconda3/bin/python -m app.workers.scheduler
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

### 14.3 节点监控 worker

```bash
sudo tee /etc/systemd/system/nebulagrid-node-monitor.service >/dev/null <<'EOF'
[Unit]
Description=NebulaGrid Node Monitor
After=network.target postgresql.service redis-server.service nebulagrid-api.service

[Service]
User=ddltm
Group=ddltm
WorkingDirectory=/home/ddltm/master/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/home/ddltm/envs/miniconda3/bin/python -m app.workers.node_monitor
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

### 14.4 任务执行 worker

```bash
sudo tee /etc/systemd/system/nebulagrid-task-executor.service >/dev/null <<'EOF'
[Unit]
Description=NebulaGrid Task Executor
After=network.target postgresql.service redis-server.service nebulagrid-api.service

[Service]
User=ddltm
Group=ddltm
WorkingDirectory=/home/ddltm/master/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/home/ddltm/envs/miniconda3/bin/python -m app.workers.task_executor
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

### 14.5 运行时守护 worker

```bash
sudo tee /etc/systemd/system/nebulagrid-runtime-guard.service >/dev/null <<'EOF'
[Unit]
Description=NebulaGrid Runtime Guard
After=network.target postgresql.service redis-server.service nebulagrid-api.service

[Service]
User=ddltm
Group=ddltm
WorkingDirectory=/home/ddltm/master/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/home/ddltm/envs/miniconda3/bin/python -m app.workers.runtime_guard
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

### 14.6 环境安装 worker

```bash
sudo tee /etc/systemd/system/nebulagrid-env-install-worker.service >/dev/null <<'EOF'
[Unit]
Description=NebulaGrid Environment Install Worker
After=network.target postgresql.service redis-server.service nebulagrid-api.service

[Service]
User=ddltm
Group=ddltm
WorkingDirectory=/home/ddltm/master/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/home/ddltm/envs/miniconda3/bin/python -m app.workers.env_install_worker
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

加载并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nebulagrid-api
sudo systemctl enable --now nebulagrid-scheduler
sudo systemctl enable --now nebulagrid-node-monitor
sudo systemctl enable --now nebulagrid-task-executor
sudo systemctl enable --now nebulagrid-runtime-guard
sudo systemctl enable --now nebulagrid-env-install-worker
```

检查：

```bash
systemctl status nebulagrid-api --no-pager
systemctl status nebulagrid-scheduler --no-pager
systemctl status nebulagrid-node-monitor --no-pager
systemctl status nebulagrid-task-executor --no-pager
systemctl status nebulagrid-runtime-guard --no-pager
systemctl status nebulagrid-env-install-worker --no-pager
```

查看日志：

```bash
journalctl -u nebulagrid-api -f
journalctl -u nebulagrid-scheduler -f
```

## 15. 部署前端文件

当前前端是零构建静态页面，不需要 Node.js、不需要 npm build。

前端位置：

```text
/home/ddltm/master/frontend
```

这个目录已经随代码一起放置。确认：

```bash
ls -la /home/ddltm/master/frontend
ls -la /home/ddltm/master/frontend/src
```

应看到：

```text
index.html
README.md
src/app.js
src/styles.css
```

## 16. 配置 Nginx

以下命令在主节点执行。

```bash
sudo tee /etc/nginx/sites-available/nebulagrid.conf >/dev/null <<'EOF'
server {
    listen 80;
    server_name _;

    # 文件管理会上传代码包、模型包或数据压缩包。Nginx 默认只有 1m，
    # 如果这里过小，请求会在到达 FastAPI 前直接返回 413。
    client_max_body_size 20g;
    client_body_timeout 3600s;

    root /home/ddltm/master/frontend;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
EOF
```

确保 Nginx 进程能读取主账户目录。Ubuntu 的 Nginx 默认以 `www-data` 运行，`/home/ddltm` 默认可能没有遍历权限。二选一：

方案 A，给目录增加只读遍历权限：

```bash
chmod 755 /home/ddltm
chmod -R a+rX /home/ddltm/master/frontend
```

方案 B，把前端文件同步到 Nginx 专用目录，代码仍留在 `/home/ddltm/master`：

```bash
sudo mkdir -p /var/www/nebulagrid
sudo rsync -av /home/ddltm/master/frontend/ /var/www/nebulagrid/
sudo chown -R www-data:www-data /var/www/nebulagrid
```

然后 Nginx 使用：

```nginx
root /var/www/nebulagrid;
```

启用站点：

```bash
sudo ln -sf /etc/nginx/sites-available/nebulagrid.conf /etc/nginx/sites-enabled/nebulagrid.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

如果上传文件时浏览器提示 `413 Request Entity Too Large`，说明当前生效的 Nginx 配置仍然限制了请求体大小。用下面命令确认生效值并重新加载：

```bash
sudo nginx -T | grep -n client_max_body_size
sudo nginx -t
sudo systemctl reload nginx
```

现在浏览器访问：

```text
http://主节点IP/
```

例如：

```text
http://192.168.1.10/
```

页面默认 API 地址是：

```text
http://127.0.0.1:8000/api
```

如果你从其他电脑访问网页，请在登录页把 API 地址改成：

```text
http://192.168.1.10/api
```

## 17. 首次登录和功能验证

默认账号：

```text
admin / admin123
```

### 17.1 API 健康检查

在主节点执行：

```bash
curl -f http://127.0.0.1:8000/api/health
curl -f http://127.0.0.1/api/health
```

### 17.2 登录 API

```bash
curl -s http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"identity":"admin","password":"admin123"}'
```

记录返回的 `access_token`。

### 17.3 登记计算节点

```bash
TOKEN='<access_token>'
curl -s http://127.0.0.1:8000/api/admin/nodes \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"name":"node-a","ip":"192.168.1.21","ssh_user":"ddltm","gpu_models":["A100","A100"]}'
```

### 17.4 查看节点

```bash
curl -s http://127.0.0.1:8000/api/nodes \
  -H "Authorization: Bearer ${TOKEN}"
```

### 17.5 提交测试任务

```bash
curl -s http://127.0.0.1:8000/api/tasks \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"description":"smoke test","workdir":"/","command":"python --version","requirement":{"need_gpus":0}}'
```

### 17.6 验证文件操作线程与打包/解压任务

文件管理中的打包和解压任务会写入 PostgreSQL 的 `file_jobs` 表，页面刷新、重新登录或 API 多 worker 部署时仍可读取最近一次任务状态。API 启动时会把上次进程遗留的 `pending/running` 文件任务标记为失败，避免重启后长期占用并发名额。当前目录打包生成 zip；解压支持 `.zip`、`.tar`、`.tar.gz`、`.tgz`、`.tar.bz2`、`.tbz2`、`.tar.xz` 和 `.txz`。

文件管理还提供共享文件夹视图，对应 `NEBULAGRID_SHARED_FOLDER_ROOT`，默认绝对路径为 `/home/ddltm/shared`。所有登录用户可以查看共享文件夹，并通过页面按钮把个人目录中的文件或文件夹复制到共享文件夹，也可以在共享文件夹中复制回自己的目录。共享视图只开放查看、下载和复制回个人目录，避免用户误删共享 SSD 根目录或其他人的资料。

列表、预览、上传、创建、保存、复制、移动、删除、打包和解压统一进入专用文件线程池，避免共享盘或 NFS IO 抖动挤占 FastAPI 默认请求线程。线程数由 `NEBULAGRID_FILE_OPERATION_WORKER_THREADS` 控制，默认 `2`；调大前应先确认共享盘吞吐足够。

```bash
curl -s http://127.0.0.1:8000/api/files/jobs/latest \
  -H "Authorization: Bearer ${TOKEN}"
```

如果刚启动过打包或解压，返回数据中应包含 `action`、`state`、`progress`、`source_path` 和 `target_path`。同一用户同时只能运行一个文件打包/解压任务，系统也会限制全局并发，避免共享盘 IO 被大量压缩任务打满。

### 17.7 验证审计日志落库与分类

关键写操作会写入 PostgreSQL 的 `audit_logs` 表，管理员后台“审计日志”页面会分页读取数据库记录，并按系统操作、用户操作、压缩文件、文件操作、任务操作、环境操作、节点操作和其他分类展示。

```bash
curl -s 'http://127.0.0.1:8000/api/admin/audit-logs?page_size=20&category=all' \
  -H "Authorization: Bearer ${TOKEN}"

curl -s 'http://127.0.0.1:8000/api/admin/audit-logs?page_size=20&category=archive' \
  -H "Authorization: Bearer ${TOKEN}"
```

如果从旧版本升级，`init_db.py` 会补齐 `audit_logs` 缺失字段并创建常用索引。该表建议纳入长期备份，用于追溯谁在何时对什么对象执行了什么操作。

## 18. 防火墙

如果启用了 UFW，在主节点开放 HTTP：

```bash
sudo ufw allow 80/tcp
sudo ufw allow from 192.168.1.0/24 to any port 2049 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
sudo ufw reload
```

如果使用 HTTPS，额外开放：

```bash
sudo ufw allow 443/tcp
```

## 19. 常见问题

### 19.1 页面打不开

检查 Nginx：

```bash
sudo nginx -t
systemctl status nginx --no-pager
journalctl -u nginx -n 100 --no-pager
```

检查前端文件：

```bash
ls -la /home/ddltm/master/frontend/index.html
```

### 19.2 前端能打开但登录失败

检查 API：

```bash
curl -v http://127.0.0.1:8000/api/health
systemctl status nebulagrid-api --no-pager
journalctl -u nebulagrid-api -n 100 --no-pager
```

如果浏览器从其他电脑访问，请把登录页 API 地址改成：

```text
http://主节点IP/api
```

### 19.3 Nginx 返回 502

`502 Bad Gateway` 表示 Nginx 正常工作，但反向代理到后端 API 失败。先看 API 服务是否启动：

```bash
systemctl status nebulagrid-api --no-pager
journalctl -u nebulagrid-api -n 200 --no-pager
curl -v http://127.0.0.1:8000/api/health
```

如果日志里出现 `sudo`、`useradd`、`chpasswd`、`chown`、`linux account command failed` 或 `interactive authentication is required`，说明 SSH 子账户同步缺少 `NOPASSWD` sudoers 授权，或者历史用户名不符合 Linux 账户规则。API 服务运行在 systemd 中，没有交互终端，所以不能依赖 sudo 密码缓存，也不能让后端保存 sudo 密码；必须给 API 运行用户配置受限的免密 sudoers。

先确认 API 实际运行用户：

```bash
systemctl cat nebulagrid-api
systemctl show nebulagrid-api -p User -p Group
```

如果 `User=` 不是 `ddltm`，第 21 节 sudoers 左侧的用户名也必须改成实际用户。然后确认 `/etc/sudoers.d/nebulagrid-ddltm` 包含第 21 节列出的 `useradd/usermod/userdel/chpasswd/mkdir/chown/chmod/find/setfacl`，再重启 API：

```bash
sudo visudo -c
sudo systemctl restart nebulagrid-api
journalctl -u nebulagrid-api -n 100 --no-pager
```

### 19.4 数据库连接失败

检查环境变量：

```bash
sudo grep NEBULAGRID_DATABASE_URL /etc/nebulagrid/backend.env
```

检查数据库：

```bash
systemctl status postgresql --no-pager
psql 'postgresql://nebulagrid:replace-with-strong-password@127.0.0.1:5432/nebulagrid' -c 'select 1;'
```

### 19.5 NFS 挂载失败

主节点检查：

```bash
sudo exportfs -v
systemctl status nfs-kernel-server --no-pager
```

计算节点检查：

```bash
showmount -e master
sudo mount -v -t nfs master:/home/ddltm/data /home/ddltm/data
sudo mount -v -t nfs master:/home/ddltm/envs /home/ddltm/envs
```

### 19.6 SSH 免密失败

主节点检查：

```bash
sudo -u ddltm ssh -v ddltm@node-a 'hostname'
```

计算节点检查：

```bash
ls -ld /home/ddltm /home/ddltm/.ssh
ls -l /home/ddltm/.ssh/authorized_keys
```

权限建议：

```bash
sudo chmod 700 /home/ddltm/.ssh
sudo chmod 600 /home/ddltm/.ssh/authorized_keys
sudo chown -R ddltm:ddltm /home/ddltm/.ssh
```

### 19.7 Python 包安装慢或失败

可以配置 pip 镜像：

```bash
sudo -u ddltm mkdir -p /home/ddltm/.pip
sudo -u ddltm tee /home/ddltm/.pip/pip.conf >/dev/null <<'EOF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
EOF
```

重新安装：

```bash
cd /home/ddltm/master/backend
/home/ddltm/envs/miniconda3/bin/python -m pip install -e .
```

## 20. 更新代码

如果代码来自 git，以下命令以 `ddltm` 身份执行代码更新和依赖安装，再用 sudo 重启服务：

```bash
cd /home/ddltm/master
git pull
cd backend
/home/ddltm/envs/miniconda3/bin/python -m pip install -e .
sudo systemctl restart nebulagrid-api nebulagrid-scheduler nebulagrid-node-monitor nebulagrid-task-executor nebulagrid-runtime-guard nebulagrid-env-install-worker
sudo systemctl reload nginx
```

如果代码来自 rsync/scp，重新同步后执行同样的 pip install 和 systemctl restart。

## 21. 主账户部署模式总结

代码根目录固定为 `/home/ddltm/master`，权限边界拆成两部分。

管理员只需要做一次：

- 安装 apt 包：`nginx`、`postgresql`、`redis-server`、`nfs-kernel-server`、`nfs-common` 等。
- 创建 `ddltm` 用户，并保证所有节点 UID/GID 一致。
- 创建 `/home/ddltm/data`、`/home/ddltm/envs` 和 `/home/ddltm/shared` 并配置 NFS。
- 创建 PostgreSQL 用户和数据库。
- 初始化 InfluxDB org、bucket 和 token。
- 创建 `/etc/nebulagrid/backend.env`。
- 创建 systemd service 文件。
- 配置 Nginx。

之后 `ddltm` 可以自己完成：

- 更新 `/home/ddltm/master` 里的代码。
- 在 miniconda base 环境里安装 Python 包。
- 运行 `scripts/init_db.py`。
- 同步远端脚本到 `/home/ddltm/envs/nebulagrid_remote`。
- 使用前端页面登记节点、提交任务、查看状态。

仍然需要 sudo 的操作：

- 安装或升级系统包。
- 修改 `/etc/exports.d/*.exports`。
- 修改 `/etc/nebulagrid/backend.env`。
- 修改 `/etc/systemd/system/*.service`。
- `systemctl restart/reload`。
- 修改 Nginx 配置。

NebulaGrid API 以 `ddltm` 运行，但创建 SSH 子账户、同步 SSH 密码、维护 Samba 账号和删除子账户需要 root 权限。生产环境启用 `NEBULAGRID_MANAGE_LINUX_ACCOUNTS=true` 和 `NEBULAGRID_MANAGE_SAMBA_ACCOUNTS=true` 后，需要给 `ddltm` 添加一条受限 sudoers 规则：

```bash
sudo visudo -f /etc/sudoers.d/nebulagrid-ddltm
```

写入：

```text
ddltm ALL=(root) NOPASSWD: /usr/sbin/useradd, /usr/sbin/usermod, /usr/sbin/userdel, /usr/sbin/chpasswd, /usr/bin/mkdir, /usr/bin/chown, /usr/bin/chmod, /usr/bin/find, /usr/bin/setfacl, /usr/bin/smbpasswd, /usr/bin/pdbedit, /bin/systemctl restart nebulagrid-api, /bin/systemctl restart nebulagrid-scheduler, /bin/systemctl restart nebulagrid-node-monitor, /bin/systemctl restart nebulagrid-task-executor, /bin/systemctl restart nebulagrid-runtime-guard, /bin/systemctl restart nebulagrid-env-install-worker, /bin/systemctl reload nginx
```

这样平台创建用户时会创建同名 Linux 账户，用户可用平台用户名和密码 SSH 到 master，并默认进入 `/home/ddltm/data/user/<user_name>`。`NEBULAGRID_MAIN_LINUX_USER` 对应的主账户会被保护，不会被平台删改系统密码。用户在 Web 修改密码或管理员重置密码时，会同步执行 `chpasswd`；删除平台用户时会同步执行 `userdel --remove`。所有用户目录按实验室共享策略设置为 `755`，便于互相读取和拷贝文件。

主节点还需要配置 Samba 的 `homes` 动态共享；计算节点不需要安装 Samba。用户在账号管理页当前账号卡片中手动勾选 Samba 服务后，系统才会创建或启用同名 Samba 账号，新建用户默认关闭。开启时用户需要输入当前密码，后端用它初始化 Samba 密码；后续用户改密或管理员重置密码时，已开启 Samba 的账号会同步更新 `smbpasswd`。

```bash
sudo cp /etc/samba/smb.conf /etc/samba/smb.conf.bak.$(date +%Y%m%d%H%M%S)
sudo tee -a /etc/samba/smb.conf >/dev/null <<'EOF'

[homes]
   comment = NebulaGrid user home
   browseable = no
   read only = no
   valid users = %S
   create mask = 0644
   directory mask = 0755
EOF
sudo testparm
sudo systemctl restart smbd
```

保存后，用 API 运行用户验证 `sudo -n` 是否能无交互执行。下面以 `ddltm` 为例：

```bash
sudo -u ddltm sudo -n /usr/sbin/useradd --create-home --home-dir /home/ddltm/data/user/nebulagrid_sudo_probe --shell /bin/bash nebulagrid_sudo_probe
printf 'nebulagrid_sudo_probe:temporary-password\n' | sudo -u ddltm sudo -n /usr/sbin/chpasswd
sudo -u ddltm sudo -n /usr/bin/chown -R nebulagrid_sudo_probe:nebulagrid_sudo_probe /home/ddltm/data/user/nebulagrid_sudo_probe
sudo -u ddltm sudo -n /usr/bin/pdbedit -L
printf 'temporary-password\ntemporary-password\n' | sudo -u ddltm sudo -n /usr/bin/smbpasswd -s -a nebulagrid_sudo_probe
sudo -u ddltm sudo -n /usr/bin/smbpasswd -x nebulagrid_sudo_probe
sudo -u ddltm sudo -n /usr/sbin/userdel --remove nebulagrid_sudo_probe
sudo systemctl restart nebulagrid-api
sudo systemctl reload nginx
```

上面的 `nebulagrid_sudo_probe` 只用于验证 sudoers 是否真的允许 API 需要的账户命令。因为 sudoers 会严格匹配命令绝对路径，所以如果这里出现 `interactive authentication is required`、`a password is required` 或 `not allowed to execute`，先修正 `/etc/sudoers.d/nebulagrid-ddltm`，不要只测试 `sudo useradd` 这种相对命令。

不要给 `ddltm` 配置宽泛的 `NOPASSWD: ALL`，这样会扩大系统风险。

## 22. 环境管理验收补充

部署完成后，环境管理建议按以下顺序验收：

1. 登录管理员账号，进入环境管理页面，确认页面会自动刷新环境列表，且 `base` 环境不展示。
2. 点击“刷新环境列表”，确认 `conda env list --json` 中的非 base 环境被同步到数据库。
3. 对一个已有环境点击“检测”，确认返回 Python 版本、PyTorch、TensorFlow、CUDA/cuDNN 和包列表。
4. 用普通用户从自己的文件根目录选择打包好的环境 zip，点击导入并确认。页面应显示 `导入中 -> 修复中 -> 测试中 -> 可用`。
5. 导入后的环境目录应位于 `/home/ddltm/envs/miniconda3/envs/<env_name>`，目录权限为 `755`，普通文件为 `644`，`bin` 或 `Scripts` 下入口文件为 `755`。
6. 检查 `/home/ddltm/data/logs/env_install_logs/env-<env_id>-<env_name>.log`，确认导入、修复、检测记录已经落盘；同时可在 PostgreSQL `env_operation_logs` 表中看到对应结构化记录。
7. 点击“创建副本”，输入新环境名，确认系统复制 `/home/ddltm/envs/miniconda3/envs/<old_env_name>` 到 `/home/ddltm/envs/miniconda3/envs/<new_env_name>`，并显示 `复制中 -> 修复中 -> 测试中 -> 可用`。
8. 在副本里运行 `pip --version` 或 `python -m pip --version`，确认 `pip` shebang 不再指向旧路径。路径修复应覆盖所有文本文件里的旧环境前缀，而不只是 conda metadata。
9. 普通用户只能删除自己导入或复制的环境；管理员可以删除所有环境。删除后数据库记录和 `miniconda3/envs/<env_name>` 目录应同时消失。
10. 普通用户只能查看自己的环境日志；管理员可以查看所有环境日志。

如果出现 `bad interpreter` 或 “错误的解释器”，优先检查环境日志中的修复阶段记录，并在环境目录中搜索旧路径：

```bash
grep -R "/home/.*/envs/<env_name>" /home/ddltm/envs/miniconda3/envs/<env_name> 2>/dev/null | head
```

如果仍有旧路径残留，说明该文件被识别为二进制或超过修复大小上限，需要单独确认文件类型，避免误改二进制包。

## 23. 最小验收清单

部署完成后，至少确认以下项目：

- 主节点 `curl http://127.0.0.1:8000/api/health` 成功。
- 浏览器能打开 `http://主节点IP/`。
- 管理员可以登录。
- 能通过页面登记计算节点。
- 主节点可以 `sudo -u ddltm ssh ddltm@node-a 'nvidia-smi -L'`。
- 计算节点可以看到 `/home/ddltm/envs/nebulagrid_remote/monitor.py`。
- PostgreSQL 中能看到 NebulaGrid 数据表。
- PostgreSQL 中能看到 `file_jobs` 表；打包/解压后 `/api/files/jobs/latest` 能返回最近任务状态。
- PostgreSQL 中能看到 `envs` 表；环境导入、复制、检测和删除后状态与真实目录一致。
- PostgreSQL 中能看到 `tasks`、`task_allocations`、`task_events`、`task_runtime_guards` 和 `env_install_jobs` 表；任务提交、调度、日志读取和环境安装作业状态都能落库。
- `/home/ddltm/data/logs/env_install_logs/` 中能看到单环境 JSON Lines 日志文件，数据库 `env_operation_logs` 中能看到同源结构化日志。
- InfluxDB 中能查询到 `node_metrics` / `gpu_metrics` 监控点。
- systemd 中 API 和 worker 服务为 running。

## 24. 当前版本边界

当前代码适合部署后开始真实机器联调。以下能力还需要继续开发完善：

- 任务服务已切换到数据库 CRUD，并支持任务可见性、批量提交、修改、挂起、删除后继确认、中止、重新提交、日志读取和历史区默认 100 条加载。
- 调度器已执行真实 GPU 选择和 allocation 事务：紧急任务优先，校验前驱任务、节点可见性、GPU 数量、GPU 型号和 GPU 复用策略，并写入任务事件与运行时守护记录。
- 执行器已通过 SSH 调用远端 runner 启动任务，记录 PID/PGID、读取状态文件、归档返回码并释放 allocation；仍需在真实节点验证 SSH key、NFS 路径、conda 激活和中止回收。
- runtime guard 已按远端 PID 树和 GPU UUID 检查实际 PID/GPU 使用并处理 `alloc_error`；仍需在真实多进程训练和节点异常场景下压测。
- env install worker 已从 `env_install_jobs` 领取本机和 compile 安装作业，执行受控安装命令并写回返回码、包状态和环境日志；上传文件真实落盘、sha256 校验、运行中安装取消和资源隔离仍需继续完善。
- Alembic 数据库迁移；当前初始化使用 ORM `create_all`，适合 MVP 部署和测试。
