"""utils/price_levels.py 纯函数单测（点位公式唯一定义处的回归锚点 + ATR动态点位与移动止盈）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.utils.price_levels import (
    atr_adaptive_levels,
    compute_trailing_stop,
    discipline_stop,
    levels_with_targets,
    linear_buy_point,
    macd_state_days,
    nonlinear_buy_point,
)


class TestLinearBuyPoint:
    def test_above_ma20_takes_fibonacci_retracement(self):
        # 现价110、MA20=100 → 回撤50%位=105，但被-5%上限(104.5)截断
        assert linear_buy_point(110.0, 100.0) == 104.5

    def test_below_ma20_falls_back_5pct(self):
        assert linear_buy_point(100.0, 110.0) == 95.0

    def test_capped_at_5pct_below_current(self):
        # MA20仅略低于现价时，回撤位会高于-5%上限，应被压到上限
        assert linear_buy_point(100.0, 99.8) == 95.0


class TestNonlinearBuyPoint:
    def test_oversold_uses_bb_lower(self):
        assert nonlinear_buy_point(25.0, 100.0, 90.0) == 90.0

    def test_normal_uses_ma20(self):
        assert nonlinear_buy_point(50.0, 100.0, 90.0) == 100.0

    def test_threshold_is_exclusive_at_30(self):
        assert nonlinear_buy_point(30.0, 100.0, 90.0) == 100.0


class TestLevelsWithTargets:
    def test_targets_and_distance(self):
        lv = levels_with_targets(100.0, 95.0, 1.15)
        assert lv['buy'] == 95.0
        assert lv['stop'] == 87.4   # 95*0.92
        assert lv['profit'] == 109.25  # 95*1.15
        assert lv['distance'] == 5.0

    def test_zero_current_no_crash(self):
        assert levels_with_targets(0.0, 95.0, 1.15)['distance'] == 0.0


class TestAtrAdaptiveLevels:
    def test_atr_stop_within_corridor(self):
        # 买入价100，ATR=4.5 -> 2*ATR=9 -> atr_stop=91.0，在[86, 95]安全走廊内
        lv = atr_adaptive_levels(105.0, 100.0, 1.15, atr=4.5)
        assert lv['buy'] == 100.0
        assert lv['stop'] == 92.0
        assert lv['atr_stop'] == 91.0
        assert lv['breakeven_trigger'] == 110.0
        assert lv['breakeven_stop'] == 101.0

    def test_atr_stop_clamped_on_extreme_volatility(self):
        # 高波动标的 ATR=12 -> 2*ATR=24 -> 止损被夹在最多 -14% (86.0)
        lv_high = atr_adaptive_levels(105.0, 100.0, 1.15, atr=12.0)
        assert lv_high['atr_stop'] == 86.0
        # 极低波动标的 ATR=1 -> 2*ATR=2 -> 止损被夹在至少 -5% (95.0)
        lv_low = atr_adaptive_levels(105.0, 100.0, 1.15, atr=1.0)
        assert lv_low['atr_stop'] == 95.0

    def test_fallback_when_atr_none(self):
        lv = atr_adaptive_levels(105.0, 100.0, 1.15, atr=None)
        assert lv['atr_stop'] == lv['stop']


class TestComputeTrailingStop:
    def test_before_breakeven_keeps_initial_stop(self):
        # 成本100，初始止损92，最高到108（未达+10%即110）-> 保持92
        assert compute_trailing_stop(100.0, 108.0, 92.0) == 92.0

    def test_at_breakeven_trigger_locks_cost_plus_one_pct(self):
        # 最高达到110 (+10%) -> 110*0.92=101.2 > 101.0 -> 上移至101.2
        assert compute_trailing_stop(100.0, 110.0, 92.0) == 101.2

    def test_trails_higher_peaks(self):
        # 最高涨到130 -> 130*0.92=119.6
        assert compute_trailing_stop(100.0, 130.0, 92.0) == 119.6


class TestMacdStateDays:
    def test_counts_consecutive_state_from_latest(self):
        # 最新为金叉，向前连续4根金叉后遇死叉
        assert macd_state_days([False, True, True, True, True]) == (True, 4)

    def test_death_cross_streak(self):
        assert macd_state_days([True, True, False, False]) == (False, 2)

    def test_empty_series(self):
        assert macd_state_days([]) == (None, 0)


def test_discipline_stop():
    assert discipline_stop(100.0) == 92.0
