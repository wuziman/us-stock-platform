"""
回测服务
- 权益曲线：每日总资产（现金+持仓市值）
- 统计指标：年化收益按实际天数折算、最大回撤取自真实资金曲线、夏普比率按日收益序列
- 大盘基准：A股默认使用沪深300指数（000300.SS），美股默认使用标普500 ETF（SPY）
- Walk-Forward：输出各段最优参数一致性/稳定性统计（众数占比、唯一组合数、稳定度评级）
"""

from collections import Counter
import itertools

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from .stock_service import StockService
from ..utils.indicators import calculate_all_indicators
from ..utils.price_levels import compute_trailing_stop


# 市场大盘指数基准（A股统一采用沪深300指数 000300.SS，替代旧版平安银行 000001.SZ）
BENCHMARK_BY_MARKET: Dict[str, str] = {
    'A': '000300.SS',
    'US': 'SPY',
}

# ============================================
# 参数网格（寻优内置默认；请求可覆盖部分键的取值列表）
# 约定：tp/sl 均用正数表示幅度；ma_cross 要求 fast < slow
# ============================================
PARAM_GRIDS: Dict[str, Dict[str, list]] = {
    'linear': {'tp': [0.10, 0.15, 0.25], 'sl': [0.06, 0.08, 0.12]},          # 9组合
    'nonlinear': {'tp': [0.30, 0.46, 0.60], 'sl': [0.06, 0.08, 0.12]},       # 9组合
    'ma_cross': {'fast': [10, 20, 30], 'slow': [40, 50, 80]},                # 过滤fast>=slow
    'macd': {'fast': [8, 12], 'slow': [21, 26], 'signal': [7, 9]},           # 8组合
}

# 各策略默认参数（与历史硬编码值一致：不传参数 = 原结果）
DEFAULT_PARAMS: Dict[str, Dict] = {
    'linear': {'tp': 0.15, 'sl': 0.08},
    'nonlinear': {'tp': 0.46, 'sl': 0.08},
    'ma_cross': {'fast': 20, 'slow': 50},
    'macd': {'fast': 12, 'slow': 26, 'signal': 9},
}

# 寻优排序指标：请求字段名 -> 结果行字段名
METRIC_FIELDS: Dict[str, str] = {
    'sharpe': 'sharpe_ratio',
    'total_return': 'total_return',
    'annual_return': 'annual_return',
    'max_drawdown': 'max_drawdown',
    'win_rate': 'win_rate',
    'trade_count': 'trade_count',
}

# 寻优结果行包含的指标字段
RESULT_ROW_FIELDS = ('total_return', 'annual_return', 'max_drawdown',
                     'sharpe_ratio', 'win_rate', 'trade_count', 'final_value')


class BacktestService:
    """策略回测服务"""

    def __init__(self):
        self.stock_service = StockService()

    def _compute_index_benchmark(self, df: pd.DataFrame, market: str,
                                 period: str, start: int = 50) -> Dict:
        """
        拉取所属市场的大盘指数基准（A股=000300.SS 沪深300，美股=SPY）并计算同区间基准收益率。
        若离线或未配置基准行情，返回 benchmark_code 与 None 收益率，不阻塞主回测流程。
        """
        benchmark_code = BENCHMARK_BY_MARKET.get(market, 'SPY')
        bench_return: Optional[float] = None
        try:
            df_bench = self.stock_service.get_stock_data(benchmark_code, market, period=period)
            if df_bench is not None and not df_bench.empty and len(df) > start:
                start_dt = df.index[start]
                end_dt = df.index[-1]
                win = df_bench[(df_bench.index >= start_dt) & (df_bench.index <= end_dt)]
                if len(win) < 2 and len(df_bench) > start:
                    win = df_bench.iloc[start:]
                if len(win) >= 2:
                    c0 = float(win.iloc[0]['Close'])
                    c1 = float(win.iloc[-1]['Close'])
                    if c0 > 0:
                        bench_return = round((c1 / c0 - 1.0) * 100.0, 2)
        except Exception:
            bench_return = None
        return {
            'benchmark_code': benchmark_code,
            'benchmark_return': bench_return,
        }

    def run_backtest(self, stock_code: str, strategy: str, market: str = "US",
                     start_date: str = None, end_date: str = None,
                     initial_capital: float = 100000, period: str = "1y",
                     commission_per_trade: float = 1.0,
                     trailing_stop: bool = False,
                     staged_entry: bool = False,
                     params: Optional[Dict] = None) -> Dict:
        """
        运行策略回测

        period: 数据区间 1y/3y/5y（A股走东方财富/腾讯前复权日K，基准为沪深300 000300.SS）
        commission_per_trade: 每笔交易手续费（买入/卖出各收一次，默认$1）
        params: 可选策略自定义参数（如寻优得到的最优 tp/sl 或 fast/slow）
        """
        df = self.stock_service.get_stock_data(stock_code, market, period=period)
        if df is None or df.empty:
            return {'error': '无法获取数据'}

        # 计算技术指标
        df = calculate_all_indicators(df)

        # 过滤日期范围
        if start_date:
            df = df[df.index >= start_date]
        if end_date:
            df = df[df.index <= end_date]

        if len(df) < 51:
            return {'error': '数据不足'}

        extra_params = dict(params) if params else {}
        if trailing_stop:
            extra_params['trailing_stop'] = True
        if staged_entry:
            extra_params['staged_entry'] = True
        result = self._execute_backtest(
            df=df, strategy=strategy, stock_code=stock_code,
            initial_capital=initial_capital, period=period,
            commission_per_trade=commission_per_trade,
            params=extra_params if extra_params else None,
        )
        bench_info = self._compute_index_benchmark(df, market, period, start=50)
        result['benchmark_code'] = bench_info['benchmark_code']
        result['benchmark_return'] = bench_info['benchmark_return']
        result['alpha_vs_benchmark'] = (
            round(result['total_return'] - bench_info['benchmark_return'], 2)
            if bench_info['benchmark_return'] is not None else None
        )
        if start_date or end_date:
            # 自定义区间时返回实际窗口，前端据此展示，避免错标成"近1年"
            result['date_range'] = f"{start_date or '起始'} ~ {end_date or '至今'}"
        return result

    def _execute_backtest(self, df: pd.DataFrame, strategy: str, stock_code: str,
                          initial_capital: float, period: str,
                          commission_per_trade: float = 1.0,
                          params: Optional[Dict] = None) -> Dict:
        """在已计算指标的DataFrame上执行单策略回测（run_backtest / run_compare 共用）"""
        strategy_map = {
            'linear': self._backtest_linear,
            'nonlinear': self._backtest_nonlinear,
            'ma_cross': self._backtest_ma_cross,
            'macd': self._backtest_macd,
        }
        if strategy not in strategy_map:
            return {'error': f'未知策略: {strategy}'}

        trades, equity_curve = strategy_map[strategy](
            df, initial_capital, commission_per_trade, params)
        final_value = equity_curve[-1][1]
        total_return = (final_value - initial_capital) / initial_capital

        # 买入持有基准（与策略权益曲线同日期轴）
        buy_hold_curve, buy_hold_return = self._calculate_buy_hold(df, initial_capital)

        result = self._calculate_metrics(
            trades=trades,
            equity_curve=equity_curve,
            total_return=total_return,
            initial_capital=initial_capital,
            final_value=final_value,
            # 年化分母从第50根K线起算（跳过预热期），与买入持有基准口径一致
            dates=(df.index[50], df.index[-1]),
            stock_code=stock_code,
            strategy=strategy,
            commission_per_trade=commission_per_trade,
        )
        result['period'] = period
        result['commission_per_trade'] = commission_per_trade
        result['buy_hold_curve'] = buy_hold_curve
        result['buy_hold_return'] = buy_hold_return
        return result

    # ============================================
    # 策略实现：返回 (成交记录, 每日总资产曲线)
    # ============================================
    @staticmethod
    def _build_equity_curve(df: pd.DataFrame, capital: float, position: int, dates_seen: set) -> List[Tuple[str, float]]:
        """辅助：当前现金+持仓在最后一根K线的总资产点"""
        date_str = df.index[-1].strftime('%Y-%m-%d')
        if date_str not in dates_seen:
            value = capital + position * df.iloc[-1]['Close']
            dates_seen.add(date_str)
            return [(date_str, round(value, 2))]
        return []

    def _backtest_linear(self, df: pd.DataFrame, initial_capital: float,
                         commission: float = 1.0, params: Optional[Dict] = None,
                         start: int = 50) -> Tuple[List[Dict], List[Tuple[str, float]]]:
        """
        线性策略回测（斐波那契回撤）
        买入：上升趋势中价格回踩触及50%斐波那契买入位（前一日计算买入位，无前视偏差）
        卖出：盘中触及+tp止盈位/-sl止损位即离场（挂单语义：按触发价成交，跳空按开盘价；params={"tp":0.15,"sl":0.08}，均用正数表示幅度，默认与历史硬编码一致）
        start: 循环起始bar（默认50，与指标预热期一致；walk-forward测试段传1以覆盖整段）
        每笔买卖各收手续费 commission
        """
        params = params or {}
        tp = abs(float(params.get('tp', 0.15)))
        sl = abs(float(params.get('sl', 0.08)))
        use_trailing = bool(params.get('trailing_stop', False))
        use_staged = bool(params.get('staged_entry', False))
        trades: List[Dict] = []
        equity_curve: List[Tuple[str, float]] = []
        capital = initial_capital
        position = 0
        buy_price = 0.0
        highest_since_entry = 0.0
        added_once = False

        for i in range(start, len(df)):
            current = df.iloc[i]
            prev = df.iloc[i - 1]
            current_price = current['Close']
            bought_this_bar = False  # 入场bar不判卖出：盘中先后顺序不可知，从次根bar起挂单生效

            # --- 买入（支持一次性建仓或分批金字塔建仓）---
            if position == 0:
                prev_ma20 = prev.get('MA20')
                prev_close = prev['Close']
                if pd.notna(prev_ma20) and prev_ma20 < prev_close:
                    price_range = prev_close - prev_ma20
                    buy_level = prev_close - 0.5 * price_range
                    # 当日最低价触及买入位 → 成交于 min(开盘价, 买入位)
                    if current['Low'] <= buy_level:
                        fill_price = min(current['Open'], buy_level)
                        alloc_ratio = 0.50 if use_staged else 0.95
                        shares = int(capital * alloc_ratio / fill_price)
                        if shares > 0:
                            position = shares
                            buy_price = fill_price
                            highest_since_entry = float(current['High'])
                            added_once = False
                            capital -= shares * fill_price + commission
                            bought_this_bar = True
                            trades.append({
                                'date': df.index[i].strftime('%Y-%m-%d'),
                                'action': 'buy',
                                'price': round(fill_price, 2),
                                'shares': shares,
                                'fee': commission
                            })
            elif use_staged and not added_once and position > 0:
                # 分批补仓：较首仓回撤5%时动用剩余资金补仓一次，摊低均价
                add_trigger = buy_price * 0.95
                if current['Low'] <= add_trigger:
                    fill_price = min(current['Open'], add_trigger)
                    add_shares = int((capital * 0.90) / fill_price)
                    if add_shares > 0:
                        total_cost = position * buy_price + add_shares * fill_price
                        position += add_shares
                        buy_price = total_cost / position
                        added_once = True
                        capital -= add_shares * fill_price + commission
                        trades.append({
                            'date': df.index[i].strftime('%Y-%m-%d'),
                            'action': 'buy',
                            'price': round(fill_price, 2),
                            'shares': add_shares,
                            'fee': commission
                        })

            # --- 卖出（盘中路径判定，预挂单语义；同日双触发保守按止损优先）---
            if position > 0 and not bought_this_bar:
                stop_level = buy_price * (1 - sl)
                if use_trailing:
                    stop_level = compute_trailing_stop(buy_price, highest_since_entry, stop_level)
                tp_level = buy_price * (1 + tp)
                sell_price = None
                if current['Low'] <= stop_level:
                    sell_price = min(current['Open'], stop_level)   # 跳空低开按更差的开盘价
                elif current['High'] >= tp_level:
                    sell_price = max(current['Open'], tp_level)     # 跳空高开按更好的开盘价
                highest_since_entry = max(highest_since_entry, float(current['High']))
                if sell_price is not None:
                    profit_pct = (sell_price - buy_price) / buy_price
                    capital += position * sell_price - commission
                    trades.append({
                        'date': df.index[i].strftime('%Y-%m-%d'),
                        'action': 'sell',
                        'price': round(sell_price, 2),
                        'shares': position,
                        'profit_pct': round(profit_pct, 4),
                        'fee': commission
                    })
                    position = 0

            # --- 记录每日总资产（现金+持仓市值）---
            equity_curve.append((
                df.index[i].strftime('%Y-%m-%d'),
                round(capital + position * current_price, 2)
            ))

        return trades, equity_curve

    def _backtest_nonlinear(self, df: pd.DataFrame, initial_capital: float,
                            commission: float = 1.0, params: Optional[Dict] = None,
                            start: int = 50) -> Tuple[List[Dict], List[Tuple[str, float]]]:
        """
        非线性策略回测（超卖反弹）
        买入：前一日RSI<30 或 当日最低价触及前一日布林带下轨
        卖出：盘中触及+tp止盈位/-sl止损位即离场（挂单语义：按触发价成交，跳空按开盘价；params={"tp":0.46,"sl":0.08}，均用正数表示幅度，默认与历史硬编码一致）
        每笔买卖各收手续费 commission
        """
        params = params or {}
        tp = abs(float(params.get('tp', 0.46)))
        sl = abs(float(params.get('sl', 0.08)))
        trades: List[Dict] = []
        equity_curve: List[Tuple[str, float]] = []
        capital = initial_capital
        position = 0
        buy_price = 0.0

        for i in range(start, len(df)):
            current = df.iloc[i]
            prev = df.iloc[i - 1]
            current_price = current['Close']
            bought_this_bar = False  # 入场bar不判卖出：盘中先后顺序不可知，从次根bar起挂单生效

            # --- 买入 ---
            if position == 0:
                rsi_prev = prev.get('RSI')
                bb_lower_prev = prev.get('BB_Lower')

                signal = False
                fill_price = current['Open']

                if pd.notna(rsi_prev) and rsi_prev < 30:
                    signal = True  # 超卖，开盘买入
                elif pd.notna(bb_lower_prev) and current['Low'] <= bb_lower_prev:
                    signal = True  # 触及下轨
                    fill_price = min(current['Open'], bb_lower_prev)

                if signal and pd.notna(fill_price):
                    shares = int(capital * 0.95 / fill_price)
                    if shares > 0:
                        position = shares
                        buy_price = fill_price
                        capital -= shares * fill_price + commission
                        bought_this_bar = True
                        trades.append({
                            'date': df.index[i].strftime('%Y-%m-%d'),
                            'action': 'buy',
                            'price': round(fill_price, 2),
                            'shares': shares,
                            'fee': commission
                        })

            # --- 卖出（盘中路径判定，预挂单语义；同日双触发保守按止损优先）---
            if position > 0 and not bought_this_bar:
                stop_level = buy_price * (1 - sl)
                tp_level = buy_price * (1 + tp)
                sell_price = None
                if current['Low'] <= stop_level:
                    sell_price = min(current['Open'], stop_level)   # 跳空低开按更差的开盘价
                elif current['High'] >= tp_level:
                    sell_price = max(current['Open'], tp_level)     # 跳空高开按更好的开盘价
                if sell_price is not None:
                    profit_pct = (sell_price - buy_price) / buy_price
                    capital += position * sell_price - commission
                    trades.append({
                        'date': df.index[i].strftime('%Y-%m-%d'),
                        'action': 'sell',
                        'price': round(sell_price, 2),
                        'shares': position,
                        'profit_pct': round(profit_pct, 4),
                        'fee': commission
                    })
                    position = 0

            equity_curve.append((
                df.index[i].strftime('%Y-%m-%d'),
                round(capital + position * current_price, 2)
            ))

        return trades, equity_curve

    def _backtest_ma_cross(self, df: pd.DataFrame, initial_capital: float,
                           commission: float = 1.0, params: Optional[Dict] = None,
                           start: int = 50) -> Tuple[List[Dict], List[Tuple[str, float]]]:
        """
        双均线交叉策略：快线上穿慢线金叉买入，下穿死叉卖出（每笔买卖各收手续费）
        params={"fast":20,"slow":50}；非默认参数时在df副本上现算均线，避免污染缓存列
        """
        params = params or {}
        fast = int(params.get('fast', 20))
        slow = int(params.get('slow', 50))
        if (fast, slow) != (20, 50):
            df = df.copy()
            df['MA20'] = df['Close'].rolling(fast).mean()
            df['MA50'] = df['Close'].rolling(slow).mean()

        trades: List[Dict] = []
        equity_curve: List[Tuple[str, float]] = []
        capital = initial_capital
        position = 0
        buy_price = 0.0

        for i in range(start, len(df)):
            current = df.iloc[i]
            prev = df.iloc[i - 1]
            current_price = current['Close']

            ma20 = current.get('MA20')
            ma50 = current.get('MA50')
            prev_ma20 = prev.get('MA20')
            prev_ma50 = prev.get('MA50')

            if all(pd.notna(v) for v in [ma20, ma50, prev_ma20, prev_ma50]):
                # 金叉买入（均线含当日收盘、收盘后才能确认 → 按当日收盘价成交，尾盘单可实现）
                if prev_ma20 <= prev_ma50 and ma20 > ma50 and position == 0:
                    fill_price = current_price
                    shares = int(capital * 0.95 / fill_price)
                    if shares > 0:
                        position = shares
                        buy_price = fill_price
                        capital -= shares * fill_price + commission
                        trades.append({
                            'date': df.index[i].strftime('%Y-%m-%d'),
                            'action': 'buy',
                            'price': round(fill_price, 2),
                            'shares': shares,
                            'fee': commission
                        })
                # 死叉卖出
                elif prev_ma20 >= prev_ma50 and ma20 < ma50 and position > 0:
                    capital += position * current_price - commission
                    profit_pct = (current_price - buy_price) / buy_price
                    trades.append({
                        'date': df.index[i].strftime('%Y-%m-%d'),
                        'action': 'sell',
                        'price': round(current_price, 2),
                        'shares': position,
                        'profit_pct': round(profit_pct, 4),
                        'fee': commission
                    })
                    position = 0

            equity_curve.append((
                df.index[i].strftime('%Y-%m-%d'),
                round(capital + position * current_price, 2)
            ))

        return trades, equity_curve

    def _backtest_macd(self, df: pd.DataFrame, initial_capital: float,
                       commission: float = 1.0, params: Optional[Dict] = None,
                       start: int = 50) -> Tuple[List[Dict], List[Tuple[str, float]]]:
        """
        MACD金叉死叉策略（每笔买卖各收手续费）
        params={"fast":12,"slow":26,"signal":9}；非默认参数时在df副本上现算EMA差与信号线
        """
        params = params or {}
        fast = int(params.get('fast', 12))
        slow = int(params.get('slow', 26))
        signal_period = int(params.get('signal', 9))
        if (fast, slow, signal_period) != (12, 26, 9):
            df = df.copy()
            exp_fast = df['Close'].ewm(span=fast, adjust=False).mean()
            exp_slow = df['Close'].ewm(span=slow, adjust=False).mean()
            df['MACD'] = exp_fast - exp_slow
            df['MACD_Signal'] = df['MACD'].ewm(span=signal_period, adjust=False).mean()

        trades: List[Dict] = []
        equity_curve: List[Tuple[str, float]] = []
        capital = initial_capital
        position = 0
        buy_price = 0.0

        for i in range(start, len(df)):
            current = df.iloc[i]
            prev = df.iloc[i - 1]
            current_price = current['Close']

            macd = current.get('MACD')
            signal = current.get('MACD_Signal')
            prev_macd = prev.get('MACD')
            prev_signal = prev.get('MACD_Signal')

            if all(pd.notna(v) for v in [macd, signal, prev_macd, prev_signal]):
                # 金叉买入（MACD含当日收盘、收盘后才能确认 → 按当日收盘价成交，尾盘单可实现）
                if prev_macd <= prev_signal and macd > signal and position == 0:
                    fill_price = current_price
                    shares = int(capital * 0.95 / fill_price)
                    if shares > 0:
                        position = shares
                        buy_price = fill_price
                        capital -= shares * fill_price + commission
                        trades.append({
                            'date': df.index[i].strftime('%Y-%m-%d'),
                            'action': 'buy',
                            'price': round(fill_price, 2),
                            'shares': shares,
                            'fee': commission
                        })
                # 死叉卖出
                elif prev_macd >= prev_signal and macd < signal and position > 0:
                    capital += position * current_price - commission
                    profit_pct = (current_price - buy_price) / buy_price
                    trades.append({
                        'date': df.index[i].strftime('%Y-%m-%d'),
                        'action': 'sell',
                        'price': round(current_price, 2),
                        'shares': position,
                        'profit_pct': round(profit_pct, 4),
                        'fee': commission
                    })
                    position = 0

            equity_curve.append((
                df.index[i].strftime('%Y-%m-%d'),
                round(capital + position * current_price, 2)
            ))

        return trades, equity_curve

    # ============================================
    # 统计指标
    # ============================================
    def _calculate_metrics(self, trades: List[Dict], equity_curve: List[Tuple[str, float]],
                           total_return: float, initial_capital: float, final_value: float,
                           dates: tuple, stock_code: str, strategy: str,
                           commission_per_trade: float = 1.0) -> Dict:
        """
        计算回测统计：
        - 年化收益按实际自然日折算
        - 最大回撤从每日总资产曲线计算
        - 夏普比率从每日总资产的日收益率序列计算（年化）
        - 总手续费 = 成交笔数（买+卖）× 单笔手续费
        """
        sell_trades = [t for t in trades if t['action'] == 'sell']
        win_trades = [t for t in sell_trades if t.get('profit_pct', 0) > 0]
        win_rate = len(win_trades) / len(sell_trades) if sell_trades else 0

        # 年化收益：按实际自然日折算复利
        days = max((dates[1] - dates[0]).days, 1)
        if final_value > 0 and initial_capital > 0:
            annual_return = (final_value / initial_capital) ** (365 / days) - 1
        else:
            annual_return = -1.0

        # 最大回撤：基于每日总资产
        values = np.array([v for _, v in equity_curve])
        running_max = np.maximum.accumulate(values)
        drawdowns = (values - running_max) / running_max
        max_drawdown = abs(drawdowns.min()) if len(drawdowns) else 0

        # 夏普比率：日收益率年化（无风险利率近似为0）
        sharpe_ratio = 0.0
        if len(values) > 2:
            daily_returns = np.diff(values) / values[:-1]
            std = daily_returns.std()
            if std > 0:
                sharpe_ratio = (daily_returns.mean() / std) * np.sqrt(252)

        return {
            'stock_code': stock_code,
            'strategy': strategy,
            'total_return': round(total_return * 100, 2),
            'annual_return': round(annual_return * 100, 2),
            'max_drawdown': round(max_drawdown * 100, 2),
            'sharpe_ratio': round(float(sharpe_ratio), 2),
            'win_rate': round(win_rate * 100, 2),
            'trade_count': len(sell_trades),
            'total_fees': round(len(trades) * commission_per_trade, 2),
            'initial_capital': initial_capital,
            'final_value': round(final_value, 2),
            'trades': trades,
            # 前端权益曲线直接用总资产序列
            'equity_curve': [{'date': d, 'value': v} for d, v in equity_curve],
        }

    # ============================================
    # 买入持有基准 + 策略对比
    # ============================================
    def _calculate_buy_hold(self, df: pd.DataFrame, initial_capital: float,
                            start: int = 50) -> Tuple[List[Dict], float]:
        """
        买入持有基准：
        - 从第start根K线（策略可交易首日，与策略权益曲线同日期轴）的开盘价全仓买入
        - 允许零碎股（shares用float），每日按收盘价估值
        - 不收手续费（仅一笔买入且持有不动，简化处理）
        返回 (每日总资产曲线 [{'date','value'}], 总收益率%)
        """
        window = df.iloc[start:]
        if window.empty:
            return [], 0.0
        first_open = float(window.iloc[0]['Open'])
        if pd.isna(first_open) or first_open <= 0:
            return [], 0.0

        shares = initial_capital / first_open
        curve = [
            {'date': d.strftime('%Y-%m-%d'), 'value': round(shares * float(c), 2)}
            for d, c in zip(window.index, window['Close'])
        ]
        total_return = (curve[-1]['value'] - initial_capital) / initial_capital
        return curve, round(total_return * 100, 2)

    @staticmethod
    def _curve_stats(values: List[float], initial_capital: float, days: int) -> Dict:
        """
        由每日总资产序列计算统计指标（用于买入持有基准，算法与 _calculate_metrics 口径一致）：
        年化收益按实际自然日复利折算、最大回撤取自真实资金曲线、夏普按日收益年化
        """
        arr = np.array(values, dtype=float)

        # 最大回撤
        max_drawdown = 0.0
        if len(arr):
            running_max = np.maximum.accumulate(arr)
            drawdowns = (arr - running_max) / running_max
            max_drawdown = abs(drawdowns.min())

        # 夏普比率
        sharpe_ratio = 0.0
        if len(arr) > 2:
            daily_returns = np.diff(arr) / arr[:-1]
            std = daily_returns.std()
            if std > 0:
                sharpe_ratio = (daily_returns.mean() / std) * np.sqrt(252)

        # 年化收益
        final_value = float(arr[-1]) if len(arr) else initial_capital
        if final_value > 0 and initial_capital > 0:
            annual_return = (final_value / initial_capital) ** (365 / max(days, 1)) - 1
        else:
            annual_return = -1.0

        total_return = (final_value - initial_capital) / initial_capital if initial_capital else 0.0
        return {
            # float()显式转换：round()不会把numpy.float64转成Python float，会泄漏到JSON序列化
            'total_return': round(float(total_return * 100), 2),
            'annual_return': round(float(annual_return * 100), 2),
            'max_drawdown': round(float(max_drawdown * 100), 2),
            'sharpe_ratio': round(float(sharpe_ratio), 2),
        }

    def run_compare(self, stock_code: str, market: str = "US", period: str = "1y",
                    initial_capital: float = 100000,
                    commission_per_trade: float = 1.0,
                    trailing_stop: bool = False,
                    staged_entry: bool = False) -> Dict:
        """
        一键对比4个策略 + 买入持有基准。
        数据只拉取一次，4个策略在同一份数据上回测，保证可比性。
        返回 strategies（各策略完整结果）/ buy_hold（基准）/ comparison（对比表格行）
        """
        df = self.stock_service.get_stock_data(stock_code, market, period=period)
        if df is None or df.empty:
            return {'error': '无法获取数据'}

        df = calculate_all_indicators(df)
        if len(df) < 51:
            return {'error': '数据不足'}

        strategy_names = [
            ('linear', '线性'),
            ('nonlinear', '非线性'),
            ('ma_cross', '双均线交叉'),
            ('macd', 'MACD'),
        ]

        extra_params = {}
        if trailing_stop:
            extra_params['trailing_stop'] = True
        if staged_entry:
            extra_params['staged_entry'] = True

        strategies: Dict[str, Dict] = {}
        comparison: List[Dict] = []
        for key, name in strategy_names:
            r = self._execute_backtest(
                df=df, strategy=key, stock_code=stock_code,
                initial_capital=initial_capital, period=period,
                commission_per_trade=commission_per_trade,
                params=extra_params if extra_params else None,
            )
            if 'error' in r:
                return r
            strategies[key] = r
            comparison.append({
                'name': name,
                'key': key,
                'total_return': r['total_return'],
                'annual_return': r['annual_return'],
                'max_drawdown': r['max_drawdown'],
                'sharpe_ratio': r['sharpe_ratio'],
                'win_rate': r['win_rate'],
                'trade_count': r['trade_count'],
                'total_fees': r['total_fees'],
                # 相对买入持有的超额收益（百分点）
                'excess_vs_buy_hold': round(r['total_return'] - r['buy_hold_return'], 2),
            })

        # 买入持有基准指标（起止日与策略可交易窗口一致）
        buy_hold_curve, buy_hold_return = self._calculate_buy_hold(df, initial_capital)
        days = max((df.index[-1] - df.index[50]).days, 1)
        stats = self._curve_stats([p['value'] for p in buy_hold_curve], initial_capital, days)
        buy_hold = {
            'total_return': buy_hold_return,
            'annual_return': stats['annual_return'],
            'max_drawdown': stats['max_drawdown'],
            'sharpe_ratio': stats['sharpe_ratio'],
            'equity_curve': buy_hold_curve,
        }
        comparison.append({
            'name': '买入持有',
            'key': 'buy_hold',
            'total_return': buy_hold['total_return'],
            'annual_return': buy_hold['annual_return'],
            'max_drawdown': buy_hold['max_drawdown'],
            'sharpe_ratio': buy_hold['sharpe_ratio'],
            'win_rate': 0.0,
            'trade_count': 0,
            'total_fees': 0.0,
            'excess_vs_buy_hold': 0.0,
        })

        bench_info = self._compute_index_benchmark(df, market, period, start=50)
        return {
            'stock_code': stock_code,
            'period': period,
            'initial_capital': initial_capital,
            'commission_per_trade': commission_per_trade,
            'benchmark_code': bench_info['benchmark_code'],
            'benchmark_return': bench_info['benchmark_return'],
            'strategies': strategies,
            'buy_hold': buy_hold,
            'comparison': comparison,
        }

    # ============================================
    # 参数网格寻优
    # ============================================
    def _strategy_fn(self, strategy: str):
        """按策略key取回测函数"""
        return {
            'linear': self._backtest_linear,
            'nonlinear': self._backtest_nonlinear,
            'ma_cross': self._backtest_ma_cross,
            'macd': self._backtest_macd,
        }[strategy]

    @staticmethod
    def _param_combos(strategy: str, override: Optional[Dict[str, list]] = None) -> List[Dict]:
        """
        由参数网格生成笛卡尔积组合列表；override 可覆盖部分键的取值列表。
        ma_cross 过滤 fast >= slow 的非法组合。
        """
        grid = {k: list(v) for k, v in PARAM_GRIDS[strategy].items()}
        for k, vals in (override or {}).items():
            if k in grid and isinstance(vals, (list, tuple)) and len(vals) > 0:
                grid[k] = list(vals)
        keys = list(grid.keys())
        combos = [dict(zip(keys, vals)) for vals in itertools.product(*[grid[k] for k in keys])]
        if strategy == 'ma_cross':
            combos = [c for c in combos if c['fast'] < c['slow']]
        return combos

    def _run_combo_metrics(self, fn, df: pd.DataFrame, params: Dict,
                           initial_capital: float, commission: float,
                           start: int = 50) -> Optional[Dict]:
        """跑单个参数组合，返回指标行（params + RESULT_ROW_FIELDS）；空曲线返回None"""
        trades, curve = fn(df, initial_capital, commission, params, start=start)
        if not curve:
            return None
        metrics = self._calculate_metrics(
            trades=trades, equity_curve=curve,
            total_return=(curve[-1][1] / initial_capital - 1),
            initial_capital=initial_capital, final_value=curve[-1][1],
            # 分母用权益曲线实际起始bar（start=50），与单策略回测口径一致
            dates=(df.index[start], df.index[-1]),
            stock_code='', strategy='',  # 行内不使用标识字段
            commission_per_trade=commission,
        )
        row = {'params': params}
        row.update({f: metrics[f] for f in RESULT_ROW_FIELDS})
        return row

    @staticmethod
    def _build_heatmap(strategy: str, results: List[Dict], metric_field: str) -> Dict:
        """
        构造热力图矩阵：z[y_idx][x_idx] 为对应参数组合的metric值（缺失组合为null）。
        linear/nonlinear/ma_cross 用两维参数；macd 三维则固定 signal=9 取 fast×slow 切片。
        """
        fixed_key, fixed_val = ('signal', 9) if strategy == 'macd' else (None, None)
        if strategy == 'macd':
            x_name, y_name = 'fast', 'slow'
        else:
            keys = list(PARAM_GRIDS[strategy].keys())
            x_name, y_name = keys[0], keys[1]

        xs = sorted({r['params'][x_name] for r in results})
        ys = sorted({r['params'][y_name] for r in results})
        lookup = {}
        for r in results:
            p = r['params']
            if fixed_key is not None and p.get(fixed_key) != fixed_val:
                continue
            lookup[(p[x_name], p[y_name])] = r[metric_field]
        z = [[lookup.get((xv, yv)) for xv in xs] for yv in ys]
        return {'x_name': x_name, 'y_name': y_name, 'x_values': xs, 'y_values': ys, 'z': z}

    def _grid_search(self, fn, df: pd.DataFrame, strategy: str,
                     initial_capital: float, commission: float,
                     combos: List[Dict], start: int = 50) -> Tuple[List[Dict], Dict]:
        """
        在给定数据窗上遍历参数组合，按夏普降序排序。
        返回 (排序后的results行列表, best行)；全部无曲线时返回 ([], {})
        """
        rows = []
        for params in combos:
            row = self._run_combo_metrics(fn, df, params, initial_capital, commission, start=start)
            if row:
                rows.append(row)
        rows.sort(key=lambda r: r['sharpe_ratio'], reverse=True)
        best = dict(rows[0]) if rows else {}
        return rows, best

    def optimize(self, stock_code: str, strategy: str, market: str = "US",
                 period: str = "1y", initial_capital: float = 100000,
                 commission_per_trade: float = 1.0, metric: str = "sharpe",
                 param_grid: Optional[Dict[str, list]] = None) -> Dict:
        """
        网格寻优：数据只拉一次、指标一次算好，对每个参数组合跑回测。
        返回 {stock_code, strategy, metric, best:{params,...指标}, results:[...按metric排序],
              heatmap:{x_name,y_name,x_values,y_values,z}, in_sample_warning: str}
        （排序方向：max_drawdown 升序，其余降序）
        """
        if strategy not in PARAM_GRIDS:
            return {'error': f'未知策略: {strategy}'}
        if metric not in METRIC_FIELDS:
            return {'error': f'不支持的排序指标: {metric}'}

        df = self.stock_service.get_stock_data(stock_code, market, period=period)
        if df is None or df.empty:
            return {'error': '无法获取数据'}
        df = calculate_all_indicators(df)
        if len(df) < 51:
            return {'error': '数据不足'}

        combos = self._param_combos(strategy, param_grid)
        if not combos:
            return {'error': '参数网格为空'}

        fn = self._strategy_fn(strategy)
        # 组合间复用同一份df：仅ma_cross/macd非默认参数时函数内部做副本，避免污染缓存列
        results, best = self._grid_search(
            fn, df, strategy, initial_capital, commission_per_trade, combos)
        if not results:
            return {'error': '回测无有效结果'}

        metric_field = METRIC_FIELDS[metric]
        # 按请求的metric降序重排（_grid_search默认按sharpe排），best=第一名
        # 例外：max_drawdown 越低越好，按升序取第一名
        descending = metric_field != 'max_drawdown'
        results.sort(key=lambda r: r[metric_field], reverse=descending)
        best = dict(results[0])
        best[metric] = best[metric_field]

        heatmap = self._build_heatmap(strategy, results, metric_field)
        return {
            'stock_code': stock_code,
            'strategy': strategy,
            'metric': metric,
            # 回显实际使用的回测口径，前端展示用（消除"静默继承顶部表单"）
            'period': period,
            'initial_capital': initial_capital,
            'best': best,
            'results': results,
            'heatmap': heatmap,
            'in_sample_warning': '样本内寻优，请用 Walk-Forward 验证',
        }

    # ============================================
    # Walk-Forward 滚动验证
    # ============================================
    @staticmethod
    def _stitch_curves(curves: List[List[Tuple[str, float]]]) -> List[Dict]:
        """
        各段权益曲线首尾相接成复合净值：
        后一段值 = 前段末值 × (本段值 / 本段初值)，每段以本段初值为基准rebase
        """
        stitched: List[Dict] = []
        prev_last: Optional[float] = None
        for curve in curves:
            if not curve:
                continue
            base = float(curve[0][1])
            if base <= 0:
                continue
            for d, v in curve:
                val = float(v) if prev_last is None else prev_last * (float(v) / base)
                stitched.append({'date': d, 'value': round(val, 2)})
            prev_last = stitched[-1]['value']
        return stitched

    @staticmethod
    def _compute_param_stability(seg_reports: List[Dict]) -> Dict:
        """
        统计 Walk-Forward 各段 best_params 的一致性/稳定性（判断样本内过拟合的核心依据）：
        - mode_params: 出现频次最高的参数组合
        - mode_count: 众数参数组合出现的段数
        - mode_ratio: 众数占比（0~1，如 1.0 表示所有段选出完全相同的最优参数）
        - mode_ratio_pct: 众数占比百分比（0~100）
        - unique_combos: 各段选出的唯一参数组合个数
        - stability_rating: 稳定度评级（'高' / '中' / '低'）
        - per_param: 每个参数维度的众数及一致性占比
        """
        total = len(seg_reports)
        if total == 0:
            return {
                'mode_params': {},
                'mode_count': 0,
                'mode_ratio': 0.0,
                'mode_ratio_pct': 0.0,
                'unique_combos': 0,
                'total_segments': 0,
                'stability_rating': '低',
                'per_param': {},
            }

        combo_tuples = [tuple(sorted((s.get('best_params') or {}).items())) for s in seg_reports]
        counts = Counter(combo_tuples)
        mode_tuple, mode_count = counts.most_common(1)[0]
        unique_combos = len(counts)
        mode_ratio = round(mode_count / total, 4)
        mode_ratio_pct = round(mode_count / total * 100.0, 1)

        if unique_combos == 1 or mode_ratio >= 0.75:
            rating = '高'
        elif mode_ratio >= 0.50:
            rating = '中'
        else:
            rating = '低'

        per_param: Dict[str, Dict] = {}
        all_keys = sorted({k for s in seg_reports for k in (s.get('best_params') or {}).keys()})
        for k in all_keys:
            vals = [s['best_params'][k] for s in seg_reports if k in (s.get('best_params') or {})]
            if vals:
                p_val, p_cnt = Counter(vals).most_common(1)[0]
                per_param[k] = {
                    'mode': p_val,
                    'mode_count': p_cnt,
                    'mode_ratio': round(p_cnt / len(vals), 4),
                }

        return {
            'mode_params': dict(mode_tuple),
            'mode_count': mode_count,
            'mode_ratio': mode_ratio,
            'mode_ratio_pct': mode_ratio_pct,
            'unique_combos': unique_combos,
            'total_segments': total,
            'stability_rating': rating,
            'per_param': per_param,
        }

    def _warmed_df(self, df_full: pd.DataFrame, strategy: str, params: Dict) -> pd.DataFrame:
        """在全量数据上按候选参数预计算策略指标列，解决切片冷启动暖机问题。"""
        if strategy == 'ma_cross':
            fast = int(params.get('fast', 20))
            slow = int(params.get('slow', 50))
            if (fast, slow) == (20, 50):
                return df_full  # calculate_all_indicators已算好默认均线
            out = df_full.copy()
            out['MA20'] = df_full['Close'].rolling(fast).mean()
            out['MA50'] = df_full['Close'].rolling(slow).mean()
            return out
        if strategy == 'macd':
            fast = int(params.get('fast', 12))
            slow = int(params.get('slow', 26))
            signal_period = int(params.get('signal', 9))
            if (fast, slow, signal_period) == (12, 26, 9):
                return df_full
            out = df_full.copy()
            exp_fast = df_full['Close'].ewm(span=fast, adjust=False).mean()
            exp_slow = df_full['Close'].ewm(span=slow, adjust=False).mean()
            out['MACD'] = exp_fast - exp_slow
            out['MACD_Signal'] = out['MACD'].ewm(span=signal_period, adjust=False).mean()
            return out
        return df_full  # linear/nonlinear的tp/sl与指标列无关

    def _fn_params_for_warmed(self, strategy: str, params: Dict) -> Dict:
        """配合_warmed_df：ma_cross/macd传默认参数让fn跳过重算（列已预热），其余策略原样传参"""
        if strategy in ('ma_cross', 'macd'):
            return dict(DEFAULT_PARAMS[strategy])
        return dict(params)

    def _select_best_params(self, df_full: pd.DataFrame, train_start: int, train_end: int,
                            fn, strategy: str,
                            initial_capital: float, commission: float) -> Tuple[Dict, float]:
        """训练窗上跑内置网格（与optimize同一套），按夏普选最优。返回 (best_params, best_sharpe)"""
        best_params: Optional[Dict] = None
        best_sharpe = -np.inf
        for params in self._param_combos(strategy):
            warmed = self._warmed_df(df_full, strategy, params)
            _, curve = fn(warmed.iloc[train_start:train_end], initial_capital,
                          commission, self._fn_params_for_warmed(strategy, params), start=1)
            if not curve:
                continue
            arr = np.array([v for _, v in curve], dtype=float)
            sharpe = 0.0
            if len(arr) > 2:
                daily = np.diff(arr) / arr[:-1]
                std = daily.std()
                if std > 0:
                    sharpe = float((daily.mean() / std) * np.sqrt(252))
            if sharpe > best_sharpe:
                best_sharpe, best_params = sharpe, params
        if best_params is None:
            # 训练窗无有效交易 → 回退默认参数
            best_params, best_sharpe = dict(DEFAULT_PARAMS[strategy]), 0.0
        return best_params, float(best_sharpe)

    def walk_forward(self, stock_code: str, strategy: str, market: str = "US",
                     period: str = "5y", initial_capital: float = 100000,
                     commission_per_trade: float = 1.0,
                     train_ratio: float = 0.6, segments: int = 2) -> Dict:
        """Walk-Forward滚动验证（含各段参数稳定性统计 param_stability）"""
        if strategy not in PARAM_GRIDS:
            return {'error': f'未知策略: {strategy}'}
        segments = int(segments)
        if segments < 1 or segments > 4:
            return {'error': 'segments 仅支持 1~4'}
        train_ratio = min(max(float(train_ratio), 0.1), 1.0)

        df = self.stock_service.get_stock_data(stock_code, market, period=period)
        if df is None or df.empty:
            return {'error': '无法获取数据'}
        df = calculate_all_indicators(df)

        n = len(df)
        if n < 60 * (segments + 1):
            return {'error': f'数据不足（Walk-Forward至少需要约{60 * (segments + 1)}根K线）'}

        block = n // (segments + 1)
        bounds = [i * block for i in range(segments + 1)] + [n]

        fn = self._strategy_fn(strategy)
        seg_reports: List[Dict] = []
        seg_strategy_curves: List[List[Tuple[str, float]]] = []
        seg_bh_curves: List[List[Dict]] = []

        for i in range(1, segments + 1):
            test_a, test_b = bounds[i], bounds[i + 1]
            train_start = int(round((1 - train_ratio) * test_a))
            train_df = df.iloc[train_start:test_a]
            test_df = df.iloc[test_a:test_b]

            best_params, is_sharpe = self._select_best_params(
                df, train_start, test_a, fn, strategy, initial_capital, commission_per_trade)

            warmed_best = self._warmed_df(df, strategy, best_params)
            _, curve = fn(warmed_best.iloc[test_a:test_b], initial_capital,
                          commission_per_trade,
                          self._fn_params_for_warmed(strategy, best_params), start=1)
            bh_curve, bh_ret = self._calculate_buy_hold(test_df, initial_capital, start=0)

            oos_ret = round(float(curve[-1][1]) / initial_capital * 100 - 100, 2)
            values = [v for _, v in curve]
            days = max((test_df.index[-1] - test_df.index[0]).days, 1)
            stats = self._curve_stats(values, initial_capital, days)
            beats = bool(oos_ret > bh_ret)

            seg_reports.append({
                'step': i,
                'train_range': [train_df.index[0].strftime('%Y-%m-%d'),
                                train_df.index[-1].strftime('%Y-%m-%d')],
                'test_range': [test_df.index[0].strftime('%Y-%m-%d'),
                               test_df.index[-1].strftime('%Y-%m-%d')],
                'best_params': best_params,
                'is_sharpe': round(is_sharpe, 2),
                'oos_return': oos_ret,
                'oos_sharpe': stats['sharpe_ratio'],
                'oos_max_drawdown': stats['max_drawdown'],
                'oos_buy_hold_return': bh_ret,
                'beats_buy_hold': beats,
            })
            seg_strategy_curves.append(curve)
            seg_bh_curves.append(bh_curve)

        stitched = self._stitch_curves(seg_strategy_curves)
        bh_stitched = self._stitch_curves(
            [[(p['date'], p['value']) for p in c] for c in seg_bh_curves])

        win_segments = sum(1 for s in seg_reports if s['beats_buy_hold'])
        param_stability = self._compute_param_stability(seg_reports)
        summary = {
            'avg_oos_return': round(float(np.mean([s['oos_return'] for s in seg_reports])), 2),
            'avg_oos_sharpe': round(float(np.mean([s['oos_sharpe'] for s in seg_reports])), 2),
            'win_segments': win_segments,
            'total_segments': segments,
            'mode_ratio': param_stability['mode_ratio'],
            'unique_combos': param_stability['unique_combos'],
            'stability_rating': param_stability['stability_rating'],
            'param_stability': param_stability,
        }

        return {
            'stock_code': stock_code,
            'strategy': strategy,
            'period': period,
            'initial_capital': initial_capital,
            'train_ratio': train_ratio,
            'total_segments': segments,
            'segments': seg_reports,
            'stitched_oos_curve': stitched,
            'oos_buy_hold_curve': bh_stitched,
            'param_stability': param_stability,
            'summary': summary,
        }

    def evaluate_df_profile(
        self,
        df: pd.DataFrame,
        strategy: str = "nonlinear",
        stock_code: str = "",
        initial_capital: float = 100000.0,
        commission_per_trade: float = 1.0,
    ) -> Optional[Dict]:
        """
        【统一回测口径单点】在给定日线 DataFrame 上直接按 BacktestService 标准规则
        （入场 bar 不判卖出 bought_this_bar=True、95% 仓位分配、每笔 $1 手续费）
        计算完整回测指标与实测盈亏比，供实测画像生成、半凯利 L1/L2 及单测共用。
        """
        if df is None or len(df) < 51:
            return None
        dfi = calculate_all_indicators(df.copy())
        res = self._execute_backtest(
            df=dfi,
            strategy=strategy,
            stock_code=stock_code,
            initial_capital=initial_capital,
            period="2y",
            commission_per_trade=commission_per_trade,
        )
        if "error" in res:
            return None

        sell_trades = [t for t in res.get("trades", []) if t.get("action") == "sell"]
        wins = [float(t.get("profit_pct", 0.0)) * 100.0 for t in sell_trades if float(t.get("profit_pct", 0.0)) > 0]
        losses = [abs(float(t.get("profit_pct", 0.0))) * 100.0 for t in sell_trades if float(t.get("profit_pct", 0.0)) < 0]
        all_rets = [float(t.get("profit_pct", 0.0)) * 100.0 for t in sell_trades]

        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0
        profit_factor = round(avg_win / avg_loss, 2) if avg_loss > 0 else (1.88 if strategy == "linear" else 4.70)
        avg_ret = round(float(np.mean(all_rets)), 2) if all_rets else 0.0

        return {
            "calibration_source": "BacktestService (95%仓位 | $1手续费 | 入场次日判离场)",
            "strategy": strategy,
            "total_trades": int(res["trade_count"]),
            "wins_count": len(wins),
            "losses_count": len(losses),
            "win_rate": float(res["win_rate"]),
            "avg_return": avg_ret,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": float(profit_factor),
            "max_drawdown": float(res["max_drawdown"]),
            "cum_return": float(res["total_return"]),
            "total_return": float(res["total_return"]),
            "sharpe_ratio": float(res["sharpe_ratio"]),
            "final_value": float(res["final_value"]),
            "trades": res["trades"],
        }
