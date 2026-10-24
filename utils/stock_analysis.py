# -*- coding: utf-8 -*-
"""
[已收敛至 stock-platform/backend/app/services/analysis_service.py]
原 v1 独立股票分析器（1081行）已归档至 archive/stock_analysis_v1_archived.py。
本文件仅保留向后兼容转发，StockAnalyzer 唯一真源为 AnalysisService。
"""
import sys
from pathlib import Path
from typing import Dict

BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = BASE_DIR / "stock-platform" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.analysis_service import AnalysisService  # noqa: E402
from app.utils.market import detect_market  # noqa: E402


class StockAnalyzer:
    """向后兼容的股票综合分析器（包装 AnalysisService）"""

    def __init__(self, config_path: str = "config/config.json"):
        self.service = AnalysisService()

    def analyze_stock(self, stock_code: str, stock_name: str, mode: str = "simple") -> Dict:
        market = detect_market(stock_code)
        return self.service.analyze_stock(stock_code=stock_code, stock_name=stock_name, market=market, mode=mode)
