# -*- coding: utf-8 -*-
"""
技术信号测试：K线形态识别（含放量确认） / 支撑阻力位 / MACD与RSI背离共振
运行：cd backend && python -m pytest tests/ -v
"""
import sys
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.pattern_service import (
    detect_candlestick_patterns,
    detect_support_resistance,
    detect_macd_divergence,
    detect_rsi_divergence,
)


def make_df(rows, start='2026-01-01'):
    """由 [open, high, low, close] 行构造OHLC DataFrame"""
    idx = pd.date_range(start, periods=len(rows), freq='D')
    df = pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close'], index=idx)
    df['Volume'] = 1000
    return df


def bar(o, c, h=None, l=None):
    """单根K线，影线默认取实体外0.1"""
    h = h if h is not None else max(o, c) + 0.1
    l = l if l is not None else min(o, c) - 0.1
    return [o, h, l, c]


def patterns_on(df):
    """返回 {形态名: [日期]} 映射"""
    result = {}
    for p in detect_candlestick_patterns(df):
        result.setdefault(p['pattern'], []).append(p['date'])
    return result


# ============================================
# K线形态识别
# ============================================
class TestCandlestickPatterns:
    def test_hammer_after_downtrend(self):
        """下跌趋势后下影长实体短的单根 → 锤子线"""
        rows = [bar(110, 108), bar(108, 106), bar(106, 104),
                bar(104, 102), bar(102, 100)]
        # 锤子线：开99.5 收100.2 低95（下影4.5≥实体0.7×2）高100.3（上影0.1≤0.21）
        rows.append(bar(99.5, 100.2, h=100.3, l=95.0))
        df = make_df(rows)
        found = patterns_on(df)
        assert '锤子线' in found

    def test_volume_surge_appends_ratio_tag(self):
        """当成交量达到前5日均量1.5倍以上时，方向性形态自动附带放量倍数标记"""
        rows = [bar(110, 108), bar(108, 106), bar(106, 104),
                bar(104, 102), bar(102, 100),
                bar(99.5, 100.2, h=100.3, l=95.0)]
        df = make_df(rows)
        df.iloc[-1, df.columns.get_loc('Volume')] = 2200  # 2.2倍放量
        found = patterns_on(df)
        assert '锤子线(放量2.2x)' in found

    def test_bullish_engulfing(self):
        """前阴后阳且实体包住 → 看涨吞没"""
        rows = [
            bar(103, 102),      # 之前走势
            bar(102, 100),      # 前阴：实体100~102
            bar(99.8, 103),     # 后阳：实体99.8~103 完全包住
        ]
        df = make_df(rows)
        found = patterns_on(df)
        assert '看涨吞没' in found

    def test_bearish_engulfing(self):
        """前阳后阴且实体包住 → 看跌吞没"""
        rows = [
            bar(100, 101),
            bar(101, 103),      # 前阳：实体101~103
            bar(103.2, 100.5),  # 后阴：实体100.5~103.2 完全包住
        ]
        df = make_df(rows)
        found = patterns_on(df)
        assert '看跌吞没' in found

    def test_doji(self):
        """开==收附近且上下影明显 → 十字星"""
        rows = [
            bar(103, 102),
            bar(102, 101),
            bar(100, 100.05, h=103, l=97),  # 实体0.05 ≤ 全幅6×0.1
        ]
        df = make_df(rows)
        found = patterns_on(df)
        assert '十字星' in found

    def test_morning_star(self):
        """下跌后 长阴+小星线+阳线收复中点 → 启明星"""
        rows = [bar(112, 110), bar(110, 108), bar(108, 106),
                bar(106, 104), bar(104, 102)]
        rows.append(bar(102, 100))            # 长阴
        rows.append(bar(99.2, 99.5, h=99.8, l=98.8))  # 小实体星线
        rows.append(bar(99.8, 102))           # 阳线收过第一根实体中点101？→需>101
        df = make_df(rows)
        found = patterns_on(df)
        # 收盘102 > 中点(102+100)/2=101 → 启明星成立
        assert '启明星' in found

    def test_dark_cloud_cover(self):
        """上涨趋势中 阴线开盘高于前日最高、收盘深入前日阳线实体50%以下 → 乌云盖顶"""
        rows = [bar(95, 96), bar(96, 97), bar(97, 98), bar(98, 99), bar(99, 100)]
        rows.append(bar(100, 103))             # 前阳：实体100~103 最高103.1
        rows.append(bar(103.5, 101, h=103.8, l=100.8))  # 阴线开103.5>103.1，收101<中点101.5
        df = make_df(rows)
        found = patterns_on(df)
        assert '乌云盖顶' in found

    def test_big_bullish_candle(self):
        """实体≥全幅80%且涨幅≥3% → 大阳线"""
        rows = [bar(100, 100.2), bar(100.2, 100.4)]
        # 开100 收103.5：实体3.5，低99.9 高103.6 全幅3.7，实体占比94.6%，涨幅3.5%
        rows.append(bar(100, 103.5, h=103.6, l=99.9))
        df = make_df(rows)
        found = patterns_on(df)
        assert '大阳线' in found

    def test_multiple_patterns_on_same_bar(self):
        """同一根K线命中多个形态时都记录"""
        rows = [bar(110, 108), bar(108, 106), bar(106, 104),
                bar(104, 102), bar(102, 100)]
        rows.append(bar(99.9, 99.6))  # 小阴线，保持下跌趋势
        rows.append(bar(99.5, 100.2, h=100.3, l=95.0))
        df = make_df(rows)
        pats = detect_candlestick_patterns(df)
        # 该K线同时是锤子线与看涨吞没（前日小阴实体99.6~99.9，今日99.5开100.2收包住）
        last_date = pats[-1]['date']
        names = {p['pattern'] for p in pats if p['date'] == last_date}
        assert len([p for p in pats if p['date'] == last_date]) >= 2
        assert '锤子线' in names
        assert '看涨吞没' in names

    def test_result_structure_and_recent_n(self):
        """返回结构与recent_n截取"""
        closes = list(np.linspace(100, 120, 40))
        rows = [bar(closes[i - 1] if i > 0 else 100, closes[i]) for i in range(len(closes))]
        df = make_df(rows)
        pats = detect_candlestick_patterns(df, recent_n=10)
        for p in pats:
            assert set(p.keys()) == {'date', 'pattern', 'direction'}
            assert p['direction'] in ('bullish', 'bearish', 'neutral')
        # 所有日期都在最近10根范围内
        dates = list(df.index[-10:].strftime('%Y-%m-%d'))
        for p in pats:
            assert p['date'] in dates


# ============================================
# 支撑阻力位
# ============================================
class TestSupportResistance:
    def _oscillation_df(self):
        """构造95-105之间反复震荡的序列，收在100"""
        closes = []
        for _ in range(5):
            closes += [95 + 10 * j / 5 for j in range(1, 6)]   # 95→105
            closes += [105 - 10 * j / 5 for j in range(1, 6)]  # 105→95
        closes += [97, 100]  # 收在100，让95在下方、105在上方
        rows = []
        prev = 95.0
        for c in closes:
            rows.append(bar(prev, c))
            prev = c
        return make_df(rows)

    def test_support_and_resistance_levels(self):
        """震荡序列 → 支撑≈95、阻力≈105（容差3%）"""
        sr = detect_support_resistance(self._oscillation_df())
        assert len(sr['supports']) >= 1
        assert len(sr['resistances']) >= 1
        assert abs(sr['supports'][0]['price'] - 95) / 95 < 0.03
        assert abs(sr['resistances'][0]['price'] - 105) / 105 < 0.03
        assert sr['supports'][0]['touches'] >= 2
        assert sr['resistances'][0]['touches'] >= 2

    def test_current_price_and_sorting(self):
        """现价正确，支撑低于现价按近到远排序，阻力高于现价同理"""
        sr = detect_support_resistance(self._oscillation_df())
        cur = sr['current_price']
        assert abs(cur - 100) < 0.5
        for s in sr['supports']:
            assert s['price'] < cur
        for r in sr['resistances']:
            assert r['price'] >= cur
        sup_prices = [s['price'] for s in sr['supports']]
        res_prices = [r['price'] for r in sr['resistances']]
        assert sup_prices == sorted(sup_prices, reverse=True)
        assert res_prices == sorted(res_prices)
        assert len(sr['supports']) <= 5
        assert len(sr['resistances']) <= 5

    def test_structure(self):
        """返回结构完整"""
        sr = detect_support_resistance(self._oscillation_df())
        assert set(sr.keys()) == {'supports', 'resistances', 'current_price'}
        for lv in sr['supports'] + sr['resistances']:
            assert set(lv.keys()) == {'price', 'touches'}


# ============================================
# MACD背离
# ============================================
class TestMacdDivergence:
    def _top_divergence_df(self):
        """先强涨后弱涨创新高 → 顶背离"""
        closes = []
        # 阶段1：强势上涨 100→135（30根，涨幅渐大且总和恰为35）
        p = 100.0
        for i in range(30):
            p += 0.44 + 0.05 * i
            closes.append(round(p, 2))
        # 阶段2：回调 135→125
        for j in range(1, 11):
            closes.append(round(135 - j, 2))
        # 阶段3：缓慢爬升 125→138（动能弱）
        for j in range(1, 19):
            closes.append(round(125 + j * 13 / 18, 2))
        # 阶段4：小幅回落3根，让最后的峰可被识别为摆动点
        closes += [round(138 - 0.5 * j, 2) for j in range(1, 4)]
        rows = []
        prev = 100.0
        for c in closes:
            rows.append(bar(prev, c))
            prev = c
        return make_df(rows)

    def test_top_divergence(self):
        """价格新高但MACD走低 → top_divergence=True，且包含RSI背离共振"""
        df = self._top_divergence_df()
        div = detect_macd_divergence(df)
        rsi_div = detect_rsi_divergence(df)
        assert div['top_divergence'] is True
        assert div['bottom_divergence'] is False
        assert '顶背离' in div['detail'] or '新高' in div['detail']
        assert rsi_div['rsi_top_divergence'] is True
        assert 'MACD+RSI双背离共振' in div['detail']

    def test_no_divergence_on_steady_trend(self):
        """单边稳定上涨（无新高+动能走低的组合）→ 不报顶背离"""
        closes = [100 + i * 0.5 for i in range(60)]
        rows = []
        prev = 100.0
        for c in closes:
            rows.append(bar(prev, c))
            prev = c
        div = detect_macd_divergence(make_df(rows))
        assert div['top_divergence'] is False
        assert div['bottom_divergence'] is False
        assert '未检测到背离' in div['detail']

    def test_structure_on_short_data(self):
        """数据不足时不抛异常且结构正确"""
        rows = [bar(100, 101), bar(101, 102), bar(102, 103)]
        div = detect_macd_divergence(make_df(rows))
        assert set(div.keys()) == {'top_divergence', 'bottom_divergence',
                                   'detail', 'checked_bars'}
        assert isinstance(div['top_divergence'], bool)
        assert isinstance(div['bottom_divergence'], bool)

    def test_structure_on_random_walk(self):
        """常规随机数据上不抛异常且返回结构正确"""
        np.random.seed(42)
        closes = list(100 + np.cumsum(np.random.randn(80)))
        rows = []
        prev = closes[0]
        for c in closes:
            rows.append(bar(prev, c))
            prev = c
        div = detect_macd_divergence(make_df(rows))
        assert set(div.keys()) == {'top_divergence', 'bottom_divergence',
                                   'detail', 'checked_bars'}
        assert div['checked_bars'] == 60  # 受默认lookback=60截断
