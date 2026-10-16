"""
仓位管理系统
根据评分和策略动态调整仓位（含分档阶梯补仓状态跟踪，杜绝同档位重复无限补仓）
"""

import json
from datetime import datetime


class PositionManager:
    """仓位管理器"""

    def __init__(self, total_capital=15000):
        self.total_capital = total_capital
        self.cash = total_capital
        self.positions = {}
        self.reserve_ratio = 0.2  # 保留20%现金

    def calculate_position_size(self, stock_score, strategy_type="both"):
        """
        根据评分计算仓位大小

        参数：
            stock_score: 综合评分（0-10分）
            strategy_type: "linear"（线性）/ "nonlinear"（非线性）/ "both"（两者都用）

        返回：
            建议仓位金额
        """
        if stock_score >= 8.0:
            base_ratio = 0.4  # 40%
        elif stock_score >= 6.5:
            base_ratio = 0.3  # 30%
        elif stock_score >= 5.0:
            base_ratio = 0.2  # 20%
        elif stock_score >= 3.5:
            base_ratio = 0.1  # 10%
        else:
            base_ratio = 0  # 不买入

        if strategy_type == "nonlinear":
            # 非线性策略盈亏比高，但仍受单票40%风控上限约束
            adjusted_ratio = min(0.40, base_ratio * 1.2)
        elif strategy_type == "linear":
            adjusted_ratio = base_ratio
        else:
            adjusted_ratio = base_ratio

        position_size = self.total_capital * adjusted_ratio
        available_cash = max(0.0, self.cash - (self.total_capital * self.reserve_ratio))
        position_size = min(position_size, available_cash)

        return position_size

    def calculate_stop_loss(self, buy_price, strategy_type="linear"):
        """计算止损价（统一 -8%）"""
        return buy_price * 0.92

    def calculate_take_profit(self, buy_price, strategy_type="linear"):
        """计算止盈价（非线性 +46% / 线性 +15%）"""
        if strategy_type == "nonlinear":
            return buy_price * 1.46
        return buy_price * 1.15

    def should_add_position(self, stock_code, current_price, buy_price=None, strategy_type="linear"):
        """
        判断是否应该补仓（按 5% / 10% / 15% 三档阶梯触发，每档仅补仓一次，最多补 3 次）

        参数：
            stock_code: 股票代码
            current_price: 当前价
            buy_price: 初始买入基准价（可选，默认取首次建仓价 initial_price）
            strategy_type: 策略类型

        返回：
            (是否补仓, 补仓金额)
        """
        if stock_code not in self.positions:
            return False, 0

        position = self.positions[stock_code]
        add_count = position.get('add_count', 0)
        if add_count >= 3:
            # 已满 3 次补仓上限，禁止继续摊平
            return False, 0

        base_price = buy_price if buy_price is not None else position.get('initial_price', position['avg_price'])
        if base_price <= 0:
            return False, 0

        drop_percent = (base_price - current_price) / base_price * 100

        # 根据已补仓次数确定下一档所需跌幅阈值：第1次需跌5%，第2次需跌10%，第3次需跌15%
        thresholds = [5.0, 10.0, 15.0]
        required_drop = thresholds[add_count]
        if drop_percent < required_drop:
            return False, 0

        # 计算跨越了几个尚未补仓的档位（例如直接暴跌12%且此前0次补仓，本次触发1档）
        add_ratio = 0.25 if strategy_type == "nonlinear" else 0.20
        original_position = position.get('initial_cost', self.total_capital * 0.3)
        add_amount = original_position * add_ratio

        available_cash = max(0.0, self.cash - (self.total_capital * self.reserve_ratio))
        add_amount = min(add_amount, available_cash)

        if add_amount > 0:
            return True, add_amount
        return False, 0

    def buy(self, stock_code, price, amount, strategy_type="linear"):
        """买入或加仓股票（自动维护首次建仓价与已补仓次数 add_count）"""
        if price <= 0:
            return False
        shares = int(amount / price)
        if shares <= 0:
            return False
        cost = shares * price

        if cost > self.cash:
            print(f"现金不足，需要${cost:.2f}，可用${self.cash:.2f}")
            return False

        self.cash -= cost

        if stock_code in self.positions:
            position = self.positions[stock_code]
            total_shares = position['shares'] + shares
            total_cost = position['cost'] + cost
            avg_price = total_cost / total_shares
            add_count = position.get('add_count', 0) + 1

            self.positions[stock_code] = {
                'shares': total_shares,
                'avg_price': avg_price,
                'cost': total_cost,
                'initial_price': position.get('initial_price', price),
                'initial_cost': position.get('initial_cost', cost),
                'add_count': add_count,
                'strategy': strategy_type
            }
            print(f"加仓 {stock_code} (第{add_count}次补仓): +{shares}股 @ ${price}，总持仓{total_shares}股，平均成本${avg_price:.2f}")
        else:
            self.positions[stock_code] = {
                'shares': shares,
                'avg_price': price,
                'cost': cost,
                'initial_price': price,
                'initial_cost': cost,
                'add_count': 0,
                'strategy': strategy_type
            }
            print(f"买入 {stock_code}: {shares}股 @ ${price}，花费${cost:.2f}")

        return True

    def sell(self, stock_code, price):
        """卖出股票"""
        if stock_code not in self.positions:
            print(f"没有持仓 {stock_code}")
            return False

        position = self.positions[stock_code]
        revenue = position['shares'] * price
        profit = revenue - position['cost']
        profit_percent = profit / position['cost'] * 100 if position['cost'] else 0

        self.cash += revenue
        del self.positions[stock_code]

        print(f"卖出 {stock_code}: {position['shares']}股 @ ${price}，盈亏${profit:.2f} ({profit_percent:.2f}%)")
        return True

    def check_stop_loss(self, stock_code, current_price):
        """检查止损"""
        if stock_code not in self.positions:
            return False

        position = self.positions[stock_code]
        stop_loss = self.calculate_stop_loss(position['avg_price'], position['strategy'])

        if current_price <= stop_loss:
            print(f"{stock_code} 触发止损，当前价${current_price} <= 止损价${stop_loss:.2f}")
            self.sell(stock_code, current_price)
            return True

        return False

    def check_take_profit(self, stock_code, current_price):
        """检查止盈"""
        if stock_code not in self.positions:
            return False

        position = self.positions[stock_code]
        take_profit = self.calculate_take_profit(position['avg_price'], position['strategy'])

        if current_price >= take_profit:
            print(f"{stock_code} 触发止盈，当前价${current_price} >= 止盈价${take_profit:.2f}")
            self.sell(stock_code, current_price)
            return True

        return False

    def get_position_summary(self):
        """获取持仓 summary"""
        total_value = self.cash

        print(f"\n{'='*60}")
        print("持仓 summary")
        print(f"{'='*60}")
        print(f"现金: ${self.cash:.2f} ({self.cash/self.total_capital*100:.1f}%)")

        for stock_code, pos in self.positions.items():
            total_value += pos['cost']
            print(f"{stock_code}: {pos['shares']}股，平均成本${pos['avg_price']:.2f}，总成本${pos['cost']:.2f}，已补仓{pos.get('add_count', 0)}次，策略: {pos['strategy']}")

        print(f"{'='*60}")
        print(f"总资产: ${total_value:.2f}")
        print(f"现金比例: {self.cash/total_value*100:.1f}%")
        print(f"持仓比例: {(total_value-self.cash)/total_value*100:.1f}%")
        print(f"{'='*60}")

        return total_value


def generate_position_plan(results, total_capital=15000):
    """生成仓位配置建议"""
    print(f"\n{'='*60}")
    print("仓位配置建议")
    print(f"总资金: ${total_capital:,.2f}")
    print(f"{'='*60}")

    sorted_results = sorted(results, key=lambda x: x['total_score'], reverse=True)

    selected_stocks = []
    for result in sorted_results[:2]:
        if result['total_score'] >= 5.0:
            selected_stocks.append(result)

    if not selected_stocks:
        print("没有符合条件的股票，建议持有现金")
        return []

    manager = PositionManager(total_capital)
    position_plan = []

    for stock in selected_stocks:
        stock_code = stock['stock_code']
        stock_name = stock['stock_name']
        total_score = stock['total_score']

        position_size = manager.calculate_position_size(total_score, "both")
        nonlinear_buy = stock['nonlinear']['buy']

        buy_price = nonlinear_buy
        shares = int(position_size / buy_price)
        actual_cost = shares * buy_price

        stop_loss = manager.calculate_stop_loss(buy_price, "nonlinear")
        take_profit = manager.calculate_take_profit(buy_price, "nonlinear")

        plan = {
            'stock_code': stock_code,
            'stock_name': stock_name,
            'total_score': total_score,
            'position_size': position_size,
            'buy_price': buy_price,
            'shares': shares,
            'actual_cost': actual_cost,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'stop_loss_percent': (buy_price - stop_loss) / buy_price * 100,
            'take_profit_percent': (take_profit - buy_price) / buy_price * 100
        }
        position_plan.append(plan)

        print(f"\n{stock_code} {stock_name} (评分: {total_score:.1f}分)")
        print(f"  建议仓位: ${position_size:,.2f} ({position_size/total_capital*100:.1f}%)")
        print(f"  买入价: ${buy_price:.2f}")
        print(f"  买入股数: {shares}股")
        print(f"  实际成本: ${actual_cost:,.2f}")
        print(f"  止损价: ${stop_loss:.2f} (-{plan['stop_loss_percent']:.1f}%)")
        print(f"  止盈价: ${take_profit:.2f} (+{plan['take_profit_percent']:.1f}%)")

    total_invested = sum(p['actual_cost'] for p in position_plan)
    cash_remaining = total_capital - total_invested

    print(f"\n{'='*60}")
    print("配置 summary")
    print(f"{'='*60}")
    print(f"总资金: ${total_capital:,.2f}")
    print(f"总投入: ${total_invested:,.2f} ({total_invested/total_capital*100:.1f}%)")
    print(f"剩余现金: ${cash_remaining:,.2f} ({cash_remaining/total_capital*100:.1f}%)")
    print(f"{'='*60}")

    return position_plan


def generate_add_position_plan(stock_code, current_price, avg_cost, strategy_type="nonlinear", add_count=0):
    """生成阶梯补仓计划（感知已补仓次数 add_count）"""
    print(f"\n{'='*60}")
    print(f"{stock_code} 补仓计划")
    print(f"{'='*60}")

    drop_percent = (avg_cost - current_price) / avg_cost * 100

    print(f"基准成本: ${avg_cost:.2f}")
    print(f"当前价: ${current_price:.2f}")
    print(f"下跌幅度: {drop_percent:.2f}% | 已补仓次数: {add_count}/3")

    unit_ratio = 0.25 if strategy_type == "nonlinear" else 0.20
    rules = [
        (5, unit_ratio, f"第1档: 补仓{int(unit_ratio*100)}%"),
        (10, unit_ratio, f"第2档: 补仓{int(unit_ratio*100)}%"),
        (15, unit_ratio, f"第3档: 补仓{int(unit_ratio*100)}%")
    ]

    print("\n补仓规则:")
    for drop, ratio, desc in rules:
        print(f"  下跌{drop}%: {desc}")

    if add_count >= len(rules):
        print("\n已达到最大补仓次数(3次)，不再补仓")
        return 0

    next_drop, next_ratio, next_desc = rules[add_count]
    if drop_percent >= next_drop:
        original_position = 15000 * 0.3
        add_amount = original_position * next_ratio
        add_shares = int(add_amount / current_price)

        print(f"\n当前建议 ({next_desc}):")
        print(f"  补仓比例: {next_ratio*100:.0f}%")
        print(f"  补仓金额: ${add_amount:,.2f}")
        print(f"  补仓股数: {add_shares}股")
        return next_ratio
    else:
        print(f"\n当前不需要补仓（下一档需下跌达到 {next_drop}%）")
        return 0


def main():
    """主函数"""
    print("仓位管理系统")
    print("=" * 60)

    sample_results = [
        {
            'stock_code': 'MU',
            'stock_name': '美光科技',
            'total_score': 6.1,
            'linear': {'buy': 923.08},
            'nonlinear': {'buy': 890.05}
        },
        {
            'stock_code': 'SOXL',
            'stock_name': '半导体ETF',
            'total_score': 5.9,
            'linear': {'buy': 137.70},
            'nonlinear': {'buy': 133.32}
        },
        {
            'stock_code': 'COHR',
            'stock_name': 'Coherent',
            'total_score': 5.9,
            'linear': {'buy': 309.54},
            'nonlinear': {'buy': 303.75}
        }
    ]

    generate_position_plan(sample_results, 15000)
    generate_add_position_plan('MU', 875.00, 923.08, 'nonlinear', add_count=0)


if __name__ == "__main__":
    main()
