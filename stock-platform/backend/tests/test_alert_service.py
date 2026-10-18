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

    def test_us_good_friday_and_cn_lunar_holidays_closed(self):
        """[第二段-5 验证] 美股耶稣受难日（Good Friday）及A股春节/清明/端午/中秋休市"""
        # 美股耶稣受难日：2025-04-18、2026-04-03、2027-03-26 均为周五
        for gf in (date(2025, 4, 18), date(2026, 4, 3), date(2027, 3, 26)):
            assert is_trading_holiday('US', gf) is True
        assert is_market_open('US', SH_TZ.localize(datetime(2026, 4, 3, 22, 0))) is False

        # A股春节(2026-02-17 周二)、清明(2026-04-06 周一)、端午(2026-06-19 周五)、中秋(2026-09-25 周五)
        for cn_hd in (date(2026, 2, 17), date(2026, 4, 6), date(2026, 6, 19), date(2026, 9, 25)):
            assert is_trading_holiday('A', cn_hd) is True
            assert is_market_open('A', SH_TZ.localize(datetime(cn_hd.year, cn_hd.month, cn_hd.day, 10, 30))) is False


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




# ============================================
# [第四段-10a] 告警真实链路集成测试（不 mock 被测函数本身，仅 mock 行情与微信 HTTP 边界）
# ============================================
class TestAlertChainIntegration:
    def _clean_db(self):
        from app.database import SessionLocal
        from app.models import Position, Setting
        from app.models_platform import PriceAlertLog, PortfolioSnapshot
        db = SessionLocal()
        try:
            db.query(Position).delete()
            db.query(PriceAlertLog).delete()
            db.query(PortfolioSnapshot).delete()
            db.query(Setting).filter(Setting.key == "price_alert_enabled").delete()
            db.commit()
        finally:
            db.close()

    def test_stop_loss_and_take_profit_chain_with_dedup_and_retry(self, monkeypatch):
        """
        验证 check_position_alerts 真实链路：
        1. 跌破止损 / 突破止盈触发微信推送并落库 PriceAlertLog
        2. 同日二次触发被 _alert_already_sent 幂等拦截（不重复推）
        3. 若微信推送失败（sent=False），不落库 PriceAlertLog，下一轮自动重试成功
        """
        from app.database import SessionLocal
        from app.models import Position
        from app.models_platform import PriceAlertLog
        from app.services.alert_service import check_position_alerts
        from app.services.report_service import ReportService
        from app.services.stock_service import StockService

        self._clean_db()
        db = SessionLocal()
        try:
            pos_sl = Position(
                stock_code="MU", stock_name="Micron", market="US",
                buy_price=100.0, quantity=10, buy_date=datetime(2026, 10, 1, 10, 0),
                stop_loss=92.0, take_profit=115.0, status="holding",
            )
            pos_tp = Position(
                stock_code="NVDA", stock_name="NVIDIA", market="US",
                buy_price=100.0, quantity=10, buy_date=datetime(2026, 10, 1, 10, 0),
                stop_loss=92.0, take_profit=115.0, status="holding",
            )
            db.add_all([pos_sl, pos_tp])
            db.commit()

            quotes = {"MU": {"price": 90.0}, "NVDA": {"price": 120.0}}
            monkeypatch.setattr(StockService, "get_realtime_quote", staticmethod(lambda code, mkt="US": quotes.get(code)))

            report_svc = ReportService()
            sent_messages = []

            # 第1轮：模拟网络抖动推送失败 -> 不写 PriceAlertLog
            monkeypatch.setattr(report_svc, "send_to_wechat", lambda msg: {"sent": False, "error": "net_err"})
            res_fail = check_position_alerts(report_svc, today="2026-10-16")
            assert len(res_fail) == 2
            assert all(r["sent"] is False for r in res_fail)
            assert db.query(PriceAlertLog).count() == 0

            # 第2轮：微信恢复正常 -> 两条告警全部推送成功并落库
            monkeypatch.setattr(report_svc, "send_to_wechat", lambda msg: (sent_messages.append(msg) or {"sent": True}))
            res_ok = check_position_alerts(report_svc, today="2026-10-16")
            assert len(res_ok) == 2
            assert all(r["sent"] is True for r in res_ok)
            assert len(sent_messages) == 2
            assert db.query(PriceAlertLog).count() == 2

            # 第3轮：同一天再次运行 -> 去重生效，0条重复推送
            res_dup = check_position_alerts(report_svc, today="2026-10-16")
            assert res_dup == []
            assert len(sent_messages) == 2
        finally:
            self._clean_db()
            db.close()

    def test_full_run_price_monitor_snapshot_and_drawdown_alert(self, monkeypatch):
        """
        验证 run_price_monitor -> update_portfolio_snapshot -> check_drawdown_alert 全链路：
        历史净值峰值 $100,000，当前持仓跌至 $75,000（回撤 25% >= 20% 阈值）-> 自动更新今日快照并推送回撤破线告警
        """
        import app.services.alert_service as alert_mod
        from app.database import SessionLocal
        from app.models import Position
        from app.models_platform import PortfolioSnapshot
        from app.services.report_service import ReportService
        from app.services.stock_service import StockService

        self._clean_db()
        db = SessionLocal()
        try:
            db.add(PortfolioSnapshot(snap_date="2026-10-10", total_value=100000.0))
            db.add(Position(
                stock_code="MU", stock_name="Micron", market="US",
                buy_price=100.0, quantity=1000, buy_date=datetime(2026, 10, 1, 10, 0),
                stop_loss=70.0, take_profit=130.0, status="holding",
            ))
            db.commit()

            # 模拟当前处于交易时段，MU 现价 $75 -> 组合总市值 $75,000（较峰值 $100,000 回撤 25%）
            monkeypatch.setattr(alert_mod, "is_market_open", lambda mkt, now=None: True)
            monkeypatch.setattr(StockService, "get_realtime_quote", staticmethod(lambda code, mkt="US": {"price": 75.0}))
            sent_payloads = []
            monkeypatch.setattr(ReportService, "send_to_wechat", lambda self, msg: (sent_payloads.append(msg) or {"sent": True}))

            out = alert_mod.run_price_monitor()
            assert out["total_value"] == 75000.0
            assert out["drawdown_alert"] is not None
            assert out["drawdown_alert"]["sent"] is True
            assert out["drawdown_alert"]["current_drawdown_pct"] == 25.0
            assert any("组合回撤破线预警" in m for m in sent_payloads)

            # 同日二次调用：回撤告警已去重
            out_dup = alert_mod.run_price_monitor()
            assert out_dup["drawdown_alert"]["already_sent"] is True
        finally:
            self._clean_db()
            db.close()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
