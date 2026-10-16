# -*- coding: utf-8 -*-
"""
价格监控纯函数测试：回撤统计 / 市场时段判定（含法定休市节假日过滤） / 财报日期挑选
全部合成数据，不依赖网络与数据库。
运行：cd backend && python -m pytest tests/test_alert_service.py -v
"""
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
import pytz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.alert_service import compute_drawdown_stats, is_market_open, is_trading_holiday
from app.routers.stocks import pick_next_earnings_date


# ============================================
# 回撤统计
# ============================================
class TestComputeDrawdownStats:
    def test_empty_series(self):
        stats = compute_drawdown_stats([])
        assert stats == {'current_drawdown_pct': 0.0, 'max_drawdown_pct': 0.0,
                         'peak_value': 0.0, 'latest_value': 0.0}

    def test_monotonic_rising_no_drawdown(self):
        stats = compute_drawdown_stats([100.0, 110.0, 120.0])
        assert stats['max_drawdown_pct'] == 0.0
        assert stats['current_drawdown_pct'] == 0.0
        assert stats['peak_value'] == 120.0
        assert stats['latest_value'] == 120.0

    def test_known_drawdown_math(self):
        # 峰值120回落到90：最大回撤=(120-90)/120=25%；回升到110：当前回撤=(120-110)/120≈8.33%
        stats = compute_drawdown_stats([100.0, 120.0, 90.0, 110.0])
        assert stats['max_drawdown_pct'] == 25.0
        assert abs(stats['current_drawdown_pct'] - 8.33) < 0.01
        assert stats['peak_value'] == 120.0
        assert stats['latest_value'] == 110.0

    def test_peak_after_trough(self):
        # 新高出现在末段时，当前回撤为0且峰值更新
        stats = compute_drawdown_stats([100.0, 80.0, 130.0])
        assert stats['current_drawdown_pct'] == 0.0
        assert stats['max_drawdown_pct'] == 20.0   # (100-80)/100
        assert stats['peak_value'] == 130.0


# ============================================
# 市场时段判定（显式构造带时区时间，不依赖本机时钟）
# ============================================
SH_TZ = pytz.timezone('Asia/Shanghai')


class TestIsMarketOpen:
    def test_us_open_during_et_hours(self):
        # 北京周三22:00 = 美东周三10:00（8月为夏令时EDT）→ 开市
        now = SH_TZ.localize(datetime(2026, 8, 26, 22, 0))
        assert is_market_open('US', now) is True

    def test_us_closed_before_open_and_after_close(self):
        # 北京周三19:00 = 美东7:00 未开盘
        assert is_market_open('US', SH_TZ.localize(datetime(2026, 8, 26, 19, 0))) is False
        # 北京周四05:00 = 美东周三17:00 已收盘
        assert is_market_open('US', SH_TZ.localize(datetime(2026, 8, 27, 5, 0))) is False

    def test_us_weekend_closed_even_if_hours_match(self):
        # 北京周六23:00 = 美东周六11:00 → 周末休市
        assert is_market_open('US', SH_TZ.localize(datetime(2026, 8, 29, 23, 0))) is False

    def test_a_share_sessions(self):
        wed = datetime(2026, 8, 26)
        assert is_market_open('A', SH_TZ.localize(wed.replace(hour=10, minute=0))) is True
        assert is_market_open('A', SH_TZ.localize(wed.replace(hour=12, minute=0))) is False  # 午休
        assert is_market_open('A', SH_TZ.localize(wed.replace(hour=14, minute=59))) is True
        assert is_market_open('A', SH_TZ.localize(wed.replace(hour=15, minute=1))) is False
        assert is_market_open('A', SH_TZ.localize(datetime(2026, 8, 30, 10, 0))) is False  # 周日

    def test_holidays_closed_even_on_weekdays(self):
        # 2026-12-25 周五圣诞节：美股休市
        assert is_trading_holiday('US', date(2026, 12, 25)) is True
        assert is_market_open('US', SH_TZ.localize(datetime(2026, 12, 25, 23, 0))) is False
        # 2026-07-03 周五（7月4日独立日落周六，美股周五补休）
        assert is_trading_holiday('US', date(2026, 7, 3)) is True
        # 2026-10-01 周四国庆节：A股休市
        assert is_trading_holiday('A', date(2026, 10, 1)) is True
        assert is_market_open('A', SH_TZ.localize(datetime(2026, 10, 1, 10, 30))) is False


# ============================================
# 财报日期挑选
# ============================================
class TestPickNextEarningsDate:
    def test_picks_earliest_future_from_candidates(self):
        today = date(2026, 8, 23)
        candidates = [date(2026, 9, 15), date(2026, 9, 2), date(2026, 1, 10)]
        ts_like = [pd.Timestamp(c) for c in candidates]  # yfinance实际返回Timestamp
        assert pick_next_earnings_date(ts_like, today) == date(2026, 9, 2)

    def test_all_past_returns_none(self):
        today = date(2026, 8, 23)
        assert pick_next_earnings_date([date(2026, 1, 1), date(2026, 8, 22)], today) is None

    def test_today_counts_as_future(self):
        today = date(2026, 8, 23)
        assert pick_next_earnings_date([today], today) == today

    def test_none_and_garbage_tolerated(self):
        today = date(2026, 8, 23)
        assert pick_next_earnings_date(None, today) is None
        assert pick_next_earnings_date(['garbage', None], today) is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
