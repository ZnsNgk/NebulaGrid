# NebulaGrid 3.0 完整部署教程

本文是一份从零开始的部署教程，目标是让没有接触过 NebulaGrid 的同学也能把系统跑起来。教程覆盖主节点、计算节点、NFS、PostgreSQL、Redis、Miniconda base 环境、后端依赖、数据库初始化、systemd 服务、Nginx、前端页面和基础验证。

> 重要说明：当前仓库已经具备 API、前端测试控制台、数据库表结构、worker 入口和远端脚本骨架。调度器、执行器、节点监控和环境安装 worker 的真实硬件闭环仍需继续接入 SSH 执行和数据库 CRUD。本教程用于部署当前代码并开始联调真实机器。

## 0. 部署拓扑

示例机器：

| 角色 | 示例主机名 | 示例 IP | 作用 |
|---|---|---|---|
| 主节点 | `master` | `192.168.1.10` | API、前端静态文件、PostgreSQL、Redis、NFS server、worker |
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
- 所有机器的 `/data` 路径一致。
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
  openssh-client
```

启动基础服务：

```bash
sudo systemctl enable --now postgresql
sudo systemctl enable --now redis-server
sudo systemctl enable --now nfs-kernel-server
sudo systemctl enable --now nginx
```

检查服务：

```bash
systemctl status postgresql --no-pager
systemctl status redis-server --no-pager
systemctl status nfs-kernel-server --no-pager
systemctl status nginx --no-pager
redis-cli ping
```

`redis-cli ping` 应返回：

```text
PONG
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

创建 NebulaGrid 数据目录：

```bash
sudo mkdir -p /data/user
sudo mkdir -p /data/logs/task_logs
sudo mkdir -p /data/logs/env_install_logs
sudo mkdir -p /data/runtime
sudo mkdir -p /data/backups
sudo mkdir -p /data/env_packages
sudo mkdir -p /data/envs/miniconda
sudo mkdir -p /data/envs/user_envs
sudo mkdir -p /data/envs/nebulagrid_remote
sudo chown -R ddltm:ddltm /data
sudo chmod 750 /data
```

写入 NFS export 配置。请把 `192.168.1.0/24` 替换成你的实际内网网段。

```bash
sudo tee /etc/exports.d/nebulagrid.exports >/dev/null <<'EOF'
/data 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)
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
sudo mkdir -p /data
```

临时挂载测试。请把 `master` 替换成主节点主机名或 IP。

```bash
sudo mount -t nfs master:/data /data
```

如果使用 IP：

```bash
sudo mount -t nfs 192.168.1.10:/data /data
```

写入开机自动挂载：

```bash
echo 'master:/data /data nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
```

如果使用 IP：

```bash
echo '192.168.1.10:/data /data nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
```

验证 NFS 读写：

```bash
sudo -u ddltm touch /data/runtime/nfs-test-$(hostname)
ls -l /data/runtime/
```

主节点和计算节点都应该能看到同一批测试文件。

## 7. 主节点安装 Miniconda base 环境

以下命令在主节点执行。

下载并安装 Miniconda 到 NFS 目录：

```bash
cd /tmp
curl -fsSLO https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p /data/envs/miniconda
sudo chown -R ddltm:ddltm /data/envs/miniconda
```

升级 pip：

```bash
sudo -u ddltm /data/envs/miniconda/bin/python -m pip install -U pip setuptools wheel
```

验证：

```bash
sudo -u ddltm /data/envs/miniconda/bin/python --version
sudo -u ddltm /data/envs/miniconda/bin/pip --version
```

计算节点也应该能通过 NFS 访问同一路径：

```bash
sudo -u ddltm ssh ddltm@node-a '/data/envs/miniconda/bin/python --version'
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

推荐代码位置有两种。

方式 A 是系统级部署目录：

```text
/opt/nebulagrid/current
```

方式 B 是主账户目录，适合你希望尽量不使用 sudo 的部署方式：

```text
/home/ddltm/master
```

如果选择方式 B，后文所有 `/opt/nebulagrid/current` 都替换为 `/home/ddltm/master`。

### 9.1 系统级目录方式

创建目录：

```bash
sudo mkdir -p /opt/nebulagrid
sudo chown -R ddltm:ddltm /opt/nebulagrid
```

方式 A：用 git 拉取：

```bash
sudo -u ddltm git clone <your-repo-url> /opt/nebulagrid/current
```

方式 B：从本地电脑拷贝：

```bash
rsync -av --delete ./NebulaGrid/ ddltm@master:/opt/nebulagrid/current/
```

方式 C：用 scp 拷贝压缩包：

```bash
tar czf nebulagrid.tar.gz NebulaGrid
scp nebulagrid.tar.gz ddltm@master:/tmp/
ssh ddltm@master
mkdir -p /opt/nebulagrid/current
tar xzf /tmp/nebulagrid.tar.gz -C /opt/nebulagrid/current --strip-components=1
```

确认目录结构：

```bash
ls -la /opt/nebulagrid/current
ls -la /opt/nebulagrid/current/backend
ls -la /opt/nebulagrid/current/frontend
```

### 9.2 主账户目录方式，推荐少 sudo 测试部署

以下命令以 `ddltm` 身份执行，不需要 sudo：

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

以下命令在主节点执行。

```bash
cd /opt/nebulagrid/current/backend
sudo -u ddltm /data/envs/miniconda/bin/python -m pip install -e .
```

如果代码部署在 `/home/ddltm/master`，并且当前已经是 `ddltm` 用户，则使用：

```bash
cd /home/ddltm/master/backend
/data/envs/miniconda/bin/python -m pip install -e .
```

如果你希望运行测试，也安装 dev 依赖：

```bash
sudo -u ddltm /data/envs/miniconda/bin/python -m pip install -e ".[dev]"
```

主账户目录方式：

```bash
/data/envs/miniconda/bin/python -m pip install -e ".[dev]"
```

验证关键包：

```bash
sudo -u ddltm /data/envs/miniconda/bin/python - <<'PY'
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
NEBULAGRID_DATABASE_URL=postgresql+psycopg://nebulagrid:replace-with-strong-password@127.0.0.1:5432/nebulagrid
NEBULAGRID_REDIS_URL=redis://127.0.0.1:6379/0
NEBULAGRID_DATA_ROOT=/data
NEBULAGRID_USER_HOME_ROOT=/data/user
NEBULAGRID_WORKSPACE_ALIAS=/workspace
NEBULAGRID_VISIBLE_ROOTS=/data/user,/data/envs/user_envs,/data/envs/miniconda
NEBULAGRID_TASK_LOG_ROOT=/data/logs/task_logs
NEBULAGRID_ENV_PACKAGE_ROOT=/data/env_packages
NEBULAGRID_ENV_INSTALL_LOG_ROOT=/data/logs/env_install_logs
NEBULAGRID_RUNTIME_ROOT=/data/runtime
NEBULAGRID_REMOTE_CODE_ROOT=/data/envs/nebulagrid_remote
NEBULAGRID_MAIN_LINUX_USER=ddltm
NEBULAGRID_SESSION_SECRET=replace-with-random-secret
NEBULAGRID_CORS_ORIGINS=http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1,http://localhost,null
NEBULAGRID_SCHEDULER_INTERVAL_SECONDS=5
NEBULAGRID_MONITOR_INTERVAL_SECONDS=5
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

以下命令在主节点执行。

```bash
cd /opt/nebulagrid/current/backend
sudo -u ddltm bash -lc 'set -a; source /etc/nebulagrid/backend.env; set +a; /data/envs/miniconda/bin/python scripts/init_db.py'
```

主账户目录方式：

```bash
cd /home/ddltm/master/backend
bash -lc 'set -a; source /etc/nebulagrid/backend.env; set +a; /data/envs/miniconda/bin/python scripts/init_db.py'
```

初始化完成后会创建：

- 数据库表。
- 默认管理员账号：`admin`。
- 默认管理员密码：`admin123`。
- 基础 settings 项。

验证数据库表：

```bash
psql 'postgresql://nebulagrid:replace-with-strong-password@127.0.0.1:5432/nebulagrid' -c '\dt'
```

> 第一次登录后请尽快修改默认密码。当前初始化脚本用于 MVP 测试，正式上线前建议改成随机一次性密码。

## 13. 同步远端脚本到 NFS

以下命令在主节点执行。

```bash
sudo -u ddltm rsync -av /opt/nebulagrid/current/backend/app/remote/ /data/envs/nebulagrid_remote/
sudo -u ddltm chmod +x /data/envs/nebulagrid_remote/*.py
```

主账户目录方式：

```bash
rsync -av /home/ddltm/master/backend/app/remote/ /data/envs/nebulagrid_remote/
chmod +x /data/envs/nebulagrid_remote/*.py
```

在计算节点上验证：

```bash
sudo -u ddltm ssh ddltm@node-a '/data/envs/miniconda/bin/python /data/envs/nebulagrid_remote/monitor.py'
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
WorkingDirectory=/opt/nebulagrid/current/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/data/envs/miniconda/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

如果代码部署在 `/home/ddltm/master`，把上面 service 文件中的：

```text
WorkingDirectory=/opt/nebulagrid/current/backend
```

改成：

```text
WorkingDirectory=/home/ddltm/master/backend
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
WorkingDirectory=/opt/nebulagrid/current/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/data/envs/miniconda/bin/python -m app.workers.scheduler
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
WorkingDirectory=/opt/nebulagrid/current/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/data/envs/miniconda/bin/python -m app.workers.node_monitor
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
WorkingDirectory=/opt/nebulagrid/current/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/data/envs/miniconda/bin/python -m app.workers.task_executor
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
WorkingDirectory=/opt/nebulagrid/current/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/data/envs/miniconda/bin/python -m app.workers.runtime_guard
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
WorkingDirectory=/opt/nebulagrid/current/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/data/envs/miniconda/bin/python -m app.workers.env_install_worker
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

推荐放置位置：

```text
/opt/nebulagrid/current/frontend
```

如果使用主账户目录方式，前端位置是：

```text
/home/ddltm/master/frontend
```

这个目录已经随代码一起放置。确认：

```bash
ls -la /opt/nebulagrid/current/frontend
ls -la /opt/nebulagrid/current/frontend/src
```

主账户目录方式：

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

    client_max_body_size 1024m;

    root /opt/nebulagrid/current/frontend;
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
    }
}
EOF
```

如果使用 `/home/ddltm/master`，把 Nginx 的 `root` 改成：

```nginx
root /home/ddltm/master/frontend;
```

并确保 Nginx 进程能读取主账户目录。Ubuntu 的 Nginx 默认以 `www-data` 运行，`/home/ddltm` 默认可能没有遍历权限。二选一：

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
  -d '{"description":"smoke test","workdir":"/workspace","command":"python --version","requirement":{"need_gpus":0}}'
```

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
ls -la /opt/nebulagrid/current/frontend/index.html
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

### 19.3 数据库连接失败

检查环境变量：

```bash
sudo grep NEBULAGRID_DATABASE_URL /etc/nebulagrid/backend.env
```

检查数据库：

```bash
systemctl status postgresql --no-pager
psql 'postgresql://nebulagrid:replace-with-strong-password@127.0.0.1:5432/nebulagrid' -c 'select 1;'
```

### 19.4 NFS 挂载失败

主节点检查：

```bash
sudo exportfs -v
systemctl status nfs-kernel-server --no-pager
```

计算节点检查：

```bash
showmount -e master
sudo mount -v -t nfs master:/data /data
```

### 19.5 SSH 免密失败

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

### 19.6 Python 包安装慢或失败

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
cd /opt/nebulagrid/current/backend
sudo -u ddltm /data/envs/miniconda/bin/python -m pip install -e .
```

## 20. 更新代码

如果代码来自 git：

```bash
cd /opt/nebulagrid/current
sudo -u ddltm git pull
cd backend
sudo -u ddltm /data/envs/miniconda/bin/python -m pip install -e .
sudo systemctl restart nebulagrid-api nebulagrid-scheduler nebulagrid-node-monitor nebulagrid-task-executor nebulagrid-runtime-guard nebulagrid-env-install-worker
sudo systemctl reload nginx
```

主账户目录方式：

```bash
cd /home/ddltm/master
git pull
cd backend
/data/envs/miniconda/bin/python -m pip install -e .
sudo systemctl restart nebulagrid-api nebulagrid-scheduler nebulagrid-node-monitor nebulagrid-task-executor nebulagrid-runtime-guard nebulagrid-env-install-worker
sudo systemctl reload nginx
```

如果代码来自 rsync/scp，重新同步后执行同样的 pip install 和 systemctl restart。

## 23. 最少 sudo 部署模式总结

如果你希望把代码部署到 `/home/ddltm/master`，推荐把权限边界拆成两部分。

管理员只需要做一次：

- 安装 apt 包：`nginx`、`postgresql`、`redis-server`、`nfs-kernel-server`、`nfs-common` 等。
- 创建 `ddltm` 用户，并保证所有节点 UID/GID 一致。
- 创建 `/data` 并配置 NFS。
- 创建 PostgreSQL 用户和数据库。
- 创建 `/etc/nebulagrid/backend.env`。
- 创建 systemd service 文件。
- 配置 Nginx。

之后 `ddltm` 可以自己完成：

- 更新 `/home/ddltm/master` 里的代码。
- 在 miniconda base 环境里安装 Python 包。
- 运行 `scripts/init_db.py`。
- 同步远端脚本到 `/data/envs/nebulagrid_remote`。
- 使用前端页面登记节点、提交任务、查看状态。

仍然需要 sudo 的操作：

- 安装或升级系统包。
- 修改 `/etc/exports.d/*.exports`。
- 修改 `/etc/nebulagrid/backend.env`。
- 修改 `/etc/systemd/system/*.service`。
- `systemctl restart/reload`。
- 修改 Nginx 配置。

如果希望 `ddltm` 能无密码重启 NebulaGrid 服务，可以让管理员添加一条非常窄的 sudoers 规则：

```bash
sudo visudo -f /etc/sudoers.d/nebulagrid-ddltm
```

写入：

```text
ddltm ALL=(root) NOPASSWD: /bin/systemctl restart nebulagrid-api, /bin/systemctl restart nebulagrid-scheduler, /bin/systemctl restart nebulagrid-node-monitor, /bin/systemctl restart nebulagrid-task-executor, /bin/systemctl restart nebulagrid-runtime-guard, /bin/systemctl restart nebulagrid-env-install-worker, /bin/systemctl reload nginx
```

保存后，`ddltm` 可以执行：

```bash
sudo systemctl restart nebulagrid-api
sudo systemctl reload nginx
```

不要给 `ddltm` 配置宽泛的 `NOPASSWD: ALL`，这样会扩大系统风险。

## 21. 最小验收清单

部署完成后，至少确认以下项目：

- 主节点 `curl http://127.0.0.1:8000/api/health` 成功。
- 浏览器能打开 `http://主节点IP/`。
- 管理员可以登录。
- 能通过页面登记计算节点。
- 主节点可以 `sudo -u ddltm ssh ddltm@node-a 'nvidia-smi -L'`。
- 计算节点可以看到 `/data/envs/nebulagrid_remote/monitor.py`。
- PostgreSQL 中能看到 NebulaGrid 数据表。
- systemd 中 API 和 worker 服务为 running。

## 22. 当前版本边界

当前代码适合部署后开始真实机器联调。以下能力还需要继续开发完善：

- API 服务从内存仓库完全切换到数据库 CRUD。
- 调度器执行真实 GPU 选择和 allocation 事务。
- 执行器通过 SSH 调用远端 runner 启动任务。
- 节点监控 worker 通过 SSH 采集并写入 node/gpu metrics。
- runtime guard 检查实际 PID/GPU 使用并处理 `alloc_error`。
- env install worker 执行真实包安装、日志回收和状态更新。
- Alembic 数据库迁移；当前初始化使用 ORM `create_all`，适合 MVP 部署和测试。
