from pathlib import Path

from app.core.errors import not_found
from app.core.rbac import Role
from app.schemas.manual import ManualDocument
from app.services.auth_service import UserRecord


MANUAL_BY_ROLE: dict[Role, tuple[str, str]] = {
    # 先统一展示架构设计书；后续可把不同角色映射到 docs 下各自的使用手册。
    Role.STUDENT: ("学生使用手册", "NebulaGrid_Tianshu_3.0_System Architecture Design.md"),
    Role.MENTOR: ("导师使用手册", "NebulaGrid_Tianshu_3.0_System Architecture Design.md"),
    Role.ADMIN: ("管理员使用手册", "NebulaGrid_Tianshu_3.0_System Architecture Design.md"),
    Role.VIEWER: ("展示端使用手册", "NebulaGrid_Tianshu_3.0_System Architecture Design.md"),
}


def get_manual_document(user: UserRecord) -> ManualDocument:
    """按用户角色选择 docs 下的 Markdown 文件，并返回给前端页面展示。"""
    title, filename = MANUAL_BY_ROLE.get(
        user.role,
        ("使用手册", "NebulaGrid_Tianshu_3.0_System Architecture Design.md"),
    )
    repo_root = Path(__file__).resolve().parents[3]
    doc_path = repo_root / "docs" / filename
    if not doc_path.is_file():
        raise not_found(f"manual document not found: {filename}")
    return ManualDocument(
        title=title,
        role=user.role.value,
        source_path=f"docs/{filename}",
        content=doc_path.read_text(encoding="utf-8"),
    )
