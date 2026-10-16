"""
技术面评分（共享纯函数）
analysis_service、report_service 与根目录日报脚本共用同一套规则，避免多份实现漂移。
规则：基线5分；
RSI<30 +2 / <40 +1（仅非空头排列时启用——空头排列下的超卖更可能是下跌中继）/ >70 -2 / >60 -1；
MACD金叉+1/死叉-1；站上MA20/MA50各+0.5、跌破各-0.5、贴线0；多头排列+0.5/空头排列-0.5/粘合0；
破布林下轨+1（仅非空头排列时启用）/破上轨-1；
量价配合（当传入 Volume_Ratio 时启用）：多头温和放量(+0.5) / 缩量回踩支撑(+0.5) / 破位放量杀跌(-0.5)；
结果夹在0-10。
"""
from typing import Dict, Tuple

import pandas as pd


def calculate_tech_score(latest: pd.Series) -> Tuple[float, Dict]:
    """对最新一根K线打技术分，返回 (0-10分, 明细dict)"""
    score = 5.0
    details = {}

    current_price = latest.get('Close', 0)
    ma20 = latest.get('MA20', float('nan'))
    ma50 = latest.get('MA50', float('nan'))

    # 空头排列判定：MA20<MA50 时超卖加分（RSI低/破下轨）不启用
    bearish_alignment = pd.notna(ma20) and pd.notna(ma50) and ma20 < ma50

    rsi = latest.get('RSI', 50)
    if pd.notna(rsi):
        details['rsi'] = round(rsi, 2)
        if rsi < 30:
            if not bearish_alignment:
                score += 2.0
        elif rsi < 40:
            if not bearish_alignment:
                score += 1.0
        elif rsi > 70:
            score -= 2.0
        elif rsi > 60:
            score -= 1.0

    macd = latest.get('MACD', 0)
    macd_signal = latest.get('MACD_Signal', 0)
    if pd.notna(macd) and pd.notna(macd_signal):
        details['macd'] = round(macd, 4)
        details['macd_signal'] = round(macd_signal, 4)
        if macd > macd_signal:
            score += 1.0
        else:
            score -= 1.0

    if pd.notna(ma20) and pd.notna(ma50):
        details['ma20'] = round(ma20, 2)
        details['ma50'] = round(ma50, 2)
        # 均线对称：站上加0.5 / 跌破扣0.5 / 恰好贴线记中性
        if current_price > ma20:
            score += 0.5
        elif current_price < ma20:
            score -= 0.5
        if current_price > ma50:
            score += 0.5
        elif current_price < ma50:
            score -= 0.5
        if ma20 > ma50:
            score += 0.5
        elif ma20 < ma50:
            score -= 0.5

    bb_upper = latest.get('BB_Upper', current_price * 1.1)
    bb_lower = latest.get('BB_Lower', current_price * 0.9)

    if pd.notna(bb_upper) and pd.notna(bb_lower):
        details['bb_upper'] = round(bb_upper, 2)
        details['bb_lower'] = round(bb_lower, 2)
        if current_price < bb_lower:
            if not bearish_alignment:
                score += 1.0
        elif current_price > bb_upper:
            score -= 1.0

    # 量价配合因子（可选字段：当数据包含 Volume_Ratio 时自动参与微调）
    vol_ratio = latest.get('Volume_Ratio', float('nan'))
    if pd.notna(vol_ratio) and vol_ratio > 0:
        details['volume_ratio'] = round(float(vol_ratio), 2)
        if pd.notna(ma20) and ma20 > 0:
            if current_price > ma20 and 1.2 <= vol_ratio <= 2.8:
                # 站上均线且温和放量上攻
                score += 0.5
            elif 0.98 * ma20 <= current_price <= 1.03 * ma20 and vol_ratio < 0.85 and not bearish_alignment:
                # 多头趋势下缩量回踩20日均线支撑
                score += 0.5
            elif current_price < ma20 and vol_ratio >= 1.6:
                # 跌破均线且放量杀跌
                score -= 0.5

    details['current_price'] = round(current_price, 2)
    score = max(0, min(10, score))

    return score, details
