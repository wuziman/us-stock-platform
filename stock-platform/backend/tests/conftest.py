"""
测试全局配置：在导入任何 app 模块之前把数据库指向临时目录，
避免测试读写生产 stock_platform.db（此前测试直接写生产K线缓存、改动调度开关）。
"""
import os
import sys
import tempfile
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_REPO_ROOT = _BACKEND_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_DB_DIR = Path(tempfile.mkdtemp(prefix="stock_platform_test_"))
os.environ["STOCK_PLATFORM_DB"] = str(_DB_DIR / "test.db")

from app.database import Base, engine  # noqa: E402
from app import models, models_platform, models_tracking  # noqa: E402, F401
Base.metadata.create_all(bind=engine)

