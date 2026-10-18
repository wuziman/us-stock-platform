"""
技术指标计算
"""

from typing import Tuple
import numpy as np
import pandas as pd


def calculate_rsi(data: pd.Series, period: int = 14, column: str = 'Close') -> pd.Series:
    if isinstance(data, pd.DataFrame):
        data = data[column]
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


def calculate_macd(data: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9, column: str = 'Close') -> tuple:
    if isinstance(data, pd.DataFrame):
        data = data[column]
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


def calculate_bollinger_bands(data: pd.Series, period: int = 20, std_dev: float = 2, column: str = 'Close') -> tuple:
    if isinstance(data, pd.DataFrame):
        data = data[column]
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


def calculate_ma(data: pd.Series, period: int = 20, column: str = 'Close') -> pd.Series:
    if isinstance(data, pd.DataFrame):
        data = data[column]
    """
    计算移动平均线

    参数：
        data: 价格序列
        period: 计算周期

    返回：
        MA序列
    """
    return data.rolling(window=period).mean()


def calculate_atr(high: pd.Series, low: pd.Series = None, close: pd.Series = None, period: int = 14) -> pd.Series:
    if isinstance(high, pd.DataFrame):
        if isinstance(low, int) and close is None:
            period = low
        df_in = high
        high, low, close = df_in['High'], df_in['Low'], df_in['Close']
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


def calculate_ema(prices: pd.Series, period: int = 20, column: str = 'Close') -> pd.Series:
    """计算指数移动平均线 (EMA)"""
    if isinstance(prices, pd.DataFrame):
        prices = prices[column]
    return prices.ewm(span=period, adjust=False).mean()


def calculate_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """计算OBV能量潮指标"""
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * volume).cumsum()


def calculate_vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """计算VWAP成交量加权平均价"""
    tp = (high + low + close) / 3.0
    cum_vol = volume.cumsum().replace(0, np.nan)
    return (tp * volume).cumsum() / cum_vol


def calculate_kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9, m1: int = 3, m2: int = 3):
    """计算KDJ指标"""
    lowest_low = low.rolling(window=n, min_periods=1).min()
    highest_high = high.rolling(window=n, min_periods=1).max()
    denom = (highest_high - lowest_low).replace(0, np.nan)
    rsv = ((close - lowest_low) / denom * 100).fillna(50.0)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def calculate_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """计算ADX平均趋向指数"""
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period, min_periods=1).mean().replace(0, np.nan)
    plus_di = 100 * (plus_dm.rolling(window=period, min_periods=1).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period, min_periods=1).mean() / atr)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0.0)
    return dx.rolling(window=period, min_periods=1).mean()


def calculate_support_resistance(df: pd.DataFrame, window: int = 20):
    """计算支撑位与阻力位"""
    recent = df.tail(window)
    return float(recent['Low'].min()), float(recent['High'].max())


def calculate_fibonacci_retracement(high_price: float, low_price: float) -> dict:
    """计算斐波那契回调位"""
    diff = high_price - low_price
    return {
        'level_0': high_price,
        'level_236': high_price - 0.236 * diff,
        'level_382': high_price - 0.382 * diff,
        'level_500': high_price - 0.500 * diff,
        'level_618': high_price - 0.618 * diff,
        'level_786': high_price - 0.786 * diff,
        'level_100': low_price,
    }

calculate_sma = calculate_ma


def calculate_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """简单移动平均线"""
    return df['Close'].rolling(window=period).mean()


def calculate_stochastic(
    df: pd.DataFrame,
    k_period: int = 14,
    d_period: int = 3
) -> Tuple[pd.Series, pd.Series]:
    """随机指标 (KDJ)"""
    low_min = df['Low'].rolling(window=k_period).min()
    high_max = df['High'].rolling(window=k_period).max()

    denom = (high_max - low_min).replace(0, np.nan)
    k = 100 * (df['Close'] - low_min) / denom
    d = k.rolling(window=d_period).mean()
    return k, d


def calculate_supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0
) -> pd.Series:
    """超级趋势指标"""
    atr = calculate_atr(df, period)
    hl2 = (df['High'] + df['Low']) / 2

    upper_band = (hl2 + (multiplier * atr)).copy()
    lower_band = (hl2 - (multiplier * atr)).copy()

    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)

    supertrend.iloc[0] = upper_band.iloc[0]
    direction.iloc[0] = 1

    for i in range(1, len(df)):
        if df['Close'].iloc[i] > upper_band.iloc[i-1]:
            direction.iloc[i] = 1
        elif df['Close'].iloc[i] < lower_band.iloc[i-1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i-1]

            if direction.iloc[i] == 1 and lower_band.iloc[i] < lower_band.iloc[i-1]:
                lower_band.iloc[i] = lower_band.iloc[i-1]
            if direction.iloc[i] == -1 and upper_band.iloc[i] > upper_band.iloc[i-1]:
                upper_band.iloc[i] = upper_band.iloc[i-1]

        supertrend.iloc[i] = lower_band.iloc[i] if direction.iloc[i] == 1 else upper_band.iloc[i]

    return supertrend


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """添加所有常用指标到DataFrame"""
    df = df.copy()

    # 移动平均线
    df['SMA_20'] = calculate_sma(df, 20)
    df['SMA_50'] = calculate_sma(df, 50)
    df['SMA_200'] = calculate_sma(df, 200)
    df['EMA_20'] = calculate_ema(df, 20)

    # RSI
    df['RSI'] = calculate_rsi(df, 14)

    # MACD
    df['MACD'], df['MACD_Signal'], df['MACD_Hist'] = calculate_macd(df)

    # 布林带
    df['BB_Upper'], df['BB_Middle'], df['BB_Lower'] = calculate_bollinger_bands(df)

    # ATR
    df['ATR'] = calculate_atr(df, 14)

    # ADX
    df['ADX'] = calculate_adx(df, 14)

    # 随机指标
    df['Stoch_K'], df['Stoch_D'] = calculate_stochastic(df)

    # OBV
    df['OBV'] = calculate_obv(df)

    # VWAP
    df['VWAP'] = calculate_vwap(df)

    return df
