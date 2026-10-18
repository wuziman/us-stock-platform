"""
价格监控服务：止损/止盈/移动保本止盈微信告警 + 组合净值快照 + 回撤破线告警 + 实盘风控开关单点读取

由 scheduler_service 每10分钟触发 run_price_monitor()：
- 仅在各持仓所属市场的交易时段内工作（美股9:30-16:00 ET / A股9:30-11:30,13:00-15:00，自动跳过美股/A股法定节假日）
- 止损/止盈/移动保本止损穿越 → 企业微信推送（同天同仓同类型只推一次，PriceAlertLog去重；推送失败次日重试）
- 每轮顺带把组合总市值 upsert 进 PortfolioSnapshot（净值曲线数据源）
- 当前回撤 >= 20%（CLAUDE.md风控规则）→ 微信告警，每天最多一次
"""

from concurrent.futures import ThreadPoolExecutor
import logging
from datetime import datetime, date, time as dtime, timedelta
from typing import Dict, List, Optional, Tuple

import pytz

from ..database import SessionLocal
from ..models import Position, Setting
from ..models_platform import PriceAlertLog, PortfolioSnapshot
from .report_service import ReportService
from .stock_service import StockService
from ..utils.market import detect_market
from ..utils.price_levels import compute_trailing_stop
from ..utils.timezone import SH_TZ, now_beijing, today_beijing

logger = logging.getLogger(__name__)

KEY_ALERT_ENABLED = 'price_alert_enabled'           # Setting键：'true'(默认)/'false'
KEY_SIZING_MODE = 'sizing_mode'                     # Setting键：'half_kelly_combined'(默认)/'fixed_2pct'
KEY_TRAILING_STOP_ENABLED = 'trailing_stop_enabled' # Setting键：'true'(默认)/'false'
KEY_STAGED_ADD_ENABLED = 'staged_add_enabled'       # Setting键：'true'(默认)/'false'
KEY_REGIME_LINKAGE_ENABLED = 'regime_linkage_enabled'  # Setting键：'true'(默认)/'false'

MAX_DRAWDOWN_PCT = 20.0                     # CLAUDE.md：组合最大回撤20%
DEFAULT_USD_CNY_RATE = 7.2                  # 美股+A股跨市场混合持仓默认折算汇率

# 平台默认开启实盘风控增强模式（Default Risk-Control On）
DEFAULT_RISK_SWITCHES: Dict[str, object] = {
    'sizing_mode': 'half_kelly_combined',
    'trailing_stop_enabled': True,
    'staged_add_enabled': True,
    'regime_linkage_enabled': True,
}


def get_risk_switches(db=None) -> Dict[str, object]:
    """
    读取实盘风控增强 4 个开关（全栈唯一单点，默认全部开启 Default Risk-Control On）：
    - sizing_mode: 'half_kelly_combined'（默认） | 'fixed_2pct'
    - trailing_stop_enabled: True（默认） | False
    - staged_add_enabled: True（默认） | False
    - regime_linkage_enabled: True（默认，统管 report_service / analysis_service / portfolio 三处） | False
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        rows = {r.key: r.value for r in db.query(Setting).all() if r.key}
        sizing_mode = (rows.get(KEY_SIZING_MODE) or 'half_kelly_combined').strip()
        if sizing_mode not in ('half_kelly_combined', 'fixed_2pct'):
            sizing_mode = 'half_kelly_combined'

        def _bool(key: str, default: bool = True) -> bool:
            raw = rows.get(key)
            if raw is None:
                return default
            return str(raw).strip().lower() not in ('false', '0', 'no', 'off')

        return {
            'sizing_mode': sizing_mode,
            'trailing_stop_enabled': _bool(KEY_TRAILING_STOP_ENABLED, True),
            'staged_add_enabled': _bool(KEY_STAGED_ADD_ENABLED, True),
            'regime_linkage_enabled': _bool(KEY_REGIME_LINKAGE_ENABLED, True),
        }
    except Exception:
        return dict(DEFAULT_RISK_SWITCHES)
    finally:
        if close_db:
            db.close()


def is_regime_linkage_enabled(db=None) -> bool:
    """判定全局市场熔断/预警点位与仓位联动开关是否开启（默认 True）"""
    return bool(get_risk_switches(db).get('regime_linkage_enabled', True))


def get_usd_cny_rate(db=None) -> float:
    """读取美元兑人民币汇率设置（跨市场混合持仓折算唯一单点），默认7.2"""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        row = db.query(Setting).filter(Setting.key == "usd_cny_rate").first()
        val = float(row.value) if row and row.value is not None else DEFAULT_USD_CNY_RATE
        return val if val > 0 else DEFAULT_USD_CNY_RATE
    except (TypeError, ValueError):
        return DEFAULT_USD_CNY_RATE
    finally:
        if close_db:
            db.close()


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


def _easter_sunday(year: int) -> date:
    """使用 Butcher 格里高利历算法精确计算任意年份复活节周日日期"""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l_val = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_val) // 451
    month = (h + l_val - 7 * m + 114) // 31
    day = ((h + l_val - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _good_friday(year: int) -> date:
    """计算美股法定休市日：耶稣受难日（Good Friday = 复活节周日前2天）"""
    return _easter_sunday(year) - timedelta(days=2)


# A股农历/节气法定休市区间表（覆盖春节、清明节、端午节、中秋节）
_CN_TRADITIONAL_HOLIDAY_RANGES: Dict[int, List[Tuple[Tuple[int, int], Tuple[int, int]]]] = {
    2024: [
        ((2, 9), (2, 16)),    # 春节
        ((4, 4), (4, 5)),     # 清明节
        ((6, 10), (6, 10)),   # 端午节
        ((9, 16), (9, 17)),   # 中秋节
    ],
    2025: [
        ((1, 28), (2, 4)),    # 春节
        ((4, 4), (4, 4)),     # 清明节
        ((5, 30), (6, 2)),    # 端午节
        ((10, 6), (10, 8)),   # 中秋节（与国庆连休）
    ],
    2026: [
        ((2, 16), (2, 23)),   # 春节
        ((4, 4), (4, 6)),     # 清明节
        ((6, 19), (6, 19)),   # 端午节
        ((9, 25), (9, 27)),   # 中秋节
    ],
    2027: [
        ((2, 5), (2, 12)),    # 春节
        ((4, 5), (4, 5)),     # 清明节
        ((6, 9), (6, 9)),     # 端午节
        ((9, 15), (9, 15)),   # 中秋节
    ],
    2028: [
        ((1, 25), (2, 1)),    # 春节
        ((4, 4), (4, 4)),     # 清明节
        ((5, 26), (5, 29)),   # 端午节
        ((10, 3), (10, 3)),   # 中秋节
    ],
}


def is_trading_holiday(market: str, d: date) -> bool:
    """判定指定日期是否为美股(US)或A股(A)主要法定休市日"""
    if market == 'US':
        for m, day_num in ((1, 1), (6, 19), (7, 4), (12, 25)):
            if _is_us_fixed_or_observed(d, m, day_num):
                return True
        if d.month == 1 and d.weekday() == 0 and 15 <= d.day <= 21:
            return True
        if d.month == 2 and d.weekday() == 0 and 15 <= d.day <= 21:
            return True
        if d == _good_friday(d.year):
            return True
        if d.month == 5 and d.weekday() == 0 and d.day >= 25:
            return True
        if d.month == 9 and d.weekday() == 0 and d.day <= 7:
            return True
        if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
            return True
        return False

    if d.month == 1 and d.day == 1:
        return True
    if d.month == 5 and 1 <= d.day <= 3:
        return True
    if d.month == 10 and 1 <= d.day <= 7:
        return True

    ranges = _CN_TRADITIONAL_HOLIDAY_RANGES.get(d.year, [])
    for (m1, d1), (m2, d2) in ranges:
        if date(d.year, m1, d1) <= d <= date(d.year, m2, d2):
            return True

    if d.year not in _CN_TRADITIONAL_HOLIDAY_RANGES and d.month == 4 and d.day in (4, 5):
        return True

    return False


# ============================================
# 纯函数（便于单测）
# ============================================
def is_market_open(market: str, now: Optional[datetime] = None) -> bool:
    """判定指定市场当前是否处于交易时段（naive时间按北京时间理解；自动过滤周末与主要法定节假日）"""
    now = now or now_beijing()
    sh_tz = SH_TZ
    if now.tzinfo is None:
        now = sh_tz.localize(now)

    if market == 'US':
        local = now.astimezone(pytz.timezone('America/New_York'))
        if local.weekday() > 4 or is_trading_holiday('US', local.date()):
            return False
        t = local.time()
        return dtime(9, 30) <= t <= dtime(16, 0)
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
            created_at=now_beijing()))
        db.commit()
    finally:
        db.close()


def _update_position_highest_price(position_id: int, new_highest: float):
    db = SessionLocal()
    try:
        pos = db.query(Position).filter(Position.id == position_id).first()
        if pos is not None:
            pos.highest_price = round(float(new_highest), 4)
            db.commit()
    finally:
        db.close()


def check_position_alerts(report_svc: ReportService, today: str) -> List[Dict]:
    """
    逐仓检查固定止损 / 移动保本止盈(trailing_stop) / 止盈穿越并推微信。
    仅推送成功才记日志（失败下一轮自动重试）。
    """
    switches = get_risk_switches()
    use_trailing = bool(switches.get('trailing_stop_enabled', True))
    triggered: List[Dict] = []

    for pos, price in _holding_quotes():
        if not price:
            continue
        prev_high = float(pos.highest_price or pos.buy_price or price)
        highest = max(prev_high, float(price))
        if highest > prev_high:
            _update_position_highest_price(pos.id, highest)
            pos.highest_price = highest

        checks = []
        base_stop = float(pos.stop_loss) if pos.stop_loss else round(pos.buy_price * 0.92, 2)
        trail_stop = (
            compute_trailing_stop(pos.buy_price, highest, base_stop)
            if use_trailing else base_stop
        )

        if pos.stop_loss and price <= pos.stop_loss:
            checks.append(('stop_loss', '🚨 止损触发预警', '≤', pos.stop_loss))
        elif use_trailing and trail_stop > base_stop and price <= trail_stop:
            checks.append(('trailing_stop', '🔒 移动保本止盈触发', '≤', trail_stop))

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
    """把组合总市值 upsert 到今天的快照（净值曲线数据源），返回总市值（跨币种混持自动走Setting汇率折算）"""
    rows = _holding_quotes()
    markets = {pos.market or detect_market(pos.stock_code) for pos, _ in rows}
    has_mixed = ('A' in markets and 'US' in markets)
    fx_rate = get_usd_cny_rate() if has_mixed else 1.0

    total = 0.0
    for pos, price in rows:
        mkt = pos.market or detect_market(pos.stock_code)
        scale = (1.0 / fx_rate) if (has_mixed and mkt == 'A') else 1.0
        total += float(pos.quantity or 0) * float(price or 0) * scale

    today = today_beijing().isoformat()
    db = SessionLocal()
    try:
        snap = db.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.snap_date == today).first()
        if snap:
            snap.total_value = round(total, 2)
        else:
            db.add(PortfolioSnapshot(snap_date=today, total_value=round(total, 2),
                                     created_at=now_beijing()))
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
    today = today_beijing().isoformat()
    triggered = check_position_alerts(report_svc, today)
    total = update_portfolio_snapshot()
    dd = check_drawdown_alert(report_svc, today)
    if triggered:
        logger.info(f"价格监控触发告警: {[(t['code'], t['type']) for t in triggered]}")
    return {'triggered': triggered, 'total_value': total, 'drawdown_alert': dd}
