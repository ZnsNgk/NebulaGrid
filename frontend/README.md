# NebulaGrid Frontend

这是 NebulaGrid 的零构建交互式前端控制台，不需要 Node.js 或 npm。页面可以直接通过 Nginx 托管，也可以在本地用 Python 静态服务打开。

## 功能

- 登录/退出
- 演示模式，无后端时也能体验页面交互
- 仪表盘总览
- 节点登记和列表查看
- 任务提交、取消、重提、查看日志
- 环境登记和列表查看
- 文件浏览和文本预览
- 用户创建和列表查看
- 系统设置和审计日志查看

## 本地打开

直接打开：

```text
frontend/index.html
```

或启动静态服务：

```bash
cd frontend
python -m http.server 5173
```

浏览器访问：

```text
http://127.0.0.1:5173
```

## 后端地址

登录页可以填写 API 地址，例如：

```text
http://127.0.0.1:8000/api
```

如果通过 Nginx 访问主节点：

```text
http://主节点IP/api
```

## 测试账号

默认测试账号：

```text
admin / admin123
```

后端没启动时，可以点击登录页的“进入演示模式”先体验完整交互。

