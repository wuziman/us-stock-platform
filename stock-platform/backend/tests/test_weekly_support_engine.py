# -*- coding: utf-8 -*-
"""
周线双底座量化引擎自动化大考 (Weekly Dual-Floor Support Engine Tests)
验收指标：
1. 美股科技七姐妹 (M7) + 半导体周期芯片 (TSM, MU, AMD, AVGO) + 高杠杆旗舰 (SOXL) 全矩阵 12 标的大考
2. 谷歌 (GOOGL) 第一底座精准命中 $290~$295，终极大底精准命中 $270~$272
3. SOXL 杠杆 ETF 1.5×ATR 专属波动通道补偿验证
4. 纯净离线合成测试，确保无网络环境下的数学逻辑绝对鲁棒
"""

import numpy as np
import pandas as pd

from app.services.weekly_support_service import WeeklySupportService

TARGET_12_TICKERS = [
    "GOOGL", "NVDA", "AAPL", "MSFT", "AMZN", "META", "TSLA",
    "TSM", "MU", "AMD", "AVGO",
    "SOXL"
]


def test_googl_weekly_support_benchmarks():
    """
    硬性红线验收：谷歌 (GOOGL)
    - 第一底座 (Tier 1) 落在 $290 ~ $295
    - 终极大底 (Tier 2) 落在 $270 ~ $272
    - 第一底座严格高于终极大底
    """
    svc = WeeklySupportService()
    res = svc.calculate_weekly_supports("GOOGL")

    assert res is not None, "GOOGL 周线底座计算结果不能为空"
    assert res["ticker"] == "GOOGL"
    assert 290.0 <= res["tier_1"] <= 295.0, f"GOOGL Tier 1 底座 {res['tier_1']} 未落在 $290~$295！"
    assert 270.0 <= res["tier_2"] <= 272.0, f"GOOGL Tier 2 铁底 {res['tier_2']} 未落在 $270~$272！"
    assert res["tier_1"] > res["tier_2"], "Tier 1 第一底座必须严格高于 Tier 2 终极底座"
    assert res["dist_to_tier2_pct"] > 0, "距离终极铁底百分比必须为正数"


def test_soxl_leveraged_volatility_compensation():
    """
    高杠杆旗舰验收：SOXL
    - 自动识别为杠杆标的 is_leveraged == True
    - 动态叠加 1.5×ATR 周线通道下沿过滤噪音
    - Tier 1 严格高于 Tier 2
    """
    svc = WeeklySupportService()
    res = svc.calculate_weekly_supports("SOXL")

    assert res is not None, "SOXL 周线底座计算结果不能为空"
    assert res["is_leveraged"] is True
    assert res["tier_1"] > 0
    assert res["tier_2"] > 0
    assert res["tier_1"] > res["tier_2"]
    assert res["atr14_wk"] > 0


def test_offline_synthetic_weekly_engine_math():
    """
    离线合成周线测试：脱离网络依赖验证纯数学计算的严密性
    """
    n = 156  # 3 年周线约 156 根
    idx = pd.date_range("2023-01-01", periods=n, freq="W-FRI")

    # 构造低点：52周最低 100.0，最近在 120 处多次触碰反弹
    base = np.linspace(80.0, 160.0, n)
    df_fake = pd.DataFrame({
        "Open": base - 1.0,
        "High": base + 3.0,
        "Low": base - 3.0,
        "Close": base,
        "Volume": np.full(n, 5000000),
    }, index=idx)

    # 在最近 52 周内人为注入 4 次在 120.0 处的回踩反弹
    recent_indices = [n - 40, n - 30, n - 20, n - 10]
    for i in recent_indices:
        df_fake.iloc[i, df_fake.columns.get_loc("Low")] = 120.0
        df_fake.iloc[i, df_fake.columns.get_loc("Close")] = 128.0

    svc = WeeklySupportService()
    res = svc.calculate_weekly_supports("TEST", df_wk=df_fake, current_price_override=145.0)

    assert res is not None
    assert res["ticker"] == "TEST"
    assert res["current_price"] == 145.0
    assert res["tier_1"] > res["tier_2"]
    assert res["tier_2"] > 0
    assert res["dist_to_tier2_pct"] > 0


def test_12_tickers_batch_real_market_matrix():
    """
    12 大热门标的全量物理真实大考：
    全部 12 只核心资产必须 100% 成功计算，且满足风控物理约束
    """
    svc = WeeklySupportService()
    matrix = svc.batch_calculate_weekly_supports(TARGET_12_TICKERS, max_workers=6)

    assert len(matrix) == 12, f"必须完整计算出全部 12 只标的，实际得到 {len(matrix)} 只"

    computed_tickers = [row["ticker"] for row in matrix]
    for expected_ticker in TARGET_12_TICKERS:
        assert expected_ticker in computed_tickers, f"缺少标的: {expected_ticker}"

    for row in matrix:
        assert row["current_price"] > 0, f"{row['ticker']} 现价异常: {row['current_price']}"
        assert row["tier_1"] > 0, f"{row['ticker']} 第一底座异常: {row['tier_1']}"
        assert row["tier_2"] > 0, f"{row['ticker']} 终极大底异常: {row['tier_2']}"
        assert row["tier_1"] > row["tier_2"], f"{row['ticker']} Tier 1 ({row['tier_1']}) 必须大于 Tier 2 ({row['tier_2']})"
        assert row["dist_to_tier2_pct"] >= 0, f"{row['ticker']} 距离终极大底百分比应 >= 0"
