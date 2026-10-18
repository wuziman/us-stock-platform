import importlib.util
import sys
from pathlib import Path

from .indicators import calculate_rsi, calculate_macd, calculate_bollinger_bands, calculate_ma

_tz_path = Path(__file__).resolve().parent / "timezone.py"
if _tz_path.exists() and f"{__name__}.timezone" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(f"{__name__}.timezone", _tz_path)
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{__name__}.timezone"] = _mod
        _spec.loader.exec_module(_mod)
        timezone = _mod
