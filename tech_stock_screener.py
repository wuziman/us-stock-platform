#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[兼容桥接层] 原周度选股引擎已完整迁入 stock-platform/backend/app/services/screener_service.py 并落库 WeeklyTop5Snapshot。
原独立根脚本已归档至 archive/tech_stock_screener_v1_archived.py（详见 archive/README.md）。
本文件仅保留对平台 screener_service 的转发，确保外部脚本或旧调用零破坏。
"""
import sys
from pathlib import Path
try:
    import yfinance as yf
except ImportError:
    yf = None

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "stock-platform" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services import screener_service as _svc  # noqa: E402

HARD_TECH_UNIVERSE = _svc.HARD_TECH_UNIVERSE
simulate_pullback_winrate = _svc.simulate_pullback_winrate


def analyze_candidate(stock_meta: dict):
    return _svc.analyze_candidate(stock_meta, ticker_cls=yf.Ticker)


def run_screener():
    return _svc.run_screener(ticker_cls=yf.Ticker)


if __name__ == "__main__":
    run_screener()
