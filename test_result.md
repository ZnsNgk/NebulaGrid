# NebulaGrid 管理员节点管理功能测试结果

测试时间：2026-05-22 01:12（Asia/Shanghai）  
测试环境：`http://10.16.59.101/`  
测试账号：`admin`、`xpc`、`wgy`  
临时测试节点：`codex-test-node-0522011240`

## 测试结论

本次针对管理员节点管理的增删改查、节点操作按钮、所有人选择相关后端接口、共享可见性和前端静态部署状态做了验证。核心链路测试通过：管理员可以新增、查询、修改、重连、强制下线和删除节点；节点所有人可以保存多个用户 ID，并且 GPU 型号顺序可以按提交顺序保存。

唯一需要关注的问题是：当 `gpu_count` 与 `gpu_models` 数量不一致时，后端确实拒绝了非法请求，但服务器返回的是 HTTP 500，而不是更适合前端展示的 400 或 422。功能上没有保存脏数据，但错误码需要后续优化。

## 详细结果

| 测试项 | 结果 | 说明 |
| --- | --- | --- |
| 环境健康检查 | 通过 | `/api/health` 返回 `service=NebulaGrid`、`status=ok`、`version=0.1.0`。 |
| 管理员登录 | 通过 | `admin / 10592036` 登录成功，`/api/auth/me` 返回 `username=admin`、`role=admin`。 |
| 所有人候选用户接口 | 通过 | `/api/users/list` 返回 10 个用户，包含 `admin`、`wgy`、`lq`、`xpc` 等。 |
| 节点列表初始查询 | 通过 | 管理员初始可见 1 个计算节点：`Node1 / 10.16.61.186`。 |
| 新增节点 | 通过 | 新增 `codex-test-node-0522011240` 成功，IP 为 `10.16.67.45`，SSH 用户为 `ddltm`。 |
| 新增后列表校验 | 通过 | 节点列表能查到新节点；所有人顺序保持为 `310001,3120114`；GPU 型号顺序保持为 `RTX 4090 -> Tesla V100`。 |
| GPU 数量/型号校验 | 警告 | 提交 `gpu_count=2` 但只填写 1 个 GPU 型号时，请求被拒绝；但返回 HTTP 500，建议改为 400/422。 |
| 修改节点 | 通过 | 修改为 `codex-test-node-0522011240-edit` 成功，IP 改为 `10.16.76.124`，所有人改为 `3120114`，GPU 改为 `NVIDIA A100`。 |
| 私有不共享：所有人可见性 | 通过 | 节点设置为 `private + none` 且所有人为 `xpc` 后，`xpc` 可以在 `/api/nodes` 看到该节点。 |
| 私有不共享：非所有人可见性 | 通过 | 同样配置下，`wgy` 在 `/api/nodes` 中看不到该节点。 |
| 重连节点 | 通过 | 调用 `/api/admin/nodes/{id}/reconnect` 后，节点状态变为 `reconnecting`。 |
| 强制下线节点 | 通过 | 调用 `/api/admin/nodes/{id}/force-offline` 后，节点状态变为 `manual_offline`，调度开关为 `false`。 |
| 删除节点 | 通过 | 删除接口成功返回；再次查询管理员节点列表时，临时节点已不存在。 |
| 临时数据清理 | 通过 | 测试节点 `id=7` 已删除，没有遗留测试节点。 |

## 前端部署检查

已直接检查服务器当前加载的静态文件：

| 检查项 | 结果 | 说明 |
| --- | --- | --- |
| 所有人搜索按钮 | 通过 | `http://10.16.59.101/src/app.js` 包含 `data-owner-search-button`。 |
| 搜索函数 | 通过 | `app.js` 包含 `function searchNodeOwners`。 |
| 所有人数据读取 | 通过 | 管理员节点页从 `/users/list` 的 `data` 中读取候选用户列表。 |
| 所有人搜索布局 | 通过 | `styles.css` 包含 `.owner-search-row` 相关样式。 |
| 左侧导航固定 | 通过 | `styles.css` 包含侧栏 `position: fixed`。 |

说明：当前环境没有可用的浏览器自动化执行入口，且本机没有 `node` 命令，因此这次没有做真实浏览器点击级 UI 自动化；已通过接口链路和服务器静态文件内容确认功能部署状态。

## 本地语法检查

已执行：

```powershell
python -m compileall backend\app\api\admin.py backend\app\db\init_db.py backend\app\db\models\core.py backend\app\schemas\nodes.py backend\app\services\dashboard_service.py backend\app\services\node_service.py
```

结果：通过，没有发现 Python 语法错误。

## 后续建议

1. 修复 `gpu_count` 与 `gpu_models` 数量不一致时返回 HTTP 500 的问题，建议统一返回 400 或 422，并让前端展示“GPU 数量必须与 GPU 型号列表条数一致”。
2. 部署到最终服务器后，再补一次真实浏览器手工或自动化检查，重点确认“搜索所有人”按钮点击、复选框勾选、提交新增/修改后的表单回填和左侧导航固定效果。
