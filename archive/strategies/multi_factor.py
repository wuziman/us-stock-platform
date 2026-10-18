"""
多因子选股策略模块
基于多个因子选择股票组合
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.indicators import calculate_rsi, calculate_sma, calculate_atr


class MultiFactorStrategy:
    """多因子选股策略"""

    def __init__(self, initial_capital: float = 100000):
        self.initial_capital = initial_capital

    def calculate_factors(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算因子分数

        返回:
            DataFrame: 包含各因子分数
        """
        factors = pd.DataFrame(index=df.index)

        # 1. 动量因子 (20日收益率)
        factors['Momentum_20'] = df['Close'].pct_change(20)

        # 2. 动量因子 (60日收益率)
        factors['Momentum_60'] = df['Close'].pct_change(60)

        # 3. 波动率因子 (20日波动率，取倒数)
        factors['Volatility'] = df['Close'].pct_change().rolling(20).std()
        factors['Volatility_Score'] = 1 / (factors['Volatility'] + 0.001)

        # 4. 成交量动量
        vol_60 = df['Volume'].rolling(60).mean().replace(0, np.nan)
        factors['Volume_Momentum'] = df['Volume'].rolling(20).mean() / vol_60

        # 5. RSI因子
        factors['RSI'] = calculate_rsi(df, 14)
        factors['RSI_Score'] = 1 - abs(factors['RSI'] - 50) / 50

        # 6. 趋势强度 (价格相对于20日均线的位置)
        sma_20 = calculate_sma(df, 20).replace(0, np.nan)
        factors['Trend_Strength'] = (df['Close'] - sma_20) / sma_20

        return factors

    def normalize_factors(self, factors: pd.DataFrame) -> pd.DataFrame:
        """因子标准化（滚动窗口 Z-score，避免全样本未来函数）"""
        normalized = factors.copy()
        for col in factors.columns:
            rolling_mean = factors[col].expanding(min_periods=20).mean()
            rolling_std = factors[col].expanding(min_periods=20).std()
            normalized[col] = np.where(
                rolling_std > 0,
                (factors[col] - rolling_mean) / rolling_std,
                0.0
            )
        return normalized

    def calculate_composite_score(
        self,
        factors: pd.DataFrame,
        weights: Dict[str, float] = None
    ) -> pd.Series:
        """
        计算综合评分

        参数:
            factors: 标准化后的因子
            weights: 因子权重
        """
        if weights is None:
            weights = {
                'Momentum_20': 0.25,
                'Momentum_60': 0.20,
                'Volatility_Score': 0.15,
                'Volume_Momentum': 0.15,
                'RSI_Score': 0.15,
                'Trend_Strength': 0.10
            }

        score = pd.Series(0.0, index=factors.index)
        for factor, weight in weights.items():
            if factor in factors.columns:
                score += factors[factor].fillna(0) * weight

        return score

    def generate_signals(
        self,
        df: pd.DataFrame,
        lookback: int = 20,
        threshold: float = 0.0
    ) -> pd.DataFrame:
        """
        生成交易信号

        参数:
            df: 原始数据
            lookback: 回看周期
            threshold: 阈值
        """
        df = df.copy()

        # 计算因子
        factors = self.calculate_factors(df)

        # 标准化
        normalized_factors = self.normalize_factors(factors)

        # 计算综合评分
        df['Score'] = self.calculate_composite_score(normalized_factors)

        # 生成信号
        df['Signal'] = 0
        df.loc[df['Score'] > threshold, 'Signal'] = 1
        df.loc[df['Score'] < -threshold, 'Signal'] = -1

        df['Trade'] = df['Signal'].diff()

        return df


class MultiStockSelector:
    """
    多股票选择器
    在多个股票中选择最优组合
    """

    def __init__(self, n_stocks: int = 10):
        self.n_stocks = n_stocks

    def rank_stocks(
        self,
        data_dict: Dict[str, pd.DataFrame],
        factor_func=None
    ) -> pd.DataFrame:
        """
        对股票进行排名

        参数:
            data_dict: {symbol: DataFrame}
            factor_func: 因子计算函数

        返回:
            DataFrame: 排名结果
        """
        rankings = []

        for symbol, df in data_dict.items():
            if len(df) < 60:  # 需要足够数据
                continue

            # 计算因子
            score = self._calculate_stock_score(df)
            rankings.append({
                'Symbol': symbol,
                'Score': score,
                'Momentum_20': df['Close'].pct_change(20).iloc[-1],
                'Momentum_60': df['Close'].pct_change(60).iloc[-1],
                'Volatility': df['Close'].pct_change().rolling(20).std().iloc[-1]
            })

        if not rankings:
            return pd.DataFrame()

        rank_df = pd.DataFrame(rankings)
        rank_df = rank_df.sort_values('Score', ascending=False)

        return rank_df

    def _calculate_stock_score(self, df: pd.DataFrame) -> float:
        """计算单只股票的综合评分"""
        # 动量 (权重40%)
        momentum_20 = df['Close'].pct_change(20).iloc[-1] if len(df) > 20 else 0
        momentum_60 = df['Close'].pct_change(60).iloc[-1] if len(df) > 60 else 0
        momentum_score = (momentum_20 * 0.6 + momentum_60 * 0.4) * 0.4

        # 波动率 (权重30%，低波动更好)
        volatility = df['Close'].pct_change().rolling(20).std().iloc[-1] if len(df) > 20 else 1
        vol_score = (1 / (volatility + 0.001)) * 0.3

        # 趋势 (权重30%)
        sma_20 = df['Close'].rolling(20).mean().iloc[-1] if len(df) > 20 else df['Close'].iloc[-1]
        trend_score = ((df['Close'].iloc[-1] / sma_20) - 1) * 0.3 if sma_20 else 0

        return float(momentum_score + vol_score + trend_score)

    def select_portfolio(
        self,
        data_dict: Dict[str, pd.DataFrame],
        allocation_method: str = 'equal'
    ) -> Dict[str, float]:
        """
        选择投资组合

        参数:
            data_dict: {symbol: DataFrame}
            allocation_method: 分配方法 ('equal', 'momentum', 'risk_parity')

        返回:
            dict: {symbol: weight}
        """
        rankings = self.rank_stocks(data_dict)

        if rankings.empty:
            return {}

        # 选择前N只
        top_stocks = rankings.head(self.n_stocks)

        if allocation_method == 'equal':
            weights = {row['Symbol']: 1 / len(top_stocks) for _, row in top_stocks.iterrows()}

        elif allocation_method == 'momentum':
            # 仅对正动量按比例加权，避免负动量产生负权重
            pos_mom = top_stocks['Momentum_20'].clip(lower=0)
            total_momentum = pos_mom.sum()
            if total_momentum > 0:
                weights = {
                    row['Symbol']: float( max(0.0, row['Momentum_20']) / total_momentum )
                    for _, row in top_stocks.iterrows()
                }
            else:
                weights = {row['Symbol']: 1 / len(top_stocks) for _, row in top_stocks.iterrows()}

        elif allocation_method == 'risk_parity':
            inv_vol = 1 / (top_stocks['Volatility'] + 0.001)
            total_inv_vol = inv_vol.sum()
            weights = {
                row['Symbol']: float(inv_vol.iloc[i] / total_inv_vol)
                for i, (_, row) in enumerate(top_stocks.iterrows())
            }

        else:
            weights = {row['Symbol']: 1 / len(top_stocks) for _, row in top_stocks.iterrows()}

        return weights


class CombinedStrategy:
    """
    组合策略
    结合趋势跟踪和多因子选股（修复建仓不扣减现金导致净值翻倍及全样本未来函数问题）
    """

    def __init__(self, initial_capital: float = 100000):
        self.initial_capital = initial_capital
        self.trend_strategy = None
        self.factor_strategy = MultiFactorStrategy()

    def backtest(
        self,
        data_dict: Dict[str, pd.DataFrame],
        rebalance_period: int = 20,
        trend_weight: float = 0.6,
        factor_weight: float = 0.4
    ) -> Tuple[pd.DataFrame, Dict]:
        """
        组合回测（真实持仓股数与现金闭环核算，无未来函数）
        """
        all_dates = set()
        for df in data_dict.values():
            all_dates.update(df.index)
        all_dates = sorted(all_dates)

        if not all_dates:
            return pd.DataFrame(), {}

        results = pd.DataFrame(index=all_dates)
        results['Capital'] = float(self.initial_capital)
        results['Total'] = float(self.initial_capital)
        results['Positions'] = ''

        cash = float(self.initial_capital)
        holdings_shares: Dict[str, float] = {}   # {symbol: shares}
        last_prices: Dict[str, float] = {}       # {symbol: last_known_close}

        for i, dt in enumerate(all_dates):
            # 更新当日最新已知收盘价
            for symbol, df in data_dict.items():
                if dt in df.index:
                    last_prices[symbol] = float(df.loc[dt, 'Close'])

            # 计算调仓前当前账户总权益 = 现金 + 持仓市值
            portfolio_equity = cash + sum(
                shares * last_prices.get(sym, 0.0)
                for sym, shares in holdings_shares.items()
            )

            # 到期再平衡（仅截取截至当日 dt 的历史数据选股，彻底消除未来函数）
            if i >= 60 and i % rebalance_period == 0:
                hist_slice = {
                    sym: df.loc[df.index <= dt]
                    for sym, df in data_dict.items()
                }
                selector = MultiStockSelector(n_stocks=5)
                weights = selector.select_portfolio(hist_slice, 'momentum')

                if weights:
                    # 先按当日收盘价全部清仓回笼为现金，再按目标权重重新建仓并扣减现金
                    cash = portfolio_equity
                    holdings_shares = {}
                    for sym, weight in weights.items():
                        price = last_prices.get(sym, 0.0)
                        if price > 0 and weight > 0:
                            alloc_amount = portfolio_equity * weight
                            shares = alloc_amount / price
                            holdings_shares[sym] = shares
                            cash -= alloc_amount

            # 重新计算当日收盘后总权益與持仓占比字符串
            holdings_val = sum(
                shares * last_prices.get(sym, 0.0)
                for sym, shares in holdings_shares.items()
            )
            total_value = cash + holdings_val
            position_str = [
                f"{sym}:{(shares * last_prices.get(sym, 0.0) / total_value):.1%}"
                for sym, shares in holdings_shares.items()
                if total_value > 0
            ]

            results.loc[dt, 'Capital'] = round(cash, 2)
            results.loc[dt, 'Total'] = round(total_value, 2)
            results.loc[dt, 'Positions'] = ','.join(position_str)

        stats = self._calculate_stats(results)
        return results, stats

    def _calculate_stats(self, df: pd.DataFrame) -> Dict:
        """计算统计"""
        final_value = float(df['Total'].iloc[-1])
        total_return = (final_value - self.initial_capital) / self.initial_capital
        years = len(df) / 252
        annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        peak = df['Total'].expanding(min_periods=1).max()
        drawdown = (df['Total'] - peak) / peak
        max_drawdown = float(drawdown.min())

        daily_returns = df['Total'].pct_change().dropna()
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe_ratio = float(np.sqrt(252) * (daily_returns.mean() / daily_returns.std()))
        else:
            sharpe_ratio = 0.0

        return {
            'total_return': float(total_return),
            'annual_return': float(annual_return),
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio,
            'final_value': final_value
        }


if __name__ == "__main__":
    from utils.data_fetcher import fetch_multiple_stocks

    symbols = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA']
    data_dict = fetch_multiple_stocks(symbols, period="1y")

    if data_dict:
        selector = MultiStockSelector(n_stocks=3)
        weights = selector.select_portfolio(data_dict, 'momentum')

        print("=== 多股票选择结果 ===")
        print("\n投资组合权重:")
        for symbol, weight in weights.items():
            print(f"  {symbol}: {weight:.2%}")
