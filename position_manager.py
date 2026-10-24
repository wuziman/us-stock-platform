#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[兼容桥接层] 仓位管理核心类 PositionManager 与计算函数已收敛至
stock-platform/backend/app/services/position_manager.py。
本文件仅保留向后兼容转发与 CLI 入口，确保外部调用与旧脚本零破坏。
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "stock-platform" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.position_manager import (  # noqa: E402
    PositionManager,
    generate_position_plan,
    generate_add_position_plan,
    main,
)

if __name__ == "__main__":
    main()
