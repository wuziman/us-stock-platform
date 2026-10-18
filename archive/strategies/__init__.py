"""策略模块"""
from .trend_following import (
    DualMovingAverage,
    MACDStrategy,
    RSIStrategy,
    SuperTrendStrategy,
    TurtleStrategy,
    MomentumStrategy,
    backtest_strategy
)
from .multi_factor import MultiFactorStrategy, MultiStockSelector, CombinedStrategy
