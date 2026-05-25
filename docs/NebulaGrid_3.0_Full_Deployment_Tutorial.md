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

### 0.1 权限边界速览

本文采用主账户目录部署模式，代码固定放在 `/home/ddltm/master`，便于后续由 `ddltm` 账户完成代码更新、Python 包安装、数据库初始化和远端脚本同步。管理员仍需要负责系统级操作：

- 安装系统包：`nginx`、`postgresql`、`influxdb2`、`redis-server`、`nfs-kernel-server`、`nfs-common`、`samba`。
- 创建 Linux 主账户 `ddltm`，并保证主节点和计算节点 UID/GID 一致。
- 创建和授权 `/home/ddltm/data`、`/home/ddltm/envs`、`/home/ddltm/shared`。
- 配置 NFS、PostgreSQL、InfluxDB、systemd、Nginx 和 `/etc/nebulagrid/backend.env`。
- 生产环境启用 Linux/Samba 子账户同步时，配置受限 `NOPASSWD` sudoers，不能给 `NOPASSWD: ALL`。

完成系统级准备后，`ddltm` 可以自己执行 `/home/ddltm/master` 内的日常代码更新、后端依赖安装、`scripts/init_db.py`、`scripts/sync_remote_scripts.py` 和部署自检。

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
  influxdb2 openssh-client samba acl
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
NEBULAGRID_SCHEDULER_INTERVAL_SECONDS=1
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

参数说明：

| 参数 | 含义 |
|---|---|
| `NEBULAGRID_APP_NAME` | 后端应用名称，主要用于接口元信息、日志和后续页面展示；通常保持 `NebulaGrid`。 |
| `NEBULAGRID_ENV` | 运行环境标识，例如 `production` 或 `development`；用于区分生产和开发配置。 |
| `NEBULAGRID_MANAGE_LINUX_ACCOUNTS` | 是否由 NebulaGrid 自动维护 master 上的平台 Linux 子账户；生产环境需要用户 SSH 到 master 时设为 `true`，并按第 21 节配置受限 sudoers。 |
| `NEBULAGRID_MANAGE_SAMBA_ACCOUNTS` | 是否同步维护 Samba 账号；只有主节点部署 Samba 且希望用户通过 SMB 访问个人目录时设为 `true`。 |
| `NEBULAGRID_DATABASE_URL` | PostgreSQL 连接串，包含数据库用户名、密码、地址、端口和库名；密码要替换为第 8 节创建的真实强密码。 |
| `NEBULAGRID_REDIS_URL` | Redis 连接串；本机默认是 `redis://127.0.0.1:6379/0`。 |
| `NEBULAGRID_INFLUXDB_URL` | InfluxDB HTTP 地址；默认本机部署时为 `http://127.0.0.1:8086`。 |
| `NEBULAGRID_INFLUXDB_ORG` | InfluxDB 组织名，必须与 `influx setup --org` 中的值一致。 |
| `NEBULAGRID_INFLUXDB_BUCKET` | InfluxDB bucket 名，保存节点和 GPU 历史监控指标，必须与初始化 bucket 一致。 |
| `NEBULAGRID_INFLUXDB_TOKEN` | InfluxDB 访问 token，用于写入和查询监控指标；属于敏感值，不要提交到 Git。 |
| `NEBULAGRID_INFLUXDB_LATEST_RANGE` | 普通节点监控读取最近数据的时间范围，例如 `30m` 表示最近 30 分钟。 |
| `NEBULAGRID_INFLUXDB_PRESENTER_RANGE` | 展示者大屏历史曲线读取的时间范围；范围越大，查询数据越多。 |
| `NEBULAGRID_INFLUXDB_PRESENTER_WINDOW` | 展示者大屏历史曲线的聚合窗口，例如 `30s` 表示按 30 秒聚合一个点。 |
| `NEBULAGRID_DATA_ROOT` | 平台数据根目录，通过 NFS 共享到计算节点；包含用户 home、日志、运行时文件和备份目录。 |
| `NEBULAGRID_USER_HOME_ROOT` | 平台用户 home 根目录，用户 `alice` 的目录会落在 `<该路径>/alice`。 |
| `NEBULAGRID_SHARED_FOLDER_ROOT` | 文件管理中“共享文件夹”视图对应的真实目录，所有登录用户可查看并与个人目录互相复制文件。 |
| `NEBULAGRID_VISIBLE_ROOTS` | 后端允许展示或解析的额外真实路径列表，多个路径用英文逗号分隔；至少应包含用户 home 根目录和 miniconda 根目录。 |
| `NEBULAGRID_CONDA_ENV_ROOT` | 用户环境目录，环境列表同步、导入、复制和删除都会以这个目录下的一级子目录为边界。 |
| `NEBULAGRID_TASK_LOG_ROOT` | 任务 stdout/stderr 日志根目录；远端 runner 会把任务日志写到这里。 |
| `NEBULAGRID_ENV_PACKAGE_ROOT` | 环境包、whl、源码包等上传或登记包的存放根目录。 |
| `NEBULAGRID_ENV_INSTALL_LOG_ROOT` | 环境导入、复制、检测、安装包和删除包的日志目录。 |
| `NEBULAGRID_RUNTIME_ROOT` | 运行时状态目录，用于保存任务 PID、PGID、远端状态文件等临时运行信息。 |
| `NEBULAGRID_REMOTE_CODE_ROOT` | 下发到共享环境目录的远端脚本目录，计算节点会从这里执行 `runner.py`、`monitor.py`、`env_probe.py` 和 `env_installer.py`。 |
| `NEBULAGRID_MINICONDA_PYTHON` | 统一 Miniconda 的 Python 解释器路径；后端脚本和环境检测会按该路径寻找 conda 安装。 |
| `NEBULAGRID_MAIN_LINUX_USER` | 平台主账户，例如 `ddltm`；master 和所有计算节点必须保持同名、同 UID、同 GID，远端 SSH 控制和任务 runner 默认使用该账户。 |
| `NEBULAGRID_SESSION_SECRET` | 后端会话或令牌签名密钥；生产环境必须换成长随机字符串，泄露后需要立即轮换并让用户重新登录。 |
| `NEBULAGRID_CORS_ORIGINS` | 允许跨域访问 API 的前端来源列表，多个来源用英文逗号分隔；前端和 API 同域部署时通常保留 `http://127.0.0.1`、`http://localhost`，开发调试可保留 `5173`。 |
| `NEBULAGRID_SCHEDULER_INTERVAL_SECONDS` | 调度器轮询待运行任务和资源分配的间隔秒数，支持 `0.5` 这类小数；推荐试运行先用 `0.5-1`，低于 `0.2` 会被后端限制，避免数据库空转。 |
| `NEBULAGRID_MONITOR_INTERVAL_SECONDS` | 节点监控 worker 采集节点 CPU、内存、GPU 和网络指标的间隔秒数。 |
| `NEBULAGRID_FILE_OPERATION_WORKER_THREADS` | 文件管理专用线程池大小，用于列表、预览、上传、复制、移动、删除、打包和解压；共享盘吞吐不足时不要调太大。 |

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
  -d '{
    "name":"node-a",
    "ip":"192.168.1.21",
    "ssh_user":"ddltm",
    "max_speed_mbps":10000,
    "gpu_schedulable_flags":[1,1,0],
    "owner_user_ids":[310001],
    "access_scope":"private",
    "sharing_scope":"group"
  }'
```

字段说明：

- GPU 数量、型号、UUID 和显存由 `nebulagrid-node-monitor` 通过 `nvidia-smi` 自动扫描并写入数据库；硬件数量变化时下一轮扫描会同步更新 GPU 清单。
- `gpu_schedulable_flags` 必须按该节点 `nvidia-smi` 顺序填写，`1` 表示该 index 可调度，`0` 表示该 index 不参与调度。未配置的 index 会被保守视为不可调度，适合屏蔽亮机卡。
- `owner_user_ids` 是节点所有人 ID 列表，可填写多个用户；管理员后台也提供搜索按钮和复选下拉框选择所有人。
- `access_scope=public` 表示公开使用，`private` 表示私有使用。
- `sharing_scope=none` 表示不共享，仅所有人和管理员可见；`group` 表示组内共享；`public` 表示所有用户可见可用。
- 共享范围只影响普通用户总览页中可用 GPU 和节点卡片的可见性；不可调度 GPU 不计入 GPU 总数、可用 GPU 数，也不会作为任务 GPU 型号选项的来源。

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

### 17.8 创建展示者大屏账号

展示者账号角色为 `viewer`，只拥有 `presenter:read` 权限。该账号登录后前端会进入 `#/presenter` 全屏视图，不显示普通控制台左侧栏目，也不会开放任务、文件、环境、节点管理或用户管理入口。页面左下角保留退出登录按钮，便于公共屏幕维护时手动结束会话。

展示者大屏通过 `GET /api/dashboard/presenter` 读取聚合数据。接口会返回节点数量、在线节点、GPU 总数、等待任务当前数、运行任务当前数、历史任务总数，以及所有计算节点的 CPU、可用内存、网络、GPU、可用显存和历史曲线。GPU 使用率和可用显存按每张 GPU 独立展示，便于公共屏幕定位单卡满载、空闲或显存不足的节点。历史曲线来自 InfluxDB 的 `node_metrics` 和 `gpu_metrics`，默认读取最近 `30m` 并按 `30s` 聚合，可通过 `NEBULAGRID_INFLUXDB_PRESENTER_RANGE` 和 `NEBULAGRID_INFLUXDB_PRESENTER_WINDOW` 调整。

创建展示者账号示例：

```bash
curl -s http://127.0.0.1:8000/api/users \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{
    "username":"viewer",
    "real_name":"展示大屏",
    "role":"viewer",
    "state":"enabled",
    "password":"replace-with-strong-password"
  }'
```

展示者会话不受普通 30 分钟静默在线窗口限制，适合无人值守屏幕长期展示。安全边界仍然保留：主动退出、管理员下线、账号停用或密码变更都会使会话失效。

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
ddltm ALL=(root) NOPASSWD: /usr/sbin/useradd, /usr/sbin/usermod, /usr/sbin/userdel, /usr/sbin/chpasswd, /usr/bin/mkdir, /usr/bin/chown, /usr/bin/chmod, /usr/bin/find, /usr/bin/setfacl, /usr/bin/smbpasswd, /usr/bin/pdbedit, /usr/bin/systemctl restart smbd, /bin/systemctl restart smbd, /bin/systemctl restart nebulagrid-api, /bin/systemctl restart nebulagrid-scheduler, /bin/systemctl restart nebulagrid-node-monitor, /bin/systemctl restart nebulagrid-task-executor, /bin/systemctl restart nebulagrid-runtime-guard, /bin/systemctl restart nebulagrid-env-install-worker, /bin/systemctl reload nginx
```

这样平台创建用户时会创建同名 Linux 账户，用户可用平台用户名和密码 SSH 到 master，并默认进入 `/home/ddltm/data/user/<user_name>`。`NEBULAGRID_MAIN_LINUX_USER` 对应的主账户会被保护，不会被平台删改系统密码。用户在 Web 修改密码或管理员重置密码时，会同步执行 `chpasswd`；删除平台用户时会同步执行 `userdel --remove`。用户目录按隔离策略设置为“目录用户可读写、主账户 `ddltm` 可维护、其他子账户不可直接进入”。这个策略依赖 POSIX ACL：文件仍归上传用户所有，但 `ddltm` 通过 ACL 获得写权限，用于任务日志更新、环境导入和后台清理。

主节点还需要配置 Samba 的 `homes` 动态共享；计算节点不需要安装 Samba。用户在账号管理页当前账号卡片中手动勾选 Samba 服务后，系统才会创建或启用同名 Samba 账号，新建用户默认关闭。开启时用户需要输入当前密码，后端会先确保同名 Linux 子账户存在，再用它初始化 Samba 密码；后续用户改密或管理员重置密码时，已开启 Samba 的账号会同步更新 `smbpasswd`。每次真实执行 Samba 账号变更后，后端会自动执行 `systemctl restart smbd`，让 Windows 共享尽快读取最新账号状态。

`[homes]` 必须显式绑定到 `/home/ddltm/data/user/%S`。不要把主账户 `ddltm` 的 home `/home/ddltm` 作为 Samba 共享根，否则任何拿到旧公共账号的人都可能通过 Windows 文件共享看到主目录。

```bash
sudo cp /etc/samba/smb.conf /etc/samba/smb.conf.bak.$(date +%Y%m%d%H%M%S)
sudo tee -a /etc/samba/smb.conf >/dev/null <<'EOF'

[homes]
   comment = NebulaGrid user home
   path = /home/ddltm/data/user/%S
   browseable = no
   read only = no
   valid users = %S
   force user = %S
   create mask = 0664
   directory mask = 2775
   force directory mode = 2000
   inherit acls = yes
   map acl inherit = yes
   follow symlinks = no
   wide links = no
EOF
sudo testparm
sudo systemctl enable --now smbd
sudo systemctl restart smbd
```

这里必须保留 `force user = %S`，让 Samba 上传文件继续归真实平台用户所有；不要改成 `force user = ddltm`，否则文件审计和用户隔离都会失真。`inherit acls = yes` 和 `map acl inherit = yes` 用来让 Samba 新建文件继承用户目录上的 ACL，避免用户上传 `*.log` 后 `ddltm` 不能追加或改写。

如果是新部署，平台创建或补齐 Linux 子账户时会自动给每个用户目录写入 ACL。若现场已经有历史用户目录，或此前用 `0644/0755` 配置上传过文件，需要按用户目录补一次 ACL。下面以用户 `test1` 为例，命令只作用于该用户目录，避免扩大到整个 `/home/ddltm`：

```bash
sudo chown -R test1:test1 /home/ddltm/data/user/test1
sudo chmod -R u+rwX,go-rwx /home/ddltm/data/user/test1
sudo setfacl -R -m u:test1:rwX,u:ddltm:rwX,m::rwx,o::--- /home/ddltm/data/user/test1
sudo find /home/ddltm/data/user/test1 -type d -exec setfacl -d -m u:test1:rwx,u:ddltm:rwx,g::---,m::rwx,o::--- {} +
```

如果要批量修复所有已有用户目录，先从数据库或 `/home/ddltm/data/user` 核对真实用户名，再逐个替换上面命令里的 `test1` 执行；不要直接对 `/home/ddltm/data/user` 做无差别 `chown -R ddltm`，否则会把用户目录属主改成主账户，破坏 `force user = %S` 的隔离模型。

验证某个用户通过 Samba 上传后的文件是否允许主账户维护：

```bash
getfacl /home/ddltm/data/user/test1/some.log
sudo -u ddltm test -w /home/ddltm/data/user/test1/some.log && echo writable
```

如果此前为了测试创建过 `ddltm` 或其他公共 Samba 账号，先禁用或删除对应 Samba 凭据，不要删除 Linux 主账户本身：

```bash
sudo smbpasswd -d ddltm || true
# 如果确认不再需要这个 Samba 凭据，可删除 Samba 账号记录：
# sudo smbpasswd -x ddltm || true
```

同时检查 `/etc/samba/smb.conf` 中不要保留 `[ddltm]`、`[data]`、`[shared]` 或其他 `path = /home/ddltm`、`path = /home/ddltm/data` 这类宽范围共享段落。NebulaGrid 只需要 `[homes]` 暴露个人目录；共享文件夹仍建议走 Web 文件管理的“共享文件夹”视图。

如果 `command -v smbpasswd`、`command -v pdbedit` 或 `command -v systemctl` 返回的不是文档中的路径，需要把 sudoers 中的路径改成现场真实路径。验证授权时不要使用真实用户密码，可创建临时探针账号。

保存后，用 API 运行用户验证 `sudo -n` 是否能无交互执行。下面以 `ddltm` 为例：

```bash
sudo -u ddltm sudo -n /usr/sbin/useradd --create-home --home-dir /home/ddltm/data/user/nebulagrid_sudo_probe --shell /bin/bash nebulagrid_sudo_probe
printf 'nebulagrid_sudo_probe:temporary-password\n' | sudo -u ddltm sudo -n /usr/sbin/chpasswd
sudo -u ddltm sudo -n /usr/bin/chown -R nebulagrid_sudo_probe:nebulagrid_sudo_probe /home/ddltm/data/user/nebulagrid_sudo_probe
sudo -u ddltm sudo -n /usr/bin/pdbedit -L
printf 'temporary-password\ntemporary-password\n' | sudo -u ddltm sudo -n /usr/bin/smbpasswd -s -a nebulagrid_sudo_probe
sudo -u ddltm sudo -n /usr/bin/smbpasswd -x nebulagrid_sudo_probe
sudo -u ddltm sudo -n /usr/bin/systemctl restart smbd
sudo -u ddltm sudo -n /usr/sbin/userdel --remove nebulagrid_sudo_probe
sudo systemctl restart nebulagrid-api
sudo systemctl reload nginx
```

上面的 `nebulagrid_sudo_probe` 只用于验证 sudoers 是否真的允许 API 需要的账户命令。因为 sudoers 会严格匹配命令绝对路径，所以如果这里出现 `interactive authentication is required`、`a password is required` 或 `not allowed to execute`，先修正 `/etc/sudoers.d/nebulagrid-ddltm`，不要只测试 `sudo useradd` 这种相对命令。如果 `command -v systemctl` 返回 `/bin/systemctl`，验证命令和 sudoers 中的 smbd 重启路径也要使用 `/bin/systemctl restart smbd`。

用户在 Windows 资源管理器中不能只访问主节点根路径，例如 `\\192.168.0.1`；`[homes]` 使用 `browseable = no`，服务器根路径不会列出每个用户目录。用户应访问自己的共享名：

```text
\\<master-ip>\<user_name>
```

例如用户 `test1` 应访问：

```text
\\192.168.0.1\test1
```

登录时用户名填写平台用户名 `test1`，密码填写该用户在账号管理页开启 Samba 时输入的当前密码。如果 Windows 缓存过旧账号或错误密码，先在 Windows CMD 中清理缓存后再连接：

```bat
net use \\192.168.0.1\test1 /delete /y
net use * /delete /y
cmdkey /delete:192.168.0.1
net use \\192.168.0.1\test1 /user:test1 *
```

如果 Windows 提示“无法访问”或“检查名称的拼写”，先从端口、服务、账号和共享名四层排查。Windows 侧确认 445 端口可达：

```powershell
Test-NetConnection 192.168.0.1 -Port 445
```

主节点侧确认 `smbd` 监听、Linux 子账户存在、Samba 账号存在、`[homes]` 生效，并直接从本机访问同名共享：

```bash
id test1
sudo pdbedit -L | grep '^test1:'
sudo ss -lntp | grep ':445'
sudo testparm -s | grep -A15 '^\[homes\]'
sudo smbclient -L //127.0.0.1 -U test1
sudo smbclient //127.0.0.1/test1 -U test1
```

排查结论按输出判断：`id test1` 失败说明 Linux 子账户未创建；`pdbedit -L` 没有 `test1` 说明 Samba 账号未创建；`ss` 没有 `:445` 说明 `smbd` 未监听；本机 `smbclient //127.0.0.1/test1 -U test1` 失败说明 Samba 配置、账号或目录权限仍有问题；本机 `smbclient` 成功而 Windows 不成功时，优先检查 Windows 凭据缓存、防火墙和 445 端口。

如果 Samba 状态在页面显示“失败”，优先检查 `journalctl -u nebulagrid-api`、`journalctl -u smbd`、`testparm`、`sudo smbclient //127.0.0.1/<user_name> -U <user_name>` 和 sudoers 路径。如果 Samba 上传成功但 `ddltm` 不能更新用户目录内的文件，优先用 `getfacl` 检查该文件和父目录是否继承了 `u:ddltm:rwX`，不要先改成宽范围 `chown -R ddltm`。Samba 协议只暴露主节点上的用户目录，不替代 NFS；NFS 仍然负责 master 与计算节点之间的训练数据和日志共享。

文件管理页面提供“共享文件夹”视图，所有登录用户都可以查看 `NEBULAGRID_SHARED_FOLDER_ROOT` 指向的共享 SSD 目录。默认部署路径为 `/home/ddltm/shared`；如果现场用 `~/ddltm/shared` 这类写法，请在写入 `backend.env` 前先展开成 API 运行用户实际看到的绝对路径，避免 systemd 环境中 `~` 指向不同 home。用户可把个人目录中的文件或文件夹复制到共享文件夹，也可在共享文件夹中把文件或文件夹复制回自己的目录；新建、删除、重命名、上传、打包和解压仍限定在个人文件视图内，避免共享根被误操作。

不要给 `ddltm` 配置宽泛的 `NOPASSWD: ALL`，这样会扩大系统风险。

### 21.1 数据库状态与共享路径说明

普通训练任务已经以 PostgreSQL 为单一状态源，不再依赖 API 进程内存。任务提交、批量提交、修改、挂起、删除、后继任务确认、中止、重新提交、日志路径、执行时间、结束时间、实际节点和实际 GPU 分配都会写入 `tasks`、`task_requirements`、`task_dependencies`、`task_allocations`、`task_events` 和 `task_runtime_guards`。

调度器按紧急任务、优先级、提交时间、前驱任务、节点可见性、GPU 数量、GPU 型号、指定节点、GPU 可调度开关和 GPU 复用策略选择资源。任务未指定节点时，候选节点按“用户自有节点 → 组内共享节点 → 组内他人公开共享的私有节点 → 其他公开节点”的顺序尝试；同一档内部按节点 ID 保持稳定顺序。只指定 GPU 型号时，系统只往所有可见候选节点中满足该型号的 GPU 上分配；只指定节点时，系统只在该节点内选择任意可调度 GPU；两者同时指定时，系统只在指定节点内选择指定型号的 GPU。每轮调度最多成功分配一个任务，并在下一轮开始时清理终态任务的未释放 allocation，避免同一张独占 GPU 在同一轮内被重复占用。调度器只持有单实例哨兵行锁，不再批量锁住等待任务行，因此用户修改等待任务和前端刷新任务列表不会被一次调度扫描长时间阻塞。执行器通过 SSH 调用 `/home/ddltm/envs/nebulagrid_remote/runner.py`，远端 runner 会写入 PID/PGID 元数据和状态文件。主节点和计算节点必须看到一致的 `/home/ddltm/data` 与 `/home/ddltm/envs` 路径，否则项目路径、环境路径或任务日志可能在计算节点上不可见。

管理员后台的“强制下线”会立即把目标节点标记为 `offline` 并关闭调度。对于该节点上仍持有未释放 allocation 的运行任务，后端会按远端进程组执行 TERM/KILL、把任务置为 `cancelled`，并释放该节点所有未释放 GPU 调度占用；审计日志会记录受影响任务和释放数量。
强制下线后的节点不会继续被节点监控 worker 自动 SSH 探测，也不会自动恢复为 `online`；维护完成后需要在节点管理里点击“重连”，下一轮监控成功才会重新上线。

任务日志默认位于 `/home/ddltm/data/logs/task_logs/<task_id>.log`；历史区默认加载最近 100 条可见任务，用户点击“查看所有历史任务”后才会加载全部可见历史任务，避免长时间运行后一次性拉取过多记录。

Runtime Guard 已经以 `task_runtime_guards` 为入口追踪运行任务。执行器启动远端 runner 后会记录 root PID 和进程组，守护进程通过 SSH 展开 PID 树，再读取 `nvidia-smi --query-compute-apps` 返回的 GPU UUID。GPU UUID 是越权判断依据，GPU index 只用于页面展示和 `CUDA_VISIBLE_DEVICES`。连续两轮发现任务使用未分配 GPU 后，系统会终止远端进程组、标记 `alloc_error`、释放 allocation，并把原因写入任务日志和 `task_events`。

环境包安装已经进入 `env_install_jobs` 队列。本机 conda/pip 离线安装、上传包安装和 compile 安装都会持久化安装命令、工作目录、日志路径、目标节点、主节点标记、GPU 可见模式和可见 GPU index。API 创建作业后会启动后台线程领取执行，生产部署仍建议同时运行 `nebulagrid-env-install-worker` 作为独立环境安装 worker；数据库作业状态会避免同一作业重复执行。安装作业日志继续复用 `/home/ddltm/data/logs/env_install_logs/env-<env_id>-<env_name>.log`，因此该路径同样必须在主节点和计算节点保持一致。

用户点击安装包页面的“在指定节点”时，后端会实时探测主节点和用户可见计算节点的 `gcc`、`g++`、`clang`、`nvcc` 与 GPU 清单。默认 GPU 模式不设置 `CUDA_VISIBLE_DEVICES`；CPU 模式设置为目标节点 GPU 总数加一；指定 GPU 模式设置为用户勾选的 GPU index 列表。同一环境安装包或删除包期间会加锁，页面状态显示为“安装中”，再次提交会提示稍后再试。

环境管理功能依赖 `/home/ddltm/envs/miniconda3/envs` 和 `/home/ddltm/data/logs/env_install_logs` 在主节点和计算节点路径一致。导入环境、创建环境副本、修复路径、检测环境和删除环境都会围绕这两个目录工作：

- 环境目录：`/home/ddltm/envs/miniconda3/envs/<env_name>`
- 环境日志：`/home/ddltm/data/logs/env_install_logs/env-<env_id>-<env_name>.log`，并同步写入数据库 `env_operation_logs`

从其他 Ubuntu 电脑导入已有 conda 环境时，推荐用户按下面流程操作：

1. 在另一台 Ubuntu x86_64/amd64 电脑上找到环境目录。环境必须来自 Linux 版 Anaconda3 或 Miniconda3 的 `envs` 一级目录，例如 `~/miniconda3/envs/<env_name>` 或 `~/anaconda3/envs/<env_name>`。
2. 在源机器上进入 `envs` 目录，把需要上传的环境目录整体打成 zip。不要只打包 `bin`、`lib` 或 `conda-meta` 子目录，也不要把多个环境混进同一个 zip：

   ```bash
   sudo apt install -y zip
   cd ~/miniconda3/envs
   zip -r <env_name>.zip <env_name>
   ```

   如果源机器用的是 Anaconda3，把 `cd` 那一行换成：

   ```bash
   cd ~/anaconda3/envs
   zip -r <env_name>.zip <env_name>
   ```

3. 用自己的 NebulaGrid 平台账号通过 `scp` 把 zip 传到 master 上自己的用户目录。这里的 `<user_name>` 是平台用户名，`master` 可以换成主节点 IP：

   ```bash
   scp <env_name>.zip <user_name>@master:/home/ddltm/data/user/<user_name>/
   ```

   传完后，用户在文件管理页的个人根目录 `/` 下应能看到 `/<env_name>.zip`。如果系统没有启用 Linux 子账户登录，也可以先让管理员把 zip 放到 `/home/ddltm/data/user/<user_name>/`，再由用户在页面导入。

4. 登录 Web 控制台，进入“环境管理”，选择“导入环境”，在自己的文件根目录中点选刚上传的 `/<env_name>.zip`，填写目标环境名并确认。后端会先把 zip 解压到临时目录，再复制到 `/home/ddltm/envs/miniconda3/envs/<target_env_name>`。
5. 导入过程中页面会依次显示 `导入中 -> 修复中 -> 测试中 -> 可用`。系统会自动修复环境中的旧路径前缀，例如原机器上的 `~/miniconda3/envs/<env_name>` 会被替换成当前集群上的 `/home/ddltm/envs/miniconda3/envs/<target_env_name>`；随后会激活环境并运行检测脚本。测试通过后，环境状态才会显示为“可用”。

环境包有明确的平台限制：只支持来自 Linux x86_64/amd64 系统的 Anaconda3/Miniconda3 环境。Linux ARM、MIPS 等其他架构的环境不能在本集群运行；Windows 和 macOS 的 conda 环境禁止上传使用。系统会在导入和检测阶段自动检查环境包的可用操作系统和可执行性；检测结果不是 Linux 环境，或环境无法在当前 Linux conda 体系中被识别和激活时，会被拒绝或标记为导入失败，不会变成可用环境。

用户导入 zip 环境包或创建副本后，系统会把环境复制到 `miniconda3/envs` 下，修复所有文本文件中的旧环境前缀，并整理权限。目录应为 `755`，普通文件为 `644`，`bin` 或 `Scripts` 下入口文件为 `755`。如果出现 `bad interpreter`、`错误的解释器` 或 `pip` 指向旧路径，优先检查环境日志和环境目录中的残留旧路径：

```bash
grep -R "/home/.*/envs/<env_name>" /home/ddltm/envs/miniconda3/envs/<env_name> 2>/dev/null | head
```

管理员可以查看全部环境日志；普通用户只能查看自己导入或复制的环境日志。日志文件采用 JSON Lines 格式，页面查看时会自动解析 JSON 字段和 `\n` 换行。

## 22. 环境管理验收补充

部署完成后，环境管理建议按以下顺序验收：

1. 登录管理员账号，进入环境管理页面，确认页面会自动刷新环境列表，且 `base` 环境不展示。
2. 点击“刷新环境列表”，确认 `conda env list --json` 中的非 base 环境被同步到数据库。
3. 对一个已有环境点击“检测”，确认返回 Python 版本、PyTorch、TensorFlow、CUDA/cuDNN 和包列表。
4. 在另一台 Ubuntu x86_64/amd64 机器的 `~/miniconda3/envs` 或 `~/anaconda3/envs` 中，把测试环境打成 zip，再用普通用户账号 `scp` 到 `/home/ddltm/data/user/<user_name>/`。用户登录后应能在文件管理个人根目录看到该 zip。
5. 用普通用户从自己的文件根目录选择打包好的环境 zip，点击导入并确认。页面应显示 `导入中 -> 修复中 -> 测试中 -> 可用`。
6. 导入后的环境目录应位于 `/home/ddltm/envs/miniconda3/envs/<env_name>`，目录权限为 `755`，普通文件为 `644`，`bin` 或 `Scripts` 下入口文件为 `755`。
7. 检查 `/home/ddltm/data/logs/env_install_logs/env-<env_id>-<env_name>.log`，确认导入、修复、检测记录已经落盘；同时可在 PostgreSQL `env_operation_logs` 表中看到对应结构化记录。
8. 尝试导入一个明显不兼容的环境包，例如 Windows、macOS 或非 amd64 Linux 环境包，确认系统会拒绝导入或把状态标记为错误，不会显示为“可用”。
9. 点击“创建副本”，输入新环境名，确认系统复制 `/home/ddltm/envs/miniconda3/envs/<old_env_name>` 到 `/home/ddltm/envs/miniconda3/envs/<new_env_name>`，并显示 `复制中 -> 修复中 -> 测试中 -> 可用`。
10. 在副本里运行 `pip --version` 或 `python -m pip --version`，确认 `pip` shebang 不再指向旧路径。路径修复应覆盖所有文本文件里的旧环境前缀，而不只是 conda metadata。
11. 普通用户只能删除自己导入或复制的环境；管理员可以删除所有环境。删除后数据库记录和 `miniconda3/envs/<env_name>` 目录应同时消失。
12. 普通用户只能查看自己的环境日志；管理员可以查看所有环境日志。

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
- 用户通过 Samba 上传到 `/home/ddltm/data/user/<user_name>` 的文件仍归该用户所有，同时 `sudo -u ddltm test -w <uploaded-file>` 返回可写，说明主账户维护 ACL 已生效。
- InfluxDB 中能查询到 `node_metrics` / `gpu_metrics` 监控点。
- systemd 中 API 和 worker 服务为 running。

## 24. 当前版本边界

当前代码适合部署后开始真实机器联调。以下能力还需要继续开发完善：

- 任务服务已切换到数据库 CRUD，并支持任务可见性、批量提交、修改、挂起、删除后继确认、中止、重新提交、日志读取和历史区默认 100 条加载。
- 调度器已执行真实 GPU 选择和 allocation 事务：紧急任务优先，校验前驱任务、节点可见性、GPU 数量、GPU 型号、指定节点、GPU 可调度开关和 GPU 复用策略，并写入任务事件与运行时守护记录。
- 执行器已通过 SSH 调用远端 runner 启动任务，记录 PID/PGID、读取状态文件、归档返回码并释放 allocation；仍需在真实节点验证 SSH key、NFS 路径、conda 激活和中止回收。
- runtime guard 已按远端 PID 树和 GPU UUID 检查实际 PID/GPU 使用并处理 `alloc_error`；仍需在真实多进程训练和节点异常场景下压测。
- env install worker 已从 `env_install_jobs` 领取本机和 compile 安装作业，执行受控安装命令并写回返回码、包状态和环境日志；上传文件真实落盘、sha256 校验、运行中安装取消和资源隔离仍需继续完善。
- Alembic 数据库迁移；当前初始化使用 ORM `create_all`，适合 MVP 部署和测试。
