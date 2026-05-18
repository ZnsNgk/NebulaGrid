# NebulaGrid Frontend

这是一个零构建静态测试控制台，用于直接联调当前 FastAPI 后端。

## 使用方式

1. 启动后端：

```bash
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

2. 打开静态前端：

```text
frontend/index.html
```

也可以启动静态服务：

```bash
cd frontend
python -m http.server 5173
```

3. 如果使用静态服务，浏览器打开：

```text
http://127.0.0.1:5173
```

默认 API 地址为：

```text
http://127.0.0.1:8000/api
```

默认测试账号：

```text
admin / admin123
```
