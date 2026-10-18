"""
仓位配置方案V2
第一次买入30%资金，明确补仓规则
"""


def generate_position_plan_v2(results, total_capital=15000):
    """
    生成仓位配置方案V2

    规则：
    1. 第一次买入：30%资金
    2. 补仓规则：下跌5%补一次，共补3次
    3. 每次补仓：原始仓位的25%
    """
    print(f"\n{'='*60}")
    print("仓位配置方案V2")
    print(f"总资金: ${total_capital:,.2f}")
    print(f"{'='*60}")

    # 按评分排序
    sorted_results = sorted(results, key=lambda x: x['total_score'], reverse=True)

    # 选择最好的1-2只股票
    selected_stocks = []
    for result in sorted_results[:2]:
        if result['total_score'] >= 5.0:
            selected_stocks.append(result)

    if not selected_stocks:
        print("没有符合条件的股票，建议持有现金")
        return []

    # 计算第一次买入仓位
    first_buy_ratio = 0.30  # 30%资金
    reserve_ratio = 0.50  # 50%预留补仓
    cash_ratio = 0.20  # 20%剩余现金

    first_buy_amount = total_capital * first_buy_ratio
    reserve_amount = total_capital * reserve_ratio
    cash_amount = total_capital * cash_ratio

    # 每只股票分配
    amount_per_stock = first_buy_amount / len(selected_stocks)

    print(f"\n第一次买入配置:")
    print(f"总资金: ${total_capital:,.2f}")
    print(f"第一次买入: {first_buy_ratio*100:.0f}% = ${first_buy_amount:,.2f}")
    print(f"预留补仓: {reserve_ratio*100:.0f}% = ${reserve_amount:,.2f}")
    print(f"剩余现金: {cash_ratio*100:.0f}% = ${cash_amount:,.2f}")
    print(f"股票数量: {len(selected_stocks)}只")
    print(f"每只分配: ${amount_per_stock:,.2f}")

    position_plans = []

    for stock in selected_stocks:
        stock_code = stock['stock_code']
        stock_name = stock['stock_name']
        total_score = stock['total_score']

        # 使用非线性策略买入价（胜率更高）
        buy_price = stock['nonlinear']['buy']

        # 计算买入股数
        shares = int(amount_per_stock / buy_price)
        actual_cost = shares * buy_price

        # 计算补仓计划
        add_positions = []
        original_position = actual_cost  # 第一次买入金额

        # 补仓规则：下跌5%、10%、15%各补25%
        for drop_percent in [5, 10, 15]:
            add_price = buy_price * (1 - drop_percent/100)
            add_amount = original_position * 0.25
            add_shares = int(add_amount / add_price)
            add_positions.append({
                'drop_percent': drop_percent,
                'price': add_price,
                'amount': add_amount,
                'shares': add_shares
            })

        plan = {
            'stock_code': stock_code,
            'stock_name': stock_name,
            'total_score': total_score,
            'first_buy': {
                'price': buy_price,
                'shares': shares,
                'cost': actual_cost,
                'percent': actual_cost / total_capital * 100
            },
            'add_positions': add_positions,
            'total_invested': actual_cost,
            'total_shares': shares,
            'avg_cost': buy_price
        }
        position_plans.append(plan)

        print(f"\n{'='*60}")
        print(f"{stock_code} {stock_name} (评分: {total_score:.1f}分)")
        print(f"{'='*60}")

        print(f"\n【第一次买入】")
        print(f"  买入价: ${buy_price:.2f}")
        print(f"  买入股数: {shares}股")
        print(f"  买入金额: ${actual_cost:,.2f}")
        print(f"  占总资金: {actual_cost/total_capital*100:.1f}%")

        print(f"\n【补仓计划】")
        print(f"  补仓规则: 下跌5%、10%、15%各补25%")
        print(f"  原始仓位: ${original_position:,.2f}")

        for i, add in enumerate(add_positions, 1):
            print(f"\n  第{i}次补仓（下跌{add['drop_percent']}%）:")
            print(f"    补仓价: ${add['price']:.2f}")
            print(f"    补仓股数: {add['shares']}股")
            print(f"    补仓金额: ${add['amount']:,.2f}")

        # 计算补仓后的平均成本
        total_cost = actual_cost
        total_shares = shares
        for add in add_positions:
            total_cost += add['amount']
            total_shares += add['shares']
        avg_cost_after = total_cost / total_shares

        print(f"\n【补仓后预期】")
        print(f"  总投入: ${total_cost:,.2f}")
        print(f"  总股数: {total_shares}股")
        print(f"  平均成本: ${avg_cost_after:.2f}")
        print(f"  平均成本降低: {(buy_price - avg_cost_after) / buy_price * 100:.1f}%")

    # 总体 summary
    total_first_buy = sum(p['first_buy']['cost'] for p in position_plans)
    total_add = sum(sum(a['amount'] for a in p['add_positions']) for p in position_plans)
    total_invested = total_first_buy + total_add
    cash_remaining = total_capital - total_first_buy

    print(f"\n{'='*60}")
    print("总体配置 summary")
    print(f"{'='*60}")
    print(f"总资金: ${total_capital:,.2f}")
    print(f"第一次买入: ${total_first_buy:,.2f} ({total_first_buy/total_capital*100:.1f}%)")
    print(f"预留补仓: ${reserve_amount:,.2f} ({reserve_ratio*100:.0f}%)")
    print(f"剩余现金: ${cash_amount:,.2f} ({cash_ratio*100:.0f}%)")
    print(f"{'='*60}")
    print(f"")
    print(f"【特点】")
    print(f"  - 没有单只股票仓位上限")
    print(f"  - 50%资金可用于补仓任意一只股票")
    print(f"  - 只买两只股票，集中资金补仓")

    return position_plans


def main():
    """主函数"""
    print("仓位配置方案V2")
    print("=" * 60)

    # 示例数据
    sample_results = [
        {
            'stock_code': 'MU',
            'stock_name': '美光科技',
            'total_score': 6.1,
            'nonlinear': {'buy': 890.05}
        },
        {
            'stock_code': 'SOXL',
            'stock_name': '半导体ETF',
            'total_score': 5.9,
            'nonlinear': {'buy': 133.32}
        }
    ]

    # 生成配置方案
    position_plans = generate_position_plan_v2(sample_results, 15000)


if __name__ == "__main__":
    main()
