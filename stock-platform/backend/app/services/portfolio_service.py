"""
持仓与实盘风控领域服务层（Portfolio Domain Service）
承载原 routers/portfolio.py 中的核心计算与状态机逻辑：
- 4级半凯利回退链（L1 实测画像 -> L2 1y回测按天缓存 -> L3 手动传参 -> L4 跳过约束）
- 科学建仓计算器 calculate_recommended_position（2%定额风险 + 半凯利 + 评分分档 + 大盘系数）
- 持仓实时富化 _enrich_holding（+10%浮盈保本移动止损 + -5%/-10%/-15%三档阶梯补仓状态机）
- 组合汇总与仓位集中度预警 compute_portfolio_summary
- 存量重复 holding 持仓行软归档合并 consolidate_duplicate_holdings
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import Position, Setting
from ..schemas import PositionResponse
from ..services.alert_service import (
    get_risk_switches,
    get_usd_cny_rate,
)
from ..services.backtest_service import BacktestService
from ..utils.market import detect_market
from ..utils.price_levels import STOP_PCT, compute_trailing_stop
from ..utils.timezone import now_beijing, to_naive_beijing, today_beijing
from .position_manager import PositionManager as UnifiedPositionManager

# 定位仓库根目录（用于加载外部可选数据文件 data/stock_profiles.json）
_REPO_ROOT = Path(__file__).resolve().parents[4]

# 风控规则（来自CLAUDE.md）
MAX_SINGLE_POSITION_PCT = 40.0   # 单只股票最大仓位40%
CASH_MIN_PCT = 15.0              # 现金保留下限15%
CASH_MAX_PCT = 25.0              # 现金保留上限25%
DEFAULT_RISK_PER_TRADE = 0.02    # 单笔最大风险占总资金2%（position_manager.py）
MAX_ADD_TRANCHES = 3             # 阶梯补仓硬上限：最多3次（-5% / -10% / -15%）
ADD_TRANCHE_THRESHOLDS = (5.0, 10.0, 15.0)

# 理论挂单名义盈亏比 vs 历史回测实测盈亏比
THEORETICAL_TARGET_STOP_RR: Dict[str, float] = {
    'linear': 1.875,     # 理论挂单名义比：+15% 止盈 / -8% 止损 = 1.875（非实测值）
    'nonlinear': 5.75,   # 理论挂单名义比：+46% 止盈 / -8% 止损 = 5.75（非实测值）
}

# 核心标的 2 年步进回测实测胜率与实测盈亏比（来自 verify_nonlinear_winrate.py 实测结果）
EMPIRICAL_STOCK_PROFILES: Dict[str, Dict[str, float]] = {
    'MU': {'win_rate': 52.0, 'reward_risk_ratio': 4.70},
    'COHR': {'win_rate': 54.3, 'reward_risk_ratio': 1.89},
    'LITE': {'win_rate': 55.6, 'reward_risk_ratio': 1.85},
    'AXTI': {'win_rate': 50.0, 'reward_risk_ratio': 1.80},
    'AAOI': {'win_rate': 48.0, 'reward_risk_ratio': 2.07},
    'SNDK': {'win_rate': 54.5, 'reward_risk_ratio': 1.80},
    'NVDA': {'win_rate': 41.7, 'reward_risk_ratio': 2.44},
    'AVGO': {'win_rate': 60.0, 'reward_risk_ratio': 1.95},
    'SOXL': {'win_rate': 46.0, 'reward_risk_ratio': 1.55},
    # NKE 2年实测胜率 28.6%、累计 -21.3%，属典型负期望标的（Kelly <= 0）
    'NKE': {'win_rate': 28.6, 'reward_risk_ratio': 1.10},
}

# L2 Fallback 1y回测按天内存缓存：key = (stock_code, strategy, 'YYYY-MM-DD') -> {'win_rate', 'rr', 'rr_source'}
_L2_BACKTEST_KELLY_CACHE: Dict[Tuple[str, str, str], Optional[Dict[str, object]]] = {}


def clear_l2_kelly_cache():
    """清空 L2 1y回测按天缓存（供单测或手动刷新调用）"""
    _L2_BACKTEST_KELLY_CACHE.clear()


def _load_profile_kelly_stats(stock_code: str) -> Optional[Tuple[float, float]]:
    """
    L1: 从 data/stock_profiles.json（优先）或内置 2 年实测画像表读取 (win_rate, reward_risk_ratio)。
    支持结构化数值字段与 stat 文本正则解析。
    """
    code = (stock_code or "").strip().upper()
    if not code:
        return None

    profile_path = _REPO_ROOT / "data" / "stock_profiles.json"
    if profile_path.exists():
        try:
            raw = json.loads(profile_path.read_text(encoding="utf-8"))
            entry = raw.get(code) if isinstance(raw, dict) else None
            if isinstance(entry, dict):
                if entry.get("win_rate") is not None and entry.get("reward_risk_ratio") is not None:
                    return float(entry["win_rate"]), float(entry["reward_risk_ratio"])
                stat_str = str(entry.get("stat") or "")
                m_wr = re.search(r"胜率\s*(\d+(?:\.\d+)?)%", stat_str)
                m_rr = re.search(r"盈亏比\s*(\d+(?:\.\d+)?)\s*:\s*1", stat_str)
                if m_wr and m_rr:
                    return float(m_wr.group(1)), float(m_rr.group(1))
        except Exception:
            pass

    if code in EMPIRICAL_STOCK_PROFILES:
        item = EMPIRICAL_STOCK_PROFILES[code]
        return float(item["win_rate"]), float(item["reward_risk_ratio"])
    return None


def _run_l2_cached_backtest_kelly(stock_code: str, strategy: str = "linear") -> Optional[Tuple[float, float, str]]:
    """
    L2: 当 stock_profiles.json 未命中时，运行 1y 回测提取实测胜率与实测分笔盈亏比，并按北京日期缓存一天。
    要求至少成交 3 笔（trade_count >= 3）方视为有效统计。
    """
    code = (stock_code or "").strip().upper()
    strat = strategy if strategy in THEORETICAL_TARGET_STOP_RR else "linear"
    if not code:
        return None

    today_str = today_beijing().isoformat()
    stale_keys = [k for k in _L2_BACKTEST_KELLY_CACHE if k[2] != today_str]
    for k in stale_keys:
        _L2_BACKTEST_KELLY_CACHE.pop(k, None)

    cache_key = (code, strat, today_str)
    if cache_key in _L2_BACKTEST_KELLY_CACHE:
        cached = _L2_BACKTEST_KELLY_CACHE[cache_key]
        if cached is None:
            return None
        return float(cached["win_rate"]), float(cached["rr"]), str(cached["rr_source"])

    try:
        bt_svc = BacktestService()
        market = detect_market(code)
        res = bt_svc.run_backtest(code, strat, market=market, period="1y")
        if not res or "error" in res or int(res.get("trade_count", 0)) < 3:
            _L2_BACKTEST_KELLY_CACHE[cache_key] = None
            return None

        win_rate = float(res["win_rate"])
        sell_trades = [t for t in res.get("trades", []) if t.get("action") == "sell" and "profit_pct" in t]
        wins = [float(t["profit_pct"]) for t in sell_trades if float(t["profit_pct"]) > 0]
        losses = [abs(float(t["profit_pct"])) for t in sell_trades if float(t["profit_pct"]) < 0]

        if wins and losses and sum(losses) > 0:
            avg_win = sum(wins) / len(wins)
            avg_loss = sum(losses) / len(losses)
            rr = round(avg_win / avg_loss, 3)
            rr_source = "empirical_backtest_1y"
        else:
            rr = THEORETICAL_TARGET_STOP_RR[strat]
            rr_source = "theoretical_target_stop_ratio"

        _L2_BACKTEST_KELLY_CACHE[cache_key] = {
            "win_rate": win_rate,
            "rr": rr,
            "rr_source": rr_source,
        }
        return win_rate, rr, rr_source
    except Exception:
        _L2_BACKTEST_KELLY_CACHE[cache_key] = None
        return None


def resolve_kelly_inputs(
    stock_code: Optional[str] = None,
    strategy: str = "linear",
    manual_win_rate: Optional[float] = None,
    manual_rr: Optional[float] = None,
) -> Dict[str, object]:
    """
    严格按 4 级 Fallback 链解析半凯利所需的 (win_rate, reward_risk_ratio)：
    1. L1 (empirical_profile): data/stock_profiles.json 及内置 2 年实测画像
    2. L2 (empirical_backtest_1y): 按天缓存的 1y 实盘回测（trade_count >= 3）
    3. L3 (manual): 调用方显式传入的 win_rate / reward_risk_ratio
    4. L4 (skipped): 前三级均无有效数据时跳过 Kelly 约束（绝不抛错、绝不算成 0 股）
    """
    strat = strategy if strategy in THEORETICAL_TARGET_STOP_RR else "linear"
    default_theo_rr = THEORETICAL_TARGET_STOP_RR[strat]

    if stock_code:
        prof = _load_profile_kelly_stats(stock_code)
        if prof is not None:
            return {
                "win_rate": prof[0],
                "reward_risk_ratio": prof[1],
                "kelly_source": "empirical_profile",
                "rr_source": "empirical_profile",
            }
        bt_stats = _run_l2_cached_backtest_kelly(stock_code, strat)
        if bt_stats is not None:
            return {
                "win_rate": bt_stats[0],
                "reward_risk_ratio": bt_stats[1],
                "kelly_source": "empirical_backtest_1y",
                "rr_source": bt_stats[2],
            }

    if manual_win_rate is not None and float(manual_win_rate) > 0:
        eff_rr = float(manual_rr) if (manual_rr is not None and float(manual_rr) > 0) else default_theo_rr
        rr_src = (
            "manual"
            if (manual_rr is not None and abs(float(manual_rr) - default_theo_rr) > 1e-6)
            else "theoretical_target_stop_ratio"
        )
        return {
            "win_rate": float(manual_win_rate),
            "reward_risk_ratio": eff_rr,
            "kelly_source": "manual",
            "rr_source": rr_src,
        }

    return {
        "win_rate": None,
        "reward_risk_ratio": default_theo_rr,
        "kelly_source": "skipped",
        "rr_source": "theoretical_target_stop_ratio",
    }


def _to_naive_dt(dt) -> Optional[datetime]:
    """将 datetime / date 统一转换为无时区 naive 北京时间 (Asia/Shanghai)"""
    return to_naive_beijing(dt)


def calculate_recommended_position(
    capital: float,
    buy_price: float,
    stop_loss: Optional[float] = None,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
    max_position_pct: float = MAX_SINGLE_POSITION_PCT / 100.0,
    win_rate: Optional[float] = None,
    reward_risk_ratio: float = THEORETICAL_TARGET_STOP_RR['linear'],
    stock_code: Optional[str] = None,
    strategy: str = "linear",
    total_score: Optional[float] = None,
    sizing_mode: str = "fixed_2pct",
    regime_scale: float = 1.0,
    regime_status: str = "NORMAL",
) -> Dict:
    """
    科学建仓计算器（打通 position_manager.py 半凯利与评分分档）：
    - 当 sizing_mode == 'fixed_2pct'：按 2% 定额风险与 40% 单票上限计算
    - 当 sizing_mode == 'half_kelly_combined'：
      取 min(2%定额风险股数, 半凯利股数[若有效], 评分分档股数[若提供]) × regime_scale
    """
    eff_capital = float(capital) if capital and capital > 0 else 100000.0
    if not buy_price or buy_price <= 0:
        return {
            'capital': round(eff_capital, 2),
            'buy_price': 0.0,
            'stop_loss': 0.0,
            'risk_per_trade_pct': round(risk_per_trade * 100, 2),
            'max_loss_amount': 0.0,
            'recommended_shares': 0,
            'risk_2pct_shares': 0,
            'half_kelly_shares': None,
            'score_tier_shares': None,
            'position_value': 0.0,
            'position_pct': 0.0,
            'first_tranche_shares': 0,
            'add_price_1': 0.0,
            'add_price_2': 0.0,
            'add_price_3': 0.0,
            'capped_by_40pct': False,
            'half_kelly_pct': None,
            'kelly_source': 'skipped',
            'rr_source': 'theoretical_target_stop_ratio',
            'kelly_warning': None,
            'sizing_mode': sizing_mode,
            'regime_status': regime_status,
            'regime_scale': regime_scale,
        }

    eff_stop = (
        float(stop_loss)
        if (stop_loss is not None and 0 < stop_loss < buy_price)
        else round(buy_price * STOP_PCT, 2)
    )
    risk_amount = eff_capital * risk_per_trade
    per_share_risk = abs(buy_price - eff_stop)
    risk_shares = int(risk_amount // per_share_risk) if per_share_risk > 0 else 0

    max_pos_value = eff_capital * max_position_pct
    max_shares_by_cap = int(max_pos_value // buy_price)
    capped = risk_shares > max_shares_by_cap
    base_2pct_shares = max(0, min(risk_shares, max_shares_by_cap))

    kelly_info = resolve_kelly_inputs(
        stock_code=stock_code,
        strategy=strategy,
        manual_win_rate=win_rate,
        manual_rr=reward_risk_ratio,
    )
    eff_wr = kelly_info["win_rate"]
    eff_rr = float(kelly_info["reward_risk_ratio"] or THEORETICAL_TARGET_STOP_RR['linear'])
    kelly_source = str(kelly_info["kelly_source"])
    rr_source = str(kelly_info["rr_source"])

    half_kelly_pct: Optional[float] = None
    half_kelly_shares: Optional[int] = None
    kelly_warning: Optional[str] = None
    negative_kelly = False

    if eff_wr is not None and eff_rr > 0:
        w = float(eff_wr) / 100.0 if float(eff_wr) > 1.0 else float(eff_wr)
        w = max(0.0, min(1.0, w))
        raw_kelly = (w * eff_rr - (1.0 - w)) / eff_rr
        if raw_kelly <= 0:
            negative_kelly = True
            half_kelly_pct = 0.0
            half_kelly_shares = 0
            kelly_warning = "历史实测为负期望(Kelly≤0)，建议放弃建仓"
        else:
            half_k_ratio = min(max_position_pct, raw_kelly * 0.5)
            half_kelly_pct = round(half_k_ratio * 100, 1)
            kelly_pm = UnifiedPositionManager(total_capital=eff_capital)
            raw_k_shares = kelly_pm.calculate_position_size_kelly(
                max_position_size=max_position_pct,
                win_rate=w,
                avg_win=eff_rr,
                avg_loss=1.0,
                capital=eff_capital,
                price=buy_price,
                kelly_fraction=0.5,
            )
            half_kelly_shares = min(raw_k_shares, max_shares_by_cap)

    score_tier_shares: Optional[int] = None
    if total_score is not None:
        tier_pm = UnifiedPositionManager(total_capital=eff_capital)
        tier_pm.reserve_ratio = 0.0
        tier_value = tier_pm.calculate_position_size(float(total_score), strategy_type=strategy)
        score_tier_shares = min(int(tier_value // buy_price), max_shares_by_cap)

    eff_scale = max(0.0, min(1.0, float(regime_scale)))
    if sizing_mode == "half_kelly_combined":
        if negative_kelly:
            shares = 0
        else:
            candidates = [base_2pct_shares]
            if half_kelly_shares is not None:
                candidates.append(half_kelly_shares)
            if score_tier_shares is not None:
                candidates.append(score_tier_shares)
            shares = int(min(candidates) * eff_scale)
    else:
        shares = int(base_2pct_shares * eff_scale)

    pos_value = round(shares * buy_price, 2)
    pos_pct = round(pos_value / eff_capital * 100, 2) if eff_capital > 0 else 0.0

    return {
        'capital': round(eff_capital, 2),
        'buy_price': round(buy_price, 2),
        'stop_loss': round(eff_stop, 2),
        'risk_per_trade_pct': round(risk_per_trade * 100, 2),
        'max_loss_amount': round(shares * per_share_risk, 2),
        'recommended_shares': shares,
        'risk_2pct_shares': base_2pct_shares,
        'half_kelly_shares': half_kelly_shares,
        'score_tier_shares': score_tier_shares,
        'position_value': pos_value,
        'position_pct': pos_pct,
        'first_tranche_shares': max(0, shares // 2),
        'add_price_1': round(buy_price * 0.95, 2),
        'add_price_2': round(buy_price * 0.90, 2),
        'add_price_3': round(buy_price * 0.85, 2),
        'capped_by_40pct': capped,
        'half_kelly_pct': half_kelly_pct,
        'win_rate_used': round(float(eff_wr), 2) if eff_wr is not None else None,
        'reward_risk_ratio_used': round(eff_rr, 3),
        'kelly_source': kelly_source,
        'rr_source': rr_source,
        'kelly_warning': kelly_warning,
        'sizing_mode': sizing_mode,
        'regime_status': regime_status,
        'regime_scale': eff_scale,
    }


def _get_total_capital(db: Session) -> float:
    """读取总资金设置，未设置为0"""
    row = db.query(Setting).filter(Setting.key == "total_capital").first()
    try:
        return float(row.value) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def _get_usd_cny_rate(db: Session) -> float:
    """读取美元兑人民币汇率设置（复用 alert_service.get_usd_cny_rate 唯一单点）"""
    return get_usd_cny_rate(db)


def _realized_pnl(sell_price: float, buy_price: float, quantity: float) -> Tuple[float, float]:
    """已实现盈亏与百分比（卖出接口与单测共用同一实现）"""
    pnl = (sell_price - buy_price) * quantity
    pct = (sell_price - buy_price) / buy_price * 100 if buy_price else 0
    return round(pnl, 2), round(pct, 2)


def _compute_holding_add_signal(
    pos: Position,
    current_price: float,
    total_capital: float = 15000.0,
) -> Dict:
    """
    调用根目录 position_manager.py 的三档阶梯补仓状态机（-5% / -10% / -15%）：
    永久锚定首次建仓价 initial_price，每档仅触发一次，最多补仓 3 次。
    """
    initial_px = float(pos.initial_price or pos.buy_price)
    add_cnt = int(pos.add_count or 0)
    eff_cap = total_capital if total_capital and total_capital > 0 else max(initial_px * pos.quantity * 3.0, 15000.0)

    pm = UnifiedPositionManager(total_capital=eff_cap)
    pm.reserve_ratio = 0.0
    pm.positions[pos.stock_code] = {
        'shares': pos.quantity,
        'avg_price': pos.buy_price,
        'cost': pos.buy_price * pos.quantity,
        'initial_price': initial_px,
        'initial_cost': initial_px * pos.quantity,
        'add_count': add_cnt,
        'strategy': 'linear',
    }
    should_add, add_amount = pm.should_add_position(
        pos.stock_code, current_price=current_price, buy_price=initial_px, strategy_type='linear'
    )
    next_tier = add_cnt + 1 if add_cnt < MAX_ADD_TRANCHES else None
    next_drop_pct = ADD_TRANCHE_THRESHOLDS[add_cnt] if add_cnt < MAX_ADD_TRANCHES else None
    next_trigger_px = round(initial_px * (1.0 - next_drop_pct / 100.0), 2) if next_drop_pct else None
    drop_from_initial_pct = round((initial_px - current_price) / initial_px * 100.0, 2) if initial_px > 0 else 0.0
    suggested_shares = int(add_amount // current_price) if (should_add and current_price > 0) else 0

    return {
        'should_add': bool(should_add and suggested_shares > 0),
        'add_count': add_cnt,
        'max_adds': MAX_ADD_TRANCHES,
        'initial_price': round(initial_px, 4),
        'drop_from_initial_pct': drop_from_initial_pct,
        'next_tier': next_tier,
        'next_tier_drop_pct': next_drop_pct,
        'next_trigger_price': next_trigger_px,
        'suggested_add_amount': round(float(add_amount), 2),
        'suggested_add_shares': suggested_shares,
    }


def _enrich_holding(
    pos: Position,
    quote,
    switches: Optional[Dict[str, object]] = None,
    total_capital: float = 0.0,
) -> PositionResponse:
    current_price = (quote or {}).get('price') or pos.buy_price
    profit_loss = (current_price - pos.buy_price) * pos.quantity
    profit_loss_pct = (current_price - pos.buy_price) / pos.buy_price * 100 if pos.buy_price else 0

    buy_dt = _to_naive_dt(pos.buy_date)
    days_held = max((now_beijing() - buy_dt).days, 0) if buy_dt else 0

    sw = switches if switches is not None else get_risk_switches()
    use_trailing = bool(sw.get('trailing_stop_enabled', True))
    use_staged_add = bool(sw.get('staged_add_enabled', True))

    initial_px = float(pos.initial_price) if pos.initial_price is not None else float(pos.buy_price)
    add_cnt = int(pos.add_count) if pos.add_count is not None else 0
    highest_px = max(float(pos.highest_price or pos.buy_price), float(current_price))
    base_stop = float(pos.stop_loss) if pos.stop_loss is not None else round(pos.buy_price * STOP_PCT, 2)

    trailing_stop_px = (
        compute_trailing_stop(pos.buy_price, highest_px, base_stop)
        if use_trailing else None
    )
    effective_stop = max(base_stop, trailing_stop_px) if trailing_stop_px is not None else pos.stop_loss
    add_signal = (
        _compute_holding_add_signal(pos, float(current_price), total_capital=total_capital)
        if use_staged_add else None
    )

    return PositionResponse(
        id=pos.id,
        stock_code=pos.stock_code,
        stock_name=pos.stock_name,
        market=pos.market,
        buy_price=pos.buy_price,
        initial_price=round(initial_px, 4),
        add_count=add_cnt,
        highest_price=round(highest_px, 4),
        quantity=pos.quantity,
        buy_date=buy_dt or now_beijing(),
        stop_loss=pos.stop_loss,
        take_profit=pos.take_profit,
        trailing_stop_price=trailing_stop_px,
        effective_stop_loss=effective_stop,
        add_position_signal=add_signal,
        status=pos.status,
        created_at=_to_naive_dt(pos.created_at) or now_beijing(),
        current_price=round(current_price, 2),
        profit_loss=round(profit_loss, 2),
        profit_loss_pct=round(profit_loss_pct, 2),
        holding_days=days_held,
    )


def compute_portfolio_summary(
    db: Session,
    fetch_quotes_fn: Callable[[List[Position]], list],
) -> Dict[str, object]:
    """持仓汇总 + 仓位集中度检查（当同时持有A股与美股时，自动将A股人民币市值按Setting汇率折算为美元汇总）"""
    positions = db.query(Position).filter(Position.status == "holding").all()

    markets_held = {p.market or detect_market(p.stock_code) for p in positions}
    has_mixed_currencies = ('A' in markets_held and 'US' in markets_held)
    fx_rate = _get_usd_cny_rate(db)

    total_value = 0.0
    total_cost = 0.0
    per_stock_value = {}

    quotes = fetch_quotes_fn(positions)
    for pos, quote in zip(positions, quotes):
        mkt = pos.market or detect_market(pos.stock_code)
        scale = (1.0 / fx_rate) if (has_mixed_currencies and mkt == 'A') else 1.0
        current_price = (quote or {}).get('price') or pos.buy_price
        value = current_price * pos.quantity * scale
        cost = pos.buy_price * pos.quantity * scale
        total_value += value
        total_cost += cost
        per_stock_value[pos.stock_code] = per_stock_value.get(pos.stock_code, 0) + value

    total_profit = total_value - total_cost
    total_profit_pct = (total_profit / total_cost * 100) if total_cost > 0 else 0

    total_capital = _get_total_capital(db)

    warnings = []
    base = total_capital if total_capital > 0 else total_value
    base_label = '总资金' if total_capital > 0 else '持仓总市值'
    for code, value in per_stock_value.items():
        weight = value / base * 100 if base > 0 else 0
        name = next((p.stock_name for p in positions if p.stock_code == code), code)
        if weight > MAX_SINGLE_POSITION_PCT:
            warnings.append({
                'level': 'error',
                'code': code,
                'name': name,
                'weight': round(weight, 1),
                'message': f"{name}({code})占{base_label} {weight:.1f}%，超过单票{MAX_SINGLE_POSITION_PCT}%上限，建议减仓"
            })

    cash_pct = (total_capital - total_value) / total_capital * 100 if total_capital > 0 else None
    if cash_pct is not None:
        if cash_pct < CASH_MIN_PCT:
            warnings.append({
                'level': 'warning',
                'code': '',
                'name': '',
                'weight': round(cash_pct, 1),
                'message': f"现金比例仅{cash_pct:.1f}%，低于{CASH_MIN_PCT:.0f}%下限，注意保留子弹"
            })
        elif cash_pct > CASH_MAX_PCT:
            warnings.append({
                'level': 'info',
                'code': '',
                'name': '',
                'weight': round(cash_pct, 1),
                'message': f"现金比例{cash_pct:.1f}%，高于{CASH_MAX_PCT:.0f}%上限，资金利用率偏低"
            })

    currency = "CNY" if markets_held == {"A"} else "USD"
    return {
        "total_positions": len(positions),
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_profit": round(total_profit, 2),
        "total_profit_pct": round(total_profit_pct, 2),
        "total_capital": total_capital,
        "cash_pct": round(cash_pct, 1) if cash_pct is not None else None,
        "usd_cny_rate": round(fx_rate, 4),
        "currency": currency,
        "warnings": warnings,
    }


def consolidate_duplicate_holdings(db: Session) -> Dict[str, object]:
    """
    排查并合并存量数据库中的重复 holding 持仓行（按 stock_code 分组，HAVING COUNT(*) > 1）：
    - 以最早买入的一行（按 buy_date ASC, id ASC）作为保留主行
    - 其余重复行改为软归档 status = 'merged'（原样保留分笔买入日期、价格、股数作为税基与复盘依据，绝不硬 DELETE）
    """
    holdings = db.query(Position).filter(Position.status == "holding").all()
    by_code: Dict[str, List[Position]] = {}
    for pos in holdings:
        by_code.setdefault(pos.stock_code, []).append(pos)

    merged_details = []
    for code, rows in by_code.items():
        if len(rows) <= 1:
            continue
        rows.sort(key=lambda r: (_to_naive_dt(r.buy_date) or datetime.min, r.id or 0))
        primary = rows[0]
        dups = rows[1:]

        initial_px = float(primary.initial_price) if primary.initial_price is not None else float(primary.buy_price)
        total_qty = sum(int(r.quantity or 0) for r in rows)
        total_cost = sum(int(r.quantity or 0) * float(r.buy_price or 0.0) for r in rows)
        new_avg = round(total_cost / total_qty, 4) if total_qty > 0 else float(primary.buy_price)
        new_add_cnt = min(MAX_ADD_TRANCHES, int(primary.add_count or 0) + len(dups))
        max_high = max(float(r.highest_price or r.buy_price or 0.0) for r in rows)
        new_stop = round(new_avg * STOP_PCT, 2)

        primary.initial_price = initial_px
        primary.quantity = total_qty
        primary.buy_price = new_avg
        primary.add_count = new_add_cnt
        primary.highest_price = max_high
        primary.stop_loss = new_stop

        merged_ids = []
        for d in dups:
            merged_ids.append(d.id)
            d.status = "merged"

        merged_details.append({
            "stock_code": code,
            "kept_id": primary.id,
            "merged_ids": merged_ids,
            "initial_price": round(initial_px, 4),
            "new_buy_price": new_avg,
            "new_quantity": total_qty,
            "add_count": new_add_cnt,
            "new_stop_loss": new_stop,
        })

    if merged_details:
        db.commit()
    return {
        "duplicate_codes_found": len(merged_details),
        "merged": merged_details,
    }
