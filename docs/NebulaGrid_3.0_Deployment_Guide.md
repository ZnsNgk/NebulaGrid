# NebulaGrid 3.0 后端部署文档

本文面向一台主节点和多台计算节点的实验室部署。主节点运行 API、PostgreSQL、InfluxDB、Redis、NFS、调度器和后台 worker；计算节点通过 NFS 挂载 `/home/ddltm/data` 和 `/home/ddltm/envs`，并允许主节点使用统一主账户 SSH 执行受控脚本。

## 1. 机器规划

示例约定如下，请按实际环境替换：

| 角色 | 主机名 | IP | 说明 |
|---|---|---|---|
| 主节点 | master | 192.168.1.10 | API、PostgreSQL、InfluxDB、Redis、NFS server、worker |
| 计算节点 | node-a | 192.168.1.21 | GPU 任务执行节点 |
| 计算节点 | node-b | 192.168.1.22 | GPU 任务执行节点 |

所有节点必须保证：

- Linux，推荐 Ubuntu Server 22.04 LTS。
- 主节点可以 SSH 到所有计算节点。
- 所有节点都存在同名主账户，例如 `ddltm`。
- `/home/ddltm/data` 和 `/home/ddltm/envs` 在所有节点路径完全一致。
- 计算节点安装 NVIDIA 驱动，`nvidia-smi` 可用。

## 2. 主节点系统依赖

```bash
sudo apt update
sudo apt install -y \
  build-essential curl git nginx nfs-kernel-server postgresql postgresql-contrib \
  redis-server influxdb2 openssh-client
```

确认服务可用：

```bash
sudo systemctl enable --now postgresql influxdb redis-server nfs-kernel-server nginx
systemctl status postgresql influxdb redis-server nfs-kernel-server nginx
```

## 3. 创建统一主账户

主节点和所有计算节点都创建同名主账户。UID/GID 建议固定，避免 NFS 权限错乱。

```bash
sudo groupadd -g 2000 ddltm || true
sudo useradd -m -u 2000 -g 2000 -s /bin/bash ddltm || true
sudo passwd ddltm
```

为主节点生成 SSH key：

```bash
sudo -u ddltm ssh-keygen -t ed25519 -f /home/ddltm/.ssh/id_ed25519 -N ""
```

把公钥加入每台计算节点的 `/home/ddltm/.ssh/authorized_keys`：

```bash
sudo -u ddltm ssh-copy-id node-a
sudo -u ddltm ssh-copy-id node-b
```

测试：

```bash
sudo -u ddltm ssh node-a 'hostname && id && nvidia-smi -L'
```

## 4. NFS 共享目录

主节点创建数据目录：

```bash
sudo mkdir -p /home/ddltm/data/user /home/ddltm/data/logs/task_logs /home/ddltm/data/logs/env_install_logs
sudo mkdir -p /home/ddltm/data/runtime /home/ddltm/data/backups /home/ddltm/envs/packages
sudo mkdir -p /home/ddltm/envs/miniconda3/envs /home/ddltm/envs/nebulagrid_remote
sudo chown -R ddltm:ddltm /home/ddltm/data
sudo chown -R ddltm:ddltm /home/ddltm/envs
sudo chmod 750 /home/ddltm/data
sudo chmod 750 /home/ddltm/envs
```

配置 NFS exports：

```bash
sudo tee /etc/exports.d/nebulagrid.exports >/dev/null <<'EOF'
/home/ddltm/data 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)
/home/ddltm/envs 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)
EOF
sudo exportfs -ra
sudo exportfs -v
```

计算节点安装客户端并挂载：

```bash
sudo apt update
sudo apt install -y nfs-common
sudo mkdir -p /home/ddltm/data
sudo mkdir -p /home/ddltm/envs
sudo mount -t nfs master:/home/ddltm/data /home/ddltm/data
sudo mount -t nfs master:/home/ddltm/envs /home/ddltm/envs
```

写入 `/etc/fstab`：

```bash
echo 'master:/home/ddltm/data /home/ddltm/data nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
echo 'master:/home/ddltm/envs /home/ddltm/envs nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
```

验证读写：

```bash
sudo -u ddltm touch /home/ddltm/data/runtime/nfs-test-from-$(hostname)
sudo -u ddltm touch /home/ddltm/envs/nfs-test-from-$(hostname)
ls -l /home/ddltm/data/runtime/
ls -l /home/ddltm/envs/
```

## 5. PostgreSQL 数据库

主节点创建数据库和用户：

```bash
sudo -u postgres psql
```

```sql
CREATE USER nebulagrid WITH PASSWORD 'replace-with-strong-password';
CREATE DATABASE nebulagrid OWNER nebulagrid;
GRANT ALL PRIVILEGES ON DATABASE nebulagrid TO nebulagrid;
\q
```

仅本机部署时无需开放 PostgreSQL 远程访问。若数据库独立部署，再修改 `postgresql.conf` 和 `pg_hba.conf`。

## 6. Redis

本机 Redis 默认即可：

```bash
redis-cli ping
```

期望返回：

```text
PONG
```

## 6.5 InfluxDB

InfluxDB 保存节点历史监控指标，PostgreSQL 只保存节点和 GPU 清单。初始化 org、bucket 和 token：

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

## 7. Miniconda base 环境

本文按你的要求使用 miniconda 的 base 环境运行后端。

在主节点安装 Miniconda：

```bash
cd /tmp
curl -fsSLO https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p /home/ddltm/envs/miniconda3
sudo chown -R ddltm:ddltm /home/ddltm/envs/miniconda3
```

初始化 shell：

```bash
sudo -u ddltm /home/ddltm/envs/miniconda3/bin/conda init bash
```

升级 base 基础工具：

```bash
sudo -u ddltm /home/ddltm/envs/miniconda3/bin/python -m pip install -U pip setuptools wheel
```

## 8. 部署代码

示例部署到 `/home/ddltm/master`：

```bash
sudo mkdir -p /home/ddltm/master
sudo chown -R ddltm:ddltm /home/ddltm/master
sudo -u ddltm git clone <your-repo-url> /home/ddltm/master
```

如果用离线包部署，把代码目录同步到 `/home/ddltm/master` 即可。

安装后端 Python 包：

```bash
cd /home/ddltm/master/backend
sudo -u ddltm /home/ddltm/envs/miniconda3/bin/python -m pip install -e ".[dev]"
```

生产环境如果不需要测试依赖，可改为：

```bash
sudo -u ddltm /home/ddltm/envs/miniconda3/bin/python -m pip install -e .
```

## 9. 环境变量配置

创建配置文件：

```bash
sudo mkdir -p /etc/nebulagrid
sudo tee /etc/nebulagrid/backend.env >/dev/null <<'EOF'
NEBULAGRID_APP_NAME=NebulaGrid
NEBULAGRID_ENV=production
NEBULAGRID_MANAGE_LINUX_ACCOUNTS=true
NEBULAGRID_DATABASE_URL=postgresql+psycopg://nebulagrid:replace-with-strong-password@127.0.0.1:5432/nebulagrid
NEBULAGRID_REDIS_URL=redis://127.0.0.1:6379/0
NEBULAGRID_INFLUXDB_URL=http://127.0.0.1:8086
NEBULAGRID_INFLUXDB_ORG=nebulagrid
NEBULAGRID_INFLUXDB_BUCKET=nebulagrid_metrics
NEBULAGRID_INFLUXDB_TOKEN=replace-with-influx-token
NEBULAGRID_INFLUXDB_LATEST_RANGE=30m
NEBULAGRID_DATA_ROOT=/home/ddltm/data
NEBULAGRID_USER_HOME_ROOT=/home/ddltm/data/user
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
NEBULAGRID_SCHEDULER_INTERVAL_SECONDS=5
NEBULAGRID_MONITOR_INTERVAL_SECONDS=5
EOF
sudo chown root:ddltm /etc/nebulagrid/backend.env
sudo chmod 640 /etc/nebulagrid/backend.env
```

## 10. 初始化数据库

```bash
cd /home/ddltm/master/backend
sudo -u ddltm env $(cat /etc/nebulagrid/backend.env | xargs) \
  /home/ddltm/envs/miniconda3/bin/python scripts/init_db.py
```

默认会创建：

- 全部 ORM 数据表。
- 管理员账号 `admin`。
- 默认密码 `admin123`。
- 基础 settings 项。

首次登录后请立即修改管理员密码。当前代码仍是 MVP 初始化逻辑，正式上线前建议把默认密码改为一次性随机密码并写入部署日志。

旧版本如果创建过 PostgreSQL 监控表，执行一次清理脚本。节点历史指标现在保存在 InfluxDB：

```bash
sudo -u ddltm env $(cat /etc/nebulagrid/backend.env | xargs) \
  /home/ddltm/envs/miniconda3/bin/python scripts/drop_postgres_metrics_tables.py
```

## 11. 下发远端脚本

将远端脚本同步到 NFS 共享目录，计算节点会通过同一路径访问：

```bash
sudo -u ddltm rsync -av /home/ddltm/master/backend/app/remote/ /home/ddltm/envs/nebulagrid_remote/
sudo -u ddltm chmod +x /home/ddltm/envs/nebulagrid_remote/*.py
```

计算节点验证：

```bash
sudo -u ddltm ssh node-a '/home/ddltm/envs/miniconda3/bin/python /home/ddltm/envs/nebulagrid_remote/monitor.py'
```

## 12. systemd 服务

### API 服务

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

### 后台 worker

```bash
for name in scheduler node_monitor task_executor runtime_guard env_install_worker; do
  unit="nebulagrid-${name}.service"
  module="app.workers.${name}"
  sudo tee /etc/systemd/system/${unit} >/dev/null <<EOF
[Unit]
Description=NebulaGrid ${name}
After=network.target postgresql.service redis-server.service nebulagrid-api.service

[Service]
User=ddltm
Group=ddltm
WorkingDirectory=/home/ddltm/master/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/home/ddltm/envs/miniconda3/bin/python -m ${module}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
done
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nebulagrid-api
sudo systemctl enable --now nebulagrid-scheduler nebulagrid-node_monitor nebulagrid-task_executor nebulagrid-runtime_guard nebulagrid-env_install_worker
```

查看日志：

```bash
journalctl -u nebulagrid-api -f
journalctl -u nebulagrid-scheduler -f
```

## 13. Nginx 反向代理

```bash
sudo tee /etc/nginx/sites-available/nebulagrid.conf >/dev/null <<'EOF'
server {
    listen 80;
    server_name nebulagrid.local;

    # 文件管理会上传代码包、模型包或数据压缩包。Nginx 默认只有 1m，
    # 如果这里过小，请求会在到达 FastAPI 前直接返回 413。
    client_max_body_size 20g;
    client_body_timeout 3600s;

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location /api/docs {
        proxy_pass http://127.0.0.1:8000/api/docs;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/nebulagrid.conf /etc/nginx/sites-enabled/nebulagrid.conf
sudo nginx -t
sudo systemctl reload nginx
```

如果上传文件时出现 `413 Request Entity Too Large`，说明当前生效的 Nginx 配置仍然限制了请求体大小。确认并重载：

```bash
sudo nginx -T | grep -n client_max_body_size
sudo nginx -t
sudo systemctl reload nginx
```

生产环境请接入 HTTPS 证书。

## 14. 健康检查

```bash
curl -f http://127.0.0.1:8000/api/health
curl -f http://127.0.0.1/api/health
```

登录测试：

```bash
curl -s http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"identity":"admin","password":"admin123"}'
```

## 15. 登记计算节点

拿到登录返回的 token 后：

```bash
TOKEN='<access_token>'
curl -s http://127.0.0.1:8000/api/admin/nodes \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"name":"node-a","ip":"192.168.1.21","ssh_user":"ddltm","gpu_models":["A100","A100"]}'
```

## 16. SSH 子账户同步

生产环境启用 `NEBULAGRID_MANAGE_LINUX_ACCOUNTS=true` 后，API 服务会在创建用户、启动补齐历史用户、修改密码和删除用户时维护 Linux 子账户。API 服务运行在 systemd 中，没有交互终端，所以不能依赖 sudo 密码缓存，也不能让后端保存 sudo 密码；这里必须给 API 运行用户配置受限的 `NOPASSWD` sudoers。

先确认 API 实际运行用户：

```bash
systemctl cat nebulagrid-api
systemctl show nebulagrid-api -p User -p Group
```

如果 `User=` 不是 `ddltm`，下面 sudoers 左侧的用户名也要改成实际用户。以 `ddltm` 为例：

```bash
sudo visudo -f /etc/sudoers.d/nebulagrid-ddltm
```

写入：

```text
ddltm ALL=(root) NOPASSWD: /usr/sbin/useradd, /usr/sbin/usermod, /usr/sbin/userdel, /usr/sbin/chpasswd, /usr/bin/mkdir, /usr/bin/chown, /usr/bin/chmod, /usr/bin/find, /usr/bin/setfacl
```

保存后用 API 运行用户和绝对路径验证 sudoers 是否生效：

```bash
sudo -u ddltm sudo -n /usr/sbin/useradd --create-home --home-dir /home/ddltm/data/user/nebulagrid_sudo_probe --shell /bin/bash nebulagrid_sudo_probe
printf 'nebulagrid_sudo_probe:temporary-password\n' | sudo -u ddltm sudo -n /usr/sbin/chpasswd
sudo -u ddltm sudo -n /usr/bin/chown -R nebulagrid_sudo_probe:nebulagrid_sudo_probe /home/ddltm/data/user/nebulagrid_sudo_probe
sudo -u ddltm sudo -n /usr/sbin/userdel --remove nebulagrid_sudo_probe
```

如果出现 `interactive authentication is required`、`a password is required` 或 `not allowed to execute`，说明 sudoers 没有匹配到 API 实际执行的绝对路径，或者授权用户不是 API 实际运行用户，需要先修正授权。

平台用户可用自己的用户名和密码 SSH 到 master，默认 home 为 `/home/ddltm/data/user/<user_name>`。`NEBULAGRID_MAIN_LINUX_USER` 对应的主账户会被保护，不会被平台删改系统密码。平台会把用户目录设置为 `755`，便于实验室成员互相读取和拷贝文件。

## 17. 文件打包/解压状态持久化

文件管理中的目录打包和压缩包解压任务会写入 PostgreSQL 的 `file_jobs` 表，而不是保存在 API 进程内存中。这样页面刷新、重新登录、API 重启或多 worker 部署时，前端仍可通过 `/api/files/jobs/latest` 读取当前用户最近一次任务状态。API 启动时会把上次进程遗留的 `pending/running` 文件任务标记为失败，避免重启后长期占用并发名额。

当前目录打包生成 zip；解压支持 `.zip`、`.tar`、`.tar.gz`、`.tgz`、`.tar.bz2`、`.tbz2`、`.tar.xz` 和 `.txz`。系统限制同一用户同时只能运行一个文件打包/解压任务，并设置全局并发上限，避免共享盘 IO 被大量压缩任务打满。

## 18. 环境管理当前实现

环境管理页面会在进入页面和点击“刷新环境列表”时执行 `conda env list --json`，并把 `NEBULAGRID_CONDA_ENV_ROOT` 下除 `base` 外的环境同步到 `envs` 表。当前环境元数据已经落库，页面刷新和 API 重启不会丢失环境记录。

当前已经支持：

- 环境列表：展示来源、状态、路径、Python 版本、所有人和操作按钮。
- 环境检测：后端通过 `source <miniconda>/bin/activate && conda activate <env_name>` 激活环境，再运行 `remote/env_probe.py`，返回 Python、PyTorch、TensorFlow、CUDA/cuDNN 和包列表。
- 环境导入：用户从自己的文件根目录选择 zip 包，后端解压到临时目录，复制到 `miniconda3/envs/<env_name>`，修复路径和权限，测试通过后设为可用。
- 环境副本：用户可基于任意可用环境创建自己的副本，后端复制 `miniconda3/envs/<old_env_name>` 到 `miniconda3/envs/<new_env_name>`，并执行路径修复、权限修复和检测。
- 环境删除：普通用户只能删除自己的环境，管理员可以删除所有环境；删除会同时清理数据库记录和 `miniconda3/envs/<env_name>` 目录。
- 环境日志：每个环境一个日志文件，位于 `NEBULAGRID_ENV_INSTALL_LOG_ROOT/env-<env_id>-<env_name>.log`。管理员可查看全部日志，普通用户只能查看自己的日志。

路径修复不是只处理 conda 元数据。当前实现会扫描环境内所有文本文件，提取旧环境前缀并替换为新路径，覆盖 `pip` shebang、`.pth`、包配置、metadata、conda 记录等常见残留；含空字节或明显二进制的文件会跳过，避免误改 `.so`、`.pyd` 等二进制包。

导入或复制后的权限规则：

- 目录：`755`
- 普通文件：`644`
- `bin/` 和 `Scripts/` 下入口文件：`755`
- 如果 API 以 root 运行，环境属主会调整为 `NEBULAGRID_MAIN_LINUX_USER`；如果 API 以普通用户运行，则只执行 chmod，不强行 chown。

环境管理相关日志和真实环境目录都在 NFS 共享路径中，因此主节点和计算节点必须看到完全一致的 `/home/ddltm/envs/miniconda3/envs` 路径。否则环境检测可能通过，但计算节点执行任务时仍会找不到解释器或包路径。

## 19. 当前实现边界

当前后端已经具备：

- FastAPI API 结构、统一响应、鉴权依赖和 RBAC 入口。
- SQLAlchemy ORM 数据模型和初始化脚本。
- 任务、节点、文件、环境、用户、审计、设置等 API 契约。
- worker 进程入口和远端脚本骨架。
- 基于 NFS 的路径规划和部署流程。

管理员审计日志已经落库到 PostgreSQL `audit_logs` 表，`/api/admin/audit-logs` 支持 `page`、`page_size` 和 `category` 查询参数。后台审计页按系统操作、用户操作、压缩文件、文件操作、任务操作、环境操作、节点操作和其他分类展示，压缩/解压分别对应 `file.archive` 与 `file.extract`。

仍需在真实机器上继续完善：

- 任务服务从内存仓库切换为数据库 CRUD。
- scheduler 的事务化 GPU 选择和 allocation 写入。
- executor 的 SSH 调用、远端 runner 启动、停止和返回码回收。
- monitor 的 SSH 指标采集写入 InfluxDB，PostgreSQL 不再保存 node/gpu metrics 表。
- runtime guard 的 PID/GPU 越权检测和 alloc_error 状态流转。
- env worker 的上传文件落盘、sha256 校验、normal/compile 安装执行仍需继续打通；环境导入、复制、检测、日志和删除已经由 API 服务实现。
- Alembic 迁移脚本；当前 `create_all` 适合 MVP 初始化，不适合长期生产演进。

建议真实机器测试顺序：

1. 先验证 NFS、SSH、miniconda、PostgreSQL、InfluxDB、Redis。
2. 启动 API 并完成登录、节点登记、任务提交 API 测试。
3. 再逐步接 scheduler/executor/monitor 的真实 SSH 行为。
4. 最后开启环境包安装和 runtime guard 的强控制逻辑。
