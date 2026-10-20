"""
两个版本对比脚本
对比简单版 vs 研究验证版
"""

import json
from datetime import datetime
from pathlib import Path
from utils.stock_analysis import StockAnalyzer


def format_comparison_report(simple_results: list, research_results: list) -> str:
    """
    格式化对比报告
    """
    report = []
    report.append(f"【版本对比报告】{datetime.now().strftime('%m-%d %H:%M')}")
    report.append("=" * 60)
    report.append("")

    # 按股票代码排序
    simple_dict = {r['stock_code']: r for r in simple_results}
    research_dict = {r['stock_code']: r for r in research_results}

    report.append("| 股票 | 版本 | 评分 | 操作 | 买入价 |")
    report.append("|------|------|------|------|--------|")

    for stock_code in simple_dict.keys():
        simple = simple_dict.get(stock_code, {})
        research = research_dict.get(stock_code, {})

        simple_score = simple.get('scores', {}).get('total', 0)
        research_score = research.get('scores', {}).get('total', 0)

        simple_action = simple.get('recommendation', {}).get('action', 'N/A')
        research_action = research.get('recommendation', {}).get('action', 'N/A')

        simple_buy = simple.get('price_levels', {}).get('buy_price', 0)
        research_buy = research.get('price_levels', {}).get('buy_price', 0)

        report.append(f"| {stock_code} | 简单版 | {simple_score} | {simple_action} | ${simple_buy} |")
        report.append(f"| {stock_code} | 研究版 | {research_score} | {research_action} | ${research_buy} |")

    report.append("")
    report.append("=" * 60)
    report.append("【权重对比】")
    report.append("简单版：技术面100%（只用技术面）")
    report.append("研究版：技术35% + 消息15% + 宏观25% + 事件25%")
    report.append("")
    report.append("【数据来源】")
    report.append("研究验证版权重来自：Alpha Learning学术研究")
    report.append("仅供参考，投资有风险")

    return "\n".join(report)


def main():
    """主函数"""
    print("开始版本对比分析...")
    print("=" * 60)

    # 初始化分析器
    analyzer = StockAnalyzer()

    # 加载股票列表
    try:
        with open("config/config.json", 'r', encoding='utf-8') as f:
            config = json.load(f)
            stocks = config.get("stocks", [])
    except Exception as e:
        print(f"加载配置失败: {e}")
        return

    # 简单版分析
    print("\n【简单版分析】")
    simple_results = []
    for stock in stocks:
        try:
            result = analyzer.analyze_stock(stock['code'], stock['name'], mode="simple")
            simple_results.append(result)
        except Exception as e:
            print(f"分析 {stock['code']} 失败: {e}")

    # 研究验证版分析
    print("\n【研究验证版分析】")
    research_results = []
    for stock in stocks:
        try:
            result = analyzer.analyze_stock(stock['code'], stock['name'], mode="research")
            research_results.append(result)
        except Exception as e:
            print(f"分析 {stock['code']} 失败: {e}")

    # 生成对比报告
    report = format_comparison_report(simple_results, research_results)

    # 保存报告
    report_dir = Path("reports")
    report_dir.mkdir(exist_ok=True)
    report_file = report_dir / f"comparison_report_{datetime.now().strftime('%Y%m%d')}.txt"

    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"\n对比报告已保存: {report_file}")
    print("\n" + report)


if __name__ == "__main__":
    main()
