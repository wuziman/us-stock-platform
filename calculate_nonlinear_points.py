"""
非线性策略点位计算
结合共享非线性买点公式（+46%止盈 / -8%止损）与多指标决策树规则计算买入点位
"""

import os
import sys
from pathlib import Path
import yfinance as yf
import pandas as pd
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "stock-platform" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from app.utils.price_levels import (
        nonlinear_buy_point,
        NONLINEAR_PROFIT_PCT,
        STOP_PCT,
    )
except ImportError:
    NONLINEAR_PROFIT_PCT = 1.46
    STOP_PCT = 0.92
    def nonlinear_buy_point(rsi: float, ma20: float, bb_lower: float) -> float:
        return bb_lower if rsi < 30.0 else ma20


def calculate_nonlinear_points(stock_code):
    """计算非线性策略的买入点位（含本地Excel兜底与统一+46%止盈/-8%止损口径）"""
    print(f"\n{'='*60}")
    print(f"计算 {stock_code} 非线性策略点位")
    print(f"{'='*60}")

    data = pd.DataFrame()
    try:
        stock = yf.Ticker(stock_code)
        data = stock.history(period="3mo")
    except Exception:
        pass

    if data is None or data.empty:
        candidates = list(BASE_DIR.glob(f"**/{stock_code}*_data.xlsx"))
        if candidates:
            data = pd.read_excel(candidates[0])
            if 'Date' in data.columns:
                data['Date'] = pd.to_datetime(data['Date'])
                data = data.set_index('Date')

    if data is None or data.empty:
        print(f"无法获取 {stock_code} 数据")
        return None

    data = data.dropna(subset=['Close'])
    if data.empty:
        return None

    # 计算技术指标（含RSI除零保护）
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_s = 100 - (100 / (1 + rs))
    rsi_s = np.where((loss == 0) & (gain > 0), 100.0, np.where((loss == 0) & (gain == 0), 50.0, rsi_s))
    data['RSI'] = pd.Series(rsi_s, index=data.index)

    # MACD
    exp1 = data['Close'].ewm(span=12, adjust=False).mean()
    exp2 = data['Close'].ewm(span=26, adjust=False).mean()
    data['MACD'] = exp1 - exp2
    data['Signal'] = data['MACD'].ewm(span=9, adjust=False).mean()

    # 均线
    data['MA20'] = data['Close'].rolling(window=20).mean()
    data['MA50'] = data['Close'].rolling(window=50).mean()

    # 布林带
    data['BB_middle'] = data['Close'].rolling(window=20).mean()
    data['BB_std'] = data['Close'].rolling(window=20).std()
    data['BB_upper'] = data['BB_middle'] + 2 * data['BB_std']
    data['BB_lower'] = data['BB_middle'] - 2 * data['BB_std']

    # 成交量比率
    vol_ma20 = data['Volume'].rolling(window=20).mean().replace(0, np.nan)
    data['Volume_Ratio'] = data['Volume'] / vol_ma20

    latest = data.iloc[-1]
    current_price = float(latest['Close'])
    rsi = float(latest['RSI']) if pd.notna(latest['RSI']) else 50.0
    macd = float(latest['MACD'])
    signal = float(latest['Signal'])
    ma20 = float(latest['MA20']) if pd.notna(latest['MA20']) else current_price * 0.95
    ma50 = float(latest['MA50']) if pd.notna(latest['MA50']) else current_price * 0.95
    bb_upper = float(latest['BB_upper']) if pd.notna(latest['BB_upper']) else current_price * 1.1
    bb_lower = float(latest['BB_lower']) if pd.notna(latest['BB_lower']) else current_price * 0.9
    volume_ratio = float(latest['Volume_Ratio']) if pd.notna(latest['Volume_Ratio']) else 1.0

    print("\n当前数据:")
    print(f"  当前价: ${current_price:.2f}")
    print(f"  RSI: {rsi:.2f}")
    print(f"  MACD: {macd:.4f} | 信号线: {signal:.4f}")
    print(f"  20日均线: ${ma20:.2f} | 50日均线: ${ma50:.2f}")
    print(f"  布林带上轨: ${bb_upper:.2f} | 布林带下轨: ${bb_lower:.2f}")
    print(f"  成交量比率: {volume_ratio:.2f}")

    # 统一基准点位（与后端 price_levels.nonlinear_buy_point 对齐）
    standard_nl_buy = nonlinear_buy_point(rsi, ma20, bb_lower)

    rules = []
    if rsi < 30 and macd > signal:
        rules.append({
            'name': '规则1: RSI超卖 + MACD金叉',
            'signal': '强买入',
            'buy_price': standard_nl_buy,
            'confidence': 90
        })
    if rsi < 30 and current_price < bb_lower:
        rules.append({
            'name': '规则2: RSI超卖 + 跌破布林带下轨',
            'signal': '买入',
            'buy_price': bb_lower,
            'confidence': 80
        })
    if macd > signal and volume_ratio > 1.5:
        rules.append({
            'name': '规则3: MACD金叉 + 成交量放大',
            'signal': '买入',
            'buy_price': standard_nl_buy,
            'confidence': 70
        })
    if abs(current_price - ma20) / ma20 < 0.02 and 40 < rsi < 60:
        rules.append({
            'name': '规则4: 价格在20日均线附近 + RSI中性',
            'signal': '买入',
            'buy_price': ma20,
            'confidence': 60
        })
    if current_price < bb_lower and volume_ratio > 1.5:
        rules.append({
            'name': '规则5: 跌破布林带下轨 + 成交量放大',
            'signal': '买入',
            'buy_price': bb_lower,
            'confidence': 65
        })

    if not rules:
        rules.append({
            'name': '标准非线性规则: 挂单20日均线/布林下轨支撑',
            'signal': '挂单伏击',
            'buy_price': standard_nl_buy,
            'confidence': 55
        })

    results = []
    for rule in rules:
        buy_price = float(rule['buy_price'])
        stop_loss = buy_price * STOP_PCT
        take_profit = buy_price * NONLINEAR_PROFIT_PCT

        result = {
            'stock_code': stock_code,
            'current_price': current_price,
            'rule': rule['name'],
            'signal': rule['signal'],
            'buy_price': buy_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'confidence': rule['confidence'],
            'risk_reward': (take_profit - buy_price) / (buy_price - stop_loss) if buy_price > stop_loss else 0
        }
        results.append(result)

        print(f"\n{rule['name']}:")
        print(f"  信号: {rule['signal']} | 买入价: ${buy_price:.2f} | 止损价: ${stop_loss:.2f} (-8%) | 止盈价: ${take_profit:.2f} (+46%)")

    return results


def main():
    """主函数"""
    print("非线性策略点位计算")
    print("=" * 60)

    stocks = ['MU', 'SOXL', 'COHR', 'NKE', 'AXTI', 'AAOI', 'LITE', 'SNDK']
    all_results = []
    for stock in stocks:
        try:
            results = calculate_nonlinear_points(stock)
            if results:
                all_results.extend(results)
        except Exception as e:
            print(f"计算 {stock} 失败: {e}")

    print(f"\n{'='*60}")
    print("非线性策略点位汇总")
    print(f"{'='*60}")
    print(f"\n{'股票':<8} {'当前价':<10} {'买入价':<10} {'止损价':<10} {'止盈价':<10} {'信心度':<8}")
    print("-" * 60)
    for result in all_results:
        print(f"{result['stock_code']:<8} ${result['current_price']:<9.2f} ${result['buy_price']:<9.2f} ${result['stop_loss']:<9.2f} ${result['take_profit']:<9.2f} {result['confidence']:<7}%")


if __name__ == "__main__":
    main()
