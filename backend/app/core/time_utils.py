from datetime import datetime


def local_datetime() -> datetime:
    """返回系统本地时区的 aware datetime，日志展示以部署机器时区为准。"""
    return datetime.now().astimezone()


def local_now() -> str:
    """返回系统本地时区 ISO 字符串，供日志文件和前端响应直接展示。"""
    return local_datetime().isoformat()


def ensure_local_datetime(value: datetime | None) -> datetime | None:
    """把数据库或外部传入的时间统一转换为系统本地时区。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone()


def parse_datetime_local(value: str | None) -> datetime | None:
    """解析前端时间；无时区输入按系统本地时区理解。"""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone()
