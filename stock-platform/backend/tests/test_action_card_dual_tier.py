# -*- coding: utf-8 -*-
"""
战役二测试：周线底座注入决策卡、双买点分级操盘体系与截面全量优先级算法
验收指标：
1. 谷歌 (GOOGL) 实盘破位场景下自动挂载周线双底座 (TIERED_DIP_BUY)
   - buy_tier1 落在 $290~$295，建议仓位 10%
   - buy_tier2 落在 $270~$272，建议仓位 15%
   - 总规划仓位 25%，止损后移至铁底下方
   - 告别机械置 0，输出专业分级埋伏指引
2. 截面综合优先级算法与四维质地门禁 (0~100分)
   - 质地 < 4.0 门禁拦截 (分数为 0)
   - 质地高且底座安全边际大的标的获得高优先级
3. 端到端集成验证：analyze_stock 完整输出双买点决策卡与截面优先级
"""

import numpy as np
import pandas as pd

from app.services.analysis_service import AnalysisService


def test_googl_dual_tier_action_card():
    """
    验收项 1：谷歌 (GOOGL) 暴跌破位场景下，接驳周线双底座
    输入：现价 $291.20，原日线止损 $292.54 已破位
    """
    weekly_support = {
        "ticker": "GOOGL",
        "current_price": 291.20,
        "tier_1": 294.87,
        "tier_2": 270.81,
        "dist_to_tier2_pct": 7.0,
        "atr14_wk": 19.61,
        "is_leveraged": False,
    }
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
        current_price=291.20,
        total_score=4.9,
        price_levels=price_levels,
        recommendation=recommendation,
        earnings_radar=earnings_radar,
        stock_code="GOOGL",
        weekly_support=weekly_support,
        macro_score=5.0,
    )

    # 1. 动作类型与标签：分级埋伏状态机
    assert card["action_type"] == "TIERED_DIP_BUY"
    assert card["action_label"] == "周线底座回踩·分级埋伏"

    # 2. 双买点数值红线验证
    assert 290.0 <= card["buy_tier1"] <= 295.0
    assert 270.0 <= card["buy_tier2"] <= 272.0
    assert card["buy_price"] == card["buy_tier1"]

    # 3. 分级仓位配比验证
    assert card["tier1_position_pct"] == "10%"
    assert card["tier2_position_pct"] == "15%"
    assert card["total_position_pct"] == "25%"

    # 4. 止损后移至 Tier 2 铁底下方缓冲位 (270.81 * 0.96 ≈ 259.98)
    assert card["stop_loss_price"] < 270.81
    assert card["stop_loss_price"] == round(270.81 * 0.96, 2)

    # 5. 一句话大白话操盘指南
    assert "周线第一底座" in card["one_sentence_reason"]
    assert "10%" in card["one_sentence_reason"]
    assert "52周年度大底" in card["one_sentence_reason"]
    assert "15%" in card["one_sentence_reason"]

    # 6. 截面综合优先级评分存在且为合理数值
    assert card["priority_score"] is not None
    assert 50.0 <= card["priority_score"] <= 100.0


def test_cross_sectional_priority_scoring_and_gatekeeper():
    """
    验收项 2：截面优先级评分公式与四维门禁验证
    """
    weekly_supp = {
        "tier_1": 100.0,
        "tier_2": 90.0,
        "dist_to_tier2_pct": 10.0,
    }

    # 1. 门禁验证：四维评分低于 4.0 分强制归 0 淘汰
    card_low_quality = AnalysisService._build_action_card(
        current_price=100.0,
        total_score=3.8,  # < 4.0
        price_levels={"linear": {"buy": 95.0, "stop": 88.0, "profit": 115.0}},
        recommendation={"action": "卖出", "level": "不推荐"},
        earnings_radar={},
        stock_code="JUNK",
        weekly_support=weekly_supp,
    )
    assert card_low_quality["priority_score"] == 0.0

    # 2. 正常标的优先级计算
    # 标的 A：质地 8.0 分，距铁底 5% (安全边际大)
    card_a = AnalysisService._build_action_card(
        current_price=100.0,
        total_score=8.0,
        price_levels={"linear": {"buy": 98.0, "stop": 90.0, "profit": 120.0}},
        recommendation={"action": "买入", "level": "推荐"},
        earnings_radar={},
        stock_code="TOP_A",
        weekly_support={"tier_1": 98.0, "tier_2": 95.0, "dist_to_tier2_pct": 5.0},
        macro_score=7.0,
    )

    # 标的 B：质地 6.0 分，距铁底 40% (安全边际小)
    card_b = AnalysisService._build_action_card(
        current_price=100.0,
        total_score=6.0,
        price_levels={"linear": {"buy": 95.0, "stop": 85.0, "profit": 115.0}},
        recommendation={"action": "买入", "level": "推荐"},
        earnings_radar={},
        stock_code="MID_B",
        weekly_support={"tier_1": 90.0, "tier_2": 60.0, "dist_to_tier2_pct": 40.0},
        macro_score=5.0,
    )

    assert card_a["priority_score"] > card_b["priority_score"]
    assert card_a["priority_score"] >= 80.0


def test_analyze_stock_end_to_end_dual_tier(monkeypatch):
    """
    验收项 3：analyze_stock 端到端集成测试，验证周线双底座自动注入
    """
    svc = AnalysisService()

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
    # 模拟盘前现价 291.20
    monkeypatch.setattr(
        svc.stock_service,
        "get_realtime_quote",
        lambda *args, **kwargs: {"code": "GOOGL", "price": 291.20, "session_type": "PRE"}
    )
    # 模拟周线服务返回双底座
    monkeypatch.setattr(
        svc.weekly_support_service,
        "calculate_weekly_supports",
        lambda *args, **kwargs: {
            "ticker": "GOOGL",
            "current_price": 291.20,
            "tier_1": 294.87,
            "tier_2": 270.81,
            "dist_to_tier2_pct": 7.0,
            "atr14_wk": 19.61,
            "is_leveraged": False,
        }
    )
    monkeypatch.setattr(svc, "_analyze_news", lambda *args: (6.0, {}))
    monkeypatch.setattr(svc, "_analyze_macro", lambda *args: (5.0, {}))
    monkeypatch.setattr(svc, "_analyze_events", lambda *args: (5.0, {}))

    res = svc.analyze_stock("GOOGL", "Alphabet", market="US")
    card = res["action_card"]

    assert card is not None
    assert card["action_type"] == "TIERED_DIP_BUY"
    assert card["action_label"] == "周线底座回踩·分级埋伏"
    assert card["buy_tier1"] == 294.87
    assert card["buy_tier2"] == 270.81
    assert card["total_position_pct"] == "25%"
    assert card["priority_score"] is not None
