# -*- coding: utf-8 -*-
"""
回测增强功能测试：每笔手续费$1 / 买入持有基准 / 4策略一键对比 / 自定义寻优参数透传
全部使用合成数据直接调用服务方法，不依赖网络。
运行：cd backend && python -m pytest tests/test_backtest_enhance.py -v
"""
import sys
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.backtest_service import BacktestService


# ============================================
# 合成数据构造（参考 test_services.make_fake_df 的做法）
# ============================================
def make_flat_df(n: int = 90, price: float = 100.0) -> pd.DataFrame:
    """构造横盘OHLCV序列，指标列由各用例按需注入以精确控制信号"""
    idx = pd.date_range('2026-01-01', periods=n, freq='B')
    close = np.full(n, price)
    return pd.DataFrame({
        'Open': close.copy(),
        'High': close + 0.5,
        'Low': close - 0.5,
        'Close': close,
        'Volume': 1_000_000,
    }, index=idx)


def make_linear_roundtrip_df() -> pd.DataFrame:
    """
    线性策略专用：恰好触发1买1卖。
    注入 MA20 = Close-2（全程多头排列）：
    - 横盘/缓涨段：买入位=前收-1，当日Low=收-0.5 始终高于买入位 → 不买
    - 第85根K线深跌（Low=101.5 触及买入位104）→ 开盘价成交买入
    - 随后3根急拉至124 → 收益+19%≥15%止盈卖出
    - 最后1根继续上涨且Low高于新买入位 → 不再买入
    """
    df = make_flat_df(90)
    close = np.concatenate([
        np.full(70, 100.0),
        np.linspace(100.0, 105.0, 15),            # 第70~84根：缓涨
        np.array([104.0]),                         # 第85根：回踩
        np.array([112.0, 118.0, 124.0, 125.0]),    # 第86~89根：拉升止盈
    ])
    df['Close'] = close
    df['Open'] = close.copy()
    df['High'] = close + 0.5
    df['Low'] = close - 0.5
    df.iloc[85, df.columns.get_loc('Low')] = 101.5  # 深跌触及斐波那契买入位
    df['MA20'] = close - 2.0                        # 直接注入，绕过指标计算
    return df


def make_nonlinear_roundtrip_df() -> pd.DataFrame:
    """非线性策略专用：第61根RSI超卖买入，第62根跌10%触发-8%止损卖出，恰好1买1卖"""
    df = make_flat_df(70)
    rsi = np.full(70, 50.0)
    rsi[60] = 25.0                                  # 前一日RSI<30 → 次日开盘买入
    df['RSI'] = rsi                                 # 注入RSI；不注入BB_Lower则下轨条件自动跳过
    df.iloc[62, df.columns.get_loc('Close')] = 90.0  # 买入次日大跌10%，触发止损
    df.iloc[62, df.columns.get_loc('Low')] = 89.5    # 保持OHLC合法（盘中触及止损位，挂单语义按止损价成交）
    return df


def make_ma_cross_roundtrip_df() -> pd.DataFrame:
    """双均线策略专用：注入MA20/MA50，第60根金叉买入、第75根死叉卖出，恰好1买1卖"""
    df = make_flat_df(80)
    ma20 = np.full(80, 99.0)
    ma20[60:75] = 101.0
    ma20[75:] = 98.0
    df['MA20'] = ma20
    df['MA50'] = 100.0
    return df


def make_macd_roundtrip_df() -> pd.DataFrame:
    """MACD策略专用：注入MACD/MACD_Signal，第61根金叉买入、第80根死叉卖出，恰好1买1卖"""
    df = make_flat_df(85)
    macd = np.full(85, -1.0)
    macd[61:80] = 1.0
    df['MACD'] = macd
    df['MACD_Signal'] = 0.0
    return df


def make_compare_df(days: int = 260) -> pd.DataFrame:
    """对比测试用：波动上行的真实感行情，供 calculate_all_indicators 计算全量指标"""
    rng = np.random.default_rng(7)
    t = np.arange(days)
    close = np.round(100 + 20 * np.sin(t / 15) + 0.08 * t + rng.normal(0, 0.4, days), 2)
    open_ = np.round(close + rng.normal(0, 0.3, days), 2)
    high = np.round(np.maximum(open_, close) + 0.8, 2)
    low = np.round(np.minimum(open_, close) - 0.8, 2)
    idx = pd.date_range('2025-01-01', periods=days, freq='B')
    return pd.DataFrame({'Open': open_, 'High': high, 'Low': low,
                         'Close': close, 'Volume': 1_000_000}, index=idx)


ROUNDTRIP_FIXTURES = {
    'linear': make_linear_roundtrip_df,
    'nonlinear': make_nonlinear_roundtrip_df,
    'ma_cross': make_ma_cross_roundtrip_df,
    'macd': make_macd_roundtrip_df,
}


# ============================================
# 手续费数学：1买1卖 → 最终资产恰好少$2，total_fees==2.0
# ============================================
class TestCommissionMath:
    @pytest.mark.parametrize('strategy', ['linear', 'nonlinear', 'ma_cross', 'macd'])
    def test_one_round_trip_costs_exactly_two_dollars(self, strategy):
        """4个策略均须在买卖两端扣手续费：同数据下 有费-无费 终值差恰好$2"""
        svc = BacktestService()
        method = getattr(svc, f'_backtest_{strategy}')
        df = ROUNDTRIP_FIXTURES[strategy]()

        trades_fee, curve_fee = method(df.copy(), 100000, 1.0)
        trades_free, curve_free = method(df.copy(), 100000, 0.0)

        # 合成数据必须恰好触发一买一卖
        buys = [t for t in trades_fee if t['action'] == 'buy']
        sells = [t for t in trades_fee if t['action'] == 'sell']
        assert len(buys) == 1 and len(sells) == 1, \
            f"{strategy} 应恰好1买1卖，实际 {len(buys)}买{len(sells)}卖"

        # 成交记录带fee字段
        assert buys[0]['fee'] == 1.0 and sells[0]['fee'] == 1.0
        assert trades_free[0]['fee'] == 0.0

        # 终值差恰好$2（整数费用减法不受2位小数舍入影响）
        diff = curve_free[-1][1] - curve_fee[-1][1]
        assert abs(diff - 2.0) < 1e-6

    def test_total_fees_metric(self):
        """_calculate_metrics 返回 total_fees = 成交笔数 × 单笔手续费"""
        svc = BacktestService()
        df = make_linear_roundtrip_df()
        trades, curve = svc._backtest_linear(df, 100000, 1.0)
        metrics = svc._calculate_metrics(
            trades=trades, equity_curve=curve,
            total_return=(curve[-1][1] / 100000 - 1),
            initial_capital=100000, final_value=curve[-1][1],
            dates=(df.index[0], df.index[-1]),
            stock_code='FAKE', strategy='linear',
        )
        assert metrics['total_fees'] == 2.0  # 1买+1卖

    def test_run_backtest_passes_commission_through(self, monkeypatch):
        """run_backtest 自定义手续费应透传：单笔$2时总费用=笔数×2"""
        svc = BacktestService()
        df = make_compare_df()
        monkeypatch.setattr(svc.stock_service, 'get_stock_data',
                            lambda *a, **k: df.copy())
        result = svc.run_backtest('FAKE', 'linear', market='US',
                                  period='1y', commission_per_trade=2.0)
        assert 'error' not in result
        assert result['commission_per_trade'] == 2.0
        assert result['total_fees'] == round(len(result['trades']) * 2.0, 2)

    def test_run_backtest_accepts_custom_params(self, monkeypatch):
        """run_backtest 支持直接传入寻优最优参数 params={'tp': 0.25, 'sl': 0.06}"""
        svc = BacktestService()
        df = make_compare_df()
        monkeypatch.setattr(svc.stock_service, 'get_stock_data',
                            lambda *a, **k: df.copy())
        res_default = svc.run_backtest('FAKE', 'linear', market='US', period='1y')
        res_custom = svc.run_backtest('FAKE', 'linear', market='US', period='1y',
                                      params={'tp': 0.25, 'sl': 0.06})
        assert 'error' not in res_custom
        assert res_custom['final_value'] != res_default['final_value']


# ============================================
# 买入持有基准
# ============================================
class TestBuyHold:
    def test_buy_hold_end_value_formula(self):
        """末值 == initial_capital × (末收盘/首日开盘)，容差0.01%"""
        svc = BacktestService()
        df = make_compare_df(days=180)
        curve, ret = svc._calculate_buy_hold(df, 100000)

        first_open = df.iloc[50]['Open']  # 从第50根K线起算
        expected_last = 100000 * float(df['Close'].iloc[-1]) / first_open
        assert abs(curve[-1]['value'] - expected_last) / expected_last < 1e-4
        assert abs(ret - (expected_last / 100000 - 1) * 100) < 0.01

    def test_buy_hold_same_date_axis_as_strategy(self):
        """基准曲线与策略权益曲线日期轴完全一致（都从第50根K线开始）"""
        svc = BacktestService()
        df = make_linear_roundtrip_df()
        _, strategy_curve = svc._backtest_linear(df, 100000, 0.0)
        bh_curve, _ = svc._calculate_buy_hold(df, 100000)
        assert [p['date'] for p in bh_curve] == [d for d, _ in strategy_curve]

    def test_run_backtest_returns_buy_hold(self, monkeypatch):
        """run_backtest 结果应包含 buy_hold_curve / buy_hold_return 且轴长一致"""
        svc = BacktestService()
        df = make_compare_df()
        monkeypatch.setattr(svc.stock_service, 'get_stock_data',
                            lambda *a, **k: df.copy())
        result = svc.run_backtest('FAKE', 'linear', market='US', period='1y')
        assert 'error' not in result
        assert 'buy_hold_curve' in result and 'buy_hold_return' in result
        assert len(result['buy_hold_curve']) == len(result['equity_curve'])
        # 末值公式一致性
        first_open = df.iloc[50]['Open']
        expected_last = 100000 * float(df['Close'].iloc[-1]) / first_open
        assert abs(result['buy_hold_curve'][-1]['value'] - expected_last) \
            / expected_last < 1e-4


# ============================================
# 策略对比服务
# ============================================
class TestCompareService:
    def _svc_with_mock(self, monkeypatch) -> BacktestService:
        svc = BacktestService()
        df = make_compare_df()
        monkeypatch.setattr(svc.stock_service, 'get_stock_data',
                            lambda *a, **k: df.copy())
        return svc

    def test_compare_structure(self, monkeypatch):
        svc = self._svc_with_mock(monkeypatch)
        result = svc.run_compare('FAKE', market='US')

        assert 'error' not in result
        assert set(result['strategies'].keys()) == {'linear', 'nonlinear', 'ma_cross', 'macd'}
        assert len(result['comparison']) == 5
        assert [row['key'] for row in result['comparison']] == \
            ['linear', 'nonlinear', 'ma_cross', 'macd', 'buy_hold']

        # 每条对比记录字段齐全
        for row in result['comparison']:
            for field in ('name', 'key', 'total_return', 'annual_return', 'max_drawdown',
                          'sharpe_ratio', 'win_rate', 'trade_count', 'total_fees',
                          'excess_vs_buy_hold'):
                assert field in row, f"对比行缺少字段 {field}"

    def test_compare_excess_math(self, monkeypatch):
        """超额收益 = 策略总收益 - 买入持有总收益；买入持有自身超额为0"""
        svc = self._svc_with_mock(monkeypatch)
        result = svc.run_compare('FAKE', market='US')
        bh_return = result['buy_hold']['total_return']

        for row in result['comparison'][:-1]:
            strategy_return = result['strategies'][row['key']]['total_return']
            assert row['excess_vs_buy_hold'] == round(strategy_return - bh_return, 2)

        last = result['comparison'][-1]
        assert last['name'] == '买入持有'
        assert last['excess_vs_buy_hold'] == 0
        assert last['total_return'] == bh_return

    def test_compare_buy_hold_consistency(self, monkeypatch):
        """对比结果中的买入持有曲线应与单策略回测返回的基准曲线一致"""
        svc = self._svc_with_mock(monkeypatch)
        result = svc.run_compare('FAKE', market='US')
        single = svc.run_backtest('FAKE', 'linear', market='US')

        assert result['buy_hold']['equity_curve'] == single['buy_hold_curve']
        assert result['strategies']['linear']['total_fees'] == \
            single['total_fees']


# ============================================
# Schema 默认值
# ============================================
class TestSchemas:
    def test_compare_request_defaults(self):
        from app.schemas import BacktestCompareRequest, BacktestRequest, BacktestResponse
        req = BacktestCompareRequest(stock_code='MU')
        assert req.period == '1y'
        assert req.initial_capital == 100000
        assert req.commission_per_trade == 1.0

        bt = BacktestRequest(stock_code='MU', strategy='linear')
        assert bt.commission_per_trade == 1.0

        resp_fields = BacktestResponse.model_fields
        assert resp_fields['total_fees'].default == 0.0
        assert resp_fields['commission_per_trade'].default == 1.0
        assert resp_fields['buy_hold_curve'].default == []


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
