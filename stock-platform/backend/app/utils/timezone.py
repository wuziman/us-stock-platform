"""
全栈统一时区工具（铁律：所有时间入库、计算、展示统一为无时区 naive 北京时间 Asia/Shanghai）

设计准则（详见仓库根目录 / stock-platform/TIMEZONE.md）：
1. 数据库存储、后端业务计算（持有天数、交易时段判定、日报时间戳、调度窗口）、接口返回统一使用
   无时区（naive）北京时间（Asia/Shanghai, UTC+8）。
2. 严禁在数据库存入带时区（aware）的 datetime，严禁在业务逻辑中直接调用依赖宿主机时区的
   裸 datetime.now() / date.today() 或转换为 timezone.utc。
3. 外部传入的时间（如前端 ISO8601 带 Z 或 +08:00）：
   - aware datetime → 先 astimezone(Asia/Shanghai)，再去 tzinfo（.replace(tzinfo=None)）
   - naive datetime → 视为北京时间直接通过
   - date (非 datetime) → 转为当日 00:00:00 的 naive 北京时间 datetime
"""

from datetime import date, datetime
from typing import Optional

import pytz

SH_TZ = pytz.timezone("Asia/Shanghai")


def to_naive_beijing(dt) -> Optional[datetime]:
    """
    将任意 datetime / date 统一转换为无时区 naive 北京时间 (Asia/Shanghai)：
    - aware datetime: astimezone(Asia/Shanghai) 后去掉 tzinfo
    - naive datetime: 视为北京时间直接原样通过
    - date (非 datetime): 转为 datetime(y, m, d)
    """
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
            return dt.astimezone(SH_TZ).replace(tzinfo=None)
        return dt
    if isinstance(dt, date):
        return datetime(dt.year, dt.month, dt.day)
    return dt


def now_beijing() -> datetime:
    """返回当前无时区 naive 北京时间（不受服务器/容器宿主机时区影响）"""
    return datetime.now(SH_TZ).replace(tzinfo=None)


def today_beijing() -> date:
    """返回当前北京时间的日期对象（不受服务器/容器宿主机时区影响）"""
    return datetime.now(SH_TZ).date()
