"""
三策略点位计算与ATR自适应动态风控——全项目唯一定义处。

此前 analysis_service 与 report_service 各自实现线性/非线性买点公式与
MACD 状态天数统计，仅靠人肉保持一致；任一侧调参另一侧必然悄悄落后。
本模块只放纯函数，不依赖服务与数据库，便于单测。
"""
import math
from typing import Dict, List, Optional, Tuple

# 纪律参数（原两处实现的共同常量；改动需同步前端写死的提示文案）
STOP_PCT = 0.92               # 纪律止损：-8%
LINEAR_PROFIT_PCT = 1.15      # 线性止盈：+15%
NONLINEAR_PROFIT_PCT = 1.46   # 非线性止盈：+46%
RSI_OVERSOLD = 30.0           # 非线性策略超卖阈值


def linear_buy_point(current: float, ma20: float) -> float:
    """线性策略（斐波那契回撤）：价格在MA20上方时取回撤50%位，否则回退5%；上限为现价-5%"""
    if ma20 < current:
        buy = current - 0.5 * (current - ma20)
    else:
        buy = current * 0.95
    return min(buy, current * 0.95)


def nonlinear_buy_point(rsi: float, ma20: float, bb_lower: float) -> float:
    """非线性策略：RSI超卖(<30)看布林下轨，否则看20日均线"""
    return bb_lower if rsi < RSI_OVERSOLD else ma20


def levels_with_targets(current: float, buy: float, profit_pct: float) -> Dict:
    """由买点生成 {buy, stop, profit, distance}；distance=现价高出买点的百分比"""
    return {
        'buy': round(buy, 2),
        'stop': round(buy * STOP_PCT, 2),
        'profit': round(buy * profit_pct, 2),
        'distance': round((current - buy) / current * 100, 2) if current else 0.0,
    }


def atr_adaptive_levels(
    current: float,
    buy: float,
    profit_pct: float,
    atr: Optional[float] = None,
    stop_atr_mult: float = 2.0,
    profit_atr_mult: float = 3.5,
) -> Dict:
    """
    结合固定比例与 ATR(14) 真实波动率生成完整点位字典：
    - 保留 buy / stop / profit / distance 四个标准字段（与 levels_with_targets 完全一致）
    - 新增 atr_stop：基于 2.0×ATR 的波动率自适应止损位（限制在买入价的 -5% ~ -14% 之间，防止低波股止损过窄、高波股无限抗单）
    - 新增 atr_profit：基于 ATR 倍数与目标止盈比例的自适应止盈位
    - 新增 breakeven_trigger：浮盈达 +10% 时的移动保本触发价（触及后建议将止损上移至成本价+1%即 breakeven_stop）
    """
    base = levels_with_targets(current, buy, profit_pct)
    if buy <= 0:
        return {
            **base,
            'atr_stop': None,
            'atr_profit': None,
            'breakeven_trigger': None,
            'breakeven_stop': None,
        }

    has_valid_atr = (
        atr is not None
        and isinstance(atr, (int, float))
        and not math.isnan(float(atr))
        and float(atr) > 0
    )
    if has_valid_atr:
        raw_atr_stop = buy - stop_atr_mult * float(atr)
        # 夹在 [-14%, -5%] 风险走廊内
        clamped_stop = min(max(raw_atr_stop, buy * 0.86), buy * 0.95)
        atr_stop = round(clamped_stop, 2)
        atr_profit = round(max(buy * profit_pct, buy + profit_atr_mult * float(atr)), 2)
    else:
        atr_stop = base['stop']
        atr_profit = base['profit']

    return {
        **base,
        'atr_stop': atr_stop,
        'atr_profit': atr_profit,
        'breakeven_trigger': round(buy * 1.10, 2),
        'breakeven_stop': round(buy * 1.01, 2),
    }


def compute_trailing_stop(
    entry_price: float,
    highest_price: float,
    initial_stop: float,
    breakeven_gain: float = 0.10,
    trailing_drawdown: float = 0.08,
) -> float:
    """
    移动止盈/保本止损计算纯函数：
    - 当最高价尚未达到 entry_price * (1 + breakeven_gain) 时，维持 initial_stop
    - 一旦最高价突破保本触发线（默认浮盈 >= +10%），止损线自动上移至 max(成本价+1%, 最高价回落8%)
    """
    if entry_price <= 0:
        return initial_stop
    if highest_price >= entry_price * (1.0 + breakeven_gain) - 1e-9:
        be_stop = entry_price * 1.01
        trail_stop = highest_price * (1.0 - trailing_drawdown)
        return round(max(initial_stop, be_stop, trail_stop), 2)
    return round(initial_stop, 2)


def macd_state_days(above: List[bool]) -> Tuple[Optional[bool], int]:
    """MACD金叉状态与已持续天数：从最新一根向前数同状态连续根数。
    above: bool列表（MACD>Signal），按时间升序；空列表返回 (None, 0)"""
    if not above:
        return None, 0
    is_golden = bool(above[-1])
    days = 0
    for v in reversed(above):
        if bool(v) == is_golden:
            days += 1
        else:
            break
    return is_golden, days


def discipline_stop(current: float) -> float:
    """MACD信号型策略的纪律止损（-8%）"""
    return round(current * STOP_PCT, 2)
