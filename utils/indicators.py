# -*- coding: utf-8 -*-
"""
[已收敛至 stock-platform/backend/app/utils/indicators.py]
原 v1 独立技术指标模块已归档至 archive/utils_indicators_v1_archived.py。
本文件仅保留向后兼容转发，所有指标计算唯一真源为 stock-platform/backend/app/utils/indicators.py。
"""
import importlib.util
from pathlib import Path

_BACKEND_IND_PATH = (
    Path(__file__).resolve().parent.parent
    / "stock-platform"
    / "backend"
    / "app"
    / "utils"
    / "indicators.py"
)
_spec = importlib.util.spec_from_file_location("_backend_indicators", str(_BACKEND_IND_PATH))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

add_all_indicators = _mod.add_all_indicators
calculate_adx = _mod.calculate_adx
calculate_all_indicators = _mod.calculate_all_indicators
calculate_atr = _mod.calculate_atr
calculate_bollinger_bands = _mod.calculate_bollinger_bands
calculate_ema = _mod.calculate_ema
calculate_fibonacci_retracement = _mod.calculate_fibonacci_retracement
calculate_kdj = _mod.calculate_kdj
calculate_ma = _mod.calculate_ma
calculate_macd = _mod.calculate_macd
calculate_obv = _mod.calculate_obv
calculate_rsi = _mod.calculate_rsi
calculate_sma = _mod.calculate_sma
calculate_stochastic = _mod.calculate_stochastic
calculate_supertrend = _mod.calculate_supertrend
calculate_support_resistance = _mod.calculate_support_resistance
calculate_vwap = _mod.calculate_vwap
calculate_sma = _mod.calculate_sma
