"""
价格监控服务：止损/止盈微信告警 + 组合净值快照 + 回撤破线告警

由 scheduler_service 每10分钟触发 run_price_monitor()：
- 仅在各持仓所属市场的交易时段内工作（美股9:30-16:00 ET / A股9:30-11:30,13:00-15:00，自动跳过美股/A股法定节假日）
- 止损/止盈穿越 → 企业微信推送（同天同仓同类型只推一次，PriceAlertLog去重；推送失败次日重试）
- 每轮顺带把组合总市值 upsert 进 PortfolioSnapshot（净值曲线数据源）
- 当前回撤 >= 20%（CLAUDE.md风控规则）→ 微信告警，每天最多一次
"""

from concurrent.futures import ThreadPoolExecutor
import logging
from datetime import datetime, date, time as dtime
from typing import Dict, List, Optional, Tuple

import pytz

from ..database import SessionLocal
from ..models import Position, Setting
from ..models_platform import PriceAlertLog, PortfolioSnapshot
from .report_service import ReportService
from .stock_service import StockService
from ..utils.market import detect_market

logger = logging.getLogger(__name__)

KEY_ALERT_ENABLED = 'price_alert_enabled'   # Setting键：'true'(默认)/'false'
MAX_DRAWDOWN_PCT = 20.0                     # CLAUDE.md：组合最大回撤20%
DEFAULT_USD_CNY_RATE = 7.2                  # 美股+A股跨市场混合持仓默认折算汇率


# ============================================
# 法定休市节假日判断（纯函数，便于单测）
# ============================================
def _is_us_fixed_or_observed(d: date, month: int, day: int) -> bool:
    """检查美股固定日期节假日及其周末顺延补休规则（周六落休周五，周日落休周一）"""
    try:
        actual = date(d.year, month, day)
    except ValueError:
        return False
    if d == actual:
        return True
    if actual.weekday() == 5 and d == date(d.year, month, day - 1):
        return True
    if actual.weekday() == 6 and d == date(d.year, month, day + 1):
        return True
    return False


def is_trading_holiday(market: str, d: date) -> bool:
    """判定指定日期是否为美股(US)或A股(A)主要法定休市日"""
    if market == 'US':
        # 1. 固定节假日及顺延：元旦(1/1)、六月节(6/19)、独立日(7/4)、圣诞节(12/25)
        for m, day_num in ((1, 1), (6, 19), (7, 4), (12, 25)):
            if _is_us_fixed_or_observed(d, m, day_num):
                return True
        # 2. 马丁路德金日：1月第3个周一
        if d.month == 1 and d.weekday() == 0 and 15 <= d.day <= 21:
            return True
        # 3. 总统日：2月第3个周一
        if d.month == 2 and d.weekday() == 0 and 15 <= d.day <= 21:
            return True
        # 4. 阵亡将士纪念日：5月最后一个周一
        if d.month == 5 and d.weekday() == 0 and d.day >= 25:
            return True
        # 5. 劳工节：9月第1个周一
        if d.month == 9 and d.weekday() == 0 and d.day <= 7:
            return True
        # 6. 感恩节：11月第4个周四
        if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
            return True
        return False

    # A股法定节假日：元旦(1/1)、五一劳动节(5/1-5/3)、十一国庆黄金周(10/1-10/7)
    if d.month == 1 and d.day == 1:
        return True
    if d.month == 5 and 1 <= d.day <= 3:
        return True
    if d.month == 10 and 1 <= d.day <= 7:
        return True
    return False


# ============================================
# 纯函数（便于单测）
# ============================================
def is_market_open(market: str, now: Optional[datetime] = None) -> bool:
    """判定指定市场当前是否处于交易时段（naive时间按北京时间理解；自动过滤周末与主要法定节假日）"""
    now = now or datetime.now()
    sh_tz = pytz.timezone('Asia/Shanghai')
    if now.tzinfo is None:
        now = sh_tz.localize(now)

    if market == 'US':
        local = now.astimezone(pytz.timezone('America/New_York'))
        if local.weekday() > 4 or is_trading_holiday('US', local.date()):
            return False
        t = local.time()
        return dtime(9, 30) <= t <= dtime(16, 0)
    # A股（本机时区即北京时间）
    local_cn = now.astimezone(sh_tz)
    if local_cn.weekday() > 4 or is_trading_holiday('A', local_cn.date()):
        return False
    t = local_cn.time()
    return dtime(9, 30) <= t <= dtime(11, 30) or dtime(13, 0) <= t <= dtime(15, 0)


def compute_drawdown_stats(values: List[float]) -> Dict:
    """给定净值序列，返回当前回撤与历史最大回撤（百分比，相对历史峰值）"""
    if not values:
        return {'current_drawdown_pct': 0.0, 'max_drawdown_pct': 0.0,
                'peak_value': 0.0, 'latest_value': 0.0}
    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100
            if dd > max_dd:
                max_dd = dd
    latest = values[-1]
    cur_dd = (peak - latest) / peak * 100 if peak > 0 else 0.0
    return {
        'current_drawdown_pct': round(cur_dd, 2),
        'max_drawdown_pct': round(max_dd, 2),
        'peak_value': round(float(max(values)), 2),
        'latest_value': round(float(latest), 2),
    }


# ============================================
# 数据访问与主流程
# ============================================
def _holding_quotes() -> List[Tuple[Position, float]]:
    """全部持仓的现价（并行拉取，行情失败/停牌回退买入价），返回[(position, price)]"""
    svc = StockService()
    db = SessionLocal()
    try:
        positions = db.query(Position).filter(Position.status == "holding").all()
    finally:
        db.close()

    if not positions:
        return []

    def _fetch_one(pos: Position) -> Tuple[Position, float]:
        market = pos.market or detect_market(pos.stock_code)
        try:
            quote = svc.get_realtime_quote(pos.stock_code, market)
        except Exception:
            quote = None
        price = quote['price'] if (quote and quote.get('price')) else pos.buy_price
        return (pos, price)

    with ThreadPoolExecutor(max_workers=min(8, len(positions))) as executor:
        return list(executor.map(_fetch_one, positions))


def _alert_already_sent(today: str, position_id: int, alert_type: str) -> bool:
    db = SessionLocal()
    try:
        dup = db.query(PriceAlertLog).filter(
            PriceAlertLog.position_id == position_id,
            PriceAlertLog.alert_type == alert_type,
            PriceAlertLog.triggered_date == today).first()
        return dup is not None
    finally:
        db.close()


def _record_alert(position_id: int, stock_code: str, stock_name: str,
                  alert_type: str, price: float, today: str):
    db = SessionLocal()
    try:
        db.add(PriceAlertLog(
            position_id=position_id, stock_code=stock_code, stock_name=stock_name,
            alert_type=alert_type, price=float(price), triggered_date=today,
            created_at=datetime.now()))
        db.commit()
    finally:
        db.close()


def check_position_alerts(report_svc: ReportService, today: str) -> List[Dict]:
    """逐仓检查止损/止盈穿越并推微信。仅推送成功才记日志（失败下一轮自动重试）"""
    triggered: List[Dict] = []
    for pos, price in _holding_quotes():
        if not price:
            continue
        checks = []
        if pos.stop_loss and price <= pos.stop_loss:
            checks.append(('stop_loss', '🚨 止损触发预警', '≤', pos.stop_loss))
        if pos.take_profit and price >= pos.take_profit:
            checks.append(('take_profit', '🎯 止盈达标提醒', '≥', pos.take_profit))

        mkt = pos.market or detect_market(pos.stock_code)
        sym = '¥' if mkt == 'A' else '$'

        for alert_type, title, op, level_price in checks:
            if _alert_already_sent(today, pos.id, alert_type):
                continue
            pnl_pct = (price / pos.buy_price - 1) * 100 if pos.buy_price else 0
            content = (
                f"### {title}\n"
                f"> **{pos.stock_code} {pos.stock_name}**\n"
                f"> 现价 **{sym}{price:.2f}** {op} 触发位 **{sym}{level_price:.2f}**\n"
                f"> 距买入价({sym}{pos.buy_price:.2f})：{pnl_pct:+.1f}%\n"
                f"> 请检查仓位纪律 ⚠️"
            )
            result = report_svc.send_to_wechat(content)
            sent = result.get('sent', False)
            if sent:
                _record_alert(pos.id, pos.stock_code, pos.stock_name or '',
                              alert_type, price, today)
            triggered.append({'code': pos.stock_code, 'type': alert_type, 'sent': sent})
    return triggered


def update_portfolio_snapshot() -> float:
    """把组合总市值 upsert 到今天的快照（净值曲线数据源），返回总市值（跨币种混持自动折算）"""
    rows = _holding_quotes()
    markets = {pos.market or detect_market(pos.stock_code) for pos, _ in rows}
    has_mixed = ('A' in markets and 'US' in markets)

    total = 0.0
    for pos, price in rows:
        mkt = pos.market or detect_market(pos.stock_code)
        scale = (1.0 / DEFAULT_USD_CNY_RATE) if (has_mixed and mkt == 'A') else 1.0
        total += float(pos.quantity or 0) * float(price or 0) * scale

    today = date.today().isoformat()
    db = SessionLocal()
    try:
        snap = db.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.snap_date == today).first()
        if snap:
            snap.total_value = round(total, 2)
        else:
            db.add(PortfolioSnapshot(snap_date=today, total_value=round(total, 2),
                                     created_at=datetime.now()))
        db.commit()
    finally:
        db.close()
    return round(total, 2)


def check_drawdown_alert(report_svc: ReportService, today: str) -> Optional[Dict]:
    """当前回撤超阈值时微信告警（组合级每天最多一次，position_id=0哨兵去重）"""
    db = SessionLocal()
    try:
        snaps = [s.total_value for s in
                 db.query(PortfolioSnapshot).order_by(PortfolioSnapshot.snap_date.asc()).all()]
    finally:
        db.close()

    stats = compute_drawdown_stats(snaps)
    if stats['peak_value'] <= 0 or stats['current_drawdown_pct'] < MAX_DRAWDOWN_PCT:
        return None
    if _alert_already_sent(today, 0, 'drawdown'):
        return {'already_sent': True, **stats}

    content = (
        f"### 🔻 组合回撤破线预警\n"
        f"> 当前组合市值 **{stats['latest_value']:,.2f}**\n"
        f"> 历史峰值 **{stats['peak_value']:,.2f}**\n"
        f"> 当前回撤 **-{stats['current_drawdown_pct']:.1f}%** 已超过 {MAX_DRAWDOWN_PCT:.0f}% 风控线\n"
        f"> 请审视整体仓位 ⚠️"
    )
    result = report_svc.send_to_wechat(content)
    sent = result.get('sent', False)
    if sent:
        _record_alert(0, 'drawdown', '组合', 'drawdown',
                      stats['latest_value'], today)
    logger.warning(f"组合回撤{stats['current_drawdown_pct']:.1f}%破线，告警{'已推送' if sent else '推送失败'}")
    return {'sent': sent, **stats}


def run_price_monitor() -> Dict:
    """调度器入口：交易时段内 查止盈止损→更新净值快照→查回撤破线"""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == KEY_ALERT_ENABLED).first()
        enabled = (row.value if row else 'true').strip().lower() != 'false'
        holdings = db.query(Position).filter(Position.status == "holding").all()
    finally:
        db.close()

    if not enabled:
        return {'skipped': '监控开关关闭'}
    if not holdings:
        return {'skipped': '无持仓'}
    markets = {p.market or detect_market(p.stock_code) for p in holdings}
    if not any(is_market_open(m) for m in markets):
        return {'skipped': '休市中'}

    report_svc = ReportService()
    today = date.today().isoformat()
    triggered = check_position_alerts(report_svc, today)
    total = update_portfolio_snapshot()
    dd = check_drawdown_alert(report_svc, today)
    if triggered:
        logger.info(f"价格监控触发告警: {[(t['code'], t['type']) for t in triggered]}")
    return {'triggered': triggered, 'total_value': total, 'drawdown_alert': dd}
