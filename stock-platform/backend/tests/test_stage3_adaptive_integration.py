# -*- coding: utf-8 -*-
"""
阶段三集成测试：实盘综合打分引擎牛熊自适应接入与前后端规格验证
1. analyze_stock 输出包含 regime_adaptive 结构
2. 大盘状态 (BULL/BEAR/SIDEWAYS) 与自适应权重和严格等于 1.0
3. mode="adaptive" 与 mode="simple" / mode="research" 评分路由切换
"""

import numpy as np
import pandas as pd

from app.services.analysis_service import AnalysisService


def test_analyze_stock_regime_adaptive_structure(monkeypatch):
    """验证 analyze_stock 综合分析返回完整的自适应大盘状态与权重配比"""
    svc = AnalysisService()

    # 构造合成日K与指标
    n = 60
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    close = np.linspace(100.0, 150.0, n)
    df_fake = pd.DataFrame({
        "Open": close - 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": np.full(n, 1000000),
    }, index=idx)

    monkeypatch.setattr(svc.stock_service, "get_stock_data", lambda *args, **kwargs: df_fake)
    monkeypatch.setattr(svc, "_analyze_news", lambda *args: (7.0, {"sentiment": "偏多"}))
    monkeypatch.setattr(svc, "_analyze_macro", lambda *args: (8.0, {"indicators": {}}))
    monkeypatch.setattr(svc, "_analyze_events", lambda *args: (6.0, {"events": []}))

    res = svc.analyze_stock("MU", "Micron", market="US", mode="simple")

    assert "regime_adaptive" in res
    ra = res["regime_adaptive"]
    assert ra is not None
    assert ra["regime"] in ("BULL", "BEAR", "SIDEWAYS")
    assert ra["mode_desc"] in ("牛市动量增强模式", "熊市防御风控模式", "震荡市平衡模式")

    weights = ra["weights"]
    assert set(weights.keys()) == {"technical", "news", "macro", "event"}
    assert abs(sum(weights.values()) - 1.0) < 1e-4

    assert "adaptive_total_score" in ra
    assert "static_total_score" in ra
    assert "score_diff" in ra
    assert round(ra["adaptive_total_score"] - ra["static_total_score"], 1) == ra["score_diff"]

    assert "action_card" in res
    ac = res["action_card"]
    assert ac is not None
    assert ac["current_price"] > 0
    assert ac["buy_price"] > 0
    assert ac["take_profit_price"] > 0
    assert ac["stop_loss_price"] > 0
    assert ac["position_pct"] != ""
    assert ac["action_label"] != ""
    assert ac["one_sentence_reason"] != ""


def test_analyze_stock_mode_switching(monkeypatch):
    """验证 adaptive 模式与 research 模式正确切换生效权重"""
    svc = AnalysisService()

    n = 60
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    close = np.linspace(100.0, 150.0, n)
    df_fake = pd.DataFrame({
        "Open": close - 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": np.full(n, 1000000),
    }, index=idx)

    monkeypatch.setattr(svc.stock_service, "get_stock_data", lambda *args, **kwargs: df_fake)
    monkeypatch.setattr(svc, "_analyze_news", lambda *args: (6.0, {}))
    monkeypatch.setattr(svc, "_analyze_macro", lambda *args: (7.0, {}))
    monkeypatch.setattr(svc, "_analyze_events", lambda *args: (5.0, {}))

    # 1. 显式 adaptive 模式
    res_adapt = svc.analyze_stock("MU", "Micron", market="US", mode="adaptive")
    assert res_adapt["scores"]["total"] == res_adapt["regime_adaptive"]["adaptive_total_score"]

    # 2. research 模式
    res_research = svc.analyze_stock("MU", "Micron", market="US", mode="research")
    assert res_research["scores"]["total"] == res_research["regime_adaptive"]["static_total_score"]
