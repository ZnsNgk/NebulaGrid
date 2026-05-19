from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
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
        json={"command": "python train.py", "workdir": "/"},
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


def test_envs_are_globally_usable_but_owner_modified() -> None:
    """验证环境可被所有用户选择使用，但包安装和删除等修改动作只允许环境所有者执行。"""
    client = make_client()
    admin_token = login_as_admin(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    create_user_response = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": "env-student",
            "real_name": "Env Student",
            "role": "student",
            "state": "enabled",
            "password": "student123",
        },
    )
    student_token = login_user(client, "env-student", "student123")
    student_headers = {"Authorization": f"Bearer {student_token}"}

    env_response = client.post(
        "/api/envs/register",
        headers=admin_headers,
        json={"name": "shared-torch", "python_version": "3.11"},
    )
    env = env_response.json()["data"]
    task_response = client.post(
        "/api/tasks",
        headers=student_headers,
        json={"command": "python train.py", "workdir": "/", "env_id": env["id"]},
    )
    forbidden_upload = client.post(
        f"/api/envs/{env['id']}/packages/upload",
        headers=student_headers,
        json={"filename": "pkg.whl"},
    )

    assert create_user_response.status_code == 200
    assert env_response.status_code == 200
    assert env["path"] == "/home/ddltm/envs/miniconda3/envs/shared-torch"
    assert client.get("/api/envs", headers=student_headers).json()["data"][0]["can_modify"] is False
    assert task_response.status_code == 200
    assert forbidden_upload.status_code == 403


def test_env_name_cannot_create_nested_conda_directory() -> None:
    """验证环境名只能映射为 miniconda envs 下的一级目录，避免 conda 无法识别。"""
    client = make_client()
    token = login_as_admin(client)
    response = client.post(
        "/api/envs/register",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "owner/nested"},
    )

    assert response.status_code == 422


def test_user_management_maps_linux_accounts_and_deletes_children() -> None:
    """验证管理员映射主账户，教学用户映射子账户，删除用户后账号不可再登录。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}

    admin_list = client.get("/api/users", headers=headers).json()["data"]
    create_response = client.post(
        "/api/users",
        headers=headers,
        json={
            "username": "delete-student",
            "real_name": "Delete Student",
            "role": "student",
            "state": "enabled",
            "password": "student123",
        },
    )
    created = create_response.json()["data"]
    delete_response = client.delete(f"/api/users/{created['id']}", headers=headers)
    login_response = client.post(
        "/api/auth/login",
        json={"identity": "delete-student", "password": "student123"},
    )

    assert admin_list[0]["linux_account_name"] == "ddltm"
    assert admin_list[0]["home_path"] == "/home/ddltm"
    assert create_response.status_code == 200
    assert created["linux_account_name"] == "delete-student"
    assert created["home_path"] == "/home/ddltm/data/user/delete-student"
    assert delete_response.status_code == 200
    assert login_response.status_code == 401


def test_last_admin_user_cannot_be_deleted() -> None:
    """验证用户删除流程保护最后一个管理员，避免系统失去管理入口。"""
    client = make_client()
    token = login_as_admin(client)
    response = client.delete("/api/users/1", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


def login_user(client: TestClient, identity: str, password: str) -> str:
    """使用指定账号登录，并返回后续测试可复用的 Bearer token。"""
    response = client.post(
        "/api/auth/login",
        json={"identity": identity, "password": password},
    )
    return response.json()["data"]["access_token"]


def test_file_manager_crud_uses_user_root_boundary(monkeypatch, tmp_path: Path) -> None:
    """验证文件管理接口在虚拟根目录 / 内完成新建、预览、保存、复制、重命名和删除。"""
    user_home_root = tmp_path / "user"
    monkeypatch.setenv("NEBULAGRID_USER_HOME_ROOT", str(user_home_root))
    monkeypatch.setenv("NEBULAGRID_VISIBLE_ROOTS", str(user_home_root))
    get_settings.cache_clear()
    try:
        client = make_client()
        admin_token = login_as_admin(client)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        client.post(
            "/api/users",
            headers=admin_headers,
            json={
                "username": "file-student",
                "real_name": "File Student",
                "role": "student",
                "state": "enabled",
                "password": "student123",
            },
        )
        user_root = user_home_root / "file-student"
        student_token = login_user(client, "file-student", "student123")
        headers = {"Authorization": f"Bearer {student_token}"}

        mkdir_response = client.post("/api/files/mkdir", headers=headers, json={"path": "/project"})
        create_response = client.post(
            "/api/files/create",
            headers=headers,
            json={"path": "/project/note.txt", "content": "hello"},
        )
        save_response = client.post(
            "/api/files/save",
            headers=headers,
            json={"path": "/project/note.txt", "content": "updated"},
        )
        copy_response = client.post(
            "/api/files/copy",
            headers=headers,
            json={"path": "/project/note.txt", "target_path": "/project/copy.txt"},
        )
        rename_response = client.post(
            "/api/files/rename",
            headers=headers,
            json={"path": "/project/copy.txt", "target_path": "/project/renamed.txt"},
        )
        preview_response = client.get("/api/files/preview?path=/project/renamed.txt", headers=headers)
        download_response = client.get("/api/files/download?path=/project/renamed.txt", headers=headers)
        delete_response = client.delete("/api/files?path=/project/renamed.txt", headers=headers)
        root_delete_response = client.delete("/api/files?path=/", headers=headers)

        assert mkdir_response.status_code == 200
        assert create_response.status_code == 200
        assert save_response.status_code == 200
        assert copy_response.status_code == 200
        assert rename_response.status_code == 200
        assert preview_response.json()["data"]["content"] == "updated"
        assert download_response.text == "updated"
        assert delete_response.status_code == 200
        assert root_delete_response.status_code == 422
        assert user_root.is_dir()
        assert (user_root / "project" / "note.txt").read_text(encoding="utf-8") == "updated"
        assert not (user_root / "project" / "renamed.txt").exists()
    finally:
        get_settings.cache_clear()
