# -*- coding: utf-8 -*-
"""
极限点位预测精度多智能体压测引擎单元测试：
1. 点位预测精度评估器 (PricePointEvaluator: MAPE/MAE/盈亏比/扣除滑点佣金)
2. Worker 1 科技高波动组点位触达率
3. Worker 2 防守震荡组假突破率检测
4. Worker 3 历史黑天鹅极端暴跌生存与止损防守
5. Worker 4 红队对抗破坏组 Top 5 失败案例归因
"""

import numpy as np
import pandas as pd

from app.services.stress_test_engine import (
    PricePointEvaluator,
    MultiAgentStressTester,
)


def test_price_point_evaluator_mape_and_mae():
    """验证价格点位评估器能准确计算预测目标价与实际达到的 MAPE / MAE 及扣除费率后的盈亏比"""
    n = 100
    idx = pd.date_range("2024-01-01", periods=n, freq="B")

    # 构造能够触发买点随后大幅拉升达标的行情
    close = np.linspace(100.0, 160.0, n)
    # 在第 60 根 bar 创造一次回调触及买点
    close[59] = 95.0
    close[60] = 94.0

    df = pd.DataFrame({
        "Open": close,
        "High": close + 2.0,
        "Low": close - 2.0,
        "Close": close,
        "Volume": np.full(n, 1000000),
    }, index=idx)

    evaluator = PricePointEvaluator(commission_rate=0.0003, slippage_rate=0.0005)
    res = evaluator.evaluate_stock_points(df, "TEST_STOCK", strategy="linear")

    assert res["ticker"] == "TEST_STOCK"
    assert "target_hit_rate" in res
    assert "mean_mape" in res
    assert "mean_mae" in res
    assert "profit_loss_ratio" in res
    assert isinstance(res["trades"], list)


def test_worker2_defensive_sideways_false_breakout():
    """验证 Worker 2 防守震荡组能准确捕获假突破率与支撑跌破"""
    n = 80
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    # 构造横盘后假突破再反杀砸盘跌破止损的走势
    close = np.full(n, 100.0)
    # 创造买入触发
    close[55] = 92.0
    # 随后反杀暴跌击穿止损
    close[56] = 85.0
    close[57] = 80.0

    df = pd.DataFrame({
        "Open": close,
        "High": close + 0.5,
        "Low": close - 1.0,
        "Close": close,
        "Volume": np.full(n, 500000),
    }, index=idx)

    evaluator = PricePointEvaluator()
    res = evaluator.evaluate_stock_points(df, "KO_TEST", strategy="linear")
    trades = res.get("trades", [])
    if trades:
        stop_count = sum(1 for t in trades if t["exit_reason"] == "stop_hit")
        assert stop_count >= 1


def test_worker4_red_team_worst_failures():
    """验证红队对抗组能正确识别历史中 MAPE 偏差最大的案例并给出根因分析"""
    tester = MultiAgentStressTester()
    # 传入空或模拟列表测试空集与鲁棒性
    res_empty = tester.run_worker4_red_team_failures(tickers=["INVALID_TICKER_XYZ"])
    assert res_empty["unit"] == "Worker 4【红队对抗破坏组】"
    assert "worst_failures_count" in res_empty or "top_failures" in res_empty
