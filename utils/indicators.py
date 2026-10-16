"""
技术指标计算模块
提供常用的技术指标计算函数
"""

import pandas as pd
import numpy as np
from typing import Tuple


def calculate_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """简单移动平均线"""
    return df['Close'].rolling(window=period).mean()


def calculate_ema(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """指数移动平均线"""
    return df['Close'].ewm(span=period, adjust=False).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """相对强弱指标 (RSI)，含除零保护"""
    close = df['Close'] if isinstance(df, pd.DataFrame) else df
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = np.where((loss == 0) & (gain > 0), 100.0, np.where((loss == 0) & (gain == 0), 50.0, rsi))
    return pd.Series(rsi, index=close.index)


def calculate_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """MACD指标"""
    close = df['Close'] if isinstance(df, pd.DataFrame) else df
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_bollinger_bands(
    df: pd.DataFrame,
    period: int = 20,
    std_dev: int = 2
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """布林带"""
    close = df['Close'] if isinstance(df, pd.DataFrame) else df
    middle = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = middle + (std_dev * std)
    lower = middle - (std_dev * std)
    return upper, middle, lower


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """平均真实波幅 (ATR)"""
    high = df['High']
    low = df['Low']
    close = df['Close'].shift()

    tr1 = high - low
    tr2 = abs(high - close)
    tr3 = abs(low - close)

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """平均趋向指数 (ADX)"""
    high = df['High']
    low = df['Low']

    # 计算+DM和-DM
    plus_dm = high.diff()
    minus_dm = low.diff()

    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm > 0] = 0
    minus_dm = abs(minus_dm)

    # 计算ATR
    atr = calculate_atr(df, period).replace(0, np.nan)

    # 计算+DI和-DI
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)

    # 计算DX和ADX
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * abs(plus_di - minus_di) / denom
    adx = dx.rolling(window=period).mean()

    return adx


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


def calculate_obv(df: pd.DataFrame) -> pd.Series:
    """能量潮指标 (OBV) - 向量化计算"""
    direction = np.sign(df['Close'].diff()).fillna(0)
    obv = (direction * df['Volume']).cumsum()
    obv.iloc[0] = 0.0
    return obv


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """成交量加权平均价 (VWAP)"""
    vol_cum = df['Volume'].cumsum().replace(0, np.nan)
    vwap = (df['Close'] * df['Volume']).cumsum() / vol_cum
    return vwap


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


if __name__ == "__main__":
    from data_fetcher import fetch_stock_data

    df = fetch_stock_data("AAPL", period="6mo")
    if not df.empty:
        df = add_all_indicators(df)
        print("指标计算完成！")
        print(df[['Close', 'SMA_20', 'RSI', 'MACD', 'BB_Upper', 'BB_Lower']].tail())
