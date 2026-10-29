# -*- coding: utf-8 -*-
"""
测试恶性跳空破位断路器与美股盘前/盘后实时价格探测
1. 谷歌 $292.05 跳空破位死卡验收 (跌破止损熔断，买点归零)
2. 跌破初级买点支撑但未破止损的缓冲状态验收 (观望企稳)
3. 美股盘前/盘后行情穿透探测与 session_type 返回
4. 综合分析 analyze_stock 穿透盘前暴跌端到端拦截验收
"""

import numpy as np
import pandas as pd

from app.services.analysis_service import AnalysisService
from app.services.stock_service import StockService


def test_gap_down_circuit_breaker_googl():
    """
    目标一验收：现价击穿止损线时的熔断死卡
    输入：现价 $292.05，原止损线 $292.54，买点 $308.65
    """
    current_price = 292.05
    total_score = 4.9
    price_levels = {
        "linear": {
            "buy": 308.65,
            "stop": 283.96,
            "profit": 354.95,
            "atr_stop": 292.54,
            "atr_profit": 354.95,
        }
    }
    recommendation = {"action": "观望", "level": "谨慎"}
    earnings_radar = {"is_imminent": False}

    card = AnalysisService._build_action_card(
        current_price=current_price,
        total_score=total_score,
        price_levels=price_levels,
        recommendation=recommendation,
        earnings_radar=earnings_radar,
        stock_code="GOOGL",
    )

    # 1. 动作类型与标签
    assert card["action_type"] == "AVOID_GAP_DOWN"
    assert card["action_label"] == "🚨 跌破止损·严禁接刀"
    # 2. 建议仓位归零
    assert card["position_pct"] == "0%"
    # 3. 买入价彻底报废归零
    assert card["buy_price"] == 0
    # 4. 严禁接飞刀警示语
    assert "现价已直接击穿原止损线，恶性跳空破位，原买点报废失效，严禁抄底接飞刀" in card["one_sentence_reason"]
    # 5. 现价与止损价保留
    assert card["current_price"] == 292.05
    assert card["stop_loss_price"] == 292.54


def test_break_support_buffer_state():
    """
    目标二验收：现价跌破初级买点支撑但尚未击穿止损线的缓冲状态
    输入：现价 $300.00，买点 $308.65，止损线 $292.54
    """
    current_price = 300.00
    total_score = 6.8
    price_levels = {
        "linear": {
            "buy": 308.65,
            "stop": 283.96,
            "profit": 354.95,
            "atr_stop": 292.54,
            "atr_profit": 354.95,
        }
    }
    recommendation = {"action": "买入", "level": "推荐"}
    earnings_radar = {"is_imminent": False}

    card = AnalysisService._build_action_card(
        current_price=current_price,
        total_score=total_score,
        price_levels=price_levels,
        recommendation=recommendation,
        earnings_radar=earnings_radar,
        stock_code="GOOGL",
    )

    # 1. 标签自动调整为 跌破初级支撑·观望企稳
    assert card["action_label"] == "跌破初级支撑·观望企稳"
    # 2. 仓位保持 0%，防止盲目接盘
    assert card["position_pct"] == "0%"
    # 3. 提示正在测试下轨，严禁盲目挂单
    assert "正在测试下轨" in card["one_sentence_reason"]
    assert "严禁盲目挂单" in card["one_sentence_reason"]


def test_stock_service_premarket_and_postmarket(monkeypatch):
    """
    验证 StockService 美股实时报价穿透探测盘前 (PRE) 和盘后 (POST) 价格
    """
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            return {
                "marketState": "PRE",
                "regularMarketPrice": 324.89,
                "regularMarketPreviousClose": 320.58,
                "preMarketPrice": 292.05,
                "preMarketChange": -32.84,
                "preMarketChangePercent": -10.11,
                "shortName": "Alphabet Inc.",
                "volume": 50000000,
                "marketCap": 3900000000000,
            }

    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", FakeTicker)

    quote = StockService.get_realtime_quote("GOOGL", "US")
    assert quote is not None
    assert quote["price"] == 292.05
    assert quote["session_type"] == "PRE"
    assert quote["market_state"] == "PRE"
    assert round(quote["change_pct"], 1) == -10.1

    # 验证盘后 (POST) 状态穿透
    class FakePostTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            return {
                "marketState": "POST",
                "regularMarketPrice": 324.89,
                "postMarketPrice": 310.50,
                "postMarketChange": -14.39,
                "postMarketChangePercent": -4.43,
                "shortName": "Alphabet Inc.",
            }

    monkeypatch.setattr(yf, "Ticker", FakePostTicker)
    quote_post = StockService.get_realtime_quote("GOOGL", "US")
    assert quote_post is not None
    assert quote_post["price"] == 310.50
    assert quote_post["session_type"] == "POST"


def test_analyze_stock_with_premarket_gap_down(monkeypatch):
    """
    端到端集成测试：analyze_stock 在遇到盘前跳空暴跌时，
    即使日K Close 滞后停留在 $324.89，也能通过实时穿透探测到 $292.05 并触发熔断
    """
    svc = AnalysisService()

    # 构造历史K线（最近一根收盘价 324.89）
    n = 60
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    close = np.linspace(300.0, 324.89, n)
    df_fake = pd.DataFrame({
        "Open": close - 1.0,
        "High": close + 2.0,
        "Low": close - 2.0,
        "Close": close,
        "Volume": np.full(n, 2000000),
    }, index=idx)

    monkeypatch.setattr(svc.stock_service, "get_stock_data", lambda *args, **kwargs: df_fake)
    # 模拟盘前暴跌至 292.05
    monkeypatch.setattr(
        svc.stock_service,
        "get_realtime_quote",
        lambda *args, **kwargs: {
            "code": "GOOGL",
            "price": 292.05,
            "session_type": "PRE",
            "market_state": "PRE",
        }
    )
    monkeypatch.setattr(svc, "_analyze_news", lambda *args: (6.0, {}))
    monkeypatch.setattr(svc, "_analyze_macro", lambda *args: (5.0, {}))
    monkeypatch.setattr(svc, "_analyze_events", lambda *args: (5.0, {}))
    # 模拟未取得周线底座数据时的保底硬熔断
    monkeypatch.setattr(svc.weekly_support_service, "calculate_weekly_supports", lambda *args, **kwargs: None)

    res = svc.analyze_stock("GOOGL", "Alphabet", market="US")
    card = res["action_card"]

    assert card is not None
    assert card["current_price"] == 292.05
    assert card["action_type"] == "AVOID_GAP_DOWN"
    assert card["action_label"] == "🚨 跌破止损·严禁接刀"
    assert card["buy_price"] == 0
    assert card["position_pct"] == "0%"
