"""
风险管理模块
包含仓位管理、止损策略等
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple


class PositionManager:
    """仓位管理器"""

    def __init__(
        self,
        max_position_size: float = 0.25,
        max_total_exposure: float = 1.0,
        max_correlated: float = 0.4
    ):
        """
        参数:
            max_position_size: 单只股票最大仓位
            max_total_exposure: 最大总仓位（1=100%）
            max_correlated: 相关性高的股票最大仓位
        """
        self.max_position_size = max_position_size
        self.max_total_exposure = max_total_exposure
        self.max_correlated = max_correlated

    def calculate_position_size(
        self,
        capital: float,
        price: float,
        stop_loss_price: float,
        risk_per_trade: float = 0.02
    ) -> int:
        """
        基于风险计算仓位大小

        参数:
            capital: 总资金
            price: 入场价格
            stop_loss_price: 止损价格
            risk_per_trade: 单笔交易风险比例

        返回:
            建议股数
        """
        # 计算风险金额
        risk_amount = capital * risk_per_trade

        # 计算每股风险
        risk_per_share = abs(price - stop_loss_price)
        if risk_per_share == 0:
            return 0

        # 计算股数
        shares = int(risk_amount / risk_per_share)

        # 检查最大仓位限制
        max_shares_by_position = int(capital * self.max_position_size / price)
        shares = min(shares, max_shares_by_position)

        return max(1, shares)

    def calculate_position_size_kelly(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        capital: float,
        price: float,
        kelly_fraction: float = 0.5
    ) -> int:
        """
        凯利公式计算仓位

        参数:
            win_rate: 胜率
            avg_win: 平均盈利
            avg_loss: 平均亏损（正数）
            capital: 总资金
            price: 当前价格
            kelly_fraction: 凯利分数（通常用半凯利）
        """
        if avg_loss == 0:
            return 0

        # 凯利公式
        win_loss_ratio = avg_win / avg_loss
        kelly = (win_rate * win_loss_ratio - (1 - win_rate)) / win_loss_ratio

        # 使用分数凯利
        kelly = max(0, kelly * kelly_fraction)

        # 计算股数
        position_value = capital * kelly
        shares = int(position_value / price)

        return max(1, shares)

    def check_portfolio_constraints(
        self,
        current_positions: Dict[str, float],
        new_position: Dict[str, float],
        correlations: pd.DataFrame = None
    ) -> bool:
        """
        检查是否满足组合约束

        参数:
            current_positions: 当前仓位 {symbol: weight}
            new_position: 新仓位 {symbol: weight}
            correlations: 相关性矩阵
        """
        # 检查总仓位
        total_exposure = sum(current_positions.values()) + sum(new_position.values())
        if total_exposure > self.max_total_exposure:
            return False

        # 检查单只股票仓位
        for symbol, weight in new_position.items():
            current_weight = current_positions.get(symbol, 0)
            if current_weight + weight > self.max_position_size:
                return False

        return True


class StopLossManager:
    """止损管理器"""

    def __init__(
        self,
        initial_stop_pct: float = 0.05,
        trailing_stop_pct: float = 0.10,
        time_stop_days: int = 20
    ):
        """
        参数:
            initial_stop_pct: 初始止损百分比
            trailing_stop_pct: 移动止损百分比
            time_stop_days: 时间止损天数
        """
        self.initial_stop_pct = initial_stop_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.time_stop_days = time_stop_days

    def calculate_initial_stop(self, entry_price: float) -> float:
        """计算初始止损价"""
        return entry_price * (1 - self.initial_stop_pct)

    def calculate_trailing_stop(
        self,
        entry_price: float,
        highest_price: float
    ) -> float:
        """计算移动止损价"""
        return highest_price * (1 - self.trailing_stop_pct)

    def calculate_atr_stop(
        self,
        entry_price: float,
        atr: float,
        multiplier: float = 2.0
    ) -> float:
        """基于ATR计算止损"""
        return entry_price - (atr * multiplier)

    def should_stop_loss(
        self,
        entry_price: float,
        current_price: float,
        highest_price: float,
        entry_date: pd.Timestamp,
        current_date: pd.Timestamp,
        atr: float = None
    ) -> Tuple[bool, str]:
        """
        检查是否触发止损

        返回:
            (是否止损, 止损原因)
        """
        # 1. 初始止损
        initial_stop = self.calculate_initial_stop(entry_price)
        if current_price <= initial_stop:
            return True, "initial_stop"

        # 2. 移动止损
        trailing_stop = self.calculate_trailing_stop(entry_price, highest_price)
        if current_price <= trailing_stop:
            return True, "trailing_stop"

        # 3. ATR止损（如果提供了ATR）
        if atr is not None:
            atr_stop = self.calculate_atr_stop(entry_price, atr)
            if current_price <= atr_stop:
                return True, "atr_stop"

        # 4. 时间止损
        days_held = (current_date - entry_date).days
        if days_held > self.time_stop_days and current_price < entry_price:
            return True, "time_stop"

        return False, ""


class RiskManager:
    """风险管理器（综合）"""

    def __init__(
        self,
        max_portfolio_risk: float = 0.10,
        max_position_risk: float = 0.02,
        max_daily_loss: float = 0.03,
        max_drawdown: float = 0.20
    ):
        """
        参数:
            max_portfolio_risk: 组合最大风险
            max_position_risk: 单笔最大风险
            max_daily_loss: 单日最大亏损
            max_drawdown: 最大回撤限制
        """
        self.max_portfolio_risk = max_portfolio_risk
        self.max_position_risk = max_position_risk
        self.max_daily_loss = max_daily_loss
        self.max_drawdown = max_drawdown

        self.position_manager = PositionManager()
        self.stop_loss_manager = StopLossManager()

        # 跟踪状态
        self.daily_pnl = 0
        self.peak_value = 0
        self.current_value = 0

    def update_state(self, portfolio_value: float, date: pd.Timestamp):
        """更新状态"""
        self.current_value = portfolio_value
        if portfolio_value > self.peak_value:
            self.peak_value = portfolio_value

    def can_trade(self) -> Tuple[bool, str]:
        """
        检查是否可以交易

        返回:
            (可以交易, 原因)
        """
        # 检查日亏损限制
        if abs(self.daily_pnl) > self.max_daily_loss * self.current_value:
            return False, "daily_loss_limit"

        # 检查最大回撤
        if self.peak_value > 0:
            current_drawdown = (self.current_value - self.peak_value) / self.peak_value
            if abs(current_drawdown) > self.max_drawdown:
                return False, "max_drawdown"

        return True, ""

    def calculate_position_size(
        self,
        price: float,
        stop_price: float,
        capital: float
    ) -> int:
        """计算仓位大小"""
        return self.position_manager.calculate_position_size(
            capital, price, stop_price, self.max_position_risk
        )

    def should_stop_loss(
        self,
        entry_price: float,
        current_price: float,
        highest_price: float,
        entry_date: pd.Timestamp,
        current_date: pd.Timestamp,
        atr: float = None
    ) -> Tuple[bool, str]:
        """检查是否触发止损"""
        return self.stop_loss_manager.should_stop_loss(
            entry_price, current_price, highest_price,
            entry_date, current_date, atr
        )

    def generate_risk_report(self) -> Dict:
        """生成风险报告"""
        current_drawdown = 0
        if self.peak_value > 0:
            current_drawdown = (self.current_value - self.peak_value) / self.peak_value

        return {
            'current_value': self.current_value,
            'peak_value': self.peak_value,
            'current_drawdown': current_drawdown,
            'daily_pnl': self.daily_pnl,
            'max_drawdown_allowed': self.max_drawdown,
            'max_daily_loss_allowed': self.max_daily_loss
        }


class TrailingStopTracker:
    """移动止损跟踪器"""

    def __init__(self):
        self.positions = {}  # {symbol: {entry_price, highest, stop_price}}

    def update(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        trailing_pct: float = 0.10
    ):
        """更新跟踪"""
        if symbol not in self.positions:
            self.positions[symbol] = {
                'entry_price': entry_price,
                'highest': current_price,
                'stop_price': current_price * (1 - trailing_pct)
            }
        else:
            pos = self.positions[symbol]
            if current_price > pos['highest']:
                pos['highest'] = current_price
                pos['stop_price'] = current_price * (1 - trailing_pct)

    def check_stop(self, symbol: str, current_price: float) -> bool:
        """检查是否触发止损"""
        if symbol in self.positions:
            return current_price <= self.positions[symbol]['stop_price']
        return False

    def remove(self, symbol: str):
        """移除仓位"""
        if symbol in self.positions:
            del self.positions[symbol]


if __name__ == "__main__":
    # 测试仓位管理
    pm = PositionManager()

    # 示例：100万资金，股票价格100元，止损价95元
    shares = pm.calculate_position_size(
        capital=1000000,
        price=100,
        stop_loss_price=95,
        risk_per_trade=0.02
    )
    print(f"建议仓位: {shares} 股")
    print(f"仓位价值: {shares * 100:,.0f} 元")
    print(f"仓位比例: {shares * 100 / 1000000:.2%}")

    # 测试止损
    slm = StopLossManager()
    entry_price = 100
    current_price = 92
    highest_price = 110
    entry_date = pd.Timestamp('2024-01-01')
    current_date = pd.Timestamp('2024-01-15')

    should_stop, reason = slm.should_stop_loss(
        entry_price, current_price, highest_price,
        entry_date, current_date
    )
    print(f"\n止损检查: {should_stop}, 原因: {reason}")
