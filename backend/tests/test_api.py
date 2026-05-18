from fastapi.testclient import TestClient

from app.main import create_app


def make_client() -> TestClient:
    """创建隔离的测试客户端，避免测试直接复用全局 app 状态。"""
    return TestClient(create_app())


def test_health_check_returns_ok() -> None:
    """验证健康检查接口使用统一响应格式并返回 ok 状态。"""
    client = make_client()
    response = client.get("/api/health")
    payload = response.json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["data"]["status"] == "ok"


def test_auth_login_and_me() -> None:
    """验证演示账号可以登录，并能通过 Bearer 令牌读取当前用户。"""
    client = make_client()
    login_response = client.post(
        "/api/auth/login",
        json={"identity": "admin", "password": "admin123"},
    )
    login_payload = login_response.json()
    token = login_payload["data"]["access_token"]

    me_response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    me_payload = me_response.json()

    assert login_response.status_code == 200
    assert me_response.status_code == 200
    assert me_payload["data"]["username"] == "admin"


def test_task_lifecycle_smoke() -> None:
    """验证任务提交、查询、取消和日志占位接口可以形成最小闭环。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}

    create_response = client.post(
        "/api/tasks",
        headers=headers,
        json={"command": "python train.py", "workdir": "/workspace"},
    )
    task_id = create_response.json()["data"]["task_id"]
    detail_response = client.get(f"/api/tasks/{task_id}", headers=headers)
    cancel_response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
    log_response = client.get(f"/api/tasks/{task_id}/log", headers=headers)

    assert create_response.status_code == 200
    assert detail_response.json()["data"]["task_id"] == task_id
    assert cancel_response.json()["data"]["state"] == "cancelled"
    assert task_id in log_response.text


def test_admin_node_and_settings_smoke() -> None:
    """验证管理员节点登记和配置读写接口符合统一响应格式。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}

    node_response = client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "node-a", "ip": "10.0.0.10", "gpu_models": ["A100"]},
    )
    settings_response = client.patch(
        "/api/admin/settings",
        headers=headers,
        json={"values": {"scheduler.enabled": "false"}},
    )

    assert node_response.status_code == 200
    assert node_response.json()["data"]["name"] == "node-a"
    assert settings_response.status_code == 200
    assert settings_response.json()["ok"] is True


def login_as_admin(client: TestClient) -> str:
    """登录演示管理员账号，并返回后续测试可复用的 Bearer token。"""
    response = client.post(
        "/api/auth/login",
        json={"identity": "admin", "password": "admin123"},
    )
    return response.json()["data"]["access_token"]
