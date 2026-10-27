# -*- coding: utf-8 -*-
"""
A 股 100 股整手（1手）交易规则单元测试：
1. 智能建仓计算器 (calculate_recommended_position) 对 A 股强制 100 股整手向下取整，美股不受影响
2. 资金不足 1 手时的友好提示 (lot_warning)
3. 回测引擎 (_execute_backtest) 在 A 股标的上所有买入成交必须是 100 的整数倍
"""

import pandas as pd
import numpy as np

from app.services.portfolio_service import calculate_recommended_position
from app.services.backtest_service import BacktestService


def test_recommended_position_a_share_lot_rounding():
    """验证 A 股建仓推荐股数必须向下取整到 100 股的整数倍，美股不受影响"""
    # 1. 美股标的 (AAPL)：任意正整数组合（例如 187 股，不强制 100 的倍数）
    res_us = calculate_recommended_position(
        capital=100000.0,
        buy_price=107.50,
        stop_loss=98.90,
        stock_code="AAPL",
        sizing_mode="fixed_2pct",
    )
    assert res_us["lot_size"] == 1
    assert res_us["lot_warning"] is None
    # 2% 风险 = $2000, 每股风险 = 8.60, 2000 / 8.60 = 232 股
    assert res_us["recommended_shares"] == 232
    assert res_us["recommended_shares"] % 100 != 0  # 美股不需要是 100 的倍数

    # 2. A 股标的 (002594 比亚迪)：按整手 100 股向下取整
    res_a = calculate_recommended_position(
        capital=100000.0,
        buy_price=107.50,
        stop_loss=98.90,
        stock_code="002594",
        sizing_mode="fixed_2pct",
    )
    assert res_a["lot_size"] == 100
    assert res_a["lot_warning"] is None
    # 原 232 股向下取整为 200 股 (2手)
    assert res_a["recommended_shares"] == 200
    assert res_a["recommended_shares"] % 100 == 0
    assert res_a["first_tranche_shares"] % 100 == 0

    # 3. A 股标的资金不足 1 手 (100股)：返回 0 股并给出友好提示
    res_small_cap = calculate_recommended_position(
        capital=10000.0,  # 仅 1 万元资金
        buy_price=1500.0, # 贵州茅台 1500 元/股，1手需要 15 万元
        stop_loss=1380.0,
        stock_code="600519",
        sizing_mode="fixed_2pct",
    )
    assert res_small_cap["lot_size"] == 100
    assert res_small_cap["recommended_shares"] == 0
    assert res_small_cap["lot_warning"] is not None
    assert "不足1手" in res_small_cap["lot_warning"]


def test_backtest_a_share_lot_size_enforcement():
    """验证回测引擎在 A 股代码上执行时，所有成交股数必须是 100 的整数倍"""
    # 构造能够触发均线金叉的合成行情
    n = 90
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    close = np.linspace(50.0, 80.0, n)
    df = pd.DataFrame({
        "Open": close,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": 1_000_000,
    }, index=idx)
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA50"] = df["Close"].rolling(50).mean()

    # 注入金叉信号：在第 60 根 bar 让 MA20 上穿 MA50
    df.loc[df.index[59], "MA20"] = 60.0
    df.loc[df.index[59], "MA50"] = 61.0
    df.loc[df.index[60], "MA20"] = 62.0
    df.loc[df.index[60], "MA50"] = 61.0

    svc = BacktestService()

    # 1. A 股代码：600519
    res_a = svc._execute_backtest(
        df=df,
        strategy="ma_cross",
        stock_code="600519",
        initial_capital=50000.0,
        period="1y",
    )
    assert "trades" in res_a
    buy_trades_a = [t for t in res_a["trades"] if t["action"] == "buy"]
    assert len(buy_trades_a) > 0
    for t in buy_trades_a:
        assert t["shares"] % 100 == 0, f"A股买入股数必须是100的整数倍: {t['shares']}"

    # 2. 美股代码：MU
    res_us = svc._execute_backtest(
        df=df,
        strategy="ma_cross",
        stock_code="MU",
        initial_capital=50000.0,
        period="1y",
    )
    buy_trades_us = [t for t in res_us["trades"] if t["action"] == "buy"]
    assert len(buy_trades_us) > 0
    # 美股可以是非 100 整数倍（50000 * 0.95 / ~62 = ~766 股）
    assert any(t["shares"] % 100 != 0 for t in buy_trades_us)
