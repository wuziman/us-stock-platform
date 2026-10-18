"""
趋势跟踪策略模块
包含多个经典的趋势跟踪策略
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from utils.indicators import (
    calculate_sma, calculate_ema, calculate_rsi, calculate_macd,
    calculate_atr, calculate_supertrend
)


class TrendFollowingStrategy:
    """趋势跟踪策略基类"""

    def __init__(self, initial_capital: float = 100000):
        self.initial_capital = initial_capital

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成交易信号（子类实现）"""
        raise NotImplementedError

    def calculate_position_size(
        self,
        capital: float,
        price: float,
        risk_per_trade: float = 0.02,
        stop_loss_pct: float = 0.05
    ) -> int:
        """
        计算仓位大小（基于风险）

        参数:
            capital: 当前资金
            price: 当前价格
            risk_per_trade: 每笔交易风险比例（默认2%）
            stop_loss_pct: 止损百分比（默认5%）
        """
        risk_amount = capital * risk_per_trade
        stop_loss_amount = price * stop_loss_pct
        shares = int(risk_amount / stop_loss_amount)
        return max(1, shares)


class DualMovingAverage(TrendFollowingStrategy):
    """
    双均线交叉策略
    买入：短期均线上穿长期均线
    卖出：短期均线下穿长期均线
    """

    def __init__(
        self,
        short_period: int = 20,
        long_period: int = 50,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.short_period = short_period
        self.long_period = long_period

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # 计算均线
        df['SMA_Short'] = calculate_sma(df, self.short_period)
        df['SMA_Long'] = calculate_sma(df, self.long_period)

        # 生成信号
        df['Signal'] = 0
        df.loc[df['SMA_Short'] > df['SMA_Long'], 'Signal'] = 1
        df.loc[df['SMA_Short'] < df['SMA_Long'], 'Signal'] = -1

        # 交易信号（信号变化时）
        df['Trade'] = df['Signal'].diff()

        return df


class MACDStrategy(TrendFollowingStrategy):
    """
    MACD策略
    买入：MACD金叉（MACD线上穿信号线）
    卖出：MACD死叉（MACD线下穿信号线）
    """

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.fast = fast
        self.slow = slow
        self.signal = signal

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # 计算MACD
        df['MACD'], df['MACD_Signal'], df['MACD_Hist'] = calculate_macd(
            df, self.fast, self.slow, self.signal
        )

        # 生成信号
        df['Signal'] = 0
        df.loc[df['MACD'] > df['MACD_Signal'], 'Signal'] = 1
        df.loc[df['MACD'] < df['MACD_Signal'], 'Signal'] = -1

        # 交易信号
        df['Trade'] = df['Signal'].diff()

        return df


class RSIStrategy(TrendFollowingStrategy):
    """
    RSI策略
    买入：RSI从超卖区（<30）回升
    卖出：RSI从超买区（>70）回落
    """

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30,
        overbought: float = 70,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # 计算RSI
        df['RSI'] = calculate_rsi(df, self.period)

        # 生成信号
        df['Signal'] = 0
        df.loc[df['RSI'] < self.oversold, 'Signal'] = 1
        df.loc[df['RSI'] > self.overbought, 'Signal'] = -1

        # 保持仓位直到反向信号
        df['Signal'] = df['Signal'].replace(0, np.nan).ffill().fillna(0)

        # 交易信号
        df['Trade'] = df['Signal'].diff()

        return df


class SuperTrendStrategy(TrendFollowingStrategy):
    """
    超级趋势策略
    基于ATR的趋势跟踪策略，对趋势市场效果很好
    """

    def __init__(
        self,
        period: int = 10,
        multiplier: float = 3.0,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.period = period
        self.multiplier = multiplier

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # 计算ATR
        df['ATR'] = calculate_atr(df, self.period)

        # 计算基础线
        hl2 = (df['High'] + df['Low']) / 2
        df['UpperBand'] = hl2 + (self.multiplier * df['ATR'])
        df['LowerBand'] = hl2 - (self.multiplier * df['ATR'])

        # 初始化
        df['SuperTrend'] = 0.0
        df['Signal'] = 0

        # 计算超级趋势
        for i in range(1, len(df)):
            if df['Close'].iloc[i] > df['UpperBand'].iloc[i-1]:
                df.loc[df.index[i], 'SuperTrend'] = df['LowerBand'].iloc[i]
                df.loc[df.index[i], 'Signal'] = 1
            elif df['Close'].iloc[i] < df['LowerBand'].iloc[i-1]:
                df.loc[df.index[i], 'SuperTrend'] = df['UpperBand'].iloc[i]
                df.loc[df.index[i], 'Signal'] = -1
            else:
                df.loc[df.index[i], 'SuperTrend'] = df['SuperTrend'].iloc[i-1]
                df.loc[df.index[i], 'Signal'] = df['Signal'].iloc[i-1]

        df['Trade'] = df['Signal'].diff()
        return df


class TurtleStrategy(TrendFollowingStrategy):
    """
    海龟交易策略
    经典的趋势跟踪系统，使用20日/55日突破
    """

    def __init__(
        self,
        entry_period: int = 20,
        exit_period: int = 10,
        atr_period: int = 20,
        risk_per_trade: float = 0.01,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.entry_period = entry_period
        self.exit_period = exit_period
        self.atr_period = atr_period
        self.risk_per_trade = risk_per_trade

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # 计算通道
        df['Entry_High'] = df['High'].rolling(window=self.entry_period).max()
        df['Entry_Low'] = df['Low'].rolling(window=self.entry_period).min()
        df['Exit_High'] = df['High'].rolling(window=self.exit_period).max()
        df['Exit_Low'] = df['Low'].rolling(window=self.exit_period).min()

        # ATR
        df['ATR'] = calculate_atr(df, self.atr_period)

        # 生成信号
        df['Signal'] = 0
        position = 0

        for i in range(max(self.entry_period, self.atr_period), len(df)):
            if position == 0:
                # 开仓条件
                if df['Close'].iloc[i] > df['Entry_High'].iloc[i-1]:
                    position = 1
                elif df['Close'].iloc[i] < df['Entry_Low'].iloc[i-1]:
                    position = -1
            elif position == 1:
                # 多头平仓
                if df['Close'].iloc[i] < df['Exit_Low'].iloc[i-1]:
                    position = 0
            elif position == -1:
                # 空头平仓
                if df['Close'].iloc[i] > df['Exit_High'].iloc[i-1]:
                    position = 0

            df.loc[df.index[i], 'Signal'] = position

        df['Trade'] = df['Signal'].diff()
        return df


class MomentumStrategy(TrendFollowingStrategy):
    """
    动量策略
    买入过去N日涨幅最大的股票
    """

    def __init__(
        self,
        lookback: int = 20,
        holding_period: int = 5,
        top_n: int = 5,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.lookback = lookback
        self.holding_period = holding_period
        self.top_n = top_n

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # 计算动量分数
        df['Momentum'] = df['Close'].pct_change(periods=self.lookback)

        # 生成信号
        df['Signal'] = 0
        df.loc[df['Momentum'] > 0, 'Signal'] = 1
        df.loc[df['Momentum'] < 0, 'Signal'] = -1

        df['Trade'] = df['Signal'].diff()
        return df


def backtest_strategy(
    df: pd.DataFrame,
    strategy: TrendFollowingStrategy,
    commission: float = 0.001,
    slippage: float = 0.001
) -> Tuple[pd.DataFrame, Dict]:
    """
    回测策略

    参数:
        df: 原始数据
        strategy: 策略实例
        commission: 手续费率
        slippage: 滑点

    返回:
        results_df: 包含回测结果的DataFrame
        stats: 统计信息
    """
    # 生成信号
    df = strategy.generate_signals(df.copy())

    # 初始化
    capital = strategy.initial_capital
    position = 0
    entry_price = 0
    trades = []

    df['Capital'] = capital
    df['Position'] = 0
    df['Holdings'] = 0
    df['Total'] = capital

    for i in range(1, len(df)):
        trade_signal = df['Trade'].iloc[i]
        current_price = df['Close'].iloc[i]

        # 买入
        if trade_signal == 1 and position == 0:
            shares = int(capital * 0.95 / current_price)  # 留5%现金
            if shares > 0:
                cost = shares * current_price * (1 + commission + slippage)
                if cost <= capital:
                    position = shares
                    entry_price = current_price
                    capital -= cost

        # 卖出
        elif trade_signal == -1 and position > 0:
            revenue = position * current_price * (1 - commission - slippage)
            capital += revenue
            trades.append({
                'entry_price': entry_price,
                'exit_price': current_price,
                'return': (current_price - entry_price) / entry_price
            })
            position = 0

        # 更新
        df.loc[df.index[i], 'Capital'] = capital
        df.loc[df.index[i], 'Position'] = position
        df.loc[df.index[i], 'Holdings'] = position * current_price
        df.loc[df.index[i], 'Total'] = capital + position * current_price

    # 计算统计
    stats = calculate_stats(df, trades, strategy.initial_capital)

    return df, stats


def calculate_stats(
    df: pd.DataFrame,
    trades: List[Dict],
    initial_capital: float
) -> Dict:
    """计算回测统计"""
    # 基本统计
    final_value = df['Total'].iloc[-1]
    total_return = (final_value - initial_capital) / initial_capital
    years = len(df) / 252
    annual_return = (1 + total_return) ** (1/years) - 1 if years > 0 else 0

    # 最大回撤
    peak = df['Total'].expanding(min_periods=1).max()
    drawdown = (df['Total'] - peak) / peak
    max_drawdown = drawdown.min()

    # 夏普比率
    daily_returns = df['Total'].pct_change().dropna()
    if daily_returns.std() > 0:
        sharpe_ratio = np.sqrt(252) * (daily_returns.mean() / daily_returns.std())
    else:
        sharpe_ratio = 0

    # 交易统计
    n_trades = len(trades)
    winning_trades = [t for t in trades if t['return'] > 0]
    win_rate = len(winning_trades) / n_trades if n_trades > 0 else 0

    avg_return = np.mean([t['return'] for t in trades]) if trades else 0
    best_trade = max([t['return'] for t in trades]) if trades else 0
    worst_trade = min([t['return'] for t in trades]) if trades else 0

    return {
        'total_return': total_return,
        'annual_return': annual_return,
        'max_drawdown': max_drawdown,
        'sharpe_ratio': sharpe_ratio,
        'n_trades': n_trades,
        'win_rate': win_rate,
        'avg_return': avg_return,
        'best_trade': best_trade,
        'worst_trade': worst_trade,
        'final_value': final_value
    }


if __name__ == "__main__":
    from utils.data_fetcher import fetch_stock_data

    # 获取数据
    df = fetch_stock_data("AAPL", period="2y")

    if not df.empty:
        # 测试双均线策略
        strategy = DualMovingAverage(short_period=20, long_period=50)
        results, stats = backtest_strategy(df, strategy)

        print("=== 双均线策略回测结果 ===")
        print(f"总收益率: {stats['total_return']:.2%}")
        print(f"年化收益率: {stats['annual_return']:.2%}")
        print(f"最大回撤: {stats['max_drawdown']:.2%}")
        print(f"夏普比率: {stats['sharpe_ratio']:.2f}")
        print(f"交易次数: {stats['n_trades']}")
        print(f"胜率: {stats['win_rate']:.2%}")
