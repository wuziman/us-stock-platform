# -*- coding: utf-8 -*-
"""
技术信号服务：K线形态识别（含量能放大确认） / 自动支撑阻力位 / MACD与RSI背离共振检测
"""
from typing import List, Dict

import numpy as np
import pandas as pd

from ..utils.indicators import calculate_macd


def _wilder_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder指数平滑RSI（避免简单14日SMA在连续小阳线时饱和为100，专用于摆动极值背离检测）"""
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.where(loss != 0, np.where(gain > 0, 100.0, 50.0))


def _trend_context(closes: pd.Series, i: int, window: int = 5) -> str:
    """
    以第i根K线的前一日收盘与其前window日均值比较，判断趋势上下文
    返回: 'up' / 'down' / 'neutral'
    """
    if i < window:
        return 'neutral'
    prev_close = closes.iloc[i - 1]
    ma = closes.iloc[i - window:i].mean()
    if prev_close < ma:
        return 'down'
    if prev_close > ma:
        return 'up'
    return 'neutral'


def _date_str(value) -> str:
    """索引转YYYY-MM-DD字符串"""
    return value.strftime('%Y-%m-%d') if hasattr(value, 'strftime') else str(value)


def detect_candlestick_patterns(df: pd.DataFrame, recent_n: int = 30) -> List[Dict]:
    """
    识别最近recent_n根K线的常见形态（当当日成交量≥前5日均量1.5倍时自动附加放量确认标记）

    识别形态：锤子线/上吊线、看涨吞没/看跌吞没、十字星、
             启明星/黄昏星、乌云盖顶、大阳线/大阴线
    同一根K线命中多个形态时全部记录

    返回: [{date, pattern, direction}]，direction: 'bullish'/'bearish'/'neutral'
    """
    results: List[Dict] = []
    n = len(df)
    if n < 3:
        return results

    o = df['Open'].astype(float)
    h = df['High'].astype(float)
    l = df['Low'].astype(float)
    c = df['Close'].astype(float)
    v = df['Volume'].astype(float) if 'Volume' in df.columns else None

    # 从第2根开始（三根形态需要前两根做上下文）
    start = max(2, n - recent_n)

    for i in range(start, n):
        found: List[tuple] = []

        op, hi, lo, cl = o.iloc[i], h.iloc[i], l.iloc[i], c.iloc[i]
        body = abs(cl - op)          # 实体
        rng = hi - lo                # 全幅
        upper_shadow = hi - max(op, cl)   # 上影线
        lower_shadow = min(op, cl) - lo   # 下影线
        trend = _trend_context(c, i)

        # 计算当日相对前5日均量的放量倍数（恒定成交量如测试集中为1.0，不触发放量后缀）
        vol_suffix = ''
        if v is not None and i >= 5:
            avg_v5 = float(v.iloc[i - 5:i].mean())
            cur_v = float(v.iloc[i])
            if avg_v5 > 0 and cur_v >= avg_v5 * 1.5:
                vol_suffix = f"(放量{cur_v / avg_v5:.1f}x)"

        # --- 单根形态 ---
        # 十字星：实体 ≤ 全幅10%
        if rng > 0 and body <= rng * 0.1:
            found.append(('十字星', 'neutral'))

        # 锤子线/上吊线：下影≥实体2倍、上影≤实体0.3倍、实体在K线上部
        # 锤子线出现在下跌趋势后（看涨），上吊线出现在上涨趋势后（看跌）
        if body > 0 and lower_shadow >= body * 2 and upper_shadow <= body * 0.3:
            if trend == 'down':
                found.append((f'锤子线{vol_suffix}', 'bullish'))
            elif trend == 'up':
                found.append((f'上吊线{vol_suffix}', 'bearish'))

        # 大阳线/大阴线：实体≥全幅80% 且 涨/跌幅≥3%
        if rng > 0 and body >= rng * 0.8 and op > 0:
            change = (cl - op) / op
            if change >= 0.03:
                found.append((f'大阳线{vol_suffix}', 'bullish'))
            elif change <= -0.03:
                found.append((f'大阴线{vol_suffix}', 'bearish'))

        # --- 两根形态 ---
        if i >= 1:
            po, pc = o.iloc[i - 1], c.iloc[i - 1]
            ph = h.iloc[i - 1]
            # 看涨吞没：前阴后阳，今日实体完全包住前日实体
            if pc < po and cl > op and op <= pc and cl >= po:
                found.append((f'看涨吞没{vol_suffix}', 'bullish'))
            # 看跌吞没：前阳后阴，今日实体完全包住前日实体
            if pc > po and cl < op and op >= pc and cl <= po:
                found.append((f'看跌吞没{vol_suffix}', 'bearish'))
            # 乌云盖顶：上涨趋势中，阴线开盘高于前日最高、收盘深入前日阳线实体50%以下
            if trend == 'up' and pc > po and cl < op and op > ph and cl < (po + pc) / 2:
                found.append((f'乌云盖顶{vol_suffix}', 'bearish'))

        # --- 三根形态 ---
        if i >= 2:
            o1, c1 = o.iloc[i - 2], c.iloc[i - 2]
            o2, c2 = o.iloc[i - 1], c.iloc[i - 1]
            body1 = abs(c1 - o1)
            body2 = abs(c2 - o2)
            mid1 = (o1 + c1) / 2
            # 启明星：下跌趋势中 长阴线 + 小实体星线 + 阳线收复第一根实体中点
            if (c1 < o1 and body1 > 0 and body2 <= body1 * 0.5
                    and cl > op and cl > mid1 and _trend_context(c, i - 2) == 'down'):
                found.append((f'启明星{vol_suffix}', 'bullish'))
            # 黄昏星：上涨趋势中 长阳线 + 小实体星线 + 阴线跌破第一根实体中点
            if (c1 > o1 and body1 > 0 and body2 <= body1 * 0.5
                    and cl < op and cl < mid1 and _trend_context(c, i - 2) == 'up'):
                found.append((f'黄昏星{vol_suffix}', 'bearish'))

        date_str = _date_str(df.index[i])
        for name, direction in found:
            results.append({'date': date_str, 'pattern': name, 'direction': direction})

    return results


def _cluster_levels(points: List[float], cluster_pct: float) -> List[Dict]:
    """
    将候选价按cluster_pct容差聚类成水平位
    每个水平位取组内均值，并记录触及次数
    """
    levels: List[Dict] = []
    if not points:
        return levels
    points = sorted(points)
    group = [points[0]]
    for p in points[1:]:
        mean = sum(group) / len(group)
        # 与当前组均值的相对差距在容差内则归入同组
        if mean > 0 and abs(p - mean) / mean <= cluster_pct:
            group.append(p)
        else:
            levels.append({'price': round(mean, 3), 'touches': len(group)})
            group = [p]
    mean = sum(group) / len(group)
    levels.append({'price': round(mean, 3), 'touches': len(group)})
    return levels


def detect_support_resistance(df: pd.DataFrame, pivot_window: int = 5,
                              cluster_pct: float = 0.02) -> Dict:
    """
    分形法自动检测支撑/阻力位

    算法：
    1. 局部极值点：某根K线的high在其前后pivot_window根中都最高→阻力候选；
       low都最低→支撑候选
    2. 候选价按cluster_pct（默认2%）容差聚类成水平位，取均值并记录触及次数
    3. 低于现价的水平位为支撑（按距离从近到远，最多5个），高于现价为阻力

    返回: {"supports": [{price, touches}], "resistances": [...], "current_price": ...}
    """
    h = df['High'].astype(float).values
    l = df['Low'].astype(float).values
    n = len(df)
    current_price = float(df['Close'].iloc[-1])

    candidates: List[float] = []
    for i in range(pivot_window, n - pivot_window):
        window_h = h[i - pivot_window:i + pivot_window + 1]
        window_l = l[i - pivot_window:i + pivot_window + 1]
        if h[i] >= window_h.max():
            candidates.append(float(h[i]))
        if l[i] <= window_l.min():
            candidates.append(float(l[i]))

    levels = _cluster_levels(candidates, cluster_pct)

    supports = sorted(
        [lv for lv in levels if lv['price'] < current_price],
        key=lambda x: current_price - x['price']
    )[:5]
    resistances = sorted(
        [lv for lv in levels if lv['price'] >= current_price],
        key=lambda x: x['price'] - current_price
    )[:5]

    return {
        'supports': supports,
        'resistances': resistances,
        'current_price': round(current_price, 3),
    }


def detect_rsi_divergence(df: pd.DataFrame, lookback: int = 60,
                          swing_window: int = 3) -> Dict:
    """
    RSI(14) 顶底背离检测（辅助 MACD 背离进行双指标共振验证）
    返回: {"rsi_top_divergence": bool, "rsi_bottom_divergence": bool, "rsi_detail": str}
    """
    n = len(df)
    if n < swing_window * 2 + 15:
        return {'rsi_top_divergence': False, 'rsi_bottom_divergence': False, 'rsi_detail': ''}

    rsi_series = _wilder_rsi(df['Close'].astype(float), 14)
    start = max(0, n - lookback)
    h = df['High'].astype(float).values[start:]
    l = df['Low'].astype(float).values[start:]
    rsi_w = rsi_series.values[start:]
    m = len(h)
    if m < swing_window * 2 + 3:
        return {'rsi_top_divergence': False, 'rsi_bottom_divergence': False, 'rsi_detail': ''}

    swing_highs: List[tuple] = []
    swing_lows: List[tuple] = []
    for i in range(swing_window, m - swing_window):
        if h[i] >= h[i - swing_window:i + swing_window + 1].max():
            swing_highs.append((i, float(h[i])))
        if l[i] <= l[i - swing_window:i + swing_window + 1].min():
            swing_lows.append((i, float(l[i])))

    rsi_top = False
    rsi_bottom = False
    rsi_detail = ''
    if len(swing_highs) >= 2:
        i1, p1 = swing_highs[-2]
        i2, p2 = swing_highs[-1]
        if p2 > p1 and pd.notna(rsi_w[i1]) and pd.notna(rsi_w[i2]) and rsi_w[i2] < rsi_w[i1] - 0.5:
            rsi_top = True
            rsi_detail = f"RSI同步顶背离({rsi_w[i1]:.1f}→{rsi_w[i2]:.1f})"
    if len(swing_lows) >= 2:
        i1, p1 = swing_lows[-2]
        i2, p2 = swing_lows[-1]
        if p2 < p1 and pd.notna(rsi_w[i1]) and pd.notna(rsi_w[i2]) and rsi_w[i2] > rsi_w[i1] + 0.5:
            rsi_bottom = True
            if not rsi_top:
                rsi_detail = f"RSI同步底背离({rsi_w[i1]:.1f}→{rsi_w[i2]:.1f})"

    return {
        'rsi_top_divergence': rsi_top,
        'rsi_bottom_divergence': rsi_bottom,
        'rsi_detail': rsi_detail,
    }


def detect_macd_divergence(df: pd.DataFrame, lookback: int = 60,
                           swing_window: int = 3) -> Dict:
    """
    MACD背离检测（顶背离/底背离，叠加RSI背离共振提示）

    算法：
    1. 在全长数据上计算MACD（保证EMA预热充分），取最近lookback根分析
    2. 找价格的摆动高低点（局部极值，前后swing_window根）
    3. 顶背离：价格创新高（后一高点>前一高点）但对应MACD柱或DIF走低
       底背离：价格创新低但MACD柱或DIF走高
    4. 若同时检出RSI摆动高低点背离，则在detail中追加【MACD+RSI双背离共振】提示

    返回: {"top_divergence": bool, "bottom_divergence": bool,
           "detail": 文字描述, "checked_bars": n}
    """
    n = len(df)
    empty = {'top_divergence': False, 'bottom_divergence': False,
             'detail': '数据不足，未检测到背离', 'checked_bars': n}
    if n < swing_window * 2 + 3:
        return empty

    # MACD在全长数据上计算，再截取窗口
    dif, _, hist = calculate_macd(df['Close'].astype(float))

    start = max(0, n - lookback)
    h = df['High'].astype(float).values[start:]
    l = df['Low'].astype(float).values[start:]
    hist_w = hist.values[start:]
    dif_w = dif.values[start:]
    m = len(h)
    if m < swing_window * 2 + 3:
        return {**empty, 'checked_bars': m}

    # 找摆动高低点（局部极值）
    swing_highs: List[tuple] = []  # [(窗口内下标, 价格)]
    swing_lows: List[tuple] = []
    for i in range(swing_window, m - swing_window):
        if h[i] >= h[i - swing_window:i + swing_window + 1].max():
            swing_highs.append((i, float(h[i])))
        if l[i] <= l[i - swing_window:i + swing_window + 1].min():
            swing_lows.append((i, float(l[i])))

    top = False
    bottom = False
    detail = '未检测到背离'
    rsi_div = detect_rsi_divergence(df, lookback=lookback, swing_window=swing_window)

    # 顶背离：取最近两个摆动高点比较
    if len(swing_highs) >= 2:
        i1, p1 = swing_highs[-2]
        i2, p2 = swing_highs[-1]
        if p2 > p1 and (hist_w[i2] < hist_w[i1] or dif_w[i2] < dif_w[i1]):
            top = True
            resonance = f" 【MACD+RSI双背离共振：{rsi_div['rsi_detail']}】" if rsi_div['rsi_top_divergence'] else ""
            detail = (f'价格高点 {p1:.2f}→{p2:.2f} 创新高，'
                      f'但MACD柱 {hist_w[i1]:.3f}→{hist_w[i2]:.3f} 走低，上涨动能减弱{resonance}')

    # 底背离：取最近两个摆动低点比较
    if len(swing_lows) >= 2:
        i1, p1 = swing_lows[-2]
        i2, p2 = swing_lows[-1]
        if p2 < p1 and (hist_w[i2] > hist_w[i1] or dif_w[i2] > dif_w[i1]):
            bottom = True
            if not top:
                resonance = f" 【MACD+RSI双背离共振：{rsi_div['rsi_detail']}】" if rsi_div['rsi_bottom_divergence'] else ""
                detail = (f'价格低点 {p1:.2f}→{p2:.2f} 创新低，'
                          f'但MACD柱 {hist_w[i1]:.3f}→{hist_w[i2]:.3f} 走高，下跌动能衰减{resonance}')

    return {
        'top_divergence': top,
        'bottom_divergence': bottom,
        'detail': detail,
        'checked_bars': m,
    }
