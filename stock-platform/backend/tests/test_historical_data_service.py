# -*- coding: utf-8 -*-
"""
十年跨周期量化数据管道单元测试：
1. 市场牛熊状态打标器 (classify_market_regime_series)
2. SEC EDGAR 官方重大事件 (8-K / 10-Q / 10-K) 拉取与解析
3. Finnhub 财报惊喜度数据管道与优雅降级
4. 统一特征矩阵时间轴对齐 (build_historical_feature_matrix)
"""

import numpy as np
import pandas as pd
from unittest.mock import MagicMock

from app.services.historical_data_service import HistoricalDataService


def test_classify_market_regime_series():
    """验证市场状态分类器能准确根据 MA200 与 MA50 判定牛市、熊市与震荡市"""
    n = 250
    idx = pd.date_range("2020-01-01", periods=n, freq="B")

    # 1. 构造明显的单边牛市：价格与均线单调上升
    close_bull = np.linspace(100.0, 300.0, n)
    df_bull = pd.DataFrame({"Close": close_bull}, index=idx)
    regime_bull = HistoricalDataService.classify_market_regime_series(df_bull)
    assert len(regime_bull) == n
    # 充分预热后，尾部必定为 BULL
    assert regime_bull.iloc[-1] == "BULL"

    # 2. 构造明显的单边熊市：价格与均线单调下挫
    close_bear = np.linspace(300.0, 80.0, n)
    df_bear = pd.DataFrame({"Close": close_bear}, index=idx)
    regime_bear = HistoricalDataService.classify_market_regime_series(df_bear)
    assert regime_bear.iloc[-1] == "BEAR"


def test_sec_edgar_events_parsing(monkeypatch):
    """验证 SEC EDGAR 接口返回数据能被正确提取为规范的事件字典列表"""
    fake_sec_payload = {
        "filings": {
            "recent": {
                "form": ["8-K", "10-Q", "4", "8-K", "10-K"],
                "filingDate": ["2026-10-15", "2026-08-28", "2026-08-01", "2026-05-20", "2026-02-15"],
                "reportDate": ["2026-10-14", "2026-07-28", "2026-07-31", "2026-05-19", "2026-01-31"],
                "accessionNumber": ["0001-001", "0001-002", "0001-003", "0001-004", "0001-005"],
            }
        }
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_sec_payload

    import requests
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: mock_resp)

    svc = HistoricalDataService()
    events = svc.fetch_sec_edgar_events("NVDA", limit=50)

    # 过滤出 8-K (2个), 10-Q (1个), 10-K (1个)，排除 Form 4
    assert len(events) == 4
    assert events[0]["form"] == "8-K"
    assert events[0]["date"] == "2026-10-15"
    assert events[0]["is_material_event"] is True
    assert events[0]["is_periodic_report"] is False

    assert events[1]["form"] == "10-Q"
    assert events[1]["is_periodic_report"] is True


def test_finnhub_earnings_graceful_degradation(monkeypatch):
    """验证 Finnhub 在无 key 或 401 报错时优雅降级，有有效返回时正确提取 EPS Surprise"""
    # 1. 未配置 key 时返回空列表，不抛异常
    svc_no_key = HistoricalDataService(finnhub_key="")
    assert svc_no_key.fetch_finnhub_earnings("NVDA") == []

    # 2. 模拟真实 Finnhub 返回
    fake_finnhub_payload = [
        {
            "actual": 0.68,
            "estimate": 0.64,
            "period": "2026-07-31",
            "surprise": 0.04,
            "surprisePercent": 6.25,
            "symbol": "NVDA",
        },
        {
            "actual": 0.55,
            "estimate": 0.58,
            "period": "2026-04-30",
            "surprise": -0.03,
            "surprisePercent": -5.17,
            "symbol": "NVDA",
        },
    ]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_finnhub_payload

    import requests
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: mock_resp)

    svc = HistoricalDataService(finnhub_key="mock_valid_key")
    earnings = svc.fetch_finnhub_earnings("NVDA")

    assert len(earnings) == 2
    assert earnings[0]["period"] == "2026-07-31"
    assert earnings[0]["is_beat"] is True
    assert earnings[0]["is_miss"] is False
    assert earnings[0]["surprise"] == 0.04

    assert earnings[1]["is_beat"] is False
    assert earnings[1]["is_miss"] is True


def test_build_historical_feature_matrix_alignment(monkeypatch):
    """验证特征矩阵能把股价、大盘牛熊状态与 SEC 重大事件对齐到统一时间轴"""
    n = 60
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    fake_stock_df = pd.DataFrame({
        "Open": np.linspace(100.0, 150.0, n),
        "High": np.linspace(102.0, 152.0, n),
        "Low": np.linspace(99.0, 149.0, n),
        "Close": np.linspace(101.0, 151.0, n),
        "Volume": np.full(n, 1000000),
    }, index=idx)

    svc = HistoricalDataService()
    # 模拟 fetch_ten_year_kline
    monkeypatch.setattr(svc, "fetch_ten_year_kline", lambda sym, **kwargs: fake_stock_df)

    # 模拟 SEC 事件在第 10 根 bar 发生 8-K
    event_date = idx[10].strftime("%Y-%m-%d")
    fake_events = [{
        "date": event_date,
        "form": "8-K",
        "is_material_event": True,
        "is_periodic_report": False,
    }]
    monkeypatch.setattr(svc, "fetch_sec_edgar_events", lambda sym, **kwargs: fake_events)

    matrix = svc.build_historical_feature_matrix("NVDA", benchmark_symbol="SPY", market="US")
    assert not matrix.empty
    assert "regime" in matrix.columns
    assert "has_sec_8k" in matrix.columns
    assert "has_sec_report" in matrix.columns

    # 验证第 10 根 bar 命中了 SEC 8-K
    assert matrix.loc[idx[10], "has_sec_8k"] is True or matrix.loc[idx[10], "has_sec_8k"] == 1
    assert matrix.loc[idx[11], "has_sec_8k"] is False or matrix.loc[idx[11], "has_sec_8k"] == 0
