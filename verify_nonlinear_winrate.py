"""
量化策略真实胜率与回测系统 (Walk-Forward 真实步进式回测)
彻底剔除未来函数与虚高假设：
1. 逐日动态计算挂单买入点（杜绝静态历史价格穿越，复用后端 price_levels 统一买点公式）
2. 逐日最低价 Low <= 买入价判定真实成交
3. 严格日内路径判定（-8% 止损优先、+15%/+46% 止盈、超时强平）
4. 单标的独占仓位状态机（杜绝连续重叠虚假交易）
5. 支持同时对比【线性策略】与【非线性策略】真实胜率、盈亏比与最大回撤，并将最新实测画像自动保存至 data/stock_profiles.json
"""

import json
import os
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

# 确保控制台 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "stock-platform" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from app.utils.price_levels import (
        linear_buy_point,
        nonlinear_buy_point,
        LINEAR_PROFIT_PCT,
        NONLINEAR_PROFIT_PCT,
        STOP_PCT,
    )
except ImportError:
    LINEAR_PROFIT_PCT = 1.15
    NONLINEAR_PROFIT_PCT = 1.46
    STOP_PCT = 0.92
    def linear_buy_point(current: float, ma20: float) -> float:
        buy = current - 0.5 * (current - ma20) if ma20 < current else current * 0.95
        return min(buy, current * 0.95)
    def nonlinear_buy_point(rsi: float, ma20: float, bb_lower: float) -> float:
        return bb_lower if rsi < 30.0 else ma20


def fetch_stock_history(stock_code: str, period: str = "2y") -> pd.DataFrame:
    """获取历史行情数据，若网络受阻则自动从本地 Excel 兜底"""
    df = pd.DataFrame()
    try:
        stock = yf.Ticker(stock_code)
        df = stock.history(period=period)
    except Exception as e:
        print(f"  [yfinance] 获取 {stock_code} 异常: {e}")

    if df is None or df.empty:
        candidates = list(BASE_DIR.glob(f"**/{stock_code}*_data.xlsx"))
        if candidates:
            df = pd.read_excel(candidates[0])
            if 'Date' in df.columns:
                df['Date'] = pd.to_datetime(df['Date'])
                df = df.set_index('Date')
            print(f"  [本地缓存] {stock_code} 加载本地数据 ({len(df)} 条)")
    return df


def simulate_strategy_walk_forward(df: pd.DataFrame, strategy_type: str = "nonlinear") -> dict:
    """
    步进式逐日回测模拟器 (Walk-Forward Simulation)
    :param df: 历史日线数据 (需含 Open, High, Low, Close)
    :param strategy_type: 'linear' (线性) 或 'nonlinear' (非线性)
    """
    if len(df) < 60:
        return None

    data = df.copy()

    # 1. 预计算技术指标（严格向前滚动，不含未来数据）
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-9)
    data['RSI'] = 100 - (100 / (1 + rs))

    data['MA20'] = data['Close'].rolling(window=20).mean()
    data['MA50'] = data['Close'].rolling(window=50).mean()

    data['BB_middle'] = data['Close'].rolling(window=20).mean()
    data['BB_std'] = data['Close'].rolling(window=20).std()
    data['BB_lower'] = data['BB_middle'] - 2 * data['BB_std']

    # 策略参数配置
    if strategy_type == "linear":
        take_profit_ratio = LINEAR_PROFIT_PCT  # +15%
        stop_loss_ratio = STOP_PCT             # -8%
        max_holding_days = 30                  # 最长持有30个交易日
    else:
        take_profit_ratio = NONLINEAR_PROFIT_PCT  # +46%
        stop_loss_ratio = STOP_PCT                # -8%
        max_holding_days = 60                     # 最长持有60个交易日

    # 2. 状态机逐日步进模拟
    position = 0  # 0=空仓, 1=持仓
    entry_price = 0.0
    entry_date = None
    stop_price = 0.0
    profit_price = 0.0
    holding_days = 0

    trades = []
    equity = 1.0
    equity_curve = [1.0]

    # 从第 50 个交易日开始（保证指标充分平稳）
    for i in range(50, len(data)):
        prev = data.iloc[i - 1]
        curr = data.iloc[i]
        curr_date = data.index[i]

        curr_open = curr['Open']
        curr_high = curr['High']
        curr_low = curr['Low']
        curr_close = curr['Close']

        # === 状态 1：空仓状态，判断今天是否能撮合成交 ===
        if position == 0:
            # 严格根据昨天收盘时的指标，推算今天的挂单买入价（走 price_levels 统一定义）
            if strategy_type == "linear":
                pending_buy = linear_buy_point(float(prev['Close']), float(prev['MA20']))
            else:
                pending_buy = nonlinear_buy_point(
                    float(prev['RSI']), float(prev['MA20']), float(prev['BB_lower'])
                )

            # 判断今天日内是否跌到挂单买入价 (Low <= pending_buy)
            if curr_low <= pending_buy and pending_buy > 0:
                fill_price = curr_open if curr_open < pending_buy else pending_buy
                position = 1
                entry_price = fill_price
                entry_date = curr_date
                stop_price = entry_price * stop_loss_ratio
                profit_price = entry_price * take_profit_ratio
                holding_days = 0

        # === 状态 2：持仓状态，判断今天是否触发止盈、止损或超时强平 ===
        elif position == 1:
            holding_days += 1
            exit_trade = False
            exit_price = 0.0
            exit_reason = ""

            # 风险优先法则：先判断是否被日内砸盘击穿止损线 (-8%)
            if curr_low <= stop_price:
                exit_trade = True
                exit_price = curr_open if curr_open < stop_price else stop_price
                exit_reason = "STOP_LOSS"
            elif curr_high >= profit_price:
                exit_trade = True
                exit_price = curr_open if curr_open > profit_price else profit_price
                exit_reason = "TAKE_PROFIT"
            elif holding_days >= max_holding_days:
                exit_trade = True
                exit_price = curr_close
                exit_reason = "TIME_EXIT"

            if exit_trade:
                trade_return = (exit_price - entry_price) / entry_price
                equity *= (1.0 + trade_return)
                is_win = trade_return > 0

                trades.append({
                    'entry_date': entry_date,
                    'exit_date': curr_date,
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'return': trade_return * 100,
                    'holding_days': holding_days,
                    'reason': exit_reason,
                    'is_win': is_win
                })

                position = 0
                entry_price = 0.0
                holding_days = 0

        equity_curve.append(equity)

    # 3. 统计指标计算
    total_trades = len(trades)
    if total_trades == 0:
        return {
            'total_trades': 0,
            'win_rate': 0.0,
            'avg_return': 0.0,
            'profit_factor': 0.0,
            'max_drawdown': 0.0,
            'cum_return': 0.0,
            'trades': []
        }

    wins = [t for t in trades if t['is_win']]
    losses = [t for t in trades if not t['is_win']]
    win_rate = (len(wins) / total_trades) * 100

    avg_win = np.mean([t['return'] for t in wins]) if wins else 0.0
    avg_loss = abs(np.mean([t['return'] for t in losses])) if losses else 1e-6
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0.0
    avg_return = np.mean([t['return'] for t in trades])

    # 最大回撤计算
    eq_series = pd.Series(equity_curve)
    cummax = eq_series.cummax()
    drawdown = (eq_series - cummax) / cummax
    max_dd = abs(drawdown.min()) * 100

    return {
        'total_trades': total_trades,
        'wins_count': len(wins),
        'losses_count': len(losses),
        'win_rate': float(win_rate),
        'avg_return': float(avg_return),
        'avg_win': float(avg_win),
        'avg_loss': float(avg_loss),
        'profit_factor': float(profit_factor),
        'max_drawdown': float(max_dd),
        'cum_return': float((equity - 1.0) * 100),
        'trades': trades
    }


def main():
    print("=" * 80)
    print("【步进式无未来函数】量化双策略真实胜率与回测对比系统")
    print(" 回测规则：逐日挂单撮合 | 严格 -8% 日内止损 | +15% / +46% 止盈 | 单仓位闭环")
    print("=" * 80)

    stocks = [
        ('MU', '美光科技'),
        ('SOXL', '半导体ETF'),
        ('NVDA', '英伟达'),
        ('AVGO', '博通'),
        ('COHR', 'Coherent光学材料'),
        ('NKE', '耐克'),
        ('AXTI', 'AXT光通信材料'),
        ('AAOI', '祥茂光电光模块'),
        ('LITE', 'Lumentum光器件'),
        ('SNDK', '闪迪')
    ]

    all_linear_results = []
    all_nonlinear_results = []
    dynamic_profiles = {}

    for code, name in stocks:
        print(f"\n正在回测: {code} ({name})...")
        df = fetch_stock_history(code, period="2y")
        if df is None or len(df) < 60:
            print(f"  ❌ {code} 数据不足，跳过")
            continue

        res_linear = simulate_strategy_walk_forward(df, strategy_type="linear")
        res_nonlinear = simulate_strategy_walk_forward(df, strategy_type="nonlinear")

        if res_linear and res_nonlinear:
            res_linear['code'] = code
            res_linear['name'] = name
            res_nonlinear['code'] = code
            res_nonlinear['name'] = name

            all_linear_results.append(res_linear)
            all_nonlinear_results.append(res_nonlinear)

            # 生成动态实测画像供日报脚本自动加载
            best_res = res_nonlinear if res_nonlinear['cum_return'] >= res_linear['cum_return'] else res_linear
            strat_label = "非线性" if best_res is res_nonlinear else "线性"
            dynamic_profiles[code] = {
                'stat': f"{strat_label}胜率 {best_res['win_rate']:.1f}% | 盈亏比 {best_res['profit_factor']:.2f}:1 | 累计 {best_res['cum_return']:+.1f}%",
            }

            print(f"  [线性策略]   交易: {res_linear['total_trades']:2d}次 | 胜率: {res_linear['win_rate']:5.1f}% | 均单收益: {res_linear['avg_return']:+5.2f}% | 累计: {res_linear['cum_return']:+6.1f}% | 最大回撤: {res_linear['max_drawdown']:4.1f}%")
            print(f"  [非线性策略] 交易: {res_nonlinear['total_trades']:2d}次 | 胜率: {res_nonlinear['win_rate']:5.1f}% | 均单收益: {res_nonlinear['avg_return']:+5.2f}% | 累计: {res_nonlinear['cum_return']:+6.1f}% | 最大回撤: {res_nonlinear['max_drawdown']:4.1f}%")

    # 持久化最新动态画像至 data/stock_profiles.json
    if dynamic_profiles:
        data_dir = BASE_DIR / "data"
        data_dir.mkdir(exist_ok=True)
        profile_path = data_dir / "stock_profiles.json"
        with open(profile_path, 'w', encoding='utf-8') as f:
            json.dump(dynamic_profiles, f, ensure_ascii=False, indent=2)
        print(f"\n📁 已将最新 2 年实测画像更新至: {profile_path}")

    # 打印汇总排行榜
    print("\n" + "=" * 90)
    print("【2年实盘级真实回测综合排行榜 (杜绝未来函数)】")
    print("=" * 90)
    print(f"{'策略类型':<10} {'标的':<18} {'真实胜率':<10} {'交易笔数':<10} {'盈亏比':<10} {'均单收益':<10} {'累计收益':<10} {'最大回撤':<10}")
    print("-" * 90)

    for r in all_linear_results:
        print(f"{'线性(+15%止盈)':<10} {r['code']+' '+r['name']:<18} {r['win_rate']:>7.1f}%  {r['total_trades']:>6d}笔    {r['profit_factor']:>6.2f}:1  {r['avg_return']:>+7.2f}%  {r['cum_return']:>+8.1f}%  {r['max_drawdown']:>7.1f}%")

    print("-" * 90)
    for r in all_nonlinear_results:
        print(f"{'非线性(+46%止盈)':<10} {r['code']+' '+r['name']:<18} {r['win_rate']:>7.1f}%  {r['total_trades']:>6d}笔    {r['profit_factor']:>6.2f}:1  {r['avg_return']:>+7.2f}%  {r['cum_return']:>+8.1f}%  {r['max_drawdown']:>7.1f}%")

    print("=" * 90)


if __name__ == "__main__":
    main()
