from .stock_service import StockService
from .analysis_service import AnalysisService
from .backtest_service import BacktestService


import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _Path

_ss_path = _Path(__file__).resolve().parent / "screener_service.py"
if _ss_path.exists() and f"{__name__}.screener_service" not in _sys.modules:
    _spec = _ilu.spec_from_file_location(f"{__name__}.screener_service", _ss_path)
    if _spec and _spec.loader:
        _mod = _ilu.module_from_spec(_spec)
        _sys.modules[f"{__name__}.screener_service"] = _mod
        _spec.loader.exec_module(_mod)
        screener_service = _mod
