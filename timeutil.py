"""
=========================================================
时间工具模块（timeutil.py）
=========================================================
职责：把库里的 published_at（混存 +00:00 / +08:00 / naive）统一
     转换成上海时区（+08:00）的展示字符串，分钟级精度。

背景：
  RSS 源返回的发布时间格式不统一：
    - 部分带 +00:00（UTC），部分带 +08:00（上海）
    - 少数是 naive（无时区，database.py 现有约定按 UTC 处理）
  钉钉推送卡片要显示"具体发布时间"，必须统一换算到 +08:00。

设计：
  - 用固定 timezone(timedelta(hours=8))，不用 zoneinfo
    （不依赖系统的 /usr/share/zoneinfo 数据）
  - 纯函数、零依赖，notifier 这种独立模块也能安全导入
=========================================================
"""

from datetime import datetime, timedelta, timezone

# 上海时区（固定偏移，不依赖系统时区库）
SHANGHAI = timezone(timedelta(hours=8))
# naive 时间的假定时区（与 database.py 现有约定一致：按 UTC 处理）
_NAIVE_TZ = timezone.utc


def to_shanghai_naive(value) -> datetime:
    """
    把任意 published_at 值归一化为上海时区的 naive datetime。

    规则：
      - 带 +00:00 → 转 +08:00（+8 小时）
      - 带 +08:00 → 原样
      - naive     → 假定 UTC，再转 +08:00（+8 小时）
      - 解析失败  → 返回 None
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        # ISO 带时区的字符串（如 2026-08-10T06:30:00+00:00）
        if value.endswith(("Z", "+00:00", "+08:00", "+09:00", "+05:30")) or "+" in value[10:]:
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            try:
                dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    dt = datetime.fromisoformat(value)
                except ValueError:
                    return None
            dt = dt.replace(tzinfo=_NAIVE_TZ)
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_NAIVE_TZ)
    return dt.astimezone(SHANGHAI).replace(tzinfo=None)


def format_minute_shanghai(value) -> str:
    """格式化到分钟：'2026-08-10 14:43'；无法解析返回空串。"""
    dt = to_shanghai_naive(value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


def shanghai_date(value) -> str:
    """取上海时区的日期 'YYYY-MM-DD'；无法解析返回空串。"""
    dt = to_shanghai_naive(value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d")


def shanghai_now() -> datetime:
    """当前上海时区的 naive datetime（用于"当日"判断）。"""
    return datetime.now(SHANGHAI).replace(tzinfo=None)
