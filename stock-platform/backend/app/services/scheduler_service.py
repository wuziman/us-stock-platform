"""
定时任务服务
- APScheduler后台调度：每30分钟检查一次是否到达日报推送窗口，并顺带清理超过24小时的陈旧K线缓存
- 防重复发送：Setting.last_sent_date 记录当日已推送
- 防开发期误发：schedule_enabled 默认false，需在页面显式开启
"""

import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Watchlist, Setting
from .report_service import ReportService
from .alert_service import run_price_monitor
from .stock_service import cleanup_expired_kline_cache
from ..routers.analysis import cleanup_expired_analysis_history
from ..utils.timezone import SH_TZ, now_beijing, today_beijing

logger = logging.getLogger(__name__)

# Setting键名
KEY_ENABLED = 'schedule_enabled'    # 'true'/'false'
KEY_HOUR = 'schedule_hour'          # '17'
KEY_MINUTE = 'schedule_minute'      # '30'
KEY_LAST_SENT = 'last_sent_date'    # 'YYYY-MM-DD'


# ============================================
# Setting读写
# ============================================
def _get_setting(db: Session, key: str, default: str = '') -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row and row.value is not None else default


def _set_setting(db: Session, key: str, value: str):
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def get_schedule_config(db: Session) -> Dict:
    """读取调度配置（未设置时返回默认值）"""
    try:
        hour = int(_get_setting(db, KEY_HOUR, '17'))
    except ValueError:
        hour = 17
    try:
        minute = int(_get_setting(db, KEY_MINUTE, '30'))
    except ValueError:
        minute = 30
    return {
        'enabled': _get_setting(db, KEY_ENABLED, 'false').strip().lower() == 'true',
        'hour': hour,
        'minute': minute,
        'last_sent_date': _get_setting(db, KEY_LAST_SENT, '') or None,
    }



# ============================================
# 本地 SQLite 数据库每日备份与 30 天滚动清理
# ============================================
BACKUP_RETENTION_DAYS = 30


def backup_database(
    db_path: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
    retention_days: int = BACKUP_RETENTION_DAYS,
    now_dt: Optional[datetime] = None,
    force: bool = False,
) -> Dict:
    """
    每日将 stock_platform.db 备份至 backups/stock_platform_YYYYMMDD.db，
    并自动清理超过 retention_days（默认 30 天）的历史备份文件。
    """
    backend_root = Path(__file__).resolve().parents[2]
    src_db = Path(db_path) if db_path is not None else (backend_root / "stock_platform.db")
    dst_dir = Path(backup_dir) if backup_dir is not None else (backend_root / "backups")
    ref_dt = now_dt or now_beijing()
    date_tag = ref_dt.strftime("%Y%m%d")
    dst_file = dst_dir / f"stock_platform_{date_tag}.db"

    created = False
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        if src_db.exists() and (force or not dst_file.exists()):
            shutil.copy2(src_db, dst_file)
            created = True
    except Exception as e:
        logger.warning(f"数据库备份失败: {e}")

    deleted_files: List[str] = []
    cutoff_date = ref_dt.date() - timedelta(days=retention_days)
    try:
        if dst_dir.exists():
            # 同时遍历 glob 与显式扫描，兼容特殊文件系统缓存
            for f in sorted(dst_dir.glob("stock_platform_*.db")):
                m = re.match(r"^stock_platform_(\d{8})\.db$", f.name)
                if not m:
                    continue
                try:
                    file_date = datetime.strptime(m.group(1), "%Y%m%d").date()
                except ValueError:
                    continue
                if file_date < cutoff_date:
                    try:
                        f.unlink()
                        deleted_files.append(f.name)
                    except Exception as del_err:
                        logger.warning(f"清理过期备份失败 {f.name}: {del_err}")
    except Exception as e:
        logger.warning(f"扫描过期备份异常: {e}")

    return {
        "backup_file": str(dst_file),
        "backup_name": dst_file.name,
        "created": created,
        "deleted_files": deleted_files,
        "retention_days": retention_days,
    }


# ============================================
# 推送决策（纯函数，便于单测）
# ============================================
def should_send(now_weekday: int,
                now_time: datetime,
                enabled: bool,
                cfg_h: int,
                cfg_m: int,
                last_sent_date: Optional[str]) -> bool:
    """
    判定当前时刻是否应发送定时日报

    - 未启用 → False
    - 非周一~五 → False
    - 当前时间与配置HH:MM相差>=30分钟 → False
    - 今天已发送过 → False
    """
    if not enabled:
        return False
    if now_weekday < 0 or now_weekday > 4:  # 0=周一 ... 6=周日
        return False
    now_minutes = now_time.hour * 60 + now_time.minute
    cfg_minutes = cfg_h * 60 + cfg_m
    if abs(now_minutes - cfg_minutes) >= 30:
        return False
    if last_sent_date == now_time.date().isoformat():
        return False
    return True


# ============================================
# 执行动作
# ============================================
def run_now() -> Dict:
    """无视开关，立即生成日报并推送企业微信"""
    db = SessionLocal()
    try:
        watchlist = db.query(Watchlist).order_by(Watchlist.created_at.desc()).all()
        items = [{'stock_code': w.stock_code, 'stock_name': w.stock_name} for w in watchlist]
    finally:
        db.close()

    if not items:
        return {'sent': False, 'message': '自选股列表为空', 'report': None}

    service = ReportService()
    data = service.generate_daily_report(items)
    result = service.send_to_wechat(data['report'])
    sent = result.get('sent', False)
    return {
        'sent': sent,
        'message': '推送成功' if sent else f"推送失败: {result.get('error')}",
        'report': data['report'],
    }


def scheduled_job():
    """APScheduler每30分钟触发：顺带清理过期K线缓存，到达推送窗口且今日未发时执行日报"""
    try:
        cleanup_expired_kline_cache(max_age_hours=24)
        cleanup_expired_analysis_history(max_age_days=90)
        backup_database(retention_days=BACKUP_RETENTION_DAYS)
        now = now_beijing()
        db = SessionLocal()
        try:
            cfg = get_schedule_config(db)
            last_sent = _get_setting(db, KEY_LAST_SENT, '') or None
        finally:
            db.close()

        if not should_send(now.weekday(), now, cfg['enabled'],
                           cfg['hour'], cfg['minute'], last_sent):
            return

        logger.info("到达定时日报推送窗口，开始生成并推送")
        result = run_now()
        if result.get('sent'):
            # 成功后记录今天已发送（北京时间日期），防止窗口内重复推送
            db = SessionLocal()
            try:
                _set_setting(db, KEY_LAST_SENT, today_beijing().isoformat())
            finally:
                db.close()
            logger.info("定时日报推送成功")
        else:
            logger.warning(f"定时日报推送失败: {result.get('message')}")
    except Exception as e:
        # 调度任务内任何异常只打日志，不向上抛出
        logger.exception(f"定时日报任务异常: {e}")


def price_monitor_job():
    """APScheduler每10分钟触发：交易时段内检查止损止盈、更新净值快照、检查回撤破线"""
    try:
        run_price_monitor()
    except Exception as e:
        # 调度任务内任何异常只打日志，不向上抛出
        logger.exception(f"价格监控任务异常: {e}")


# ============================================
# 调度器生命周期
# ============================================
def start_scheduler(app) -> BackgroundScheduler:
    """启动后台调度器（防重复启动），保存到app.state.scheduler"""
    existing = getattr(app.state, 'scheduler', None)
    if existing is not None:
        return existing

    scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
    scheduler.add_job(
        scheduled_job,
        'interval',
        minutes=30,
        id='daily_report_job',
        max_instances=1,   # 上一次未跑完时不并发
        coalesce=True,     # 积压多次触发合并为一次
        next_run_time=SH_TZ.localize(now_beijing()),  # 启动即检查一次（显式北京时间）
    )
    scheduler.add_job(
        price_monitor_job,
        'interval',
        minutes=10,
        id='price_monitor_job',
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("定时调度器已启动：每30分钟检查自动日报 + 每10分钟检查止损止盈/净值快照")
    return scheduler
