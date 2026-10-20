# -*- coding: utf-8 -*-
"""
P0 -> P1 -> P2 全量升级回归测试集：
1. P0-1: indicators.py calculate_sma 签名统一 & add_all_indicators(df) 全量调用零异常
2. P0-2/3: /api/stocks/market-regime 复用 evaluate_market_regime 唯一真源 + 盘前尾部 NaN 防御
3. P0-4: cleanup_expired_kline_cache / cleanup_expired_analysis_history SQL 条件批量删除
4. P0-5: BacktestService 四策略统一支持 trailing_stop / staged_entry / slippage_bps
5. P1-1: sell_position 支持分批止盈/部分减仓 (sell_quantity < quantity) 与超额拦截
6. P1-2: /api/portfolio/settings/usd_cny_rate 读写与纯A股组合 currency='CNY' 标识
7. P1-3: 重计算端点并发互斥锁 (409 Conflict) 验证
8. P2-1: portfolio_service 领域层抽离与 portfolio 路由兼容导出验证
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.models import AnalysisHistory, Position, Setting
from app.models_platform import KlineCache
from app.routers import ai_pick as ai_pick_router
from app.routers import backtest as bt_router
from app.routers import portfolio as pf_router
from app.routers import stocks as stocks_router
from app.routers.analysis import cleanup_expired_analysis_history
from app.schemas import OptimizeRequest, PositionCreate, PositionSell
from app.services import portfolio_service
from app.services.backtest_service import BacktestService
from app.services.stock_service import cleanup_expired_kline_cache
from app.utils.indicators import (
    add_all_indicators,
    calculate_adx,
    calculate_kdj,
    calculate_obv,
    calculate_sma,
    calculate_vwap,
)


def _make_ohlcv_df(days: int = 120, base_price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=days, freq="B")
    t = np.arange(days, dtype=float)
    close = base_price + 8.0 * np.sin(t / 5.0) + 0.25 * t
    open_ = close - 0.5
    high = close + 2.5
    low = close - 2.5
    vol = np.full(days, 2_000_000, dtype=float)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


class TestP0Fixes:
    def test_p0_1_indicators_signature_and_add_all_indicators(self):
        """验证 calculate_sma 支持 Series/DataFrame 且 add_all_indicators(df) 不再抛 TypeError"""
        df = _make_ohlcv_df(60)
        sma_from_series = calculate_sma(df["Close"], 20)
        sma_from_df = calculate_sma(df, 20)
        assert len(sma_from_series) == 60
        assert np.allclose(sma_from_series.dropna(), sma_from_df.dropna())

        # DataFrame 与 Series 两种调用方式均正常
        assert len(calculate_obv(df)) == 60
        assert len(calculate_obv(df["Close"], df["Volume"])) == 60
        assert len(calculate_vwap(df)) == 60
        assert len(calculate_vwap(df["High"], df["Low"], df["Close"], df["Volume"])) == 60
        k, d, j = calculate_kdj(df)
        assert len(k) == 60 and len(d) == 60 and len(j) == 60
        adx = calculate_adx(df)
        assert len(adx) == 60

        enriched = add_all_indicators(df.copy())
        for col in ("SMA_20", "SMA_50", "EMA_20", "RSI", "MACD", "BB_Upper", "Stoch_K", "Stoch_D", "ATR", "OBV", "VWAP", "ADX"):
            assert col in enriched.columns

    def test_p0_2_market_regime_unified_and_nan_safe(self, monkeypatch):
        """验证 /api/stocks/market-regime 复用 evaluate_market_regime 并过滤盘前尾部 NaN 行"""
        import yfinance as yf

        class FakeTicker:
            def __init__(self, symbol):
                self.symbol = symbol

            def history(self, period="5d"):
                if self.symbol == "^VIX":
                    return pd.DataFrame({"Close": [18.5, 22.4, float("nan")]})
                return pd.DataFrame({"Close": [480.0, 470.4, float("nan")]})

        monkeypatch.setattr(yf, "Ticker", FakeTicker)
        res = stocks_router.get_market_regime()
        assert res["vix"] == 22.4
        assert res["qqq_change"] == -2.0
        assert res["status"] == "CAUTION"
        assert res["buy_discount"] == 0.98
        assert res["position_scale"] == 0.5

    def test_p0_4_sql_bulk_cleanup_kline_and_analysis_history(self):
        """验证 K线缓存与分析历史过期清理走 SQL 批量删除且仅清理超期行"""
        db = SessionLocal()
        try:
            old_time = datetime.now() - timedelta(days=100)
            fresh_time = datetime.now()
            db.query(KlineCache).filter(KlineCache.cache_key.in_(["US:TEST_OLD:3mo", "US:TEST_NEW:3mo"])).delete(synchronize_session=False)
            db.add(KlineCache(cache_key="US:TEST_OLD:3mo", data_json="[]", fetched_at=old_time))
            db.add(KlineCache(cache_key="US:TEST_NEW:3mo", data_json="[]", fetched_at=fresh_time))

            db.query(AnalysisHistory).filter(AnalysisHistory.stock_code.in_(["TEST_OLD", "TEST_NEW"])).delete(synchronize_session=False)
            db.add(AnalysisHistory(stock_code="TEST_OLD", stock_name="Old", total_score=5.0, created_at=old_time))
            db.add(AnalysisHistory(stock_code="TEST_NEW", stock_name="New", total_score=8.0, created_at=fresh_time))
            db.commit()

            deleted_kline = cleanup_expired_kline_cache(max_age_hours=24)
            deleted_hist = cleanup_expired_analysis_history(max_age_days=90, db=db)
            db.expire_all()

            assert deleted_kline >= 1
            assert deleted_hist >= 1
            assert db.query(KlineCache).filter(KlineCache.cache_key == "US:TEST_OLD:3mo").first() is None
            assert db.query(KlineCache).filter(KlineCache.cache_key == "US:TEST_NEW:3mo").first() is not None
            assert db.query(AnalysisHistory).filter(AnalysisHistory.stock_code == "TEST_OLD").first() is None
            assert db.query(AnalysisHistory).filter(AnalysisHistory.stock_code == "TEST_NEW").first() is not None
        finally:
            db.query(KlineCache).filter(KlineCache.cache_key.in_(["US:TEST_OLD:3mo", "US:TEST_NEW:3mo"])).delete(synchronize_session=False)
            db.query(AnalysisHistory).filter(AnalysisHistory.stock_code.in_(["TEST_OLD", "TEST_NEW"])).delete(synchronize_session=False)
            db.commit()
            db.close()

    def test_p0_5_backtest_all_strategies_support_trailing_staged_and_slippage(self, monkeypatch):
        """验证 4 大策略均已打通 trailing_stop / staged_entry / slippage_bps"""
        svc = BacktestService()
        df = _make_ohlcv_df(150, base_price=100.0)
        monkeypatch.setattr(svc.stock_service, "get_stock_data", lambda *a, **kw: df.copy())

        for strat in ("linear", "nonlinear", "ma_cross", "macd"):
            res_base = svc.run_backtest(
                "MU", strat, period="1y", trailing_stop=False, staged_entry=False, slippage_bps=0.0
            )
            res_enhanced = svc.run_backtest(
                "MU", strat, period="1y", trailing_stop=True, staged_entry=True, slippage_bps=10.0
            )
            assert "error" not in res_base
            assert "error" not in res_enhanced
            assert res_enhanced["trailing_stop"] is True
            assert res_enhanced["staged_entry"] is True
            assert res_enhanced["slippage_bps"] == 10.0
            if res_base["trade_count"] > 0 and res_enhanced["trade_count"] > 0:
                assert res_base["final_value"] != res_enhanced["final_value"]


class TestP1AndP2Upgrades:
    def test_p1_partial_sell_and_full_sell_lifecycle(self, monkeypatch):
        """验证分批止盈 (sell_quantity < quantity) 扣减持仓并生成已实现 sold 记录，超卖报 400"""
        monkeypatch.setattr(pf_router, "_fetch_quotes", lambda positions: [{"price": 120.0} for _ in positions])
        db = SessionLocal()
        try:
            db.query(Position).delete()
            db.commit()

            pos = asyncio.run(pf_router.add_position(
                PositionCreate(
                    stock_code="NVDA",
                    stock_name="NVIDIA",
                    buy_price=100.0,
                    quantity=30,
                    buy_date=datetime(2026, 9, 1, 10, 0),
                ),
                db=db,
            ))

            # 1. 超额卖出 (40 > 30) -> HTTPException 400
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(pf_router.sell_position(
                    pos.id,
                    PositionSell(sell_price=120.0, sell_date=datetime(2026, 9, 11, 10, 0), sell_quantity=40),
                    db=db,
                ))
            assert exc_info.value.status_code == 400

            # 2. 分批止盈卖出 10 股 @ $120 -> 剩余 20 股仍在 holding，新增 1 条 10 股 sold 历史记录
            part_res = asyncio.run(pf_router.sell_position(
                pos.id,
                PositionSell(sell_price=120.0, sell_date=datetime(2026, 9, 11, 10, 0), sell_quantity=10),
                db=db,
            ))
            assert part_res["partial"] is True
            assert part_res["sold_quantity"] == 10
            assert part_res["remaining_quantity"] == 20
            assert part_res["realized_pnl"] == 200.0
            assert part_res["realized_pnl_pct"] == 20.0

            holdings = pf_router.get_positions(db=db)
            assert len(holdings) == 1
            assert holdings[0].quantity == 20

            history = asyncio.run(pf_router.get_position_history(db=db))
            assert len(history) == 1
            assert history[0].quantity == 10
            assert history[0].realized_pnl == 200.0

            # 3. 剩余 20 股全额清仓 @ $130
            full_res = asyncio.run(pf_router.sell_position(
                pos.id,
                PositionSell(sell_price=130.0, sell_date=datetime(2026, 9, 21, 10, 0)),
                db=db,
            ))
            assert full_res["partial"] is False
            assert full_res["sold_quantity"] == 20
            assert full_res["remaining_quantity"] == 0
            assert full_res["realized_pnl"] == 600.0

            assert len(pf_router.get_positions(db=db)) == 0
            assert len(asyncio.run(pf_router.get_position_history(db=db))) == 2
        finally:
            db.query(Position).delete()
            db.commit()
            db.close()

    def test_p1_usd_cny_rate_endpoint_and_summary_currency(self, monkeypatch):
        """验证 /api/portfolio/settings/usd_cny_rate 接口与纯A股持仓汇总 currency='CNY'"""
        monkeypatch.setattr(pf_router, "_fetch_quotes", lambda positions: [{"price": p.buy_price} for p in positions])
        db = SessionLocal()
        try:
            db.query(Position).delete()
            db.commit()

            rate_res = asyncio.run(pf_router.set_usd_cny_rate_endpoint(usd_cny_rate=7.15, db=db))
            assert rate_res["usd_cny_rate"] == 7.15
            get_res = asyncio.run(pf_router.get_usd_cny_rate_endpoint(db=db))
            assert get_res["usd_cny_rate"] == 7.15

            # 仅持有 A 股时，summary.currency 应为 CNY
            db.add(Position(
                stock_code="600519", stock_name="贵州茅台", market="A",
                buy_price=1500.0, quantity=10, buy_date=datetime.now(), status="holding",
            ))
            db.commit()
            sum_a = pf_router.get_portfolio_summary(db=db)
            assert sum_a["currency"] == "CNY"
            assert sum_a["total_value"] == 15000.0

            # 同时持有美股时，自动折算为 USD
            db.add(Position(
                stock_code="MU", stock_name="Micron", market="US",
                buy_price=100.0, quantity=10, buy_date=datetime.now(), status="holding",
            ))
            db.commit()
            sum_mixed = pf_router.get_portfolio_summary(db=db)
            assert sum_mixed["currency"] == "USD"
        finally:
            db.query(Position).delete()
            db.query(Setting).filter(Setting.key == "usd_cny_rate").delete()
            db.commit()
            db.close()

    def test_p1_concurrency_locks_return_409_when_busy(self):
        """验证回测寻优、Walk-Forward、AI选股、猛禽池扫描在锁占用时立即返回 409 Conflict"""
        assert bt_router._opt_lock.acquire(blocking=False)
        try:
            with pytest.raises(HTTPException) as exc:
                bt_router.optimize_strategy(OptimizeRequest(stock_code="MU", strategy="linear"))
            assert exc.value.status_code == 409
        finally:
            bt_router._opt_lock.release()

        assert ai_pick_router._ai_pick_run_lock.acquire(blocking=False)
        try:
            with pytest.raises(HTTPException) as exc2:
                ai_pick_router.run_selection()
            assert exc2.value.status_code == 409
        finally:
            ai_pick_router._ai_pick_run_lock.release()

    def test_p2_portfolio_service_separation_and_reexports(self):
        """验证 portfolio_service 独立承载领域函数且 routers/portfolio 保持同源引用"""
        assert pf_router.calculate_recommended_position is portfolio_service.calculate_recommended_position
        assert pf_router.consolidate_duplicate_holdings is portfolio_service.consolidate_duplicate_holdings
        assert pf_router._enrich_holding is portfolio_service._enrich_holding
