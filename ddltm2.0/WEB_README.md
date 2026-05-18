# Master Web 控制台使用说明

本版本在 `master1.2` 中新增了一个可嵌入 master 进程的网页端，入口文件为 `web_app.py`。

## 1. 安装依赖

```bash
cd master1.2
pip install -r requirements_web.txt
```

如果不安装 Flask，master 原有 socket / PyQt 客户端功能仍可运行，但 Web 控制台不会启动。

## 2. 启动

```bash
cd master1.2
python main.py
```

默认访问地址：

```text
http://<master-ip>:8080
```

首次使用请先注册账号。账号密码保存在 `web_users.db`，密码使用 PBKDF2-HMAC-SHA256 加盐哈希存储。

## 3. 配置项

`conf.json` 中新增：

```json
"web_info": {
  "enabled": true,
  "host": "0.0.0.0",
  "port": 8080,
  "user_db": "./web_users.db",
  "allow_register": true,
  "terminal_enabled": false,
  "max_upload_mb": 512,
  "shell_timeout": 120,
  "secret_key": ""
}
```

说明：

- `enabled`: 是否启动 Web 控制台。
- `host` / `port`: 监听地址和端口。
- `allow_register`: 是否允许注册新账号。建议首次注册管理员账号后改为 `false`。
- `terminal_enabled`: 是否开启网页命令行。该功能会在 master 机器本地执行 shell 命令，建议默认保持 `false`。
- `max_upload_mb`: 网页端最大上传文件大小。
- `secret_key`: Flask session 密钥。生产环境建议填一个随机长字符串；留空会在每次启动时自动生成，重启后登录态会失效。

## 4. 已实现功能

- 用户注册 / 登录 / 退出。
- 节点监控总览：在线节点、CPU、内存、网络、GPU 状态、调度占用状态。
- 任务管理：添加、批量添加、修改、删除、重新提交、中止运行任务、查看任务日志。
- 文件管理：浏览 `visible_folders` 指定目录、预览/编辑文本文件、上传、下载、新建、重命名、删除、复制、移动。
- 环境管理：环境列表、环境测试、Python 版本、PyTorch CUDA、TensorFlow CUDA、conda 包列表。
- 日志查看：服务端日志与任务日志。
- 命令行：默认关闭，可通过 `terminal_enabled` 手动开启。

## 5. 与原客户端的关系

网页端直接嵌入 master 进程，并复用 `wait_queue`、`wait_task`、`exec_task`、`hist_task`、`stop_task`、`slaver_state` 等状态；原 PyQt 客户端仍可继续使用，两者可以同时连接 master。
