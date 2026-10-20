"""
混合策略回测系统
结合线性和非线性的优点（含本地缓存兜底与除零保护）
"""

from pathlib import Path
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


class HybridBacktestEngine:
    """混合策略回测引擎"""

    def __init__(self, stock_code, start_date, end_date):
        self.stock_code = stock_code
        self.start_date = start_date
        self.end_date = end_date
        self.data = None

    def fetch_data(self):
        """获取历史数据（支持本地Excel兜底）"""
        print(f"获取 {self.stock_code} 历史数据...")
        try:
            stock = yf.Ticker(self.stock_code)
            self.data = stock.history(start=self.start_date, end=self.end_date)
        except Exception:
            self.data = pd.DataFrame()

        if self.data is None or self.data.empty:
            candidates = list(Path('.').glob(f"**/{self.stock_code}*_data.xlsx"))
            if candidates:
                df = pd.read_excel(candidates[0])
                if 'Date' in df.columns:
                    df['Date'] = pd.to_datetime(df['Date'])
                    df = df.set_index('Date')
                self.data = df
        return self.data

    def calculate_indicators(self):
        """计算技术指标"""
        if self.data is None:
            self.fetch_data()

        df = self.data.copy()

        # RSI (含除零保护)
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        rsi = np.where((loss == 0) & (gain > 0), 100.0, np.where((loss == 0) & (gain == 0), 50.0, rsi))
        df['RSI'] = pd.Series(rsi, index=df.index)

        # MACD
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()

        # 均线
        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA50'] = df['Close'].rolling(window=50).mean()

        # 布林带
        df['BB_middle'] = df['Close'].rolling(window=20).mean()
        df['BB_std'] = df['Close'].rolling(window=20).std()
        df['BB_upper'] = df['BB_middle'] + 2 * df['BB_std']
        df['BB_lower'] = df['BB_middle'] - 2 * df['BB_std']

        # 成交量比率
        df['Volume_MA20'] = df['Volume'].rolling(window=20).mean().replace(0, np.nan)
        df['Volume_Ratio'] = df['Volume'] / df['Volume_MA20']

        self.data = df
        return df

    def linear_signal(self):
        """线性策略信号"""
        df = self.data.copy()

        df['RSI_score'] = 0.0
        df.loc[df['RSI'] < 30, 'RSI_score'] = 2.0
        df.loc[(df['RSI'] >= 30) & (df['RSI'] < 40), 'RSI_score'] = 1.0
        df.loc[(df['RSI'] >= 40) & (df['RSI'] < 60), 'RSI_score'] = 0.0
        df.loc[(df['RSI'] >= 60) & (df['RSI'] < 70), 'RSI_score'] = -1.0
        df.loc[df['RSI'] >= 70, 'RSI_score'] = -2.0

        df['MACD_score'] = 0.0
        df.loc[df['MACD'] > df['Signal'], 'MACD_score'] = 1.0
        df.loc[df['MACD'] < df['Signal'], 'MACD_score'] = -1.0

        df['MA_score'] = 0.0
        df.loc[df['Close'] > df['MA20'], 'MA_score'] += 0.5
        df.loc[df['Close'] > df['MA50'], 'MA_score'] += 0.5
        df.loc[df['MA20'] > df['MA50'], 'MA_score'] += 0.5

        df['BB_score'] = 0.0
        df.loc[df['Close'] < df['BB_lower'], 'BB_score'] = 1.0
        df.loc[df['Close'] > df['BB_upper'], 'BB_score'] = -1.0

        df['Linear_Score'] = (
            df['RSI_score'] * 0.3 +
            df['MACD_score'] * 0.3 +
            df['MA_score'] * 0.2 +
            df['BB_score'] * 0.2
        )

        df['Linear_Signal'] = 0
        df.loc[df['Linear_Score'] > 0.5, 'Linear_Signal'] = 1
        df.loc[df['Linear_Score'] < -0.5, 'Linear_Signal'] = -1

        self.data = df
        return df

    def nonlinear_signal(self):
        """非线性策略信号"""
        df = self.data.copy()
        df['NonLinear_Score'] = 0.0

        df.loc[(df['RSI'] < 30) & (df['MACD'] > df['Signal']), 'NonLinear_Score'] = 2.0
        df.loc[(df['RSI'] < 30) & (df['Close'] < df['BB_lower']), 'NonLinear_Score'] = 1.5
        df.loc[(df['MACD'] > df['Signal']) & (df['Volume_Ratio'] > 1.5), 'NonLinear_Score'] = 1.0
        df.loc[(df['RSI'] > 70) & (df['MACD'] < df['Signal']), 'NonLinear_Score'] = -2.0
        df.loc[(df['RSI'] > 70) & (df['Close'] > df['BB_upper']), 'NonLinear_Score'] = -1.5
        df.loc[(df['MACD'] < df['Signal']) & (df['Volume_Ratio'] > 1.5), 'NonLinear_Score'] = -1.0

        df['NonLinear_Signal'] = 0
        df.loc[df['NonLinear_Score'] > 0.5, 'NonLinear_Signal'] = 1
        df.loc[df['NonLinear_Score'] < -0.5, 'NonLinear_Signal'] = -1

        self.data = df
        return df

    def hybrid_signal(self):
        """混合策略信号"""
        self.linear_signal()
        self.nonlinear_signal()
        df = self.data.copy()

        df['Hybrid_Signal'] = df['Linear_Signal'] + df['NonLinear_Signal']
        df['Hybrid_Signal_Standard'] = 0
        df.loc[df['Hybrid_Signal'] >= 1, 'Hybrid_Signal_Standard'] = 1
        df.loc[df['Hybrid_Signal'] <= -1, 'Hybrid_Signal_Standard'] = -1

        self.data = df
        return df

    def backtest(self, signal_column, initial_capital=10000):
        """回测策略（使用前一日信号在当日收盘执行，消除未来函数）"""
        df = self.data.copy()

        capital = initial_capital
        shares = 0
        position = 0
        trades = []
        equity_curve = []

        for i in range(1, len(df)):
            signal = df[signal_column].iloc[i - 1]
            price = df['Close'].iloc[i]

            if signal == 1 and position == 0:
                shares = int(capital / price)
                if shares > 0:
                    cost = shares * price
                    capital -= cost
                    position = 1
                    trades.append({
                        'date': df.index[i],
                        'action': 'BUY',
                        'price': price,
                        'shares': shares,
                        'cost': cost
                    })

            elif signal == -1 and position == 1:
                revenue = shares * price
                capital += revenue
                profit = revenue - trades[-1]['cost']
                trades.append({
                    'date': df.index[i],
                    'action': 'SELL',
                    'price': price,
                    'shares': shares,
                    'revenue': revenue,
                    'profit': profit
                })
                shares = 0
                position = 0

            total_value = capital + shares * price
            equity_curve.append({
                'date': df.index[i],
                'equity': total_value
            })

        final_price = df['Close'].iloc[-1]
        final_value = capital + shares * final_price

        return {
            'initial_capital': initial_capital,
            'final_value': final_value,
            'total_return': (final_value - initial_capital) / initial_capital * 100,
            'trades': trades,
            'equity_curve': equity_curve
        }

    def calculate_metrics(self, backtest_result):
        """计算回测指标"""
        equity_curve = pd.DataFrame(backtest_result['equity_curve'])
        equity_curve['returns'] = equity_curve['equity'].pct_change()

        total_days = max(len(equity_curve), 1)
        total_return = backtest_result['total_return'] / 100
        annualized_return = (1 + total_return) ** (252 / total_days) - 1

        equity_curve['cummax'] = equity_curve['equity'].cummax()
        equity_curve['drawdown'] = (equity_curve['equity'] - equity_curve['cummax']) / equity_curve['cummax']
        max_drawdown = abs(float(equity_curve['drawdown'].min())) * 100

        risk_free_rate = 0.03
        daily_returns = equity_curve['returns'].dropna()
        std = daily_returns.std()
        sharpe_ratio = float((daily_returns.mean() * 252 - risk_free_rate) / (std * np.sqrt(252))) if std and std > 0 else 0.0

        trades = backtest_result['trades']
        winning_trades = [t for t in trades if t.get('profit', 0) > 0]
        total_trades = len([t for t in trades if t['action'] == 'SELL'])
        win_rate = len(winning_trades) / total_trades * 100 if total_trades > 0 else 0.0

        return {
            'annualized_return': float(annualized_return * 100),
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio,
            'win_rate': float(win_rate),
            'total_trades': total_trades
        }


def compare_all_strategies(stock_code, years=1):
    """对比所有策略"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365 * years)

    engine = HybridBacktestEngine(stock_code, start_date, end_date)
    engine.fetch_data()
    if engine.data is None or engine.data.empty:
        return None
    engine.calculate_indicators()

    results = {}
    engine.linear_signal()
    results['linear'] = engine.calculate_metrics(engine.backtest('Linear_Signal'))

    engine.nonlinear_signal()
    results['nonlinear'] = engine.calculate_metrics(engine.backtest('NonLinear_Signal'))

    engine.hybrid_signal()
    results['hybrid'] = engine.calculate_metrics(engine.backtest('Hybrid_Signal_Standard'))

    return results


def main():
    stocks = ['MU', 'SOXL', 'COHR', 'NKE']
    for stock in stocks:
        try:
            compare_all_strategies(stock, years=1)
        except Exception as e:
            print(f"回测 {stock} 失败: {e}")


if __name__ == "__main__":
    main()
