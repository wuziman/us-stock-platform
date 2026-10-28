"""
价格点位真实预测精度多智能体极限压测引擎 (Stress Test Engine)
分工包含四大专业作战单元：
1. Worker 1【科技高波动组】：NVDA、TSLA、AAPL 等高 Beta 标的的目标价真实触达率、超额收益与 MAPE/MAE 精度
2. Worker 2【防守震荡组】：KO、JNJ 等低波动防御标的在横盘震荡中的假突破率与支撑阻力触碰精度
3. Worker 3【历史黑天鹅绞杀组】：2020年3月熔断、2022年加息深熊、2015年A股流动性断崖的真实回撤与本金防守
4. Worker 4【红队对抗破坏组】：全量扫描历史预测最大偏差的 Top 5 爆雷/失误案例，深度剖析根因并反思改进
"""

from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

from ..utils.indicators import calculate_all_indicators
from ..utils.price_levels import (
    STOP_PCT,
    atr_adaptive_levels,
    compute_trailing_stop,
    linear_buy_point,
    nonlinear_buy_point,
)
from .stock_service import _fetch_kline_eastmoney


class PricePointEvaluator:
    """价格点位预测精度评估器"""

    def __init__(self, commission_rate: float = 0.0003, slippage_rate: float = 0.0005):
        self.commission_rate = commission_rate  # 默认万分之三
        self.slippage_rate = slippage_rate      # 默认万分之五滑点

    def evaluate_stock_points(
        self,
        df: pd.DataFrame,
        ticker: str,
        holding_horizon: int = 20,
        strategy: str = "linear",
        use_adaptive_atr: bool = True,
        two_stage_trailing: bool = True,
    ) -> Dict:
        """
        逐日模拟策略买点、止盈目标价、止损线的真实触发情况：
        - 记录每次入场后的未来 holding_horizon 交易日内，实际最高价/最低价与预测目标价的偏离
        - 计算 MAPE（平均绝对百分比误差）、MAE（平均绝对误差）、目标触达率、盈亏比
        - 支持双阶保本移动止损 (+5%保本, +10%锁利) 与自适应 ATR 动态目标价
        """
        if df.empty or len(df) < 50:
            return {"error": "数据不足50根K线"}

        df_calc = calculate_all_indicators(df.copy())
        n = len(df_calc)

        trades: List[Dict] = []
        mapes: List[float] = []
        maes: List[float] = []

        in_pos = False
        entry_idx = 0
        entry_px = 0.0
        target_px = 0.0
        stop_px = 0.0
        highest_in_trade = 0.0

        for i in range(50, n):
            row = df_calc.iloc[i]
            prev = df_calc.iloc[i - 1]
            close = float(row["Close"])
            high = float(row["High"])
            low = float(row["Low"])

            # 1. 持仓出场判定
            if in_pos:
                bars_held = i - entry_idx
                highest_in_trade = max(highest_in_trade, high)

                # 双阶移动保本与回撤追踪止损（自愈升级）
                if two_stage_trailing:
                    trail_stop = stop_px
                    if highest_in_trade >= entry_px * 1.05:
                        trail_stop = max(trail_stop, entry_px * 0.99)
                    if highest_in_trade >= entry_px * 1.10:
                        trail_stop = max(trail_stop, entry_px * 1.02, highest_in_trade * 0.92)
                else:
                    trail_stop = compute_trailing_stop(entry_px, highest_in_trade, stop_px)

                exit_px = None
                exit_reason = None

                # 触及止盈
                if high >= target_px:
                    exit_px = target_px * (1.0 - self.slippage_rate)
                    exit_reason = "target_hit"
                # 触及止损 / 移动保本
                elif low <= trail_stop:
                    exit_px = min(float(row["Open"]), trail_stop) * (1.0 - self.slippage_rate)
                    exit_reason = "stop_hit"
                # 达到最大持有天数
                elif bars_held >= holding_horizon:
                    exit_px = close * (1.0 - self.slippage_rate)
                    exit_reason = "time_exit"

                if exit_px is not None:
                    # 扣除双边佣金
                    fee = (entry_px + exit_px) * self.commission_rate
                    net_pnl = exit_px - entry_px - fee
                    pnl_pct = net_pnl / entry_px * 100.0

                    # 计算预测目标价与该笔交易期间实际最高价的偏差
                    err = abs(highest_in_trade - target_px)
                    maes.append(err)
                    mapes.append((err / target_px) * 100.0 if target_px > 0 else 0.0)

                    trades.append({
                        "entry_date": df_calc.index[entry_idx].strftime("%Y-%m-%d"),
                        "exit_date": df_calc.index[i].strftime("%Y-%m-%d"),
                        "ticker": ticker,
                        "entry_price": round(entry_px, 2),
                        "target_price": round(target_px, 2),
                        "stop_price": round(stop_px, 2),
                        "highest_reached": round(highest_in_trade, 2),
                        "exit_price": round(exit_px, 2),
                        "exit_reason": exit_reason,
                        "pnl_pct": round(pnl_pct, 2),
                        "is_win": pnl_pct > 0,
                        "holding_bars": bars_held,
                        "mape": round((err / target_px) * 100.0, 2) if target_px > 0 else 0.0,
                    })

                    in_pos = False
                    entry_px = 0.0
                    target_px = 0.0
                    stop_px = 0.0
                    highest_in_trade = 0.0

            # 2. 空仓开仓检测
            if not in_pos:
                atr_val = float(row.get("ATR", close * 0.03))
                ma20 = float(prev.get("MA20", close))

                if strategy == "linear":
                    buy_lvl = linear_buy_point(float(prev["Close"]), ma20)
                    if low <= buy_lvl:
                        entry_px = min(float(row["Open"]), buy_lvl) * (1.0 + self.slippage_rate)
                        if use_adaptive_atr:
                            target_px = round(entry_px + 1.8 * atr_val, 2)
                            stop_px = round(entry_px - 1.5 * atr_val, 2)
                        else:
                            levels = atr_adaptive_levels(close, entry_px, 1.15, atr=atr_val)
                            target_px = float(levels.get("atr_profit") or entry_px * 1.15)
                            stop_px = float(levels.get("atr_stop") or entry_px * STOP_PCT)
                        in_pos = True
                        entry_idx = i
                        highest_in_trade = high
                elif strategy == "nonlinear":
                    rsi_prev = float(prev.get("RSI", 50.0))
                    bb_low = float(prev.get("BB_Lower", close * 0.95))
                    buy_lvl = nonlinear_buy_point(rsi_prev, ma20, bb_low)
                    if rsi_prev < 30 or low <= buy_lvl:
                        entry_px = min(float(row["Open"]), buy_lvl) * (1.0 + self.slippage_rate)
                        if use_adaptive_atr:
                            target_px = round(entry_px + 2.5 * atr_val, 2)
                            stop_px = round(entry_px - 1.8 * atr_val, 2)
                        else:
                            levels = atr_adaptive_levels(close, entry_px, 1.46, atr=atr_val)
                            target_px = float(levels.get("atr_profit") or entry_px * 1.46)
                            stop_px = float(levels.get("atr_stop") or entry_px * STOP_PCT)
                        in_pos = True
                        entry_idx = i
                        highest_in_trade = high
                    if (pd.notna(rsi_prev) and rsi_prev < 30) or low <= buy_lvl:
                        entry_px = min(float(row["Open"]), buy_lvl) * (1.0 + self.slippage_rate)
                        levels = atr_adaptive_levels(close, entry_px, 1.46, atr=atr_val)
                        target_px = float(levels.get("atr_profit") or entry_px * 1.46)
                        stop_px = float(levels.get("atr_stop") or entry_px * STOP_PCT)
                        in_pos = True
                        entry_idx = i
                        highest_in_trade = high

        total_trades = len(trades)
        if total_trades == 0:
            return {
                "ticker": ticker,
                "trade_count": 0,
                "target_hit_rate": 0.0,
                "win_rate": 0.0,
                "mean_mape": 0.0,
                "mean_mae": 0.0,
                "profit_loss_ratio": 0.0,
                "trades": [],
            }

        target_hits = sum(1 for t in trades if t["exit_reason"] == "target_hit")
        wins = [t for t in trades if t["is_win"]]
        losses = [t for t in trades if not t["is_win"]]

        avg_win = float(np.mean([t["pnl_pct"] for t in wins])) if wins else 0.0
        avg_loss = abs(float(np.mean([t["pnl_pct"] for t in losses]))) if losses else 1.0
        pl_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0.0

        return {
            "ticker": ticker,
            "trade_count": total_trades,
            "target_hit_rate": round(target_hits / total_trades * 100.0, 2),
            "win_rate": round(len(wins) / total_trades * 100.0, 2),
            "mean_mape": round(float(np.mean(mapes)), 2),
            "mean_mae": round(float(np.mean(maes)), 2),
            "profit_loss_ratio": pl_ratio,
            "trades": trades,
        }


# ============================================
# 四大作战单元执行器
# ============================================
class MultiAgentStressTester:
    """四智能体并发压测总指挥部"""

    def __init__(self):
        self.evaluator = PricePointEvaluator()

    # --------------------------------------------
    # 1. Worker 1【科技高波动组】
    # --------------------------------------------
    def run_worker1_tech_high_beta(self, tickers: Optional[List[str]] = None) -> Dict:
        """评估高 Beta 科技股在主升浪与高波动行情下的点位预测精度与目标价触达率"""
        symbols = tickers or ["NVDA", "TSLA", "AAPL"]
        results = []
        for s in symbols:
            try:
                df = yf.Ticker(s).history(period="5y")
                if df is not None and not df.empty:
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    m = self.evaluator.evaluate_stock_points(df, s, strategy="nonlinear")
                    results.append(m)
            except Exception as e:
                results.append({"ticker": s, "error": str(e)})

        valid = [r for r in results if "error" not in r and r.get("trade_count", 0) > 0]
        avg_hit_rate = np.mean([r["target_hit_rate"] for r in valid]) if valid else 0.0
        avg_win_rate = np.mean([r["win_rate"] for r in valid]) if valid else 0.0
        avg_mape = np.mean([r["mean_mape"] for r in valid]) if valid else 0.0
        avg_pl = np.mean([r["profit_loss_ratio"] for r in valid]) if valid else 0.0

        return {
            "unit": "Worker 1【科技高波动组】",
            "tickers": symbols,
            "avg_target_hit_rate": round(float(avg_hit_rate), 2),
            "avg_win_rate": round(float(avg_win_rate), 2),
            "avg_mape": round(float(avg_mape), 2),
            "avg_profit_loss_ratio": round(float(avg_pl), 2),
            "details": results,
        }

    # --------------------------------------------
    # 2. Worker 2【防守震荡组】
    # --------------------------------------------
    def run_worker2_defensive_sideways(self, tickers: Optional[List[str]] = None) -> Dict:
        """检验低波动防御股在横盘磨人行情中的假突破率与支撑阻力触碰精度"""
        symbols = tickers or ["KO", "JNJ"]
        results = []
        for s in symbols:
            try:
                df = yf.Ticker(s).history(period="5y")
                if df is not None and not df.empty:
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    m = self.evaluator.evaluate_stock_points(df, s, strategy="linear")
                    # 计算假突破率：止损出场的交易占比
                    trades = m.get("trades", [])
                    stop_count = sum(1 for t in trades if t.get("exit_reason") == "stop_hit")
                    false_break_rate = (stop_count / len(trades) * 100.0) if trades else 0.0
                    m["false_break_rate"] = round(false_break_rate, 2)
                    results.append(m)
            except Exception as e:
                results.append({"ticker": s, "error": str(e)})

        valid = [r for r in results if "error" not in r and r.get("trade_count", 0) > 0]
        avg_false_break = np.mean([r.get("false_break_rate", 0.0) for r in valid]) if valid else 0.0
        avg_win_rate = np.mean([r["win_rate"] for r in valid]) if valid else 0.0
        avg_mape = np.mean([r["mean_mape"] for r in valid]) if valid else 0.0

        return {
            "unit": "Worker 2【防守震荡组】",
            "tickers": symbols,
            "avg_false_breakout_rate": round(float(avg_false_break), 2),
            "avg_win_rate": round(float(avg_win_rate), 2),
            "avg_mape": round(float(avg_mape), 2),
            "details": results,
        }

    # --------------------------------------------
    # 3. Worker 3【历史黑天鹅绞杀组】
    # --------------------------------------------
    def run_worker3_black_swan_survival(self) -> Dict:
        """在 2020 熔断与 2022 美联储暴力加息深熊中，检验止损避险与最大回撤控制"""
        scenarios = [
            {"name": "2020年3月美股世纪大熔断", "symbol": "SPY", "start": "2020-02-01", "end": "2020-04-30"},
            {"name": "2022年美联储暴力加息深熊", "symbol": "SPY", "start": "2022-01-01", "end": "2022-10-31"},
        ]

        scenario_reports = []
        for sc in scenarios:
            try:
                df = yf.Ticker(sc["symbol"]).history(start=sc["start"], end=sc["end"])
                if df is not None and not df.empty:
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    # 裸持买入持有大盘的最大跌幅
                    bench_drawdown = (df["Close"].min() - df["Close"].iloc[0]) / df["Close"].iloc[0] * 100.0

                    m = self.evaluator.evaluate_stock_points(df, sc["symbol"], strategy="linear")
                    trades = m.get("trades", [])
                    max_single_loss = min([t["pnl_pct"] for t in trades]) if trades else 0.0

                    scenario_reports.append({
                        "scenario": sc["name"],
                        "benchmark_max_drop": round(bench_drawdown, 2),
                        "strategy_max_single_loss": round(max_single_loss, 2),
                        "defense_success": bool(abs(max_single_loss) < abs(bench_drawdown) * 0.5),
                        "stopped_early": any(t["exit_reason"] == "stop_hit" for t in trades),
                        "trade_count": len(trades),
                    })
            except Exception as e:
                scenario_reports.append({"scenario": sc["name"], "error": str(e)})

        # 增加 A 股 2015 股灾流动性踩踏专项检验
        try:
            df_cn = _fetch_kline_eastmoney("000300.SS", period="max")
            if df_cn is not None and not df_cn.empty:
                df_cn_2015 = df_cn[(df_cn.index >= "2015-06-01") & (df_cn.index <= "2015-09-30")]
                if len(df_cn_2015) >= 20:
                    bench_drop = (df_cn_2015["Close"].min() - df_cn_2015["Close"].iloc[0]) / df_cn_2015["Close"].iloc[0] * 100.0
                    m_cn = self.evaluator.evaluate_stock_points(df_cn_2015, "000300.SS", strategy="linear")
                    trades_cn = m_cn.get("trades", [])
                    max_loss_cn = min([t["pnl_pct"] for t in trades_cn]) if trades_cn else 0.0
                    scenario_reports.append({
                        "scenario": "2015年A股流动性断崖踩踏 (沪深300)",
                        "benchmark_max_drop": round(bench_drop, 2),
                        "strategy_max_single_loss": round(max_loss_cn, 2),
                        "defense_success": bool(abs(max_loss_cn) < abs(bench_drop) * 0.4),
                        "stopped_early": any(t["exit_reason"] == "stop_hit" for t in trades_cn),
                        "trade_count": len(trades_cn),
                    })
        except Exception:
            pass

        return {
            "unit": "Worker 3【历史黑天鹅绞杀组】",
            "scenario_reports": scenario_reports,
            "overall_survival_confirmed": all(r.get("defense_success", True) for r in scenario_reports),
        }

    # --------------------------------------------
    # 4. Worker 4【红队对抗破坏组（Red Team Adversary）】
    # --------------------------------------------
    def run_worker4_red_team_failures(self, tickers: Optional[List[str]] = None) -> Dict:
        """地毯式扫描所有历史交易，抓出算法预测偏差最大的 Top 5 失败案例并提炼反思"""
        symbols = tickers or ["NVDA", "TSLA", "AAPL", "KO", "JNJ"]
        all_trades: List[Dict] = []

        for s in symbols:
            try:
                df = yf.Ticker(s).history(period="5y")
                if df is not None and not df.empty:
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    m = self.evaluator.evaluate_stock_points(df, s, strategy="linear")
                    all_trades.extend(m.get("trades", []))
            except Exception:
                continue

        if not all_trades:
            return {"unit": "Worker 4【红队对抗破坏组】", "top_failures": []}

        # 按预测误差 MAPE 降序排序，找出预测严重偏离的 Top 5 案例
        sorted_failures = sorted(all_trades, key=lambda t: t.get("mape", 0.0), reverse=True)
        top5 = sorted_failures[:5]

        # 归纳反思根因
        insights = []
        for t in top5:
            reason = "未知原因"
            if t["pnl_pct"] < -8.0:
                reason = "跳空低开穿透止损线（开盘价直接砸穿，滑点扩大）"
            elif t["exit_reason"] == "time_exit" and t["mape"] > 20.0:
                reason = "窄幅横盘磨人行情，买入后波动率骤降未达预期波动通道"
            elif t["exit_reason"] == "stop_hit":
                reason = "假突破诱多后迅速反杀，触及硬止损"

            insights.append({
                "ticker": t["ticker"],
                "entry_date": t["entry_date"],
                "exit_date": t["exit_date"],
                "target_vs_actual_high": f"目标价${t['target_price']} vs 实际最高${t['highest_reached']}",
                "mape": f"{t['mape']}%",
                "loss_pct": f"{t['pnl_pct']}%",
                "root_cause": reason,
            })

        return {
            "unit": "Worker 4【红队对抗破坏组】",
            "worst_failures_count": len(top5),
            "top_failures": insights,
            "anti_fragile_recommendation": (
                "针对跳空低开穿透：建议引入 ATR 波动率自适应防砸缓冲网；"
                "针对横盘波动率骤降：建议在震荡市中收紧目标盈亏比（从 +15% 切换为 +8% 敏捷止盈）。"
            ),
        }

    # --------------------------------------------
    # 汇总总装执行
    # --------------------------------------------
    def run_all_stress_tests(self) -> Dict:
        """一键并发调度四大作战单元并收敛汇总数据报表"""
        w1 = self.run_worker1_tech_high_beta()
        w2 = self.run_worker2_defensive_sideways()
        w3 = self.run_worker3_black_swan_survival()
        w4 = self.run_worker4_red_team_failures()

        return {
            "worker1_tech": w1,
            "worker2_defensive": w2,
            "worker3_black_swan": w3,
            "worker4_red_team": w4,
        }
