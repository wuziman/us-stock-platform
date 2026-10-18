# -*- coding: utf-8 -*-
"""
第三批·回测优化测试：策略参数化 / 网格寻优(optimize) / Walk-Forward验证
全部使用合成数据直接调用服务方法，不依赖网络。
运行：cd backend && python -m pytest tests/test_optimize_wf.py -v
"""
import sys
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.backtest_service import (
    BacktestService,
    PARAM_GRIDS,
    DEFAULT_PARAMS,
)
from app.utils.indicators import calculate_all_indicators


# ============================================
# 合成数据（与改造前基准采集所用完全一致：seed=7 波动上行260日）
# ============================================
def make_wave_df(days: int = 260, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = np.arange(days)
    close = np.round(100 + 20 * np.sin(t / 15) + 0.08 * t + rng.normal(0, 0.4, days), 2)
    open_ = np.round(close + rng.normal(0, 0.3, days), 2)
    high = np.round(np.maximum(open_, close) + 0.8, 2)
    low = np.round(np.minimum(open_, close) - 0.8, 2)
    idx = pd.date_range('2025-01-01', periods=days, freq='B')
    return pd.DataFrame({'Open': open_, 'High': high, 'Low': low,
                         'Close': close, 'Volume': 1_000_000}, index=idx)


def _wave_with_indicators(days: int = 260, seed: int = 7) -> pd.DataFrame:
    return calculate_all_indicators(make_wave_df(days, seed))


# 当前引擎在同一合成数据上的结果（回归锚点）
BASELINE = {
    # strategy: (final_value, len(trades))
    'linear': (97236.78, 6),
    'nonlinear': (101119.17, 13),
    'ma_cross': (108621.99, 4),
    'macd': (206913.31, 5),
}


# ============================================
# 策略参数化：不传参数 = 原硬编码结果；显式默认参数 = 不传参数
# ============================================
class TestParametrizedStrategies:
    def test_default_matches_pre_refactor_baseline(self):
        """4策略不传params必须与旧硬编码逻辑逐位一致（final_value/成交笔数）"""
        svc = BacktestService()
        dfi = _wave_with_indicators()
        for strat, (exp_final, exp_trades) in BASELINE.items():
            fn = getattr(svc, f'_backtest_{strat}')
            trades, curve = fn(dfi, 100000, 1.0)
            assert curve[-1][1] == exp_final, \
                f"{strat} 默认参数回归失败: {curve[-1][1]} != {exp_final}"
            assert len(trades) == exp_trades

    @pytest.mark.parametrize('strategy', ['linear', 'nonlinear', 'ma_cross', 'macd'])
    def test_explicit_default_params_equal_no_params(self, strategy):
        """显式传入默认参数dict的结果与不传参数完全一致"""
        svc = BacktestService()
        dfi = _wave_with_indicators()
        fn = getattr(svc, f'_backtest_{strategy}')
        tr_none, c_none = fn(dfi.copy(), 100000, 1.0)
        tr_def, c_def = fn(dfi.copy(), 100000, 1.0, dict(DEFAULT_PARAMS[strategy]))
        assert c_none == c_def and tr_none == tr_def

    def test_linear_tp_sl_params_change_behavior(self):
        """自定义tp/sl生效：放宽止盈止损后交易行为应改变且曲线仍完整"""
        svc = BacktestService()
        dfi = _wave_with_indicators()
        _, c_tight = svc._backtest_linear(dfi.copy(), 100000, 1.0,
                                          {'tp': 0.05, 'sl': 0.03})
        _, c_loose = svc._backtest_linear(dfi.copy(), 100000, 1.0,
                                          {'tp': 0.30, 'sl': 0.20})
        assert c_tight[-1][1] != c_loose[-1][1]
        assert len(c_tight) == len(c_loose) == len(dfi) - 50
        # sl允许负数写法（取绝对值），与正数幅度等价
        _, c_neg = svc._backtest_linear(dfi.copy(), 100000, 1.0,
                                        {'tp': 0.05, 'sl': -0.03})
        assert c_neg[-1][1] == c_tight[-1][1]

    def test_ma_cross_custom_params_no_df_pollution(self):
        """非默认fast/slow在副本上现算均线，不得污染调用方DataFrame的MA20/MA50列"""
        svc = BacktestService()
        dfi = _wave_with_indicators()
        ma20_before = dfi['MA20'].copy()
        ma50_before = dfi['MA50'].copy()
        trades, curve = svc._backtest_ma_cross(dfi, 100000, 1.0, {'fast': 10, 'slow': 40})
        assert dfi['MA20'].equals(ma20_before)
        assert dfi['MA50'].equals(ma50_before)
        assert len(curve) == len(dfi) - 50  # 循环仍从第50根开始

    def test_macd_custom_params_recompute(self):
        """非默认fast/slow/signal内部现算EMA差与信号线，曲线长度不变且结果可复现"""
        svc = BacktestService()
        dfi = _wave_with_indicators()
        params = {'fast': 8, 'slow': 21, 'signal': 7}
        tr1, c1 = svc._backtest_macd(dfi.copy(), 100000, 1.0, dict(params))
        tr2, c2 = svc._backtest_macd(dfi.copy(), 100000, 1.0, dict(params))
        assert c1 == c2 and len(c1) == len(dfi) - 50
        _, c_default = svc._backtest_macd(dfi.copy(), 100000, 1.0)
        assert not (c1[-1][1] == c_default[-1][1] and tr1 == _)

    def test_start_override_for_walk_forward(self):
        """start=1时权益曲线覆盖除首根外的全部bar（walk-forward测试段用）"""
        svc = BacktestService()
        dfi = _wave_with_indicators()
        trades, curve = svc._backtest_linear(dfi, 100000, 1.0, start=1)
        assert len(curve) == len(dfi) - 1


# ============================================
# optimize 网格寻优
# ============================================
class TestOptimize:
    def _svc_with_mock(self, monkeypatch, days: int = 300) -> BacktestService:
        svc = BacktestService()
        df = make_wave_df(days)
        monkeypatch.setattr(svc.stock_service, 'get_stock_data',
                            lambda *a, **k: df.copy())
        return svc

    def test_structure_and_grid_sizes(self, monkeypatch):
        """各策略组合数符合网格定义；字段齐全；按sharpe降序；含in_sample_warning (P0-8)"""
        expected_n = {'linear': 9, 'nonlinear': 9, 'macd': 8}
        for strat, n in expected_n.items():
            svc = self._svc_with_mock(monkeypatch)
            r = svc.optimize('FAKE', strat, market='US')
            assert 'error' not in r, r.get('error')
            assert len(r['results']) == n
            assert r.get('in_sample_warning') == '样本内寻优，请用 Walk-Forward 验证'
            assert r['best'][r['metric']] == r['best']['sharpe_ratio']
            required = {'params', 'total_return', 'annual_return', 'max_drawdown',
                        'sharpe_ratio', 'win_rate', 'trade_count', 'final_value'}
            for row in r['results']:
                assert required.issubset(row.keys())
            sharpes = [row['sharpe_ratio'] for row in r['results']]
            assert sharpes == sorted(sharpes, reverse=True)

    def test_best_is_metric_max(self, monkeypatch):
        """best必须是results中metric最大者"""
        svc = self._svc_with_mock(monkeypatch)
        r = svc.optimize('FAKE', 'linear', market='US')
        max_sharpe = max(row['sharpe_ratio'] for row in r['results'])
        assert r['best']['sharpe_ratio'] == max_sharpe
        assert r['best']['params'] == \
            [row for row in r['results'] if row['sharpe_ratio'] == max_sharpe][0]['params']

    def test_heatmap_dims_and_mapping(self, monkeypatch):
        """heatmap z维度=x_len×y_len；z[y_idx][x_idx]与对应params的result行一致"""
        svc = self._svc_with_mock(monkeypatch)
        r = svc.optimize('FAKE', 'linear', market='US')
        hm = r['heatmap']
        assert hm['x_name'] == 'tp' and hm['y_name'] == 'sl'
        assert hm['x_values'] == sorted(PARAM_GRIDS['linear']['tp'])
        assert hm['y_values'] == sorted(PARAM_GRIDS['linear']['sl'])
        assert len(hm['z']) == len(hm['y_values'])           # 行数=y
        assert all(len(row) == len(hm['x_values']) for row in hm['z'])  # 列数=x
        target = [row for row in r['results']
                  if row['params']['tp'] == hm['x_values'][2]
                  and row['params']['sl'] == hm['y_values'][1]][0]
        assert hm['z'][1][2] == target['sharpe_ratio']

    def test_heatmap_macd_signal9_slice(self, monkeypatch):
        """macd三维网格：results保留全部8组合，热力图固定signal=9取fast×slow切片"""
        svc = self._svc_with_mock(monkeypatch)
        r = svc.optimize('FAKE', 'macd', market='US')
        assert len(r['results']) == 8
        hm = r['heatmap']
        assert hm['x_name'] == 'fast' and hm['y_name'] == 'slow'
        assert len(hm['z']) == 2 and len(hm['z'][0]) == 2
        slice_params = {(p['fast'], p['slow'])
                        for p in (row['params'] for row in r['results'])
                        if p['signal'] == 9}
        assert len(slice_params) == 4
        lookup = {(row['params']['fast'], row['params']['slow']): row['sharpe_ratio']
                  for row in r['results'] if row['params']['signal'] == 9}
        for yi, sv in enumerate(hm['y_values']):
            for xi, fv in enumerate(hm['x_values']):
                assert hm['z'][yi][xi] == lookup[(fv, sv)]

    def test_ma_cross_grid_filters_invalid(self, monkeypatch):
        """所有返回组合均满足 fast < slow"""
        svc = self._svc_with_mock(monkeypatch)
        r = svc.optimize('FAKE', 'ma_cross', market='US')
        assert all(row['params']['fast'] < row['params']['slow']
                   for row in r['results'])

    def test_metric_and_grid_overrides(self, monkeypatch):
        """metric切换排序依据；param_grid可覆盖部分键；best[metric]对齐 (P0-4)"""
        svc = self._svc_with_mock(monkeypatch)
        r = svc.optimize('FAKE', 'linear', market='US',
                         metric='total_return', param_grid={'tp': [0.12]})
        assert r['metric'] == 'total_return'
        tps = {row['params']['tp'] for row in r['results']}
        assert tps == {0.12}
        rets = [row['total_return'] for row in r['results']]
        assert rets == sorted(rets, reverse=True)
        max_ret = max(rets)
        assert r['best']['total_return'] == max_ret
        assert r['best'][r['metric']] == max_ret

    def test_max_drawdown_metric_sorts_ascending(self, monkeypatch):
        """按max_drawdown寻优时best必须是回撤最小者"""
        svc = self._svc_with_mock(monkeypatch)
        r = svc.optimize('FAKE', 'linear', market='US', metric='max_drawdown')
        dds = [row['max_drawdown'] for row in r['results']]
        assert dds == sorted(dds)
        min_dd = min(dds)
        assert r['best']['max_drawdown'] == min_dd
        assert r['best']['params'] == \
            [row for row in r['results'] if row['max_drawdown'] == min_dd][0]['params']

    def test_unknown_strategy_or_metric(self, monkeypatch):
        svc = self._svc_with_mock(monkeypatch)
        assert 'error' in svc.optimize('FAKE', 'bogus')
        assert 'error' in svc.optimize('FAKE', 'linear', metric='bogus')

    def test_insufficient_data(self, monkeypatch):
        svc = BacktestService()
        df = make_wave_df(40)  # 少于50根K线
        monkeypatch.setattr(svc.stock_service, 'get_stock_data',
                            lambda *a, **k: df.copy())
        assert 'error' in svc.optimize('FAKE', 'linear')


# ============================================
# walk_forward 滚动验证
# ============================================
class TestWalkForward:
    def _svc_with_mock(self, monkeypatch, days: int = 630) -> BacktestService:
        svc = BacktestService()
        df = make_wave_df(days)
        monkeypatch.setattr(svc.stock_service, 'get_stock_data',
                            lambda *a, **k: df.copy())
        return svc

    def test_two_segment_structure(self, monkeypatch):
        """2段验证：分段数/步号/区间顺序正确，summary数学一致"""
        svc = self._svc_with_mock(monkeypatch)
        wf = svc.walk_forward('FAKE', 'linear', market='US', segments=2)
        assert 'error' not in wf
        assert len(wf['segments']) == 2
        assert [s['step'] for s in wf['segments']] == [1, 2]
        for s in wf['segments']:
            assert s['train_range'][1] <= s['test_range'][0]
            assert set(s.keys()) >= {
                'step', 'train_range', 'test_range', 'best_params', 'is_sharpe',
                'oos_return', 'oos_sharpe', 'oos_max_drawdown',
                'oos_buy_hold_return', 'beats_buy_hold'}
        summary = wf['summary']
        assert summary['total_segments'] == 2
        assert summary['win_segments'] == \
            sum(1 for s in wf['segments'] if s['beats_buy_hold'])
        assert abs(summary['avg_oos_return']
                   - round(float(np.mean([s['oos_return'] for s in wf['segments']])), 2)) <= 0.01

    def test_stitched_curve_length_and_continuity(self, monkeypatch):
        """OOS拼接曲线长度=各测试段equity长度之和；段边界处新段首值==前段末值"""
        days = 630
        svc = self._svc_with_mock(monkeypatch, days)
        wf = svc.walk_forward('FAKE', 'linear', market='US', segments=2)
        block = days // 3
        seg_lens = [block - 1, days - 2 * block - 1]
        stitched = wf['stitched_oos_curve']
        assert len(stitched) == sum(seg_lens)

        boundary = seg_lens[0]
        assert stitched[boundary]['value'] == stitched[boundary - 1]['value']

        dates = [p['date'] for p in stitched]
        assert all(dates[i] < dates[i + 1] for i in range(len(dates) - 1))

    def test_beats_flag_consistency(self, monkeypatch):
        """beats_buy_hold 必须与报告的 oos_return/oos_buy_hold_return 自洽"""
        svc = self._svc_with_mock(monkeypatch)
        wf = svc.walk_forward('FAKE', 'nonlinear', market='US', segments=3)
        for s in wf['segments']:
            assert s['beats_buy_hold'] == (s['oos_return'] > s['oos_buy_hold_return'])

    def test_train_ratio_one_is_anchored_blocks(self, monkeypatch):
        """train_ratio=1 时训练窗起点回到块0（完整锚定窗口 块[0..i-1]）"""
        svc = self._svc_with_mock(monkeypatch)
        wf = svc.walk_forward('FAKE', 'ma_cross', market='US',
                              segments=3, train_ratio=1.0)
        first_starts = {s['step']: s['train_range'][0] for s in wf['segments']}
        assert len(set(first_starts.values())) == 1

    def test_validation_errors(self, monkeypatch):
        """segments越界/未知策略/数据过短均报错"""
        svc = self._svc_with_mock(monkeypatch)
        assert 'error' in svc.walk_forward('FAKE', 'linear', segments=0)
        assert 'error' in svc.walk_forward('FAKE', 'linear', segments=5)
        assert 'error' in svc.walk_forward('FAKE', 'bogus')
        short = BacktestService()
        df = make_wave_df(150)  # 60*(2+1)=180不够
        monkeypatch.setattr(short.stock_service, 'get_stock_data',
                            lambda *a, **k: df.copy())
        assert 'error' in short.walk_forward('FAKE', 'linear', segments=2)

    def test_walk_forward_param_stability_statistics(self, monkeypatch):
        """[第二段-7 验证] WF返回参数稳定性统计（众数占比、唯一组合数、稳定度评级）"""
        svc = self._svc_with_mock(monkeypatch, days=700)
        wf = svc.walk_forward('FAKE', 'linear', market='US', segments=3)
        assert 'param_stability' in wf
        ps = wf['param_stability']
        assert set(ps.keys()) >= {
            'mode_params', 'mode_count', 'mode_ratio', 'mode_ratio_pct',
            'unique_combos', 'total_segments', 'stability_rating', 'per_param'
        }
        assert ps['total_segments'] == 3
        assert 1 <= ps['unique_combos'] <= 3
        assert 0.0 < ps['mode_ratio'] <= 1.0
        assert ps['stability_rating'] in ('高', '中', '低')
        assert wf['summary']['mode_ratio'] == ps['mode_ratio']
        assert wf['summary']['unique_combos'] == ps['unique_combos']
        assert wf['summary']['stability_rating'] == ps['stability_rating']

        # 纯函数边界验证：3段完全一致 -> 众数占比 1.0、唯一组合数 1、评级 '高'
        high_ps = BacktestService._compute_param_stability([
            {'best_params': {'tp': 0.15, 'sl': 0.08}},
            {'best_params': {'tp': 0.15, 'sl': 0.08}},
            {'best_params': {'tp': 0.15, 'sl': 0.08}},
        ])
        assert high_ps['mode_ratio'] == 1.0
        assert high_ps['unique_combos'] == 1
        assert high_ps['stability_rating'] == '高'

        # 3段互不相同 -> 众数占比 0.3333、唯一组合数 3、评级 '低'
        low_ps = BacktestService._compute_param_stability([
            {'best_params': {'tp': 0.10, 'sl': 0.06}},
            {'best_params': {'tp': 0.15, 'sl': 0.08}},
            {'best_params': {'tp': 0.25, 'sl': 0.12}},
        ])
        assert abs(low_ps['mode_ratio'] - 0.3333) < 0.001
        assert low_ps['unique_combos'] == 3
        assert low_ps['stability_rating'] == '低'

    def test_cn_benchmark_csi300_in_backtest_and_macro(self, monkeypatch):
        """[第二段-6 验证] A股基准统一为沪深300（000300.SS），覆盖回测与宏观分析"""
        from app.services.analysis_service import AnalysisService
        from app.services.stock_service import _resolve_cn_symbol

        # 1. 代码解析：000300.SS 必须解析为上交所指数 sh000300 / 1.000300，绝不能误判为深市 sz000300
        code, prefix, secid = _resolve_cn_symbol('000300.SS')
        assert (code, prefix, secid) == ('000300', 'sh', '1.000300')

        # 2. 回测层：A股回测与多策略对比请求沪深300（000300.SS）
        svc = BacktestService()
        queried_codes = []
        df = make_wave_df(120)

        def fake_get_data(code, market='A', period='1y', use_cache=True):
            queried_codes.append((code, market))
            return df.copy()

        monkeypatch.setattr(svc.stock_service, 'get_stock_data', fake_get_data)
        bt = svc.run_backtest('600519', 'linear', market='A', period='1y')
        assert bt['benchmark_code'] == '000300.SS'
        assert bt['benchmark_return'] is not None
        assert ('000300.SS', 'A') in queried_codes
        assert all(c != '000001' and c != '000001.SZ' for c, _ in queried_codes)

        # 3. 宏观层：AnalysisService._analyze_macro_cn 请求 000300.SS 而非 000001
        an_svc = AnalysisService()
        macro_queried = []
        monkeypatch.setattr(
            an_svc.stock_service, 'get_stock_data',
            lambda code, market='A', period='3mo', use_cache=True: (macro_queried.append(code) or df.copy())
        )
        m_score, m_details = an_svc._analyze_macro_cn()
        assert macro_queried == ['000300.SS']
        assert m_details['indicators']['benchmark'] == '000300.SS'
        assert any('000300.SS' in s for s in m_details['interpretations'])


# ============================================
# Schema 默认值
# ============================================
class TestSchemas:
    def test_optimize_request_defaults(self):
        from app.schemas import OptimizeRequest
        req = OptimizeRequest(stock_code='MU', strategy='linear')
        assert req.period == '1y'
        assert req.initial_capital == 100000
        assert req.commission_per_trade == 1.0
        assert req.metric == 'sharpe'
        assert req.param_grid is None

    def test_walkforward_request_defaults(self):
        from app.schemas import WalkForwardRequest
        req = WalkForwardRequest(stock_code='600519', strategy='macd')
        assert req.period == '5y'
        assert req.initial_capital == 100000
        assert req.commission_per_trade == 1.0
        assert req.train_ratio == 0.6
        assert req.segments == 2



    def test_unified_backtest_calibration_and_v1_divergence(self):
        """
        [第三批·第一段-2 真实非空洞（Non-Vacuous）验证]
        1. 构造专门触发新老口径差异的行情序列，直接对比归档旧版
           `archive/verify_nonlinear_winrate_v1_archived.py::simulate_strategy_walk_forward`
           与新版 `BacktestService.evaluate_df_profile`，断言两者结果不同且差异方向严格符合预期：
           - 差异A（30天超时强平 TIME_EXIT）：买入后横盘微涨 +2% 持续 40 天（未触及 +15% 止盈或 -8% 止损），
             旧版在第 30 天触发 TIME_EXIT 记为 1 笔胜场（100% 胜率），新版严口径不触发强平（0 笔已平仓交易）。
           - 差异B（非线性 RSI>=30 回踩 MA20 误触发）：RSI 处于 45~55 中性区（>=30）、小幅回踩 MA20 但未触及布林下轨，随后大涨 +50%，
             旧版因 `nonlinear_buy_point` 在 RSI>=30 时退化为 MA20 而买入记胜，新版严口径拒绝在非超卖区（RSI>=30 且未破下轨）入场（0 笔交易）。
           - 差异C（95% 仓位 + $1 手续费 vs 100% 满仓 0 佣金）：对同一笔标准 +15% 止盈交易，
             旧版计 100% 满仓 0 佣金收益率 = +15.00%，新版因 95% 仓位上限与买卖双边佣金，总收益率严格低于旧版（约 +14.23%）。
        2. 验证改造后的根脚本 `verify_nonlinear_winrate.simulate_strategy_walk_forward` 已收敛至 `BacktestService` 严口径。
        """
        import importlib.util
        import verify_nonlinear_winrate as vnw_current

        repo_root = Path(__file__).resolve().parents[3]
        arch_path = repo_root / "archive" / "verify_nonlinear_winrate_v1_archived.py"
        spec = importlib.util.spec_from_file_location("vnw_archived", str(arch_path))
        vnw_archived = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vnw_archived)

        svc = BacktestService()
        idx = pd.date_range("2025-01-01", periods=100, freq="B")

        # --- 场景 A：触发线性买点后横盘微涨 +2.1% 超过 30 个交易日（测试 TIME_EXIT 差异）---
        # 前 45 天 96.0，第 45~54 天升至 104.0（使 bar 54 的 prev_ma20=100.0 < prev_close=104.0）
        closes_a = [96.0] * 45 + [104.0] * 10 + [97.0] * 45
        opens_a = list(closes_a)
        highs_a = [c + 0.5 for c in closes_a]
        lows_a = [c - 0.5 for c in closes_a]
        # 第 55 天跳空低开至 95.0（同时低于 BacktestService 买点 102.0 与旧版买点 98.8，两边均以开盘价 95.0 成交）
        opens_a[55] = 95.0
        lows_a[55] = 95.0
        closes_a[55] = 97.0
        highs_a[55] = 97.5
        df_a = pd.DataFrame({"Open": opens_a, "High": highs_a, "Low": lows_a, "Close": closes_a, "Volume": 1000000}, index=idx)

        old_a = vnw_archived.simulate_strategy_walk_forward(df_a, strategy_type="linear")
        new_a = svc.evaluate_df_profile(df_a, strategy="linear", initial_capital=100000.0, commission_per_trade=1.0)
        assert old_a["total_trades"] == 1 and old_a["trades"][0]["reason"] == "TIME_EXIT"
        assert old_a["win_rate"] == 100.0
        assert new_a["total_trades"] == 0, "BacktestService 严口径无 30 天 TIME_EXIT，未触及止盈止损不得强平计胜"

        # --- 场景 B：RSI 处于中性区（~50 >= 30），价格回踩 MA20 但高于 BB_Lower，随后暴涨 +55%（测试非线性买点差异）---
        closes_b, opens_b, highs_b, lows_b = [], [], [], []
        for k in range(100):
            base = 100.0 + (2.0 if k % 2 == 0 else -2.0)
            closes_b.append(base)
            opens_b.append(base)
            highs_b.append(base + 0.5)
            lows_b.append(base - 0.5)
        lows_b[55] = 99.0
        opens_b[55] = 100.0
        closes_b[55] = 100.0
        for k in range(56, 100):
            opens_b[k] = 155.0
            closes_b[k] = 155.0
            highs_b[k] = 156.0
            lows_b[k] = 154.0
        df_b = pd.DataFrame({"Open": opens_b, "High": highs_b, "Low": lows_b, "Close": closes_b, "Volume": 1000000}, index=idx)

        old_b = vnw_archived.simulate_strategy_walk_forward(df_b, strategy_type="nonlinear")
        new_b = svc.evaluate_df_profile(df_b, strategy="nonlinear", initial_capital=100000.0, commission_per_trade=1.0)
        assert old_b["total_trades"] >= 1, "旧版在 RSI>=30 时将买点退化为 MA20，会在回踩 MA20 时误入场"
        assert new_b["total_trades"] == 0, "新版 BacktestService 严口径要求 RSI<30 或触及 BB_Lower，拒绝普通 MA20 回踩"

        # --- 场景 C：标准单笔 +15% 线性止盈（测试 100% 满仓 0 佣金 vs 95% 仓位 + $1 手续费收益差）---
        closes_c = [96.0] * 45 + [104.0] * 10 + [96.0] * 45
        opens_c = list(closes_c)
        highs_c = [c + 0.5 for c in closes_c]
        lows_c = [c - 0.5 for c in closes_c]
        # 第 55 天跳空低开至 95.0（两边均按 95.0 买入）
        opens_c[55] = 95.0
        lows_c[55] = 95.0
        closes_c[55] = 96.0
        highs_c[55] = 96.5
        # 第 56 天冲高至 112.0（突破 95.0 * 1.15 = 109.25 挂单止盈价，两边均按 109.25 止盈离场），随后稳定在 96.0
        opens_c[56] = 96.0
        highs_c[56] = 112.0
        lows_c[56] = 95.8
        closes_c[56] = 96.0
        df_c = pd.DataFrame({"Open": opens_c, "High": highs_c, "Low": lows_c, "Close": closes_c, "Volume": 1000000}, index=idx)

        old_c = vnw_archived.simulate_strategy_walk_forward(df_c, strategy_type="linear")
        new_c = svc.evaluate_df_profile(df_c, strategy="linear", initial_capital=100000.0, commission_per_trade=1.0)
        assert old_c["total_trades"] == 1 and new_c["total_trades"] == 1
        assert abs(old_c["cum_return"] - 15.0) < 0.05, f"旧版 100% 满仓 0 佣金收益应为 15.0%，实际 {old_c['cum_return']}"
        assert new_c["total_return"] < old_c["cum_return"], "新版含 95% 仓位上限与手续费，总收益必须低于旧版满仓零佣金"
        assert 14.2 <= new_c["total_return"] <= 14.3, f"新版 95% 仓位扣 $2 佣金后收益约为 14.25%，实际 {new_c['total_return']}"

        # 同时验证现行 verify_nonlinear_winrate 已与 BacktestService 收敛一致
        curr_c = vnw_current.simulate_strategy_walk_forward(df_c, strategy_type="linear", initial_capital=100000.0, commission_per_trade=1.0)
        assert curr_c["cum_return"] == new_c["total_return"]
        assert curr_c["total_trades"] == new_c["total_trades"]


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
