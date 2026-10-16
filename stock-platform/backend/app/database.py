"""
数据库配置
"""

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

# 基于本文件的绝对路径，避免从其他目录启动uvicorn时静默新建空库。
# 测试隔离：设置 STOCK_PLATFORM_DB 环境变量后所有连接指向指定库（tests/conftest.py 注入临时目录）
_DB_PATH = Path(os.environ.get("STOCK_PLATFORM_DB", "") or (Path(__file__).resolve().parents[1] / "stock_platform.db"))
SQLALCHEMY_DATABASE_URL = f"sqlite:///{_DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30}  # 写锁等待30秒
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """启用WAL日志模式：读写不互斥，缓解APScheduler线程与请求线程并发写的锁冲突"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
