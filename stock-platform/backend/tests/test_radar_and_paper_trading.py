# -*- coding: utf-8 -*-
"""
自选股买入雷达与一键模拟记账本单元测试：
1. 买入雷达距离测算、三色状态打标与排序
2. 模拟记账开仓、实时盯盘自动止盈/止损结算
3. 模拟盘实战胜率与累计盈亏统计
"""

import pandas as pd

from app.database import SessionLocal
from app.models_platform import PaperTrade
from app.schemas import PaperTradeOpenRequest
from app.services.paper_trading_service import PaperTradingService
from app.services.radar_service import RadarService


def test_radar_service_item_and_sorting(monkeypatch):
    """验证买入雷达能正确测算与买点的偏差百分比并标注状态"""
    svc = RadarService()

    fake_df = pd.DataFrame({
        "Close": [100.0] * 30,
    })
    monkeypatch.setattr(svc.stock_service, "get_stock_data", lambda *args, **kwargs: fake_df)

    # 1. 现价 <= 买点 (买点 95.0，现价 94.0) -> TRIGGERED
    monkeypatch.setattr(svc.stock_service, "get_realtime_quote", lambda *args: {"price": 94.0, "change_percent": -1.5})
    res_trig = svc.scan_stock_radar_item("SOXL", "Semiconductor 3X", "US")
    assert res_trig is not None
    assert res_trig["status"] == "TRIGGERED"
    assert res_trig["distance_pct"] <= 0.5

    # 2. 现价高于买点 1.0% -> APPROACHING
    monkeypatch.setattr(svc.stock_service, "get_realtime_quote", lambda *args: {"price": 96.0, "change_percent": 0.5})
    res_appr = svc.scan_stock_radar_item("NVDA", "Nvidia", "US")
    assert res_appr is not None
    assert res_appr["status"] == "APPROACHING"

    # 3. 现价远高于买点 10% -> WAITING
    monkeypatch.setattr(svc.stock_service, "get_realtime_quote", lambda *args: {"price": 110.0, "change_percent": 2.5})
    res_wait = svc.scan_stock_radar_item("AAPL", "Apple", "US")
    assert res_wait is not None
    assert res_wait["status"] == "WAITING"


def test_paper_trading_lifecycle_and_settlement(monkeypatch):
    """验证模拟盘记账开仓、达到止盈止损自动结算及实战胜率统计"""
    db = SessionLocal()
    svc = PaperTradingService()

    try:
        db.query(PaperTrade).delete()
        db.commit()

        # 1. 记录开仓单 1
        req1 = PaperTradeOpenRequest(
            stock_code="NVDA",
            stock_name="英伟达",
            market="US",
            buy_price=100.0,
            position_pct="20%",
            take_profit_price=115.0,
            stop_loss_price=92.0,
        )
        t1 = svc.open_trade(req1, db)
        assert t1.id is not None
        assert t1.status == "OPEN"

        # 2. 记录开仓单 2
        req2 = PaperTradeOpenRequest(
            stock_code="TSLA",
            stock_name="特斯拉",
            market="US",
            buy_price=200.0,
            position_pct="15%",
            take_profit_price=230.0,
            stop_loss_price=184.0,
        )
        t2 = svc.open_trade(req2, db)
        assert t2.id is not None

        # 3. 模拟实时盯盘：NVDA 冲破止盈价 116.0，TSLA 跌破止损价 180.0
        def fake_quote(code, market):
            if code == "NVDA":
                return {"price": 116.0}
            if code == "TSLA":
                return {"price": 180.0}
            return {"price": 100.0}

        monkeypatch.setattr(svc.stock_service, "get_realtime_quote", fake_quote)

        positions = svc.get_trades_with_settlement(db)
        assert len(positions) == 2

        nvda_pos = next(p for p in positions if p["stock_code"] == "NVDA")
        assert nvda_pos["status"] == "PROFIT"
        assert nvda_pos["exit_reason"] == "TARGET_HIT"
        assert nvda_pos["pnl_pct"] == 15.0

        tsla_pos = next(p for p in positions if p["stock_code"] == "TSLA")
        assert tsla_pos["status"] == "LOSS"
        assert tsla_pos["exit_reason"] == "STOP_HIT"
        assert tsla_pos["pnl_pct"] < 0

        # 4. 统计胜率
        summary = svc.get_summary(db)
        assert summary["total_settled_trades"] == 2
        assert summary["win_trades"] == 1
        assert summary["loss_trades"] == 1
        assert summary["win_rate_pct"] == 50.0
        assert summary["open_trades_count"] == 0

        # 5. 删除记录
        ok = svc.delete_trade(t1.id, db)
        assert ok is True
        assert db.query(PaperTrade).filter(PaperTrade.id == t1.id).first() is None
    finally:
        db.query(PaperTrade).delete()
        db.commit()
        db.close()
