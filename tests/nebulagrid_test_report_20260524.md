# NebulaGrid 3.0 联调测试报告

## 1. 测试概览

- 测试日期：2026-05-24
- 测试对象：NebulaGrid 3.0 已部署环境
- 主控节点：`10.16.20.253`
- 前端入口：`http://10.16.20.253/`
- 测试依据：
  - `docs/` 目录内系统设计、部署、运维与使用文档
  - `tests/test_plan.md`
  - 用户补充的实际部署文档 `C:\Users\xpc13\OneDrive\NebulaGrid_3.0_Full_Deployment_Tutorial.md`
  - 用户提供的测试账号与节点信息文件
- 测试方式：后端 API、服务端命令、远程节点连通性、任务调度、Runtime Guard、文件/环境/用户权限、前端浏览器人工路径验证
- 敏感信息处理：本报告不记录密码、登录 Token、Cookie 或其他认证密钥。

## 2. 总体结论

本轮测试覆盖了 `tests/test_plan.md` 中的核心生产联调路径。后端核心能力、用户/权限、节点注册与监控、任务调度、GPU 分配、Runtime Guard、日志流、文件管理、已有 Conda 环境检测、服务重启恢复以及主要前端页面均通过验证。

总体结论：**可进入试运行**。

当前未发现 P0/P1 阻断问题。发现 2 个非阻断告警：

| 编号 | 级别 | 结论 |
| --- | --- | --- |
| NG-20260524-001 | WARN | 实际 systemd 工作目录为 `/home/ddltm/NebulaGrid/backend/`，与用户补充部署文档中的 `/home/ddltm/master/backend` 不一致。运行态正常，但文档或服务路径建议统一。 |
| NG-20260524-002 | INFO/WARN | 管理员文件页默认展示主账号家目录中的点目录，例如 `.ssh`、`.config`。管理员本身有高权限，功能不阻断，但建议后续优化默认展示范围或隐藏高噪声点目录。 |
| NG-20260524-003 | WARN | 前端节点卡片中 GPU 使用率和显存可以展示，但节点级 CPU、可用内存、上传、下载显示为 `-`。代码链路期望这些字段来自 InfluxDB 的 `node_metrics`，需要继续确认生产环境是否实际写入 `node_metrics`。 |

## 3. 实际部署差异与基线确认

用户补充文档与现场运行环境存在一处关键路径差异，测试时按现场真实运行路径执行：

| 项目 | 文档/配置值 | 实际验证结果 | 结论 |
| --- | --- | --- | --- |
| 主控 IP | `10.16.20.253` | `10.16.20.253` | PASS |
| 共享目录根 | `/home/ddltm/data_ssd` | 存在且可访问 | PASS |
| 旧共享目录 | `/home/ddltm/shared` | 不存在 | PASS，实际部署不使用该路径 |
| 后端环境文件 | `/etc/nebulagrid/backend.env` | 存在，服务使用该配置 | PASS |
| 远程代码目录 | `/home/ddltm/envs/nebulagrid_remote` | 存在，节点脚本可访问 | PASS |
| Conda 环境根 | `/home/ddltm/envs/miniconda3/envs` | 存在 | PASS |
| 文档后端目录 | `/home/ddltm/master/backend` | 不存在 | WARN |
| 实际 systemd 工作目录 | `/home/ddltm/NebulaGrid/backend/` | 存在且服务正常运行 | PASS |

已确认 `/home/ddltm/data_ssd/old` 等旧文件目录仅被读取观察，未执行写入、移动或删除。

## 4. 测试节点清单

测试前 API 节点列表为空。根据用户提供的节点清单，已通过管理接口注册 3 台节点并完成监控同步。

| 节点名 | IP | GPU | API 状态 | GPU UUID 同步 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `node-rtx4080-01` | `10.16.87.102` | 2 x RTX 4080 | online | 已同步 2 张 | PASS |
| `node-rtx3090-01` | `10.16.17.69` | 2 x RTX 3090 | online | 已同步 2 张 | PASS |
| `node-p40-01` | `10.16.88.63` | GTX 745 + 2 x Tesla P40 | online | 已同步 3 张 | PASS |

前端总览页同步显示：3 台在线节点、7 张 GPU、监控数据正常刷新。

## 5. 后端与部署验证

| 用例 | 验证内容 | 结果 |
| --- | --- | --- |
| DEP-01 | `GET /api/health` 返回 `ok=true`、`status=ok`、带 `request_id` | PASS |
| DEP-02 | 未登录访问 `/api/auth/me` 返回 401 | PASS |
| DEP-03 | 管理员登录、`/api/auth/me`、仪表盘、用户、任务、环境、文件、审计 API 可用 | PASS |
| DEP-04 | PostgreSQL 服务与核心表结构存在 | PASS |
| DEP-05 | Redis 返回 `PONG` | PASS |
| DEP-06 | InfluxDB 服务 active，`gpu_metrics` 有近实时数据 | PASS |
| DEP-07 | Nginx 服务 active，前端根路径 HTTP 200 | PASS |
| DEP-08 | NebulaGrid 相关 systemd 服务 active/running | PASS |
| DEP-09 | 远程脚本 `runner.py`、`monitor.py`、`env_installer.py` 存在 | PASS |
| DEP-10 | 实际路径下运行部署自检脚本，节点 SSH 与 `nvidia-smi` 可用 | PASS |
| OPS-03 | 重启 NebulaGrid 服务后 API 与任务列表恢复正常 | PASS |

已验证的服务包括：

- `nebulagrid-api`
- `nebulagrid-scheduler`
- `nebulagrid-node-monitor`
- `nebulagrid-task-executor`
- `nebulagrid-runtime-guard`
- `nebulagrid-env-install-worker`

说明：服务重启验证使用了交互式 sudo 密码。若后续需要自动化无人值守重启，建议单独确认 `systemctl` 相关 sudoers 规则是否已完全覆盖。

## 6. 用户、权限与 Samba

| 用例 | 验证内容 | 结果 |
| --- | --- | --- |
| USER-01 | 管理员创建导师、学生、展示用户、禁用用户 | PASS |
| USER-02 | 导师 A 仅能看到自己名下学生，不能看到导师 B 学生 | PASS |
| USER-03 | 学生用户登录后权限受限 | PASS |
| USER-04 | 展示用户仅能访问大屏页和大屏 API，访问任务/文件/用户 API 返回 403 | PASS |
| USER-05 | 禁用用户后，旧会话失效，重新登录被拒绝 | PASS |
| USER-06 | 学生 Linux 账号可通过 SSH 登录，默认目录为自己的数据目录 | PASS |
| USER-07 | 学生自助启用 Samba 后，`pdbedit -L` 可看到对应账号 | PASS |
| USER-08 | 学生自助关闭 Samba 后，API 返回 disabled 状态 | PASS |

测试中创建并保留的测试用户：

- `mentor-a`
- `mentor-b`
- `student-a1`
- `student-a2`
- `student-b1`
- `student-free`
- `screen-viewer`
- `disabled-user`

这些账号用于权限回归与审计追溯，未在本轮测试结束时删除。

## 7. 文件管理

| 用例 | 验证内容 | 结果 |
| --- | --- | --- |
| FILE-01 | 学生在个人目录创建测试文件 | PASS |
| FILE-02 | 文件预览内容正确 | PASS |
| FILE-03 | 路径穿越 `../../..` 被 422 拒绝 | PASS |
| FILE-04 | 删除根目录被拒绝 | PASS |
| FILE-05 | 共享目录列表可访问，未触碰旧文件 | PASS |
| FILE-06 | 测试文件清理成功 | PASS |

本轮临时文件：`/ngtest_20260524_200131.txt`。该文件已通过 API 删除，未遗留在学生目录。

## 8. 环境管理

| 用例 | 验证内容 | 结果 |
| --- | --- | --- |
| ENV-01 | 环境列表接口可用 | PASS |
| ENV-02 | 发现已有环境 `Zns29` | PASS |
| ENV-03 | 环境路径 `/home/ddltm/envs/miniconda3/envs/Zns29` 可检测 | PASS |
| ENV-04 | `/api/envs/1/test` 返回成功 | PASS |

为避免破坏已有运行环境，本轮未执行环境删除、克隆、导入、批量安装/卸载包等高影响操作。

## 9. 任务调度、GPU 与 Runtime Guard

任务均由 `student-a1` 提交，主要使用 `node-rtx4080-01` 和环境 `Zns29` 验证。

| 任务 ID | 类型 | 验证内容 | 结果 |
| --- | --- | --- | --- |
| `260524200347504` | CPU | CPU-only Python 任务提交、运行、成功结束 | PASS |
| `260524200357783` | 单 GPU | 分配 1 张 GPU，PyTorch CUDA 可用，中等负载矩阵运算成功 | PASS |
| `260524200430501` | 双 GPU | 分配 2 张 GPU，两个设备均可被 PyTorch 使用 | PASS |
| `260524200459054` | 失败任务 | 命令返回码 3，任务进入 failed，GPU 释放 | PASS |
| `260524200649738` | 实时日志 | SSE 接收运行中日志事件，最终成功 | PASS |
| `260524200507305` | 日志下载 | 日志下载 HTTP 200，内容完整 | PASS |
| `260524200526822` | 取消任务 | 运行中取消成功，日志记录被用户终止 | PASS |
| `260524200536122` | 重新提交 | 从失败任务重新提交生成新任务，保留原失败行为 | PASS |
| `260524200542292` | Runtime Guard | 任务越权使用未分配 GPU，被 Guard 检测并置为 `alloc_error` | PASS |

Runtime Guard 重点结果：

- 任务实际分配 GPU：`[0]`
- 测试命令故意覆盖 `CUDA_VISIBLE_DEVICES=1`
- Guard 检测到未分配 GPU 被使用
- 任务最终状态：`alloc_error`
- 运行日志包含 Runtime Guard 终止信息

结论：GPU 可见性约束、日志采集、调度状态流转、失败处理、取消处理和 Guard 兜底均可用。

## 10. 前端验证

前端测试使用 Codex 内置浏览器打开 `http://10.16.20.253/` 完成。浏览器控制台未发现应用侧 error/warn 日志。

| 页面 | 验证内容 | 结果 | 截图 |
| --- | --- | --- | --- |
| 登录页 | 页面正常渲染，管理员可登录 | PASS | [frontend_login.png](screenshots/frontend_login.png) |
| 管理员总览 | 3 节点、7 GPU、GPU 监控卡片显示正常；节点级 CPU/内存/网络显示为 `-` | WARN | [frontend_admin_dashboard.png](screenshots/frontend_admin_dashboard.png) |
| 任务页 | 添加任务、批量添加、等待区、执行区、历史区入口可见 | PASS | [frontend_tasks.png](screenshots/frontend_tasks.png) |
| 文件页 | 根目录、上级、刷新、共享文件夹、上传/下载/删除等控件可见 | PASS | [frontend_files.png](screenshots/frontend_files.png) |
| 环境页 | 环境列表、检测、日志、复制、包管理等控件可见 | PASS | [frontend_envs.png](screenshots/frontend_envs.png) |
| 管理页 | 总览、节点管理、用户管理、登录管理、系统设置、审计日志入口可见 | PASS | [frontend_admin.png](screenshots/frontend_admin.png) |
| 展示用户 | 登录后自动进入大屏页，普通导航不可见 | PASS | [frontend_viewer_presenter.png](screenshots/frontend_viewer_presenter.png) |

前端页面已验证的关键行为：

- 管理员登录后进入 `#/dashboard`
- 左侧导航和主要模块路由可访问
- 展示用户登录后进入 `#/presenter`
- 展示用户页面仅保留大屏相关刷新和退出能力
- 浏览器控制台无应用侧异常

## 11. 未覆盖或刻意跳过项

以下项目未在本轮执行，原因是耗时较长、破坏性较强或不适合在已有服务端旧文件存在的环境中直接操作：

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| 4 小时以上长稳任务 | 未覆盖 | 本轮已执行中等 GPU 负载，未做长时间 soak |
| 强制断开节点/NFS 故障注入 | 未覆盖 | 可能影响当前测试节点和共享存储状态 |
| 数据库备份与恢复演练 | 未覆盖 | 恢复动作可能改变线上数据 |
| 环境删除/克隆/导入破坏性流程 | 未覆盖 | 避免破坏已有 Conda 环境 |
| Windows SMB 客户端挂载 | 部分覆盖 | 已验证 Samba API、`pdbedit` 和 `smbd`，未从 Windows 资源管理器实际挂载 |

## 12. 风险与建议

1. 统一部署文档与运行路径  
   实际 systemd 使用 `/home/ddltm/NebulaGrid/backend/`，而用户补充部署文档写的是 `/home/ddltm/master/backend`。建议更新部署文档，或统一服务工作目录，避免后续运维按文档执行自检时误判失败。

2. 优化管理员文件页默认目录展示  
   管理员文件页当前可看到主账号家目录下多个点目录。该行为对管理员不构成越权，但会增加误操作风险。建议后续默认进入平台数据目录，或在 UI 中折叠/隐藏点目录。

3. 明确自动化运维 sudoers 范围  
   服务重启已验证可用，但依赖交互式 sudo 密码。若后续要接入无人值守运维任务，建议确认 systemd 服务管理命令的免密 sudo 规则。

4. 补查 `node_metrics` 写入链路  
   GPU 指标来自 `gpu_metrics` 且当前可展示；CPU、可用内存、上传、下载应来自 `node_metrics`，当前前端显示为空。建议在主节点查询 InfluxDB 是否存在 `node_metrics`，并确认 `/home/ddltm/envs/nebulagrid_remote/monitor.py` 与当前仓库 `backend/app/remote/monitor.py` 是否一致。

5. 保留测试账号用于回归  
   本轮创建的测试导师、学生、展示用户和禁用用户可继续用于回归。如果要清理，应按本报告中的账号清单逐个处理，避免误删真实用户。

## 13. 最终判定

NebulaGrid 3.0 当前部署在核心功能上满足试运行要求：

- API 健康、鉴权、权限模型正常
- 3 台测试节点在线，7 张 GPU 监控同步正常
- CPU、单 GPU、双 GPU、失败、取消、重新提交、日志下载和实时日志任务流通过
- Runtime Guard 可检测并终止未分配 GPU 使用
- 文件路径边界保护有效
- Samba 自助开关可用
- 主要前端页面可正常访问，无应用侧控制台异常

建议在进入更长周期生产试运行前，优先处理部署路径文档不一致问题，并在低峰期补充长稳任务、故障注入、数据库恢复和真实 Windows SMB 挂载验证。
