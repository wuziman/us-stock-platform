# -*- coding: utf-8 -*-
"""
自动化测试：K线缓存 + 过期缓存自动清理 + 定时日报决策逻辑
运行：cd backend && python -m pytest tests/test_cache_schedule.py -v
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Base, engine, SessionLocal
from app.models_platform import KlineCache
from app.services import stock_service, scheduler_service
from app.services.stock_service import StockService

# 确保kline_cache表存在（正常由main.py的create_all创建）
Base.metadata.create_all(bind=engine)


# ============================================
# 工具
# ============================================
def make_fake_df(days: int = 10) -> pd.DataFrame:
    """构造一段简单的日K数据"""
    idx = pd.date_range('2026-08-03', periods=days, freq='B')
    return pd.DataFrame({
        'Open': [100.0 + i for i in range(days)],
        'High': [101.0 + i for i in range(days)],
        'Low': [99.0 + i for i in range(days)],
        'Close': [100.5 + i for i in range(days)],
        'Volume': [1000] * days,
    }, index=idx)


TEST_CODE = 'ZZZCACHE'  # 专用测试代码，避免污染真实股票的缓存


def _cleanup_test_rows():
    db = SessionLocal()
    try:
        db.query(KlineCache).filter(
            KlineCache.cache_key.like(f"%{TEST_CODE}%")).delete()
        db.commit()
    finally:
        db.close()


# ============================================
# K线缓存
# ============================================
class TestKlineCache:
    def setup_method(self):
        _cleanup_test_rows()

    def teardown_method(self):
        _cleanup_test_rows()

    def test_first_call_fetches_second_hits_cache(self, monkeypatch):
        """第一次调用走fetch并写缓存；第二次命中缓存，fetch不被调用"""
        calls = []
        fake_df = make_fake_df()

        def fake_fetch(code, market, period):
            calls.append((code, market, period))
            return fake_df

        monkeypatch.setattr(stock_service, '_fetch_kline', fake_fetch)

        df1 = StockService.get_stock_data(TEST_CODE, 'US', '3mo')  # noqa: F841 -- 触发首次取数并写入缓存
        assert len(calls) == 1

        df2 = StockService.get_stock_data(TEST_CODE, 'US', '3mo')
        assert len(calls) == 1, "第二次应命中缓存，不应再触发fetch"
        # 缓存还原的数据内容一致
        assert list(df2['Close']) == list(fake_df['Close'])
        assert isinstance(df2.index, pd.DatetimeIndex)
        assert str(df2.index[0].date()) == '2026-08-03'

    def test_ttl_expiry_triggers_refetch(self, monkeypatch):
        """把fetched_at改老超过TTL后，再次调用应重新fetch并刷新缓存时间"""
        calls = []

        def fake_fetch(code, market, period):
            calls.append(1)
            return make_fake_df()

        monkeypatch.setattr(stock_service, '_fetch_kline', fake_fetch)

        StockService.get_stock_data(TEST_CODE, 'US', '3mo')
        assert len(calls) == 1

        # 手动把fetched_at改老40分钟（TTL为30分钟）
        db = SessionLocal()
        try:
            row = db.query(KlineCache).filter(
                KlineCache.cache_key == f"US:{TEST_CODE}:3mo").first()
            assert row is not None
            row.fetched_at = datetime.now() - timedelta(minutes=40)
            db.commit()
        finally:
            db.close()

        StockService.get_stock_data(TEST_CODE, 'US', '3mo')
        assert len(calls) == 2, "缓存过期后应重新fetch"

        # 过期重拉后fetched_at被刷新
        db = SessionLocal()
        try:
            row = db.query(KlineCache).filter(
                KlineCache.cache_key == f"US:{TEST_CODE}:3mo").first()
            assert datetime.now() - row.fetched_at < timedelta(minutes=1)
        finally:
            db.close()

    def test_fresh_cache_within_ttl_no_refetch(self, monkeypatch):
        """TTL内即使手动改动缓存内容也直接返回缓存（证明未重新fetch）"""
        monkeypatch.setattr(
            stock_service, '_fetch_kline',
            lambda code, market, period: make_fake_df())

        StockService.get_stock_data(TEST_CODE, 'US', '3mo')

        # 篡改缓存里的收盘价：若再次fetch则篡改会被覆盖
        db = SessionLocal()
        try:
            row = db.query(KlineCache).filter(
                KlineCache.cache_key == f"US:{TEST_CODE}:3mo").first()
            tampered = row.data_json.replace('"Close": 100.5', '"Close": 666.6')
            row.data_json = tampered
            db.commit()
        finally:
            db.close()

        df = StockService.get_stock_data(TEST_CODE, 'US', '3mo')
        assert float(df['Close'].iloc[0]) == 666.6, "TTL内应直接返回缓存数据"

    def test_fetch_failure_not_cached(self, monkeypatch):
        """fetch失败返回None且不写缓存，下次调用仍会重试"""
        calls = []

        def bad_fetch(code, market, period):
            calls.append(1)
            return None

        monkeypatch.setattr(stock_service, '_fetch_kline', bad_fetch)

        assert StockService.get_stock_data(TEST_CODE, 'US', '3mo') is None
        assert StockService.get_stock_data(TEST_CODE, 'US', '3mo') is None
        assert len(calls) == 2, "失败结果不应被缓存，每次都应重试"

    def test_clear_kline_cache(self, monkeypatch):
        """clear_kline_cache清空缓存表"""
        monkeypatch.setattr(
            stock_service, '_fetch_kline',
            lambda code, market, period: make_fake_df())
        StockService.get_stock_data(TEST_CODE, 'US', '3mo')

        removed = stock_service.clear_kline_cache()
        assert removed >= 1

        db = SessionLocal()
        try:
            row = db.query(KlineCache).filter(
                KlineCache.cache_key == f"US:{TEST_CODE}:3mo").first()
            assert row is None
        finally:
            db.close()

    def test_cleanup_expired_kline_cache(self, monkeypatch):
        """cleanup_expired_kline_cache 仅清理超过 max_age_hours 的陈旧记录，保留新鲜记录"""
        monkeypatch.setattr(
            stock_service, '_fetch_kline',
            lambda code, market, period: make_fake_df())
        StockService.get_stock_data(TEST_CODE, 'US', '3mo')
        StockService.get_stock_data(TEST_CODE, 'US', '1y')

        db = SessionLocal()
        try:
            old_row = db.query(KlineCache).filter(
                KlineCache.cache_key == f"US:{TEST_CODE}:3mo").first()
            old_row.fetched_at = datetime.now() - timedelta(hours=30)
            db.commit()
        finally:
            db.close()

        removed = stock_service.cleanup_expired_kline_cache(max_age_hours=24)
        assert removed >= 1

        db = SessionLocal()
        try:
            assert db.query(KlineCache).filter(
                KlineCache.cache_key == f"US:{TEST_CODE}:3mo").first() is None
            assert db.query(KlineCache).filter(
                KlineCache.cache_key == f"US:{TEST_CODE}:1y").first() is not None
        finally:
            db.close()


# ============================================
# 定时日报决策逻辑 should_send（纯函数）
# ============================================
class TestShouldSend:
    # 周四 17:25（配置17:30，相差5分钟）
    THU_1725 = datetime(2026, 8, 20, 17, 25)

    def test_all_conditions_met(self):
        """工作日+窗口内+已启用+今日未发 → 应发送"""
        assert scheduler_service.should_send(
            3, self.THU_1725, True, 17, 30, last_sent_date='2026-08-19') is True

    def test_never_sent(self):
        assert scheduler_service.should_send(
            3, self.THU_1725, True, 17, 30, last_sent_date=None) is True

    def test_disabled_returns_false(self):
        """未启用 → 不发送（防开发期误发）"""
        assert scheduler_service.should_send(
            3, self.THU_1725, False, 17, 30, last_sent_date=None) is False

    def test_weekend_returns_false(self):
        """周六(5)/周日(6) → 不发送"""
        assert scheduler_service.should_send(
            5, self.THU_1725, True, 17, 30, last_sent_date=None) is False
        assert scheduler_service.should_send(
            6, self.THU_1725, True, 17, 30, last_sent_date=None) is False

    def test_monday_to_friday_ok(self):
        """周一~周五均视为工作日"""
        for weekday in range(5):
            assert scheduler_service.should_send(
                weekday, self.THU_1725, True, 17, 30, last_sent_date=None) is True

    def test_outside_window_returns_false(self):
        """与配置时间相差>=30分钟 → 不发送"""
        late = datetime(2026, 8, 20, 18, 0)   # 相差30分钟
        early = datetime(2026, 8, 20, 16, 0)  # 相差90分钟
        assert scheduler_service.should_send(
            3, late, True, 17, 30, last_sent_date=None) is False
        assert scheduler_service.should_send(
            3, early, True, 17, 30, last_sent_date=None) is False

    def test_window_boundaries(self):
        """边界：相差29分钟→发送；恰好30分钟→不发送"""
        within = datetime(2026, 8, 20, 17, 59)  # 与17:30相差29分钟
        exact = datetime(2026, 8, 20, 17, 0)    # 与17:30相差30分钟
        assert scheduler_service.should_send(
            3, within, True, 17, 30, last_sent_date=None) is True
        assert scheduler_service.should_send(
            3, exact, True, 17, 30, last_sent_date=None) is False

    def test_already_sent_today_returns_false(self):
        """今天已发送过 → 不重复发送（防重复推送）"""
        assert scheduler_service.should_send(
            3, self.THU_1725, True, 17, 30,
            last_sent_date='2026-08-20') is False

    def test_sent_yesterday_still_sends(self):
        """昨天发过不影响今天发送"""
        assert scheduler_service.should_send(
            3, self.THU_1725, True, 17, 30,
            last_sent_date='2026-08-19') is True

    def test_custom_time_window(self):
        """自定义时间（如12:00午间推送）同样生效"""
        noon = datetime(2026, 8, 20, 12, 10)
        assert scheduler_service.should_send(
            3, noon, True, 12, 0, last_sent_date=None) is True
        assert scheduler_service.should_send(
            3, noon, True, 15, 0, last_sent_date=None) is False


# ============================================
# scheduled_job 安全护栏
# ============================================
class TestScheduledJobGuard:
    def test_skips_when_disabled(self, monkeypatch):
        """开关关闭时scheduled_job绝不执行推送（防开发期误发的最后防线）。
        恢复逻辑包在 finally 里：断言失败也不能把开关永久留在 false"""
        db = SessionLocal()
        try:
            original = scheduler_service._get_setting(
                db, scheduler_service.KEY_ENABLED, '')
            scheduler_service._set_setting(
                db, scheduler_service.KEY_ENABLED, 'false')
        finally:
            db.close()

        try:
            executed = []

            def fake_run_now():
                executed.append(1)
                return {'sent': True, 'message': '', 'report': ''}

            monkeypatch.setattr(scheduler_service, 'run_now', fake_run_now)
            scheduler_service.scheduled_job()
            assert executed == [], "开关关闭时不应执行推送"
        finally:
            # 恢复原有开关状态（无论断言成败）
            if original:
                db = SessionLocal()
                try:
                    scheduler_service._set_setting(
                        db, scheduler_service.KEY_ENABLED, original)
                finally:
                    db.close()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
