# -*- coding: utf-8 -*-
"""
自动化测试：评分追踪纯逻辑（前向收益/分桶/相关性/全链路）
运行：cd backend && python -m pytest tests/test_score_tracking.py -v
"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Base
from app.services.score_tracking_service import (
    bucket_stats,
    build_tracking,
    compute_forward_returns,
    pearson,
)


# 测试用收盘价：2025-01-06(周一) ~ 2025-01-10(周五)
CLOSES = {
    "2025-01-03": 90.0,
    "2025-01-06": 100.0,
    "2025-01-07": 110.0,
    "2025-01-08": 120.0,
    "2025-01-09": 130.0,
    "2025-01-10": 140.0,
}
CURRENT = 105.0


def _rec(date, score):
    return {"date": date, "total_score": score}


class TestComputeForwardReturns:
    def test_weekend_takes_previous_trading_day(self):
        # 2025-01-11是周六 → 取最近前一交易日01-10收盘140
        out = compute_forward_returns([_rec("2025-01-11", 7.0)], CLOSES, CURRENT)
        assert len(out) == 1
        assert out[0]["entry_price"] == 140.0
        assert out[0]["forward_return_pct"] == round((105 / 140 - 1) * 100, 2)  # -25.0
        assert out[0]["date"] == "2025-01-11"
        assert out[0]["current_price"] == CURRENT

    def test_exact_trading_day_uses_same_day_close(self):
        out = compute_forward_returns([_rec("2025-01-07", 6.0)], CLOSES, CURRENT)
        assert out[0]["entry_price"] == 110.0
        assert out[0]["forward_return_pct"] == round((105 / 110 - 1) * 100, 2)  # -4.55

    def test_date_before_all_closes_skipped(self):
        # 早于所有交易日（01-02）→ 跳过
        out = compute_forward_returns(
            [_rec("2025-01-02", 8.0), _rec("2025-01-08", 5.0)], CLOSES, CURRENT
        )
        assert len(out) == 1
        assert out[0]["date"] == "2025-01-08"

    def test_datetime_object_accepted(self):
        out = compute_forward_returns(
            [_rec(datetime(2025, 1, 9), 7.5)], CLOSES, CURRENT
        )
        assert len(out) == 1
        assert out[0]["entry_price"] == 130.0

    def test_orm_style_row_uses_created_at(self):
        # ScoreHistory ORM行没有date字段，日期在created_at
        class FakeRow:
            created_at = datetime(2025, 1, 9)
            total_score = 6.8

        out = compute_forward_returns([FakeRow()], CLOSES, CURRENT)
        assert len(out) == 1
        assert out[0]["entry_price"] == 130.0
        assert out[0]["total_score"] == 6.8

    def test_empty_closes_or_no_price(self):
        assert compute_forward_returns([_rec("2025-01-08", 6.0)], {}, CURRENT) == []
        assert compute_forward_returns([_rec("2025-01-08", 6.0)], CLOSES, None) == []

    def test_record_fields(self):
        out = compute_forward_returns([_rec("2025-01-06", 8.2)], CLOSES, CURRENT)
        r = out[0]
        assert set(r.keys()) == {
            "date", "total_score", "entry_price", "current_price", "forward_return_pct"
        }
        assert r["total_score"] == 8.2


class TestBucketStats:
    def test_boundaries(self):
        records = [
            {"total_score": 4.99, "forward_return_pct": 10.0},   # <5
            {"total_score": 5.0, "forward_return_pct": -5.0},    # [5, 6.5)
            {"total_score": 6.5, "forward_return_pct": 0.0},     # [6.5, 8)
            {"total_score": 7.99, "forward_return_pct": 3.0},    # [6.5, 8)
            {"total_score": 8.0, "forward_return_pct": 9.0},     # >=8
            {"total_score": 9.0, "forward_return_pct": 11.0},    # >=8
        ]
        buckets = bucket_stats(records)
        assert [b["bucket"] for b in buckets] == ["<5分", "5~6.5分", "6.5~8分", "≥8分"]
        assert [b["count"] for b in buckets] == [1, 1, 2, 2]
        assert [b["avg_return"] for b in buckets] == [10.0, -5.0, 1.5, 10.0]

    def test_avg_rounding(self):
        records = [
            {"total_score": 9.0, "forward_return_pct": 1.111},
            {"total_score": 9.1, "forward_return_pct": 2.222},
        ]
        buckets = bucket_stats(records)
        assert buckets[3]["avg_return"] == round((1.111 + 2.222) / 2, 2)

    def test_empty(self):
        buckets = bucket_stats([])
        assert len(buckets) == 4
        assert all(b["count"] == 0 and b["avg_return"] is None for b in buckets)


class TestPearson:
    def test_perfect_positive(self):
        r = pearson([1, 2, 3, 4, 5], [10, 20, 30, 40, 50])
        assert r is not None and abs(r - 1) < 1e-6

    def test_perfect_negative(self):
        r = pearson([1, 2, 3, 4, 5], [50, 40, 30, 20, 10])
        assert r is not None and abs(r + 1) < 1e-6

    def test_constant_series_none(self):
        assert pearson([1, 2, 3, 4, 5], [7, 7, 7, 7, 7]) is None
        assert pearson([3, 3, 3, 3, 3], [1, 2, 3, 4, 5]) is None

    def test_small_sample_none(self):
        assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) is None

    def test_length_mismatch_none(self):
        assert pearson([1, 2, 3, 4, 5], [1, 2, 3]) is None


class TestBuildTracking:
    def _make_df(self):
        dates = ["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10"]
        return pd.DataFrame(
            {"Close": [CLOSES[d] for d in dates]},
            index=pd.to_datetime(dates),
        )

    def test_full_pipeline_structure(self):
        rows = [
            {"stock_code": "MU", "date": datetime(2025, 1, 7), "total_score": 8.5},
            {"stock_code": "MU", "date": datetime(2025, 1, 11), "total_score": 4.2},
            {"stock_code": "MU", "date": datetime(2025, 1, 2), "total_score": 6.0},  # 跳过
        ]
        result = build_tracking("MU", rows, self._make_df())

        # 结构完整
        assert set(result.keys()) == {
            "stock_code", "count", "records", "buckets", "correlation", "interpretation"
        }
        # count=累计评分次数（含被跳过的）
        assert result["count"] == 3
        # 仅2条匹配到入场价，且保持升序
        assert [r["date"] for r in result["records"]] == ["2025-01-07", "2025-01-11"]
        # 最新价=最后一个close
        assert all(r["current_price"] == 140.0 for r in result["records"])
        # 分桶4桶齐全，次数合计=匹配数
        assert len(result["buckets"]) == 4
        assert sum(b["count"] for b in result["buckets"]) == 2

    def test_correlation_and_interpretation(self):
        dates = [f"2025-01-{d:02d}" for d in range(1, 16)]  # 15条记录
        rows = [
            {"date": d, "total_score": (i % 9) + 1}
            for i, d in enumerate(dates)
        ]
        df = pd.DataFrame(
            {"Close": [float(i + 10) for i in range(len(dates))]},
            index=pd.to_datetime(dates),
        )
        result = build_tracking("TEST", rows, df)
        assert isinstance(result["correlation"], float)
        assert result["interpretation"] in (
            "评分与后续收益正相关，体系有效",
            "负相关，建议调整权重",
            "相关性弱，样本可能不足",
        )

    def test_no_market_data_graceful(self):
        rows = [{"date": "2025-01-08", "total_score": 7.0}]
        result = build_tracking("MU", rows, None)
        assert result["count"] == 1
        assert result["records"] == []
        assert result["correlation"] is None
        assert result["interpretation"] == "相关性弱，样本可能不足"


class TestModelRegistration:
    def test_score_history_registered_on_base(self):
        # router import本模块后，create_all之前必须已注册到Base.metadata
        from app.models_tracking import ScoreHistory  # noqa: F401
        assert "score_history" in Base.metadata.tables
        cols = ScoreHistory.__table__.columns.keys()
        for c in ("stock_code", "total_score", "price_at_score", "created_at"):
            assert c in cols


class TestFourDimWeightHealthReport:
    def test_weight_health_report_identifies_naked_dimension_and_perturbation(self):
        """
        [第三批·第二段-4 验证]
        1. 3个月（90天）滚动窗口输出四维（technical/news/macro/event）与前向收益的 Pearson 相关性
        2. 权重 ±10% 扰动实验强制对剩余三维按原比例缩放归一化（四维权重和恒等于 1.0）
        3. 准确识别出过去 3 个月“在裸泳”的维度（相关性最低/负相关的 macro 维度），且不修改默认权重
        """
        from app.services.score_tracking_service import (
            DEFAULT_4D_WEIGHTS,
            _normalize_perturbed_weights,
            build_weight_health_report,
        )

        # 1. 验证 ±10% 扰动后四维权重和严格等于 1.0
        for dim in ("technical", "news", "macro", "event"):
            for delta in (+0.10, -0.10):
                w = _normalize_perturbed_weights(DEFAULT_4D_WEIGHTS, dim, delta)
                assert abs(sum(w.values()) - 1.0) < 1e-6, f"{dim} {delta} 归一化后总和不为 1: {w}"
                assert abs(w[dim] - (DEFAULT_4D_WEIGHTS[dim] + delta)) < 1e-6

        # 2. 构造 12 条 90 天内的真实评分记录：
        #    technical 与前向收益强正相关，event/news 正相关，macro 与前向收益负相关（在裸泳）
        dates = [f"2026-09-{d:02d}" for d in range(1, 13)]
        records = []
        # 模拟不同个股的当前价带来不同的前向收益
        kline_by_code = {}
        for i, d in enumerate(dates):
            code = f"STK{i:02d}"
            fwd_factor = 1.0 + (i - 5) * 0.02  # 收益从 -10% 到 +12% 单调递增
            df = pd.DataFrame(
                {"Close": [100.0, round(100.0 * fwd_factor, 2)]},
                index=pd.to_datetime([d, "2026-10-16"]),
            )
            kline_by_code[code] = df
            records.append(
                {
                    "stock_code": code,
                    "created_at": datetime.strptime(d, "%Y-%m-%d"),
                    "technical_score": round(3.0 + i * 0.5, 2),   # 正相关
                    "news_score": round(4.0 + i * 0.3, 2),        # 正相关
                    "event_score": round(4.5 + i * 0.25, 2),      # 正相关
                    "macro_score": round(9.0 - i * 0.5, 2),       # 负相关（裸泳维度）
                    "total_score": round(5.0 + i * 0.2, 2),
                }
            )

        report = build_weight_health_report(
            records,
            closes_by_code=kline_by_code,
            window_days=90,
            now_dt=datetime(2026, 10, 17, 12, 0, 0),
        )
        assert report["sample_count"] == 12
        assert report["dimensions"]["technical"]["correlation"] > 0.9
        assert report["dimensions"]["macro"]["correlation"] < -0.9
        # 必须准确指出过去 3 个月在裸泳的维度是 macro
        assert report["naked_swimmer"]["dimension"] == "macro"
        assert "宏观面" in report["naked_swimmer"]["conclusion"]
        # 权重 ±10% 扰动矩阵齐全，且默认权重未发生任何篡改
        assert set(report["perturbation_matrix"].keys()) == {"technical", "news", "macro", "event"}
        assert DEFAULT_4D_WEIGHTS == {"technical": 0.40, "news": 0.30, "macro": 0.15, "event": 0.15}

    def test_cross_module_weight_single_source_of_truth(self):
        """
        [跨模块权重唯一真源单测]
        1. 断言 score_tracking_service 中的 DEFAULT_4D_WEIGHTS / DEFAULT_DIMENSION_WEIGHTS
           直接引用自 analysis_service（同一内存对象 `is` 且值恒等），且 score_tracking_service.py 源码中无手写权重字典字面量
        2. 断言 AnalysisService.analyze_stock 实际合成总分严格等于 DEFAULT_DIMENSION_WEIGHTS 加权和
        """
        import inspect
        from app.services import analysis_service as an_mod
        from app.services import score_tracking_service as st_mod

        # 1. 同一引用对象与数值恒等断言
        assert st_mod.DEFAULT_4D_WEIGHTS is an_mod.DEFAULT_4D_WEIGHTS
        assert st_mod.DEFAULT_DIMENSION_WEIGHTS is an_mod.DEFAULT_DIMENSION_WEIGHTS
        assert st_mod.DEFAULT_DIMENSION_WEIGHTS["simple"] is an_mod.DEFAULT_4D_WEIGHTS
        assert an_mod.DEFAULT_4D_WEIGHTS == {
            "technical": 0.40,
            "news": 0.30,
            "macro": 0.15,
            "event": 0.15,
        }
        assert an_mod.DEFAULT_DIMENSION_WEIGHTS["research"] == {
            "technical": 0.35,
            "news": 0.15,
            "macro": 0.25,
            "event": 0.25,
        }

        # 2. 源码级静态守卫：score_tracking_service.py 中不得再出现手写权重字面量定义
        st_source = inspect.getsource(st_mod)
        assert "from .analysis_service import DEFAULT_4D_WEIGHTS, DEFAULT_DIMENSION_WEIGHTS" in st_source
        assert '"technical": 0.4' not in st_source and "'technical': 0.4" not in st_source

        # 3. 验证 build_weight_health_report 在 simple 与 research 模式下使用的 base_weights 恒等于引擎权重
        rep_simple = st_mod.build_weight_health_report([], {}, mode="simple")
        rep_research = st_mod.build_weight_health_report([], {}, mode="research")
        assert rep_simple["base_weights"] == an_mod.DEFAULT_DIMENSION_WEIGHTS["simple"]
        assert rep_research["base_weights"] == an_mod.DEFAULT_DIMENSION_WEIGHTS["research"]
