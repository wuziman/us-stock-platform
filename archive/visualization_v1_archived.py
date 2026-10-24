"""
可视化模块
提供回测结果和交易信号的可视化
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from typing import Dict, List, Optional
import warnings

warnings.filterwarnings('ignore')

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Arial Unicode MS', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


def plot_backtest_results(
    df: pd.DataFrame,
    title: str = "回测结果",
    save_path: str = None
):
    """
    绘制回测结果

    参数:
        df: 包含回测结果的DataFrame
        title: 图表标题
        save_path: 保存路径
    """
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), height_ratios=[3, 1, 1])

    # 1. 价格和信号
    ax1 = axes[0]
    ax1.plot(df.index, df['Close'], label='Close Price', color='black', linewidth=1)

    if 'SMA_Short' in df.columns:
        ax1.plot(df.index, df['SMA_Short'], label='Short MA', color='blue', alpha=0.7)
    if 'SMA_Long' in df.columns:
        ax1.plot(df.index, df['SMA_Long'], label='Long MA', color='red', alpha=0.7)

    # 标记买入信号
    buy_signals = df[df['Trade'] == 1]
    sell_signals = df[df['Trade'] == -1]

    ax1.scatter(buy_signals.index, buy_signals['Close'],
               marker='^', color='green', s=100, label='Buy', zorder=5)
    ax1.scatter(sell_signals.index, sell_signals['Close'],
               marker='v', color='red', s=100, label='Sell', zorder=5)

    ax1.set_title(title, fontsize=14)
    ax1.set_ylabel('Price')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)

    # 2. 资金曲线
    ax2 = axes[1]
    if 'Total' in df.columns:
        ax2.plot(df.index, df['Total'], label='Total Value', color='blue', linewidth=1)
        ax2.fill_between(df.index, df['Total'], alpha=0.3)
        ax2.set_ylabel('Portfolio Value')
        ax2.legend(loc='upper left')
        ax2.grid(True, alpha=0.3)

    # 3. 回撤
    ax3 = axes[2]
    if 'Total' in df.columns:
        peak = df['Total'].expanding(min_periods=1).max()
        drawdown = (df['Total'] - peak) / peak
        ax3.fill_between(df.index, drawdown, 0, color='red', alpha=0.3)
        ax3.set_ylabel('Drawdown')
        ax3.set_xlabel('Date')
        ax3.grid(True, alpha=0.3)

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"图表已保存: {save_path}")

    plt.show()


def plot_strategy_comparison(
    results_dict: Dict[str, pd.DataFrame],
    title: str = "策略对比",
    save_path: str = None
):
    """
    对比多个策略的表现

    参数:
        results_dict: {策略名: 回测结果DataFrame}
        title: 图表标题
        save_path: 保存路径
    """
    fig, axes = plt.subplots(2, 1, figsize=(14, 8))

    # 1. 资金曲线对比
    ax1 = axes[0]
    colors = ['blue', 'red', 'green', 'orange', 'purple', 'brown']

    for i, (name, df) in enumerate(results_dict.items()):
        if 'Total' in df.columns:
            # 归一化到1
            normalized = df['Total'] / df['Total'].iloc[0]
            ax1.plot(df.index, normalized, label=name,
                    color=colors[i % len(colors)], linewidth=1.5)

    ax1.set_title(title, fontsize=14)
    ax1.set_ylabel('Normalized Value')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)

    # 2. 回撤对比
    ax2 = axes[1]
    for i, (name, df) in enumerate(results_dict.items()):
        if 'Total' in df.columns:
            peak = df['Total'].expanding(min_periods=1).max()
            drawdown = (df['Total'] - peak) / peak
            ax2.plot(df.index, drawdown, label=name,
                    color=colors[i % len(colors)], linewidth=1.5)

    ax2.set_ylabel('Drawdown')
    ax2.set_xlabel('Date')
    ax2.legend(loc='lower left')
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"图表已保存: {save_path}")

    plt.show()


def plot_monthly_returns(
    df: pd.DataFrame,
    title: str = "月度收益分布",
    save_path: str = None
):
    """
    绘制月度收益分布

    参数:
        df: 回测结果
        title: 图表标题
        save_path: 保存路径
    """
    if 'Total' not in df.columns:
        print("错误: DataFrame中缺少'Total'列")
        return

    # 计算月度收益
    monthly_returns = df['Total'].resample('ME').last().pct_change().dropna()

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # 1. 月度收益柱状图
    ax1 = axes[0]
    colors = ['green' if x > 0 else 'red' for x in monthly_returns]
    ax1.bar(monthly_returns.index, monthly_returns * 100, color=colors, alpha=0.7)
    ax1.axhline(y=0, color='black', linestyle='-', linewidth=0.5)
    ax1.set_title(title, fontsize=14)
    ax1.set_ylabel('Return (%)')
    ax1.set_xlabel('Date')
    ax1.grid(True, alpha=0.3)

    # 2. 收益分布直方图
    ax2 = axes[1]
    ax2.hist(monthly_returns * 100, bins=20, color='steelblue', alpha=0.7, edgecolor='black')
    ax2.axvline(x=monthly_returns.mean() * 100, color='red', linestyle='--',
                label=f'Mean: {monthly_returns.mean()*100:.2f}%')
    ax2.set_title('Monthly Returns Distribution', fontsize=14)
    ax2.set_xlabel('Return (%)')
    ax2.set_ylabel('Frequency')
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"图表已保存: {save_path}")

    plt.show()


def plot_indicators(
    df: pd.DataFrame,
    indicators: List[str] = None,
    title: str = "技术指标",
    save_path: str = None
):
    """
    绘制技术指标

    参数:
        df: 包含指标数据的DataFrame
        indicators: 要绘制的指标列表
        title: 图表标题
        save_path: 保存路径
    """
    if indicators is None:
        indicators = [col for col in df.columns if col not in
                     ['Open', 'High', 'Low', 'Close', 'Volume', 'Symbol']]

    n_indicators = len(indicators)
    if n_indicators == 0:
        print("没有找到要绘制的指标")
        return

    fig, axes = plt.subplots(n_indicators + 1, 1, figsize=(14, 3 * (n_indicators + 1)))

    # 价格图
    axes[0].plot(df.index, df['Close'], color='black', linewidth=1)
    axes[0].set_title(title, fontsize=14)
    axes[0].set_ylabel('Price')
    axes[0].grid(True, alpha=0.3)

    # 指标图
    for i, indicator in enumerate(indicators):
        if indicator in df.columns:
            axes[i + 1].plot(df.index, df[indicator], label=indicator, linewidth=1)
            axes[i + 1].set_ylabel(indicator)
            axes[i + 1].legend(loc='upper left')
            axes[i + 1].grid(True, alpha=0.3)

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"图表已保存: {save_path}")

    plt.show()


def plot_portfolio_allocation(
    weights: Dict[str, float],
    title: str = "投资组合配置",
    save_path: str = None
):
    """
    绘制投资组合饼图

    参数:
        weights: {股票代码: 权重}
        title: 图表标题
        save_path: 保存路径
    """
    fig, ax = plt.subplots(figsize=(10, 8))

    labels = list(weights.keys())
    sizes = list(weights.values())
    colors = plt.cm.Set3(np.linspace(0, 1, len(labels)))

    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, colors=colors,
        autopct='%1.1f%%', startangle=90,
        pctdistance=0.85
    )

    # 美化
    for text in texts:
        text.set_fontsize(12)
    for autotext in autotexts:
        autotext.set_fontsize(10)
        autotext.set_fontweight('bold')

    ax.set_title(title, fontsize=14)

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"图表已保存: {save_path}")

    plt.show()


def create_summary_table(stats: Dict, strategy_name: str = "Strategy") -> pd.DataFrame:
    """创建统计摘要表格"""
    data = {
        'Metric': [
            'Total Return',
            'Annual Return',
            'Max Drawdown',
            'Sharpe Ratio',
            'Win Rate',
            'Total Trades',
            'Avg Return per Trade',
            'Best Trade',
            'Worst Trade'
        ],
        'Value': [
            f"{stats.get('total_return', 0):.2%}",
            f"{stats.get('annual_return', 0):.2%}",
            f"{stats.get('max_drawdown', 0):.2%}",
            f"{stats.get('sharpe_ratio', 0):.2f}",
            f"{stats.get('win_rate', 0):.2%}",
            f"{stats.get('n_trades', 0)}",
            f"{stats.get('avg_return', 0):.2%}",
            f"{stats.get('best_trade', 0):.2%}",
            f"{stats.get('worst_trade', 0):.2%}"
        ]
    }

    df = pd.DataFrame(data)
    df = df.set_index('Metric')
    df.columns = [strategy_name]

    return df


if __name__ == "__main__":
    # 创建示例数据
    dates = pd.date_range(start='2024-01-01', periods=252, freq='B')
    np.random.seed(42)

    # 模拟价格数据
    returns = np.random.normal(0.0005, 0.015, 252)
    prices = 100 * np.exp(np.cumsum(returns))

    df = pd.DataFrame({
        'Close': prices,
        'Total': prices * 1.1,
        'Trade': np.random.choice([0, 1, -1], size=252, p=[0.95, 0.025, 0.025])
    }, index=dates)

    # 测试绘图
    plot_backtest_results(df, title="示例回测结果")
