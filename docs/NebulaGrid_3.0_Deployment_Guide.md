# NebulaGrid 3.0 后端部署文档

本文面向一台主节点和多台计算节点的实验室部署。主节点运行 API、PostgreSQL、Redis、NFS、调度器和后台 worker；计算节点通过 NFS 挂载 `/data`，并允许主节点使用统一主账户 SSH 执行受控脚本。

## 1. 机器规划

示例约定如下，请按实际环境替换：

| 角色 | 主机名 | IP | 说明 |
|---|---|---|---|
| 主节点 | master | 192.168.1.10 | API、DB、Redis、NFS server、worker |
| 计算节点 | node-a | 192.168.1.21 | GPU 任务执行节点 |
| 计算节点 | node-b | 192.168.1.22 | GPU 任务执行节点 |

所有节点必须保证：

- Linux，推荐 Ubuntu Server 22.04 LTS。
- 主节点可以 SSH 到所有计算节点。
- 所有节点都存在同名主账户，例如 `ddltm`。
- `/data` 在所有节点路径完全一致。
- 计算节点安装 NVIDIA 驱动，`nvidia-smi` 可用。

## 2. 主节点系统依赖

```bash
sudo apt update
sudo apt install -y \
  build-essential curl git nginx nfs-kernel-server postgresql postgresql-contrib \
  redis-server openssh-client
```

确认服务可用：

```bash
sudo systemctl enable --now postgresql redis-server nfs-kernel-server nginx
systemctl status postgresql redis-server nfs-kernel-server nginx
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
sudo mkdir -p /data/user /data/logs/task_logs /data/logs/env_install_logs
sudo mkdir -p /data/runtime /data/backups /data/env_packages
sudo mkdir -p /data/envs/miniconda /data/envs/user_envs /data/envs/nebulagrid_remote
sudo chown -R ddltm:ddltm /data
sudo chmod 750 /data
```

配置 NFS exports：

```bash
sudo tee /etc/exports.d/nebulagrid.exports >/dev/null <<'EOF'
/data 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)
EOF
sudo exportfs -ra
sudo exportfs -v
```

计算节点安装客户端并挂载：

```bash
sudo apt update
sudo apt install -y nfs-common
sudo mkdir -p /data
sudo mount -t nfs master:/data /data
```

写入 `/etc/fstab`：

```bash
echo 'master:/data /data nfs defaults,_netdev 0 0' | sudo tee -a /etc/fstab
```

验证读写：

```bash
sudo -u ddltm touch /data/runtime/nfs-test-from-$(hostname)
ls -l /data/runtime/
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

## 7. Miniconda base 环境

本文按你的要求使用 miniconda 的 base 环境运行后端。

在主节点安装 Miniconda：

```bash
cd /tmp
curl -fsSLO https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p /data/envs/miniconda
sudo chown -R ddltm:ddltm /data/envs/miniconda
```

初始化 shell：

```bash
sudo -u ddltm /data/envs/miniconda/bin/conda init bash
```

升级 base 基础工具：

```bash
sudo -u ddltm /data/envs/miniconda/bin/python -m pip install -U pip setuptools wheel
```

## 8. 部署代码

示例部署到 `/opt/nebulagrid/current`：

```bash
sudo mkdir -p /opt/nebulagrid
sudo chown -R ddltm:ddltm /opt/nebulagrid
sudo -u ddltm git clone <your-repo-url> /opt/nebulagrid/current
```

如果用离线包部署，把代码目录同步到 `/opt/nebulagrid/current` 即可。

安装后端 Python 包：

```bash
cd /opt/nebulagrid/current/backend
sudo -u ddltm /data/envs/miniconda/bin/python -m pip install -e ".[dev]"
```

生产环境如果不需要测试依赖，可改为：

```bash
sudo -u ddltm /data/envs/miniconda/bin/python -m pip install -e .
```

## 9. 环境变量配置

创建配置文件：

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
NEBULAGRID_SCHEDULER_INTERVAL_SECONDS=5
NEBULAGRID_MONITOR_INTERVAL_SECONDS=5
EOF
sudo chown root:ddltm /etc/nebulagrid/backend.env
sudo chmod 640 /etc/nebulagrid/backend.env
```

## 10. 初始化数据库

```bash
cd /opt/nebulagrid/current/backend
sudo -u ddltm env $(cat /etc/nebulagrid/backend.env | xargs) \
  /data/envs/miniconda/bin/python scripts/init_db.py
```

默认会创建：

- 全部 ORM 数据表。
- 管理员账号 `admin`。
- 默认密码 `admin123`。
- 基础 settings 项。

首次登录后请立即修改管理员密码。当前代码仍是 MVP 初始化逻辑，正式上线前建议把默认密码改为一次性随机密码并写入部署日志。

## 11. 下发远端脚本

将远端脚本同步到 NFS 共享目录，计算节点会通过同一路径访问：

```bash
sudo -u ddltm rsync -av /opt/nebulagrid/current/backend/app/remote/ /data/envs/nebulagrid_remote/
sudo -u ddltm chmod +x /data/envs/nebulagrid_remote/*.py
```

计算节点验证：

```bash
sudo -u ddltm ssh node-a '/data/envs/miniconda/bin/python /data/envs/nebulagrid_remote/monitor.py'
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
WorkingDirectory=/opt/nebulagrid/current/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/data/envs/miniconda/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
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
WorkingDirectory=/opt/nebulagrid/current/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/data/envs/miniconda/bin/python -m ${module}
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

    client_max_body_size 1024m;

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
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

## 16. 当前实现边界

当前后端已经具备：

- FastAPI API 结构、统一响应、鉴权依赖和 RBAC 入口。
- SQLAlchemy ORM 数据模型和初始化脚本。
- 任务、节点、文件、环境、用户、审计、设置等 API 契约。
- worker 进程入口和远端脚本骨架。
- 基于 NFS 的路径规划和部署流程。

仍需在真实机器上继续完善：

- API service 从内存仓库切换为数据库 CRUD。
- scheduler 的事务化 GPU 选择和 allocation 写入。
- executor 的 SSH 调用、远端 runner 启动、停止和返回码回收。
- monitor 的 SSH 指标采集入库。
- runtime guard 的 PID/GPU 越权检测和 alloc_error 状态流转。
- env worker 的上传文件落盘、sha256 校验、normal/compile 安装执行。
- Alembic 迁移脚本；当前 `create_all` 适合 MVP 初始化，不适合长期生产演进。

建议真实机器测试顺序：

1. 先验证 NFS、SSH、miniconda、PostgreSQL、Redis。
2. 启动 API 并完成登录、节点登记、任务提交 API 测试。
3. 再逐步接 scheduler/executor/monitor 的真实 SSH 行为。
4. 最后开启环境包安装和 runtime guard 的强控制逻辑。

