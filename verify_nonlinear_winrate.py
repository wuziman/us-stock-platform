#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[口径已统一至 BacktestService]
原独立回测脚本（100%满仓0手续费复利 + 30/60天强制超时平仓）已归档至 archive/verify_nonlinear_winrate_v1_archived.py。
当前模块直接复用 stock-platform/backend/app/services/backtest_service.py 的 BacktestService.evaluate_df_profile：
- 入场当日不判卖出（bought_this_bar=True，严防日内高低点先后顺序未知的前视偏差）
- 95% 仓位分配 + 每笔 $1 手续费
- 同一参数下与平台 BacktestService 的胜率、收益率、最大回撤、成交笔数 100% 严格一致
"""

import json
import sys
from pathlib import Path
import pandas as pd
try:
    import yfinance as yf
except ImportError:
    yf = None

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "stock-platform" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.backtest_service import BacktestService  # noqa: E402


def fetch_stock_history(stock_code: str, period: str = "2y") -> pd.DataFrame:
    """获取历史行情数据，若网络受阻则自动从本地 Excel 兜底"""
    df = pd.DataFrame()
    try:
        stock = yf.Ticker(stock_code)
        df = stock.history(period=period)
    except Exception:
        pass

    if df is None or df.empty:
        candidates = list(BASE_DIR.glob(f"**/{stock_code}*_data.xlsx"))
        if candidates:
            df = pd.read_excel(candidates[0])
            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"])
                df = df.set_index("Date")
            print(f"  [本地缓存] {stock_code} 加载本地数据 ({len(df)} 条)")
    return df


def simulate_strategy_walk_forward(
    df: pd.DataFrame,
    strategy_type: str = "nonlinear",
    initial_capital: float = 100000.0,
    commission_per_trade: float = 1.0,
) -> dict:
    """直接委托 BacktestService.evaluate_df_profile 执行回测，确保全栈单一回测口径"""
    svc = BacktestService()
    return svc.evaluate_df_profile(
        df=df,
        strategy=strategy_type,
        initial_capital=initial_capital,
        commission_per_trade=commission_per_trade,
    )


def main():
    stocks = [
        ("MU", "美光科技"), ("SOXL", "半导体ETF"), ("NVDA", "英伟达"), ("AVGO", "博通"),
        ("COHR", "Coherent光学材料"), ("NKE", "耐克"), ("AXTI", "AXT光通信材料"),
        ("AAOI", "祥茂光电光模块"), ("LITE", "Lumentum光器件"), ("SNDK", "闪迪"),
    ]
    dynamic_profiles = {}
    for code, name in stocks:
        df = fetch_stock_history(code, period="2y")
        if df is None or len(df) < 60:
            continue
        res_linear = simulate_strategy_walk_forward(df, "linear")
        res_nonlinear = simulate_strategy_walk_forward(df, "nonlinear")
        if res_linear and res_nonlinear:
            best_res = res_nonlinear if res_nonlinear["cum_return"] >= res_linear["cum_return"] else res_linear
            strat_label = "非线性" if best_res is res_nonlinear else "线性"
            dynamic_profiles[code] = {
                "win_rate": round(best_res["win_rate"], 1),
                "reward_risk_ratio": round(best_res["profit_factor"], 2),
                "calibration_source": "BacktestService (95%仓位|$1手续费|入场次日判离场)",
                "stat": (
                    f"[口径:BacktestService] {strat_label}胜率 {best_res['win_rate']:.1f}% | "
                    f"盈亏比 {best_res['profit_factor']:.2f}:1 | 累计 {best_res['cum_return']:+.1f}%"
                ),
            }
    if dynamic_profiles:
        data_dir = BASE_DIR / "data"
        data_dir.mkdir(exist_ok=True)
        (data_dir / "stock_profiles.json").write_text(
            json.dumps(dynamic_profiles, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return dynamic_profiles


if __name__ == "__main__":
    main()
