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

`/home/ddltm/data` 和 `/home/ddltm/envs` 归属应为 `ddltm:ddltm`。以 `ddltm` 执行：

```bash
rsync -av /home/ddltm/master/backend/app/remote/ /home/ddltm/envs/nebulagrid_remote/
chmod +x /home/ddltm/envs/nebulagrid_remote/*.py
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
rsync -av /home/ddltm/master/backend/app/remote/ /home/ddltm/envs/nebulagrid_remote/
```

然后管理员重启服务：

```bash
sudo systemctl restart nebulagrid-api nebulagrid-scheduler nebulagrid-node-monitor nebulagrid-task-executor nebulagrid-runtime-guard nebulagrid-env-install-worker
sudo systemctl reload nginx
```

如果管理员愿意给 `ddltm` 很窄的重启权限，可以配置 sudoers：

```bash
sudo visudo -f /etc/sudoers.d/nebulagrid-ddltm
```

写入：

```text
ddltm ALL=(root) NOPASSWD: /bin/systemctl restart nebulagrid-api, /bin/systemctl restart nebulagrid-scheduler, /bin/systemctl restart nebulagrid-node-monitor, /bin/systemctl restart nebulagrid-task-executor, /bin/systemctl restart nebulagrid-runtime-guard, /bin/systemctl restart nebulagrid-env-install-worker, /bin/systemctl reload nginx
```

不要给 `NOPASSWD: ALL`。



