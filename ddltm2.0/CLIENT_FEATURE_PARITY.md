# Web 端与 PyQt 客户端功能对照

本版本的 Web 端是按 `client1.2/control/main_ui.py`、`task_ctrl.py`、`multi_task_ctrl.py`、`thread.py` 和 `master1.2/process.py` 的协议重新对照实现的。Web 端嵌入在 master 进程中运行，共用 master 内存里的 `wait_task / exec_task / hist_task / stop_task` 和节点监控状态。

## 1. 登录/注册

客户端原版没有登录系统；Web 端新增：

- `/login` 登录；
- `/register` 注册；
- SQLite 保存用户；
- `conf.json -> web_info.allow_register` 可关闭注册。

## 2. 节点监控

对应客户端：

- `mode == "mon"`；
- 显示节点名、CPU 使用、CPU 占用、可用 RAM、网络上下行、GPU 使用、GPU 调度占用、可用显存。

Web 实现：

- `monitor.py` 增加 `latest_info` 快照；
- Web 端从 `master.monitor.latest_info` 与 `master.slaver_state` 合并展示；
- 不影响原 socket 客户端接收监控信息。

## 3. 任务管理

对应客户端按钮/模式：

- 添加任务：`add_task`；
- 批量添加任务：`multi_add_task`；
- 修改等待区/历史区任务：`change_task`；
- 删除等待区/历史区任务：`del_task`；
- 删除当前任务及后继任务：已按客户端复选框语义实现；
- 中止执行区任务：`stop_task`；
- 重新提交：`resub_task`；
- 查看任务日志：`get_train_log_path` 对应功能，Web 端直接读取 `root/train_log_path/task_id.log`；
- GPU 型号筛选：`get_gpu_type` 对应功能。

任务字段保持一致：

```json
{
  "task_id": "",
  "envs": "",
  "path": "/data/",
  "exec": "python train.py",
  "need_gpus": "1",
  "slaver": "<默认>",
  "prev": "(无)",
  "is_urgent": 0,
  "is_reuse_gpu": 0,
  "state": "",
  "gpu_type": []
}
```

## 4. 批量任务

对应客户端 `multi_task_ctrl.py`：

- 每行一条命令；
- 空行忽略；
- `#` 开头的行忽略；
- 行内 `#` 后面视作注释；
- 共享环境、路径、节点、GPU 数量、GPU 类型、紧急、复用 GPU 选项；
- 前驱任务默认为 `(无)`。

## 5. 文件管理

对应客户端功能：

- `get_root`：显示可见目录；
- `get_folder`：目录浏览；
- `get_file`：文本文件预览；
- `save_file`：保存文本文件；
- `file_operate`：新建文件夹、新建文件、重命名；
- `rm_file`：删除文件/目录；
- `scp_thread`：上传、下载、复制到、移动到。

Web 端因为运行在 master 内部，不再经由客户端本地 SCP，而是直接操作 master 文件系统；功能等价，路径仍受 `visible_folders` 限制。

## 6. 环境管理

对应客户端按钮/模式：

- 环境列表：`env_list`；
- 测试环境：`test_env`；
- Python 版本：`py_v`；
- PyTorch CUDA：`cuda_pt`；
- TensorFlow CUDA：`cuda_tf`；
- 包列表：`pkgs`。

Web 端这些操作优先复用 `master1.2/process.py` 的同名模式，避免逻辑分叉。

## 7. 命令行

对应客户端 `terminal_thread`。

Web 端提供命令执行入口，默认关闭：

```json
"terminal_enabled": false
```

开启后相当于在 master 上执行命令。考虑到 Web 命令行风险高，建议只在可信内网或 VPN 后使用。

## 8. 日志

对应客户端：

- 服务端日志：`get_server_log`；
- 任务日志：`get_train_log_path` + `tail/cat`。

Web 端直接读取日志文件尾部，避免长期 SSH tail 线程堆积。

## 9. 与原客户端共存

- 原 PyQt 客户端 socket 协议不变；
- Web 端作为 master 内的额外线程启动；
- 原 `Communicate`、`Task_Control`、`Monitor` 仍按原流程运行；
- Web 端不会占用 data/monitor/train_state socket 端口。
