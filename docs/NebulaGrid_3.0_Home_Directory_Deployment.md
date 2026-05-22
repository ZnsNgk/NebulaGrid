# NebulaGrid 主账户目录部署说明

本文说明如何把 NebulaGrid 代码部署到主账户目录，例如：

```text
/home/ddltm/master
```

这种方式适合实验室测试：管理员只负责系统级准备，后续代码更新、Python 包安装、数据库初始化和远端脚本同步尽量由 `ddltm` 自己完成。

## 1. 哪些步骤仍需要 sudo

以下操作必须由管理员执行，或临时授予 sudo：

- 安装系统包：`nginx`、`postgresql`、`influxdb2`、`redis-server`、`nfs-kernel-server`、`nfs-common`。
- 创建 Linux 用户 `ddltm`，并保证主节点和计算节点 UID/GID 一致。
- 创建和授权 `/home/ddltm/data`、`/home/ddltm/envs`。
- 配置 NFS：`/etc/exports.d/nebulagrid.exports`。
- 创建 PostgreSQL 用户和数据库。
- 初始化 InfluxDB org、bucket 和 token。
- 创建 `/etc/nebulagrid/backend.env`。
- 写入 `/etc/systemd/system/nebulagrid-*.service`。
- 配置 Nginx。
- 执行 `systemctl restart/reload`。

完成这些之后，日常更新可以基本不使用 sudo。

## 2. 代码放置位置

以 `ddltm` 登录主节点：

```bash
mkdir -p /home/ddltm/master
```

用 git：

```bash
git clone <your-repo-url> /home/ddltm/master
```

或从本地同步：

```bash
rsync -av --delete ./NebulaGrid/ ddltm@master:/home/ddltm/master/
```

确认：

```bash
ls -la /home/ddltm/master
ls -la /home/ddltm/master/backend
ls -la /home/ddltm/master/frontend
```

## 3. 后端依赖安装

以 `ddltm` 执行：

```bash
cd /home/ddltm/master/backend
/home/ddltm/envs/miniconda3/bin/python -m pip install -e .
```

需要测试依赖时：

```bash
/home/ddltm/envs/miniconda3/bin/python -m pip install -e ".[dev]"
```

## 4. 初始化数据库

管理员先创建 `/etc/nebulagrid/backend.env`，并确保 `ddltm` 可读：

```bash
sudo chown root:ddltm /etc/nebulagrid/backend.env
sudo chmod 640 /etc/nebulagrid/backend.env
```

然后 `ddltm` 执行：

```bash
cd /home/ddltm/master/backend
bash -lc 'set -a; source /etc/nebulagrid/backend.env; set +a; /home/ddltm/envs/miniconda3/bin/python scripts/init_db.py'
```

如果是从旧版本升级，并且 PostgreSQL 中曾经创建过节点监控表，再执行一次清理脚本。节点/GPU 历史监控指标现在写入 InfluxDB：

```bash
cd /home/ddltm/master/backend
bash -lc 'set -a; source /etc/nebulagrid/backend.env; set +a; /home/ddltm/envs/miniconda3/bin/python scripts/drop_postgres_metrics_tables.py'
```

`/etc/nebulagrid/backend.env` 至少需要包含 InfluxDB 连接信息：

```bash
NEBULAGRID_INFLUXDB_URL=http://127.0.0.1:8086
NEBULAGRID_INFLUXDB_ORG=nebulagrid
NEBULAGRID_INFLUXDB_BUCKET=nebulagrid_metrics
NEBULAGRID_INFLUXDB_TOKEN=change-this-influx-token
NEBULAGRID_INFLUXDB_LATEST_RANGE=30m
```

## 5. 同步远端脚本

`/home/ddltm/data` 和 `/home/ddltm/envs` 归属应为 `ddltm:ddltm`。远端脚本位于 `backend/app/remote/*.py`，优先使用同步工具，确保新增的 `monitor.py`、`runner.py`、`env_probe.py`、`env_installer.py` 等脚本都会被下发：

```bash
cd /home/ddltm/master/backend
env $(cat /etc/nebulagrid/backend.env | xargs) \
  /home/ddltm/envs/miniconda3/bin/python scripts/sync_remote_scripts.py
```

如果计算节点没有通过 NFS 看到同一个 `/home/ddltm/envs/nebulagrid_remote`，可在节点登记完成后主动分发到数据库中的全部计算节点：

```bash
cd /home/ddltm/master/backend
env $(cat /etc/nebulagrid/backend.env | xargs) \
  /home/ddltm/envs/miniconda3/bin/python scripts/sync_remote_scripts.py --all-db-nodes
```

验证计算节点可读取：

```bash
ssh ddltm@node-a '/home/ddltm/envs/miniconda3/bin/python /home/ddltm/envs/nebulagrid_remote/monitor.py'
```

## 6. systemd 服务要改的路径

所有 NebulaGrid service 文件都应使用主账户代码目录：

例如 API 服务：

```ini
[Service]
User=ddltm
Group=ddltm
WorkingDirectory=/home/ddltm/master/backend
EnvironmentFile=/etc/nebulagrid/backend.env
ExecStart=/home/ddltm/envs/miniconda3/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always
RestartSec=3
```

修改 service 后需要管理员执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart nebulagrid-api
```

## 7. Nginx 前端路径

如果直接让 Nginx 读取 `/home/ddltm/master/frontend`：

```nginx
root /home/ddltm/master/frontend;
```

需要保证 Nginx 用户能读：

```bash
chmod 755 /home/ddltm
chmod -R a+rX /home/ddltm/master/frontend
```

更稳妥的方式是保留代码在 `/home/ddltm/master`，但把前端同步到 `/var/www/nebulagrid`：

```bash
sudo mkdir -p /var/www/nebulagrid
sudo rsync -av /home/ddltm/master/frontend/ /var/www/nebulagrid/
sudo chown -R www-data:www-data /var/www/nebulagrid
```

Nginx 使用：

```nginx
root /var/www/nebulagrid;
```

## 8. 日常更新流程

以 `ddltm` 执行：

```bash
cd /home/ddltm/master
git pull
cd backend
/home/ddltm/envs/miniconda3/bin/python -m pip install -e .
env $(cat /etc/nebulagrid/backend.env | xargs) \
  /home/ddltm/envs/miniconda3/bin/python scripts/sync_remote_scripts.py
```

然后管理员重启服务：

```bash
sudo systemctl restart nebulagrid-api nebulagrid-scheduler nebulagrid-node-monitor nebulagrid-task-executor nebulagrid-runtime-guard nebulagrid-env-install-worker
sudo systemctl reload nginx
```

如果启用了 SSH 子账户同步或希望 `ddltm` 可以执行受控重启，需要配置很窄的 sudoers。API 服务运行在 systemd 中，没有交互终端，所以不能依赖 sudo 密码缓存，也不能让后端保存 sudo 密码；所有需要 API 调用的命令必须使用 `NOPASSWD` 和绝对路径授权。

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
ddltm ALL=(root) NOPASSWD: /usr/sbin/useradd, /usr/sbin/usermod, /usr/sbin/userdel, /usr/sbin/chpasswd, /usr/bin/mkdir, /usr/bin/chown, /usr/bin/chmod, /usr/bin/find, /usr/bin/setfacl, /bin/systemctl restart nebulagrid-api, /bin/systemctl restart nebulagrid-scheduler, /bin/systemctl restart nebulagrid-node-monitor, /bin/systemctl restart nebulagrid-task-executor, /bin/systemctl restart nebulagrid-runtime-guard, /bin/systemctl restart nebulagrid-env-install-worker, /bin/systemctl reload nginx
```

保存后用 API 运行用户和绝对路径验证 sudoers 是否生效：

```bash
sudo -u ddltm sudo -n /usr/sbin/useradd --create-home --home-dir /home/ddltm/data/user/nebulagrid_sudo_probe --shell /bin/bash nebulagrid_sudo_probe
printf 'nebulagrid_sudo_probe:temporary-password\n' | sudo -u ddltm sudo -n /usr/sbin/chpasswd
sudo -u ddltm sudo -n /usr/bin/chown -R nebulagrid_sudo_probe:nebulagrid_sudo_probe /home/ddltm/data/user/nebulagrid_sudo_probe
sudo -u ddltm sudo -n /usr/sbin/userdel --remove nebulagrid_sudo_probe
```

如果出现 `interactive authentication is required`、`a password is required` 或 `not allowed to execute`，说明 sudoers 没有匹配到 API 实际执行的绝对路径，或者授权用户不是 API 实际运行用户，需要先修正授权。

不要给 `NOPASSWD: ALL`。

## 9. 文件任务状态

文件管理中的目录打包和压缩包解压任务会写入 PostgreSQL 的 `file_jobs` 表，而不是保存在 API 进程内存中。页面刷新、重新登录、API 重启或多 worker 部署后，前端仍可通过 `/api/files/jobs/latest` 读取当前用户最近一次任务状态。API 启动时会把上次进程遗留的 `pending/running` 文件任务标记为失败，避免重启后长期占用并发名额。

当前目录打包生成 zip；解压支持 `.zip`、`.tar`、`.tar.gz`、`.tgz`、`.tar.bz2`、`.tbz2`、`.tar.xz` 和 `.txz`。同一用户同时只能运行一个文件打包/解压任务，系统也会限制全局并发，避免共享盘 IO 被大量压缩任务打满。

## 10. 任务调度状态

普通训练任务已经以 PostgreSQL 为单一状态源，不再依赖 API 进程内存。任务提交、批量提交、修改、挂起、删除、后继任务确认、中止、重新提交、日志路径、执行时间、结束时间、实际节点和实际 GPU 分配都会写入 `tasks`、`task_requirements`、`task_dependencies`、`task_allocations`、`task_events` 和 `task_runtime_guards`。

调度器按紧急任务、优先级、提交时间、前驱任务、节点可见性、GPU 数量、GPU 型号和 GPU 复用策略选择资源。执行器通过 SSH 调用 `/home/ddltm/envs/nebulagrid_remote/runner.py`，远端 runner 会写入 PID/PGID 元数据和状态文件。主节点和计算节点必须看到一致的 `/home/ddltm/data` 与 `/home/ddltm/envs` 路径，否则项目路径、环境路径或任务日志可能在计算节点上不可见。

任务日志默认位于 `/home/ddltm/data/logs/task_logs/<task_id>.log`；历史区默认加载最近 100 条可见任务，用户点击“查看所有历史任务”后才会加载全部可见历史任务，避免长时间运行后一次性拉取过多记录。

## 11. 运行时守护与环境安装作业

Runtime Guard 已经以 `task_runtime_guards` 为入口追踪运行任务。执行器启动远端 runner 后会记录 root PID 和进程组，守护进程通过 SSH 展开 PID 树，再读取 `nvidia-smi --query-compute-apps` 返回的 GPU UUID。GPU UUID 是越权判断依据，GPU index 只用于页面展示和 `CUDA_VISIBLE_DEVICES`。连续两轮发现任务使用未分配 GPU 后，系统会终止远端进程组、标记 `alloc_error`、释放 allocation，并把原因写入任务日志和 `task_events`。

环境包安装已经进入 `env_install_jobs` 队列。本机 conda/pip 离线安装、上传包安装和 compile 安装都会持久化安装命令、工作目录、日志路径、目标节点、主节点标记、GPU 可见模式和可见 GPU index。API 创建作业后会启动后台线程领取执行，生产部署仍建议同时运行 `nebulagrid-env-install-worker` 作为独立环境安装 worker；数据库作业状态会避免同一作业重复执行。安装作业日志继续复用 `/home/ddltm/data/logs/env_install_logs/env-<env_id>-<env_name>.log`，因此该路径同样必须在主节点和计算节点保持一致。

用户点击安装包页面的“在指定节点”时，后端会实时探测主节点和用户可见计算节点的 `gcc`、`g++`、`clang`、`nvcc` 与 GPU 清单。默认 GPU 模式不设置 `CUDA_VISIBLE_DEVICES`；CPU 模式设置为目标节点 GPU 总数加一；指定 GPU 模式设置为用户勾选的 GPU index 列表。同一环境安装包或删除包期间会加锁，页面状态显示为“安装中”，再次提交会提示稍后再试。

## 12. 环境目录与日志

环境管理功能依赖 `/home/ddltm/envs/miniconda3/envs` 和 `/home/ddltm/data/logs/env_install_logs` 在主节点和计算节点路径一致。导入环境、创建环境副本、修复路径、检测环境和删除环境都会围绕这两个目录工作：

- 环境目录：`/home/ddltm/envs/miniconda3/envs/<env_name>`
- 环境日志：`/home/ddltm/data/logs/env_install_logs/env-<env_id>-<env_name>.log`，并同步写入数据库 `env_operation_logs`

用户导入 zip 环境包或创建副本后，系统会把环境复制到 `miniconda3/envs` 下，修复所有文本文件中的旧环境前缀，并整理权限。目录应为 `755`，普通文件为 `644`，`bin` 或 `Scripts` 下入口文件为 `755`。

如果出现 `bad interpreter`、`错误的解释器` 或 `pip` 指向旧路径，优先检查环境日志和环境目录中的残留旧路径：

```bash
grep -R "/home/.*/envs/<env_name>" /home/ddltm/envs/miniconda3/envs/<env_name> 2>/dev/null | head
```

管理员可以查看全部环境日志；普通用户只能查看自己导入或复制的环境日志。日志文件采用 JSON Lines 格式，页面查看时会自动解析 JSON 字段和 `\n` 换行。
