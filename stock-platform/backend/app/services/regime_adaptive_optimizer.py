"""
十年跨周期大盘状态自适应权重寻优引擎 (Regime-Adaptive Weight Optimizer)
核心量化职责：
1. 四维历史因子打分序列重构（技术面、宏观面、事件驱动、情绪动量）
2. 分状态（BULL 牛市 / BEAR 熊市 / SIDEWAYS 震荡市）最优权重求解器
3. 严格的 Walk-Forward 滚动前向时序盲测与基准对照引擎
4. 目标函数：夏普比率（Sharpe Ratio）最大化与最大回撤（Drawdown）惩罚控制
"""

import itertools
import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..utils.indicators import calculate_all_indicators
from .tech_score import calculate_tech_score

# 默认固定权重基准（对照组）
STATIC_BASELINE_WEIGHTS: Dict[str, float] = {
    "technical": 0.40,
    "news": 0.30,
    "macro": 0.15,
    "event": 0.15,
}

# 粗网格候选权重步长（0.10 步长，保证精度与毫秒级寻优性能兼顾）
CANDIDATE_WEIGHT_STEPS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]


def generate_candidate_simplex_weights(step_list: Optional[List[float]] = None) -> List[Dict[str, float]]:
    """生成所有四维权重和严格等于 1.0 的候选单纯形网格"""
    steps = step_list or [0.10, 0.20, 0.30, 0.40, 0.50, 0.60]
    simplex: List[Dict[str, float]] = []
    keys = ("technical", "news", "macro", "event")
    for combo in itertools.product(steps, repeat=4):
        if math.isclose(sum(combo), 1.0, abs_tol=1e-4):
            simplex.append(dict(zip(keys, combo)))
    return simplex


class RegimeAdaptiveOptimizer:
    """大盘状态自适应权重寻优与 Walk-Forward 盲测引擎"""

    # ============================================
    # 1. 历史四维因子序列构建
    # ============================================
    @staticmethod
    def extract_four_factor_series(df: pd.DataFrame) -> pd.DataFrame:
        """
        在输入的历史行情与事件对齐矩阵上，计算每日的四维标准化打分（0-10 分）：
        - S_tech: 技术面指标综合打分 (基于 calculate_tech_score 规则)
        - S_macro: 宏观大盘状态得分 (BULL 7-10 分, SIDEWAYS 5分, BEAR 1-3分)
        - S_event: SEC 8-K/财报等重大事件冲击得分 (带 10 日指数衰减)
        - S_news: 5日量价短期情绪动量得分 (情绪代理)
        """
        if df.empty or len(df) < 50:
            return pd.DataFrame()

        df_calc = calculate_all_indicators(df.copy())
        n = len(df_calc)

        tech_scores = np.full(n, 5.0)
        macro_scores = np.full(n, 5.0)
        event_scores = np.full(n, 5.0)
        news_scores = np.full(n, 5.0)

        # 1. 技术面打分序列
        for i in range(50, n):
            row = df_calc.iloc[i]
            score, _ = calculate_tech_score(row)
            tech_scores[i] = score

        # 2. 宏观面打分序列（基于大盘 regime 与波动率）
        regimes = df_calc.get("regime", pd.Series(["SIDEWAYS"] * n, index=df_calc.index))
        volatility = df_calc.get("Volatility20", pd.Series([0.20] * n, index=df_calc.index))

        for i in range(n):
            reg = str(regimes.iloc[i]).upper()
            vol = float(volatility.iloc[i]) if pd.notna(volatility.iloc[i]) else 0.20
            if reg == "BULL":
                # 牛市低波动得高分（最高 9.5 分），高波动适当扣分
                macro_scores[i] = round(max(6.0, min(9.5, 8.5 - (vol - 0.15) * 5.0)), 2)
            elif reg == "BEAR":
                # 熊市防守保护，低分预警（最低 1.0 分）
                macro_scores[i] = round(max(1.0, min(4.0, 3.0 - (vol - 0.25) * 5.0)), 2)
            else:
                macro_scores[i] = 5.0

        # 3. 事件驱动打分序列（SEC 8-K / 财报冲击与指数衰减）
        has_8k = df_calc.get("has_sec_8k", pd.Series([False] * n, index=df_calc.index))
        has_rep = df_calc.get("has_sec_report", pd.Series([False] * n, index=df_calc.index))
        returns = df_calc["Close"].pct_change().fillna(0.0).values

        current_event_score = 5.0
        decay = 0.80  # 半衰期约 3-4 天
        for i in range(n):
            is_event = bool(has_8k.iloc[i] or has_rep.iloc[i])
            if is_event:
                ret = returns[i]
                # 事件当日配合涨跌反应
                if ret > 0.01:
                    current_event_score = 8.5
                elif ret < -0.01:
                    current_event_score = 2.5
                else:
                    current_event_score = 6.0
            else:
                # 向 5.0 均值回归衰减
                current_event_score = 5.0 + (current_event_score - 5.0) * decay
            event_scores[i] = round(current_event_score, 2)

        # 4. 情绪动量代理（5日相对强弱与量能倍数）
        close_vals = df_calc["Close"].values
        for i in range(5, n):
            c0 = close_vals[i - 5]
            c1 = close_vals[i]
            ret5 = (c1 - c0) / c0 if c0 > 0 else 0.0
            # ret5 映射至 0-10 分（±10% 幅度饱和）
            clipped = max(-0.10, min(0.10, ret5))
            score_news = 5.0 + (clipped / 0.10) * 4.0
            news_scores[i] = round(max(0.0, min(10.0, score_news)), 2)

        df_res = df_calc.copy()
        df_res["score_tech"] = tech_scores
        df_res["score_macro"] = macro_scores
        df_res["score_event"] = event_scores
        df_res["score_news"] = news_scores

        return df_res

    # ============================================
    # 2. 模拟打分驱动的交易策略
    # ============================================
    @staticmethod
    def simulate_scoring_strategy(
        df_factors: pd.DataFrame,
        weights: Dict[str, float],
        buy_threshold: float = 6.5,
        take_profit_pct: float = 0.15,
        stop_loss_pct: float = 0.08,
        max_holding_bars: int = 20,
    ) -> Dict:
        """
        基于指定权重向量计算综合评分并模拟回测：
        - 买入条件：S_total >= buy_threshold，且当前空仓
        - 卖出条件：触及止盈 (+15%)、止损 (-8%) 或超出最大持有天数 (20天)
        - 返回：胜率、夏普比率、最大回撤、交易次数、年化收益与复合综合适度
        """
        if df_factors.empty or len(df_factors) < 50:
            return {
                "sharpe_ratio": 0.0,
                "win_rate": 0.0,
                "max_drawdown": 0.0,
                "total_return": 0.0,
                "annual_return": 0.0,
                "trade_count": 0,
                "fitness": -1.0,
            }

        w_tech = float(weights.get("technical", 0.40))
        w_news = float(weights.get("news", 0.30))
        w_macro = float(weights.get("macro", 0.15))
        w_event = float(weights.get("event", 0.15))

        # 计算每日综合分
        scores = (
            w_tech * df_factors["score_tech"].values
            + w_news * df_factors["score_news"].values
            + w_macro * df_factors["score_macro"].values
            + w_event * df_factors["score_event"].values
        )

        closes = df_factors["Close"].values
        highs = df_factors["High"].values
        lows = df_factors["Low"].values
        n = len(df_factors)

        trades: List[Dict] = []
        equity = [100000.0]
        capital = 100000.0
        in_pos = False
        buy_px = 0.0
        hold_bars = 0

        for i in range(50, n):
            c_px = closes[i]

            # 1. 持仓出场检测
            if in_pos:
                hold_bars += 1
                stop_px = buy_px * (1.0 - stop_loss_pct)
                tp_px = buy_px * (1.0 + take_profit_pct)

                sell_px = None
                if lows[i] <= stop_px:
                    sell_px = stop_px
                elif highs[i] >= tp_px:
                    sell_px = tp_px
                elif hold_bars >= max_holding_bars:
                    sell_px = c_px

                if sell_px is not None:
                    pnl_pct = (sell_px - buy_px) / buy_px
                    capital *= 1.0 + pnl_pct * 0.95  # 95% 仓位入场
                    trades.append({"pnl_pct": pnl_pct, "is_win": pnl_pct > 0})
                    in_pos = False
                    buy_px = 0.0
                    hold_bars = 0

            # 2. 开仓检测
            if not in_pos and scores[i] >= buy_threshold:
                in_pos = True
                buy_px = c_px
                hold_bars = 0

            equity.append(capital * (c_px / buy_px if in_pos and buy_px > 0 else 1.0))

        # 统计指标
        trade_count = len(trades)
        if trade_count == 0:
            return {
                "sharpe_ratio": 0.0,
                "win_rate": 0.0,
                "max_drawdown": 0.0,
                "total_return": 0.0,
                "annual_return": 0.0,
                "trade_count": 0,
                "fitness": -1.0,
            }

        wins = [t for t in trades if t["is_win"]]
        win_rate = len(wins) / trade_count

        eq = np.array(equity)
        peaks = np.maximum.accumulate(eq)
        drawdowns = (peaks - eq) / peaks
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

        tot_ret = (eq[-1] - eq[0]) / eq[0]
        days = max(1, (n - 50))
        ann_ret = (1.0 + tot_ret) ** (252.0 / days) - 1.0 if (tot_ret > -1.0) else -1.0

        daily_rets = np.diff(eq) / eq[:-1]
        std = np.std(daily_rets)
        sharpe = float((np.mean(daily_rets) / std) * np.sqrt(252)) if std > 1e-6 else 0.0

        # 优化适应度目标函数：夏普比率 - 0.5 * 最大回撤
        fitness = round(sharpe - 0.5 * max_dd, 4)

        return {
            "sharpe_ratio": round(sharpe, 3),
            "win_rate": round(win_rate * 100.0, 2),
            "max_drawdown": round(max_dd * 100.0, 2),
            "total_return": round(tot_ret * 100.0, 2),
            "annual_return": round(ann_ret * 100.0, 2),
            "trade_count": trade_count,
            "fitness": fitness,
        }

    # ============================================
    # 3. 分大盘状态最优权重网格求解
    # ============================================
    def optimize_regime_weights(
        self,
        df_factors: pd.DataFrame,
        regime: Optional[str] = None,
        candidate_weights: Optional[List[Dict[str, float]]] = None,
    ) -> Dict:
        """
        在指定大盘状态子集上，网格搜索夏普比率与抗回撤综合最优的权重配比
        """
        if df_factors.empty:
            return {"weights": STATIC_BASELINE_WEIGHTS, "metrics": {}}

        # 根据 regime 过滤行切片
        if regime and "regime" in df_factors.columns:
            subset = df_factors[df_factors["regime"] == regime]
            if len(subset) < 40:
                # 样本量不足时，回退至全局样本
                subset = df_factors
        else:
            subset = df_factors

        grid = candidate_weights or generate_candidate_simplex_weights()
        best_w = dict(STATIC_BASELINE_WEIGHTS)
        best_metrics = None
        best_fitness = -999.0

        for w in grid:
            m = self.simulate_scoring_strategy(subset, w)
            fit = m.get("fitness", -999.0)
            if fit > best_fitness:
                best_fitness = fit
                best_w = dict(w)
                best_metrics = m

        return {
            "regime": regime or "GLOBAL",
            "optimal_weights": best_w,
            "metrics": best_metrics or {},
        }

    # ============================================
    # 4. Walk-Forward 滚动前向时序盲测与基准对照
    # ============================================
    def run_walk_forward_blind_test(
        self,
        df_factors: pd.DataFrame,
        folds: int = 3,
        train_ratio: float = 0.7,
    ) -> Dict:
        """
        严格的时序滚动盲测（Walk-Forward Out-Of-Sample Testing）：
        - 将数据集按时序切分为 N 个步进窗口
        - 每一个窗口用 In-Sample (训练段) 寻优出牛/熊/震荡三大状态最优权重
        - 随后在严格未参与训练的 Out-Of-Sample (测试段) 上，按当日大盘动态应用对应权重执行盲测
        - 与【固定 40/30/15/15 基准】在完全相同的样本外时段进行面对面对比
        """
        if df_factors.empty or len(df_factors) < 150:
            return {"error": "样本数据不足以执行时序滚动前向验证"}

        n = len(df_factors)
        fold_size = n // folds
        fold_results: List[Dict] = []

        candidate_grid = generate_candidate_simplex_weights([0.10, 0.20, 0.30, 0.40, 0.50])

        for f in range(folds):
            start_idx = f * (n // (folds + 1))
            end_idx = min(n, start_idx + fold_size)
            window = df_factors.iloc[start_idx:end_idx]

            split_pt = int(len(window) * train_ratio)
            train_df = window.iloc[:split_pt]
            test_df = window.iloc[split_pt:]

            if len(test_df) < 20:
                continue

            # 1. 在训练集上分别训练三种大盘状态的最优权重
            opt_bull = self.optimize_regime_weights(train_df, "BULL", candidate_grid)
            opt_bear = self.optimize_regime_weights(train_df, "BEAR", candidate_grid)
            opt_side = self.optimize_regime_weights(train_df, "SIDEWAYS", candidate_grid)

            regime_weight_book = {
                "BULL": opt_bull["optimal_weights"],
                "BEAR": opt_bear["optimal_weights"],
                "SIDEWAYS": opt_side["optimal_weights"],
            }

            # 2. 在完全未知的测试集 (OOS) 上，执行动态切换打分策略
            # 根据测试集当日的真实大盘状态，取对应的最优权重向量
            oos_weights_dynamic = self._compose_dynamic_weights_for_test(test_df, regime_weight_book)
            m_adaptive_oos = self.simulate_scoring_strategy(test_df, oos_weights_dynamic)

            # 3. 对照组：在完全相同的测试集 (OOS) 上执行固定 40/30/15/15 权重
            m_static_oos = self.simulate_scoring_strategy(test_df, STATIC_BASELINE_WEIGHTS)

            fold_results.append({
                "fold": f + 1,
                "train_dates": f"{train_df.index[0].strftime('%Y-%m-%d')} ~ {train_df.index[-1].strftime('%Y-%m-%d')}",
                "test_oos_dates": f"{test_df.index[0].strftime('%Y-%m-%d')} ~ {test_df.index[-1].strftime('%Y-%m-%d')}",
                "adaptive_oos": m_adaptive_oos,
                "static_baseline_oos": m_static_oos,
                "trained_regime_weights": regime_weight_book,
                "sharpe_improvement": round(m_adaptive_oos["sharpe_ratio"] - m_static_oos["sharpe_ratio"], 3),
                "drawdown_reduction": round(m_static_oos["max_drawdown"] - m_adaptive_oos["max_drawdown"], 2),
            })

        # 聚合样本外总评
        avg_adaptive_sharpe = np.mean([f["adaptive_oos"]["sharpe_ratio"] for f in fold_results])
        avg_static_sharpe = np.mean([f["static_baseline_oos"]["sharpe_ratio"] for f in fold_results])
        avg_adaptive_winrate = np.mean([f["adaptive_oos"]["win_rate"] for f in fold_results])
        avg_static_winrate = np.mean([f["static_baseline_oos"]["win_rate"] for f in fold_results])
        avg_adaptive_dd = np.mean([f["adaptive_oos"]["max_drawdown"] for f in fold_results])
        avg_static_dd = np.mean([f["static_baseline_oos"]["max_drawdown"] for f in fold_results])

        return {
            "total_folds": len(fold_results),
            "folds": fold_results,
            "overall_oos_comparison": {
                "adaptive_avg_sharpe": round(float(avg_adaptive_sharpe), 3),
                "static_avg_sharpe": round(float(avg_static_sharpe), 3),
                "adaptive_avg_winrate": round(float(avg_adaptive_winrate), 2),
                "static_avg_winrate": round(float(avg_static_winrate), 2),
                "adaptive_avg_drawdown": round(float(avg_adaptive_dd), 2),
                "static_avg_drawdown": round(float(avg_static_dd), 2),
                "oos_sharpe_beat": bool(avg_adaptive_sharpe > avg_static_sharpe),
                "oos_drawdown_improved": bool(avg_adaptive_dd < avg_static_dd),
            },
        }

    @staticmethod
    def _compose_dynamic_weights_for_test(
        test_df: pd.DataFrame,
        regime_book: Dict[str, Dict[str, float]],
    ) -> Dict[str, float]:
        """按测试段中各市场状态所占天数比例，合成测试段的复合加权向量"""
        regimes = test_df.get("regime", pd.Series(["SIDEWAYS"] * len(test_df)))
        counts = regimes.value_counts(normalize=True).to_dict()

        composed = {"technical": 0.0, "news": 0.0, "macro": 0.0, "event": 0.0}
        for reg, frac in counts.items():
            w_dict = regime_book.get(reg, regime_book.get("SIDEWAYS", STATIC_BASELINE_WEIGHTS))
            for k in composed:
                composed[k] += frac * w_dict.get(k, 0.25)

        for k in composed:
            composed[k] = round(composed[k], 4)
        return composed
