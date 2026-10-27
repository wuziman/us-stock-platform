# -*- coding: utf-8 -*-
"""
大盘状态自适应权重寻优引擎单元测试：
1. 单纯形权重生成合法性（权重总和严格等于 1.0）
2. 历史四维因子标准化打分提取（0-10 范围与合理性）
3. 单一状态权重网格寻优与适应度函数
4. 严格的时序滚动盲测 (Walk-Forward Out-Of-Sample Testing)
"""

import numpy as np
import pandas as pd

from app.services.regime_adaptive_optimizer import (
    RegimeAdaptiveOptimizer,
    generate_candidate_simplex_weights,
    STATIC_BASELINE_WEIGHTS,
)


def test_generate_candidate_simplex_weights():
    """验证生成的权重组合中每个权重均非负，且四维权重总和严格等于 1.0"""
    simplex = generate_candidate_simplex_weights([0.10, 0.20, 0.30, 0.40, 0.50])
    assert len(simplex) > 0
    for w in simplex:
        assert set(w.keys()) == {"technical", "news", "macro", "event"}
        assert all(v >= 0.0 for v in w.values())
        assert abs(sum(w.values()) - 1.0) < 1e-4


def test_extract_four_factor_series_bounds():
    """验证四维因子打分序列提取后各维度均夹在 [0.0, 10.0] 合法区间内"""
    n = 100
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = np.linspace(100.0, 150.0, n)
    df = pd.DataFrame({
        "Open": close - 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": np.full(n, 1000000),
        "regime": ["BULL"] * 50 + ["BEAR"] * 50,
        "has_sec_8k": [False] * 90 + [True] * 10,
        "has_sec_report": [False] * 95 + [True] * 5,
    }, index=idx)

    opt = RegimeAdaptiveOptimizer()
    df_factors = opt.extract_four_factor_series(df)

    assert "score_tech" in df_factors.columns
    assert "score_macro" in df_factors.columns
    assert "score_event" in df_factors.columns
    assert "score_news" in df_factors.columns

    for col in ["score_tech", "score_macro", "score_event", "score_news"]:
        vals = df_factors[col].values
        assert np.all(vals >= 0.0)
        assert np.all(vals <= 10.0)

    # 验证牛市阶段宏观分显著高于熊市阶段
    mean_macro_bull = df_factors.iloc[:50]["score_macro"].mean()
    mean_macro_bear = df_factors.iloc[50:]["score_macro"].mean()
    assert mean_macro_bull > mean_macro_bear


def test_simulate_scoring_strategy_execution():
    """验证综合评分策略能正确模拟开仓与止盈止损"""
    n = 120
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = np.linspace(100.0, 200.0, n)

    df_factors = pd.DataFrame({
        "Open": close,
        "High": close + 2.0,
        "Low": close - 2.0,
        "Close": close,
        "score_tech": [8.0] * n,
        "score_macro": [8.0] * n,
        "score_event": [8.0] * n,
        "score_news": [8.0] * n,
    }, index=idx)

    opt = RegimeAdaptiveOptimizer()
    res = opt.simulate_scoring_strategy(df_factors, STATIC_BASELINE_WEIGHTS, buy_threshold=6.5)

    assert res["trade_count"] > 0
    assert res["win_rate"] >= 0.0
    assert "sharpe_ratio" in res
    assert "max_drawdown" in res
    assert "fitness" in res


def test_walk_forward_blind_test_execution():
    """验证多折 Walk-Forward 滚动盲测能稳定运行并输出对比汇总指标"""
    n = 300
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    # 模拟包含上涨与回调的波浪行情
    t = np.linspace(0, 4 * np.pi, n)
    trend = np.linspace(100, 200, n)
    wave = 15.0 * np.sin(t)
    close = trend + wave

    df_factors = pd.DataFrame({
        "Open": close - 0.5,
        "High": close + 2.0,
        "Low": close - 2.0,
        "Close": close,
        "Volume": np.full(n, 1000000),
        "regime": ["BULL"] * 100 + ["SIDEWAYS"] * 100 + ["BEAR"] * 100,
        "score_tech": np.random.uniform(4.0, 8.5, n),
        "score_macro": np.random.uniform(3.0, 9.0, n),
        "score_event": np.random.uniform(4.0, 7.0, n),
        "score_news": np.random.uniform(3.0, 8.0, n),
    }, index=idx)

    opt = RegimeAdaptiveOptimizer()
    wf_res = opt.run_walk_forward_blind_test(df_factors, folds=3, train_ratio=0.7)

    assert "total_folds" in wf_res
    assert wf_res["total_folds"] >= 2
    assert "overall_oos_comparison" in wf_res
    comp = wf_res["overall_oos_comparison"]
    assert "adaptive_avg_sharpe" in comp
    assert "static_avg_sharpe" in comp
    assert "adaptive_avg_winrate" in comp
