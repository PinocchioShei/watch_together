"""通用时间工具函数。"""

from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_str(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def str_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
