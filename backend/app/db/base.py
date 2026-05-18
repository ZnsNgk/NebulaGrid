from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy 声明式模型基类，所有 ORM 表模型都继承它。"""

