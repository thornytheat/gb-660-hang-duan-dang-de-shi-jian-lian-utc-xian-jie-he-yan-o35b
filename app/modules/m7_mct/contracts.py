"""入参契约与领域错误。沿用 app.errors.DomainError 的形态（code/message/status）。"""
import re
from datetime import date, datetime, timezone
from app.errors import DomainError

AIRPORT_RE = re.compile(r"^[A-Z]{3}$")
TERMINAL_RE = re.compile(r"^[A-Z0-9]{1,8}$")
MAX_CONNECTION_MINUTES = 720  # 与 M5 demo 口径一致：等待超过 12 小时判 TOO_LONG


def fail(code: str, message: str, status: int = 422):
    raise DomainError(code, message, status)


def airport(value: str) -> str:
    token = str(value).strip().upper()
    if not AIRPORT_RE.fullmatch(token):
        fail("BAD_AIRPORT", "机场必须为 3 位 IATA 大写代码")
    return token


def terminal(value: str) -> str:
    token = str(value).strip().upper()
    if not TERMINAL_RE.fullmatch(token):
        fail("BAD_TERMINAL", "航站楼代码为 1-8 位大写字母或数字")
    return token


def iso_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        fail("BAD_DATE", "日期需为 ISO 8601 日期（YYYY-MM-DD）")


def utc_instant(value: str) -> datetime:
    """仅解析调用方已给出的时刻；本模块不做当地时刻换算（不改航段时刻解析）。"""
    try:
        instant = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        fail("BAD_INSTANT", "时刻需为带偏移的 ISO 8601 时间")
    if instant.tzinfo is None:
        fail("BAD_INSTANT", "时刻必须带 UTC 偏移")
    return instant.astimezone(timezone.utc)


def positive_minutes(value, field: str) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        fail("BAD_MINUTES", f"{field} 必须为整数分钟")
    if minutes <= 0:
        fail("BAD_MINUTES", f"{field} 必须为正整数")
    return minutes


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
