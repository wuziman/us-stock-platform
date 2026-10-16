"""
技术指标计算
"""

import numpy as np
import pandas as pd


def calculate_rsi(data: pd.Series, period: int = 14) -> pd.Series:
    """
    计算RSI（相对强弱指标），含除零保护（连涨14日loss=0时返回100，横盘gain=loss=0时返回50）

    参数：
        data: 价格序列
        period: 计算周期，默认14

    返回：
        RSI序列
    """
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # 当 loss == 0 且 gain > 0 时 RSI = 100；当 gain == 0 且 loss == 0 时 RSI = 50
    rsi = np.where((loss == 0) & (gain > 0), 100.0, np.where((loss == 0) & (gain == 0), 50.0, rsi))
    return pd.Series(rsi, index=data.index)


def calculate_macd(data: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """
    计算MACD

    参数：
        data: 价格序列
        fast: 快线周期，默认12
        slow: 慢线周期，默认26
        signal: 信号线周期，默认9

    返回：
        (MACD线, 信号线, 柱状图)
    """
    exp1 = data.ewm(span=fast, adjust=False).mean()
    exp2 = data.ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    return macd, signal_line, histogram


def calculate_bollinger_bands(data: pd.Series, period: int = 20, std_dev: int = 2) -> tuple:
    """
    计算布林带

    参数：
        data: 价格序列
        period: 计算周期，默认20
        std_dev: 标准差倍数，默认2

    返回：
        (上轨, 中轨, 下轨)
    """
    middle = data.rolling(window=period).mean()
    std = data.rolling(window=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def calculate_ma(data: pd.Series, period: int) -> pd.Series:
    """
    计算移动平均线

    参数：
        data: 价格序列
        period: 计算周期

    返回：
        MA序列
    """
    return data.rolling(window=period).mean()


def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    计算ATR（真实波动幅度均值）

    参数：
        high: 最高价序列
        low: 最低价序列
        close: 收盘价序列
        period: 计算周期，默认14

    返回：
        ATR序列
    """
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr


def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算所有技术指标

    参数：
        df: 包含Open, High, Low, Close, Volume的DataFrame

    返回：
        添加了技术指标的DataFrame
    """
    result = df.copy()

    # RSI
    result['RSI'] = calculate_rsi(result['Close'])

    # MACD
    result['MACD'], result['MACD_Signal'], result['MACD_Hist'] = calculate_macd(result['Close'])

    # 布林带
    result['BB_Upper'], result['BB_Middle'], result['BB_Lower'] = calculate_bollinger_bands(result['Close'])

    # 均线
    result['MA5'] = calculate_ma(result['Close'], 5)
    result['MA10'] = calculate_ma(result['Close'], 10)
    result['MA20'] = calculate_ma(result['Close'], 20)
    result['MA50'] = calculate_ma(result['Close'], 50)

    # ATR
    result['ATR'] = calculate_atr(result['High'], result['Low'], result['Close'])

    return result
