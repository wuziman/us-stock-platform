# -*- coding: utf-8 -*-
"""
[第三段-9 独立专项测验] 2026-10-15 市场熔断与波动预警真实联动测试
不与其它回归混跑，单独验证 ReportService 真实链路：
- NORMAL 档：基准买点 buy_low 不变，position_scale = 1.0
- CAUTION 档：linear / nonlinear 的 buy_low 真实下移 2%（×0.98），position_scale = 0.5
- CIRCUIT_BREAKER 档：全面冻结新开仓点位（buy=None, buy_low=None, macd_add=None, macd_watch=None, position_scale=0.0）
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.report_service import ReportService


def _make_2026_10_15_kline(days: int = 65) -> pd.DataFrame:
    idx = pd.date_range(end="2026-10-15", periods=days, freq="B")
    t = np.arange(days, dtype=float)
    close = 110.0 + 3.5 * np.sin(t / 5.0) + 0.12 * t
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.8,
            "Low": close - 1.5,
            "Close": close,
            "Volume": np.full(days, 2_000_000, dtype=int),
        },
        index=idx,
    )


class TestRegimeLinkage20261015:
    def _run_report_on_2026_10_15(self, monkeypatch, vix_close: float, qqq_prev: float, qqq_curr: float):
        svc = ReportService()
        df_stock = _make_2026_10_15_kline()
        df_vix = pd.DataFrame(
            {"Close": [16.0, vix_close]},
            index=pd.to_datetime(["2026-10-14", "2026-10-15"]),
        )
        df_qqq = pd.DataFrame(
            {"Close": [qqq_prev, qqq_curr]},
            index=pd.to_datetime(["2026-10-14", "2026-10-15"]),
        )

        def fake_get_stock_data(code, market="US", period="3mo", use_cache=True):
            if code == "^VIX":
                return df_vix.copy()
            if code == "QQQ":
                return df_qqq.copy()
            return df_stock.copy()

        monkeypatch.setattr(svc.stock_service, "get_stock_data", fake_get_stock_data)
        return svc.generate_daily_report(
            [{"stock_code": "MU", "stock_name": "Micron"}],
            as_of_date="2026-10-15",
        )

    def test_2026_10_15_caution_shifts_buy_low_down_2pct_and_cb_freezes_entries(self, monkeypatch):
        # 1. NORMAL 基准态 (VIX=15.5, QQQ=+0.4%)
        res_norm = self._run_report_on_2026_10_15(monkeypatch, vix_close=15.5, qqq_prev=480.0, qqq_curr=481.92)
        snap_norm = res_norm["snapshots"][0]
        norm_lin_buy = snap_norm["linear"]["buy_low"]
        norm_nl_buy = snap_norm["nonlinear"]["buy_low"]
        assert res_norm["regime"]["status"] == "NORMAL"
        assert snap_norm["position_scale"] == 1.0

        # 2. CAUTION 预警态 (2026-10-15 VIX=24.2, QQQ=-1.8%) -> buy_low 真实下移 2%，仓位减半
        res_caut = self._run_report_on_2026_10_15(monkeypatch, vix_close=24.2, qqq_prev=480.0, qqq_curr=471.36)
        snap_caut = res_caut["snapshots"][0]
        assert res_caut["regime"]["status"] == "CAUTION"
        assert snap_caut["linear"]["buy_low"] == round(norm_lin_buy * 0.98, 2)
        assert snap_caut["linear"]["buy"] == round(norm_lin_buy * 0.98, 2)
        assert snap_caut["nonlinear"]["buy_low"] == round(norm_nl_buy * 0.98, 2)
        assert snap_caut["nonlinear"]["buy"] == round(norm_nl_buy * 0.98, 2)
        assert snap_caut["position_scale"] == 0.5

        # 3. CIRCUIT_BREAKER 熔断态 (2026-10-15 VIX=31.5, QQQ=-3.2%) -> 无新开仓点位
        res_cb = self._run_report_on_2026_10_15(monkeypatch, vix_close=31.5, qqq_prev=480.0, qqq_curr=464.64)
        snap_cb = res_cb["snapshots"][0]
        assert res_cb["regime"]["status"] == "CIRCUIT_BREAKER"
        assert snap_cb["linear"]["buy"] is None
        assert snap_cb["linear"]["buy_low"] is None
        assert snap_cb["nonlinear"]["buy"] is None
        assert snap_cb["nonlinear"]["buy_low"] is None
        assert snap_cb["macd_add"] is None
        assert snap_cb["macd_watch"] is None
        assert snap_cb["position_scale"] == 0.0
        assert "🚫熔断禁买(无新开仓点位)" in res_cb["report"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
